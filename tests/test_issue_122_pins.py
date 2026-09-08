"""Remeasurement retains ordered profiles and the latest canonical result.

The execution owner archives a slot before another unit starts. The profile
history retains repeated observations even when complete attempt archives
share identical contents. Generation score history and event predecessors
remain readable through their existing owners.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from tests._runtime_builders import make_generation, runtime_config
from zicato.core import BoardEntry, ScoringWeights
from zicato.core.measurement import MeasurementDraw
from zicato.core.types import LossProfile
from zicato.core.workspace import run_id_for_unit
from zicato.runtime.lock import acquire_workspace_lock
from zicato.telemetry.reducer import write_loss_profile
from zicato.tournament import runner, unit_cache
from zicato.tournament.scheduling import _run_unit_cache_first
from zicato.tournament.scoring import read_gen_score, write_gen_score
from zicato.workspace import WorkspaceLayout, read_loss

EPOCH = "2026-07-29_alpha"
CHAMPION = "v0"


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / ".zicato"
    ws.mkdir()
    return ws


def _aggregate(scalar: float, pass_rate: float, drift_loss_mean: float) -> dict[str, Any]:
    """A generation aggregate in the shape ``gen_score.json`` persists."""
    return {
        "generation_id": CHAMPION,
        "scalar": scalar,
        "pass_rate": pass_rate,
        "drift_loss_mean": drift_loss_mean,
    }


def _loss(entry_id: str, *, drift_loss: float, pass_fail: bool) -> LossProfile:
    """One board unit's reduced result — the per-entry evidence #130 needs."""
    return LossProfile(
        run_id=f"{CHAMPION}--{entry_id}",
        entry_id=entry_id,
        generation_id=CHAMPION,
        epoch_id=EPOCH,
        metric_counts=(),
        plan_revisions=0,
        task_failure_ratio=0.0,
        runtime_ms=1000,
        wall_clock_budget_exceeded=False,
        expectation_result=None,
        drift_loss=drift_loss,
        pass_fail=pass_fail,
    )


# ---------------------------------------------------------------------------
# Pin 1 — the flat path stays the latest (back-compat half; holds TODAY)
# ---------------------------------------------------------------------------


def test_gen_score_flat_path_still_holds_the_latest_measurement(workspace: Path) -> None:
    """Not a pin — the constraint any fix must not break.

    Every existing reader (fast-mode champion reuse, the dashboard, the
    propose path) reads the flat ``gen_score.json`` and must keep seeing the
    most recent measurement there.
    """
    layout = WorkspaceLayout.from_root(workspace)
    write_gen_score(workspace, EPOCH, CHAMPION, _aggregate(8.479, 0.75, 2.1))
    write_gen_score(workspace, EPOCH, CHAMPION, _aggregate(5.917, 0.75, 1.4))

    assert read_gen_score(layout, EPOCH, CHAMPION).scalar == pytest.approx(5.917)


# ---------------------------------------------------------------------------
# Pin 2 — the re-measurement must not destroy the one before it
# ---------------------------------------------------------------------------


def test_gen_score_rewrite_retains_the_prior_measurement(workspace: Path) -> None:
    """Three defences of an unchanged champion must leave three measurements.

    This is the whole issue in one assertion: an identical champion scoring
    8.479 / 5.917 / 6.229 at an identical pass rate is a 2.56 swing entirely
    in the drift term, and it is only visible if all three numbers survive.
    """
    layout = WorkspaceLayout.from_root(workspace)
    write_gen_score(workspace, EPOCH, CHAMPION, _aggregate(8.479, 0.75, 2.1))
    write_gen_score(workspace, EPOCH, CHAMPION, _aggregate(5.917, 0.75, 1.4))
    write_gen_score(workspace, EPOCH, CHAMPION, _aggregate(6.229, 0.75, 1.6))

    # The flat file is still the latest, and the archive is the ONE thing
    # that now sits beside it (the pin's original assertion was that the
    # generation directory held nothing else — it holds exactly one more).
    assert read_gen_score(layout, EPOCH, CHAMPION).scalar == pytest.approx(6.229)
    gen_dir = layout.gen_score(EPOCH, CHAMPION).parent
    assert sorted(p.name for p in gen_dir.iterdir()) == [
        "gen_score.history.jsonl",
        "gen_score.json",
    ]

    from zicato.tournament.scoring import read_gen_score_history  # noqa: PLC0415

    history = [row.to_dict() for row in read_gen_score_history(layout, EPOCH, CHAMPION)]
    assert [round(float(m["scalar"]), 3) for m in history] == [8.479, 5.917, 6.229]
    # The pass rate is identical across all three — the swing is pure drift,
    # which is exactly the diagnosis the history is meant to make possible.
    assert {float(m["pass_rate"]) for m in history} == {0.75}


