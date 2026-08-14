"""Tests for the zicato CLI root group itself."""

from __future__ import annotations

import click
from click.testing import CliRunner

from zicato.cli import main
from zicato.cli.discovery import build_cli_root


def test_build_cli_root_returns_click_group() -> None:
    root = build_cli_root()
    assert isinstance(root, click.Group)
    assert root.name == "zicato"


def test_root_help_lists_primary_and_grouped_registration() -> None:
    runner = CliRunner()
    result = runner.invoke(build_cli_root(), ["--help"])
    assert result.exit_code == 0, result.output
    assert "init" in result.output
    assert "epoch" in result.output
    assert "register" not in result.output


def test_root_unknown_command_exits_nonzero() -> None:
    runner = CliRunner()
    result = runner.invoke(build_cli_root(), ["does-not-exist"])
    assert result.exit_code != 0


def test_main_is_callable() -> None:
    # We don't actually invoke main() with sys.argv here — just verify
    # it's the expected callable. CliRunner exercises the full path
    # already via build_cli_root().
    assert callable(main)


def test_version_option_renders() -> None:
    runner = CliRunner()
    result = runner.invoke(build_cli_root(), ["--version"])
    assert result.exit_code == 0, result.output
    assert "zicato" in result.output.lower()
