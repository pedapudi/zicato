"""Retain a proposal host and its process group until shutdown is confirmed."""

from __future__ import annotations

import asyncio
import logging
import signal
from dataclasses import dataclass

import foe

from zicato.runtime.lock import (
    group_has_live_members,
    is_pid_alive,
    pid_start_time,
    signal_owned_process,
)

log = logging.getLogger(__name__)

_CANCEL_GRACE_S = 30.0
_SIGNAL_GRACE_S = 1.0


@dataclass
class EpisodeProcess:
    """Own the host wait task and captured identity before the startup handshake."""

    handle: foe.Handle | None = None
    start_time: float | None = None
    wait_task: asyncio.Task[foe.Outcome] | None = None

    def attach(self, handle: foe.Handle) -> None:
        self.handle = handle
        self.start_time = pid_start_time(handle.pid)
        self.wait_task = asyncio.create_task(handle.wait())

    async def wait(self, launch: asyncio.Task[foe.Handle]) -> foe.Outcome:
        await asyncio.shield(launch)
        assert self.wait_task is not None
        return await asyncio.shield(self.wait_task)

    def _group_gone(self) -> bool:
        assert self.handle is not None
        # Launch creates an isolated group whose id is the captured handle pid.
        # Its absence needs no signal identity; close still joins host reaping.
        return not group_has_live_members(self.handle.pid)

    async def close(self, launch: asyncio.Task[foe.Handle]) -> None:
        """Join startup, host shutdown, and group death before releasing inputs.

        Failed identity reads or signal delivery retain the owner and retry on
        the invoking event loop. A bounded signal attempt is not completion.
        """
        while self.handle is None and not launch.done():
            await asyncio.sleep(0.01)
        if self.handle is None:
            await launch
            return

        cancel: asyncio.Task[foe.Outcome] | None = None
        if not self._group_gone():
            cancel = asyncio.create_task(self.handle.cancel())
            try:
                await asyncio.wait_for(asyncio.shield(cancel), timeout=_CANCEL_GRACE_S)
            except (Exception, asyncio.CancelledError) as exc:
                log.debug("proposal cancellation did not settle the host: %s", exc)

        while not self._group_gone():
            for sig in (signal.SIGTERM, signal.SIGKILL):
                while not self._group_gone() and not signal_owned_process(
                    self.handle.pid,
                    self.start_time,
                    self.handle.pid,
                    sig,
                    leader_exited=not is_pid_alive(self.handle.pid),
                ):
                    await asyncio.sleep(0.01)
                deadline = asyncio.get_running_loop().time() + _SIGNAL_GRACE_S
                while not self._group_gone() and asyncio.get_running_loop().time() < deadline:
                    await asyncio.sleep(0.01)
                if self._group_gone():
                    break

        # The host owns direct-child reaping and its reader tasks. Group death
        # alone does not establish that those obligations have finished.
        assert self.wait_task is not None
        results = await asyncio.gather(
            launch, self.wait_task, *(() if cancel is None else (cancel,)), return_exceptions=True
        )
        for result in results:
            if isinstance(result, BaseException):
                raise result


__all__ = ["EpisodeProcess"]
