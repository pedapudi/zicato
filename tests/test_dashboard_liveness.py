"""The ONE liveness derivation — ``runtime_view.derive_liveness``.

Liveness is a property of the CLOCK, not of file presence. The workspace
that motivated this (issue #194) has been dead since June and still holds a
heartbeat reading ``tournament:round_0:racing-final``, an
``active_tournament.json`` reading ``phase: running`` and seven
``active_runs`` records — every one of which the dashboard used to read as
"something is running right now".

Every clock in here is INJECTED (``now=``) or pinned relative to a fixed
base; nothing keys off the ambient wall clock, so a slow CI box cannot age
a fixture across the threshold mid-test.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from zicato.query.paths import WorkspacePaths
from zicato.query.runtime_view import (
    LIVENESS_INTERRUPTED,
    LIVENESS_LIVE,
    LIVENESS_SETTLED,
    STALE_HEARTBEAT_S,
    derive_liveness,
    fresh_run_count,
    is_active_phase,
    read_active_runs_view,
)
from zicato.runtime import progress_log
from zicato.runtime.lock import pid_start_time

#: The pinned "now" every fixture is dated against.
NOW = _dt.datetime(2026, 8, 9, 12, 0, 0, tzinfo=_dt.UTC)


def _iso(ts: _dt.datetime) -> str:
    return ts.isoformat().replace("+00:00", "Z")


def _ws(
    tmp_path: Path,
    *,
    phase: str | None = "tournament:round_0:v1",
    beat_age_s: float | None = 1.0,
    run_ages_s: list[float] | None = None,
    run_identity: tuple[int, float | None] | None = None,
    lock_owner: tuple[int, float | None] | None = None,
) -> Path:
    """A runtime workspace with a heartbeat aged ``beat_age_s`` before NOW.

    ``phase=None`` writes no heartbeat at all. ``run_ages_s`` writes one
    ``active_runs`` record per entry, each ``last_progress`` that many
    seconds before NOW.

    ``run_identity`` is the ``(pid, pid_start_time)`` every run record
    claims (default: a made-up pid and no start time, the shape of a
    producer from before the worker stamped one). ``lock_owner`` is the
    ``(pid, start_time)`` written to ``runtime/lock.json``; a lock naming a
    live LOCAL process is what proves the reader shares the workers' host.
    """
    ws = tmp_path / ".zicato"
    (ws / "runtime" / "active_runs").mkdir(parents=True)
    if beat_age_s is not None:
        beat = NOW - _dt.timedelta(seconds=beat_age_s)
        (ws / "runtime" / "heartbeat.json").write_text(
            json.dumps(
                {
                    "pid": 1,
                    "instance_id": "default",
                    "started_at": _iso(NOW - _dt.timedelta(hours=1)),
                    "last_heartbeat": _iso(beat),
                    "epoch_id": "e0",
                    "generation_id": "v1",
                    "phase": phase or "",
                    "round_index": 0,
                }
            ),
            encoding="utf-8",
        )
    if lock_owner is not None:
        (ws / "runtime" / "lock.json").write_text(
            json.dumps(
                {
                    "pid": lock_owner[0],
                    "instance_id": "default",
                    "acquired_at": _iso(NOW - _dt.timedelta(hours=1)),
                    "workspace_root": str(ws.parent),
                    "start_time": lock_owner[1],
                }
            ),
            encoding="utf-8",
        )
    for i, age in enumerate(run_ages_s or []):
        started = NOW - _dt.timedelta(seconds=age)
        (ws / "runtime" / "active_runs" / f"r{i}.json").write_text(
            json.dumps(
                {
                    "run_id": f"r{i}",
                    "pid": run_identity[0] if run_identity else 100 + i,
                    "pid_start_time": run_identity[1] if run_identity else None,
                    "started_at": _iso(started),
                    "last_progress": _iso(started),
                    "wall_clock_budget_seconds": 180,
                    "deadline": _iso(started + _dt.timedelta(seconds=180)),
                    "events_jsonl_path": str(ws / "events.jsonl"),
                    "entry_id": f"entry_{i}",
                    "generation_id": "v1",
                    "epoch_id": "e0",
                }
            ),
            encoding="utf-8",
        )
    return ws


def _derive(ws: Path) -> dict[str, Any]:
    return derive_liveness(WorkspacePaths(ws), now=NOW)


# ---------------------------------------------------------------------------
# The three states
# ---------------------------------------------------------------------------


def test_fresh_heartbeat_on_an_active_phase_is_live(tmp_path: Path) -> None:
    out = _derive(_ws(tmp_path))
    assert out["state"] == LIVENESS_LIVE
    # A live run has not ended, so it reports no end.
    assert "ended_at" not in out
    assert out["last_heartbeat"] == _iso(NOW - _dt.timedelta(seconds=1))
    assert out["epoch_id"] == "e0"


def test_the_june_workspace_shape_is_interrupted(tmp_path: Path) -> None:
    """A frozen mid-round heartbeat + seven frozen in-flight runs.

    The exact shape of the dead-since-June workspace: every file says
    "running", nothing has moved in two months.
    """
    two_months = 61 * 24 * 3600.0
    ws = _ws(
        tmp_path,
        phase="tournament:round_0:racing-final",
        beat_age_s=two_months,
        run_ages_s=[two_months] * 7,
    )
    out = _derive(ws)
    assert out["state"] == LIVENESS_INTERRUPTED
    # It stopped mid-flight; the last beat is the last moment it was seen.
    assert out["ended_at"] == _iso(NOW - _dt.timedelta(seconds=two_months))


def test_terminal_progress_event_is_settled(tmp_path: Path) -> None:
    ws = _ws(tmp_path, beat_age_s=1.0)
    progress_log.append_progress(ws, progress_log.ROUND_START)
    progress_log.append_progress(ws, progress_log.SETTLED)
    out = _derive(ws)
    assert out["state"] == LIVENESS_SETTLED
    assert out["ended_at"] == progress_log.tail(ws).ts  # type: ignore[union-attr]


def test_terminal_beats_a_fresh_heartbeat(tmp_path: Path) -> None:
    """The cross-boundary edge: fresh + terminal is SETTLED, not live.

    The beater keeps pulsing for a beat or two after the loop appends its
    terminal event. The end is authoritative — a settled run must not read
    live for the tail of the staleness window.
    """
    ws = _ws(tmp_path, phase="tournament:round_0:v1", beat_age_s=0.0)
    progress_log.append_progress(ws, progress_log.STOPPED)
    assert _derive(ws)["state"] == LIVENESS_SETTLED


def test_at_rest_phase_settles_without_a_progress_log(tmp_path: Path) -> None:
    """A workspace older than the progress log records its end in the phase."""
    out = _derive(_ws(tmp_path, phase="evolve_n_rounds:done", beat_age_s=0.0))
    assert out["state"] == LIVENESS_SETTLED
    assert out["ended_at"] == _iso(NOW)


def test_non_terminal_progress_tail_with_a_stale_beat_is_interrupted(tmp_path: Path) -> None:
    ws = _ws(tmp_path, beat_age_s=3600.0)
    progress_log.append_progress(ws, progress_log.TOURNAMENT_START)
    assert _derive(ws)["state"] == LIVENESS_INTERRUPTED


def test_never_run_workspace_is_settled_with_nothing_to_report(tmp_path: Path) -> None:
    ws = tmp_path / ".zicato"
    (ws / "runtime").mkdir(parents=True)
    assert _derive(ws) == {"state": LIVENESS_SETTLED}


# ---------------------------------------------------------------------------
# Clock edges — the threshold itself
# ---------------------------------------------------------------------------


def test_a_beat_exactly_at_the_threshold_is_still_live(tmp_path: Path) -> None:
    assert _derive(_ws(tmp_path, beat_age_s=STALE_HEARTBEAT_S))["state"] == LIVENESS_LIVE


def test_a_beat_just_past_the_threshold_is_interrupted(tmp_path: Path) -> None:
    ws = _ws(tmp_path, beat_age_s=STALE_HEARTBEAT_S + 0.5)
    assert _derive(ws)["state"] == LIVENESS_INTERRUPTED


def test_a_beat_from_the_future_reads_live_not_stale(tmp_path: Path) -> None:
    """Clock skew between writer and reader must not kill a live run."""
    assert _derive(_ws(tmp_path, beat_age_s=-5.0))["state"] == LIVENESS_LIVE


def test_an_unparseable_beat_is_not_fresh(tmp_path: Path) -> None:
    """No ageable timestamp means NOT live — never a default to live.

    The reader substitutes the heartbeat file's mtime, which for a file
    written long ago is itself stale.
    """
    ws = _ws(tmp_path, beat_age_s=1.0)
    hb = ws / "runtime" / "heartbeat.json"
    record = json.loads(hb.read_text())
    record["last_heartbeat"] = "not-a-timestamp"
    hb.write_text(json.dumps(record), encoding="utf-8")
    old = (NOW - _dt.timedelta(days=60)).timestamp()
    os.utime(hb, (old, old))
    assert _derive(ws)["state"] == LIVENESS_INTERRUPTED


def test_a_fresh_worker_keeps_the_verdict_live_through_a_frozen_beat(tmp_path: Path) -> None:
    """Per-run beaters are independent of the orchestrator beat.

    A worker parked in a long model call keeps bumping ``last_progress``
    while the orchestrator's asyncio beater is starved — that is a live
    run, not an interrupted one.
    """
    ws = _ws(tmp_path, beat_age_s=600.0, run_ages_s=[2.0])
    assert _derive(ws)["state"] == LIVENESS_LIVE


def test_stale_workers_alone_do_not_make_it_live(tmp_path: Path) -> None:
    """The June bug in miniature: leftover active_runs are not a pulse."""
    ws = _ws(tmp_path, beat_age_s=None, run_ages_s=[86400.0, 86400.0])
    assert _derive(ws)["state"] == LIVENESS_INTERRUPTED


def test_active_runs_carry_an_ageable_timestamp(tmp_path: Path) -> None:
    """Each run record ships ``last_progress_ts`` (ms epoch).

    The frontend ages the in-flight tally off THIS field, exactly as it ages
    the heartbeat off ``ts`` — no ISO parsing, no magnitude guessing. Without
    it a consumer can only count records, which is how a workspace dead since
    June reported seven units running.
    """
    ws = _ws(tmp_path, run_ages_s=[5.0])
    rows = read_active_runs_view(WorkspacePaths(ws))
    assert len(rows) == 1
    expected = int((NOW - _dt.timedelta(seconds=5.0)).timestamp() * 1000)
    assert rows[0]["last_progress_ts"] == expected


# ---------------------------------------------------------------------------
# Process identity — reaping a dead worker's record (#270)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def dead_worker() -> tuple[int, float]:
    """A ``(pid, pid_start_time)`` pair naming a process that has exited.

    Read off a real child while it lives, so the pair is exactly what a
    worker stamps on its own record; the child is then reaped, which is
    what makes the identity check answer "gone".
    """
    proc = subprocess.Popen(  # noqa: S603
        [sys.executable, "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE
    )
    start = pid_start_time(proc.pid)
    proc.kill()
    proc.wait(timeout=30)
    if start is None:
        pytest.skip("no readable process start time on this platform")
    return (proc.pid, start)


def _this_process() -> tuple[int, float | None]:
    """The reader's own ``(pid, start_time)`` — a lock naming it is host-local."""
    return (os.getpid(), pid_start_time(os.getpid()))


