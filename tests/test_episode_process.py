"""Process-boundary proofs for proposal cleanup and verified group signalling."""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from zicato.runtime import lock
from zicato.tournament import worker_transport


@pytest.mark.asyncio
@pytest.mark.parametrize("unreadable_after_term", [False, True])
async def test_live_worker_with_unreadable_identity_retains_ownership(
    monkeypatch: pytest.MonkeyPatch, unreadable_after_term: bool
) -> None:
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); "
        "print('ready',flush=True); time.sleep(60)",
        stdout=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    assert proc.stdout is not None
    assert await proc.stdout.readline() == b"ready\n"
    token = lock.pid_start_time(proc.pid)
    assert token is not None
    real_start = lock.pid_start_time
    real_killpg = os.killpg
    signals: list[int] = []

    def observe(pgid: int, sig: int) -> None:
        signals.append(sig)
        real_killpg(pgid, sig)

    def start_time(pid: int) -> float | None:
        if pid == proc.pid and (not unreadable_after_term or signals):
            return None
        return real_start(pid)

    monkeypatch.setattr(lock, "pid_start_time", start_time)
    monkeypatch.setattr(os, "killpg", observe)
    monkeypatch.setattr(worker_transport, "_SIGTERM_TO_SIGKILL_GRACE_S", 0.02)
    try:
        stopped = await worker_transport._terminate_worker(
            proc, expected_start_time=token, pgid=proc.pid
        )
        assert not stopped, "unreadable live identity must retain cleanup ownership"
        assert signals == ([signal.SIGTERM] if unreadable_after_term else [])
        assert proc.returncode is None
    finally:
        if proc.returncode is None:
            assert real_start(proc.pid) == token
            real_killpg(proc.pid, signal.SIGKILL)
        await proc.wait()


@pytest.mark.parametrize(
    "boundary",
    [
        "spawn-cancel",
        "handshake-cancel",
        "wait-cancel",
        "handshake-timeout",
        "wait-timeout",
        "leader-exit",
    ],
)
def test_proposal_keeps_inputs_and_writer_until_descendants_are_reaped(
    tmp_path: Path, boundary: str
) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "tests._proposal_cancellation_probe", str(tmp_path), boundary],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
