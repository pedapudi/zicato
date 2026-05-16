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
  registered. When ``__all__`` is defined on the module, only the
  names it lists are considered candidate commands; otherwise every
  module-level :class:`click.Command` attribute is considered.
* Commands that are already mounted under a :class:`click.Group`
  defined on the same module are filtered out, so a module that
  exposes a group of subcommands surfaces only the group at the root.

Help layout
-----------
The root group is a :class:`ZicatoGroup`, a small :class:`click.Group`
subclass that renders ``zicato --help`` in two labelled sections — the
**happy path** (``init`` then ``evolve``) and the **advanced /
debugging** commands — instead of one flat alphabetical list. A command
is classified by name: :data:`HAPPY_PATH_COMMANDS` is the happy path and
everything else discovered is advanced. The root help also carries an
epilog with worked usage examples.
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

#: The two commands that make up the happy path, in the order an
#: operator runs them. Everything else discovered under
#: ``zicato.cli.commands`` is treated as an advanced / debugging command
#: and grouped separately in ``zicato --help``.
HAPPY_PATH_COMMANDS: tuple[str, ...] = ("init", "evolve")

#: One-line summaries for the two happy-path commands, shown in the
#: dedicated "happy path" section of ``zicato --help``. Kept here (and
#: not just derived from the command's own short help) so the section
#: reads as a coherent two-step story regardless of how each command
#: file phrases its own docstring.
_HAPPY_PATH_BLURB: dict[str, str] = {
    "init": "Scaffold a fresh .zicato/ workspace (run once per project).",
    "evolve": "Resolve the contract, auto-open an epoch on any change, and run the loop.",
}

#: The epilog appended to ``zicato --help``. Worked examples first, then
#: a one-paragraph mental model. Kept terse — a new operator should be
#: able to copy a line and go.
_ROOT_EPILOG = """\
\b
Happy path (this is the whole tool for most operators):
  zicato init                       # once: scaffold ./.zicato/
  zicato evolve \\
      --harness-call-llm  my_pkg.llms:harness \\
      --auxiliary-call-llm my_pkg.llms:aux \\
      --rounds 4                    # propose -> tournament -> promote, x4

\b
How `evolve` decides on an epoch:
  evolve resolves the evaluation contract (board + proposer brief +
  scoring + the registered inner-harness identity), hashes it, and
  compares that hash to the current epoch. On any change it closes the
  old epoch and opens a fresh one before running -- you never run
  `epoch` / `register` / `propose` / `tournament` by hand. Pass
  --no-auto-epoch to make a drifted contract an error instead.

\b
Advanced commands (off the happy path -- inspection & debugging):
  Use these to look inside a workspace or drive one step manually.
  `evolve` already orchestrates register / propose / tournament /
  reindex / epoch internally; reach for them only when debugging.

\b
First time? Run:
  zicato init && zicato evolve --help
"""


def _resolve_version() -> str:
    """Best-effort version string for ``--version``.

    Prefers ``importlib.metadata`` (works once the wheel is installed)
    but falls back to ``zicato.__version__`` for an editable / not-yet-
    installed working tree.
    """
    try:
        from importlib.metadata import PackageNotFoundError
        from importlib.metadata import version as _pkg_version

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