@pytest.mark.parametrize(
    ("samples", "worker_persists"),
    [
        (((2.1, True),), True),
        (((2.1, True), (1.4, False)), True),
        (((2.1, True), (1.4, True), (1.9, True)), True),
        (((2.1, True), (9.9, False)), False),
        (((2.1, True), (1.4, True), (2.1, True), (1.9, True)), True),
    ],
)
def test_executed_measurements_preserve_ordered_loss_history(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
    samples: tuple[tuple[float, bool], ...],
    worker_persists: bool,
) -> None:
    """Only the execution boundary reads history, including identical reruns."""
    generation = replace(make_generation(workspace, CHAMPION), epoch_id=EPOCH)
    entry = BoardEntry(id="e1", kind="single_turn", input="input", wall_clock_budget_seconds=1)
    path = unit_cache._unit_loss_path(workspace, EPOCH, CHAMPION, entry.id, 0, base_seed=None)
    history_reads: list[Path] = []
    archive = unit_cache.archive_outgoing_unit_loss

    def observe_archive(path: Path, **kwargs: Any) -> None:
        if path.exists():
            history_reads.append(path)
        archive(path, **kwargs)

    monkeypatch.setattr(unit_cache, "archive_outgoing_unit_loss", observe_archive)
    for drift_loss, pass_fail in samples:
        loss = replace(
            _loss(entry.id, drift_loss=drift_loss, pass_fail=pass_fail),
            run_id=run_id_for_unit(CHAMPION, entry.id, base_seed=None),
            measurement=MeasurementDraw.from_index(0, base_seed=None),
            execution_started=True,
        )

        async def measured(profile: LossProfile = loss, **kwargs: Any) -> LossProfile:
            if worker_persists:
                write_loss_profile(profile, path)
            return profile

        monkeypatch.setattr(runner, "_run_single", measured)
        with acquire_workspace_lock(workspace, "loss-history-test") as writer:
            asyncio.run(
                _run_unit_cache_first(
                    writer=writer,
                    adapter=object(),
                    generation=generation,
                    entry=entry,
                    weights=ScoringWeights(),
                    config=runtime_config(workspace),
                    workspace_root=workspace,
                    epoch_id=EPOCH,
                    side="parent",
                    force_fresh=True,
                )
            )

    latest = read_loss(
        WorkspaceLayout.from_root(workspace), EPOCH, CHAMPION, entry.id, base_seed=None
    )
    assert latest is not None
    assert latest["pass_fail"] is samples[-1][1]
    assert latest["drift_loss"] == pytest.approx(samples[-1][0])
    history = unit_cache.read_unit_loss_history(
        workspace, EPOCH, CHAMPION, entry.id, base_seed=None
    )
    assert [h.pass_fail for h in history] == [passed for _, passed in samples]
    assert [round(h.drift_loss, 3) for h in history] == [value for value, _ in samples]
    assert history_reads == [path] * (len(samples) - 1)


# ---------------------------------------------------------------------------
# Pin 4 — raw telemetry is truncated, not just the reduced record
# ---------------------------------------------------------------------------


def test_rerunning_a_unit_does_not_truncate_the_prior_events_log(workspace: Path) -> None:
    """The evidence loss reaches below the reduced record.

    ``loss.json`` can in principle be re-derived from ``events.jsonl``; once
    the events file is truncated the measurement is unreconstructable by any
    means. This pin asserts the archive covers the raw layer too.

    As WRITTEN the pin overwrote the file with two bare ``write_text``
    calls and asserted a two-element history, which no fix could satisfy —
    nothing in that sequence goes near the code that opens the sink. It
    drives the real seam instead: the worker and both sink factories call
    ``archive_prior_events`` immediately before constructing the
    ``mode="write"`` sink, which is the moment the truncation happens.
    """
    from zicato.core.workspace import events_jsonl_path  # noqa: PLC0415
    from zicato.telemetry.sink import archive_prior_events  # noqa: PLC0415

    events = events_jsonl_path(workspace, EPOCH, CHAMPION, "e1")
    events.parent.mkdir(parents=True, exist_ok=True)
    events.write_text('{"seq": 1, "round": "first"}\n', encoding="utf-8")
    # The next round's sink wiring: archive, THEN the mode="write" open
    # that truncates (simulated here by the write itself).
    archive_prior_events(events)
    events.write_text('{"seq": 1, "round": "second"}\n', encoding="utf-8")

    # The canonical file still holds only the latest run's telemetry —
    # every existing reader (the reducer, the run-log walker) is untouched.
    assert "first" not in events.read_text(encoding="utf-8")

    from zicato.workspace import read_events_history  # noqa: PLC0415

    layout = WorkspaceLayout.from_root(workspace)
    history = read_events_history(layout, EPOCH, CHAMPION, "e1")
    assert len(history) == 2, "the first round's raw telemetry is gone"
    assert history[0][0]["round"] == "first"
    assert history[1][0]["round"] == "second"


def test_sink_construction_archives_the_prior_events_file(workspace: Path) -> None:
    """The PRODUCTION path archives — not just the helper the pin calls.

    ``make_run_sink`` is one of the three seams that open the truncating
    sink (the other two are ``make_run_sinks`` and the worker's
    ``_build_sinks``); constructing one over an occupied path must leave
    the prior telemetry recoverable.
    """
    pytest.importorskip("goldfive.sinks.persistence")
    from zicato.telemetry.sink import make_run_sink  # noqa: PLC0415

    layout = WorkspaceLayout.from_root(workspace)
    events = layout.events(EPOCH, CHAMPION, "e1")
    events.parent.mkdir(parents=True, exist_ok=True)
    events.write_text('{"seq": 1, "round": "first"}\n', encoding="utf-8")

    make_run_sink(workspace, EPOCH, CHAMPION, "e1")

    prev = layout.events_prev(EPOCH, CHAMPION, "e1")
    assert prev.exists()
    assert "first" in prev.read_text(encoding="utf-8")
