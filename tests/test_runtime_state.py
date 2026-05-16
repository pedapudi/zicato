"""Tests for ``zicato.runtime.state``.

Coverage:

* Heartbeat round-trip serialization.
* Active run lifecycle (write → touch_progress → remove).
* Active tournament lifecycle, entry updates.
* Atomic-write discipline (no half-written files; ``.tmp`` collisions
  are not surfaced as half-files to the reader).
"""

from __future__ import annotations

import json
from pathlib import Path

from zicato.runtime.paths import active_run_path, heartbeat_path
from zicato.runtime.state import (
    ActiveRun,
    ActiveTournament,
    ActiveTournamentEntry,
    Heartbeat,
    clear_active_tournament,
    list_active_runs,
    read_active_tournament,
    read_heartbeat,
    remove_active_run,
    touch_active_run_progress,
    update_tournament_entry,
    write_active_run,
    write_active_tournament,
    write_heartbeat,
)

# ---------------------------------------------------------------------------
# Heartbeat
# ---------------------------------------------------------------------------


def test_read_heartbeat_returns_none_when_missing(tmp_path: Path) -> None:
    assert read_heartbeat(tmp_path) is None


def test_heartbeat_round_trip(tmp_path: Path) -> None:
    hb = Heartbeat(
        pid=12345,
        instance_id="default",
        started_at="2026-05-14T10:00:00Z",
        last_heartbeat="2026-05-14T10:00:05Z",
        epoch_id="2026-05-14_demo",
        generation_id="v3",
        phase="proposer",
        round_index=2,
        round_started_at="2026-05-14T10:00:03Z",
        harmonograf_url="127.0.0.1:7531",
    )
    write_heartbeat(tmp_path, hb)
    got = read_heartbeat(tmp_path)
    assert got == hb
    assert got is not None
    assert got.harmonograf_url == "127.0.0.1:7531"


def test_heartbeat_harmonograf_url_back_compat(tmp_path: Path) -> None:
    """A heartbeat JSON written by an old writer (no harmonograf_url) loads."""
    legacy = {
        "pid": 7,
        "instance_id": "old",
        "started_at": "2026-05-14T00:00:00Z",
        "last_heartbeat": "2026-05-14T00:00:00Z",
        "epoch_id": "e0",
        "generation_id": "v0",
        "phase": "proposer",
        "round_index": 0,
        "round_started_at": "",
    }
    hb = Heartbeat.from_dict(legacy)
    # Missing field defaults to empty string, not a KeyError.
    assert hb.harmonograf_url == ""


def test_heartbeat_defaults_round_trip(tmp_path: Path) -> None:
    hb = Heartbeat(
        pid=1,
        instance_id="x",
        started_at="2026-05-14T00:00:00Z",
        last_heartbeat="2026-05-14T00:00:00Z",
    )
    write_heartbeat(tmp_path, hb)
    got = read_heartbeat(tmp_path)
    assert got == hb
    # Defaults are empty strings + zero, not None.
    assert got is not None
    assert got.epoch_id == ""
    assert got.round_index == 0


def test_heartbeat_atomic_write_leaves_no_tmp(tmp_path: Path) -> None:
    hb = Heartbeat(pid=1, instance_id="x", started_at="t", last_heartbeat="t")
    write_heartbeat(tmp_path, hb)
    rt_dir = heartbeat_path(tmp_path).parent
    # Only the final heartbeat.json should exist; no stray .tmp files.
    leftover = [p.name for p in rt_dir.iterdir() if p.name.endswith(".tmp")]
    assert leftover == []


def test_heartbeat_file_is_pretty_printed_json(tmp_path: Path) -> None:
    hb = Heartbeat(pid=42, instance_id="x", started_at="t", last_heartbeat="t")
    write_heartbeat(tmp_path, hb)
    text = heartbeat_path(tmp_path).read_text()
    # Indented + sorted keys means "instance_id" comes before "pid" and
    # there is a newline between fields.
    parsed = json.loads(text)
    assert parsed["pid"] == 42
    assert "\n" in text


