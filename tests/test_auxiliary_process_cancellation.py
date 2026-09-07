"""Regression and export subprocesses are reaped before cancellation returns."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from zicato.proposer.episode_export import write_episode_export
from zicato.runtime import process as process_owner
from zicato.runtime.lock import pid_start_time
from zicato.tournament.regression import run_regression_suite

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["regression", "export"])
@pytest.mark.parametrize("boundary", ["spawn", "communication"])
async def test_cancellation_joins_spawn_and_reaps_owned_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str, boundary: str
) -> None:
    started, allow_spawn = asyncio.Event(), asyncio.Event()
    cleanup_entered, allow_cleanup = asyncio.Event(), asyncio.Event()
    observed: list[tuple[asyncio.subprocess.Process, float | None]] = []
    real_spawn = asyncio.create_subprocess_exec
    real_terminate = process_owner.terminate_process

    async def spawn(*args: Any, **kwargs: Any) -> asyncio.subprocess.Process:
        proc = await real_spawn(*args, **kwargs)
        observed.append((proc, pid_start_time(proc.pid)))
        started.set()
        if boundary == "spawn":
            await allow_spawn.wait()
        return proc

    async def terminate(*args: Any, **kwargs: Any) -> bool:
        cleanup_entered.set()
        await allow_cleanup.wait()
        return await real_terminate(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(process_owner, "terminate_process", terminate)
    (tmp_path / "tests").mkdir()
    command = (sys.executable, "-c", "import time; time.sleep(60)")
    if operation == "regression":
        work = run_regression_suite(tmp_path, test_command=command)
    else:
        binary = tmp_path / "export-command"
        binary.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(60)\n")
        binary.chmod(0o700)
        work = write_episode_export(binary, tmp_path)
    task = asyncio.create_task(work)
    try:
        await asyncio.wait_for(started.wait(), 3)
        task.cancel("original cancellation")
        if boundary == "communication":
            await asyncio.wait_for(cleanup_entered.wait(), 0.5)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not task.done(), "producer discarded a child before finalization"
        task.cancel("repeated cancellation")
        await asyncio.sleep(0)
        assert not task.done()
        allow_spawn.set()
        await asyncio.wait_for(cleanup_entered.wait(), 2)
        proc, token = observed[0]
        assert proc.returncode is None and token is not None
        assert pid_start_time(proc.pid) == token
        allow_cleanup.set()
        with pytest.raises(asyncio.CancelledError, match="original cancellation"):
            await asyncio.wait_for(task, 3)
        assert proc.returncode is not None
        with pytest.raises(ProcessLookupError):
            os.kill(proc.pid, 0)
    finally:
        allow_spawn.set()
        allow_cleanup.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        for proc, token in observed:
            if proc.returncode is None:
                assert token is not None and pid_start_time(proc.pid) == token
                proc.kill()
                await proc.wait()
