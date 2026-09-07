"""Cleanup signals only the process identity and scope captured at spawn."""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from pathlib import Path

import pytest

from zicato.runtime import lock
from zicato.runtime.process import processes_gone, terminate_process
from zicato.tournament import runner


class PendingProcess:
    """An asynchronous handle whose exit notification has not arrived."""

    pid = 99_999_999
    returncode: int | None = None

    async def wait(self) -> int:
        self.returncode = 0
        return 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "token,group,expected",
    [
        (None, True, None),
        (None, False, None),
        (123.0, False, "leader"),
        (123.0, True, "group"),
        (122.0, True, None),
    ],
    ids=[
        "missing-token-group",
        "missing-token-leader",
        "captured-leader",
        "captured-group",
        "recycled-pid",
    ],
)
async def test_cleanup_never_invents_identity_or_expands_signal_scope(
    monkeypatch: pytest.MonkeyPatch,
    token: float | None,
    group: bool,
    expected: str | None,
) -> None:
    proc = PendingProcess()
    signals: list[tuple[str, int, int]] = []
    monkeypatch.setattr(lock, "pid_start_time", lambda _pid: 123.0)
    monkeypatch.setattr(lock, "group_has_live_members", lambda _pgid: False)
    monkeypatch.setattr(os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(os, "kill", lambda pid, sig: signals.append(("leader", pid, sig)))
    monkeypatch.setattr(os, "killpg", lambda pid, sig: signals.append(("group", pid, sig)))
    stopped = await terminate_process(
        proc,
        expected_start_time=token,
        pgid=proc.pid if group else None,
        signal_grace_s=0.01,
    )
    assert stopped is (expected is not None)
    assert signals == ([] if expected is None else [(expected, proc.pid, signal.SIGTERM)])


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["token", "group"])
async def test_worker_keeps_resources_when_spawn_identity_is_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    missing: str,
) -> None:
    resources = runner._WorkerResources(tmp_path, "unit", tmp_path / "args", tmp_path / "result")
    resources.proc = PendingProcess()
    resources.pgid = resources.proc.pid if missing == "token" else None
    resources.start_time = None if missing == "token" else 123.0
    resources.args_path.write_text("arguments")
    resources.result_path.write_text("result")
    monkeypatch.setattr(runner, "_runtime_state", lambda: None)

    async def unexpected_cleanup(*_args, **_kwargs):
        pytest.fail("missing spawn identity must not reach signal fallback")

    monkeypatch.setattr(runner, "_terminate_worker", unexpected_cleanup)
    key = (tmp_path.resolve(), resources.proc.pid, resources.start_time)
    monkeypatch.setattr(runner, "_retained_worker_resources", {key: resources})
    assert await runner.retry_worker_cleanup(tmp_path) == 0
    assert runner._retained_worker_resources == {key: resources}
    assert not resources.released
    assert resources.args_path.read_text() == "arguments"
    assert resources.result_path.read_text() == "result"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "captured_group", [True, False], ids=["captured-empty-group", "missing-group"]
)
@pytest.mark.parametrize("token", [None, 122.0], ids=["missing-token", "recycled-pid"])
async def test_reaped_worker_completion_requires_its_captured_group(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    captured_group: bool,
    token: float | None,
) -> None:
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-c", "pass", start_new_session=True
    )
    await proc.wait()
    resources = runner._WorkerResources(tmp_path, "unit", tmp_path / "args", tmp_path / "result")
    resources.proc = proc
    resources.pgid = proc.pid if captured_group else None
    resources.start_time = token
    monkeypatch.setattr(lock, "pid_start_time", lambda _pid: 123.0)
    assert resources.processes_gone() is captured_group
    if captured_group:
        assert processes_gone(proc, expected_start_time=token, pgid=proc.pid)
