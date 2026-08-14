"""Builder focus is part of the dashboard command, not a root alias."""

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from click.testing import CliRunner

from zicato.cli.commands.dashboard import BUILDER_FRAGMENT, dashboard_cmd, dashboard_url
from zicato.cli.discovery import build_cli_root


def test_builder_focus_has_no_root_command() -> None:
    assert "builder" not in build_cli_root().commands


def test_builder_deep_link() -> None:
    assert BUILDER_FRAGMENT == "/#/builder"
    assert dashboard_url("127.0.0.1", 7892, "builder") == "http://127.0.0.1:7892/#/builder"


def test_dashboard_builder_focus_uses_dashboard_server(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: dict[str, Any] = {}
    server = SimpleNamespace(run=lambda **kwargs: captured.update(kwargs))
    monkeypatch.setitem(
        __import__("sys").modules, "zicato.dashboard", SimpleNamespace(server=server)
    )
    monkeypatch.setitem(__import__("sys").modules, "zicato.dashboard.server", server)

    result = CliRunner().invoke(
        dashboard_cmd,
        ["--workspace", str(tmp_path), "--port", "8123", "--view", "builder"],
    )
    assert result.exit_code == 0, result.output
    assert "http://127.0.0.1:8123/#/builder" in result.output
    assert captured["workspace_root"] == tmp_path.resolve()
