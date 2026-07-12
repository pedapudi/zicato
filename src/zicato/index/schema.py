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
SCHEMA_VERSION = 12


class IndexSchemaNewerError(RuntimeError):
    """The index database was written by a NEWER zicato than this build.

    Raised by :func:`apply_schema` when ``PRAGMA user_version`` exceeds
    :data:`SCHEMA_VERSION`: an older writer must never silently re-stamp
    a newer database DOWN (the newer schema may carry columns/semantics
    this build does not understand, and the down-stamp would corrupt the
    version signal for the newer build). The index is derived and always
    rebuildable, so the recovery is cheap — upgrade zicato, or delete the
    database and run ``zicato reindex``.
    """


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


#: Columns added in v2 that older v1 databases will be missing. Each
#: entry is ``(table, column, ddl_type)``; :func:`_migrate_inplace`
#: adds whichever of these are absent so an existing v1 file becomes
#: queryable under v2 without a full rebuild. The full rebuild path
#: (``zicato reindex``) drops the file and re-applies the v2 CREATE
#: TABLE statements above, so this migration only matters on
#: incremental opens against a pre-existing file.
_V2_ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("epochs", "goal", "TEXT"),
    ("epochs", "parent_epoch_id", "TEXT"),
    ("runs", "tournament_id", "TEXT"),
    ("loss_profiles", "tournament_id", "TEXT"),
)


#: Columns added in v3 (the configurable-tournament-structure feature).
#: Same incremental-open ALTER pattern as :data:`_V2_ADDED_COLUMNS`: a
#: pre-existing v2 database gains these as ``NULL`` columns on open; a
#: full ``zicato reindex`` drops the file and re-applies the v3 CREATE
#: TABLE statement above, then re-derives the columns
#: (``"gauntlet"`` for runs that predate the feature).
_V3_ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("tournaments", "structure", "TEXT"),
    ("tournaments", "structure_params_json", "TEXT"),
    ("tournaments", "competitors_json", "TEXT"),
    ("tournaments", "rounds_json", "TEXT"),
    ("tournaments", "standings_json", "TEXT"),
)


#: Columns added in v4 (per-board-run tournament provenance). A run
#: carries the ``match_id`` of the matchup it executed within (e.g.
#: ``"rung0_m2"``, ``"racing-final"``), so the dashboard can relate a
#: board run to its rung/matchup. Same incremental-open ALTER pattern as
#: the earlier waves: a pre-existing v3 database gains these as ``NULL``
#: columns on open (legacy runs stay untagged — the field is simply
#: absent), and a full ``zicato reindex`` re-derives what it can from
#: each run's ``loss.json`` (which now carries ``match_id`` for new runs).
_V4_ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("runs", "match_id", "TEXT"),
    ("loss_profiles", "match_id", "TEXT"),
)


#: Columns added in v5 (the live proposing-step tracker). The
#: per-challenger field-status records — applied vs rejected + reason —
#: are persisted alongside the settled bracket so a completed epoch's
#: candidate-generation step survives for post-hoc viewing (the same
#: incremental-open ALTER pattern as the earlier waves; legacy rows gain
#: it as ``NULL``, a full ``zicato reindex`` re-derives what it can).
_V5_ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("tournaments", "field_status_json", "TEXT"),
)


#: Columns added in v6 (champion self-containment / carried-over
#: provenance). A materialised champion's per-board ``loss_profiles`` row
#: carries ``cached`` (1 when the result was carried forward from a prior
#: epoch rather than run live this epoch) plus ``source_epoch`` /
#: ``source_run`` naming where the live evaluation happened, so a reader
#: can show the champion as scored-but-cached and never double-count it as
#: a fresh evaluation. Same incremental-open ALTER pattern as the earlier
#: waves: a pre-existing v5 database gains these as ``NULL`` columns on
#: open (legacy rows read as not-cached — ``cached IS NULL`` is treated as
#: fresh), and a full ``zicato reindex`` re-derives them from each run's
#: ``loss.json`` (which now carries the provenance for materialised runs).
_V6_ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("loss_profiles", "cached", "INTEGER"),
    ("loss_profiles", "source_epoch", "TEXT"),
    ("loss_profiles", "source_run", "TEXT"),
)


