"""Tests for the leaked-dashboard safety net in ``tests/conftest.py``.

The reaper exists to kill real ``python -m zicato.dashboard`` children a
test leaks. Its failure mode — the one observed live — is killing a
dashboard it does NOT own: a concurrently-running ``zicato evolve`` on the
same host spawns exactly such a child, and the old before/after pid
snapshot classified it as "leaked" and group-killed it, taking the whole
innocent evolve invocation down (evolve appeared to hang before its first
round whenever the suite ran alongside it).

These tests pin the two safety properties that prevent a recurrence:

1. SELECTION is workspace-scoped — only dashboards whose ``--workspace``
   argv points inside THIS session's pytest temp root are ever selected.
2. The KILL path never signals the test runner's own process group.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from tests import conftest as suite_conftest


def _ps_line(pid: int, args: str) -> str:
    return f"{pid:>7} {args}"


def _fake_ps(monkeypatch: pytest.MonkeyPatch, lines: list[str]) -> None:
    """Point the reaper's ``ps -eo pid,args`` read at a canned table."""
    payload = "    PID ARGS\n" + "\n".join(lines) + "\n"

    def _run(*_args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=["ps"], returncode=0, stdout=payload)

    monkeypatch.setattr(subprocess, "run", _run)


def test_dashboard_workspace_arg_parsing() -> None:
    """``--workspace <path>`` is extracted; absent/trailing forms yield None."""
    parse = suite_conftest._dashboard_workspace_arg
    assert parse("python -m zicato.dashboard --workspace /a/b --port 1") == "/a/b"
    assert parse("python -m zicato.dashboard --port 1") is None
    assert parse("python -m zicato.dashboard --workspace") is None


def test_selection_is_scoped_to_the_session_tmp_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Only marker children whose workspace lives under the session tmp root
    are selected; an operator's / a concurrent evolve's dashboard is not.

    This is the regression pin for the observed incident: the dashboard of
    a live ``zicato evolve`` (workspace outside pytest's basetemp) must be
    invisible to the sweep.
    """
    ours = tmp_path / "fixture-ws" / ".zicato"
    ours.mkdir(parents=True)
    foreign = "/home/operator/project/.zicato"
    _fake_ps(
        monkeypatch,
        [
            # Ours: marker + workspace under tmp_path -> selected.
            _ps_line(101, f"python -m zicato.dashboard --workspace {ours} --port 7892"),
            # A concurrent evolve's child: marker but FOREIGN workspace.
            _ps_line(102, f"python -m zicato.dashboard --workspace {foreign} --port 7892"),
            # An operator's standalone `zicato dashboard`: no `-m` marker.
            _ps_line(103, f"/venv/bin/python /venv/bin/zicato dashboard --workspace {ours}"),
            # Marker but no --workspace argv: not something this suite spawns.
            _ps_line(104, "python -m zicato.dashboard --port 7892"),
            # Unrelated process.
            _ps_line(105, "python -m http.server 8000"),
        ],
    )
    assert suite_conftest._session_dashboard_pids(tmp_path.resolve()) == [101]


def test_reaper_never_signals_a_foreign_dashboard(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The sweep sends NO signal at all when the only live dashboards serve
    workspaces outside the session tmp root."""
    foreign = "/srv/live/.zicato"
    _fake_ps(
        monkeypatch,
        [_ps_line(4242, f"python -m zicato.dashboard --workspace {foreign} --port 7892")],
    )
    signalled: list[tuple[str, int]] = []
    monkeypatch.setattr(os, "kill", lambda pid, _sig: signalled.append(("kill", pid)))
    monkeypatch.setattr(os, "killpg", lambda pgid, _sig: signalled.append(("killpg", pgid)))
    assert suite_conftest._reap_session_dashboards(tmp_path.resolve()) == []
    assert signalled == []


def test_reaper_kills_by_pid_when_child_shares_our_group(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A session-owned leak that still shares the test runner's process
    group is signalled by BARE PID — ``killpg`` against our own group would
    take the pytest session down with it.

    A real sleeper child is spawned WITHOUT a new session so it genuinely
    shares this process's group; the signal calls are spied (not delivered)
    so the assertion is on the decision, then the child is cleaned up.
    """
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        ws = tmp_path / ".zicato"
        ws.mkdir()
        _fake_ps(
            monkeypatch,
            [_ps_line(child.pid, f"python -m zicato.dashboard --workspace {ws}")],
        )
        signalled: list[tuple[str, int]] = []
        monkeypatch.setattr(os, "kill", lambda pid, _sig: signalled.append(("kill", pid)))
        monkeypatch.setattr(os, "killpg", lambda pgid, _sig: signalled.append(("killpg", pgid)))
        reaped = suite_conftest._reap_session_dashboards(tmp_path.resolve())
        assert reaped == [child.pid]
        assert signalled, "the sweep must signal a session-owned leak"
        assert all(kind == "kill" for kind, _ in signalled), (
            "a child sharing our process group must be signalled by bare pid, "
            f"never killpg: {signalled}"
        )
        assert all(target == child.pid for _, target in signalled)
    finally:
        # Drop the os.kill spy BEFORE cleanup so Popen.kill() really signals.
        monkeypatch.undo()
        child.kill()
        child.wait(timeout=10)
