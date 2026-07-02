"""Per-board-run tournament provenance: each board RUN is tagged with the
``match_id`` (and a derived ``rung``) of the matchup it executed within.

Three layers are exercised, all without a live evolve run:

1. **Threading** — ``run_matchup(match_id=...)`` threads the tag down to
   every board-entry run (the stubbed ``_run_single`` sees it), and the
   runner stamps it onto the persisted ``LossProfile`` + rewrites
   ``loss.json`` so a later full reindex re-derives it.
2. **Derivation** — ``rung_for_match_id`` projects a ``match_id`` to its
   coarse rung/phase label.
3. **Exposure** — ``/api/.../per-entry`` (``build_per_entry_for_generation``)
   carries ``match_id`` + ``rung`` per run, and an untagged legacy run
   yields ``None`` (not an error).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import zicato.tournament.runner as runner_mod
from zicato.core import (
    BoardEntry,
    Generation,
    LossProfile,
    RuntimeConfig,
    ScoringWeights,
)
from zicato.core.types import DriftCount, ExpectationResult
from zicato.core.workspace import loss_profile_path
from zicato.dashboard.state_reader import WorkspacePaths, build_per_entry_for_generation
from zicato.epoch.journal import write_experiment
from zicato.epoch.lifecycle import new_epoch
from zicato.epoch.lineage import append_to_lineage
from zicato.index.ingest import rebuild_index
from zicato.selection.strategy import rung_for_match_id
from zicato.telemetry.reducer import read_loss_profile, write_loss_profile
from zicato.testing.fixtures import make_experiment, make_outcome_record
from zicato.tournament.runner import run_matchup

# ---------------------------------------------------------------------------
# 1. rung derivation
# ---------------------------------------------------------------------------


def test_rung_for_match_id_maps_racing_forms() -> None:
    assert rung_for_match_id("rung0_m2") == "rung 0"
    assert rung_for_match_id("rung1_m0") == "rung 1"
    assert rung_for_match_id("rung12_m3") == "rung 12"
    assert rung_for_match_id("racing-final") == "final"
    assert rung_for_match_id("final") == "final"
    # Untagged -> None (gauntlet duel / legacy run), never an error.
    assert rung_for_match_id("") is None
    assert rung_for_match_id(None) is None
    # Non-racing structures keep a stable verbatim label.
    assert rung_for_match_id("WB-R1-0") == "WB-R1-0"


# ---------------------------------------------------------------------------
# 2. runner threads match_id down to each board run
# ---------------------------------------------------------------------------


async def _harness_call(system: str, user: str, model: str) -> str:
    return ""


async def _aux_call(system: str, user: str, model: str) -> str:
    return ""


def _config(tmp_path: Path) -> RuntimeConfig:
    return RuntimeConfig(
        instance_id="test",
        workspace_root=tmp_path,
        harness_call_llm=_harness_call,
        auxiliary_call_llm=_aux_call,
    )


def _gen(tmp_path: Path, gen_id: str) -> Generation:
    return Generation(
        id=gen_id,
        epoch_id="e0",
        parent_id=None,
        snapshot_root=tmp_path / f"snap_{gen_id}",
        created_at="2024-01-01T00:00:00Z",
    )


def _board() -> list[BoardEntry]:
    return [
        BoardEntry(id="entry_a", kind="single_turn", wall_clock_budget_seconds=60, input="x"),
    ]


def test_run_matchup_threads_match_id_to_each_run(monkeypatch, tmp_path) -> None:
    """The match_id reaches every board-entry run via run_matchup."""
    seen: list[str] = []

    async def fake_run_single(
        *, adapter, generation, entry, weights, config, workspace_root, epoch_id, side, match_id=""
    ):
        del adapter, weights, config, workspace_root, epoch_id, side
        seen.append(match_id)
        return LossProfile(
            run_id=f"run-{generation.id}-{entry.id}",
            entry_id=entry.id,
            generation_id=generation.id,
            epoch_id="e0",
            drift_counts=(DriftCount(kind="off_topic", severity="info", count=0),),
            plan_revisions=0,
            task_failure_ratio=0.0,
            runtime_ms=1000,
            wall_clock_budget_exceeded=False,
            expectation_result=ExpectationResult(kind="predicate", passed=True),
            drift_loss=1.0,
            pass_fail=True,
            match_id=match_id,
        )

    monkeypatch.setattr(runner_mod, "_run_single", fake_run_single)
    asyncio.run(
        run_matchup(
            adapter=object(),
            left_gen=_gen(tmp_path, "v0"),
            right_gen=_gen(tmp_path, "v1"),
            board=_board(),
            weights=ScoringWeights(),
            config=_config(tmp_path),
            workspace_root=tmp_path,
            epoch_id="e0",
            match_id="rung0_m2",
        )
    )
    # Both sides of the single board entry saw the matchup id.
    assert seen == ["rung0_m2", "rung0_m2"]


def test_run_matchup_stamps_judge_only_onto_each_entry(monkeypatch, tmp_path) -> None:
    """The racing/run_matchup path honours a board-level ``judge_only=True``.

    Regression guard: ``judge_only`` was stamped by the gauntlet paths
    (``run_tournament`` / ``run_fast_mode``) but NOT by ``run_matchup`` —
    the racing / multi-challenger path — so racing entries reached the
    adapter without the flag and the default steering planner ran despite
    judge-only mode. We stub ``_run_single`` (no live LLM) and assert the
    entries it receives carry ``judge_only`` in their context, i.e.
    ``_stamp_judge_only`` ran on this path. FAILS on the pre-fix runner
    (run_matchup had no judge_only parameter); passes after.
    """
    seen_contexts: list[dict[str, str]] = []

    async def fake_run_single(
        *, adapter, generation, entry, weights, config, workspace_root, epoch_id, side, match_id=""
    ):
        del adapter, generation, weights, config, workspace_root, epoch_id, side, match_id
        seen_contexts.append(dict(entry.context))
        return LossProfile(
            run_id="run-x",
            entry_id=entry.id,
            generation_id="v0",
            epoch_id="e0",
            drift_counts=(DriftCount(kind="off_topic", severity="info", count=0),),
            plan_revisions=0,
            task_failure_ratio=0.0,
            runtime_ms=1000,
            wall_clock_budget_exceeded=False,
            expectation_result=ExpectationResult(kind="predicate", passed=True),
            drift_loss=1.0,
            pass_fail=True,
        )

    monkeypatch.setattr(runner_mod, "_run_single", fake_run_single)
    asyncio.run(
        run_matchup(
            adapter=object(),
            left_gen=_gen(tmp_path, "v0"),
            right_gen=_gen(tmp_path, "v1"),
            board=_board(),
            weights=ScoringWeights(),
            config=_config(tmp_path),
            workspace_root=tmp_path,
            epoch_id="e0",
            judge_only=True,
        )
    )
    # One entry, two sides (champion + challenger) => two runs, both stamped.
    assert seen_contexts, "no board runs were scheduled"
    assert all(ctx.get("judge_only") == "true" for ctx in seen_contexts)


def test_run_matchup_default_leaves_judge_only_unset(monkeypatch, tmp_path) -> None:
    """Default (steering) racing run is byte-identical: no judge_only stamp."""
    seen_contexts: list[dict[str, str]] = []

    async def fake_run_single(
        *, adapter, generation, entry, weights, config, workspace_root, epoch_id, side, match_id=""
    ):
        del adapter, generation, weights, config, workspace_root, epoch_id, side, match_id
        seen_contexts.append(dict(entry.context))
        return LossProfile(
            run_id="run-x",
            entry_id=entry.id,
            generation_id="v0",
            epoch_id="e0",
            drift_counts=(DriftCount(kind="off_topic", severity="info", count=0),),
            plan_revisions=0,
            task_failure_ratio=0.0,
            runtime_ms=1000,
            wall_clock_budget_exceeded=False,
            expectation_result=ExpectationResult(kind="predicate", passed=True),
            drift_loss=1.0,
            pass_fail=True,
        )

    monkeypatch.setattr(runner_mod, "_run_single", fake_run_single)
    asyncio.run(
        run_matchup(
            adapter=object(),
            left_gen=_gen(tmp_path, "v0"),
            right_gen=_gen(tmp_path, "v1"),
            board=_board(),
            weights=ScoringWeights(),
            config=_config(tmp_path),
            workspace_root=tmp_path,
            epoch_id="e0",
        )
    )
    assert seen_contexts, "no board runs were scheduled"
    assert all("judge_only" not in ctx for ctx in seen_contexts)


def test_run_single_stamps_match_id_onto_loss_json(monkeypatch, tmp_path) -> None:
    """_run_single rewrites loss.json with the match_id so reindex re-derives it.

    The worker subprocess is stubbed out: we pre-write the worker's
    ``loss.json`` (untagged, as the worker would) and a result file, then
    drive ``_run_single`` with ``match_id`` set and assert the on-disk
    profile comes back tagged.
    """
    ws = tmp_path / ".zicato"
    epoch_id, gen_id, entry_id = "e0", "v1", "entry_a"
    lpath = loss_profile_path(ws, epoch_id, gen_id, entry_id)
    base = LossProfile(
        run_id=f"{gen_id}--{entry_id}",
        entry_id=entry_id,
        generation_id=gen_id,
        epoch_id=epoch_id,
        drift_counts=(),
        plan_revisions=0,
        task_failure_ratio=0.0,
        runtime_ms=10,
        wall_clock_budget_exceeded=False,
        expectation_result=None,
        drift_loss=1.0,
        pass_fail=None,
    )
    write_loss_profile(base, lpath)
    assert read_loss_profile(lpath).match_id == ""  # the worker left it blank

    # Stub the subprocess machinery: no checkout, no spawn, a clean exit
    # whose result file points at the pre-written loss.json.
    from zicato.epoch.genstore import EphemeralCheckout

    monkeypatch.setattr(
        runner_mod,
        "_checkout_run_snapshot",
        lambda **kwargs: EphemeralCheckout(
            working_dir=tmp_path / "snap",
            scratch_dir=tmp_path / "scratch",
            cleanup=lambda: None,
        ),
    )
    monkeypatch.setattr(runner_mod, "_discard_run_snapshot", lambda c: None)
    monkeypatch.setattr(runner_mod, "_ingest_run_into_index", lambda *a, **k: None)

    class _Proc:
        returncode = 0

        async def wait(self) -> int:
            return 0

    async def fake_spawn(*args, **kwargs):
        return _Proc()

    monkeypatch.setattr(runner_mod.asyncio, "create_subprocess_exec", fake_spawn)
    # The worker result file is read via _load_worker_result; point it at
    # the loss.json we pre-wrote.
    monkeypatch.setattr(
        runner_mod,
        "_load_worker_result",
        lambda result_path: {"loss_profile_path": str(lpath)},
    )

    gen = Generation(
        id=gen_id,
        epoch_id=epoch_id,
        parent_id=None,
        snapshot_root=tmp_path / "snap_v1",
        created_at="2024-01-01T00:00:00Z",
    )

    class _Adapter:
        def worker_spec(self) -> dict:
            return {"kind": "stub"}

    result = asyncio.run(
        runner_mod._run_single(
            adapter=_Adapter(),
            generation=gen,
            entry=_board()[0],
            weights=ScoringWeights(),
            config=_config(tmp_path),
            workspace_root=ws,
            epoch_id=epoch_id,
            side="child",
            match_id="rung1_m0",
        )
    )
    # The returned in-memory profile is tagged...
    assert result.match_id == "rung1_m0"
    # ...AND the on-disk loss.json was rewritten so a full reindex re-derives it.
    assert read_loss_profile(lpath).match_id == "rung1_m0"


# ---------------------------------------------------------------------------
# 3. per-entry API exposes match_id + rung (and tolerates legacy untagged)
# ---------------------------------------------------------------------------


def _seed_lineage(ws: Path, epoch_id: str) -> None:
    g0 = Generation(
        id="v0",
        epoch_id=epoch_id,
        parent_id=None,
        snapshot_root=Path("/tmp/snap/v0"),
        created_at="2026-01-01T00:00:00Z",
        promoted=True,
    )
    g1 = Generation(
        id="v1",
        epoch_id=epoch_id,
        parent_id="v0",
        snapshot_root=Path("/tmp/snap/v1"),
        created_at="2026-01-02T00:00:00Z",
        promoted=True,
    )
    append_to_lineage(ws, epoch_id, g0, None)
    append_to_lineage(ws, epoch_id, g1, "v0")


def _build_workspace_with_tagged_run(tmp_path: Path) -> tuple[Path, str]:
    """One epoch; v1 has a tagged run (rung0_m1) and v0 has an untagged run."""
    ws = tmp_path / ".zicato"
    board = tmp_path / "board.jsonl"
    board.write_text(
        '{"id": "entry_a", "kind": "single_turn", '
        '"wall_clock_budget_seconds": 60, "input": "hi"}\n',
        encoding="utf-8",
    )
    rubric = tmp_path / "rubric.md"
    rubric.write_text("# rubric\n", encoding="utf-8")

    cfg = new_epoch(ws, "alpha", board, rubric, ScoringWeights())
    eid = cfg.id
    _seed_lineage(ws, eid)
    exp = make_experiment(
        epoch_id=eid,
        generation_id="v1",
        parent_generation_id="v0",
        outcome=make_outcome_record(),
    )
    write_experiment(ws, eid, "v1", exp)

    # v1 run: tagged with a matchup id (the challenger ran within rung0_m1).
    tagged = LossProfile(
        run_id=f"run_{eid}_v1_entry_a",
        entry_id="entry_a",
        generation_id="v1",
        epoch_id=eid,
        drift_counts=(),
        plan_revisions=0,
        task_failure_ratio=0.0,
        runtime_ms=1234,
        wall_clock_budget_exceeded=False,
        expectation_result=None,
        drift_loss=2.5,
        pass_fail=None,
        match_id="rung0_m1",
    )
    write_loss_profile(tagged, loss_profile_path(ws, eid, "v1", "entry_a"))

    # v0 run: a legacy / untagged run (match_id left at the default "").
    untagged = LossProfile(
        run_id=f"run_{eid}_v0_entry_a",
        entry_id="entry_a",
        generation_id="v0",
        epoch_id=eid,
        drift_counts=(),
        plan_revisions=0,
        task_failure_ratio=0.0,
        runtime_ms=999,
        wall_clock_budget_exceeded=False,
        expectation_result=None,
        drift_loss=3.0,
        pass_fail=None,
    )
    write_loss_profile(untagged, loss_profile_path(ws, eid, "v0", "entry_a"))
    return ws, eid


def test_per_entry_exposes_match_id_and_rung(tmp_path) -> None:
    ws, eid = _build_workspace_with_tagged_run(tmp_path)
    rebuild_index(ws)

    payload = build_per_entry_for_generation(WorkspacePaths(ws), eid, "v1")
    assert len(payload["entries"]) == 1
    entry = payload["entries"][0]
    # The exact additive contract the UI reads.
    assert entry["match_id"] == "rung0_m1"
    assert entry["rung"] == "rung 0"


def test_per_entry_legacy_untagged_run_yields_null(tmp_path) -> None:
    ws, eid = _build_workspace_with_tagged_run(tmp_path)
    rebuild_index(ws)

    # v0's run was never tagged — match_id/rung must be None, not an error.
    payload = build_per_entry_for_generation(WorkspacePaths(ws), eid, "v0")
    assert len(payload["entries"]) == 1
    entry = payload["entries"][0]
    assert entry["match_id"] is None
    assert entry["rung"] is None


def test_per_entry_cached_provenance_keys_default_safely(tmp_path) -> None:
    """The cached-champion provenance keys are additive + tolerant.

    The per-entry payload always carries ``cached`` / ``source_epoch`` /
    ``source_run`` so the dashboard can render a cached-champion badge. On an
    index whose ``loss_profiles`` rows do NOT (yet) carry those columns the
    read tolerates the absence — ``cached`` defaults to ``False`` and the
    ``source_*`` fields to ``None``, never raising.
    """
    ws, eid = _build_workspace_with_tagged_run(tmp_path)
    rebuild_index(ws)

    payload = build_per_entry_for_generation(WorkspacePaths(ws), eid, "v1")
    assert len(payload["entries"]) == 1
    entry = payload["entries"][0]
    # the keys are ALWAYS present (the UI reads them unconditionally)...
    assert "cached" in entry
    assert "source_epoch" in entry
    assert "source_run" in entry
    # ...and default safely for a freshly-executed (non-cached) run.
    assert entry["cached"] is False
    assert entry["source_epoch"] is None
    assert entry["source_run"] is None


def test_reindex_carries_match_id_into_index(tmp_path) -> None:
    """A full reindex re-derives match_id straight from loss.json."""
    from zicato.index.query import loss_profiles_for_generation

    ws, eid = _build_workspace_with_tagged_run(tmp_path)
    db = rebuild_index(ws)

    rows = loss_profiles_for_generation(db, eid, "v1")
    assert len(rows) == 1
    assert rows[0]["match_id"] == "rung0_m1"

    legacy = loss_profiles_for_generation(db, eid, "v0")
    assert len(legacy) == 1
    assert legacy[0]["match_id"] is None
