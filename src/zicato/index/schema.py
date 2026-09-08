"""The supported SQLite schema for the derived analytical index.

Python readers and the supervisor share the table and column contract.
SCHEMA_VERSION identifies both the table layout and projection semantics.
It is stamped in PRAGMA user_version and mirrored in schema_meta.

An empty database receives this schema. Every incompatible database is
rebuilt from canonical workspace records by the index repair owner.
Incremental writers and read-only queries never migrate a database.

Table builds insert and upsert statements from the DDL's column lists.
The ingest_cursors table records which epoch contents have been projected;
canonical records remain the source for every analytical result."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from functools import cache
from typing import Any

#: Bump this whenever the table/column shape below changes. Stamped
#: into ``PRAGMA user_version`` and the ``schema_meta`` table by
#: :func:`apply_schema`.
SCHEMA_VERSION = 15


class IndexSchemaError(sqlite3.DatabaseError):
    """The derived database does not use the supported index schema."""


#: The canonical table DDL. Ordered so that ``CREATE TABLE`` statements
#: precede the ``CREATE INDEX`` statements that reference them. Every
#: statement applies to an empty database. Rebuild constructs a separate
#: database and publishes it only after the canonical projection succeeds.
_TABLE_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS epochs (
      epoch_id TEXT PRIMARY KEY,
      contract_hash TEXT,
      created_at TEXT,
      closed INTEGER,
      goal TEXT,
      parent_epoch_id TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS generations (
      epoch_id TEXT,
      generation_id TEXT,
      parent_generation_id TEXT,
      promoted INTEGER,
      created_at TEXT,
      round_index INTEGER,
      elo REAL,
      elo_se REAL,
      elo_games INTEGER,
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
      runtime_ms INTEGER,
      tournament_id TEXT,
      match_id TEXT
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
      loss_json TEXT,
      tournament_id TEXT,
      match_id TEXT,
      cached INTEGER,
      source_epoch TEXT,
      source_run TEXT,
      abort_cause TEXT
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
      ran_at TEXT,
      structure TEXT,
      structure_params_json TEXT,
      competitors_json TEXT,
      rounds_json TEXT,
      standings_json TEXT,
      field_status_json TEXT,
      champion_eval_mode TEXT,
      champion_run_ref TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS judge_losses (
      run_id TEXT,
      judge_name TEXT,
      weighted_loss REAL,
      raw_loss REAL,
      weight REAL,
      PRIMARY KEY (run_id, judge_name)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS reflections (
      reflection_id TEXT PRIMARY KEY,
      epoch_id TEXT,
      created_at TEXT,
      mode TEXT,
      executed INTEGER,
      noise_floor_max_abs_delta REAL,
      decision_flip_p REAL,
      n_findings INTEGER,
      n_judges INTEGER,
      verdict_counts_json TEXT
    )
    """,
    # The frontier key is (epoch, generation, status, round_retired), not
    # (epoch, generation): a generation is NOT unique per epoch. It can be
    # admitted, retired as ``promoted`` when it is crowned, then re-admitted
    # once it is dethroned — so the same id appears in BOTH ``members`` and
    # ``retired`` of the same file, and once in ``retired`` per round it left.
    # ``round_retired`` is NULL on a member row, which is already unique on
    # the three columns before it. A narrower key let the retired row (written
    # second) silently REPLACE the live member row.
    """
    CREATE TABLE IF NOT EXISTS pareto_frontier (
      epoch_id TEXT,
      generation_id TEXT,
      status TEXT,
      round_admitted INTEGER,
      round_retired INTEGER,
      retired_reason TEXT,
      champion_generation_id TEXT,
      scalar REAL,
      axis_values_json TEXT,
      beats_champion_on_json TEXT,
      PRIMARY KEY (epoch_id, generation_id, status, round_retired)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS judge_scorecards (
      reflection_id TEXT,
      judge_name TEXT,
      tp INTEGER,
      fp INTEGER,
      fn INTEGER,
      tn INTEGER,
      ambiguous INTEGER,
      precision REAL,
      recall REAL,
      f1 REAL,
      severity_accuracy REAL,
      disagreement_rate REAL,
      kappa REAL,
      exercised INTEGER,
      redundant_with_json TEXT,
      PRIMARY KEY (reflection_id, judge_name)
    )
    """,
    # The one table that is NOT a projection of a canonical file: it records
    # what the WORKSPACE looked like when each epoch was last projected, so
    # ``validate_index`` can spot a diverged epoch from four cheap directory
    # counts instead of re-deriving every row. See ANALYTICAL-INDEX.md §5.2.
    """
    CREATE TABLE IF NOT EXISTS ingest_cursors (
      epoch_id TEXT PRIMARY KEY,
      experiments_count INTEGER,
      runs_count INTEGER,
      round_dirs_count INTEGER,
      reflections_count INTEGER,
      lineage_generations_count INTEGER,
      last_ingested_at TEXT
    )
    """,
)


