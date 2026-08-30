"""``zicato init`` — bootstrap a fresh ``.zicato/`` workspace.

This is the discovered subcommand. The body delegates to
:func:`zicato.cli.init_cmd.initialize_workspace` so the same logic can
be reused outside click.
"""

from __future__ import annotations

from pathlib import Path

import click

from zicato.cli.init_cmd import initialize_workspace


@click.command(
    name="init",
    short_help="Scaffold a fresh .zicato/ workspace (run once per project).",
)
@click.option(
    "--workspace",
    default=".zicato",
    type=click.Path(file_okay=False, dir_okay=True),
    show_default=True,
    help="Workspace directory to create.",
)
@click.option(
    "--instance-id",
    "instance_id",
    default="default",
    show_default=True,
    help="Logical instance identifier recorded in config.json.",
)
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Overwrite config.json / lineage.json if the workspace already exists.",
)
@click.option(
    "--reset-lineage",
    "reset_lineage",
    is_flag=True,
    default=False,
    help="Allow --force to discard a lineage.json that already records epochs.",
)
def init_cmd(workspace: str, instance_id: str, force: bool, reset_lineage: bool) -> None:
    """Scaffold a fresh .zicato/ workspace — step one of the happy path.

    This is the first of the two commands you run. It creates the
    workspace directory if it doesn't exist, writes an empty lineage
    DAG (lineage.json: {"epochs": []}), and writes config.json
    containing {instance_id, created_at, generation_source_backend}. It also scaffolds
    the operator's live scoring.json (next to the workspace, only when
    absent) with the full recommended contract — racing field 4,
    replicates 2, the evidence gate enabled explicitly. Run it once per
    project; then point `zicato evolve` at the same workspace.

    Refuses to overwrite an existing workspace unless --force is
    passed. Force rewrites config.json and lineage.json while preserving a
    valid configured generation source backend; it does not delete epoch
    artifacts living alongside. It refuses outright when lineage.json
    already records epochs, since those decisions are not reconstructible;
    add --reset-lineage to discard them deliberately. To change only the
    generation source backend on an existing workspace, use
    `zicato repair generation-source-backend`, which merges that one key.

    \b
    Example:
      zicato init --workspace .zicato --instance-id my-project
    """
    workspace_root = Path(workspace)
    try:
        config = initialize_workspace(
            workspace_root,
            instance_id=instance_id,
            force=force,
            reset_lineage=reset_lineage,
        )
    except FileExistsError as exc:
        # Click renders UsageError nicely and exits non-zero.
        raise click.UsageError(str(exc)) from exc

    click.echo(
        f"initialized workspace at {workspace_root!s} "
        f"(instance_id={config['instance_id']!r}, created_at={config['created_at']!r})"
    )


__all__ = ["init_cmd"]
