"""Tests for the ``zicato dashboard`` command and the dashboard wiring
into ``zicato evolve``.

Covered:

* ``zicato dashboard`` is discovered as a root subcommand with the
  ``--workspace`` / ``--host`` / ``--port`` options and the right
  defaults.
* ``resolve_static_dir`` points at the bundled
  ``zicato/dashboard/static`` directory and honours the env override.
* ``zicato evolve`` spawns the watchdog supervisor with
  ``--no-dashboard`` and ALSO spawns the Python dashboard service.
* ``zicato evolve --no-dashboard`` suppresses the dashboard spawn.
* Both children are torn down when the evolve loop exits.

The subprocess spawns are mocked — no real supervisor binary, no real
dashboard server.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from zicato.cli.commands.dashboard import dashboard_cmd, resolve_static_dir
from zicato.cli.discovery import build_cli_root

# ---------------------------------------------------------------------------
# Stub LLMs for evolve invocations
# ---------------------------------------------------------------------------


async def _harness_call_llm(system: str, user: str, model: str) -> str:
    del system, user, model
    return ""


async def _aux_call_llm(system: str, user: str, model: str) -> str:
    del system, user, model
    return ""


# ---------------------------------------------------------------------------
# zicato dashboard — command registration + options
# ---------------------------------------------------------------------------


def test_dashboard_is_registered_root_command() -> None:
    """``zicato dashboard`` shows up in the root group's --help."""
    runner = CliRunner()
    result = runner.invoke(build_cli_root(), ["--help"])
    assert result.exit_code == 0, result.output
    assert "dashboard" in result.output


def test_dashboard_command_options_and_defaults() -> None:
    """``zicato dashboard --help`` exposes workspace / host / port with
    the documented defaults."""
    runner = CliRunner()
    result = runner.invoke(dashboard_cmd, ["--help"])
    assert result.exit_code == 0, result.output
    for opt in ("--workspace", "--host", "--port"):
        assert opt in result.output, f"dashboard missing option {opt}"
    # Defaults: cwd's .zicato workspace, loopback host, port 7892.
    assert ".zicato" in result.output
    assert "127.0.0.1" in result.output
    assert "7892" in result.output


def test_resolve_static_dir_points_at_bundled_static() -> None:
    """``resolve_static_dir`` resolves the in-tree dashboard static dir."""
    static = resolve_static_dir()
    assert static.name == "static"
    assert static.parent.name == "dashboard"
    # The bundle exists in the checkout.
    assert (static / "index.html").exists()


def test_resolve_static_dir_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """``ZICATO_DASHBOARD_STATIC_DIR`` short-circuits resolution."""
    monkeypatch.setenv("ZICATO_DASHBOARD_STATIC_DIR", "/custom/static")
    assert resolve_static_dir() == Path("/custom/static")