def _count(ws: Path) -> int:
    paths = WorkspacePaths(ws)
    return fresh_run_count(read_active_runs_view(paths), NOW, paths=paths)


def test_a_dead_worker_is_reaped_without_waiting_out_the_window(
    tmp_path: Path, dead_worker: tuple[int, float]
) -> None:
    """Identity beats the staleness proxy: a seconds-old record whose worker
    is provably gone is not in flight, and does not hold the verdict live."""
    ws = _ws(
        tmp_path,
        beat_age_s=None,
        run_ages_s=[1.0],
        run_identity=dead_worker,
        lock_owner=_this_process(),
    )
    assert _count(ws) == 0
    assert _derive(ws)["state"] == LIVENESS_INTERRUPTED


def test_a_live_worker_survives_the_identity_gate(tmp_path: Path) -> None:
    """The gate only ever tightens — a record naming a live local process
    (here, the test process itself) still counts."""
    ws = _ws(
        tmp_path,
        beat_age_s=None,
        run_ages_s=[1.0],
        run_identity=_this_process(),
        lock_owner=_this_process(),
    )
    assert _count(ws) == 1
    assert _derive(ws)["state"] == LIVENESS_LIVE


def test_a_dead_worker_still_counts_when_the_lock_names_another_host(
    tmp_path: Path, dead_worker: tuple[int, float]
) -> None:
    """A workspace synced from another machine: the lock owner is not a live
    local process, so the recorded pids carry no local meaning and the
    timestamp rule stands alone."""
    ws = _ws(
        tmp_path,
        beat_age_s=None,
        run_ages_s=[1.0],
        run_identity=dead_worker,
        lock_owner=(dead_worker[0], dead_worker[1] + 1.0),
    )
    assert _count(ws) == 1


