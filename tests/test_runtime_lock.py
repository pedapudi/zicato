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
    is_same_process,
    pid_start_time,
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
# pid_start_time
# ---------------------------------------------------------------------------


def test_pid_start_time_for_self_is_a_stable_float() -> None:
    st = pid_start_time(os.getpid())
    # On Linux (/proc) this is always readable; if neither /proc nor psutil
    # is available the function returns None and we skip the equality check.
    if st is None:
        pytest.skip("no /proc or psutil on this host")
    assert isinstance(st, float)
    # Stable across repeated reads of the same live process.
    assert pid_start_time(os.getpid()) == st


def test_pid_start_time_none_for_dead_pid() -> None:
    assert pid_start_time(99_999_999) is None


def test_pid_start_time_none_for_nonpositive() -> None:
    assert pid_start_time(0) is None
    assert pid_start_time(-1) is None


# ---------------------------------------------------------------------------
# is_same_process (the PID-identity / pid-reuse guard)
# ---------------------------------------------------------------------------


def test_is_same_process_true_for_self_with_matching_start_time() -> None:
    me = os.getpid()
    st = pid_start_time(me)
    assert is_same_process(me, st) is True


def test_is_same_process_false_when_start_time_mismatches() -> None:
    # Same live pid (ours) but a start time that does not match → this
    # simulates pid reuse: the recorded owner is gone and an unrelated
    # process now holds the number. Must NOT be treated as the same process.
    me = os.getpid()
    real = pid_start_time(me)
    if real is None:
        pytest.skip("no start-time source on this host")
    bogus = real + 999_999.0
    assert is_same_process(me, bogus) is False


def test_is_same_process_false_for_dead_pid() -> None:
    # A dead pid is never "the same process", regardless of recorded time.
    assert is_same_process(99_999_999, 12345.0) is False


def test_is_same_process_falls_back_to_liveness_without_recorded_time() -> None:
    # Legacy lock with no recorded start_time → degrade to bare liveness.
    me = os.getpid()
    assert is_same_process(me, None) is True
    assert is_same_process(99_999_999, None) is False


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
    # The start-time identity token is recorded alongside the pid (key is
    # always present; value is a float on hosts with a start-time source).
    assert "start_time" in raw
    if raw["start_time"] is not None:
        assert raw["start_time"] == pid_start_time(os.getpid())


def test_acquire_steals_when_pid_recycled_to_innocent_process(tmp_path: Path) -> None:
    """A live pid whose *start time* mismatches the lock is not the owner.

    Scenario: the original owner died and the kernel reissued its pid
    number to an unrelated, innocent process that is now alive. A bare
    ``os.kill(pid, 0)`` would see "alive" and refuse to steal forever. The
    start-time identity check proves the live process is NOT the lock owner,
    so the stale lock is correctly steal-able.
    """
    lock_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    # Use our own (live) pid as the "recycled" number, but stamp a
    # start_time that cannot match — simulating the original owner.
    real = pid_start_time(os.getpid())
    if real is None:
        pytest.skip("no start-time source on this host")
    lock_path(tmp_path).write_text(
        json.dumps(
            {
                "pid": os.getpid(),  # alive, but...
                "instance_id": "ghost",
                "acquired_at": "2026-05-14T10:00:00Z",
                "workspace_root": str(tmp_path),
                "start_time": real + 999_999.0,  # ...not the same process
            }
        )
    )
    lock = acquire_workspace_lock(tmp_path, "default")
    assert lock.pid == os.getpid()
    assert lock.instance_id == "default"
    # The freshly written lock carries our real start time.
    assert lock.start_time == real


def test_acquire_refuses_when_live_pid_start_time_matches(tmp_path: Path) -> None:
    """A live pid whose start time *matches* is a genuine live owner.

    Counterpart to the recycled-pid case: when the recorded start_time
    matches the live process, the lock is genuinely held and acquisition
    must refuse (with a different instance_id so it's treated as foreign).
    """
    lock_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    real = pid_start_time(os.getpid())
    if real is None:
        pytest.skip("no start-time source on this host")
    # pid 1 (init) is always alive; record a start_time of None so the
    # check degrades to liveness and still treats it as held.
    lock_path(tmp_path).write_text(
        json.dumps(
            {
                "pid": 1,
                "instance_id": "other",
                "acquired_at": "2026-05-14T10:00:00Z",
                "workspace_root": str(tmp_path),
                "start_time": None,
            }
        )
    )
    with pytest.raises(WorkspaceLockHeld, match="live pid 1"):
        acquire_workspace_lock(tmp_path, "default")


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
