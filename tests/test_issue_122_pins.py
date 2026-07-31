"""Regression pins for issue #122 — a re-measurement kept the one before it.

The champion is a single generation id that defends across many rounds, and
every artifact describing one of its evaluations is keyed by
``(epoch, generation, entry)`` with NO round dimension:

* ``generations/<g>/gen_score.json`` — :func:`zicato.evolve.ingest._cache_gen_score`
  writes it unconditionally (``ingest.py`` ~47);
* ``generations/<g>/runs/<entry>/loss.json`` (``loss.r<N>.json`` for
  replicates) — :func:`zicato.tournament.unit_cache._persist_unit_loss`;
* ``generations/<g>/runs/<entry>/events.jsonl`` — the worker opens the sink
  with ``mode="write"`` (``_tournament_worker.py`` ~459), so the raw
  telemetry is truncated too, not appended;
* ``generations/<g>/runs/<entry>/result.json`` — the same replicate slotting.

Under the cache-first default (``--mode fast``) that is not data loss: the
champion's units are cache HITS, nothing is re-measured, and at-most-once is
the point. The loss is specific to ``--mode full``, where
``champion_force_fresh=(not fast_mode) and resumed_experiment is None``
(``orchestrator.py`` ~1118) re-samples the champion every round and then
writes the new sample over the old one. That is the sharp edge: the ONE mode
whose stated purpose is re-sampling for noise is the mode that destroys the
sample it would be compared against.

What survives today, and what does not
--------------------------------------
The round log preserves the champion's per-round AGGREGATE scalar —
``GateEvaluated.champion_scalar`` is durable under
``epochs/<e>/rounds/<n>/round_log.jsonl`` — which is why the reporter could
reconstruct 8.479 / 5.917 / 6.229 at all. What no artifact preserves is the
decomposition (pass rate, drift-loss mean) and, decisively for the
attributable-per-entry-regression check cross-referenced on the issue, the
PER-ENTRY results of the parent's earlier measurement. ``UnitCompleted``
carries ``entry_id`` / ``replicate`` / ``side`` and no numbers at all.

The fix deliberately does NOT re-key the unit cache. Making the cache key
round-aware would turn every champion lookup into a miss and re-run the
champion each round in fast mode — breaking the at-most-once discipline the
cache exists to enforce. What these pins hold is ARCHIVE-ON-OVERWRITE: the
canonical flat path keeps holding the latest measurement (so every existing
reader is untouched), and the outgoing measurement is retained beside it —
``gen_score.history.jsonl`` (every aggregate, unbounded but tiny),
``loss.archive.jsonl`` (every displaced per-entry profile), and
``events.prev.jsonl`` (exactly one prior raw telemetry file).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from zicato.core.types import LossProfile
from zicato.evolve.ingest import _cache_gen_score
from zicato.workspace import WorkspaceLayout, read_gen_score, read_loss

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
        drift_counts=(),
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
    _cache_gen_score(workspace, EPOCH, CHAMPION, _aggregate(8.479, 0.75, 2.1))
    _cache_gen_score(workspace, EPOCH, CHAMPION, _aggregate(5.917, 0.75, 1.4))

    assert read_gen_score(layout, EPOCH, CHAMPION)["scalar"] == pytest.approx(5.917)


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
    _cache_gen_score(workspace, EPOCH, CHAMPION, _aggregate(8.479, 0.75, 2.1))
    _cache_gen_score(workspace, EPOCH, CHAMPION, _aggregate(5.917, 0.75, 1.4))
    _cache_gen_score(workspace, EPOCH, CHAMPION, _aggregate(6.229, 0.75, 1.6))

    # The flat file is still the latest, and the archive is the ONE thing
    # that now sits beside it (the pin's original assertion was that the
    # generation directory held nothing else — it holds exactly one more).
    assert read_gen_score(layout, EPOCH, CHAMPION)["scalar"] == pytest.approx(6.229)
    gen_dir = layout.gen_score(EPOCH, CHAMPION).parent
    assert sorted(p.name for p in gen_dir.iterdir()) == [
        "gen_score.history.jsonl",
        "gen_score.json",
    ]

    from zicato.workspace import read_gen_score_history  # noqa: PLC0415

    history = read_gen_score_history(layout, EPOCH, CHAMPION)
    assert [round(float(m["scalar"]), 3) for m in history] == [8.479, 5.917, 6.229]
    # The pass rate is identical across all three — the swing is pure drift,
    # which is exactly the diagnosis the history is meant to make possible.
    assert {float(m["pass_rate"]) for m in history} == {0.75}


# ---------------------------------------------------------------------------
# Pin 3 — per-entry evidence, the live-gating prerequisite
# ---------------------------------------------------------------------------


def test_forced_fresh_unit_rerun_retains_the_prior_per_entry_result(workspace: Path) -> None:
    """The parent's per-entry results must survive its own re-measurement.

    Attributable per-entry regression — did an entry that passed under the
    parent fail under the challenger — is computable only while the parent's
    earlier per-entry results still exist at the moment the child is scored.
    ``--mode full`` re-runs the champion every round and overwrites exactly
    those results.
    """
    from zicato.tournament.unit_cache import _persist_unit_loss  # noqa: PLC0415

    layout = WorkspaceLayout.from_root(workspace)
    for loss in (
        _loss("e1", drift_loss=2.1, pass_fail=True),
        _loss("e1", drift_loss=1.4, pass_fail=False),
    ):
        _persist_unit_loss(
            workspace_root=workspace,
            epoch_id=EPOCH,
            generation_id=CHAMPION,
            entry_id="e1",
            replicate_index=0,
            loss=loss,
        )

    # Back-compat: the canonical slot is still the cache key and still the
    # latest — the unit cache's at-most-once discipline is untouched. This
    # half holds today, and any fix must keep it holding.
    latest = read_loss(layout, EPOCH, CHAMPION, "e1")
    assert latest is not None
    assert latest["pass_fail"] is False
    assert latest["drift_loss"] == pytest.approx(1.4)

    from zicato.tournament.unit_cache import read_unit_loss_history  # noqa: PLC0415

    history = read_unit_loss_history(workspace, EPOCH, CHAMPION, "e1", 0)
    assert [h.pass_fail for h in history] == [True, False]
    assert [round(h.drift_loss, 3) for h in history] == [2.1, 1.4]


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


# ---------------------------------------------------------------------------
# Pin 6 — the loss archive must sit in the process that TRUNCATES the slot
# ---------------------------------------------------------------------------
#
# The worker SUBPROCESS writes the canonical ``loss.json``; the orchestrator's
# ``_persist_unit_loss`` then re-persists the identical profile. Archiving from
# the orchestrator alone gets both halves wrong: the measurement this run
# displaced is already gone from disk by then (so nothing real is retained),
# and the profile it does find is the one it is about to write (so every fresh
# unit run appends a copy of the CURRENT measurement). These two pins model the
# real two-writer flow rather than calling ``_persist_unit_loss`` twice.


def test_a_unit_measured_once_reads_back_as_one_measurement(workspace: Path) -> None:
    """The orchestrator's idempotent re-persist is not a second measurement."""
    from zicato.telemetry.reducer import write_loss_profile  # noqa: PLC0415
    from zicato.tournament.unit_cache import (  # noqa: PLC0415
        _persist_unit_loss,
        _unit_loss_path,
        archive_outgoing_unit_loss,
        read_unit_loss_history,
    )

    only = _loss("e1", drift_loss=2.1, pass_fail=True)
    path = _unit_loss_path(workspace, EPOCH, CHAMPION, "e1", 0)
    path.parent.mkdir(parents=True, exist_ok=True)

    archive_outgoing_unit_loss(path)  # the worker, before it truncates
    write_loss_profile(only, path)  # the worker's write
    _persist_unit_loss(  # the orchestrator's idempotent re-persist
        workspace_root=workspace,
        epoch_id=EPOCH,
        generation_id=CHAMPION,
        entry_id="e1",
        replicate_index=0,
        loss=only,
    )

    history = read_unit_loss_history(workspace, EPOCH, CHAMPION, "e1", 0)
    assert [round(h.drift_loss, 3) for h in history] == [2.1]


