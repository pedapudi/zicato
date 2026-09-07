"""Join child tasks before their owner propagates cancellation or failure."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from typing import Literal, TypeVar, overload

_Result = TypeVar("_Result")


@overload
async def gather_owned(
    *awaitables: Awaitable[_Result], return_exceptions: Literal[False] = False
) -> list[_Result]: ...


@overload
async def gather_owned(
    *awaitables: Awaitable[_Result], return_exceptions: Literal[True]
) -> list[_Result | BaseException]: ...


async def gather_owned(
    *awaitables: Awaitable[_Result], return_exceptions: bool = False
) -> list[_Result] | list[_Result | BaseException]:
    """Gather in submission order and join every child before propagating failure.

    Cancellation requests reach each unfinished child once. Repeated requests
    to the parent cannot interrupt child cleanup. Each child remains responsible
    for bounding its cleanup and retaining resources it cannot release safely.
    """
    children = [asyncio.ensure_future(awaitable) for awaitable in awaitables]
    batch = asyncio.gather(*children, return_exceptions=return_exceptions)
    try:
        return await asyncio.shield(batch)
    except BaseException:
        for child in children:
            if not child.done():
                child.cancel()
        settled = asyncio.gather(*children, return_exceptions=True)
        while True:
            try:
                await asyncio.shield(settled)
                break
            except asyncio.CancelledError:
                if settled.cancelled():
                    raise
        # The original gather can still have pending completion callbacks.
        # Consume its result after joining every child, preserving the failure
        # already escaping this owner.
        try:
            await finish_task(batch)
        except BaseException:
            pass
        raise


async def finish_task(
    task: asyncio.Future[_Result],
) -> tuple[_Result, asyncio.CancelledError | None]:
    """Await owned work through repeated cancellation, retaining the first request."""
    cancelled = None
    while True:
        try:
            return await asyncio.shield(task), cancelled
        except asyncio.CancelledError as exc:
            if task.done():
                return task.result(), cancelled or exc
            if cancelled is None:
                cancelled = exc
