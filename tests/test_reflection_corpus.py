"""Observation corpus — passive ingest fidelity + active reserved-base draws.

Passive: an ingest over a workspace built with the R1 writers stamps
``verbatim`` when a ``judge_io.jsonl`` sidecar is present, degrades to
``preview`` for a legacy run with only ``loss.json``, and REFERENCES the
artifacts by path (never copies their bytes).

Active: draws land at ``REFLECTION_REPLICATE_BASE + j`` (asserted on the cache
slot filenames), a SECOND run is all cache HITs (zero ``_run_single`` calls),
and an infra abort on any unit VOIDS the draw with
:class:`ReflectionDrawInconclusive`.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

import zicato.tournament.runner as runner_mod
from zicato.core import (
    BoardEntry,
    DriftCount,
    Generation,
    JudgeLoss,
    LossProfile,
    RuntimeConfig,
    ScoringWeights,
)
from zicato.core.workspace import loss_profile_path, run_dir
from zicato.judge_runtime.io_capture import JudgeIOFileSink, judge_io_path_for_loss
from zicato.reflection.corpus import (
    FIDELITY_PREVIEW,
    FIDELITY_VERBATIM,
    ObservationRun,
    ReflectionDrawInconclusive,
    ingest_lineage,
    read_corpus,
    run_corpus,
)
from zicato.reflection.plan import new_plan, read_plan
from zicato.tournament.unit_cache import _unit_loss_path, unit_events_path, unit_result_path

EPOCH = "epoch-1"
CREATED_AT = "2026-07-01T00:00:00+00:00"

TRANSCRIPT_SENTINEL = "VERBATIM-TRANSCRIPT-abc123-do-not-copy-into-corpus"


def _loss(
    *,
    generation_id: str,
    entry_id: str,
    drift_loss: float = 2.0,
    pass_fail: bool | None = True,
    judge_losses: tuple[JudgeLoss, ...] = (),
    drift_counts: tuple[DriftCount, ...] = (),
) -> LossProfile:
    return LossProfile(
        run_id=f"run-{generation_id}-{entry_id}",
        entry_id=entry_id,
        generation_id=generation_id,
        epoch_id=EPOCH,
        drift_counts=drift_counts,
        plan_revisions=0,
        task_failure_ratio=0.0,
        runtime_ms=42,
        wall_clock_budget_exceeded=False,
        expectation_result=None,
        drift_loss=drift_loss,
        pass_fail=pass_fail,
        per_judge_loss=judge_losses,
    )


def _write_loss(workspace: Path, gen: str, entry: str, replicate: int, loss: LossProfile) -> Path:
    from zicato.telemetry import reducer

    path = _unit_loss_path(workspace, EPOCH, gen, entry, replicate)
    reducer.write_loss_profile(loss, path)
    return path


def _write_result_json(loss_path: Path) -> None:
    """A minimal valid result.json (the R1 result-tier capture)."""
    payload = {
        "format_version": 1,
        "run_id": "run-x",
        "entry_id": "entryA",
        "final_output": TRANSCRIPT_SENTINEL,
        "transcript": [TRANSCRIPT_SENTINEL],
        "runtime_ms": 42,
        "aborted": False,
        "abort_reason": "",
        "clipped": False,
    }
    unit_result_path(loss_path).write_text(json.dumps(payload), encoding="utf-8")


def _write_judge_io(loss_path: Path, *, fired: bool) -> None:
    """One verbatim judge_io.jsonl record via the R1 sink."""
    sink = JudgeIOFileSink(judge_io_path_for_loss(loss_path))
    sink.record(
        "citation_judge",
        reasoning_text=TRANSCRIPT_SENTINEL,
        transcript_window=(TRANSCRIPT_SENTINEL,),
        raw_response="{}",
        drift_emitted=fired,
        kind="custom:citation_judge",
        severity="warning" if fired else "info",
        detail="missing citation" if fired else "",
    )


# ---------------------------------------------------------------------------
# Passive ingest — fidelity tiers + references-not-copies
# ---------------------------------------------------------------------------


def test_passive_ingest_verbatim_tier_when_judge_io_present(tmp_path: Path) -> None:
    workspace = tmp_path / ".zicato"
    loss = _loss(
        generation_id="v1",
        entry_id="entryA",
        judge_losses=(JudgeLoss("citation_judge", raw_loss=3.0, weight=1.0, weighted_loss=3.0),),
        drift_counts=(DriftCount(kind="custom:citation_judge", severity="warning", count=1),),
    )
    loss_path = _write_loss(workspace, "v1", "entryA", 0, loss)
    _write_result_json(loss_path)
    _write_judge_io(loss_path, fired=True)

    runs = ingest_lineage(
        workspace_root=workspace,
        epoch_id=EPOCH,
        reflection_id="refl-x",
        candidates=["v1"],
        entries=["entryA"],
        weights=ScoringWeights(),
    )
    assert len(runs) == 1
    obs = runs[0]
    assert obs.fidelity == FIDELITY_VERBATIM
    assert obs.has_judge_io is True
    assert obs.has_result is True
    # Verbatim judge decision carried out (fired + rationale + span ref).
    assert obs.judge_decisions[0]["judge_name"] == "citation_judge"
    assert obs.judge_decisions[0]["fired"] is True
    assert obs.judge_decisions[0]["transcript_span"] is not None
    # loss_decomposition draws from per_judge_loss.
    assert obs.loss_decomposition["judge:citation_judge"] == 3.0


def test_passive_ingest_preview_tier_for_legacy_run_without_result(tmp_path: Path) -> None:
    workspace = tmp_path / ".zicato"
    loss = _loss(generation_id="v0", entry_id="entryA")
    _write_loss(workspace, "v0", "entryA", 0, loss)  # loss.json only — legacy

    runs = ingest_lineage(
        workspace_root=workspace,
        epoch_id=EPOCH,
        reflection_id="refl-x",
        candidates=["v0"],
        entries=["entryA"],
        weights=ScoringWeights(),
    )
    assert len(runs) == 1
    assert runs[0].fidelity == FIDELITY_PREVIEW
    assert runs[0].has_result is False
    assert runs[0].has_judge_io is False


def test_passive_ingest_references_artifacts_never_copies(tmp_path: Path) -> None:
    workspace = tmp_path / ".zicato"
    loss = _loss(generation_id="v1", entry_id="entryA")
    loss_path = _write_loss(workspace, "v1", "entryA", 0, loss)
    _write_result_json(loss_path)

    runs = ingest_lineage(
        workspace_root=workspace,
        epoch_id=EPOCH,
        reflection_id="refl-x",
        candidates=["v1"],
        entries=["entryA"],
        weights=ScoringWeights(),
    )
    obs = runs[0]
    # References are PATHS to the real artifacts.
    assert obs.loss_ref == str(loss_path)
    assert obs.transcript_ref == str(unit_result_path(loss_path))
    # The verbatim transcript bytes are NOT duplicated into the record.
    assert TRANSCRIPT_SENTINEL not in json.dumps(obs.to_json())


def test_passive_ingest_picks_up_calibration_replicate_slots(tmp_path: Path) -> None:
    """loss.r1000 (a free A/A calibration replicate) is ingested too."""
    workspace = tmp_path / ".zicato"
    for replicate in (0, 1000, 1001):
        loss_path = _write_loss(
            workspace, "v1", "entryA", replicate, _loss(generation_id="v1", entry_id="entryA")
        )
        unit_events_path(loss_path).write_text("{}\n", encoding="utf-8")

    runs = ingest_lineage(
        workspace_root=workspace,
        epoch_id=EPOCH,
        reflection_id="refl-x",
        candidates=["v1"],
        entries=["entryA"],
        weights=ScoringWeights(),
    )
    assert sorted(o.replicate for o in runs) == [0, 1000, 1001]
    refs = {o.replicate: Path(o.transcript_ref or "").name for o in runs}
    assert refs == {0: "events.jsonl", 1000: "events.r1000.jsonl", 1001: "events.r1001.jsonl"}


def test_passive_ingest_reserved_base_allowlist_excludes_degraded_probes(tmp_path: Path) -> None:
    """B1: r0/1000/4000/5000 are ingested; r2000 (preflight) + r3000 (screen) excluded."""
    workspace = tmp_path / ".zicato"
    for replicate in (0, 1000, 2000, 3000, 4000, 5000):
        _write_loss(
            workspace, "v1", "entryA", replicate, _loss(generation_id="v1", entry_id="entryA")
        )

    runs = ingest_lineage(
        workspace_root=workspace,
        epoch_id=EPOCH,
        reflection_id="refl-x",
        candidates=["v1"],
        entries=["entryA"],
        weights=ScoringWeights(),
    )
    ingested = sorted(o.replicate for o in runs)
    # r1000 (calibration), r4000 (evidence), r5000 (reflection), r0 (duel) IN;
    # r2000 (preflight degraded probe) + r3000 (screen base) OUT.
    assert ingested == [0, 1000, 4000, 5000]
    assert 2000 not in ingested
    assert 3000 not in ingested


def test_reserved_base_filter_is_an_allowlist() -> None:
    """An unclaimed index answers False, so a band added later starts EXCLUDED.

    The filter is shared with the proposer's baseline reader, where admitting
    an unattributed slot by default would let a future degraded-probe band
    reach the prompt as champion behaviour.
    """
    from zicato.tournament.unit_cache import is_own_code_board_draw

    # Every claimed own-code base, at both ends of its block.
    for index in (0, 1, 999, 1000, 1999, 4000, 4999, 5000, 5999, 6000, 6999):
        assert is_own_code_board_draw(index), index
    # The pre-flight's degraded probes and the screen's panel-subset draws.
    for index in (2000, 2999, 3000, 3001, 3999):
        assert not is_own_code_board_draw(index), index
    # Unclaimed above the ledger, and defensively below it.
    for index in (7000, 10_000, -1):
        assert not is_own_code_board_draw(index), index


def test_observation_run_json_round_trip() -> None:
    obs = ObservationRun(
        reflection_id="refl-x",
        candidate_id="v1",
        entry_id="entryA",
        replicate=5000,
        scalar=1.5,
        drift_loss=2.0,
        pass_fail=True,
        runtime_ms=42,
        aborted=False,
        abort_cause=None,
        fidelity=FIDELITY_VERBATIM,
        has_result=True,
        has_judge_io=True,
        loss_ref="/x/loss.json",
        transcript_ref="/x/result.json",
        drift_events=({"kind": "off_topic", "severity": "info", "judge_name": "", "count": 1},),
        judge_decisions=({"judge_name": "j", "fired": True},),
        loss_decomposition={"judge:j": 1.0},
    )
    assert ObservationRun.from_json(obs.to_json()) == obs


# ---------------------------------------------------------------------------
# Active corpus — reserved base 5000+j, cache idempotency, infra-abort void
# ---------------------------------------------------------------------------


def _board() -> list[BoardEntry]:
    return [
        BoardEntry(id="entryA", kind="single_turn", wall_clock_budget_seconds=10),
        BoardEntry(id="entryB", kind="single_turn", wall_clock_budget_seconds=10),
    ]


def _generation(gen_id: str, snapshot: Path) -> Generation:
    return Generation(
        id=gen_id, epoch_id=EPOCH, parent_id=None, snapshot_root=snapshot, created_at=""
    )


def _config(workspace: Path) -> RuntimeConfig:
    return RuntimeConfig(
        instance_id="default",
        workspace_root=workspace,
        target_call_llm=None,
        evaluation_call_llm=None,
    )


class _CountingRunSingle:
    """A stub ``_run_single`` that counts calls and returns a clean loss."""

    def __init__(self, *, abort_cause: str | None = None) -> None:
        self.calls = 0
        self._abort_cause = abort_cause

    async def __call__(
        self,
        *,
        adapter: object,
        generation: Generation,
        entry: BoardEntry,
        weights: object,
        config: object,
        workspace_root: Path,
        epoch_id: str,
        side: str,
        match_id: str = "",
    ) -> LossProfile:
        self.calls += 1
        return LossProfile(
            run_id=f"run-{generation.id}-{entry.id}",
            entry_id=entry.id,
            generation_id=generation.id,
            epoch_id=epoch_id,
            drift_counts=(),
            plan_revisions=0,
            task_failure_ratio=0.0,
            runtime_ms=10,
            wall_clock_budget_exceeded=False,
            expectation_result=None,
            drift_loss=1.0,
            pass_fail=True,
            abort_cause=self._abort_cause,
        )


def _run(coro):  # pragma: no cover - trivial helper
    return asyncio.run(coro)


def test_active_corpus_lands_draws_at_reserved_base(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / ".zicato"
    stub = _CountingRunSingle()
    monkeypatch.setattr(runner_mod, "_run_single", stub)

    plan = new_plan(
        epoch_id=EPOCH,
        candidates=["v1"],
        entries=["entryA", "entryB"],
        replicates=3,
        created_at=CREATED_AT,
        token="active",
    )
    runs = _run(
        run_corpus(
            adapter=object(),
            plan=plan,
            generations=[_generation("v1", tmp_path / "snap")],
            board=_board(),
            weights=ScoringWeights(),
            config=_config(workspace),
            workspace_root=workspace,
        )
    )

    # 1 candidate x 2 entries x 3 replicates = 6 observations, all at base 5000+j.
    assert len(runs) == 6
    assert {o.replicate for o in runs} == {5000, 5001, 5002}
    # The cache slot filenames prove the reserved base.
    rundir = run_dir(workspace, EPOCH, "v1", "entryA")
    assert (rundir / "loss.r5000.json").exists()
    assert (rundir / "loss.r5001.json").exists()
    assert (rundir / "loss.r5002.json").exists()
    # r0 (the canonical tournament slot) is NEVER touched.
    assert not loss_profile_path(workspace, EPOCH, "v1", "entryA").exists()
    # First run: a fresh _run_single per (entry, replicate).
    assert stub.calls == 6

    # Persistence: corpus.jsonl written + plan marked executed.
    persisted = read_corpus(workspace, EPOCH, plan.reflection_id)
    assert len(persisted) == 6
    reloaded = read_plan(workspace, EPOCH, plan.reflection_id)
    assert reloaded is not None and reloaded.executed is True


def test_active_corpus_second_run_is_all_cache_hits(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / ".zicato"
    stub = _CountingRunSingle()
    monkeypatch.setattr(runner_mod, "_run_single", stub)

    plan = new_plan(
        epoch_id=EPOCH,
        candidates=["v1"],
        entries=["entryA", "entryB"],
        replicates=2,
        created_at=CREATED_AT,
        token="idem",
    )
    args = dict(
        adapter=object(),
        plan=plan,
        generations=[_generation("v1", tmp_path / "snap")],
        board=_board(),
        weights=ScoringWeights(),
        config=_config(workspace),
        workspace_root=workspace,
    )
    first = _run(run_corpus(**args))
    assert stub.calls == 4  # 2 entries x 2 replicates, all fresh

    stub.calls = 0
    second = _run(run_corpus(**args))
    # A re-run of the same frozen plan is ALL cache HITs — zero fresh runs.
    assert stub.calls == 0
    assert [o.to_json() for o in second] == [o.to_json() for o in first]


def test_active_corpus_infra_abort_voids_the_draw(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / ".zicato"
    # An infra abort cause (not a budget exhaustion) on every unit.
    stub = _CountingRunSingle(abort_cause="nonzero_exit:1")
    monkeypatch.setattr(runner_mod, "_run_single", stub)

    plan = new_plan(
        epoch_id=EPOCH,
        candidates=["v1"],
        entries=["entryA"],
        replicates=2,
        created_at=CREATED_AT,
        token="abort",
    )
    with pytest.raises(ReflectionDrawInconclusive):
        _run(
            run_corpus(
                adapter=object(),
                plan=plan,
                generations=[_generation("v1", tmp_path / "snap")],
                board=[_board()[0]],
                weights=ScoringWeights(),
                config=_config(workspace),
                workspace_root=workspace,
            )
        )
    # Infra aborts are never cached, so nothing was persisted at the slot.
    assert not (run_dir(workspace, EPOCH, "v1", "entryA") / "loss.r5000.json").exists()
