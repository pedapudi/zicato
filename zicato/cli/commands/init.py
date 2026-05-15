"""``zicato init`` — bootstrap a fresh ``.zicato/`` workspace.

This is the discovered subcommand. The body delegates to
:func:`zicato.cli.init_cmd.initialize_workspace` so the same logic can
be reused outside click.
"""

from __future__ import annotations

from pathlib import Path

import click

from zicato.cli.init_cmd import initialize_workspace


@click.command(name="init")
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
def init_cmd(workspace: str, instance_id: str, force: bool) -> None:
    """Initialize a new ``.zicato/`` workspace.

    Creates the directory if it doesn't exist, writes an empty
    ``lineage.json`` (``{"nodes": [], "edges": []}``), and writes
    ``config.json`` containing ``{instance_id, created_at}``. Refuses to
    overwrite an existing workspace unless ``--force`` is passed.
    """
    workspace_root = Path(workspace)
    try:
        config = initialize_workspace(
            workspace_root,
            instance_id=instance_id,
            force=force,
        )
    except FileExistsError as exc:
        # Click renders UsageError nicely and exits non-zero.
        raise click.UsageError(str(exc)) from exc

    click.echo(
        f"initialized workspace at {workspace_root!s} "
        f"(instance_id={config['instance_id']!r}, created_at={config['created_at']!r})"
    )


__all__ = ["init_cmd"]