# ---------------------------------------------------------------------------
# Active runs
# ---------------------------------------------------------------------------


def _sample_run(run_id: str = "run_a") -> ActiveRun:
    return ActiveRun(
        run_id=run_id,
        pid=999,
        started_at="2026-05-14T10:00:00Z",
        last_progress="2026-05-14T10:00:00Z",
        wall_clock_budget_seconds=60,
        deadline="2026-05-14T10:01:00Z",
        events_jsonl_path="/tmp/events.jsonl",
        entry_id="entry_a",
        generation_id="v1",
        epoch_id="2026-05-14_demo",
    )


def test_list_active_runs_empty_when_no_dir(tmp_path: Path) -> None:
    assert list_active_runs(tmp_path) == []


def test_write_and_list_active_runs(tmp_path: Path) -> None:
    write_active_run(tmp_path, _sample_run("run_a"))
    write_active_run(tmp_path, _sample_run("run_b"))
    got = list_active_runs(tmp_path)
    assert [r.run_id for r in got] == ["run_a", "run_b"]


def test_list_active_runs_skips_non_json(tmp_path: Path) -> None:
    write_active_run(tmp_path, _sample_run("run_a"))
    # Drop a .tmp file as if a write were in progress; it must NOT be
    # surfaced by the reader.
    runs_dir = active_run_path(tmp_path, "run_a").parent
    (runs_dir / "run_b.json.tmp").write_text("partial")
    got = list_active_runs(tmp_path)
    assert [r.run_id for r in got] == ["run_a"]


def test_touch_active_run_progress_bumps_timestamp(tmp_path: Path) -> None:
    write_active_run(tmp_path, _sample_run("run_a"))
    before = list_active_runs(tmp_path)[0]
    touch_active_run_progress(tmp_path, "run_a")
    after = list_active_runs(tmp_path)[0]
    # Other fields preserved, last_progress updated.
    assert after.run_id == before.run_id
    assert after.pid == before.pid
    assert after.started_at == before.started_at
    # last_progress is now a fresh ISO-8601 string (≠ initial value or
    # at least equal-to-now-or-later).
    assert after.last_progress >= before.last_progress
    assert after.last_progress.endswith("Z")


def test_touch_active_run_progress_no_op_when_missing(tmp_path: Path) -> None:
    # No-op rather than error. The orchestrator may race the cleanup.
    touch_active_run_progress(tmp_path, "does_not_exist")  # must not raise
    assert list_active_runs(tmp_path) == []


def test_remove_active_run_idempotent(tmp_path: Path) -> None:
    write_active_run(tmp_path, _sample_run("run_a"))
    remove_active_run(tmp_path, "run_a")
    assert list_active_runs(tmp_path) == []
    # Second remove is a no-op.
    remove_active_run(tmp_path, "run_a")


def test_active_run_round_trip(tmp_path: Path) -> None:
    run = _sample_run("run_xyz")
    write_active_run(tmp_path, run)
    [back] = list_active_runs(tmp_path)
    assert back == run


# ---------------------------------------------------------------------------
# Active tournament
# ---------------------------------------------------------------------------


def _sample_tournament() -> ActiveTournament:
    return ActiveTournament(
        tournament_id="tourn_e1_v2",
        parent_generation_id="v1",
        child_generation_id="v2",
        epoch_id="2026-05-14_demo",
        started_at="2026-05-14T10:00:00Z",
        entries=[
            ActiveTournamentEntry(entry_id="entry_a", side="parent", status="queued"),
            ActiveTournamentEntry(entry_id="entry_a", side="child", status="queued"),
            ActiveTournamentEntry(entry_id="entry_b", side="parent", status="queued"),
        ],
    )