#: Columns added in v7 (per-generation evolve-round grouping). A
#: generation row carries ``round_index`` — the evolve round that MINTED
#: it (its birth round; the genesis seed ``v0`` is round 0). Consumers
#: group an epoch's generations as ``Epoch -> Round -> {challengers minted
#: that round}``. Same incremental-open ALTER pattern as the earlier
#: waves: a pre-existing v6 database gains the column as ``NULL`` on open
#: (legacy generations read as ``round_index IS NULL`` — birth round
#: unknown), and a full ``zicato reindex`` re-derives it from
#: ``lineage.json`` (which now carries ``round_index`` per generation).
_V7_ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (("generations", "round_index", "INTEGER"),)


#: Columns added in v8 (per-round champion evaluation provenance). A
#: tournament row carries ``champion_eval_mode`` — how the champion
#: (parent / left) side was evaluated that round (``"full"`` = run live,
#: ``"fast"`` = cached per-board scalars reused, ``"fast-degraded"`` =
#: fast requested but the cache had to be seeded by a single live run) —
#: plus ``champion_run_ref``, a best-effort pointer at the champion's
#: per-round run/output (the champion generation's directory). Source is
#: the crowning matchup's OutcomeRecord (``champion_eval_mode`` defaults
#: to ``"full"`` for journals that predate the field). Same incremental-
#: open ALTER pattern as the earlier waves: a pre-existing v7 database
#: gains these as ``NULL`` columns on open (legacy rows read as
#: ``champion_eval_mode IS NULL`` — mode unknown, treat as ``"full"``),
#: and a full ``zicato reindex`` re-derives them from each experiment's
#: resolved outcome.
_V8_ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("tournaments", "champion_eval_mode", "TEXT"),
    ("tournaments", "champion_run_ref", "TEXT"),
)


#: Columns added in v9 (abort-cause provenance). A run aborted by infra
#: synthesises a worst-case ``loss_profiles`` row; ``abort_cause`` records
#: WHY (``budget_exhausted`` = genuine wall-clock exhaustion, vs the infra
#: causes ``parent_kill`` / ``gone_no_result`` / ``nonzero_exit:{code}`` /
#: ``prepare_failed`` / ``result_unreadable``) so loop-health can distinguish
#: an honest agent infinite-loop from a transient crash from our OWN watchdog
#: over-firing — without re-parsing each row's ``loss_json`` blob. Same
#: incremental-open ALTER pattern as the earlier waves: a pre-existing v8
#: database gains the column as ``NULL`` on open (legacy rows + every cleanly-
#: reduced non-aborted run read as ``abort_cause IS NULL``), and a full
#: ``zicato reindex`` re-derives it from each run's ``loss.json`` (which now
#: carries the cause for aborted runs).
_V9_ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (("loss_profiles", "abort_cause", "TEXT"),)


#: Columns added in v10 (the read-only Elo analytics fold;
#: FUNCTIONALITY-RECOMMENDATIONS.md §5). A generation row carries ``elo``
#: (its folded Elo rating across the lineage's settled match ledger) and
#: ``elo_games`` (how many settled duels contributed to it). These are
#: **derived, read-only** analytics columns — Elo is for visibility, never
#: for the promote decision — re-derived at index time by
#: :func:`zicato.index.elo.fold_elo_into_index`. Same incremental-open
#: ALTER pattern as the earlier waves: a pre-existing v9 database gains the
#: columns as ``NULL`` on open (a generation reads as ``elo IS NULL`` —
#: rating not yet computed), and a full ``zicato reindex`` re-derives them
#: from the ingested ``tournaments`` rows after the tournaments land.
_V10_ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("generations", "elo", "REAL"),
    ("generations", "elo_games", "INTEGER"),
)


