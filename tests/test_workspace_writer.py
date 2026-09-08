"""Workspace mutation ownership is exclusive across invocations."""

from __future__ import annotations

import asyncio
import json
import multiprocessing
import os
from pathlib import Path
from typing import Any

import pytest

from zicato.runtime import lock as locks
from zicato.runtime.paths import lock_path


def test_fork_child_does_not_retain_or_unlock_parent_lease(tmp_path: Path) -> None:
    writer = locks.acquire_workspace_lock(tmp_path, "parent")
    release_read, release_write = os.pipe()
    ready_read, ready_write = os.pipe()
    child = os.fork()
    if child == 0:
        os.close(release_write)
        os.close(ready_read)
        try:
            try:
                contender = locks.acquire_workspace_lock(tmp_path, "child")
            except locks.WorkspaceLockHeld:
                os.write(ready_write, b"refused")
            else:
                locks.release_workspace_lock(contender)
                os.write(ready_write, b"acquired")
            os.close(ready_write)
            os.read(release_read, 1)
            os._exit(0)
        except BaseException:
            os._exit(1)
    os.close(release_read)
    os.close(ready_write)
    try:
        assert os.read(ready_read, 20) == b"refused"
        locks.release_workspace_lock(writer)
        with locks.acquire_workspace_lock(tmp_path, "following-invocation"):
            pass
    finally:
        locks.release_workspace_lock(writer)
        os.close(ready_read)
        os.close(release_write)
        _, status = os.waitpid(child, 0)
    assert os.waitstatus_to_exitcode(status) == 0


def test_failed_scope_entry_releases_lease_and_preserves_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    writer = locks.acquire_workspace_lock(tmp_path, "invocation")
    original = OSError("writer metadata unavailable")

    def fail_validation(*_: Any) -> None:
        raise original

    try:
        with monkeypatch.context() as scoped:
            scoped.setattr(locks, "validate_workspace_lock", fail_validation)
            with pytest.raises(OSError) as raised, writer:
                pytest.fail("failed validation entered the scope")
        assert raised.value is original
        with locks.acquire_workspace_lock(tmp_path, "following-invocation"):
            pass
    finally:
        locks.release_workspace_lock(writer)


@pytest.mark.parametrize("operation", ["enter", "release"])
def test_metadata_failure_releases_lease_and_allows_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    writer = locks.acquire_workspace_lock(tmp_path, "invocation")
    original = OSError("writer metadata unavailable")

    def unavailable_backend(*_: Any, **__: Any) -> Any:
        raise original

    try:
        with monkeypatch.context() as scoped:
            scoped.setattr(locks, "workspace_backend", unavailable_backend)
            with pytest.raises(OSError) as raised:
                if operation == "enter":
                    with writer:
                        pytest.fail("unreadable metadata entered the scope")
                else:
                    locks.release_workspace_lock(writer)
        assert raised.value is original
        assert lock_path(tmp_path).exists()
        with pytest.raises(locks.WorkspaceLockHeld):
            locks.acquire_workspace_lock(tmp_path, "strict-contender", steal_stale=False)
        with locks.acquire_workspace_lock(tmp_path, "following-invocation"):
            pass
    finally:
        locks.release_workspace_lock(writer)


def test_same_process_cannot_acquire_twice(tmp_path: Path) -> None:
    first = locks.acquire_workspace_lock(tmp_path, "invocation")
    try:
        with pytest.raises(locks.WorkspaceLockHeld):
            locks.acquire_workspace_lock(tmp_path, "invocation")
    finally:
        locks.release_workspace_lock(first)


def test_releasing_predecessor_preserves_same_process_successor(tmp_path: Path) -> None:
    first = locks.acquire_workspace_lock(tmp_path, "invocation")
    locks.release_workspace_lock(first)
    successor = locks.acquire_workspace_lock(tmp_path, "invocation")
    try:
        locks.release_workspace_lock(first)
        assert json.loads(lock_path(tmp_path).read_text()) == successor.to_dict()
    finally:
        locks.release_workspace_lock(successor)


def test_status_descriptor_cannot_release_writer(tmp_path: Path) -> None:
    writer = locks.acquire_workspace_lock(tmp_path, "invocation")
    try:
        status = locks.read_workspace_lock(tmp_path)
        assert status is not None
        for name in ("progress_log", "tournament_log"):
            with pytest.raises(locks.WorkspaceLockHeld):
                getattr(status, name)
        locks.release_workspace_lock(status)
        assert lock_path(tmp_path).exists()
    finally:
        locks.release_workspace_lock(writer)


