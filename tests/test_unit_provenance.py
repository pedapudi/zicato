"""Per-unit provenance: wall-clock span, attempt records, penalty attribution.

Three durable facts a board unit's record did not carry:

* WHEN the unit ran (:attr:`LossProfile.started_at` / ``ended_at``). A
  duration alone cannot order two units, place one on a timeline, or show
  which ran concurrently.
* THAT a unit ran more than once. The cache keeps one profile per unit, so
  a unit that failed twice and then passed used to be indistinguishable
  from one that passed first time. Sibling ``loss.a{n}.json`` files keep
  the executions the canonical slot does not survive to show.
* WHY the reducer scored a run worst-case
  (:attr:`LossProfile.not_completed_reason`). The not-completed penalty
  lands as a large ``drift_loss`` beside an empty ``metric_counts``; without
  the reason nothing in the record accounts for the number.

The penalty's ARITHMETIC is unchanged — the attribution is additive. The
end-to-end proof that a crashed run still scores exactly 60.0 under the
default weights lives in
:func:`tests.test_subprocess_workers.test_worker_penalises_aborted_run_in_loss_json`,
which also asserts the new attribution on a real worker's ``loss.tournament.r0.json``.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

import zicato.tournament.worker_execution as _tournament_worker_execution
from zicato.core import (
    BUDGET_ABORT_CAUSE,
    BoardEntry,
    Generation,
    LossProfile,
    RuntimeConfig,
    ScoringWeights,
)
from zicato.core.measurement import MeasurementDraw, MeasurementPurpose, measurement_artifact_path
from zicato.core.workspace import run_id_for_unit
from zicato.runtime.lock import acquire_workspace_lock
from zicato.telemetry.reducer import (
    loss_profile_from_dict,
    read_loss_profile,
    write_loss_profile,
)
from zicato.testing.fixtures import make_loss_profile
from zicato.tournament.scheduling import _run_unit_cache_first
from zicato.tournament.unit_cache import (
    _average_losses,
    _persist_unit_loss,
    _unit_loss_path,
    is_unit_attempt_slot,
    record_unit_attempt,
    unit_result_path,
)
from zicato.tournament.worker_transport import _aborted_loss_profile

_EPOCH = "e0"


def _entry(entry_id: str = "entry_a") -> BoardEntry:
    return BoardEntry(
        id=entry_id,
        kind="single_turn",
        wall_clock_budget_seconds=60,
        input="hello",
    )


def _generation(tmp_path: Path, gen_id: str = "v0") -> Generation:
    return Generation(
        id=gen_id,
        epoch_id=_EPOCH,
        parent_id=None,
        snapshot_root=tmp_path / f"snap_{gen_id}",
        created_at="2026-01-01T00:00:00Z",
    )


def _runtime_config(workspace: Path) -> RuntimeConfig:
    async def _call(system: str, user: str, model: str) -> str:
        del system, user, model
        return ""

    return RuntimeConfig(
        instance_id="test",
        workspace_root=workspace,
        target_call_llm=_call,
        evaluation_call_llm=_call,
    )


def _stub_run_single(monkeypatch: pytest.MonkeyPatch, profiles: list[LossProfile]) -> list[str]:
    """Serve ``profiles`` in order to successive ``_run_single`` calls."""
    calls: list[str] = []
    queue = list(profiles)

    async def fake_run_single(
        *,
        adapter: Any,
        generation: Generation,
        entry: BoardEntry,
        weights: ScoringWeights,
        config: RuntimeConfig,
        writer: Any,
        workspace_root: Path,
        epoch_id: str,
        side: str,
        match_id: str = "",
    ) -> LossProfile:
        del adapter, weights, config, workspace_root, epoch_id, side, match_id
        calls.append(generation.id)
        return queue.pop(0)

    monkeypatch.setattr(_tournament_worker_execution, "_run_single", fake_run_single)
    return calls


def _evaluate(
    workspace: Path,
    generation: Generation,
    entry: BoardEntry,
    *,
    force_fresh: bool = False,
) -> LossProfile:
    with acquire_workspace_lock(workspace, "test") as writer:
        return asyncio.run(
            _run_unit_cache_first(
                writer=writer,
                adapter=object(),
                generation=generation,
                entry=entry,
                weights=ScoringWeights(),
                config=_runtime_config(workspace),
                workspace_root=workspace,
                epoch_id=_EPOCH,
                side="parent",
                force_fresh=force_fresh,
            )
        )


# ---------------------------------------------------------------------------
# Wall-clock span
# ---------------------------------------------------------------------------


def test_timestamps_survive_the_loss_json_round_trip(tmp_path: Path) -> None:
    profile = make_loss_profile(
        started_at="2026-08-18T10:00:00Z",
        ended_at="2026-08-18T10:00:42Z",
    )
    path = tmp_path / "loss.tournament.r0.json"
    write_loss_profile(profile, path)

    read_back = read_loss_profile(path)
    assert read_back.started_at == "2026-08-18T10:00:00Z"
    assert read_back.ended_at == "2026-08-18T10:00:42Z"


def test_a_cached_unit_keeps_the_times_of_the_run_that_produced_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A cache HIT reports when the unit RAN, not when it was reused."""
    workspace = tmp_path / ".zicato"
    workspace.mkdir()
    generation = _generation(tmp_path)
    entry = _entry()

    produced = make_loss_profile(
        measurement=replace(MeasurementDraw(MeasurementPurpose.TOURNAMENT, 0), base_seed=None),
        run_id=run_id_for_unit(
            generation.id, entry.id, base_seed=None, epoch_id=generation.epoch_id
        ),
        entry_id=entry.id,
        generation_id=generation.id,
        epoch_id=_EPOCH,
        started_at="2026-08-18T09:00:00Z",
        ended_at="2026-08-18T09:00:07Z",
    )
    _persist_unit_loss(
        workspace_root=workspace,
        epoch_id=_EPOCH,
        generation_id=generation.id,
        entry_id=entry.id,
        measurement=MeasurementDraw(MeasurementPurpose.TOURNAMENT, 0),
        loss=produced,
    )
    calls = _stub_run_single(monkeypatch, [])

    reused = _evaluate(workspace, generation, entry)

    assert calls == [], "a cache hit must not execute the unit"
    assert reused.started_at == "2026-08-18T09:00:00Z"
    assert reused.ended_at == "2026-08-18T09:00:07Z"


