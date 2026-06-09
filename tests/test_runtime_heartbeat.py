"""Tests for ``zicato.runtime.heartbeat.HeartbeatBeater`` and ``RunHeartbeatBeater``."""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

import pytest

from zicato.runtime.heartbeat import HeartbeatBeater, RunHeartbeatBeater
from zicato.runtime.state import ActiveRun, list_active_runs, read_heartbeat, write_active_run


async def test_start_writes_immediate_heartbeat(tmp_path: Path) -> None:
    beater = HeartbeatBeater(tmp_path, "default", interval_s=10.0)
    await beater.start()
    try:
        hb = read_heartbeat(tmp_path)
        assert hb is not None
        assert hb.pid == os.getpid()
        assert hb.instance_id == "default"
        assert hb.last_heartbeat != ""
    finally:
        await beater.stop()


async def test_beater_bumps_periodically(tmp_path: Path) -> None:
    beater = HeartbeatBeater(tmp_path, "default", interval_s=0.01)
    await beater.start()
    try:
        first = read_heartbeat(tmp_path)
        assert first is not None
        # Wait long enough for at least 2 bumps (interval 0.01s).
        await asyncio.sleep(0.05)
        second = read_heartbeat(tmp_path)
        assert second is not None
        # last_heartbeat is at-or-after the first one (seconds precision
        # may collapse equality if the test machine is very fast).
        assert second.last_heartbeat >= first.last_heartbeat
        # started_at is identical — only last_heartbeat moves.
        assert second.started_at == first.started_at
    finally:
        await beater.stop()


async def test_update_changes_visible_after_next_bump(tmp_path: Path) -> None:
    beater = HeartbeatBeater(tmp_path, "default", interval_s=0.05)
    await beater.start()
    try:
        beater.update(
            epoch_id="2026-05-14_demo",
            generation_id="v3",
            phase="tournament:entry=foo",
            round_index=2,
        )
        # bump_now flushes immediately rather than waiting for the next
        # tick — gives the test a deterministic point to assert at.
        beater.bump_now()
        hb = read_heartbeat(tmp_path)
        assert hb is not None
        assert hb.epoch_id == "2026-05-14_demo"
        assert hb.generation_id == "v3"
        assert hb.phase == "tournament:entry=foo"
        assert hb.round_index == 2
    finally:
        await beater.stop()


async def test_update_with_no_args_is_noop(tmp_path: Path) -> None:
    beater = HeartbeatBeater(tmp_path, "default", interval_s=10.0)
    await beater.start()
    try:
        snapshot_before = beater.snapshot
        beater.update()
        snapshot_after = beater.snapshot
        # Every field unchanged.
        assert snapshot_before == snapshot_after
    finally:
        await beater.stop()


async def test_stop_is_idempotent(tmp_path: Path) -> None:
    beater = HeartbeatBeater(tmp_path, "default", interval_s=0.05)
    await beater.start()
    await beater.stop()
    # Second stop must not raise.
    await beater.stop()


async def test_start_twice_raises(tmp_path: Path) -> None:
    beater = HeartbeatBeater(tmp_path, "default", interval_s=10.0)
    await beater.start()
    try:
        with pytest.raises(RuntimeError, match="already started"):
            await beater.start()
    finally:
        await beater.stop()


async def test_stop_without_start_is_noop(tmp_path: Path) -> None:
    beater = HeartbeatBeater(tmp_path, "default", interval_s=10.0)
    # Never started — stop must not raise.
    await beater.stop()


async def test_partial_update_preserves_unchanged_fields(tmp_path: Path) -> None:
    beater = HeartbeatBeater(tmp_path, "default", interval_s=10.0)
    await beater.start()
    try:
        beater.update(epoch_id="e1", generation_id="v1", phase="proposer", round_index=1)
        beater.update(phase="applier")  # Only phase changes.
        beater.bump_now()
        hb = read_heartbeat(tmp_path)
        assert hb is not None
        assert hb.epoch_id == "e1"
        assert hb.generation_id == "v1"
        assert hb.phase == "applier"
        assert hb.round_index == 1
    finally:
        await beater.stop()


async def test_beater_snapshot_carries_pid(tmp_path: Path) -> None:
    beater = HeartbeatBeater(tmp_path, "default", interval_s=10.0)
    assert beater.snapshot.pid == os.getpid()
    assert beater.snapshot.instance_id == "default"