#: Secondary indexes. Created after the tables they reference.
_INDEX_STATEMENTS: tuple[str, ...] = (
    "CREATE INDEX IF NOT EXISTS idx_runs_gen ON runs(epoch_id, generation_id)",
    "CREATE INDEX IF NOT EXISTS idx_loss_gen ON loss_profiles(epoch_id, generation_id)",
    "CREATE INDEX IF NOT EXISTS idx_metric_run ON metric_counts(run_id)",
    "CREATE INDEX IF NOT EXISTS idx_judge_losses_run ON judge_losses(run_id)",
    "CREATE INDEX IF NOT EXISTS idx_runs_tournament ON runs(tournament_id)",
    "CREATE INDEX IF NOT EXISTS idx_loss_tournament ON loss_profiles(tournament_id)",
    "CREATE INDEX IF NOT EXISTS idx_epochs_parent ON epochs(parent_epoch_id)",
    "CREATE INDEX IF NOT EXISTS idx_reflections_epoch ON reflections(epoch_id)",
    "CREATE INDEX IF NOT EXISTS idx_judge_scorecards_refl ON judge_scorecards(reflection_id)",
    "CREATE INDEX IF NOT EXISTS idx_pareto_frontier_epoch ON pareto_frontier(epoch_id)",
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
    """Create the supported schema in an empty database.

    An existing supported database needs no schema writes. Any other populated
    database must be rebuilt from canonical records by the index repair owner.
    """
    current = read_schema_version(conn)
    if current == SCHEMA_VERSION:
        return
    if current or conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone():
        require_schema(current)
    for stmt in _TABLE_STATEMENTS:
        conn.execute(stmt)
    for stmt in _INDEX_STATEMENTS:
        conn.execute(stmt)
    conn.execute(_SCHEMA_META_DDL)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.executemany(
        "INSERT INTO schema_meta(key, value) VALUES (?, ?)",
        (
            ("schema_version", str(SCHEMA_VERSION)),
            ("description", "zicato analytical index — derived, rebuildable from .zicato/ files"),
        ),
    )
    conn.commit()


def require_schema(current: int) -> None:
    """Refuse an incompatible index before reading or incrementally writing it."""
    if current != SCHEMA_VERSION:
        raise IndexSchemaError(
            f"index database schema is {current}; this build requires {SCHEMA_VERSION}; "
            "run `zicato repair index` to rebuild it from canonical workspace records."
        )


@cache
def _columns_by_table() -> dict[str, tuple[str, ...]]:
    """Return every table's column names, in declaration order.

    Derived by applying :data:`_TABLE_STATEMENTS` to a scratch in-memory
    database and reading ``PRAGMA table_info`` back, so the answer is
    whatever SQLite itself makes of the DDL rather than a second parse of
    it. Cached: the DDL is a module constant.
    """
    conn = sqlite3.connect(":memory:")
    try:
        for statement in _TABLE_STATEMENTS:
            conn.execute(statement)
        names = [str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master")]
        return {
            name: tuple(str(row[1]) for row in conn.execute(f"PRAGMA table_info({name})"))
            for name in names
        }
    finally:
        conn.close()


def table_columns(table: str) -> tuple[str, ...]:
    """Return one table's column names, in declaration order.

    Raises :class:`KeyError` for a name the DDL does not declare.
    """
    try:
        return _columns_by_table()[table]
    except KeyError:
        raise KeyError(f"no table named {table!r} in the index schema") from None


@dataclass(frozen=True)
class Table:
    """One writer's view of a table: which columns it writes, and how.

    A writer names the table and the ways its columns depart from "write
    every column, overwrite every column on a re-ingest". The column list
    itself comes from :func:`table_columns`, so a statement built here
    cannot drift from the DDL above.

    :param name: the table the statements address.
    :param key: the table's primary key as this writer keys it. An
        :attr:`upsert` matches its ``ON CONFLICT`` clause on these columns
        and assigns none of them; an :attr:`insert_or_replace` resolves
        against the same columns without naming them.
    :param preserved_when_incoming_null: columns whose stored value
        survives a re-ingest that supplies ``NULL``. The writer cannot
        always recover these (a tournament link resolved from a file that
        has since been deleted, say), and a null from one pass must not
        erase what an earlier pass established.
    :param preserved_when_already_set: columns whose stored value
        survives a re-ingest unconditionally, even when the incoming
        value is not null. For a column that a different pass fills in
        with the richer value, this writer's value is only a fallback for
        a row that does not exist yet.
    :param set_on_insert_only: columns written when the row is created
        and left untouched on a re-ingest, with no fallback to the
        incoming value.
    :param written_elsewhere: columns another writer owns. They are
        absent from the statements entirely, so this writer neither
        creates nor clears them.
    """

    name: str
    key: tuple[str, ...] = ()
    preserved_when_incoming_null: tuple[str, ...] = ()
    preserved_when_already_set: tuple[str, ...] = ()
    set_on_insert_only: tuple[str, ...] = ()
    written_elsewhere: tuple[str, ...] = ()

    @property
    def columns(self) -> tuple[str, ...]:
        """The columns this writer supplies, in declaration order."""
        return tuple(c for c in table_columns(self.name) if c not in self.written_elsewhere)

    @property
    def insert(self) -> str:
        """``INSERT INTO <table>(<columns>) VALUES(?, …)``."""
        columns = self.columns
        placeholders = ", ".join("?" * len(columns))
        return f"INSERT INTO {self.name}({', '.join(columns)}) VALUES({placeholders})"

    @property
    def insert_or_replace(self) -> str:
        """The same insert, with a conflicting row replaced whole."""
        return self.insert.replace("INSERT INTO", "INSERT OR REPLACE INTO", 1)

    @property
    def upsert(self) -> str:
        """The insert plus the ``ON CONFLICT DO UPDATE`` clause the fields describe."""
        assignments = []
        for column in self.columns:
            if column in self.key or column in self.set_on_insert_only:
                continue
            if column in self.preserved_when_already_set:
                assignments.append(f"{column} = COALESCE({self.name}.{column}, excluded.{column})")
            elif column in self.preserved_when_incoming_null:
                assignments.append(f"{column} = COALESCE(excluded.{column}, {self.name}.{column})")
            else:
                assignments.append(f"{column} = excluded.{column}")
        clause = ", ".join(assignments)
        return f"{self.insert} ON CONFLICT({', '.join(self.key)}) DO UPDATE SET {clause}"

    def bind(self, **values: Any) -> tuple[Any, ...]:
        """Order one row's values to match :attr:`columns`.

        Raises :class:`KeyError` unless the keyword names match the columns
        one for one, which is what makes adding a column to the DDL a loud
        failure at every writer that has not been taught to supply it.
        """
        if values.keys() != set(self.columns):
            raise KeyError(f"{self.name}: values do not match the statement's columns")
        return tuple(values[column] for column in self.columns)

    def upsert_row(self, conn: sqlite3.Connection, **values: Any) -> None:
        """Insert one row, updating a conflicting one as the fields describe."""
        conn.execute(self.upsert, self.bind(**values))


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
    "IndexSchemaError",
    "Table",
    "apply_schema",
    "require_schema",
    "read_schema_version",
    "table_columns",
]
