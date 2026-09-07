"""Invocation cleanup preserves ownership and the invoking task's context."""

from __future__ import annotations

import asyncio
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tests._orchestrator_harness import bootstrap_workspace
from zicato.evolve import lifecycle_services, loop, round_entry
from zicato.evolve.invocation import validated_invocation
from zicato.runtime.heartbeat import HeartbeatBeater
from zicato.runtime.lock import WorkspaceLockHeld, acquire_workspace_lock
from zicato.telemetry.meta_loop import (
    current_meta_emitter,
    reset_current_emitter,
    set_current_emitter,
)


@pytest.mark.parametrize(
    "failure_at",
    ["stream_setup", "mutation_surface", "heartbeat_start", "terminal", "heartbeat_stop"],
)
def test_loop_failure_closes_every_acquired_resource(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure_at: str
) -> None:
    workspace, epoch_id = bootstrap_workspace(tmp_path)
    closed: list[str] = []
    original = RuntimeError(f"{failure_at} failed")
    monkeypatch.setattr("zicato.check.require_workspace_valid", lambda *a, **k: None)

    def fail(*_: Any, **__: Any) -> Any:
        raise original

    def stream(*_: Any, **__: Any) -> Any:
        if failure_at == "stream_setup":
            raise original
        return SimpleNamespace(close=lambda: closed.append("stream"))

    class Emitter:
        session_id = "teardown-test"

        @asynccontextmanager
        async def span(self, *_: Any, **__: Any) -> Any:
            yield None

        async def close(self) -> None:
            closed.append("emitter")

    emitter = Emitter()
    monkeypatch.setattr(loop, "install_log_stream", stream)
    monkeypatch.setattr(
        lifecycle_services,
        "_resolve_or_launch_harmonograf",
        lambda *_: ("", SimpleNamespace(shutdown=lambda: closed.append("server"))),
    )
    monkeypatch.setattr(lifecycle_services, "_build_meta_loop_emitter_safe", lambda *_: emitter)
    real_start, real_stop = HeartbeatBeater.start, HeartbeatBeater.stop

    async def start(beater: HeartbeatBeater) -> None:
        await real_start(beater)
        if failure_at == "heartbeat_start":
            raise original

    async def stop(beater: HeartbeatBeater) -> None:
        await real_stop(beater)
        closed.append("heartbeat")
        if failure_at == "heartbeat_stop":
            raise original

    async def round_body(**_: Any) -> Any:
        from zicato.evolve.round_api import EvolveRoundOutcome

        return EvolveRoundOutcome("v0", "v1", "rejected", "test", 1.0, 2.0, 1.0)

    monkeypatch.setattr(HeartbeatBeater, "start", start)
    monkeypatch.setattr(HeartbeatBeater, "stop", stop)
    monkeypatch.setattr(round_entry, "_evolve_once", round_body)
    if failure_at == "mutation_surface":
        monkeypatch.setattr("zicato.workspace_loader.activate_mutation_surface", fail)
    if failure_at == "terminal":
        monkeypatch.setattr("zicato.evolve.dashboard_projection._mark_run_terminal", fail)

    async def exercise() -> None:
        previous = Emitter()
        token = set_current_emitter(previous)  # type: ignore[arg-type]
        try:
            with pytest.raises(RuntimeError) as raised:
                await loop.evolve_n_rounds(
                    rounds=1,
                    workspace_root=workspace,
                    epoch_id=epoch_id,
                    target_call_llm=fail,
                    evaluation_call_llm=fail,
                    stop_on_degenerate_health=False,
                )
            assert raised.value is original
            assert current_meta_emitter() is previous
        finally:
            reset_current_emitter(token)

    asyncio.run(exercise())
    expected = [] if failure_at == "stream_setup" else ["stream"]
    if failure_at not in {"stream_setup", "mutation_surface"}:
        expected = ["heartbeat", "emitter", "server", "stream"]
    assert sorted(closed) == sorted(expected)
    if "emitter" in closed:
        assert closed.index("emitter") < closed.index("server")
    with acquire_workspace_lock(workspace, "following-invocation"):
        pass


@pytest.mark.parametrize("body_fails", [False, True])
def test_repeated_cancellation_waits_for_cleanup_and_preserves_primary_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body_fails: bool
) -> None:
    monkeypatch.setattr("zicato.check.require_workspace_valid", lambda *a, **k: None)

    async def exercise() -> None:
        started, finish = asyncio.Event(), asyncio.Event()
        closed: list[str] = []
        original = RuntimeError("round persistence failed")

        async def delayed_cleanup() -> None:
            started.set()
            await finish.wait()
            closed.append("async-resource")
            raise OSError("resource close failed")

        async def invocation() -> None:
            async with validated_invocation(tmp_path, None, "owner") as context:
                context.resources.callback(closed.append, "sync-resource")
                context.resources.push_async_callback(delayed_cleanup)
                if body_fails:
                    raise original

        task = asyncio.create_task(invocation())
        try:
            await asyncio.wait_for(started.wait(), timeout=2)
            for _ in range(3):
                task.cancel("stop invocation")
                await asyncio.sleep(0)
            assert not task.done()
            assert not closed
            with pytest.raises(WorkspaceLockHeld):
                acquire_workspace_lock(tmp_path, "contender")
            finish.set()
            if body_fails:
                with pytest.raises(RuntimeError) as raised:
                    await task
                assert raised.value is original
            else:
                with pytest.raises(asyncio.CancelledError, match="stop invocation"):
                    await task
            assert closed == ["async-resource", "sync-resource"]
            with acquire_workspace_lock(tmp_path, "following-invocation"):
                pass
        finally:
            finish.set()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(exercise())


def test_invocation_retains_writer_until_resistant_descendant_exits(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "tests._worker_cancellation_probe",
            str(tmp_path / "ws"),
            "--invocation",
        ],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