def test_a_dead_worker_still_counts_with_no_lock(
    tmp_path: Path, dead_worker: tuple[int, float]
) -> None:
    """No lock (a workspace at rest) proves no host-locality either."""
    ws = _ws(tmp_path, beat_age_s=None, run_ages_s=[1.0], run_identity=dead_worker)
    assert _count(ws) == 1


def test_a_record_without_identity_fields_counts(tmp_path: Path) -> None:
    """Back-compat: a producer that stamped no ``pid_start_time`` cannot be
    judged dead — a bare pid number is not an identity."""
    ws = _ws(tmp_path, beat_age_s=None, run_ages_s=[1.0], lock_owner=_this_process())
    assert _count(ws) == 1


def test_a_stale_record_stays_out_however_alive_its_pid_is(tmp_path: Path) -> None:
    """The identity gate never ADMITS a record the timestamp rule rejected."""
    ws = _ws(
        tmp_path,
        beat_age_s=None,
        run_ages_s=[86400.0],
        run_identity=_this_process(),
        lock_owner=_this_process(),
    )
    assert _count(ws) == 0


# ---------------------------------------------------------------------------
# The SERVED per-row verdict (#273 residual)
# ---------------------------------------------------------------------------
#
# The browser can age a timestamp but can never run the identity gate — it is
# not the worker's host. So the server decides per row and sends the answer;
# these pin that the served verdict is the tally's own rule, row by row.


