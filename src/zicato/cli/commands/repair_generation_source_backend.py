"""``zicato repair generation-source-backend`` — set the source-backend knob.

ADVANCED / DEBUGGING — off the happy path. ``zicato init`` writes
``generation_source_backend`` into every workspace it creates, and the
generation store reads that field and nothing else. Two kinds of workspace
need this command:

* one created before the field existed, whose ``config.json`` has no key —
  every store construction refuses it;
* one whose key names the backend the source data on disk was NOT written
  by — refused for the same reason, because reading a directory-snapshot
  workspace as git (or the reverse) does not fail, it reports every
  generation as having no source tree.

The alternative remedy is ``zicato init --force``, and it is the wrong one:
force rebuilds ``config.json`` from scratch (dropping ``contract``,
``mutable_trees``, ``source_roots``, ``adk_entrypoint`` and everything else
a registration wrote) and resets ``lineage.json`` to an empty epoch list.
This command writes exactly one key and leaves every other byte of the
workspace alone.

The value is validated against the store's own
:data:`~zicato.epoch.genstore.KNOWN_GENERATION_SOURCE_BACKENDS`, and — unless
``--force`` is passed — against the source data on disk, so a typo cannot
put a workspace back into the state that sent the operator here.
"""

from __future__ import annotations

from pathlib import Path

import click

from zicato.workspace.config_io import (
    read_workspace_config,
    workspace_is_initialized,
    write_workspace_config,
)


@click.command(
    name="repair-generation-source-backend",
    short_help="Advanced: set generation_source_backend on an existing workspace.",
)
@click.option(
    "--workspace",
    default=".zicato",
    show_default=True,
    type=click.Path(file_okay=False, dir_okay=True),
    help="Path to the zicato workspace directory.",
)
@click.option(
    "--backend",
    required=True,
    type=click.Choice(["git", "directory"]),
    help="The backend that wrote this workspace's generation source trees.",
)
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Write the value even when the source data on disk names the other backend.",
)
def repair_generation_source_backend_cmd(workspace: str, backend: str, force: bool) -> None:
    """Advanced: set ``generation_source_backend`` on an existing workspace.

    Merges the one key into the existing ``config.json`` — every other key
    (contract, mutable_trees, source_roots, models, instance_id) and
    ``lineage.json`` are untouched. Use this rather than ``zicato init
    --force``, which rewrites the config and resets the lineage.

    Refuses a value the workspace's own source data contradicts unless
    ``--force`` is passed.
    """
    from zicato.epoch.genstore import (  # noqa: PLC0415
        GENERATION_SOURCE_BACKEND_KEY,
        generation_source_evidence,
    )

    workspace_root = Path(workspace)
    if not workspace_is_initialized(workspace_root):
        raise click.UsageError(
            f"workspace {workspace_root!s} is not initialized; run `zicato init` first"
        )

    evidence = generation_source_evidence(workspace_root)
    if evidence is not None and evidence != backend and not force:
        raise click.UsageError(
            f"workspace {workspace_root!s} holds {evidence!r} generation source data; "
            f"setting {GENERATION_SOURCE_BACKEND_KEY} to {backend!r} would report every "
            f"generation as having no source tree. Pass --backend {evidence} to match the "
            f"data, or --force to write {backend!r} anyway."
        )

    config = read_workspace_config(workspace_root)
    previous = config.get(GENERATION_SOURCE_BACKEND_KEY)
    config[GENERATION_SOURCE_BACKEND_KEY] = backend
    write_workspace_config(workspace_root, config)

    was = f"{previous!r}" if isinstance(previous, str) and previous else "unset"
    click.echo(
        f"set {GENERATION_SOURCE_BACKEND_KEY} = {backend!r} in {workspace_root!s} (was {was})"
    )


__all__ = ["repair_generation_source_backend_cmd"]
