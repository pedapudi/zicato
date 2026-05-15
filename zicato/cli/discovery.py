"""Auto-discovery of zicato CLI subcommands.

The root :class:`click.Group` is built dynamically: every importable
module under :mod:`zicato.cli.commands` is imported, and any top-level
:class:`click.Command` or :class:`click.Group` defined on that module is
attached to the root. This lets parallel workstreams ship subcommands
as independent files (one command per module, or a sub-group per
module) without ever editing the root.

Robustness rules:

* If a command module fails to import (``ImportError``, ``SyntaxError``,
  arbitrary ``Exception`` at module top level), we log a warning and
  continue. A single broken plugin must not kill the whole CLI.
* A module with no :class:`click.Command` attributes is silently
  ignored — modules can hold helpers without claiming a subcommand.
* If a module exposes multiple top-level commands, all of them are
  registered. Module-level ``__all__`` is *not* consulted; presence of
  a top-level :class:`click.Command` instance is the only criterion.
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
from typing import TYPE_CHECKING

import click

if TYPE_CHECKING:
    from types import ModuleType

logger = logging.getLogger(__name__)


def _resolve_version() -> str:
    """Best-effort version string for ``--version``.

    Prefers ``importlib.metadata`` (works once the wheel is installed)
    but falls back to ``zicato.__version__`` for an editable / not-yet-
    installed working tree.
    """
    try:
        from importlib.metadata import PackageNotFoundError, version as _pkg_version

        try:
            return _pkg_version("zicato")
        except PackageNotFoundError:
            pass
    except ImportError:  # pragma: no cover - stdlib on 3.11+
        pass
    try:
        from zicato import __version__ as _v

        return _v
    except Exception:  # pragma: no cover - defensive
        return "0.0.0"


def _iter_command_modules() -> list[str]:
    """Return the fully-qualified names of modules under
    ``zicato.cli.commands``.

    Done lazily so tests can monkeypatch the commands package's
    ``__path__`` (e.g. to inject a synthetic command module) before the
    root group is built.
    """
    # Import inside the function so the package's ``__path__`` reflects
    # any monkeypatching done by tests prior to ``build_cli_root``.
    from zicato.cli import commands as commands_pkg

    names: list[str] = []
    for module_info in pkgutil.iter_modules(commands_pkg.__path__):
        # Skip private modules like ``_helpers``.
        if module_info.name.startswith("_"):
            continue
        names.append(f"{commands_pkg.__name__}.{module_info.name}")
    return names


def _extract_commands(module: ModuleType) -> list[click.Command]:
    """Return the top-level :class:`click.Command` objects defined on
    ``module``.

    Includes :class:`click.Group` (a subclass of
    :class:`click.Command`). Order is the module's ``dir()`` order
    (alphabetical), which keeps the registration deterministic.
    """
    found: list[click.Command] = []
    for attr_name in dir(module):
        if attr_name.startswith("_"):
            continue
        obj = getattr(module, attr_name, None)
        if isinstance(obj, click.Command):
            found.append(obj)
    return found


def build_cli_root() -> click.Group:
    """Construct the root :class:`click.Group` for the zicato CLI.

    Iterates every module under :mod:`zicato.cli.commands`, imports it,
    and attaches each top-level :class:`click.Command` or
    :class:`click.Group` to the root. Import errors are logged and the
    module is skipped — a broken plugin does not kill the CLI.
    """

    @click.group(name="zicato")
    @click.version_option(version=_resolve_version(), prog_name="zicato")
    def root() -> None:
        """zicato — a self-improving harness for multi-agent systems."""

    # Track names we've already attached so two modules can't register
    # the same subcommand name (the second wins is confusing; first wins
    # is predictable).
    seen: set[str] = set()

    for module_name in _iter_command_modules():
        try:
            module = importlib.import_module(module_name)
        except Exception as exc:  # pragma: no cover - exercised via tests
            logger.warning(
                "zicato.cli: failed to import command module %r: %s",
                module_name,
                exc,
            )
            continue

        commands = _extract_commands(module)
        if not commands:
            continue

        for cmd in commands:
            cmd_name = cmd.name or module_name.rsplit(".", 1)[-1]
            if cmd_name in seen:
                logger.warning(
                    "zicato.cli: duplicate command name %r from %r ignored",
                    cmd_name,
                    module_name,
                )
                continue
            root.add_command(cmd, name=cmd_name)
            seen.add(cmd_name)

    return root


__all__ = ["build_cli_root"]
