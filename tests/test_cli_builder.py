"""Tests for the ``zicato builder`` command.

Covered:

* ``zicato builder`` is discovered as a root subcommand with the
  ``--workspace`` / ``--dashboard-port`` options and the documented defaults.
* It wires to the SAME dashboard launch machinery as ``zicato dashboard``
  (``dashboard.server.run`` with the workspace, loopback host, port and the
  bundled static dir).
* It surfaces the builder DEEP-LINK (``http://127.0.0.1:<port>/#/builder``) as
  the primary link, and binds loopback-only (never ``0.0.0.0``).
* A missing dashboard service errors cleanly rather than tracebacking.

The subprocess / server run is mocked — no real dashboard server is started.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from zicato.cli.commands.builder import BUILDER_FRAGMENT, builder_cmd, builder_url
from zicato.cli.discovery import build_cli_root


def test_builder_is_registered_root_command() -> None:
    """``zicato builder`` shows up in the root group's --help."""
    runner = CliRunner()
    result = runner.invoke(build_cli_root(), ["--help"])
    assert result.exit_code == 0, result.output
    assert "builder" in result.output


def test_builder_command_options_and_defaults() -> None:
    """``zicato builder --help`` exposes workspace / dashboard-port with the
    documented defaults."""
    runner = CliRunner()
    result = runner.invoke(builder_cmd, ["--help"])
    assert result.exit_code == 0, result.output
    for opt in ("--workspace", "--dashboard-port"):
        assert opt in result.output, f"builder missing option {opt}"
    # Defaults: cwd's .zicato workspace, port 7892.
    assert ".zicato" in result.output
    assert "7892" in result.output


def test_builder_url_is_the_builder_deep_link() -> None:
    """``builder_url`` deep-links the dashboard at the builder fragment."""
    assert BUILDER_FRAGMENT == "/#/builder"
    assert builder_url("127.0.0.1", 7892) == "http://127.0.0.1:7892/#/builder"


def test_builder_invokes_server_run_and_surfaces_the_builder_url(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``zicato builder`` calls ``dashboard.server.run`` with the workspace,
    loopback host, port and bundled static dir, and prints the builder URL."""
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
        builder_cmd,
        ["--workspace", str(tmp_path), "--dashboard-port", "8123"],
    )
    assert result.exit_code == 0, result.output

    # Same dashboard launch machinery: server.run with workspace / host / port /
    # bundled static dir.
    assert captured["workspace_root"] == tmp_path.resolve()
    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 8123
    assert captured["static_dir"].name == "static"

    # The builder DEEP-LINK is the primary printed link.
    assert "http://127.0.0.1:8123/#/builder" in result.output


def test_builder_binds_loopback_only(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The bind address is fixed at loopback — never a routable interface."""
    import types

    captured: dict[str, Any] = {}
    fake_server = types.SimpleNamespace(run=lambda **kw: captured.update(kw))
    fake_pkg = types.SimpleNamespace(server=fake_server)
    monkeypatch.setitem(__import__("sys").modules, "zicato.dashboard", fake_pkg)
    monkeypatch.setitem(__import__("sys").modules, "zicato.dashboard.server", fake_server)

    runner = CliRunner()
    result = runner.invoke(builder_cmd, ["--workspace", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert captured["host"] == "127.0.0.1"
    assert captured["host"] != "0.0.0.0"
    assert "0.0.0.0" not in result.output


def test_builder_reports_missing_service(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the dashboard service is unavailable the command errors cleanly
    rather than tracebacking."""
    import builtins

    real_import = builtins.__import__

    def _blocking_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "zicato.dashboard" or name.startswith("zicato.dashboard."):
            raise ImportError("no dashboard service in this build")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _blocking_import)

    runner = CliRunner()
    result = runner.invoke(builder_cmd, ["--workspace", "."])
    assert result.exit_code != 0
    assert "dashboard service" in result.output.lower()
