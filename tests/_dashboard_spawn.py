"""A non-spawning stand-in for the dashboard child process.

``zicato evolve`` launches ``python -m zicato.dashboard`` as a child and
deliberately leaves it serving when the run ends. A test that lets the
real spawn happen therefore orphans a dashboard that squats on the
dashboard port until the session manager reaps it, so every CLI test that
runs ``evolve`` to a normal conclusion replaces the spawn with the fake
installed here.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest


class FakeDashboardProc:
    """Minimal stand-in for an ``asyncio.subprocess.Process``.

    Records terminate/kill so a test can assert the teardown path while
    never starting a real OS process.
    """

    def __init__(self, argv: tuple[str, ...]) -> None:
        self.argv = argv
        self.returncode: int | None = None
        self.terminated = False
        self.killed = False

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = 0

    def kill(self) -> None:  # pragma: no cover - escalation path
        self.killed = True
        self.returncode = -9

    async def wait(self) -> int:
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


def install_spawn_mock(monkeypatch: pytest.MonkeyPatch) -> list[FakeDashboardProc]:
    """Replace ``asyncio.create_subprocess_exec`` with a non-spawning fake.

    Returns the list the fake appends each child to, in spawn order, so a
    caller can assert on what would have been launched.

    A dashboard spawn also gets a fake endpoint file written for it. The
    real server writes that file once it has bound a port, and the CLI
    reads it back to report the URL; without it the CLI would poll for its
    whole fallback timeout before giving up, adding that wait to every test
    that spawns one.
    """
    spawned: list[FakeDashboardProc] = []

    async def _fake_exec(*args: Any, **kwargs: Any) -> FakeDashboardProc:
        del kwargs
        argv = tuple(str(a) for a in args)
        proc = FakeDashboardProc(argv)
        spawned.append(proc)
        if "zicato.dashboard" in argv and "--workspace" in argv:
            ws = Path(argv[argv.index("--workspace") + 1])
            host = "127.0.0.1"
            if "--host" in argv:
                host = argv[argv.index("--host") + 1]
            port = 7892
            if "--port" in argv:
                port = int(argv[argv.index("--port") + 1])
            from zicato.runtime.paths import dashboard_endpoint_path

            endpoint = dashboard_endpoint_path(ws)
            endpoint.parent.mkdir(parents=True, exist_ok=True)
            endpoint.write_text(
                json.dumps({"host": host, "port": port}),
                encoding="utf-8",
            )
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
    return spawned
