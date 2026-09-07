"""Proposal completion joins the host after the captured isolated group exits."""

from __future__ import annotations

import asyncio
from pathlib import Path

import foe
import pytest

from tests._foe_support import fake_foe_binary, return_turn, scripted_model
from zicato.proposer import episode_process


@pytest.mark.asyncio
async def test_missing_start_token_does_not_prevent_reaped_proposal_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    process = episode_process.EpisodeProcess()
    model = scripted_model(tmp_path, [return_turn({})])
    model.update(model.pop("options"))
    join_allowed = asyncio.Event()
    monkeypatch.setattr(episode_process, "pid_start_time", lambda _pid: None)

    def unexpected_signal(*_args, **_kwargs):
        pytest.fail("a missing start token cannot authorize a cleanup signal")

    monkeypatch.setattr(episode_process, "signal_owned_process", unexpected_signal)

    def spawned(handle: foe.Handle) -> None:
        wait = handle.wait

        async def delayed_join():
            result = await wait()
            await join_allowed.wait()
            return result

        monkeypatch.setattr(handle, "wait", delayed_join)
        process.attach(handle)

    launch = asyncio.create_task(
        foe.start_config(
            {
                "task": "Return an empty result.",
                "instructions": {},
                "tools": [],
                "budget": {"model_calls": 1},
                "done_when": {"returns": {"type": "object"}},
                "model": model,
            },
            binary=fake_foe_binary(tmp_path / "bin"),
            log_dir=tmp_path / "episode",
            start_new_session=True,
            on_spawn=spawned,
        )
    )
    close = None
    try:
        async with asyncio.timeout(2):
            handle = await launch
            while not handle.done:
                await asyncio.sleep(0.001)
            assert isinstance(handle.outcome, foe.Completed)
            assert process.start_time is None
            assert not episode_process.group_has_live_members(handle.pid)
            close = asyncio.create_task(process.close(launch))
            await asyncio.sleep(0)
            assert not close.done(), "group absence must still join the host wait task"
            join_allowed.set()
            await close
            assert process.wait_task is not None and process.wait_task.done()
    finally:
        join_allowed.set()
        if close is not None and not close.done():
            close.cancel()
        tasks = [task for task in (launch, process.wait_task, close) if task is not None]
        await asyncio.gather(*tasks, return_exceptions=True)