def test_worker_archives_the_measurement_its_write_displaces(workspace: Path) -> None:
    """Three ``--mode full`` rounds yield three measurements, in order."""
    from zicato.telemetry.reducer import write_loss_profile  # noqa: PLC0415
    from zicato.tournament.unit_cache import (  # noqa: PLC0415
        _persist_unit_loss,
        _unit_loss_path,
        archive_outgoing_unit_loss,
        read_unit_loss_history,
    )

    path = _unit_loss_path(workspace, EPOCH, CHAMPION, "e1", 0)
    path.parent.mkdir(parents=True, exist_ok=True)
    for drift_loss in (2.1, 1.4, 1.9):
        loss = _loss("e1", drift_loss=drift_loss, pass_fail=True)
        archive_outgoing_unit_loss(path)
        write_loss_profile(loss, path)
        _persist_unit_loss(
            workspace_root=workspace,
            epoch_id=EPOCH,
            generation_id=CHAMPION,
            entry_id="e1",
            replicate_index=0,
            loss=loss,
        )

    history = read_unit_loss_history(workspace, EPOCH, CHAMPION, "e1", 0)
    assert [round(h.drift_loss, 3) for h in history] == [2.1, 1.4, 1.9]


def test_a_displacing_write_with_no_worker_still_archives(workspace: Path) -> None:
    """The skipped-unit path has no worker, so ``_persist_unit_loss`` archives.

    A budget-skip synthesises its own loss and writes it straight over an
    occupied slot; the ``incoming`` guard must suppress only the re-persist of
    the SAME profile, never a genuine displacement.
    """
    from zicato.tournament.unit_cache import (  # noqa: PLC0415
        _persist_unit_loss,
        read_unit_loss_history,
    )

    for loss in (
        _loss("e1", drift_loss=2.1, pass_fail=True),
        _loss("e1", drift_loss=9.9, pass_fail=False),
    ):
        _persist_unit_loss(
            workspace_root=workspace,
            epoch_id=EPOCH,
            generation_id=CHAMPION,
            entry_id="e1",
            replicate_index=0,
            loss=loss,
        )

    history = read_unit_loss_history(workspace, EPOCH, CHAMPION, "e1", 0)
    assert [round(h.drift_loss, 3) for h in history] == [2.1, 9.9]
