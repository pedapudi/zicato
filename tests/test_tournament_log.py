"""Tests for the active-tournament EVENT LOG (RUNTIME-V2 Phase 3).

The in-progress tournament's live state migrated from a mutable
``active_tournament.json`` SNAPSHOT (multiple read-modify-writers racing
the same file) to a single-writer, append-only EVENT LOG
(:mod:`zicato.runtime.tournament_log`). The public ``state`` helpers keep
their signatures but now append typed events; a reader FOLDS the log into
the same :class:`ActiveTournament` the snapshot held.

These pin the new behaviour:

* the live producer writes the JSONL log, NOT the legacy snapshot file;
* every state-mutating helper is ONE append (no read-modify-write), and
  the fold reproduces the snapshot view byte-for-byte;
* a ``Snapshot`` republish supersedes prior state but carries the runner's
  accumulated live deltas forward (the dashboard keeps the live standing);
* INTERLEAVED writers (the orchestrator republish + the runner's per-board
  delta) cannot lose each other's update — the lost-update race the
  snapshot had is gone;
* a compat reader still surfaces a legacy ``active_tournament.json``;
* ``clear`` drops both the log and the legacy snapshot.
"""

from __future__ import annotations

import json
from pathlib import Path

from zicato.runtime import tournament_log
from zicato.runtime.paths import active_tournament_log_path, active_tournament_path
from zicato.runtime.state import (
    ActiveTournament,
    ActiveTournamentEntry,
    clear_active_tournament,
    read_active_tournament,
    read_active_tournament_snapshot,
    update_tournament_entry,
    update_tournament_partial_aggregate,
    update_tournament_projected,
    write_active_tournament,
)


def _sample() -> ActiveTournament:
    return ActiveTournament(
        tournament_id="tourn_e1_v2",
        parent_generation_id="v1",
        child_generation_id="v2",
        epoch_id="e1",
        started_at="2026-06-09T10:00:00Z",
        entries=[
            ActiveTournamentEntry(entry_id="b0", side="parent", status="queued"),
            ActiveTournamentEntry(entry_id="b0", side="child", status="queued"),
        ],
        round_index=2,
        total_rounds=5,
    )


# ---------------------------------------------------------------------------
# On-disk format: the live producer writes the LOG, not the snapshot file
# ---------------------------------------------------------------------------


def test_write_produces_the_event_log_not_the_legacy_snapshot(tmp_path: Path) -> None:
    write_active_tournament(tmp_path, _sample())
    # The new live-state file is the append-only JSONL event log.
    log_path = active_tournament_log_path(tmp_path)
    assert log_path.exists(), "the active-tournament event log is written"
    # The legacy mutable snapshot is NOT written by the live producer.
    assert not active_tournament_path(tmp_path).exists(), "no legacy snapshot file"
    # The first line is a typed, sequenced Snapshot event.
    first = json.loads(log_path.read_text().splitlines()[0])
    assert first["type"] == "Snapshot"
    assert first["seq"] == 1
    assert first["payload"]["tournament_id"] == "tourn_e1_v2"


def test_each_mutation_is_one_append_no_read_modify_write(tmp_path: Path) -> None:
    write_active_tournament(tmp_path, _sample())
    update_tournament_entry(tmp_path, "b0", "child", status="running")
    update_tournament_partial_aggregate(tmp_path, challenger_agg={"scalar": 0.5})
    update_tournament_projected(
        tmp_path, {"v2": {"scalar": 0.4, "boards_done": 1, "boards_total": 4}}
    )
    lines = active_tournament_log_path(tmp_path).read_text().splitlines()
    # Four mutations → four appended events, monotonic gap-free seq.
    types = [json.loads(line)["type"] for line in lines]
    seqs = [json.loads(line)["seq"] for line in lines]
    assert types == ["Snapshot", "EntryUpdate", "PartialAggregate", "ProjectedUpdate"]
    assert seqs == [1, 2, 3, 4]


# ---------------------------------------------------------------------------
# The fold reproduces the snapshot view
# ---------------------------------------------------------------------------


def test_fold_reproduces_the_snapshot_view(tmp_path: Path) -> None:
    write_active_tournament(tmp_path, _sample())
    update_tournament_entry(tmp_path, "b0", "child", status="running", started_at="t")
    update_tournament_partial_aggregate(tmp_path, challenger_agg={"scalar": 0.5, "entry_count": 1})
    got = read_active_tournament(tmp_path)
    assert got is not None
    by_pair = {(e.entry_id, e.side): e for e in got.entries}
    assert by_pair[("b0", "child")].status == "running"
    assert by_pair[("b0", "child")].started_at == "t"
    assert by_pair[("b0", "parent")].status == "queued"  # untouched side
    assert got.partial_challenger_agg == {"scalar": 0.5, "entry_count": 1}
    assert got.partial_champion_agg == {}
    # Unmutated envelope fields survive the fold.
    assert got.round_index == 2
    assert got.total_rounds == 5