def test_dashboard_invokes_server_run(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """``zicato dashboard`` calls ``dashboard.server.run`` with the
    workspace, host, port and bundled static dir."""
    import types

    captured: dict[str, Any] = {}

    def _fake_run(**kwargs: Any) -> None:
        captured.update(kwargs)

    fake_server = types.SimpleNamespace(run=_fake_run)
    fake_pkg = types.SimpleNamespace(server=fake_server)
    monkeypatch.setitem(__import__("sys").modules, "zicato.dashboard", fake_pkg)
    monkeypatch.setitem(__import__("sys").modules, "zicato.dashboard.server", fake_server)

    runner = CliRunner()
    result = runner.invoke(
        dashboard_cmd,
        ["--workspace", str(tmp_path), "--host", "127.0.0.1", "--port", "8001"],
    )
    assert result.exit_code == 0, result.output
    assert captured["workspace_root"] == tmp_path.resolve()
    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 8001
    assert captured["static_dir"].name == "static"
    # The command no longer pre-prints the requested-port URL: the
    # definitive ``Dashboard:`` line is emitted by ``server.run`` once the
    # real bound port is known (a TIME_WAIT bounce can shift it), so the
    # command must NOT advertise the requested port itself. It still prints
    # the "Serving workspace" line.
    assert "http://127.0.0.1:8001" not in result.output
    assert "Serving workspace" in result.output


def test_dashboard_reports_missing_service(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the dashboard service is unavailable the command errors
    cleanly rather than tracebacking."""
    import builtins

    real_import = builtins.__import__

    def _blocking_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "zicato.dashboard" or name.startswith("zicato.dashboard."):
            raise ImportError("no dashboard service in this build")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _blocking_import)

    runner = CliRunner()
    result = runner.invoke(dashboard_cmd, ["--workspace", "."])
    assert result.exit_code != 0
    assert "dashboard service" in result.output.lower()


# ---------------------------------------------------------------------------
# zicato evolve — dashboard auto-spawn wiring
# ---------------------------------------------------------------------------


class _FakeProc:
    """Minimal stand-in for an asyncio subprocess."""

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


def _install_evolve_mocks(monkeypatch: pytest.MonkeyPatch, spawned: list[_FakeProc]) -> None:
    """Mock the subprocess spawns and ``evolve_n_rounds`` for an
    ``evolve`` invocation."""

    async def _fake_exec(*args: str, **kwargs: object) -> _FakeProc:
        proc = _FakeProc(tuple(str(a) for a in args))
        spawned.append(proc)
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)

    # A built supervisor binary always "resolves" so the supervisor
    # branch is exercised deterministically.
    import zicato.cli.commands.evolve as ev

    monkeypatch.setattr(ev, "_resolve_supervisor_binary", lambda: Path("/fake/zicato-supervisor"))

    async def _fake_evolve_n_rounds(**kwargs: Any) -> list[Any]:
        stop_reason_out = kwargs.get("stop_reason_out")
        if stop_reason_out is not None:
            stop_reason_out.append("completed")
        return []

    import zicato.orchestrator as orch_mod

    monkeypatch.setattr(orch_mod, "evolve_n_rounds", _fake_evolve_n_rounds)


def _evolve_args(*extra: str) -> list[str]:
    return [
        "--harness-call-llm",
        "tests.test_cli_dashboard:_harness_call_llm",
        "--auxiliary-call-llm",
        "tests.test_cli_dashboard:_aux_call_llm",
        *extra,
    ]


def test_evolve_spawns_supervisor_and_dashboard(monkeypatch: pytest.MonkeyPatch) -> None:
    """``zicato evolve`` spawns the watchdog supervisor (--no-dashboard)
    AND the Python dashboard service."""
    from zicato.cli.commands.evolve import evolve_cmd

    spawned: list[_FakeProc] = []
    _install_evolve_mocks(monkeypatch, spawned)

    runner = CliRunner()
    result = runner.invoke(evolve_cmd, _evolve_args())
    assert result.exit_code == 0, result.output

    # Two children: the supervisor and the dashboard.
    assert len(spawned) == 2

    sup_argv = next(a.argv for a in spawned if "zicato-supervisor" in a.argv[0])
    assert "--no-dashboard" in sup_argv, "supervisor must be watchdog-only"

    dash_argv = next(a.argv for a in spawned if "zicato.dashboard" in a.argv)
    assert "-m" in dash_argv and "zicato.dashboard" in dash_argv
    assert "--host" in dash_argv
    assert dash_argv[dash_argv.index("--host") + 1] == "127.0.0.1"
    assert "0.0.0.0" not in dash_argv

    # The Python dashboard URL is reported as the primary link.
    assert "Dashboard: http://127.0.0.1:7892" in result.output


def test_evolve_dashboard_port_flag_is_plumbed(monkeypatch: pytest.MonkeyPatch) -> None:
    """``--dashboard-port`` flows through to the dashboard spawn + URL."""
    from zicato.cli.commands.evolve import evolve_cmd

    spawned: list[_FakeProc] = []
    _install_evolve_mocks(monkeypatch, spawned)

    runner = CliRunner()
    result = runner.invoke(evolve_cmd, _evolve_args("--dashboard-port", "9100"))
    assert result.exit_code == 0, result.output

    dash_argv = next(a.argv for a in spawned if "zicato.dashboard" in a.argv)
    assert dash_argv[dash_argv.index("--port") + 1] == "9100"
    assert "Dashboard: http://127.0.0.1:9100" in result.output


def test_evolve_no_dashboard_suppresses_both_spawns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--no-dashboard`` suppresses both the dashboard and the watchdog."""
    from zicato.cli.commands.evolve import evolve_cmd

    spawned: list[_FakeProc] = []
    _install_evolve_mocks(monkeypatch, spawned)

    runner = CliRunner()
    result = runner.invoke(evolve_cmd, _evolve_args("--no-dashboard"))
    assert result.exit_code == 0, result.output
    assert spawned == [], "no children should be spawned with --no-dashboard"
    assert "Dashboard:" not in result.output


def test_evolve_keeps_dashboard_serving_at_normal_conclusion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """At a NORMAL evolve conclusion the watchdog is torn down but the
    dashboard is deliberately LEFT serving (#5) — the final state of the
    run is exactly when the live view is most interesting. evolve prints a
    clear "still serving" line."""
    from zicato.cli.commands.evolve import evolve_cmd

    spawned: list[_FakeProc] = []
    _install_evolve_mocks(monkeypatch, spawned)

    runner = CliRunner()
    result = runner.invoke(evolve_cmd, _evolve_args())
    assert result.exit_code == 0, result.output

    assert len(spawned) == 2
    sup = next(p for p in spawned if "zicato-supervisor" in p.argv[0])
    dash = next(p for p in spawned if "zicato.dashboard" in p.argv)

    # Watchdog supervisor: torn down (no purpose once the loop ends).
    assert sup.terminated, "watchdog supervisor should be torn down on exit"
    # Dashboard: LEFT serving (NOT terminated) so the operator can inspect
    # the final state.
    assert not dash.terminated, "dashboard must be left serving at normal conclusion"
    assert "still serving" in result.output.lower()


def test_evolve_tears_down_both_children_on_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A genuine error path still cleans up BOTH children — only the
    normal-conclusion teardown of the dashboard is suppressed (#5)."""
    import zicato.orchestrator as orch_mod
    from zicato.cli.commands.evolve import evolve_cmd

    spawned: list[_FakeProc] = []
    _install_evolve_mocks(monkeypatch, spawned)

    async def _boom(**_kwargs: Any) -> list[Any]:
        raise RuntimeError("contract drifted")

    monkeypatch.setattr(orch_mod, "evolve_n_rounds", _boom)

    runner = CliRunner()
    result = runner.invoke(evolve_cmd, _evolve_args())
    # RuntimeError is surfaced as a clean CLI error (non-zero exit).
    assert result.exit_code != 0

    assert len(spawned) == 2
    for proc in spawned:
        assert proc.terminated, f"child {proc.argv} was not torn down on the error path"
