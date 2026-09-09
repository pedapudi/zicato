"""Cancel a worker group inside an isolated process that can reap its descendants."""

from __future__ import annotations

import asyncio
import ctypes
import json
import os
import signal
import sys
import time
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from tests._runtime_builders import make_generation
from tests._subprocess_worker_support import SleepingAdapter, evaluation_call_llm, target_call_llm
from zicato.core import BoardEntry, RuntimeConfig, ScoringWeights
from zicato.evolve import round_entry
from zicato.runtime.lock import WorkspaceLockHeld, acquire_workspace_lock, pid_start_time
from zicato.runtime.paths import active_run_path
from zicato.runtime.writer import workspace_writer
from zicato.storage import atomic_write_json
from zicato.tournament import runner, worker_transport


class DescendantAdapter(SleepingAdapter):
    def worker_spec(self) -> dict[str, str]:
        return {"kind": "import", "factory": "tests._worker_cancellation_probe:DescendantAdapter"}

    def load(self, generation_root: Path):
        reader, writer = os.pipe()
        child_pid = os.fork()
        if child_pid == 0:
            os.close(reader)
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            os.write(writer, b"ready")
            os.close(writer)
            while True:
                signal.pause()
        os.close(writer)
        assert os.read(reader, 5) == b"ready"
        os.close(reader)
        atomic_write_json(generation_root / "descendant.json", {"pid": child_pid})
        return super().load(generation_root)


async def probe(workspace: Path, *, invocation: bool = False) -> None:
    # Adoption applies only to this dedicated test process.
    assert ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0) == 0
    workspace.mkdir()
    generation = make_generation(workspace)
    worker_transport._SIGTERM_TO_SIGKILL_GRACE_S = 0.05
    config = RuntimeConfig(
        instance_id="cancellation-probe",
        workspace_root=workspace,
        target_call_llm=target_call_llm,
        evaluation_call_llm=evaluation_call_llm,
        supervisor_kill_wait_s=0.05,
        host_worker_permits=1,
        worker_permit_dir=workspace.parent / "permits",
    )
    captured = {}
    real_spawn = asyncio.create_subprocess_exec

    async def spawn(*args, **kwargs):
        proc = await real_spawn(*args, **kwargs)
        captured["proc"] = proc
        captured["start_time"] = pid_start_time(proc.pid)
        payload = json.loads(Path(args[-1]).read_text())
        captured["snapshot"] = Path(payload["runtime_context"]["run"]["snapshot_root"])
        return proc

    child_pid = None
    child_start_time = None
    retry_entered, allow_retry = asyncio.Event(), asyncio.Event()
    real_terminate = runner._terminate_worker
    attempts = 0

    async def defer_termination(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts <= 2:
            return False
        retry_entered.set()
        await allow_retry.wait()
        return await real_terminate(*args, **kwargs)

    executions = 0

    async def execute_worker(**kwargs):
        nonlocal executions
        executions += 1
        assert executions == 1, "competing invocation reached execution"
        invocation_context = kwargs.get("invocation")
        async with workspace_writer(
            workspace,
            writer=invocation_context.writer if invocation_context is not None else None,
            instance_id=config.instance_id,
            cleanup=lambda: runner.drain_worker_cleanup(workspace),
        ) as writer:
            return await runner._run_single(
                writer=writer,
                adapter=DescendantAdapter(),
                generation=generation,
                entry=BoardEntry(
                    id="descendant", kind="single_turn", input="", wall_clock_budget_seconds=60
                ),
                weights=ScoringWeights(),
                config=config,
                workspace_root=workspace,
                epoch_id="e0",
                side="parent",
            )

    with ExitStack() as patches:
        patches.enter_context(patch.object(asyncio, "create_subprocess_exec", spawn))
        if invocation:
            patches.enter_context(patch.object(runner, "_terminate_worker", defer_termination))
            patches.enter_context(patch.object(round_entry, "_evolve_once", execute_worker))
            patches.enter_context(
                patch("zicato.check.require_workspace_valid", lambda *a, **k: None)
            )
        task = asyncio.create_task(
            round_entry.evolve_once(workspace_root=workspace) if invocation else execute_worker()
        )
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                snapshot = captured.get("snapshot")
                if snapshot is not None and (snapshot / "descendant.json").exists():
                    break
                await asyncio.sleep(0.01)
            else:
                raise AssertionError("worker did not create its descendant")
            child_pid = json.loads((snapshot / "descendant.json").read_text())["pid"]
            child_start_time = pid_start_time(child_pid)
            proc = captured["proc"]
            assert child_start_time is not None
            assert os.getpgid(child_pid) == os.getpgid(proc.pid) == proc.pid
            task.cancel("cancel descendant group")
            if invocation:
                async with asyncio.timeout(5):
                    while not runner._retained_worker_resources and not task.done():
                        await asyncio.sleep(0.01)
                assert not task.done(), "invocation returned with a retained worker"
                await asyncio.wait_for(retry_entered.wait(), timeout=5)
                assert attempts == 3, "invocation accepted an unconfirmed cleanup attempt"
                for _ in range(3):
                    task.cancel("repeat cancellation")
                    await asyncio.sleep(0)
                assert not task.done(), "cancellation abandoned worker ownership"
                assert proc.returncode is None and snapshot.exists()
                assert all(
                    owner.permit.held for owner in runner._retained_worker_resources.values()
                )
                try:
                    await round_entry.evolve_once(workspace_root=workspace)
                except WorkspaceLockHeld:
                    pass
                else:
                    raise AssertionError("competing invocation acquired the workspace")
                allow_retry.set()
            try:
                await asyncio.wait_for(task, timeout=5)
            except asyncio.CancelledError as exc:
                if invocation:
                    assert exc.args == ("cancel descendant group",)
            else:
                raise AssertionError("cancelled worker returned evaluation evidence")
            assert proc.returncode == -signal.SIGTERM
            reaped, status = os.waitpid(child_pid, os.WNOHANG)
            assert reaped == child_pid, "resistant descendant survived worker cancellation"
            child_pid = None
            assert os.waitstatus_to_exitcode(status) == -signal.SIGKILL
            assert not snapshot.exists()
            assert not active_run_path(workspace, f"{generation.id}--descendant").exists()
            assert not runner._retained_worker_resources
            if invocation:
                with acquire_workspace_lock(workspace, "following-invocation"):
                    pass
        finally:
            allow_retry.set()
            proc = captured.get("proc")
            if proc is not None and proc.returncode is None:
                assert pid_start_time(proc.pid) == captured["start_time"]
                assert os.getpgid(proc.pid) == proc.pid
                os.killpg(proc.pid, signal.SIGKILL)
                await asyncio.wait_for(proc.wait(), timeout=5)
            if child_pid is not None:
                assert pid_start_time(child_pid) == child_start_time
                assert os.getpgid(child_pid) == proc.pid
                os.kill(child_pid, signal.SIGKILL)
                await asyncio.wait_for(asyncio.to_thread(os.waitpid, child_pid, 0), timeout=5)
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await runner.retry_worker_cleanup(workspace)


if __name__ == "__main__":
    asyncio.run(probe(Path(sys.argv[1]), invocation="--invocation" in sys.argv[2:]))
