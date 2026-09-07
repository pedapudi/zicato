"""Exercise proposal cancellation in an isolated descendant-adoption process."""

from __future__ import annotations

import asyncio
import ctypes
import json
import os
import signal
import sys
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from tests._fake_foe import BUILD_ID, LOG_VERSION, RUNTIME_VERSION, Log
from tests.test_proposer_foe_agent import Workspace
from zicato.evolve.invocation import validated_invocation
from zicato.proposer import episode_process, foe_agent
from zicato.proposer.proposer import ProposerExhausted
from zicato.runtime.lock import (
    WorkspaceLockHeld,
    acquire_workspace_lock,
    pid_start_time,
    release_workspace_lock,
)
from zicato.runtime.state import list_active_runs


def runtime(boundary: str, arguments: list[str]) -> None:
    document = json.loads(Path(arguments[arguments.index("--config") + 1]).read_text())
    log_dir = Path(arguments[arguments.index("--log-dir") + 1])
    scratch = Path(document["grants"]["write"][0])
    reader, writer = os.pipe()
    child = os.fork()
    if child == 0:
        os.close(reader)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        if boundary == "leader-exit":
            os.close(0)
            os.close(1)
        os.write(writer, b"ready")
        os.close(writer)
        while True:
            signal.pause()
    os.close(writer)
    assert os.read(reader, 5) == b"ready"
    os.close(reader)
    (log_dir / "ownership.json").write_text(
        json.dumps({"pid": os.getpid(), "child": child, "scratch": str(scratch)})
    )
    if not boundary.startswith("handshake"):
        log = Log(log_dir, LOG_VERSION, None)
        log.emit(
            "episode/start",
            {"id": "owned-proposal", "runtime": {"version": RUNTIME_VERSION, "build": BUILD_ID}},
        )
        if boundary == "leader-exit":
            import time

            while not (log_dir / "end-leader").exists():
                time.sleep(0.005)
            log.emit("episode/end", {"outcome": {"kind": "failed", "error": "leader ended"}})
            os._exit(0)
    while True:
        signal.pause()


