"""Retain workspace mutation authority until asynchronous producers finish."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path

from zicato.runtime.lock import (
    WorkspaceLock,
    acquire_workspace_lock,
    release_workspace_lock,
    validate_workspace_lock,
)
from zicato.util.async_tasks import finish_task

log = logging.getLogger(__name__)


@asynccontextmanager
async def workspace_writer(
    workspace_root: Path,
    *,
    writer: WorkspaceLock | None,
    instance_id: str,
    cleanup: Callable[[], Awaitable[None]],
) -> AsyncIterator[WorkspaceLock]:
    """Borrow a validated writer or own one through producer cleanup.

    A borrowed writer remains its caller's responsibility. A standalone
    owner waits for cleanup on the producer's event loop before release.
    Further cancellation cannot interrupt that wait or replace an error
    already escaping the producer. Cleanup failures retain the lease and retry
    on the owning loop until cleanup succeeds.
    """
    owned = writer is None
    if writer is None:
        writer = acquire_workspace_lock(workspace_root, instance_id)
    else:
        validate_workspace_lock(writer, workspace_root)

    async def close() -> None:
        while True:
            try:
                await cleanup()
            except (Exception, asyncio.CancelledError) as exc:
                log.warning("workspace cleanup remains unconfirmed: %s", exc)
                await asyncio.sleep(0.01)
            else:
                release_workspace_lock(writer)
                return

    primary_failure: BaseException | None = None
    try:
        yield writer
    except BaseException as exc:
        primary_failure = exc
        raise
    finally:
        if owned:
            task = asyncio.create_task(close())
            _, cancelled = await finish_task(task)
            if primary_failure is None and cancelled is not None:
                raise cancelled