def test_read_active_tournament_returns_none_when_missing(tmp_path: Path) -> None:
    assert read_active_tournament(tmp_path) is None


def test_active_tournament_round_trip(tmp_path: Path) -> None:
    t = _sample_tournament()
    write_active_tournament(tmp_path, t)
    got = read_active_tournament(tmp_path)
    assert got == t


def test_active_tournament_round_fields_round_trip(tmp_path: Path) -> None:
    """round_index / total_rounds survive a write → read cycle."""
    t = ActiveTournament(
        tournament_id="tourn_e1_v2",
        parent_generation_id="v1",
        child_generation_id="v2",
        epoch_id="e1",
        started_at="2026-05-14T10:00:00Z",
        round_index=3,
        total_rounds=8,
    )
    write_active_tournament(tmp_path, t)
    got = read_active_tournament(tmp_path)
    assert got is not None
    assert got.round_index == 3
    assert got.total_rounds == 8
    assert got == t


def test_active_tournament_round_fields_back_compat(tmp_path: Path) -> None:
    """A tournament JSON without the new round fields still loads (defaults 0)."""
    legacy = {
        "tournament_id": "t",
        "parent_generation_id": "v1",
        "child_generation_id": "v2",
        "epoch_id": "e",
        "started_at": "2026-05-14T10:00:00Z",
        "phase": "running",
        "entries": [],
    }
    t = ActiveTournament.from_dict(legacy)
    assert t.round_index == 0
    assert t.total_rounds == 0


def test_active_tournament_entry_loss_summary_round_trips(tmp_path: Path) -> None:
    t = ActiveTournament(
        tournament_id="t",
        parent_generation_id="v1",
        child_generation_id="v2",
        epoch_id="e",
        started_at="t",
        entries=[
            ActiveTournamentEntry(
                entry_id="ea",
                side="child",
                status="completed",
                loss_summary={"drift_loss": 0.12, "pass_fail": 1.0},
                drift_count_snapshot={"INTENT_DIVERGENCE": 3, "TOOL_MISUSE": 1},
            )
        ],
    )
    write_active_tournament(tmp_path, t)
    got = read_active_tournament(tmp_path)
    assert got is not None
    assert got.entries[0].loss_summary == {"drift_loss": 0.12, "pass_fail": 1.0}
    assert got.entries[0].drift_count_snapshot == {
        "INTENT_DIVERGENCE": 3,
        "TOOL_MISUSE": 1,
    }


def test_update_tournament_entry_targets_child_side_only(tmp_path: Path) -> None:
    """A child-side update lands on the child row only; the parent row is untouched.

    Each board entry has TWO rows in the tournament grid (one per side).
    ``update_tournament_entry`` keys on ``(entry_id, side)`` so a
    child-side transition cannot bleed onto the parent's same-entry row.
    """
    t = _sample_tournament()  # entry_a on both sides, entry_b parent-only.
    write_active_tournament(tmp_path, t)
    update_tournament_entry(tmp_path, "entry_a", "child", status="completed")
    got = read_active_tournament(tmp_path)
    assert got is not None
    by_pair = {(e.entry_id, e.side): e.status for e in got.entries}
    # Only the child row moved; the parent entry_a row stayed queued.
    assert by_pair[("entry_a", "child")] == "completed"
    assert by_pair[("entry_a", "parent")] == "queued"
    assert by_pair[("entry_b", "parent")] == "queued"


def test_update_tournament_entry_targets_parent_side_only(tmp_path: Path) -> None:
    """Symmetric: a parent-side update lands on the parent row only."""
    t = _sample_tournament()
    write_active_tournament(tmp_path, t)
    update_tournament_entry(tmp_path, "entry_a", "parent", status="running")
    got = read_active_tournament(tmp_path)
    assert got is not None
    by_pair = {(e.entry_id, e.side): e.status for e in got.entries}
    assert by_pair[("entry_a", "parent")] == "running"
    assert by_pair[("entry_a", "child")] == "queued"
    assert by_pair[("entry_b", "parent")] == "queued"


