"""Rebuild the derived analytical index from canonical workspace records.

Routine invocation startup repairs incompatible indexes and changed epochs.
The explicit command also reconstructs an index after manual file edits or
when checking that its contents equal the canonical records."""

from __future__ import annotations

from pathlib import Path

import click

from zicato.evolve.settlement_recovery import acknowledge_repaired_settlement_indexes
from zicato.index.ingest import (
    backfill_generations,
    rebuild_index,
)
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
    """Rebuild the analytical index from canonical workspace files.

    A private scratch database is populated before publication. A failed build
    preserves the existing index. The command reports counts from the result."""
    ws = Path(workspace).resolve()
    db_path = rebuild_index(ws)
    acknowledge_repaired_settlement_indexes(ws)
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
    short_help="Advanced: reconcile indexed generation facts with lineage.",
)
@click.option(
    "--workspace",
    default=".zicato",
    show_default=True,
    help="Path to the zicato workspace directory.",
)
def reindex_generations_cmd(workspace: str) -> None:
    """Advanced: reconcile only the `generations` table from disk.

    Canonical lineage supplies each generation's parent, promotion state,
    creation timestamp, and birth round. The workspace writer lease covers
    the repair. Ratings and other index tables remain unchanged; use
    `zicato repair index` for a full rebuild.

    Repeating the repair makes no changes. Workspace files are read only.
    """
    ws = Path(workspace).resolve()
    result = backfill_generations(ws)
    click.echo(
        f"Reconciled generations table at {ws / 'index.db'}: "
        f"{result['updated']} updated of {result['scanned']} scanned."
    )


__all__ = ["reindex_cmd", "reindex_generations_cmd"]
