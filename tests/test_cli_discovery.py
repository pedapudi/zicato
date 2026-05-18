"""Tests for the auto-discovery of zicato CLI subcommands.

Exercises three behaviours:

1. A synthetic module placed under ``zicato.cli.commands`` is picked up
   and registered.
2. A module that fails to import is logged-and-skipped, not fatal.
3. Modules without a :class:`click.Command` are silently ignored.
"""

from __future__ import annotations

import logging
import sys
import textwrap
from pathlib import Path

import click
import pytest
from click.testing import CliRunner

from zicato.cli.discovery import build_cli_root


def _write_command_module(pkg_dir: Path, filename: str, body: str) -> None:
    """Write a python file under ``pkg_dir`` with ``body`` as its source."""
    (pkg_dir / filename).write_text(textwrap.dedent(body))


def _make_isolated_commands_pkg(
    tmp_path: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Point ``zicato.cli.commands.__path__`` at a fresh tmpdir so the
    test owns the discovery namespace for the duration of the test.

    Returns the tmpdir.
    """
    import zicato.cli.commands as commands_pkg

    pkg_dir = Path(tmp_path) / "zicato_test_commands"
    pkg_dir.mkdir()
    # Don't drop an __init__.py — pkgutil.iter_modules walks raw dirs
    # and we want the synthetic modules to be importable as
    # ``zicato.cli.commands.<name>``.
    monkeypatch.setattr(commands_pkg, "__path__", [str(pkg_dir)])

    # Evict any previously-imported synthetic modules so a second test
    # gets a fresh import.
    for name in list(sys.modules):
        if name.startswith("zicato.cli.commands.") and not name.endswith((".init", ".register")):
            sys.modules.pop(name, None)
    return pkg_dir


def test_discovery_picks_up_synthetic_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pkg_dir = _make_isolated_commands_pkg(tmp_path, monkeypatch)
    _write_command_module(
        pkg_dir,
        "synthetic.py",
        """
        import click

        @click.command(name="synthetic")
        def synthetic_cmd():
            click.echo("hello from synthetic")
        """,
    )

    root = build_cli_root()
    assert "synthetic" in root.commands

    runner = CliRunner()
    result = runner.invoke(root, ["synthetic"])
    assert result.exit_code == 0, result.output
    assert "hello from synthetic" in result.output


def test_discovery_picks_up_multiple_commands_per_module(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pkg_dir = _make_isolated_commands_pkg(tmp_path, monkeypatch)
    _write_command_module(
        pkg_dir,
        "multi.py",
        """
        import click

        @click.command(name="alpha")
        def alpha_cmd():
            click.echo("alpha")

        @click.command(name="beta")
        def beta_cmd():
            click.echo("beta")
        """,
    )

    root = build_cli_root()
    assert "alpha" in root.commands
    assert "beta" in root.commands


def test_discovery_accepts_subgroups(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pkg_dir = _make_isolated_commands_pkg(tmp_path, monkeypatch)
    _write_command_module(
        pkg_dir,
        "grouped.py",
        """
        import click

        @click.group(name="grouped")
        def grouped_group():
            pass

        @grouped_group.command(name="leaf")
        def leaf_cmd():
            click.echo("leaf")
        """,
    )

    root = build_cli_root()
    assert "grouped" in root.commands
    assert isinstance(root.commands["grouped"], click.Group)

    runner = CliRunner()
    result = runner.invoke(root, ["grouped", "leaf"])
    assert result.exit_code == 0, result.output
    assert "leaf" in result.output


def test_discovery_ignores_modules_with_no_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pkg_dir = _make_isolated_commands_pkg(tmp_path, monkeypatch)
    _write_command_module(
        pkg_dir,
        "helpers.py",
        """
        def some_helper():
            return 42
        """,
    )
    # The module exists but exposes no click.Command — the CLI must
    # still build successfully.
    root = build_cli_root()
    assert "helpers" not in root.commands


def test_discovery_skips_modules_starting_with_underscore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pkg_dir = _make_isolated_commands_pkg(tmp_path, monkeypatch)
    _write_command_module(
        pkg_dir,
        "_private.py",
        """
        import click

        @click.command(name="private")
        def private_cmd():
            click.echo("should not appear")
        """,
    )

    root = build_cli_root()
    assert "private" not in root.commands


def test_broken_module_does_not_kill_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    pkg_dir = _make_isolated_commands_pkg(tmp_path, monkeypatch)

    # One healthy module:
    _write_command_module(
        pkg_dir,
        "healthy.py",
        """
        import click

        @click.command(name="healthy")
        def healthy_cmd():
            click.echo("ok")
        """,
    )
    # One module that explodes at import time:
    _write_command_module(
        pkg_dir,
        "broken.py",
        """
        raise RuntimeError("intentional import-time failure")
        """,
    )

    with caplog.at_level(logging.WARNING, logger="zicato.cli.discovery"):
        root = build_cli_root()

    # Healthy command still registered.
    assert "healthy" in root.commands
    # Broken module's failure was logged, not raised.
    assert any(
        "broken" in record.getMessage() and "failed to import" in record.getMessage()
        for record in caplog.records
    ), caplog.records


def test_duplicate_command_name_first_wins(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    pkg_dir = _make_isolated_commands_pkg(tmp_path, monkeypatch)
    # iter_modules order is alphabetical; "a_first" wins over "b_second".
    _write_command_module(
        pkg_dir,
        "a_first.py",
        """
        import click

        @click.command(name="dup")
        def first_dup():
            click.echo("first")
        """,
    )
    _write_command_module(
        pkg_dir,
        "b_second.py",
        """
        import click

        @click.command(name="dup")
        def second_dup():
            click.echo("second")
        """,
    )

    with caplog.at_level(logging.WARNING, logger="zicato.cli.discovery"):
        root = build_cli_root()

    assert "dup" in root.commands
    runner = CliRunner()
    result = runner.invoke(root, ["dup"])
    assert result.exit_code == 0
    assert "first" in result.output
    # A warning about the duplicate must have been logged.
    assert any("duplicate command name" in r.getMessage() for r in caplog.records)