def test_update_tournament_entry_preserves_side_field(tmp_path: Path) -> None:
    """A status update must not perturb either row's ``side`` label."""
    write_active_tournament(tmp_path, _sample_tournament())
    update_tournament_entry(tmp_path, "entry_a", "child", status="completed")
    got = read_active_tournament(tmp_path)
    assert got is not None
    sides = sorted((e.entry_id, e.side) for e in got.entries)
    # All three rows keep their original (entry_id, side) identity.
    assert sides == [
        ("entry_a", "child"),
        ("entry_a", "parent"),
        ("entry_b", "parent"),
    ]


def test_update_tournament_entry_no_match_is_noop(tmp_path: Path) -> None:
    """An (entry_id, side) pair that matches no row leaves the file unchanged."""
    write_active_tournament(tmp_path, _sample_tournament())
    # entry_b has no child row.
    update_tournament_entry(tmp_path, "entry_b", "child", status="completed")
    got = read_active_tournament(tmp_path)
    assert got is not None
    assert all(e.status == "queued" for e in got.entries)


def test_update_tournament_entry_tolerates_duplicate_pairs(tmp_path: Path) -> None:
    """Two rows sharing the same (entry_id, side): only the first is updated, no crash."""
    t = ActiveTournament(
        tournament_id="t",
        parent_generation_id="v1",
        child_generation_id="v2",
        epoch_id="e",
        started_at="2026-05-14T10:00:00Z",
        entries=[
            ActiveTournamentEntry(entry_id="dup", side="parent", status="queued"),
            ActiveTournamentEntry(entry_id="dup", side="parent", status="queued"),
        ],
    )
    write_active_tournament(tmp_path, t)
    update_tournament_entry(tmp_path, "dup", "parent", status="running")
    got = read_active_tournament(tmp_path)
    assert got is not None
    # First duplicate updated, second left untouched — no exception raised.
    assert [e.status for e in got.entries] == ["running", "queued"]


def test_update_tournament_entry_no_op_without_file(tmp_path: Path) -> None:
    # Must not raise; just a no-op.
    update_tournament_entry(tmp_path, "anything", "parent", status="running")
    assert read_active_tournament(tmp_path) is None


def test_clear_active_tournament_idempotent(tmp_path: Path) -> None:
    write_active_tournament(tmp_path, _sample_tournament())
    clear_active_tournament(tmp_path)
    assert read_active_tournament(tmp_path) is None
    # Second clear is a no-op.
    clear_active_tournament(tmp_path)


def test_atomic_write_does_not_leave_partial_on_overwrite(tmp_path: Path) -> None:
    """If a writer overwrites, the reader never sees an empty file.

    Hard to truly simulate without a kernel-level crash, but we can
    verify the on-disk shape: after a successful overwrite, the final
    file is fully valid JSON and no .tmp lingers.
    """
    write_active_run(tmp_path, _sample_run("run_a"))
    write_active_run(
        tmp_path,
        ActiveRun(
            run_id="run_a",
            pid=42,
            started_at="2026-05-14T10:01:00Z",
            last_progress="2026-05-14T10:01:00Z",
            wall_clock_budget_seconds=120,
            deadline="2026-05-14T10:03:00Z",
            events_jsonl_path="/tmp/e.jsonl",
            entry_id="entry_a",
            generation_id="v1",
            epoch_id="e",
        ),
    )
    runs_dir = active_run_path(tmp_path, "run_a").parent
    # Only run_a.json, no .tmp.
    files = sorted(p.name for p in runs_dir.iterdir())
    assert files == ["run_a.json"]
    # And it parses.
    parsed = json.loads(active_run_path(tmp_path, "run_a").read_text())
    assert parsed["pid"] == 42
