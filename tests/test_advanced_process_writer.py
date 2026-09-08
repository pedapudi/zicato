"""Standalone producers retain the workspace writer through real process cleanup."""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from tests._runtime_builders import (
    make_generation,
    prepare_tournament_epoch,
    record_tournament_score,
)
from tests._subprocess_worker_support import SleepingAdapter
from tests.test_subprocess_workers import _config, _entry
from zicato.core import ScoringWeights
from zicato.runtime.lock import WorkspaceLockHeld, acquire_workspace_lock, read_workspace_lock
from zicato.runtime.state import list_active_runs
from zicato.tournament import runner
from zicato.tournament.scoring import read_gen_score
from zicato.workspace.layout import WorkspaceLayout

pytestmark = pytest.mark.integration


def _assert_excluded(root: Path) -> None:
    with pytest.raises(WorkspaceLockHeld):
        with acquire_workspace_lock(root, "competing-producer"):
            pass


@asynccontextmanager
async def _caller_writer(root: Path, nested: bool):
    if not nested:
        yield None
        return
    from zicato.runtime.writer import workspace_writer

    async with workspace_writer(
        root,
        writer=None,
        instance_id="enclosing-invocation",
        cleanup=lambda: runner.drain_worker_cleanup(root),
    ) as writer:
        yield writer


@pytest.mark.asyncio
async def test_failed_cleanup_keeps_a_reachable_lease_until_retry(
    tmp_path: Path,
) -> None:
    import gc
    import weakref

    from zicato.runtime.writer import workspace_writer

    retry_entered, finish_retry = asyncio.Event(), asyncio.Event()
    attempts = 0
    leases: list[Any] = []

    async def cleanup() -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("termination remains unconfirmed")
        retry_entered.set()
        await finish_retry.wait()

    async def execute() -> None:
        async with workspace_writer(
            tmp_path, writer=None, instance_id="producer", cleanup=cleanup
        ) as writer:
            assert writer._lease is not None
            leases.append(weakref.ref(writer._lease))
            raise ValueError("original failure")

    task = asyncio.create_task(execute())
    try:
        await asyncio.wait_for(retry_entered.wait(), 0.5)
        gc.collect()
        assert leases[0]() is not None
        _assert_excluded(tmp_path)
        task.cancel("cancellation during retry")
        await asyncio.sleep(0)
        assert not task.done()
        finish_retry.set()
        with pytest.raises(ValueError, match="original failure"):
            await task
        assert attempts == 2
        with acquire_workspace_lock(tmp_path, "successor"):
            pass
    finally:
        finish_retry.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("entry_point", ["run_tournament", "run_fast_mode", "run_matchup"])
@pytest.mark.parametrize("nested", [False, True])
async def test_tournament_retains_writer_through_repeated_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry_point: str, nested: bool
) -> None:
    root = tmp_path / "workspace"
    parent, child = make_generation(root), make_generation(root, "v1")
    cleanup_entered, allow_cleanup = asyncio.Event(), asyncio.Event()
    real_terminate = runner._terminate_worker
    observed_pids: list[int] = []
    termination_attempts = 0

    async def pause_cleanup(*args: Any, **kwargs: Any) -> bool:
        nonlocal termination_attempts
        termination_attempts += 1
        if termination_attempts == 1:
            return False
        cleanup_entered.set()
        await allow_cleanup.wait()
        return await real_terminate(*args, **kwargs)

    monkeypatch.setattr(runner, "_terminate_worker", pause_cleanup)
    config = _config(root, supervisor_kill_wait_s=0.01)
    config = replace(config, parallelism=1, host_worker_permits=1)
    board, weights = [_entry()], ScoringWeights()
    epoch_id = prepare_tournament_epoch(root, config, board, weights)
    parent, child = replace(parent, epoch_id=epoch_id), replace(child, epoch_id=epoch_id)
    parent_score = {"scalar": 0.0, "generation_id": parent.id, "base_seed": config.seed}
    if entry_point == "run_fast_mode":
        record_tournament_score(root, epoch_id, parent.id, parent_score)
        stored_parent = read_gen_score(WorkspaceLayout(root), epoch_id, parent.id)
        assert stored_parent is not None
        parent_score = stored_parent.to_dict()

    async def execute() -> None:
        async with _caller_writer(root, nested) as writer:
            kwargs: dict[str, Any] = {
                "adapter": SleepingAdapter(),
                "board": board,
                "weights": weights,
                "config": config,
                "workspace_root": root,
                "epoch_id": epoch_id,
            }
            if nested:
                kwargs["writer"] = writer
            if entry_point == "run_matchup":
                kwargs.update(left_gen=parent, right_gen=child)
            elif entry_point == "run_fast_mode":
                kwargs.update(
                    child_gen=child,
                    parent_generation_id=parent.id,
                    parent_historical_agg=parent_score,
                )
            else:
                kwargs.update(parent_gen=parent, child_gen=child)
            await getattr(runner, entry_point)(**kwargs)

    task = asyncio.create_task(execute())
    try:
        async with asyncio.timeout(8):
            while not (records := list_active_runs(root)):
                if task.done():
                    await task
                await asyncio.sleep(0.01)
        observed_pids.extend(record.pid for record in records)
        _assert_excluded(root)
        owner = read_workspace_lock(root)
        assert owner is not None
        task.cancel("original cancellation")
        await asyncio.wait_for(cleanup_entered.wait(), 3)
        assert any(key[0] == root.resolve() for key in runner._retained_worker_resources)
        task.cancel("repeated cancellation")
        await asyncio.sleep(0)
        assert not task.done()
        assert read_workspace_lock(root) == owner
        _assert_excluded(root)
        allow_cleanup.set()
        with pytest.raises(asyncio.CancelledError, match="original cancellation"):
            await task
        assert list_active_runs(root) == []
        assert not any(key[0] == root.resolve() for key in runner._retained_worker_resources)
        for pid in observed_pids:
            with pytest.raises(ProcessLookupError):
                os.kill(pid, 0)
        with acquire_workspace_lock(root, "successor"):
            pass
    finally:
        allow_cleanup.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await runner.drain_worker_cleanup(root)