def _rows(ws: Path) -> list[dict[str, Any]]:
    return read_active_runs_view(WorkspacePaths(ws), now=NOW)


def test_each_row_carries_the_servers_in_flight_verdict(tmp_path: Path) -> None:
    """A beating record serves ``fresh: true``, a stale one ``fresh: false``."""
    ws = _ws(tmp_path, run_ages_s=[5.0, 86400.0])
    assert [row["fresh"] for row in _rows(ws)] == [True, False]


def test_the_per_row_verdict_runs_the_identity_gate(
    tmp_path: Path, dead_worker: tuple[int, float]
) -> None:
    """A seconds-old record whose worker is provably gone serves fresh:false.

    This is the half a client cannot reach: by the timestamp alone the row
    is one second old, and only the pid identity says the work is over.
    """
    ws = _ws(
        tmp_path,
        beat_age_s=None,
        run_ages_s=[1.0],
        run_identity=dead_worker,
        lock_owner=_this_process(),
    )
    assert [row["fresh"] for row in _rows(ws)] == [False]


def test_the_per_row_verdict_and_the_tally_never_disagree(
    tmp_path: Path, dead_worker: tuple[int, float]
) -> None:
    """One rule, two surfaces: the tally is the count of fresh rows."""
    ws = _ws(
        tmp_path,
        run_ages_s=[1.0, 86400.0, 2.0],
        run_identity=dead_worker,
        lock_owner=_this_process(),
    )
    rows = _rows(ws)
    assert sum(1 for row in rows if row["fresh"]) == _count(ws)


# ---------------------------------------------------------------------------
# The phase vocabulary
# ---------------------------------------------------------------------------


def test_is_active_phase_reads_the_terminal_token_in_any_segment() -> None:
    assert is_active_phase("tournament:round_0:rung0_m3")
    assert is_active_phase("proposing:field")
    assert not is_active_phase("evolve_n_rounds:done")
    assert not is_active_phase("tournament:round_0:error")
    assert not is_active_phase("idle")
    # An absent phase carries no claim either way — it is not ACTIVE, but
    # (unlike an at-rest token) it does not settle a pulsing workspace.
    assert not is_active_phase("")
    assert not is_active_phase(None)


def test_an_empty_phase_on_a_fresh_beat_stays_live(tmp_path: Path) -> None:
    """The beater's very first write has no phase yet — that is a live run."""
    assert _derive(_ws(tmp_path, phase="", beat_age_s=0.0))["state"] == LIVENESS_LIVE


def test_a_persistent_harmonograf_does_not_synthesize_a_pulse(tmp_path: Path) -> None:
    """``read_heartbeat_dict`` synthesizes a now-stamped heartbeat for a
    post-mortem workspace with a persistent harmonograf server. Liveness
    reads the RAW record instead, so the synthetic stamp cannot resurrect
    a dead workspace."""
    ws = tmp_path / ".zicato"
    (ws / "runtime").mkdir(parents=True)
    paths = WorkspacePaths(ws)
    paths.harmonograf_url = "http://127.0.0.1:9999"  # type: ignore[attr-defined]
    assert derive_liveness(paths, now=NOW)["state"] == LIVENESS_SETTLED
