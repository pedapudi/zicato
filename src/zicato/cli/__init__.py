"""zicato.cli — Click-based command-line interface.

This package exposes :func:`main` as the console-script entry point for
the ``zicato`` executable. The root :class:`click.Group` is constructed
by :func:`zicato.cli.discovery.build_cli_root`, which imports each
command module under :mod:`zicato.cli.commands` by name and places every
command at its fixed position in the tree. A command module that fails to
import fails the CLI; there is no plugin discovery.

Subcommands are split across small modules so that each command's code
lives in one file without touching the root group.
"""

from __future__ import annotations

from zicato.cli.discovery import build_cli_root


def main() -> None:
    """Console-script entry point.

    Builds the click root group and invokes it.
    Click handles ``sys.argv`` parsing internally.
    """
    root = build_cli_root()
    root()


__all__ = ["main", "build_cli_root"]