#: Tables added in v11 (the board-reflection projection; BOARD-REFLECTION.md
#: R4). ``reflections`` carries one row per reflection run (its four-pillar
#: bill-of-health summary) and ``judge_scorecards`` one row per
#: ``(reflection_id, judge_name)`` (the confusion-matrix scorecard). Unlike
#: the earlier waves these are WHOLE NEW TABLES, not new columns, so the
#: ``CREATE TABLE IF NOT EXISTS`` pass in :func:`apply_schema` materialises
#: them on any open — a pre-existing v10 database simply gains the two empty
#: tables, and a full ``zicato reindex`` re-derives their rows from each
#: reflection's persisted ``plan.json`` / ``scorecards.json`` / ``findings.json``
#: (files canonical; the index is a projection, so a reflection is readable
#: with no index at all). The generations ``elo`` / ``elo_games`` stubs are
#: untouched. No ``_migrate_inplace`` column-ALTER entry is needed for a whole
#: new table.
_V11_ADDED_TABLES: tuple[str, ...] = ("reflections", "judge_scorecards")


#: Columns added in v12 (the standard error of the Bradley--Terry rating fold).
#: A generation row gains ``elo_se`` — the standard error of its ``elo`` on the
#: same Elo scale, from the inverse Fisher information of the batch BT fit
#: (:func:`zicato.index.elo.fold_elo_into_index`). Like ``elo`` / ``elo_games``
#: it is a **derived, read-only, visibility-only** analytics column — the rating
#: and its uncertainty never gate the promote decision — re-derived at index
#: time from the ingested ``tournaments`` rows. Same incremental-open ALTER
#: pattern as the earlier waves: a pre-existing v11 (or older) database gains
#: the column as ``NULL`` on open (a generation reads as ``elo_se IS NULL`` —
#: uncertainty not yet computed), and the next ``zicato reindex`` re-derives it
#: after the tournaments land. The fold writes ``elo_se`` only when the column
#: is present, so a v10/v11 index that has ``elo`` but not yet this column still
#: folds its two older rating columns.
_V12_ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (("generations", "elo_se", "REAL"),)


def apply_schema(conn: sqlite3.Connection) -> None:
    """Create every table + index + stamp the schema version.

    Idempotent — every statement is ``IF NOT EXISTS`` and the
    ``schema_meta`` row is upserted, so running this against an
    already-initialised database is a no-op. The canonical build path
    drops the database file first (see
    :func:`zicato.index.ingest.rebuild_index`), but ``ingest_run`` /
    ``ingest_experiment`` call this on an existing file to be safe.

    When the file pre-dates :data:`SCHEMA_VERSION` (e.g. a v1 database
    opened by a v2-aware writer), the missing columns are added in
    place via ``ALTER TABLE`` so the incremental writer can proceed
    without forcing the operator to run ``zicato reindex`` first.

    Both the ``user_version`` pragma and the ``schema_meta`` table are
    stamped with :data:`SCHEMA_VERSION`.

    Raises :class:`IndexSchemaNewerError` when the database's stamped
    ``user_version`` EXCEEDS this build's :data:`SCHEMA_VERSION` —
    re-stamping a newer database down would silently corrupt the version
    signal for the newer writer. (In-place migration only ever carries an
    OLDER database forward.)
    """
    current = read_schema_version(conn)
    if current > SCHEMA_VERSION:
        raise IndexSchemaNewerError(
            f"index database schema is v{current}, newer than this build's "
            f"v{SCHEMA_VERSION}; refusing to re-stamp it down. Upgrade "
            "zicato, or delete the index database and run `zicato reindex` "
            "(the index is derived — a rebuild loses nothing)."
        )
    # Step the v1 -> v2 migration first so an older file's CREATE TABLE
    # statement (a no-op because the table already exists) does not skip
    # adding the new columns. Then the IF-NOT-EXISTS statements below
    # handle the fresh-database case.
    _migrate_inplace(conn)
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


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    """Return the current column names of ``table`` (empty if absent)."""
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    except sqlite3.Error:
        return set()
    return {r[1] for r in rows}