@pytest.mark.asyncio
@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("trigger", ["cancel", "validation_error"])
async def test_proposal_retains_writer_through_validation_and_cancelled_drain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, nested: bool, trigger: str
) -> None:
    from tests._foe_support import call_turn, return_turn
    from tests.test_proposer_foe_agent import _EDITED_FILE, _HYPOTHESIS, Workspace, _edit
    from zicato.proposer import foe_agent

    workspace = Workspace(tmp_path, [call_turn(_edit(_EDITED_FILE)), return_turn(_HYPOTHESIS)])
    validation, drain_entered, allow_drain = asyncio.Event(), asyncio.Event(), asyncio.Event()
    pids: list[int] = []
    real_register = foe_agent._register_active_run
    real_drain = runner.drain_worker_cleanup

    def observe_registration(*args: Any, **kwargs: Any) -> Any:
        _assert_excluded(workspace.root)
        owner = real_register(*args, **kwargs)
        assert owner is not None
        pids.append(owner.pid)
        return owner

    async def validate(_experiment: Any) -> list[str]:
        _assert_excluded(workspace.root)
        validation.set()
        if trigger == "validation_error":
            raise ValueError("validation failed")
        await asyncio.Event().wait()
        return []

    async def drain(root: Path) -> None:
        drain_entered.set()
        await allow_drain.wait()
        await real_drain(root)

    monkeypatch.setattr(foe_agent, "_register_active_run", observe_registration)
    monkeypatch.setattr(runner, "drain_worker_cleanup", drain)

    async def execute() -> None:
        async with _caller_writer(workspace.root, nested) as writer:
            fields = {"validate_experiment": validate}
            if nested:
                fields["writer"] = writer
            await workspace.agent().propose(workspace.context(**fields))

    task = asyncio.create_task(execute())
    try:
        async with asyncio.timeout(8):
            while not validation.is_set():
                if task.done():
                    await task
                await asyncio.sleep(0.01)
        assert len(pids) == 1
        if trigger == "cancel":
            task.cancel("original cancellation")
        await asyncio.wait_for(drain_entered.wait(), 2)
        task.cancel("repeated cancellation")
        await asyncio.sleep(0)
        assert not task.done()
        _assert_excluded(workspace.root)
        allow_drain.set()
        expected = asyncio.CancelledError if trigger == "cancel" else ValueError
        message = "original cancellation" if trigger == "cancel" else "validation failed"
        with pytest.raises(expected, match=message):
            await task
        assert list_active_runs(workspace.root) == []
        with pytest.raises(ProcessLookupError):
            os.kill(pids[0], 0)
        with acquire_workspace_lock(workspace.root, "successor"):
            pass
    finally:
        allow_drain.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def test_proposal_command_holds_writer_until_experiment_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.test_cli_propose import _run, _workspace
    from zicato.cli.commands import propose

    root, _epoch_id = _workspace(tmp_path)
    real_write = propose._write_proposal
    published: list[Path] = []

    def publish(workspace: Path, *args: Any, **kwargs: Any) -> Path:
        _assert_excluded(workspace)
        result = real_write(workspace, *args, **kwargs)
        published.append(result)
        return result

    monkeypatch.setattr(propose, "_write_proposal", publish)
    result = _run(root)
    assert result.exit_code == 0, result.output
    assert len(published) == 1 and published[0].is_file()
    with acquire_workspace_lock(root, "successor"):
        pass