def test_the_replicate_fold_carries_the_representative_span(tmp_path: Path) -> None:
    """N replicates are N disjoint spans; the fold reports replicate 0's."""
    del tmp_path
    first = make_loss_profile(
        entry_id="entry_a",
        drift_loss=1.0,
        started_at="2026-08-18T09:00:00Z",
        ended_at="2026-08-18T09:00:05Z",
    )
    second = make_loss_profile(
        entry_id="entry_a",
        drift_loss=3.0,
        started_at="2026-08-18T09:10:00Z",
        ended_at="2026-08-18T09:10:05Z",
    )

    folded = _average_losses([{"entry_a": first}, {"entry_a": second}])["entry_a"]

    assert folded.drift_loss == pytest.approx(2.0)
    assert folded.started_at == first.started_at
    assert folded.ended_at == first.ended_at


# ---------------------------------------------------------------------------
# Attempt records
# ---------------------------------------------------------------------------


def test_a_failed_then_passing_unit_leaves_an_attempt_record(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The infra abort is discarded for scoring but kept as ``loss.tournament.r0.a1.json``."""
    workspace = tmp_path / ".zicato"
    workspace.mkdir()
    generation = _generation(tmp_path)
    entry = _entry()

    failed = make_loss_profile(
        run_id=f"{generation.id}--{entry.id}",
        entry_id=entry.id,
        generation_id=generation.id,
        epoch_id=_EPOCH,
        drift_loss=60.0,
        wall_clock_budget_exceeded=True,
        abort_cause="parent_kill",
        not_completed_reason="parent_kill",
    )
    passed = make_loss_profile(
        run_id=f"{generation.id}--{entry.id}",
        entry_id=entry.id,
        generation_id=generation.id,
        epoch_id=_EPOCH,
        drift_loss=1.5,
    )
    _stub_run_single(monkeypatch, [failed, passed])

    first = _evaluate(workspace, generation, entry)
    second = _evaluate(workspace, generation, entry)

    assert first.abort_cause == "parent_kill"
    assert second.drift_loss == pytest.approx(1.5)

    canonical = _unit_loss_path(
        workspace,
        _EPOCH,
        generation.id,
        entry.id,
        MeasurementDraw(MeasurementPurpose.TOURNAMENT, 0),
        base_seed=None,
    )
    attempt = canonical.with_name("loss.tournament.r0.a1.json")
    assert attempt.exists(), "the discarded execution must survive as an attempt"
    # Scoring reads the canonical slot, which holds the run that succeeded.
    assert read_loss_profile(canonical).drift_loss == pytest.approx(1.5)
    # The attempt names why it failed.
    recorded = read_loss_profile(attempt)
    assert recorded.abort_cause == "parent_kill"
    assert recorded.not_completed_reason == "parent_kill"


def test_a_re_measured_unit_keeps_the_superseded_measurement_and_its_twin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``force_fresh`` copies the slot aside before the re-run overwrites it."""
    workspace = tmp_path / ".zicato"
    workspace.mkdir()
    generation = _generation(tmp_path)
    entry = _entry()

    canonical = _unit_loss_path(
        workspace,
        _EPOCH,
        generation.id,
        entry.id,
        MeasurementDraw(MeasurementPurpose.TOURNAMENT, 0),
        base_seed=None,
    )
    first = make_loss_profile(
        measurement=replace(MeasurementDraw(MeasurementPurpose.TOURNAMENT, 0), base_seed=None),
        run_id=run_id_for_unit(
            generation.id, entry.id, base_seed=None, epoch_id=generation.epoch_id
        ),
        entry_id=entry.id,
        generation_id=generation.id,
        epoch_id=_EPOCH,
        drift_loss=4.0,
    )
    _persist_unit_loss(
        workspace_root=workspace,
        epoch_id=_EPOCH,
        generation_id=generation.id,
        entry_id=entry.id,
        measurement=MeasurementDraw(MeasurementPurpose.TOURNAMENT, 0),
        loss=first,
    )
    unit_result_path(canonical).write_text('{"format_version": 1}', encoding="utf-8")

    second = make_loss_profile(
        run_id=f"{generation.id}--{entry.id}",
        entry_id=entry.id,
        generation_id=generation.id,
        epoch_id=_EPOCH,
        drift_loss=9.0,
    )
    _stub_run_single(monkeypatch, [second])

    fresh = _evaluate(workspace, generation, entry, force_fresh=True)

    assert fresh.drift_loss == pytest.approx(9.0)
    assert read_loss_profile(canonical).drift_loss == pytest.approx(9.0)
    archives = list((canonical.parent / "attempts").glob("loss.tournament.r0-*"))
    assert len(archives) == 1
    attempt = archives[0] / "loss.tournament.r0.json"
    assert read_loss_profile(attempt).drift_loss == pytest.approx(4.0)
    # The result.tournament.r0.json twin rides along, so the superseded execution keeps
    # the harness output that produced it.
    assert unit_result_path(attempt).exists()


def test_an_attempt_record_never_reads_as_a_replicate(tmp_path: Path) -> None:
    from zicato.core.measurement import artifact_measurement
    from zicato.tournament.unit_cache import own_code_board_draws

    for purpose in (MeasurementPurpose.TOURNAMENT, MeasurementPurpose.CALIBRATION):
        record = MeasurementDraw(purpose, 0)
        path = measurement_artifact_path(tmp_path, "loss", record)
        write_loss_profile(make_loss_profile(measurement=record), path)
        attempt = path.with_name(path.stem + ".a1.json")
        write_loss_profile(make_loss_profile(measurement=record), attempt)
        assert artifact_measurement(attempt.name) is None
        assert is_unit_attempt_slot(attempt)
    assert {path.name for _, path in own_code_board_draws(tmp_path)} == {
        "loss.tournament.r0.json",
        "loss.calibration.r0.json",
    }


def test_attempt_records_do_not_move_the_folded_per_entry_loss(tmp_path: Path) -> None:
    """Scoring reads the same replicate set before and after attempts exist."""
    from zicato.tournament.unit_cache import own_code_board_draws

    run_dir = tmp_path / "runs" / "entry_a"
    run_dir.mkdir(parents=True)
    write_loss_profile(
        make_loss_profile(
            entry_id="entry_a",
            drift_loss=2.0,
            measurement=MeasurementDraw(MeasurementPurpose.TOURNAMENT, 0),
        ),
        measurement_artifact_path(
            run_dir, "loss", MeasurementDraw(MeasurementPurpose.TOURNAMENT, 0)
        ),
    )
    write_loss_profile(
        make_loss_profile(
            entry_id="entry_a",
            drift_loss=4.0,
            measurement=MeasurementDraw(MeasurementPurpose.CALIBRATION, 0),
            match_id="aa-calibration:0",
        ),
        measurement_artifact_path(
            run_dir, "loss", MeasurementDraw(MeasurementPurpose.CALIBRATION, 0)
        ),
    )

    def _folded() -> float:
        profiles = [read_loss_profile(p) for _, p in own_code_board_draws(run_dir)]
        return _average_losses([{"entry_a": p} for p in profiles])["entry_a"].drift_loss

    assert _folded() == pytest.approx(3.0)
    write_loss_profile(
        make_loss_profile(entry_id="entry_a", drift_loss=60.0),
        run_dir / "loss.tournament.r0.a1.json",
    )
    write_loss_profile(
        make_loss_profile(entry_id="entry_a", drift_loss=60.0),
        run_dir / "loss.calibration.r0.a1.json",
    )

    assert _folded() == pytest.approx(3.0)


def test_a_failed_attempt_write_never_costs_the_round(tmp_path: Path) -> None:
    """Best-effort: an unwritable run directory is logged, not raised."""
    workspace = tmp_path / ".zicato"
    record_unit_attempt(
        workspace_root=workspace,
        epoch_id=_EPOCH,
        generation_id="v0",
        entry_id="entry_a",
        measurement=MeasurementDraw(MeasurementPurpose.TOURNAMENT, 0),
    )  # nothing on disk to copy — a no-op, not an error


# ---------------------------------------------------------------------------
# Not-completed attribution
# ---------------------------------------------------------------------------


def test_a_synthesised_abort_attributes_its_penalty(tmp_path: Path) -> None:
    """The parent's worst-case twin names its cause the same way the worker does.

    It states the same facts too — no drift observed, every task failed, the
    run did not complete — from which the failure channel derives the charge.
    """
    del tmp_path
    entry = _entry()

    for cause in ("parent_kill", "gone_no_result", "nonzero_exit:1", BUDGET_ABORT_CAUSE):
        profile = _aborted_loss_profile(
            run_id="r",
            entry=entry,
            generation_id="v0",
            epoch_id=_EPOCH,
            runtime_ms=0,
            abort_cause=cause,
        )
        assert profile.not_completed_reason == cause
        assert profile.abort_cause == cause
        assert profile.drift_loss == 0.0
        assert profile.not_completed is True
        assert profile.task_failure_ratio == pytest.approx(1.0)


def test_not_completed_reason_survives_the_loss_json_round_trip(tmp_path: Path) -> None:
    profile = make_loss_profile(
        drift_loss=60.0,
        not_completed_reason="harness_exception:ValueError",
    )
    path = tmp_path / "loss.tournament.r0.json"
    write_loss_profile(profile, path)

    assert read_loss_profile(path).not_completed_reason == "harness_exception:ValueError"


@pytest.mark.asyncio
@pytest.mark.parametrize("seed", [None, 17])
async def test_adding_seed_provenance_preserves_the_unknown_predecessor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, seed: int | None
) -> None:
    from dataclasses import replace

    from zicato.tournament.unit_cache import read_unit_loss_history

    generation, entry = _generation(tmp_path), _entry()
    path = _unit_loss_path(
        tmp_path,
        _EPOCH,
        generation.id,
        entry.id,
        MeasurementDraw(MeasurementPurpose.TOURNAMENT, 0),
        base_seed=seed,
    )
    previous = make_loss_profile(
        run_id=run_id_for_unit(
            generation.id, entry.id, base_seed=seed, epoch_id=generation.epoch_id
        ),
        epoch_id=_EPOCH,
        generation_id=generation.id,
        entry_id=entry.id,
    )
    write_loss_profile(previous, path)
    measured = replace(
        previous,
        measurement=replace(MeasurementDraw(MeasurementPurpose.TOURNAMENT, 0), base_seed=seed),
    )
    calls = _stub_run_single(monkeypatch, [measured])
    with acquire_workspace_lock(tmp_path, "test") as writer:
        for _ in range(2):
            await _run_unit_cache_first(
                writer=writer,
                adapter=object(),
                generation=generation,
                entry=entry,
                weights=ScoringWeights(),
                config=replace(_runtime_config(tmp_path), seed=seed),
                workspace_root=tmp_path,
                epoch_id=_EPOCH,
                side="parent",
            )
    assert calls == [generation.id]
    archive = path.with_name("loss.archive.jsonl")
    records = [json.loads(line) for line in archive.read_text().splitlines()]
    assert len(records) == 1
    assert loss_profile_from_dict(records[0]["profile"]).measurement is None
    history = read_unit_loss_history(tmp_path, _EPOCH, generation.id, entry.id, base_seed=seed)
    assert len(history) == 2
    assert history[0].measurement is None
    assert history[1].measurement == measured.measurement
