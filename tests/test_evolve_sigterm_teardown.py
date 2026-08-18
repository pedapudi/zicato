"""Real-subprocess test for ``zicato evolve``'s SIGTERM teardown (task #12).

Post-#72 the evolve children (dashboard + watchdog supervisor) live in
their OWN sessions, so an unhandled SIGTERM of evolve used to orphan both:
the default disposition kills the process without unwinding the
``_terminate_child`` teardown. The evolve command now installs a
``loop.add_signal_handler(SIGTERM, ...)`` that converts the signal into a
cooperative cancellation of the main task — the SAME teardown path the
error/interrupt exit uses, through ``evolve_n_rounds``'s ``finally``
(workspace lock released, heartbeat stopped) — and exits 143 (128+15).

The test drives a REAL evolve process (the actual Click command +
``evolve_n_rounds``; only ``evolve_once`` and the two child spawners are
stubbed inside the driver — the children are real subprocesses in their
own sessions, exactly the topology under test) and SIGTERMs it mid-round.

Normal completion deliberately leaves the dashboard serving; that
behavior is pinned by
``tests/test_cli_dashboard.py::test_evolve_keeps_dashboard_serving_at_normal_conclusion``
and is untouched (the handler fires on signal-interrupted exits only).
"""

from __future__ import annotations

import errno
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.test_orchestrator import _bootstrap_workspace
from zicato.runtime.resume import prepare_resume

_DRIVER = """
import asyncio
import json
import sys
from pathlib import Path

workspace = Path(sys.argv[1])
epoch_id = sys.argv[2]
pid_file = Path(sys.argv[3])

import zicato.cli.commands.evolve as ev
import zicato.check as check
import zicato.orchestrator as orch

spawned: list[int] = []


async def _sleeper_child(*_a, **_k):
    # A REAL child subprocess in its OWN session -- the post-#72 topology
    # under test -- standing in for the dashboard / supervisor services.
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-c", "import time; time.sleep(300)",
        start_new_session=True,
    )
    spawned.append(proc.pid)
    if len(spawned) == 2:
        pid_file.write_text(json.dumps(spawned))
    return proc


ev._maybe_spawn_supervisor = _sleeper_child
ev._maybe_spawn_dashboard = _sleeper_child


async def _no_report(*_a, **_k):
    return None


ev._report_dashboard_url = _no_report
ev._announce_dashboard_still_serving = lambda *a, **k: None
# The pre-spend wiring gate: this fixture workspace is minimal and the
# test is about SIGTERM teardown, not wiring. Patched in the CHILD,
# since the child is a real process the test cannot monkeypatch into.
check.require_workspace_valid = lambda *a, **k: None


async def _hang_mid_round(**_kwargs):
    # "Mid-round": the loop holds the workspace lock and the round never
    # settles until the SIGTERM arrives.
    await asyncio.sleep(600)


import zicato.evolve.gauntlet as gauntlet
gauntlet.evolve_once = _hang_mid_round

ev.evolve_cmd.main(
    args=[
        "--workspace", str(workspace),
        "--epoch", epoch_id,
        "--rounds", "1",
        "--harness-call-llm", "llm_stubs:harness_call_llm",
        "--auxiliary-call-llm", "llm_stubs:aux_call_llm",
    ],
    standalone_mode=True,
)
"""

_LLM_STUBS = """
async def harness_call_llm(system, user, model):
    return ""


async def aux_call_llm(system, user, model):
    return ""
"""


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            return False
        return True
    return True


def _wait_until(predicate, *, timeout_s: float, what: str) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError(f"timed out after {timeout_s}s waiting for {what}")


@pytest.mark.slow
def test_sigterm_mid_round_reaps_children_and_releases_lock(tmp_path: Path) -> None:
    workspace, epoch_id = _bootstrap_workspace(tmp_path)
    driver = tmp_path / "driver.py"
    driver.write_text(_DRIVER)
    (tmp_path / "llm_stubs.py").write_text(_LLM_STUBS)
    pid_file = tmp_path / "children.json"
    lock_file = workspace / "runtime" / "lock.json"

    env = dict(os.environ)
    env["PYTHONPATH"] = str(tmp_path) + os.pathsep + env.get("PYTHONPATH", "")

    proc = subprocess.Popen(
        [sys.executable, str(driver), str(workspace), epoch_id, str(pid_file)],
        env=env,
        cwd=tmp_path,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        # The driver in its own session so the SIGTERM below reaches ONLY
        # it -- proving the children are reaped by evolve's teardown, not
        # by any group/session side effect.
        start_new_session=True,
    )
    try:
        # Both children spawned AND the evolve loop holds the lock
        # (mid-round) before the signal fires.
        _wait_until(pid_file.exists, timeout_s=30, what="both children to spawn")
        _wait_until(lock_file.exists, timeout_s=30, what="the workspace lock")
        children = json.loads(pid_file.read_text())
        assert len(children) == 2
        assert all(_pid_alive(pid) for pid in children)

        os.kill(proc.pid, signal.SIGTERM)
        returncode = proc.wait(timeout=30)

        # The conventional fatal-signal exit status -- an orderly unwind,
        # not the signal's default kill (which would report -SIGTERM here).
        assert returncode == 128 + signal.SIGTERM

        # Both own-session children were reaped through _terminate_child.
        for pid in children:
            _wait_until(
                lambda p=pid: not _pid_alive(p),
                timeout_s=10,
                what=f"child {pid} to be reaped",
            )

        # The evolve loop's finally released the workspace lock.
        assert not lock_file.exists(), "workspace lock must be released on SIGTERM"

        # The workspace is resume-clean: nothing was half-written (the
        # rigged round never proposed), so the conservative reconciliation
        # classifies it clean for the next evolve.
        plan = prepare_resume(workspace, epoch_id)
        assert plan.classification == "clean"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)
        # Belt and braces: never leak the sleeper children even if the
        # assertions above failed before the teardown reaped them.
        if pid_file.exists():
            for pid in json.loads(pid_file.read_text()):
                if _pid_alive(pid):
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except OSError:
                        pass