def test_snapshot_republish_supersedes_but_carries_runner_deltas(tmp_path: Path) -> None:
    """A republish resets the base but the runner's accumulated live state
    is carried forward (the dashboard keeps the live projected standing).

    This is the producer's contract: ``_publish_active_tournament`` folds
    the current view, then republishes a Snapshot carrying the runner's
    ``projected`` / partial aggregates forward — so a republish + the
    runner's per-board deltas compose instead of clobbering to empty.
    """
    write_active_tournament(tmp_path, _sample())
    update_tournament_projected(
        tmp_path, {"v2": {"scalar": 0.4, "boards_done": 1, "boards_total": 4}}
    )
    # The producer reads the folded view (carrying projected) and republishes.
    folded = read_active_tournament(tmp_path)
    assert folded is not None and folded.projected.get("v2")
    write_active_tournament(tmp_path, folded)  # the carry-forward republish.
    after = read_active_tournament(tmp_path)
    assert after is not None
    assert after.projected.get("v2", {}).get("scalar") == 0.4


# ---------------------------------------------------------------------------
# The lost-update race the snapshot had is gone
# ---------------------------------------------------------------------------


def test_interleaved_writers_do_not_lose_updates(tmp_path: Path) -> None:
    """The orchestrator's entry transition and the runner's aggregate
    fold are SEPARATE appends — neither clobbers the other even when their
    writes interleave (the ``_publish_active_tournament`` lost-update race).
    """
    write_active_tournament(tmp_path, _sample())
    # Interleave a per-entry transition (orchestrator) with a partial
    # aggregate + projection (runner) — as two distinct writers would.
    update_tournament_entry(tmp_path, "b0", "child", status="running")
    update_tournament_partial_aggregate(tmp_path, challenger_agg={"scalar": 0.5})
    update_tournament_entry(tmp_path, "b0", "parent", status="running")
    update_tournament_partial_aggregate(tmp_path, champion_agg={"scalar": 0.9})
    got = read_active_tournament(tmp_path)
    assert got is not None
    by_pair = {(e.entry_id, e.side): e.status for e in got.entries}
    # BOTH entry transitions survived.
    assert by_pair[("b0", "child")] == "running"
    assert by_pair[("b0", "parent")] == "running"
    # BOTH aggregate sides survived.
    assert got.partial_challenger_agg == {"scalar": 0.5}
    assert got.partial_champion_agg == {"scalar": 0.9}


def test_projected_update_folds_into_live_progress_in_the_reader(tmp_path: Path) -> None:
    """A racing rung's per-lane ``live_progress`` picks up the runner's
    projected ``boards_done`` / ``projected_scalar`` in the FOLD — the same
    overlay the snapshot writer baked in, now reader-side.
    """
    base = ActiveTournament(
        tournament_id="t",
        parent_generation_id="",
        child_generation_id="",
        epoch_id="e",
        started_at="t",
        structure="racing",
        competitors=[
            {"generation_id": "v0", "role": "champion"},
            {"generation_id": "v5", "role": "challenger"},
        ],
        rounds=[
            {
                "label": "Rung 1",
                "matches": [
                    {
                        "match_id": "rung1_m0",
                        "competitors": ["v0", "v5"],
                        "live_progress": {
                            "v5": {"boards_total": 8, "inflight": 1},
                        },
                    }
                ],
            }
        ],
    )
    write_active_tournament(tmp_path, base)
    update_tournament_projected(
        tmp_path, {"v5": {"scalar": 9.6, "boards_done": 6, "boards_total": 8}}
    )
    got = read_active_tournament(tmp_path)
    assert got is not None
    lane = got.rounds[0]["matches"][0]["live_progress"]["v5"]
    assert lane["boards_done"] == 6
    assert lane["projected_scalar"] == 9.6
    assert lane["projected"] is True


# ---------------------------------------------------------------------------
# Compat reader + clear
# ---------------------------------------------------------------------------


def test_compat_reader_folds_a_legacy_snapshot_when_no_log(tmp_path: Path) -> None:
    """A pre-RUNTIME-V2 ``active_tournament.json`` snapshot (no event log)
    is still surfaced by the folded read — the migration's compat path.
    """
    legacy = {
        "tournament_id": "t",
        "parent_generation_id": "v1",
        "child_generation_id": "v2",
        "epoch_id": "e",
        "started_at": "2026-06-09T10:00:00Z",
        "phase": "running",
        "entries": [{"entry_id": "b0", "side": "child", "status": "completed"}],
    }
    active_tournament_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    active_tournament_path(tmp_path).write_text(json.dumps(legacy))
    # No event log present → the fold falls back to the legacy snapshot.
    assert not tournament_log.has_log(tmp_path)
    got = read_active_tournament(tmp_path)
    assert got is not None
    assert got.tournament_id == "t"
    assert got.entries[0].status == "completed"
    # The direct compat reader sees the same legacy snapshot.
    assert read_active_tournament_snapshot(tmp_path) is not None


def test_clear_removes_both_the_log_and_the_legacy_snapshot(tmp_path: Path) -> None:
    write_active_tournament(tmp_path, _sample())
    # Also drop a stale legacy snapshot to prove clear drops both.
    active_tournament_path(tmp_path).write_text("{}")
    clear_active_tournament(tmp_path)
    assert not active_tournament_log_path(tmp_path).exists()
    assert not active_tournament_path(tmp_path).exists()
    assert read_active_tournament(tmp_path) is None


def test_read_is_none_when_nothing_written(tmp_path: Path) -> None:
    assert read_active_tournament(tmp_path) is None
    assert not tournament_log.has_log(tmp_path)
