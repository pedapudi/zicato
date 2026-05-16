"""``zicato reindex`` — full rebuild of the SQLite analytical index.

The index (``.zicato/index.db``) is derived data: a queryable
projection of the canonical workspace files. ``zicato reindex`` drops
it and rebuilds it from scratch by walking every epoch / generation /
run under the workspace.

Operators run this when:

* they want to query the index for the first time on an existing
  workspace,
* the index drifted from the files (a manual file edit, a crash mid-
  dual-write), or
* :data:`zicato.index.schema.SCHEMA_VERSION` was bumped and the old
  database is stale.

The command is thin — it resolves the workspace path, calls
:func:`zicato.index.ingest.rebuild_index`, and prints a row-count
summary so the operator can sanity-check what was indexed.
"""

from __future__ import annotations

from pathlib import Path

import click

from zicato.index.ingest import rebuild_index
from zicato.index.query import index_counts


@click.command(name="reindex")
@click.option(
    "--workspace",
    default=".zicato",
    show_default=True,
    help="Path to the zicato workspace directory.",
)
def reindex_cmd(workspace: str) -> None:
    """Rebuild the SQLite analytical index from the workspace files.

    Drops ``index.db`` and re-derives every row from the canonical
    files under the workspace. Prints a summary of how many epochs,
    generations, and runs were indexed.
    """
    ws = Path(workspace).resolve()
    db_path = rebuild_index(ws)
    counts = index_counts(db_path)
    click.echo(f"Rebuilt index at {db_path}.")
    click.echo(
        f"  {counts['epochs']} epochs, "
        f"{counts['generations']} generations, "
        f"{counts['experiments']} experiments indexed."
    )
    click.echo(
        f"  {counts['runs']} runs, "
        f"{counts['loss_profiles']} loss profiles, "
        f"{counts['metric_counts']} metric counts, "
        f"{counts['tournaments']} tournaments indexed."
    )


__all__ = ["reindex_cmd"]
