"""The zicato SQLite analytical index.

The index (``.zicato/index.db``) is a *derived*, queryable view of
cross-run data. The workspace files under ``.zicato/`` remain the
canonical source of truth — the index carries no information that is
not already on disk, and :func:`zicato.index.ingest.rebuild_index`
reconstructs it from those files at any time.

Module layout::

    zicato/index/
      schema.py    # the SQLite DDL + schema_version contract
      ingest.py    # build / incrementally update index.db from files
      query.py     # thin read helpers (connection, common selects)

Why a derived index?
--------------------
Cross-run analysis (R9-2's analytics surface, the Rust supervisor's
status queries) wants to ask questions like "every promoted generation
across all epochs" or "the loss profile of every aborted run" without
re-walking the whole workspace tree on each call. SQLite gives those
consumers an indexed, transactional read surface for free, using only
the standard library. The schema in :mod:`zicato.index.schema` is a
shared contract — sibling workstreams build against it directly.

The index is opened in WAL mode so the Rust supervisor and R9-2 can
read it concurrently while the orchestrator dual-writes new rows via
:func:`zicato.index.ingest.ingest_run` /
:func:`zicato.index.ingest.ingest_experiment`.

Self-healing
------------
Routine reindexing is automatic. :func:`zicato.index.ingest.ensure_index`
builds an absent or wrong-schema index (atomically — a failed build leaves
the existing one untouched), and
:func:`zicato.index.ingest.heal_index` re-projects only the epochs whose
persisted cursors disagree with the workspace. Both run at ``evolve`` start;
the dashboard runs only the former. ``zicato reindex`` remains the explicit
forensic rebuild. See ``docs/design/ANALYTICAL-INDEX.md`` §5.
"""

from __future__ import annotations

from zicato.index.ingest import (
    backfill_generations,
    ensure_index,
    heal_index,
    ingest_experiment,
    ingest_field_tournament,
    ingest_run,
    rebuild_index,
    validate_index,
)
from zicato.index.query import open_index
from zicato.index.schema import SCHEMA_VERSION

__all__ = [
    "SCHEMA_VERSION",
    "rebuild_index",
    "ensure_index",
    "validate_index",
    "heal_index",
    "ingest_run",
    "ingest_experiment",
    "ingest_field_tournament",
    "backfill_generations",
    "open_index",
]
