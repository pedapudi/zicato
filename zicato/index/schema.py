"""The SQLite DDL for the zicato analytical index.

This module is a **shared contract**. R9-2's analytics surface and the
Rust supervisor both query ``.zicato/index.db`` directly, so the table
and column shapes here must not change without coordinating with those
consumers. The DDL is kept as plain SQL strings (rather than an ORM
schema) precisely so siblings in other languages can mirror it
verbatim.

Schema versioning
-----------------
:data:`SCHEMA_VERSION` is stamped into the database two ways so a
future migration is detectable from either a SQL client or the Python
helpers:

* The SQLite ``user_version`` pragma — readable with
  ``PRAGMA user_version`` from any client, no table join needed.
* A one-row ``schema_meta`` table — carries the version plus a
  human-readable note, queryable with a normal ``SELECT``.

:func:`apply_schema` writes both. :func:`read_schema_version` reads the
pragma (the authoritative source). A consumer that opens a database
whose ``user_version`` does not equal :data:`SCHEMA_VERSION` should
treat the index as stale and ask the operator to run ``zicato
reindex``.

The tables are all derived from canonical workspace files:

* ``epochs`` / ``generations`` — from ``lineage.json`` + per-epoch
  ``config.json``.
* ``experiments`` / ``patches`` — from each generation's
  ``experiment.json`` (+ ``patches/*.json``).
* ``runs`` / ``loss_profiles`` / ``metric_counts`` — from each run's
  ``loss.json`` and ``events.jsonl``.
* ``tournaments`` — from the resolved outcome on each experiment.
"""

from __future__ import annotations

import sqlite3

#: Bump this whenever the table/column shape below changes. Stamped
#: into ``PRAGMA user_version`` and the ``schema_meta`` table by
#: :func:`apply_schema`.
SCHEMA_VERSION = 1


#: The canonical table DDL. Ordered so that ``CREATE TABLE`` statements
#: precede the ``CREATE INDEX`` statements that reference them. Every
#: statement is ``IF NOT EXISTS`` so :func:`apply_schema` is safe to run
#: against a partially-built database, though the canonical build path
#: (:func:`zicato.index.ingest.rebuild_index`) drops the file first.
_TABLE_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS epochs (
      epoch_id TEXT PRIMARY KEY,
      contract_hash TEXT,
      created_at TEXT,
      closed INTEGER
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS generations (
      epoch_id TEXT,
      generation_id TEXT,
      parent_generation_id TEXT,
      promoted INTEGER,
      created_at TEXT,
      PRIMARY KEY (epoch_id, generation_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS experiments (
      epoch_id TEXT,
      generation_id TEXT,
      hypothesis_core_idea TEXT,
      hypothesis_why TEXT,
      hypothesis_json TEXT,
      tournament_decision TEXT,
      rejection_reason TEXT,
      scalar_score_delta REAL,
      drift_loss_delta REAL,
      pass_rate_delta REAL,
      outcome_json TEXT,
      PRIMARY KEY (epoch_id, generation_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS patches (
      patch_id TEXT PRIMARY KEY,
      epoch_id TEXT,
      generation_id TEXT,
      mutation_id TEXT,
      op TEXT,
      rationale TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS runs (
      run_id TEXT PRIMARY KEY,
      epoch_id TEXT,
      generation_id TEXT,
      entry_id TEXT,
      started_at TEXT,
      ended_at TEXT,
      aborted INTEGER,
      runtime_ms INTEGER
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS loss_profiles (
      run_id TEXT PRIMARY KEY,
      epoch_id TEXT,
      generation_id TEXT,
      entry_id TEXT,
      drift_loss REAL,
      pass_fail INTEGER,
      runtime_ms INTEGER,
      wall_clock_budget_exceeded INTEGER,
      loss_json TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS metric_counts (
      run_id TEXT,
      namespace TEXT,
      name TEXT,
      severity TEXT,
      count REAL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS tournaments (
      tournament_id TEXT PRIMARY KEY,
      epoch_id TEXT,
      parent_generation_id TEXT,
      child_generation_id TEXT,
      decision TEXT,
      parent_scalar REAL,
      child_scalar REAL,
      delta_scalar REAL,
      rejection_reason TEXT,
      ran_at TEXT
    )
    """,
)


#: Secondary indexes. Created after the tables they reference.
_INDEX_STATEMENTS: tuple[str, ...] = (
    "CREATE INDEX IF NOT EXISTS idx_runs_gen ON runs(epoch_id, generation_id)",
    "CREATE INDEX IF NOT EXISTS idx_loss_gen ON loss_profiles(epoch_id, generation_id)",
    "CREATE INDEX IF NOT EXISTS idx_metric_run ON metric_counts(run_id)",
)


#: The ``schema_meta`` table is not part of the cross-language data
#: contract — it is a zicato-side convenience mirror of the pragma so a
#: plain ``SELECT`` can recover the version + a human note. Consumers
#: should treat ``PRAGMA user_version`` as authoritative.
_SCHEMA_META_DDL = """
CREATE TABLE IF NOT EXISTS schema_meta (
  key TEXT PRIMARY KEY,
  value TEXT
)
"""


def apply_schema(conn: sqlite3.Connection) -> None:
    """Create every table + index + stamp the schema version.

    Idempotent — every statement is ``IF NOT EXISTS`` and the
    ``schema_meta`` row is upserted, so running this against an
    already-initialised database is a no-op. The canonical build path
    drops the database file first (see
    :func:`zicato.index.ingest.rebuild_index`), but ``ingest_run`` /
    ``ingest_experiment`` call this on an existing file to be safe.

    Both the ``user_version`` pragma and the ``schema_meta`` table are
    stamped with :data:`SCHEMA_VERSION`.
    """
    for stmt in _TABLE_STATEMENTS:
        conn.execute(stmt)
    for stmt in _INDEX_STATEMENTS:
        conn.execute(stmt)
    conn.execute(_SCHEMA_META_DDL)
    # The pragma cannot take a bound parameter, so the literal is
    # interpolated — SCHEMA_VERSION is a module-level int constant, never
    # operator input, so this is not an injection surface.
    conn.execute(f"PRAGMA user_version = {int(SCHEMA_VERSION)}")
    conn.execute(
        "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (str(SCHEMA_VERSION),),
    )
    conn.execute(
        "INSERT INTO schema_meta(key, value) VALUES('description', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        ("zicato analytical index — derived, rebuildable from .zicato/ files",),
    )
    conn.commit()


def read_schema_version(conn: sqlite3.Connection) -> int:
    """Return the database's stamped schema version.

    Reads ``PRAGMA user_version`` — the authoritative source. A value
    of ``0`` means the schema was never applied (a fresh / empty SQLite
    file defaults ``user_version`` to ``0``, and :data:`SCHEMA_VERSION`
    starts at ``1``).
    """
    row = conn.execute("PRAGMA user_version").fetchone()
    if row is None:
        return 0
    return int(row[0])


__all__ = [
    "SCHEMA_VERSION",
    "apply_schema",
    "read_schema_version",
]
