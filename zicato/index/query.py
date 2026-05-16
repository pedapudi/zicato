"""Thin read helpers over the zicato analytical index.

This module is the read side of the index — it owns connection
construction and a small set of common ``SELECT`` helpers. It does not
write; all writes go through :mod:`zicato.index.ingest`.

Two design rules:

* **WAL-friendly opens.** :func:`open_index` opens the database in WAL
  journal mode so a reader (R9-2's analytics surface, the Rust
  supervisor) does not block the orchestrator's live dual-writes and
  vice versa. The connection's ``row_factory`` is :class:`sqlite3.Row`
  so callers get name-addressable rows.
* **Tolerate a missing database.** Every helper here is given a
  ``db_path`` and is expected to be called against a workspace that may
  never have been indexed. :func:`open_index` raises a clear
  :class:`IndexNotBuiltError` (whose message points at ``zicato
  reindex``); the convenience selectors below catch that and return an
  empty result so a caller building a dashboard does not have to
  special-case the first run.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from zicato.index.schema import read_schema_version


class IndexNotBuiltError(FileNotFoundError):
    """Raised when an index database is requested but does not exist.

    A subclass of :class:`FileNotFoundError` so existing
    ``except FileNotFoundError`` handlers keep working, but with a
    message that explicitly tells the operator to run ``zicato
    reindex``.
    """


def open_index(db_path: Path) -> sqlite3.Connection:
    """Open the index database for reading.

    The connection is configured for the index's concurrent-read
    posture:

    * ``row_factory = sqlite3.Row`` — callers index columns by name.
    * WAL journal mode — readers never block the orchestrator's
      dual-writes. ``PRAGMA journal_mode=WAL`` is a no-op when the file
      was already created in WAL mode (the canonical case), and harmless
      otherwise.
    * ``PRAGMA busy_timeout`` — a short wait so a read that races a
      writer's commit retries instead of raising ``database is locked``.

    Raises
    ------
    IndexNotBuiltError
        If ``db_path`` does not exist. The message suggests
        ``zicato reindex``.

    Notes
    -----
    The connection is *read-oriented* but not hard read-only — SQLite's
    URI ``mode=ro`` would refuse to even create the WAL sidecar files,
    which trips up some environments. We instead open normally and
    simply never issue writes from this module.
    """
    if not db_path.exists():
        raise IndexNotBuiltError(
            f"zicato index database not found at {db_path}; "
            "run `zicato reindex` to build it from the workspace files"
        )
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def index_schema_version(db_path: Path) -> int | None:
    """Return the schema version stamped in the index, or ``None``.

    ``None`` means the database file does not exist. A returned integer
    is the value of ``PRAGMA user_version`` — a caller can compare it to
    :data:`zicato.index.schema.SCHEMA_VERSION` to detect a stale index.
    """
    if not db_path.exists():
        return None
    conn = sqlite3.connect(str(db_path))
    try:
        return read_schema_version(conn)
    finally:
        conn.close()


def _select(db_path: Path, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
    """Run a read query, returning rows; ``[]`` when the index is missing.

    Centralises the "tolerate a missing database" rule for the
    convenience selectors below. A missing file yields an empty list
    rather than an exception so dashboard-style callers can render an
    empty state on a never-indexed workspace.
    """
    try:
        conn = open_index(db_path)
    except IndexNotBuiltError:
        return []
    try:
        return list(conn.execute(sql, tuple(params)).fetchall())
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Common selectors
# ---------------------------------------------------------------------------


def all_epochs(db_path: Path) -> list[sqlite3.Row]:
    """Return every indexed epoch, oldest first."""
    return _select(
        db_path,
        "SELECT epoch_id, contract_hash, created_at, closed "
        "FROM epochs ORDER BY created_at, epoch_id",
    )


def generations_for_epoch(db_path: Path, epoch_id: str) -> list[sqlite3.Row]:
    """Return every generation under ``epoch_id``, oldest first."""
    return _select(
        db_path,
        "SELECT epoch_id, generation_id, parent_generation_id, promoted, created_at "
        "FROM generations WHERE epoch_id = ? ORDER BY created_at, generation_id",
        (epoch_id,),
    )


def runs_for_generation(db_path: Path, epoch_id: str, generation_id: str) -> list[sqlite3.Row]:
    """Return every run row under one generation, ordered by entry id."""
    return _select(
        db_path,
        "SELECT run_id, epoch_id, generation_id, entry_id, started_at, ended_at, "
        "aborted, runtime_ms FROM runs "
        "WHERE epoch_id = ? AND generation_id = ? ORDER BY entry_id, run_id",
        (epoch_id, generation_id),
    )


def loss_profiles_for_generation(
    db_path: Path, epoch_id: str, generation_id: str
) -> list[sqlite3.Row]:
    """Return every loss-profile row under one generation."""
    return _select(
        db_path,
        "SELECT run_id, epoch_id, generation_id, entry_id, drift_loss, pass_fail, "
        "runtime_ms, wall_clock_budget_exceeded, loss_json FROM loss_profiles "
        "WHERE epoch_id = ? AND generation_id = ? ORDER BY entry_id, run_id",
        (epoch_id, generation_id),
    )


def metric_counts_for_run(db_path: Path, run_id: str) -> list[sqlite3.Row]:
    """Return every metric-count row recorded for one run."""
    return _select(
        db_path,
        "SELECT run_id, namespace, name, severity, count FROM metric_counts "
        "WHERE run_id = ? ORDER BY name, severity",
        (run_id,),
    )


def experiments_for_epoch(db_path: Path, epoch_id: str) -> list[sqlite3.Row]:
    """Return every experiment row under ``epoch_id``."""
    return _select(
        db_path,
        "SELECT epoch_id, generation_id, hypothesis_core_idea, hypothesis_why, "
        "hypothesis_json, tournament_decision, rejection_reason, scalar_score_delta, "
        "drift_loss_delta, pass_rate_delta, outcome_json FROM experiments "
        "WHERE epoch_id = ? ORDER BY generation_id",
        (epoch_id,),
    )


def tournaments_for_epoch(db_path: Path, epoch_id: str) -> list[sqlite3.Row]:
    """Return every resolved tournament row under ``epoch_id``."""
    return _select(
        db_path,
        "SELECT tournament_id, epoch_id, parent_generation_id, child_generation_id, "
        "decision, parent_scalar, child_scalar, delta_scalar, rejection_reason, ran_at "
        "FROM tournaments WHERE epoch_id = ? ORDER BY ran_at, tournament_id",
        (epoch_id,),
    )


def index_counts(db_path: Path) -> dict[str, int]:
    """Return a per-table row-count summary of the index.

    Returns a dict keyed by table name. When the index does not exist
    every count is ``0`` — the helper tolerates a missing database the
    same way the selectors do.
    """
    tables = (
        "epochs",
        "generations",
        "experiments",
        "patches",
        "runs",
        "loss_profiles",
        "metric_counts",
        "tournaments",
    )
    out: dict[str, int] = dict.fromkeys(tables, 0)
    try:
        conn = open_index(db_path)
    except IndexNotBuiltError:
        return out
    try:
        for table in tables:
            row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
            out[table] = int(row[0]) if row is not None else 0
    finally:
        conn.close()
    return out


__all__ = [
    "IndexNotBuiltError",
    "open_index",
    "index_schema_version",
    "all_epochs",
    "generations_for_epoch",
    "runs_for_generation",
    "loss_profiles_for_generation",
    "metric_counts_for_run",
    "experiments_for_epoch",
    "tournaments_for_epoch",
    "index_counts",
]