def test_only_acquired_handle_authorizes_matching_workspace(tmp_path: Path) -> None:
    with locks.acquire_workspace_lock(tmp_path, "invocation") as writer:
        locks.validate_workspace_lock(writer, tmp_path)
        for handle, root in (
            (locks.WorkspaceLock.from_dict(writer.to_dict()), tmp_path),
            (writer, tmp_path / "different-workspace"),
        ):
            with pytest.raises(locks.WorkspaceLockHeld):
                locks.validate_workspace_lock(handle, root)
    with pytest.raises(locks.WorkspaceLockHeld):
        locks.validate_workspace_lock(writer, tmp_path)
    for name in ("progress_log", "tournament_log"):
        with pytest.raises(locks.WorkspaceLockHeld):
            getattr(writer, name)


@pytest.mark.parametrize("rounds", [None, 1])
def test_public_entry_points_refuse_competing_writer_before_validation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, rounds: int | None
) -> None:
    from zicato.orchestrator import evolve_n_rounds, evolve_once

    def unexpected_validation(*_: Any, **__: Any) -> None:
        pytest.fail("a competing invocation reached validation")

    monkeypatch.setattr("zicato.check.require_workspace_valid", unexpected_validation)
    with locks.acquire_workspace_lock(tmp_path, "owner"):
        before = {
            path.relative_to(tmp_path): path.read_bytes()
            for path in tmp_path.rglob("*")
            if path.is_file()
        }
        with pytest.raises(locks.WorkspaceLockHeld):
            asyncio.run(
                evolve_once(workspace_root=tmp_path)
                if rounds is None
                else evolve_n_rounds(rounds=rounds, workspace_root=tmp_path)
            )
        assert before == {
            path.relative_to(tmp_path): path.read_bytes()
            for path in tmp_path.rglob("*")
            if path.is_file()
        }


@pytest.mark.parametrize("exit_kind", ["completed", "rejected", "failed", "cancelled"])
def test_public_invocations_retain_and_release_ownership(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, exit_kind: str
) -> None:
    from zicato.orchestrator import evolve_n_rounds, evolve_once

    monkeypatch.setattr("zicato.check.require_workspace_valid", lambda *a, **k: None)

    async def exercise() -> None:
        entered = asyncio.Event()
        finish = asyncio.Event()

        async def execute_owned(**kwargs: Any) -> str:
            writer = kwargs["invocation"].writer
            locks.validate_workspace_lock(writer, tmp_path)
            entered.set()
            await finish.wait()
            if exit_kind == "failed":
                raise RuntimeError("round persistence failed")
            return exit_kind

        monkeypatch.setattr("zicato.evolve.round_entry._evolve_once", execute_owned)
        monkeypatch.setattr("zicato.evolve.loop._evolve_n_rounds", execute_owned)
        for multi in (False, True):
            entered.clear()
            finish.clear()
            task = asyncio.create_task(
                evolve_n_rounds(rounds=1, workspace_root=tmp_path)
                if multi
                else evolve_once(workspace_root=tmp_path)
            )
            try:
                await asyncio.wait_for(entered.wait(), timeout=2)
                for contender in (
                    evolve_once(workspace_root=tmp_path),
                    evolve_n_rounds(rounds=1, workspace_root=tmp_path),
                ):
                    with pytest.raises(locks.WorkspaceLockHeld):
                        await contender
                if exit_kind == "cancelled":
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await task
                else:
                    finish.set()
                    if exit_kind == "failed":
                        with pytest.raises(RuntimeError, match="round persistence failed"):
                            await task
                    else:
                        assert await task == exit_kind
            finally:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
            with locks.acquire_workspace_lock(tmp_path, "following-invocation"):
                pass

    asyncio.run(exercise())


def _compete(root: str, barrier: Any, finish: Any, results: Any) -> None:
    barrier.wait(timeout=10)
    try:
        writer = locks.acquire_workspace_lock(Path(root), "contender")
    except locks.WorkspaceLockHeld:
        results.put("refused")
        return
    results.put("acquired")
    try:
        assert finish.wait(timeout=10)
    finally:
        locks.release_workspace_lock(writer)


def test_competing_processes_have_one_writer(tmp_path: Path) -> None:
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(3)
    finish = context.Event()
    results = context.Queue()
    children = [
        context.Process(target=_compete, args=(str(tmp_path), barrier, finish, results))
        for _ in range(2)
    ]
    for child in children:
        child.start()
    try:
        barrier.wait(timeout=10)
        assert sorted(results.get(timeout=10) for _ in children) == ["acquired", "refused"]
    finally:
        finish.set()
        for child in children:
            child.join(timeout=10)
            if child.is_alive():
                child.kill()
                child.join(timeout=10)
        results.close()
    assert all(child.exitcode == 0 for child in children)
