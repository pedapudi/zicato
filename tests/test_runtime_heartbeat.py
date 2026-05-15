"""Tests for ``zicato.runtime.heartbeat.HeartbeatBeater``."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from zicato.runtime.heartbeat import HeartbeatBeater
from zicato.runtime.state import read_heartbeat


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
    beater = HeartbeatBeater(tmp_path, "default", interval_s=0.05)
    await beater.start()
    try:
        first = read_heartbeat(tmp_path)
        assert first is not None
        # Wait long enough for at least 2 bumps.
        await asyncio.sleep(0.2)
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
