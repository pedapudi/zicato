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
* :func:`_reap_leaked_dashboards` is an autouse, session-scoped safety
  net: even if some test spawns a real dashboard child, it is killed at
  session end so nothing leaks past the suite. The sweep is scoped to
  dashboards whose ``--workspace`` lies inside this session's pytest temp
  root — a dashboard serving any OTHER workspace (an operator's live
  instance, a ``zicato evolve`` running concurrently on the same host) is
  provably not the suite's and is never signalled.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tests._dashboard_spawn import FakeDashboardProc, install_spawn_mock

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


# ---------------------------------------------------------------------------
# The slow tier: dropped from the bare default run, and from nothing else.
# ---------------------------------------------------------------------------


def _selects_something(config: pytest.Config) -> bool:
    """Did this invocation name what it wants to run?

    Three things count as naming. A positional argument — a path, a
    directory or a node id — means the caller pointed at something; pytest
    records that it fell back to the configured ``testpaths`` instead, so
    anything other than that fallback is a selection. An explicit ``-m``
    means the caller wrote their own marker expression, and the one in
    ``addopts`` is replaced rather than combined, so honouring the tier on
    top of it would silently narrow what they asked for. A ``-k`` is a
    selection by name like any other: someone who types
    ``pytest -k convergence`` has said which tests they want.

    The command line is read from ``invocation_params.args``, which holds
    what was typed WITHOUT the ``addopts`` pytest prepends. Reading
    ``config.option.markexpr`` instead would be wrong: ``addopts`` always
    sets it, so it can never distinguish the two cases.
    """
    if config.args_source is not pytest.Config.ArgsSource.TESTPATHS:
        return True
    return any(
        argument in ("-m", "--markexpr", "-k", "--keyword")
        or argument.startswith(("--markexpr=", "--keyword="))
        or (argument.startswith(("-m", "-k")) and not argument.startswith("--"))
        for argument in config.invocation_params.args
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Deselect the `slow` tier when, and only when, the run is the bare default.

    The tier exists so the run a contributor gets by typing `pytest` is
    about a minute rather than seven. It is a scheduling decision and never
    a coverage one, so the moment anyone names a file, a test or a marker
    expression, they get what they named:

        pytest                              the default tier
        pytest tests/test_convergence_known_answer.py   both of its tests
        pytest tests/x.py::test_y           that test, whatever tier it is in
        pytest -k convergence               what the name matches, both tiers
        pytest -m slow                      the tier alone
        pytest -m "not node and not cascade_oc"         everything

    Implemented here rather than as a `not slow` term in `addopts` because
    an `addopts` term applies to every invocation, including one that names
    a `slow` test — which would report green having run nothing.
    """
    if not _selects_something(config):
        kept, dropped = [], []
        for item in items:
            (dropped if item.get_closest_marker("slow") else kept).append(item)
        if dropped:
            config.hook.pytest_deselected(items=dropped)
            items[:] = kept


@pytest.fixture(autouse=True)
def _isolate_config_pins() -> Iterator[None]:
    """Clear process-pinned config overrides around every test.

    CLI commands (and tests exercising them) pin flag values process-wide
    via :func:`zicato.config.pin_overrides`; the pins are module-global
    state and would otherwise leak from one test into the next. Cleared
    on BOTH sides so a test neither inherits nor bequeaths pins.
    """
    from zicato.config import clear_pinned_overrides

    clear_pinned_overrides()
    yield
    clear_pinned_overrides()


@pytest.fixture(autouse=True)
def _isolate_mutation_syntax_table() -> Iterator[None]:
    """Restore the built-in mutation syntax table around every test.

    The declared table (MUTATION-SURFACE.md §2.5) is process-level state:
    the run path installs the contract's table once per invocation so
    propose-time and apply-time enumeration cannot disagree about what is
    surface. In a test process that same property is a leak — a test that
    activates a workspace declaring ``.ts`` would otherwise leave every
    LATER test enumerating under it, an order-dependent failure rather than
    an obvious one. Restored on BOTH sides, like the config pins above, so
    a test neither inherits nor bequeaths a declared surface.
    """
    from zicato.mutation.markers import install_syntax_table

    install_syntax_table(None)
    yield
    install_syntax_table(None)


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

    def _build(
        spec: ProposerSpec,
        proposer_path: Path | None = None,
        external_config: Any = None,
    ) -> Any:
        if spec == ProposerSpec.default():
            return proposer_agent_mod.DefaultProposerAgent(spec)
        return real_build(spec, proposer_path, external_config)

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
    :func:`zicato.evolve.lifecycle_services._resolve_or_launch_harmonograf`, which — when
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

    from zicato.evolve import lifecycle_services

    def _no_launch(workspace_root: Path) -> tuple[str, Any]:
        del workspace_root
        return "", lifecycle_services._NoopShutdownHandle()

    monkeypatch.setattr(lifecycle_services, "_resolve_or_launch_harmonograf", _no_launch)


# ---------------------------------------------------------------------------
# Dashboard subprocess hygiene
# ---------------------------------------------------------------------------

# The exact argv fragment a real dashboard child carries. Only ``python -m
# zicato.dashboard`` children are test orphans; a live ``zicato dashboard``
# (no ``-m``) launched by the operator is a different process and is never
# touched here.
_DASHBOARD_ARGV_MARKER = "-m zicato.dashboard"


@pytest.fixture
def mock_dashboard_spawn(monkeypatch: pytest.MonkeyPatch) -> list[FakeDashboardProc]:
    """Patch ``asyncio.create_subprocess_exec`` with a non-spawning fake.

    Returns the list of spawned :class:`FakeDashboardProc` so a test can
    assert on the recorded children. Any CLI test that runs ``evolve`` to
    a normal conclusion must use this (directly or transitively) so the
    real dashboard child is never launched and then orphaned.
    """
    return install_spawn_mock(monkeypatch)


def _dashboard_workspace_arg(args: str) -> str | None:
    """The ``--workspace`` value carried by a dashboard argv line, or ``None``.

    The evolve CLI and the test fixtures always spawn the dashboard child
    with an explicit ``--workspace <path>`` pair, so a marker line without
    one is not a process this suite could have spawned.
    """
    tokens = args.split()
    for i, token in enumerate(tokens):
        if token == "--workspace" and i + 1 < len(tokens):
            return tokens[i + 1]
    return None


def _session_dashboard_pids(tmp_root: Path) -> list[int]:
    """PIDs of live ``python -m zicato.dashboard`` children OWNED BY THIS
    TEST SESSION — i.e. whose ``--workspace`` argv points inside this
    session's pytest temp root.

    The workspace path is the ownership fingerprint: every fixture
    workspace a test could hand to a real dashboard child lives under the
    session's ``tmp_path`` basetemp. A dashboard serving ANY other
    workspace — an operator's live instance, a concurrently-running
    ``zicato evolve`` on this host — is provably not ours and must never
    be selected. (The previous before/after pid-snapshot heuristic got
    exactly that wrong: a dashboard that a *concurrent* evolve spawned
    mid-session looked "leaked" and was group-killed, taking the whole
    innocent evolve invocation down with it.)
    """
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
        if len(head) != 2 or not head[0].isdigit():
            continue
        workspace = _dashboard_workspace_arg(head[1])
        if workspace is None:
            continue
        try:
            inside = Path(workspace).resolve().is_relative_to(tmp_root)
        except (OSError, ValueError):  # pragma: no cover - unresolvable path
            inside = False
        if inside:
            pids.append(int(head[0]))
    return pids


def _reap_session_dashboards(tmp_root: Path) -> list[int]:
    """Kill every leaked session-owned dashboard; return the pids acted on.

    Escalates SIGTERM -> SIGKILL. Kills by PROCESS GROUP when the child
    leads its own group (the evolve spawn helpers use
    ``start_new_session=True``, so a real dashboard child parents any
    harmonograf it launched in a group of its own) — but NEVER signals our
    own process group: if the target still shares the test runner's group,
    only the bare pid is signalled, so the safety net cannot take down the
    pytest session itself.
    """
    own_pgid = os.getpgid(0)
    reaped: list[int] = []
    for pid in _session_dashboard_pids(tmp_root):
        reaped.append(pid)
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                pgid = os.getpgid(pid)
                if pgid == own_pgid:
                    os.kill(pid, sig)
                else:
                    os.killpg(pgid, sig)
            except (ProcessLookupError, PermissionError, OSError):
                try:
                    os.kill(pid, sig)
                except (ProcessLookupError, PermissionError, OSError):
                    break
    return reaped


@pytest.fixture(autouse=True, scope="session")
def _reap_leaked_dashboards(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[None]:
    """Safety net: kill any real dashboard child the session leaks.

    A test should never spawn a real dashboard (see ``mock_dashboard_spawn``),
    but if one slips through, terminate it (and its own process group) so
    nothing — including any harmonograf the dashboard parents — is left
    squatting on a port. Only ``-m zicato.dashboard`` children are
    considered (a live operator ``zicato dashboard`` is a distinct argv),
    and only those whose ``--workspace`` lies inside THIS session's pytest
    temp root are touched — a dashboard serving any other workspace (an
    operator's, a concurrent ``zicato evolve``'s on the same host) is not
    ours and is never signalled. See :func:`_session_dashboard_pids` /
    :func:`_reap_session_dashboards`.

    SESSION-scoped on purpose: the net's contract is only that a leaked
    child never survives the session, so one sweep at session end (per
    xdist worker) keeps that contract at ~0 cost.
    """
    basetemp = tmp_path_factory.getbasetemp().resolve()
    # Under xdist each worker's basetemp is <session-base>/popen-gwN; sweep
    # the whole session base so a worker can also catch a sibling's leak.
    tmp_root = basetemp.parent if basetemp.name.startswith("popen-") else basetemp
    try:
        yield
    finally:
        _reap_session_dashboards(tmp_root)


# ---------------------------------------------------------------------------
# Minimal on-disk contract inputs
# ---------------------------------------------------------------------------


@pytest.fixture()
def board_file(tmp_path: Path) -> Path:
    """A board file holding one single-turn entry.

    The smallest board the epoch commands accept. Its content is
    scaffolding for tests whose subject is epoch lifecycle, lineage or
    analysis rather than the board itself; a test about board parsing
    writes its own.
    """
    path = tmp_path / "board.jsonl"
    path.write_text(
        '{"id": "e1", "kind": "single_turn", "wall_clock_budget_seconds": 60, "input": "hi"}\n'
    )
    return path


@pytest.fixture()
def epoch_root(tmp_path: Path) -> tuple[Path, str]:
    """An empty epoch directory inside a workspace, and that epoch's id.

    Nothing but the directory is written, so a test that reads an epoch
    with no generations sees the same tree the lifecycle would have left
    immediately after ``epoch new``.
    """
    ws = tmp_path / ".zicato"
    epoch_id = "2026-04-08_test"
    (ws / "epochs" / epoch_id).mkdir(parents=True)
    return ws, epoch_id


@pytest.fixture()
def static_dir(tmp_path: Path) -> Path:
    """A static asset root holding the one file the dashboard server requires.

    The server refuses to start without an ``index.html`` to serve. Its
    content is never asserted by the suites that use this fixture — they
    exercise endpoints, not the served page — so it is the shortest
    well-formed document. A suite whose subject IS the served page builds
    its own root.
    """
    d = tmp_path / "static"
    d.mkdir()
    (d / "index.html").write_text("<!doctype html><title>z</title>", encoding="utf-8")
    return d
