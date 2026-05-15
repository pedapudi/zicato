"""``zicato register`` — record adapter entrypoint and mutable trees.

After ``zicato init`` creates the workspace, ``zicato register`` is the
one-shot step that tells zicato *which* agent to run and *which* source
trees the proposer is allowed to mutate. The entrypoint follows the
common Python convention ``module.path:symbol``; mutable trees are
filesystem roots (one or many, passed as repeated ``--mutable-tree``
flags).

The values are persisted to ``{workspace}/config.json`` so subsequent
subcommands can read them back without re-asking the operator.
"""

from __future__ import annotations

from pathlib import Path

import click

from zicato.cli.common import (
    read_workspace_config,
    workspace_is_initialized,
    write_workspace_config,
)


def _validate_entrypoint(entrypoint: str) -> None:
    """Ensure ``entrypoint`` looks like ``module.path:symbol``.

    This is a syntactic check only — we don't import the module here so
    ``register`` works in environments where the agent's runtime deps
    aren't installed yet.
    """
    if ":" not in entrypoint:
        raise click.BadParameter(
            f"entrypoint {entrypoint!r} must be of the form 'module.path:symbol'",
            param_hint="--adk",
        )
    module_part, _, symbol_part = entrypoint.partition(":")
    if not module_part or not symbol_part:
        raise click.BadParameter(
            f"entrypoint {entrypoint!r} must have both module and symbol",
            param_hint="--adk",
        )


@click.command(name="register")
@click.option(
    "--workspace",
    default=".zicato",
    type=click.Path(file_okay=False, dir_okay=True),
    show_default=True,
    help="Workspace directory to update.",
)
@click.option(
    "--adk",
    "entrypoint",
    required=True,
    help="Adapter entrypoint in 'module.path:agent_symbol' form.",
)
@click.option(
    "--mutable-tree",
    "mutable_trees",
    multiple=True,
    type=click.Path(),
    help="Source root the proposer is allowed to mutate (repeatable).",
)
def register_cmd(
    workspace: str,
    entrypoint: str,
    mutable_trees: tuple[str, ...],
) -> None:
    """Record the adapter entrypoint and mutable trees in the workspace.

    Merges into the existing ``config.json`` rather than replacing it,
    so any keys :func:`zicato.cli.init_cmd.initialize_workspace` wrote
    (``instance_id``, ``created_at``) are preserved.
    """
    _validate_entrypoint(entrypoint)
    workspace_root = Path(workspace)
    if not workspace_is_initialized(workspace_root):
        raise click.UsageError(
            f"workspace {workspace_root!s} is not initialized; run `zicato init` first"
        )

    config = read_workspace_config(workspace_root)
    config["adk_entrypoint"] = entrypoint
    config["mutable_trees"] = list(mutable_trees)
    write_workspace_config(workspace_root, config)

    click.echo(
        f"registered entrypoint {entrypoint!r} with {len(mutable_trees)} "
        f"mutable tree(s) in {workspace_root!s}"
    )


__all__ = ["register_cmd"]
