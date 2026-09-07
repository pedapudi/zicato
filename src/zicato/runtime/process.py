"""Communicate with an isolated child while retaining its process group."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Sequence
from pathlib import Path
from subprocess import CompletedProcess
from typing import Any

from zicato.runtime.lock import group_has_live_members, pid_start_time
from zicato.util.async_tasks import finish_task

log = logging.getLogger(__name__)

DEFAULT_SIGNAL_GRACE_S = 5.0


def processes_gone(
    proc: Any, *, expected_start_time: float | None = None, pgid: int | None = None
) -> bool:
    """Confirm reaping and absence of the captured group, or explicit leader-only scope.

    Completion needs no start token once the direct child has been reaped.
    Signalling separately requires the identity captured at spawn.
    """
    from zicato.runtime.lock import group_has_live_members  # noqa: PLC0415

    if proc.returncode is None:
        return False
    if pgid is None:
        return True
    if pgid <= 1 or pgid != proc.pid or pgid == os.getpgrp():
        return False
    return not group_has_live_members(pgid)


async def terminate_process(
    proc: Any,
    *,
    expected_start_time: float | None = None,
    pgid: int | None = None,
    signal_grace_s: float = DEFAULT_SIGNAL_GRACE_S,
) -> bool:
    """Bounded fallback termination of the owned leader and its process group.

    The caller captures the start token and group at spawn. A group retains its
    identity while members survive its leader; a replacement leader with a
    different token invalidates ownership. Unconfirmed termination returns False.
    Cancellation propagates so the caller can own and shield its cleanup task.
    """
    import signal  # noqa: PLC0415

    from zicato.runtime.lock import signal_owned_process  # noqa: PLC0415

    for sig in (signal.SIGTERM, signal.SIGKILL):
        if processes_gone(proc, expected_start_time=expected_start_time, pgid=pgid):
            return True
        if not signal_owned_process(
            proc.pid,
            expected_start_time,
            pgid,
            sig,
            leader_exited=proc.returncode is not None,
        ):
            return False
        deadline = asyncio.get_running_loop().time() + signal_grace_s
        while asyncio.get_running_loop().time() < deadline:
            try:
                await asyncio.wait_for(proc.wait(), timeout=0.05)
            except TimeoutError:
                pass
            if processes_gone(proc, expected_start_time=expected_start_time, pgid=pgid):
                return True
            await asyncio.sleep(0.01)
    return processes_gone(proc, expected_start_time=expected_start_time, pgid=pgid)


async def run_process(
    command: Sequence[str],
    *,
    timeout_s: float,
    cwd: Path | None = None,
    merge_stderr: bool = False,
) -> CompletedProcess[bytes]:
    """Capture output and join the isolated group on every exit.

    Startup is shielded so cancellation cannot discard a spawned child's
    handle. Cleanup reuses the worker's verified termination policy and stays
    on the original event loop until the leader is reaped and its group has
    no live members. Repeated cancellation preserves the first failure.
    """

    async def spawn() -> tuple[asyncio.subprocess.Process, float | None]:
        proc = await asyncio.create_subprocess_exec(
            *command,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT if merge_stderr else asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        return proc, pid_start_time(proc.pid)

    launch = asyncio.create_task(spawn())
    communication: asyncio.Task[tuple[bytes, bytes]] | None = None

    async def close() -> None:
        nonlocal communication
        try:
            proc, start_time = await launch
        except BaseException:
            # The original await owns a launch failure; no handle was returned.
            return
        if communication is None:
            communication = asyncio.create_task(proc.communicate())
        while proc.returncode is None or group_has_live_members(proc.pid):
            if start_time is not None:
                try:
                    await terminate_process(proc, expected_start_time=start_time, pgid=proc.pid)
                except Exception as exc:  # noqa: BLE001 — retain ownership until confirmed
                    log.warning("process cleanup remains unconfirmed: %s", exc)
            if proc.returncode is None or group_has_live_members(proc.pid):
                await asyncio.sleep(0.01)
        await proc.wait()
        await asyncio.gather(communication, return_exceptions=True)

    primary_failure: BaseException | None = None
    try:
        async with asyncio.timeout(timeout_s):
            proc, _start_time = await asyncio.shield(launch)
            communication = asyncio.create_task(proc.communicate())
            stdout, stderr = await asyncio.shield(communication)
            assert proc.returncode is not None
            return CompletedProcess(command, proc.returncode, stdout, stderr)
    except BaseException as exc:
        primary_failure = exc
        raise
    finally:
        cleanup = asyncio.create_task(close())
        _, cancelled = await finish_task(cleanup)
        if primary_failure is None and cancelled is not None:
            raise cancelled
