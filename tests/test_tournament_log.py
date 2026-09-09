"""Tournament event reconstruction, interleaved updates and state clearing."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from zicato.runtime import tournament_log
from zicato.runtime.lock import acquire_workspace_lock
from zicato.runtime.paths import active_tournament_log_path
from zicato.runtime.state import (
    ActiveTournament,
    ActiveTournamentEntry,
    clear_active_tournament,
    read_active_tournament,
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
    with acquire_workspace_lock(tmp_path, "test-publication") as writer:
        write_active_tournament(writer, _sample())
    # The producer appends a typed, sequenced event.
    log_path = active_tournament_log_path(tmp_path)
    assert log_path.exists(), "the active-tournament event log is written"
    # A publication leaves unrelated runtime paths absent.
    assert not (tmp_path / "runtime" / "active_tournament.json").exists(), "no legacy snapshot file"
    # The first line is a typed, sequenced Snapshot event.
    first = json.loads(log_path.read_text().splitlines()[0])
    assert first["type"] == "Snapshot"
    assert first["seq"] == 1
    assert first["payload"]["tournament_id"] == "tourn_e1_v2"


def test_each_mutation_is_one_append_no_read_modify_write(tmp_path: Path) -> None:
    with acquire_workspace_lock(tmp_path, "test-publication") as writer:
        with patch.object(writer.tournament_log, "tail", wraps=writer.tournament_log.tail) as scan:
            write_active_tournament(writer, _sample())
            update_tournament_entry(writer, "b0", "child", status="running")
            update_tournament_partial_aggregate(writer, challenger_agg={"scalar": 0.5})
            update_tournament_projected(
                writer, {"v2": {"scalar": 0.4, "boards_done": 1, "boards_total": 4}}
            )
        assert scan.call_count == 1
    lines = active_tournament_log_path(tmp_path).read_text().splitlines()
    # Four mutations → four appended events, monotonic gap-free seq.
    types = [json.loads(line)["type"] for line in lines]
    seqs = [json.loads(line)["seq"] for line in lines]
    assert types == ["Snapshot", "Update", "Update", "Update"]
    assert seqs == [1, 2, 3, 4]


# ---------------------------------------------------------------------------
# The fold reproduces the snapshot view
# ---------------------------------------------------------------------------


def test_fold_reproduces_the_snapshot_view(tmp_path: Path) -> None:
    with acquire_workspace_lock(tmp_path, "test-publication") as writer:
        write_active_tournament(writer, _sample())
        update_tournament_entry(writer, "b0", "child", status="running", started_at="t")
        update_tournament_partial_aggregate(
            writer, challenger_agg={"scalar": 0.5, "entry_count": 1}
        )
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
    with acquire_workspace_lock(tmp_path, "test-publication") as writer:
        write_active_tournament(writer, _sample())
        update_tournament_projected(
            writer, {"v2": {"scalar": 0.4, "boards_done": 1, "boards_total": 4}}
        )
        # The producer reads the folded view (carrying projected) and republishes.
        folded = read_active_tournament(tmp_path)
        assert folded is not None and folded.projected.get("v2")
        write_active_tournament(writer, folded)  # the carry-forward republish.
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
    with acquire_workspace_lock(tmp_path, "test-publication") as writer:
        write_active_tournament(writer, _sample())
        # Interleave a per-entry transition (orchestrator) with a partial
        # aggregate + projection (runner) — as two distinct writers would.
        update_tournament_entry(writer, "b0", "child", status="running")
        update_tournament_partial_aggregate(writer, challenger_agg={"scalar": 0.5})
        update_tournament_entry(writer, "b0", "parent", status="running")
        update_tournament_partial_aggregate(writer, champion_agg={"scalar": 0.9})
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
                        "pending": True,
                        "competitors": ["v0", "v5"],
                        "live_progress": {
                            "v5": {"boards_total": 8, "inflight": 1},
                        },
                    }
                ],
            }
        ],
    )
    with acquire_workspace_lock(tmp_path, "test-publication") as writer:
        write_active_tournament(writer, base)
        update_tournament_projected(
            writer, {"v5": {"scalar": 9.6, "boards_done": 6, "boards_total": 8}}
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


@pytest.mark.parametrize(
    "log_contents", [None, "", '{"seq":1,"ts":"t","type":"EntryUpdate","payload":{}}\n']
)
def test_missing_live_state_ignores_and_preserves_saved_snapshot(
    tmp_path: Path, log_contents: str | None
) -> None:
    snapshot = tmp_path / "runtime" / "active_tournament.json"
    snapshot.parent.mkdir(parents=True)
    saved = json.dumps(_sample().to_dict())
    snapshot.write_text(saved)
    if log_contents is not None:
        active_tournament_log_path(tmp_path).write_text(log_contents)
    assert read_active_tournament(tmp_path) is None
    with acquire_workspace_lock(tmp_path, "test-publication") as writer:
        clear_active_tournament(writer)
    assert snapshot.read_text() == saved


def test_read_is_none_when_nothing_written(tmp_path: Path) -> None:
    assert read_active_tournament(tmp_path) is None
    assert not tournament_log.has_log(tmp_path)


def test_writer_recovers_after_an_append_reports_failure(tmp_path: Path) -> None:
    with acquire_workspace_lock(tmp_path, "test-publication") as writer:
        write_active_tournament(writer, _sample())
        append = writer.tournament_log.append

        def uncertain_append(*args, **kwargs):
            append(*args, **kwargs)
            raise OSError("write completion was not confirmed")

        with patch.object(writer.tournament_log, "append", side_effect=uncertain_append):
            with pytest.raises(OSError):
                update_tournament_partial_aggregate(writer, challenger_agg={"scalar": 0.5})
        update_tournament_entry(writer, "b0", "child", status="completed")
    got = read_active_tournament(tmp_path)
    assert got is not None
    assert got.partial_challenger_agg == {"scalar": 0.5}
    assert got.entries[1].status == "completed"


def test_runtime_cleanup_prevents_updates_to_a_discarded_tournament(tmp_path: Path) -> None:
    from zicato.runtime.resume import clear_runtime_state

    with acquire_workspace_lock(tmp_path, "test-publication") as writer:
        write_active_tournament(writer, _sample())
        clear_runtime_state(writer)
        update_tournament_partial_aggregate(writer, challenger_agg={"scalar": 0.5})
        assert read_active_tournament(tmp_path) is None
        assert not active_tournament_log_path(tmp_path).exists()
        write_active_tournament(writer, _sample())
        update_tournament_entry(writer, "b0", "child", status="running")
    got = read_active_tournament(tmp_path)
    assert got is not None and got.entries[1].status == "running"
    assert not got.partial_challenger_agg
