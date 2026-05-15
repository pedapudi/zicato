"""Tests for ``zicato.runtime.lock`` — pid-based workspace lock."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from zicato.runtime.lock import (
    WorkspaceLock,
    WorkspaceLockHeld,
    acquire_workspace_lock,
    is_pid_alive,
    release_workspace_lock,
)
from zicato.runtime.paths import lock_path

# ---------------------------------------------------------------------------
# is_pid_alive
# ---------------------------------------------------------------------------


def test_is_pid_alive_for_self() -> None:
    assert is_pid_alive(os.getpid()) is True


def test_is_pid_alive_false_for_pid_zero() -> None:
    # Pid 0 has special meaning on POSIX (broadcast); treat as not alive.
    assert is_pid_alive(0) is False


def test_is_pid_alive_false_for_negative_pid() -> None:
    assert is_pid_alive(-1) is False


def test_is_pid_alive_false_for_definitely_dead_pid() -> None:
    # Pick a very large pid that almost certainly does not exist. On
    # Linux pid_max defaults to 32768 or 4194304; either way 99999999
    # is well past it.
    assert is_pid_alive(99_999_999) is False


# ---------------------------------------------------------------------------
# acquire_workspace_lock
# ---------------------------------------------------------------------------


def test_acquire_writes_lock_file(tmp_path: Path) -> None:
    lock = acquire_workspace_lock(tmp_path, "default")
    assert lock.pid == os.getpid()
    assert lock.instance_id == "default"
    assert lock.workspace_root == tmp_path

    # On-disk shape matches.
    raw = json.loads(lock_path(tmp_path).read_text())
    assert raw["pid"] == os.getpid()
    assert raw["instance_id"] == "default"
    assert raw["acquired_at"].endswith("Z")


def test_acquire_same_pid_idempotent(tmp_path: Path) -> None:
    first = acquire_workspace_lock(tmp_path, "default")
    second = acquire_workspace_lock(tmp_path, "default")
    # Both descriptors refer to the same lock; acquired_at is preserved
    # across the idempotent second call.
    assert first == second


def test_acquire_different_pid_alive_raises(tmp_path: Path) -> None:
    """Simulate a lock held by another live pid by writing the file by hand.

    We use the current pid as the "other live pid" — that's guaranteed
    alive, and the acquire path treats it as a foreign pid because we
    construct the on-disk record with a different instance_id and the
    actual acquire call runs with the same pid... so we need to fake
    the on-disk pid differently. Use any live system pid; pid 1 (init)
    is always alive on Linux.
    """
    lock_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    lock_path(tmp_path).write_text(
        json.dumps(
            {
                "pid": 1,  # init — always alive
                "instance_id": "other",
                "acquired_at": "2026-05-14T10:00:00Z",
                "workspace_root": str(tmp_path),
            }
        )
    )
    with pytest.raises(WorkspaceLockHeld, match="live pid 1"):
        acquire_workspace_lock(tmp_path, "default")


def test_acquire_steals_dead_pid_by_default(tmp_path: Path) -> None:
    lock_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    lock_path(tmp_path).write_text(
        json.dumps(
            {
                "pid": 99_999_999,  # definitely dead
                "instance_id": "ghost",
                "acquired_at": "2026-05-14T10:00:00Z",
                "workspace_root": str(tmp_path),
            }
        )
    )
    lock = acquire_workspace_lock(tmp_path, "default")
    assert lock.pid == os.getpid()
    assert lock.instance_id == "default"


def test_acquire_refuses_to_steal_when_steal_stale_false(tmp_path: Path) -> None:
    lock_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    lock_path(tmp_path).write_text(
        json.dumps(
            {
                "pid": 99_999_999,
                "instance_id": "ghost",
                "acquired_at": "2026-05-14T10:00:00Z",
                "workspace_root": str(tmp_path),
            }
        )
    )
    with pytest.raises(WorkspaceLockHeld, match="stale pid"):
        acquire_workspace_lock(tmp_path, "default", steal_stale=False)


def test_acquire_creates_runtime_dir(tmp_path: Path) -> None:
    # Bare tmp_path with no .zicato/ tree yet; acquire must build it.
    assert not (tmp_path / ".zicato").exists()
    acquire_workspace_lock(tmp_path, "default")
    assert lock_path(tmp_path).exists()


# ---------------------------------------------------------------------------
# release_workspace_lock
# ---------------------------------------------------------------------------


def test_release_removes_file(tmp_path: Path) -> None:
    lock = acquire_workspace_lock(tmp_path, "default")
    assert lock_path(tmp_path).exists()
    release_workspace_lock(lock)
    assert not lock_path(tmp_path).exists()


def test_release_idempotent(tmp_path: Path) -> None:
    lock = acquire_workspace_lock(tmp_path, "default")
    release_workspace_lock(lock)
    # Second release is a no-op.
    release_workspace_lock(lock)


def test_release_no_op_when_file_missing(tmp_path: Path) -> None:
    # Fabricate a lock handle without ever writing the file.
    fake = WorkspaceLock(
        pid=os.getpid(),
        instance_id="default",
        acquired_at="2026-05-14T10:00:00Z",
        workspace_root=tmp_path,
    )
    release_workspace_lock(fake)  # must not raise


def test_release_refuses_to_remove_foreign_lock(tmp_path: Path) -> None:
    """Calling release with a stale handle does not stomp a successor.

    Scenario: process A acquires, dies, process B steals, A's handle
    is still in memory somewhere and ends up calling release. The
    release path must leave B's lock alone.
    """
    # Write a lock that belongs to a different pid/instance.
    lock_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    lock_path(tmp_path).write_text(
        json.dumps(
            {
                "pid": 1,  # live (init), different from us
                "instance_id": "successor",
                "acquired_at": "2026-05-14T10:00:00Z",
                "workspace_root": str(tmp_path),
            }
        )
    )
    stale = WorkspaceLock(
        pid=os.getpid(),
        instance_id="default",
        acquired_at="2026-05-14T09:00:00Z",
        workspace_root=tmp_path,
    )
    release_workspace_lock(stale)
    # File still present — we did not stomp the successor.
    assert lock_path(tmp_path).exists()
    raw = json.loads(lock_path(tmp_path).read_text())
    assert raw["instance_id"] == "successor"
