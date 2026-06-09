"""Test-suite configuration root.

Pins the repository root on ``sys.path`` explicitly rather than
relying on pytest's implicit ``rootdir`` insertion. ``tests/`` is an
importable package (``tests._subprocess_worker_support`` is loaded by
directly-spawned worker subprocesses), and once the ``zicato`` package
moved under a ``src/`` root the implicit path handling is no longer
something to lean on — making the repo root explicit here keeps
``import tests.*`` resolvable from both the in-process test session
and any worker subprocess that inherits the environment.

Also provides shared scaffolding for the CLI tests that invoke
``zicato evolve``:

* :data:`FakeDashboardProc` / the ``mock_dashboard_spawn`` fixture stub
  out ``asyncio.create_subprocess_exec`` so an ``evolve`` invocation
  never launches a *real* ``python -m zicato.dashboard`` child. evolve
  deliberately LEAVES the dashboard serving at a normal conclusion, so a
  test that lets the real spawn happen orphans the dashboard subprocess
  (it squats on the dashboard port and is reaped by ``systemd --user``,
  never the test). Mocking the spawn keeps those tests hermetic.
* :func:`_reap_leaked_dashboards` is an autouse safety net: even if some
  test spawns a real dashboard child, it is group-killed at teardown so
  nothing leaks across the session.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

# tests/conftest.py -> repository root.
_REPO_ROOT = Path(__file__).resolve().parent.parent

if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


# ---------------------------------------------------------------------------
# Default-proposer pinning for the google-adk-free orchestrator/evolve suite
# ---------------------------------------------------------------------------

#: Test modules that exercise the REAL default-proposer selection (and so
#: must NOT have the builtin default pinned to the text shim). They either
#: assert on the selected agent type or drive the ADK default agent through
#: their own monkeypatched ``build_default_adk_agent``.
_REAL_DEFAULT_PROPOSER_MODULES = frozenset(
    {
        "test_proposer_agent",
        "test_proposer_adk_agent",
    }
)


@pytest.fixture(autouse=True)
def _pin_default_proposer_to_text_shim(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pin the builtin-default proposer to the text-shim engine in tests.

    The production DEFAULT proposer is the tool-using ADK agent, which pulls
    in the optional ``google-adk`` extra and a real model at propose time.
    The orchestrator / evolve test suites stub the auxiliary callable and
    assert on tournament / lineage outcomes — they are about the loop, not
    the proposer model — and the suite contract is that they run without
    ``google-adk`` or real model traffic.

    This autouse fixture wraps
    :func:`zicato.proposer.agent.build_proposer_agent` so the builtin-default
    spec resolves to a text-shim :class:`DefaultProposerAgent` (driven by the
    stubbed auxiliary callable, exactly as before the default flipped), while
    EVERY other spec — a custom ``agent.py`` proposer, a skills-only dir —
    still flows through the real builder. Modules in
    :data:`_REAL_DEFAULT_PROPOSER_MODULES` opt out so the real selection (and
    the real ADK default-agent path) is still tested directly there.
    """
    module_name = request.module.__name__.rsplit(".", 1)[-1]
    if module_name in _REAL_DEFAULT_PROPOSER_MODULES:
        return

    from zicato.core.types import ProposerSpec
    from zicato.proposer import agent as proposer_agent_mod

    real_build = proposer_agent_mod.build_proposer_agent

    def _build(spec: ProposerSpec, proposer_path: Path | None = None) -> Any:
        if spec == ProposerSpec.default():
            return proposer_agent_mod.DefaultProposerAgent(spec)
        return real_build(spec, proposer_path)

    monkeypatch.setattr(proposer_agent_mod, "build_proposer_agent", _build)


# ---------------------------------------------------------------------------
# Harmonograf auto-launch neutering for the evolve-driver suite
# ---------------------------------------------------------------------------

#: Test modules that exercise the REAL harmonograf auto-launch /
#: resolution path and so must NOT have it stubbed. They either drive the
#: supervisor directly or assert on the launch/no-launch decision.
_REAL_HARMONOGRAF_LAUNCH_MODULES = frozenset(
    {
        "test_harmonograf_supervisor",
        "test_harmonograf_operator_e2e",
        "test_resolver_opt_out_does_not_launch",
    }
)