async def probe(root: Path, boundary: str) -> None:
    # Only this isolated controller adopts orphaned descendants.
    assert ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0) == 0
    workspace = Workspace(root, [])
    workspace.binary.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        f"sys.path.insert(0, {str(Path(__file__).resolve().parents[1])!r})\n"
        "from tests._proposal_cancellation_probe import runtime\n"
        f"runtime({boundary!r}, sys.argv[1:])\n"
    )
    captured = {}
    spawned, return_spawn, refused = asyncio.Event(), asyncio.Event(), asyncio.Event()
    allow_signals = asyncio.Event()
    refused_signals: list[signal.Signals] = []
    real_spawn = asyncio.create_subprocess_exec
    real_signal = episode_process.signal_owned_process
    real_config = foe_agent.resolve_foe_config

    async def spawn(*args, **kwargs):
        proc = await real_spawn(*args, **kwargs)
        captured["proc"] = proc
        captured["token"] = pid_start_time(proc.pid)
        spawned.set()
        if boundary == "spawn-cancel":
            await return_spawn.wait()
        return proc

    def defer_signal(*args, **kwargs):
        if not allow_signals.is_set():
            refused_signals.append(args[3])
            refused.set()
            return False
        return real_signal(*args, **kwargs)

    def config(binding):
        resolved = real_config(binding)
        if boundary.endswith("timeout"):
            resolved = replace(resolved, budget=replace(resolved.budget, seconds=0.2))
        return resolved

    async def invoke():
        async with validated_invocation(workspace.root, "e1", "proposal-owner") as invocation:
            return await workspace.agent().propose(
                replace(workspace.context(), writer=invocation.writer)
            )

    child = None
    child_token = None
    task = None
    with ExitStack() as patches:
        patches.enter_context(patch.object(asyncio, "create_subprocess_exec", spawn))
        patches.enter_context(patch.object(episode_process, "signal_owned_process", defer_signal))
        patches.enter_context(patch.object(episode_process, "_CANCEL_GRACE_S", 0.02))
        patches.enter_context(patch.object(episode_process, "_SIGNAL_GRACE_S", 0.02))
        patches.enter_context(patch.object(foe_agent, "resolve_foe_config", config))
        patches.enter_context(patch("zicato.check.require_workspace_valid", lambda *a, **k: None))
        task = asyncio.create_task(invoke())
        try:
            async with asyncio.timeout(5):
                await spawned.wait()
                ownership_file = workspace.episode_log() / "ownership.json"
                while not ownership_file.exists():
                    await asyncio.sleep(0.005)
                owned = json.loads(ownership_file.read_text())
                child = owned["child"]
                child_token = pid_start_time(child)
                scratch = Path(owned["scratch"])
                assert child_token is not None
                proc = captured["proc"]
                assert os.getpgid(child) == proc.pid

                if boundary == "spawn-cancel":
                    task.cancel("original proposal cancellation")
                    await asyncio.sleep(0.02)
                    assert not task.done(), "proposal returned while startup still owned a process"
                    assert scratch.exists(), "startup cancellation removed live proposal inputs"
                    return_spawn.set()
                elif boundary.endswith("cancel"):
                    task.cancel("original proposal cancellation")
                elif boundary == "leader-exit":
                    while not list_active_runs(workspace.root):
                        await asyncio.sleep(0.005)
                    (workspace.episode_log() / "end-leader").touch()

                while not refused.is_set():
                    assert not task.done(), "proposal returned before group termination"
                    await asyncio.sleep(0.005)
                assert not task.done(), "proposal returned before group termination"
                if boundary == "leader-exit":
                    assert proc.returncode == 0
                    assert pid_start_time(proc.pid) is None
                assert scratch.exists(), "live proposal lost its scratch inputs"
                assert len(list_active_runs(workspace.root)) == 1
                for _ in range(3):
                    task.cancel("repeated proposal cancellation")
                    await asyncio.sleep(0.005)
                assert not task.done(), "repeated cancellation interrupted proposal cleanup"
                try:
                    competitor = acquire_workspace_lock(workspace.root, "competitor")
                except WorkspaceLockHeld:
                    pass
                else:
                    release_workspace_lock(competitor)
                    raise AssertionError("competing invocation acquired live proposal ownership")

                while len(refused_signals) < 3:
                    await asyncio.sleep(0.005)
                assert set(refused_signals) == {signal.SIGTERM}, (
                    "signal refusal advanced escalation before TERM delivery",
                    refused_signals,
                )
                allow_signals.set()
                if boundary.endswith("timeout"):
                    try:
                        await task
                    except ProposerExhausted as exc:
                        assert exc.limit == "seconds"
                    else:
                        raise AssertionError("proposal deadline did not remain the primary failure")
                else:
                    try:
                        await task
                    except asyncio.CancelledError as exc:
                        expected = (
                            "repeated proposal cancellation"
                            if boundary == "leader-exit"
                            else "original proposal cancellation"
                        )
                        assert str(exc) == expected
                    else:
                        raise AssertionError("proposal cancellation did not propagate")
                assert not scratch.exists()
                assert proc.returncode == (0 if boundary == "leader-exit" else -signal.SIGTERM)
                reaped, status = os.waitpid(child, 0)
                assert reaped == child and os.waitstatus_to_exitcode(status) == -signal.SIGKILL
                child = None
                assert list_active_runs(workspace.root) == []
                writer = acquire_workspace_lock(workspace.root, "successor")
                release_workspace_lock(writer)
        finally:
            return_spawn.set()
            allow_signals.set()
            proc = captured.get("proc")
            if proc is not None and proc.returncode is None:
                assert pid_start_time(proc.pid) == captured["token"]
                os.kill(proc.pid, signal.SIGKILL)
            if child is not None:
                if pid_start_time(child) == child_token:
                    os.kill(child, signal.SIGKILL)
            if proc is not None:
                await proc.wait()
            if child is not None:
                os.waitpid(child, 0)
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(probe(Path(sys.argv[1]), sys.argv[2]))
