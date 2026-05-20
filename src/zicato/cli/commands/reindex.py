"""``zicato reindex`` — full rebuild of the SQLite analytical index.

ADVANCED / DEBUGGING — off the happy path. ``zicato evolve`` keeps the
index up to date as it runs. Run ``zicato reindex`` by hand only to
rebuild a stale or drifted index.

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

from zicato.index.ingest import backfill_generations, rebuild_index
from zicato.index.query import index_counts


@click.command(
    name="reindex",
    short_help="Advanced: rebuild the SQLite analytical index from workspace files.",
)
@click.option(
    "--workspace",
    default=".zicato",
    show_default=True,
    help="Path to the zicato workspace directory.",
)
def reindex_cmd(workspace: str) -> None:
    """Advanced: rebuild the SQLite analytical index from workspace files.

    Off the happy path — `zicato evolve` keeps the index current.
    Drops index.db and re-derives every row from the canonical files
    under the workspace. Prints a summary of how many epochs,
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


@click.command(
    name="reindex-generations",
    short_help=(
        "Advanced: reconcile the index `generations` table against disk " "(parent + promoted)."
    ),
)
@click.option(
    "--workspace",
    default=".zicato",
    show_default=True,
    help="Path to the zicato workspace directory.",
)
def reindex_generations_cmd(workspace: str) -> None:
    """Advanced: reconcile only the `generations` table from disk.

    Off the happy path. Targeted repair for workspaces whose
    `generations` rows were written by a buggy live dual-write
    (parent_generation_id NULL, promoted clamped to 0 on every row
    except the seed). Walks lineage.json + every experiment.json and
    rewrites only the parent_generation_id and promoted flag of each
    `generations` row. The rest of the index is left alone — use
    `zicato reindex` for a full rebuild.

    Idempotent. Read-only against workspace files.
    """
    ws = Path(workspace).resolve()
    result = backfill_generations(ws)
    click.echo(
        f"Reconciled generations table at {ws / 'index.db'}: "
        f"{result['updated']} updated of {result['scanned']} scanned."
    )


__all__ = ["reindex_cmd", "reindex_generations_cmd"]