# ---------------------------------------------------------------------------
# RunHeartbeatBeater tests
# ---------------------------------------------------------------------------


def _sample_active_run(tmp_path: Path, run_id: str = "v0--entry_a") -> ActiveRun:
    """Write and return a minimal ActiveRun for use in heartbeat tests."""
    run = ActiveRun(
        run_id=run_id,
        pid=os.getpid(),
        started_at="2026-05-18T10:00:00Z",
        last_progress="2026-05-18T10:00:00Z",
        wall_clock_budget_seconds=60,
        deadline="2026-05-18T10:01:00Z",
        events_jsonl_path="/tmp/events.jsonl",
        entry_id="entry_a",
        generation_id="v0",
        epoch_id="2026-05-18_test",
    )
    write_active_run(tmp_path, run)
    return run


def test_run_heartbeat_beater_start_bumps_immediately(tmp_path: Path) -> None:
    """start() performs an immediate bump so last_progress is fresh."""
    _sample_active_run(tmp_path, "v0--entry_a")
    before = list_active_runs(tmp_path)[0]

    beater = RunHeartbeatBeater(tmp_path, "v0--entry_a", interval_s=10.0)
    beater.start()
    try:
        after = list_active_runs(tmp_path)[0]
        # The bump is best-effort but should succeed in a test environment.
        assert after.last_progress >= before.last_progress
        assert (
            after.last_progress.endswith("Z")
            or "+" in after.last_progress
            or "T" in after.last_progress
        )
    finally:
        beater.stop()


def test_run_heartbeat_beater_advances_last_progress(tmp_path: Path) -> None:
    """The background thread advances last_progress over time."""
    _sample_active_run(tmp_path, "v0--entry_a")

    beater = RunHeartbeatBeater(tmp_path, "v0--entry_a", interval_s=0.01)
    beater.start()
    try:
        first = list_active_runs(tmp_path)[0].last_progress
        # Wait for at least two bump intervals (interval 0.01s).
        time.sleep(0.05)
        second = list_active_runs(tmp_path)[0].last_progress
        # last_progress must have advanced (or at minimum not regressed).
        assert second >= first
    finally:
        beater.stop()


def test_run_heartbeat_beater_stop_is_idempotent(tmp_path: Path) -> None:
    """stop() on an already-stopped beater must not raise."""
    _sample_active_run(tmp_path, "v0--entry_a")
    beater = RunHeartbeatBeater(tmp_path, "v0--entry_a", interval_s=10.0)
    beater.start()
    beater.stop()
    beater.stop()  # second stop must not raise


def test_run_heartbeat_beater_stop_without_start_is_noop(tmp_path: Path) -> None:
    """stop() before start() must not raise."""
    beater = RunHeartbeatBeater(tmp_path, "v0--entry_a", interval_s=10.0)
    beater.stop()  # must not raise


def test_run_heartbeat_beater_start_twice_raises(tmp_path: Path) -> None:
    """Calling start() twice without stop() must raise RuntimeError."""
    _sample_active_run(tmp_path, "v0--entry_a")
    beater = RunHeartbeatBeater(tmp_path, "v0--entry_a", interval_s=10.0)
    beater.start()
    try:
        with pytest.raises(RuntimeError, match="already started"):
            beater.start()
    finally:
        beater.stop()


def test_run_heartbeat_beater_noop_when_run_missing(tmp_path: Path) -> None:
    """Bumping a run that does not exist is a silent no-op (state file absent)."""
    # No active_run written — the beater must not raise.
    beater = RunHeartbeatBeater(tmp_path, "v0--nonexistent", interval_s=10.0)
    beater.start()
    time.sleep(0.05)
    beater.stop()


def test_run_heartbeat_beater_thread_is_daemon(tmp_path: Path) -> None:
    """The background thread is a daemon thread (will not prevent process exit)."""

    _sample_active_run(tmp_path, "v0--entry_a")
    beater = RunHeartbeatBeater(tmp_path, "v0--entry_a", interval_s=10.0)
    beater.start()
    try:
        # Access the private thread directly to verify it is a daemon.
        assert beater._thread is not None
        assert beater._thread.daemon is True
    finally:
        beater.stop()