@pytest.fixture(autouse=True)
def _stub_harmonograf_launch(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stub the evolve harmonograf auto-launch to a no-op for the loop suite.

    ``orchestrator.evolve_n_rounds`` calls
    :func:`zicato.orchestrator._resolve_or_launch_harmonograf`, which — when
    ``ZICATO_HARMONOGRAF_URL`` is unset — spawns a *real* in-process
    harmonograf server and health-polls it (~5s) once per evolve. The
    orchestrator/evolve test suites are about the tournament/lineage loop,
    not the live console; the console is additive and never load-bearing,
    so launching a real server in each of those tests adds ~50s of pure
    startup wait across the suite for zero behavioural coverage.

    This autouse fixture replaces the resolver with one that returns the
    same shape the opt-out / degraded-install path already returns —
    ``("", _NoopShutdownHandle())`` — so the orchestrator runs its
    JSONL-only telemetry branch and the ``finally`` block's unconditional
    ``handle.shutdown()`` stays a clean no-op. Modules in
    :data:`_REAL_HARMONOGRAF_LAUNCH_MODULES` opt out so the launch / no-
    launch decision and the supervisor lifecycle are still covered there.
    """
    module_name = request.module.__name__.rsplit(".", 1)[-1]
    if module_name in _REAL_HARMONOGRAF_LAUNCH_MODULES:
        return

    import zicato.orchestrator as orchestrator_mod

    def _no_launch(workspace_root: Path) -> tuple[str, Any]:
        del workspace_root
        return "", orchestrator_mod._NoopShutdownHandle()

    monkeypatch.setattr(orchestrator_mod, "_resolve_or_launch_harmonograf", _no_launch)


# ---------------------------------------------------------------------------
# Dashboard subprocess hygiene
# ---------------------------------------------------------------------------

# The exact argv fragment a real dashboard child carries. Only ``python -m
# zicato.dashboard`` children are test orphans; a live ``zicato dashboard``
# (no ``-m``) launched by the operator is a different process and is never
# touched here.
_DASHBOARD_ARGV_MARKER = "-m zicato.dashboard"


class FakeDashboardProc:
    """Minimal stand-in for an ``asyncio.subprocess.Process``.

    Records terminate/kill so a test can assert the teardown path while
    never starting a real OS process. Mirrors the ``_FakeProc`` used by
    ``test_cli_dashboard.py`` so the two CLI test modules behave the same.
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


@pytest.fixture
def mock_dashboard_spawn(monkeypatch: pytest.MonkeyPatch) -> list[FakeDashboardProc]:
    """Patch ``asyncio.create_subprocess_exec`` with a non-spawning fake.

    Returns the list of spawned :class:`FakeDashboardProc` so a test can
    assert on the recorded children. Any CLI test that runs ``evolve`` to
    a normal conclusion must use this (directly or transitively) so the
    real dashboard child is never launched and then orphaned.
    """
    spawned: list[FakeDashboardProc] = []

    async def _fake_exec(*args: Any, **kwargs: Any) -> FakeDashboardProc:
        del kwargs
        argv = tuple(str(a) for a in args)
        proc = FakeDashboardProc(argv)
        spawned.append(proc)
        # If this is the dashboard spawn, publish a fake endpoint file so the
        # CLI's bound-port readback resolves immediately instead of polling
        # the full fallback timeout (the real server would write this once it
        # bound a port).
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


def _leaked_dashboard_pids() -> list[int]:
    """PIDs of any live ``python -m zicato.dashboard`` child."""
    try:
        out = subprocess.run(
            ["ps", "-eo", "pid,args"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - ps absent
        return []
    pids: list[int] = []
    for line in out.splitlines():
        if _DASHBOARD_ARGV_MARKER not in line:
            continue
        head = line.strip().split(None, 1)
        if head and head[0].isdigit():
            pids.append(int(head[0]))
    return pids


@pytest.fixture(autouse=True)
def _reap_leaked_dashboards() -> Iterator[None]:
    """Safety net: group-kill any real dashboard child a test leaks.

    A test should never spawn a real dashboard (see ``mock_dashboard_spawn``),
    but if one slips through, terminate the whole process group so nothing —
    including any harmonograf the dashboard parents — is left squatting on a
    port. Only ``-m zicato.dashboard`` children are reaped; a live operator
    ``zicato dashboard`` is a distinct argv and is never matched.
    """
    before = set(_leaked_dashboard_pids())
    try:
        yield
    finally:
        leaked = [pid for pid in _leaked_dashboard_pids() if pid not in before]
        for pid in leaked:
            # Kill the whole session/process group when we can, falling back
            # to the bare pid; escalate SIGTERM -> SIGKILL.
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    pgid = os.getpgid(pid)
                    os.killpg(pgid, sig)
                except (ProcessLookupError, PermissionError, OSError):
                    try:
                        os.kill(pid, sig)
                    except (ProcessLookupError, PermissionError, OSError):
                        break
