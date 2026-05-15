"""zicato.cli — Click-based command-line interface.

This package exposes :func:`main` as the console-script entry point for
the ``zicato`` executable. The root :class:`click.Group` is constructed
by :func:`zicato.cli.discovery.build_cli_root`, which auto-discovers
every importable module under :mod:`zicato.cli.commands` and registers
its top-level :class:`click.Command` / :class:`click.Group` objects.

Subcommands are intentionally split across many small modules so that
parallel work streams can each own a single command file without
stepping on the root group. A broken plugin module logs a warning and
is skipped; it does not crash the CLI.
"""

from __future__ import annotations

from zicato.cli.discovery import build_cli_root


def main() -> None:
    """Console-script entry point.

    Bootstraps the click root group via auto-discovery and invokes it.
    Click handles ``sys.argv`` parsing internally.
    """
    root = build_cli_root()
    root()


__all__ = ["main", "build_cli_root"]