class ZicatoGroup(click.Group):
    """Root group that renders ``--help`` as happy-path + advanced sections.

    Stock :class:`click.Group` prints every subcommand in a single
    alphabetical ``Commands:`` list. That buries the two commands an
    operator actually needs (``init`` and ``evolve``) among nine
    inspection / debugging commands. This subclass overrides
    :meth:`format_commands` to print two labelled sections instead:

    * **Happy path** — ``init`` then ``evolve``, in run order, each with
      a hand-written one-line blurb (:data:`_HAPPY_PATH_BLURB`).
    * **Advanced commands (off the happy path)** — every other
      discovered command, alphabetically, with its own short help.

    Everything else (option formatting, the epilog, ``--version``) is
    inherited unchanged.
    """

    def format_commands(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        """Write the two command sections into ``formatter``.

        Hidden commands are skipped (matching click's default). A
        happy-path command that somehow failed to register is simply
        omitted from its section rather than crashing the help screen.
        """
        commands: list[tuple[str, click.Command]] = []
        for name in self.list_commands(ctx):
            cmd = self.get_command(ctx, name)
            if cmd is None or cmd.hidden:
                continue
            commands.append((name, cmd))

        if not commands:
            return

        happy: list[tuple[str, click.Command]] = []
        advanced: list[tuple[str, click.Command]] = []
        by_name = dict(commands)
        for name in HAPPY_PATH_COMMANDS:
            if name in by_name:
                happy.append((name, by_name[name]))
        for name, cmd in commands:
            if name not in HAPPY_PATH_COMMANDS:
                advanced.append((name, cmd))

        # Width is shared across both sections so the two-column layout
        # lines up regardless of which section a command lands in.
        limit = formatter.width - 6 - max(len(name) for name, _ in commands)

        if happy:
            rows = [
                (name, _HAPPY_PATH_BLURB.get(name, cmd.get_short_help_str(limit)))
                for name, cmd in happy
            ]
            with formatter.section("Happy path — start here (just these two)"):
                formatter.write_dl(rows)

        if advanced:
            rows = [(name, cmd.get_short_help_str(limit)) for name, cmd in advanced]
            with formatter.section(
                "Advanced commands — off the happy path (inspection & debugging)"
            ):
                formatter.write_dl(rows)


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

    Commands that are already attached as a child of another
    :class:`click.Group` defined on the same module are filtered out —
    so a module that defines a group with sub-commands surfaces only
    the group, not its sub-commands. Modules can also opt into an
    explicit allow-list via ``__all__``: if present, only attributes
    listed there are considered candidate commands.
    """
    explicit: list[str] | None = None
    raw_all = getattr(module, "__all__", None)
    if isinstance(raw_all, list | tuple):
        explicit = [str(n) for n in raw_all]

    candidates: list[click.Command] = []
    for attr_name in dir(module):
        if attr_name.startswith("_"):
            continue
        if explicit is not None and attr_name not in explicit:
            continue
        obj = getattr(module, attr_name, None)
        if isinstance(obj, click.Command):
            candidates.append(obj)

    # Collect the names of every command already mounted under a group
    # on this module so we don't double-register them at the root.
    nested: set[int] = set()
    for cmd in candidates:
        if isinstance(cmd, click.Group):
            for child in cmd.commands.values():
                nested.add(id(child))

    return [cmd for cmd in candidates if id(cmd) not in nested]


@click.command(name="help", add_help_option=False)
@click.argument("command", required=False)
@click.pass_context
def _help_cmd(ctx: click.Context, command: str | None) -> None:
    """Show help for zicato, or for one COMMAND.

    ``zicato help`` is an explicit alias for ``zicato --help`` so the
    bare verb works the way a new operator expects. ``zicato help
    evolve`` is equivalent to ``zicato evolve --help``.
    """
    # ``ctx.parent`` is the root group's context — that is the help we
    # want to render, not this command's own.
    root_ctx = ctx.parent or ctx
    root = root_ctx.command
    if command is None:
        click.echo(root.get_help(root_ctx))
        return
    if isinstance(root, click.Group):
        sub = root.get_command(root_ctx, command)
        if sub is None:
            raise click.UsageError(
                f"no such command {command!r}; run `zicato help` for the list",
                ctx=root_ctx,
            )
        sub_ctx = click.Context(sub, info_name=command, parent=root_ctx)
        click.echo(sub.get_help(sub_ctx))
        return
    click.echo(root.get_help(root_ctx))  # pragma: no cover - root is a group


def build_cli_root() -> click.Group:
    """Construct the root :class:`click.Group` for the zicato CLI.

    Iterates every module under :mod:`zicato.cli.commands`, imports it,
    and attaches each top-level :class:`click.Command` or
    :class:`click.Group` to the root. Import errors are logged and the
    module is skipped — a broken plugin does not kill the CLI.

    The root is a :class:`ZicatoGroup` so ``zicato --help`` renders the
    happy-path / advanced sections; a built-in ``help`` command is
    attached as an alias for ``--help``.
    """

    @click.group(
        name="zicato",
        cls=ZicatoGroup,
        epilog=_ROOT_EPILOG,
    )
    @click.version_option(version=_resolve_version(), prog_name="zicato")
    def root() -> None:
        """zicato — a self-improving harness for multi-agent systems.

        zicato wraps an inner multi-agent harness in an evolve loop: it
        proposes a small change, runs a scored tournament between the
        parent and the child, and keeps the winner — round after round.

        \b
        The whole tool, for most operators, is two commands:
          1. zicato init    — scaffold a ./.zicato/ workspace (once).
          2. zicato evolve  — the single entry point to the loop.

        `evolve` is self-orchestrating. It resolves the evaluation
        contract (board + proposer brief + scoring + the registered
        inner-harness identity), and if that contract has drifted from
        the current epoch it closes the old epoch and opens a fresh one
        before running. It then proposes, runs the tournament, promotes
        or rejects, and repeats — so you never run `register`,
        `propose`, `tournament`, `reindex`, or `epoch` by hand.

        Those commands still exist, grouped below as advanced /
        debugging commands, for inspecting a workspace or driving a
        single step manually.

        `evolve` also launches the live dashboard and prints its URL.

        Run `zicato evolve --help` for the full set of loop options, or
        `zicato help <command>` for any single command.
        """

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

    # The ``help`` alias is part of the root group itself, not a
    # discovered command module — attach it last so a (hypothetical)
    # discovered ``help`` module would have won the name first.
    if "help" not in seen:
        root.add_command(_help_cmd, name="help")

    return root


__all__ = ["build_cli_root", "ZicatoGroup", "HAPPY_PATH_COMMANDS"]
