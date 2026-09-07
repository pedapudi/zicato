"""A selection batch owns its subprocess tasks through cancellation and failure."""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Any

import pytest

from zicato.core.types import TournamentStructure
from zicato.runtime.lock import pid_start_time
from zicato.selection import Contestant, make_strategy
from zicato.selection.driver import evaluate_tournament

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_nested_batches_preserve_cancellation_during_loop_shutdown() -> None:
    from zicato.util.async_tasks import gather_owned

    tasks: dict[str, asyncio.Task[Any]] = {}
    ready = asyncio.Event()
    started = 0

    async def child(name: str) -> None:
        nonlocal started
        task = asyncio.current_task()
        assert task is not None
        tasks[name] = task
        started += 1
        if started == 4:
            ready.set()
        await asyncio.Future()

    async def nested(name: str) -> None:
        task = asyncio.current_task()
        assert task is not None
        tasks[name] = task
        await gather_owned(child(name + "_parent"), child(name + "_child"))

    tasks["field"] = asyncio.create_task(gather_owned(nested("first"), nested("second")))
    await ready.wait()
    # Loop shutdown can cancel descendants before their enclosing batch.
    for name in (
        "field",
        "first",
        "first_parent",
        "first_child",
        "second_parent",
        "second_child",
        "second",
    ):
        tasks[name].cancel()
    results = await asyncio.gather(*tasks.values(), return_exceptions=True)
    assert all(isinstance(result, asyncio.CancelledError) for result in results), results


@pytest.mark.asyncio
@pytest.mark.parametrize("trigger", ["cancel", "repeated_cancel", "failure"])
async def test_selection_batch_joins_owned_process_cleanup(trigger: str) -> None:
    strategy = make_strategy(
        TournamentStructure(
            structure="racing",
            params={"field_size": 2, "board_ids": ["a", "b", "c", "d"], "rung0_board_size": 1},
        )
    )
    ready = asyncio.Event()
    start_failure = asyncio.Event()
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()
    children: list[asyncio.Task[Any]] = []
    processes: list[tuple[asyncio.subprocess.Process, float | None]] = []

    async def request_field(_size: int) -> Any:
        return Contestant("v0", "champion"), [
            Contestant("v1", "challenger"),
            Contestant("v2", "challenger"),
        ]

    async def run_matchup(matchup: Any) -> Any:
        child = asyncio.current_task()
        assert child is not None
        children.append(child)
        if matchup.right.generation_id == "v1":
            await start_failure.wait()
            raise RuntimeError("matchup failed")
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-c", "import time; time.sleep(60)", start_new_session=True
        )
        identity = pid_start_time(process.pid)
        processes.append((process, identity))
        ready.set()
        try:
            await process.wait()
        finally:
            cleanup_started.set()
            await release_cleanup.wait()
            assert identity is not None and pid_start_time(process.pid) == identity
            process.terminate()
            await process.wait()

    parent = asyncio.create_task(
        evaluate_tournament(strategy, request_field=request_field, run_matchup=run_matchup)
    )
    try:
        await asyncio.wait_for(ready.wait(), 5)
        if trigger == "failure":
            start_failure.set()
        else:
            parent.cancel("operator cancellation")
        # A failing batch must cancel its sibling before it can join cleanup.
        # The timeout also bounds a regression that leaves that sibling running.
        await asyncio.wait_for(cleanup_started.wait(), 0.5)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not parent.done(), "selection returned while an owned subprocess remained alive"
        if trigger == "repeated_cancel":
            parent.cancel("repeated cancellation")
            await asyncio.sleep(0)
            assert not parent.done()
            assert not children[1].done()
        assert processes[0][0].returncode is None
        release_cleanup.set()
        expected = RuntimeError if trigger == "failure" else asyncio.CancelledError
        with pytest.raises(expected):
            await parent
        assert all(child.done() for child in children)
        assert processes[0][0].returncode is not None
        with pytest.raises(ProcessLookupError):
            os.kill(processes[0][0].pid, 0)
    finally:
        release_cleanup.set()
        for child in children:
            if not child.done():
                child.cancel()
        await asyncio.gather(*children, return_exceptions=True)
        for process, identity in processes:
            if process.returncode is None:
                assert identity is not None and pid_start_time(process.pid) == identity
                process.kill()
                await process.wait()
        if not parent.done():
            parent.cancel()
        await asyncio.gather(parent, return_exceptions=True)
