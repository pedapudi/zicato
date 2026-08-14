"""``zicato repair index`` — full rebuild of the SQLite analytical index.

ADVANCED / FORENSIC — off the happy path, and no longer part of routine
operation. Reindexing is automatic: ``zicato evolve`` builds an absent or
wrong-schema index and re-projects any diverged epoch at its own start, and
the dashboard builds an absent one when it boots
(``docs/design/ANALYTICAL-INDEX.md`` §5).

The index (``.zicato/index.db``) is derived data: a queryable projection of
the canonical workspace files. ``zicato repair index`` re-derives every row by
walking every epoch / generation / run under the workspace, into a scratch
file that is renamed into place on success — so a rebuild that FAILS leaves
the existing index untouched rather than destroying it.

What still calls for running it by hand (§5.4):

* **Downgrade recovery** — an index written by a NEWER zicato raises
  ``IndexSchemaNewerError`` and is never auto-deleted; the operator deletes
  it and rebuilds deliberately.
* **Post-surgery rebuilds** — after hand-editing a value INSIDE a canonical
  file without changing any file count, which the cheap per-epoch cursors
  cannot see.
* **Determinism assertion** — proving the index equals a pure re-projection
  of the files (what the REINDEX-DUMP parity gate does) needs the
  from-scratch path by definition.
* **Anything broader than one epoch** — the automatic heal's unit is the
  epoch.

The command is thin — it resolves the workspace path, calls
:func:`zicato.index.ingest.rebuild_index`, and prints a row-count
summary so the operator can sanity-check what was indexed.
"""

from __future__ import annotations

from pathlib import Path

import click

from zicato.index.ingest import (
    backfill_generations,
    backfill_tournament_fk,
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
    """Advanced: rebuild the SQLite analytical index from workspace files.

    Off the happy path. Routine reindexing is AUTOMATIC — `zicato evolve`
    builds an absent or wrong-schema index and re-projects any diverged
    epoch at its own start, and the dashboard builds an absent one when it
    boots. Reach for this command for downgrade recovery, after hand-editing
    a value inside a canonical file, or to assert the index is a pure
    re-projection of the files.

    Re-derives every row from the canonical files under the workspace, into
    a scratch file renamed into place on success — a failed rebuild leaves
    the existing index untouched. Prints a summary of how many epochs,
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
    `zicato repair index` for a full rebuild.

    Idempotent. Read-only against workspace files.
    """
    ws = Path(workspace).resolve()
    result = backfill_generations(ws)
    click.echo(
        f"Reconciled generations table at {ws / 'index.db'}: "
        f"{result['updated']} updated of {result['scanned']} scanned."
    )


@click.command(
    name="repair-tournament-fk",
    short_help=(
        "Advanced: backfill tournament_id on runs / loss_profiles + parent_epoch_id on epochs."
    ),
)
@click.option(
    "--workspace",
    default=".zicato",
    show_default=True,
    help="Path to the zicato workspace directory.",
)
def repair_tournament_fk_cmd(workspace: str) -> None:
    """Advanced: backfill schema-v2 cross-cutting FKs on an existing index.

    Off the happy path. Schema v2 added a ``tournament_id`` column to
    ``runs`` and ``loss_profiles`` plus a ``parent_epoch_id`` column on
    ``epochs``. New writes populate them automatically; this command
    repairs rows that were ingested under the v1 schema by walking
    every epoch in ``lineage.json`` and every ``experiment.json`` on
    disk and rewriting the FK columns from those sources.

    Idempotent: cells that already carry the correct value are skipped,
    so a re-run against a healthy index is a no-op. Read-only against
    the workspace files — only the SQLite index is mutated.

    Folded into one command because the two backfills share the same
    walk and operators always want both columns repaired together.
    """
    ws = Path(workspace).resolve()
    result = backfill_tournament_fk(ws)
    click.echo(
        f"Backfilled tournament FK at {ws / 'index.db'}: "
        f"{result['runs_updated']} runs, "
        f"{result['loss_updated']} loss profiles, "
        f"{result['epochs_updated']} epochs updated "
        f"of {result['scanned']} generations scanned."
    )


__all__ = ["reindex_cmd", "reindex_generations_cmd", "repair_tournament_fk_cmd"]
