"""``zicato init`` — bootstrap a fresh ``.zicato/`` workspace.

This is the discovered subcommand. The body delegates to
:func:`zicato.cli.init_cmd.initialize_workspace` so the same logic can
be reused outside click.
"""

from __future__ import annotations

from pathlib import Path

import click

from zicato.cli.example_scaffold import example_paths
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
@click.option(
    "--example",
    is_flag=True,
    default=False,
    help=(
        "Also scaffold a complete runnable example project next to the workspace "
        "and wire config.json to it, so `zicato evolve` runs a first round with "
        "nothing hand-authored. Existing files are left alone."
    ),
)
def init_cmd(
    workspace: str, instance_id: str, force: bool, reset_lineage: bool, example: bool
) -> None:
    """Scaffold a fresh .zicato/ workspace — step one of the happy path.

    This is the first of the two commands you run. It creates the
    workspace directory if it doesn't exist, writes an empty lineage
    DAG (lineage.json: {"epochs": []}), and writes config.json
    containing {instance_id, created_at, generation_source_backend}. It also scaffolds
    the operator's live scoring.json (next to the workspace, only when
    absent) with the recommended contract: a racing tournament that
    fields four challengers per round, two replicates per duel, and the
    evidence gate enabled with its replicate budget stated. Run it once
    per project; then point `zicato evolve` at the same workspace.

    Refuses to overwrite an existing workspace unless --force is
    passed. Force rewrites config.json and lineage.json while preserving a
    valid configured generation source backend; it does not delete epoch
    artifacts living alongside. It refuses outright when lineage.json
    already records epochs, since those decisions are not reconstructible;
    add --reset-lineage to discard them deliberately. To change only the
    generation source backend on an existing workspace, use
    `zicato repair generation-source-backend`, which merges that one key.

    --example also writes a complete project next to the workspace: a
    system under test carrying one mutable span, an adapter that runs it,
    predicates that grade it, a proposer that edits it, callables for the
    two model roles, a four-entry board, a proposer brief, and a scoring
    contract. config.json is wired to all of it, so the next command can
    be `zicato evolve`. It uses no model and needs no endpoint. Put the
    project directory on PYTHONPATH first — the dotted paths in
    config.json resolve there, in this process and in every tournament
    worker.

    \b
    Example:
      zicato init --workspace .zicato --instance-id my-project
      zicato init --example
    """
    workspace_root = Path(workspace)
    project_root = workspace_root.resolve().parent
    # Recorded before the scaffold runs: afterwards every path exists, and
    # the report would claim to have written files it left alone.
    already_there = (
        [path for path in example_paths(project_root) if path.exists()] if example else []
    )
    try:
        config = initialize_workspace(
            workspace_root,
            instance_id=instance_id,
            force=force,
            reset_lineage=reset_lineage,
            example=example,
        )
    except FileExistsError as exc:
        # Click renders UsageError nicely and exits non-zero.
        raise click.UsageError(str(exc)) from exc

    click.echo(
        f"initialized workspace at {workspace_root!s} "
        f"(instance_id={config['instance_id']!r}, created_at={config['created_at']!r})"
    )
    if not example:
        return
    for path in example_paths(project_root):
        state = "already present, left alone" if path in already_there else "written"
        click.echo(f"  {path.name}: {state}")
    click.echo(
        f"\nexport PYTHONPATH={project_root} to make the example importable, "
        f"then run `zicato evolve --workspace {workspace_root!s} --rounds 3`."
    )


__all__ = ["init_cmd"]