def _migrate_inplace(conn: sqlite3.Connection) -> None:
    """Carry an older-schema database forward to :data:`SCHEMA_VERSION`.

    The canonical rebuild path drops the database file first, so this
    migrator only runs against databases that exist with an older
    ``user_version``. The migrations here are additive (column adds via
    ``ALTER TABLE ... ADD COLUMN``) and idempotent — running the
    migrator against an already-current database is a no-op.

    The consolidated v1 -> v2 step covers every column that landed in
    SCHEMA_VERSION 2: ``epochs.goal``, ``epochs.parent_epoch_id``,
    ``runs.tournament_id``, ``loss_profiles.tournament_id``. The
    ``judge_losses`` table is created (rather than altered) by the
    regular ``CREATE TABLE IF NOT EXISTS`` pass, so it does not need
    a migration entry. The v2 -> v3 step adds the configurable-
    tournament-structure columns to ``tournaments``; the v3 -> v4 step
    adds ``runs.match_id`` + ``loss_profiles.match_id`` (per-board-run
    tournament provenance). Each ALTER is guarded by a column-presence
    check so the migration is idempotent; tables that do not yet
    exist (fresh database) are skipped — the subsequent CREATE TABLE
    statement will already include the column.
    """
    current = read_schema_version(conn)
    if current >= SCHEMA_VERSION:
        return

    if current < 2:
        for table, column, ddl_type in _V2_ADDED_COLUMNS:
            if not _table_exists(conn, table):
                continue
            if column in _column_names(conn, table):
                continue
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}")

    if current < 3:
        for table, column, ddl_type in _V3_ADDED_COLUMNS:
            if not _table_exists(conn, table):
                continue
            if column in _column_names(conn, table):
                continue
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}")

    if current < 4:
        for table, column, ddl_type in _V4_ADDED_COLUMNS:
            if not _table_exists(conn, table):
                continue
            if column in _column_names(conn, table):
                continue
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}")

    if current < 5:
        for table, column, ddl_type in _V5_ADDED_COLUMNS:
            if not _table_exists(conn, table):
                continue
            if column in _column_names(conn, table):
                continue
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}")

    if current < 6:
        for table, column, ddl_type in _V6_ADDED_COLUMNS:
            if not _table_exists(conn, table):
                continue
            if column in _column_names(conn, table):
                continue
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}")

    if current < 7:
        for table, column, ddl_type in _V7_ADDED_COLUMNS:
            if not _table_exists(conn, table):
                continue
            if column in _column_names(conn, table):
                continue
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}")

    if current < 8:
        for table, column, ddl_type in _V8_ADDED_COLUMNS:
            if not _table_exists(conn, table):
                continue
            if column in _column_names(conn, table):
                continue
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}")

    if current < 9:
        for table, column, ddl_type in _V9_ADDED_COLUMNS:
            if not _table_exists(conn, table):
                continue
            if column in _column_names(conn, table):
                continue
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}")

    if current < 10:
        for table, column, ddl_type in _V10_ADDED_COLUMNS:
            if not _table_exists(conn, table):
                continue
            if column in _column_names(conn, table):
                continue
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}")

    # v11 added WHOLE tables (reflections / judge_scorecards), materialised by
    # the CREATE TABLE IF NOT EXISTS pass — no column ALTER needed here. v12
    # adds the ``generations.elo_se`` rating-uncertainty column.
    if current < 12:
        for table, column, ddl_type in _V12_ADDED_COLUMNS:
            if not _table_exists(conn, table):
                continue
            if column in _column_names(conn, table):
                continue
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}")


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
    "IndexSchemaNewerError",
    "apply_schema",
    "read_schema_version",
]
