"""Validate public evolve calls while holding exclusive mutation ownership."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

from zicato.runtime.lock import WorkspaceLock, acquire_workspace_lock, release_workspace_lock

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class InvocationContext:
    """Mutation ownership retained from validation through invocation teardown."""

    writer: WorkspaceLock
    resources: AsyncExitStack = field(default_factory=AsyncExitStack)

    async def _close(self) -> BaseException | None:
        """Finish worker ownership before releasing services and the writer."""
        from zicato.tournament.runner import drain_worker_cleanup  # noqa: PLC0415

        failure: BaseException | None = None
        try:
            await drain_worker_cleanup(self.writer.workspace_root)
        except asyncio.CancelledError as exc:
            # The drain propagates cancellation only after every worker exits.
            failure = exc
        try:
            await self.resources.aclose()
        except BaseException as exc:
            failure = failure or exc
        return failure


@asynccontextmanager
async def validated_invocation(
    workspace_root: Path, epoch_id: str | None, instance_id: str
) -> AsyncIterator[InvocationContext]:
    """Refuse competing invocations before validation or any execution writes."""
    from zicato.check import require_workspace_valid  # noqa: PLC0415

    writer = acquire_workspace_lock(workspace_root, instance_id)
    invocation = InvocationContext(writer)
    invocation.resources.callback(release_workspace_lock, writer)
    primary_failure: BaseException | None = None
    try:
        require_workspace_valid(
            writer.workspace_root, epoch_id=epoch_id, live_contract=epoch_id is None
        )
        yield invocation
    except BaseException as exc:
        primary_failure = exc
        raise
    finally:
        cleanup = asyncio.create_task(invocation._close())
        cancelled: asyncio.CancelledError | None = None
        while True:
            try:
                cleanup_failure = await asyncio.shield(cleanup)
                break
            except asyncio.CancelledError as exc:
                if cleanup.cancelled():
                    raise
                cancelled = cancelled or exc
        if primary_failure is None:
            if cancelled is not None:
                raise cancelled
            if cleanup_failure is not None:
                raise cleanup_failure
        elif cleanup_failure is not None:
            log.warning("invocation cleanup failed: %s", cleanup_failure)
