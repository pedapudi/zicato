"""v10 schema: the read-only Elo analytics columns + additive migration.

The Elo analytics fold (FUNCTIONALITY-RECOMMENDATIONS.md §5) adds two
columns to the ``generations`` table — ``elo`` (REAL, the folded rating)
and ``elo_games`` (INTEGER, how many settled duels contributed). This
pins the fresh-build shape, the SCHEMA_VERSION bump, and the additive
v9 -> v10 in-place migration (the same pattern as the earlier waves): an
existing v9 database gains the columns as NULL on open; a full rebuild
re-applies the v10 CREATE TABLE. The columns are derived/read-only — Elo
never gates promotion.

Stacked on the v9 abort-cause column (PR #33); a pre-existing v8 database
migrates THROUGH v9 (``loss_profiles.abort_cause``) to v10 in one open.
"""

from __future__ import annotations

import sqlite3

from zicato.index.schema import (
    _V10_ADDED_COLUMNS,
    SCHEMA_VERSION,
    apply_schema,
    read_schema_version,
)

_V10_COLS = ("elo", "elo_games")


def _generation_cols(conn: sqlite3.Connection) -> set[str]:
    return {r[1] for r in conn.execute("PRAGMA table_info(generations)")}


def _loss_profile_cols(conn: sqlite3.Connection) -> set[str]:
    return {r[1] for r in conn.execute("PRAGMA table_info(loss_profiles)")}


def test_schema_version_is_at_least_ten() -> None:
    # The Elo analytics columns landed at SCHEMA_VERSION 10 (stacked on the
    # v9 abort-cause column).
    assert SCHEMA_VERSION >= 10


def test_fresh_build_has_v10_columns() -> None:
    conn = sqlite3.connect(":memory:")
    apply_schema(conn)
    assert read_schema_version(conn) >= 10
    cols = _generation_cols(conn)
    for c in _V10_COLS:
        assert c in cols
    conn.close()


def test_pre_v10_database_is_detected_as_stale() -> None:
    # A pre-v10 index stamped at v9 must read back below SCHEMA_VERSION so a
    # consumer detects it as stale and asks the operator to run
    # ``zicato reindex`` (which rebuilds with the new columns).
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA user_version = 9")
    conn.commit()
    assert read_schema_version(conn) < SCHEMA_VERSION
    conn.close()


def test_v9_database_migrates_in_place_to_v10() -> None:
    conn = sqlite3.connect(":memory:")
    # A v9-shaped generations table (no elo columns) stamped v9.
    conn.execute(
        "CREATE TABLE generations (epoch_id TEXT, generation_id TEXT, "
        "parent_generation_id TEXT, promoted INTEGER, created_at TEXT, "
        "round_index INTEGER, PRIMARY KEY (epoch_id, generation_id))"
    )
    conn.execute(
        "INSERT INTO generations(epoch_id, generation_id, parent_generation_id, "
        "promoted, created_at, round_index) VALUES('e', 'v0', NULL, 1, 't', 0)"
    )
    conn.execute("PRAGMA user_version = 9")
    conn.commit()

    apply_schema(conn)

    # The v9 -> current migration adds the v10 columns and the file ends
    # stamped at the current version.
    assert read_schema_version(conn) == SCHEMA_VERSION
    cols = _generation_cols(conn)
    for c in _V10_COLS:
        assert c in cols
    # The pre-existing row survives with the new columns NULL.
    row = conn.execute(
        "SELECT elo, elo_games FROM generations WHERE generation_id = 'v0'"
    ).fetchone()
    assert row == (None, None)
    conn.close()


def test_v8_database_migrates_through_v9_to_v10() -> None:
    # A pre-existing v8 database migrates THROUGH the v9 abort-cause column
    # (PR #33) to the v10 Elo columns in a single open — both waves land.
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE generations (epoch_id TEXT, generation_id TEXT, "
        "parent_generation_id TEXT, promoted INTEGER, created_at TEXT, "
        "round_index INTEGER, PRIMARY KEY (epoch_id, generation_id))"
    )
    # A v8-shaped loss_profiles table (no abort_cause column).
    conn.execute(
        "CREATE TABLE loss_profiles (run_id TEXT PRIMARY KEY, epoch_id TEXT, "
        "generation_id TEXT, entry_id TEXT, drift_loss REAL, pass_fail INTEGER, "
        "runtime_ms INTEGER, wall_clock_budget_exceeded INTEGER, loss_json TEXT, "
        "tournament_id TEXT, match_id TEXT, cached INTEGER, source_epoch TEXT, "
        "source_run TEXT)"
    )
    conn.execute("PRAGMA user_version = 8")
    conn.commit()

    apply_schema(conn)

    assert read_schema_version(conn) == SCHEMA_VERSION
    # v9 column (abort-cause) landed.
    assert "abort_cause" in _loss_profile_cols(conn)
    # v10 columns (Elo) landed.
    for c in _V10_COLS:
        assert c in _generation_cols(conn)
    conn.close()


def test_migration_is_idempotent() -> None:
    # Running apply_schema twice against an already-current database is a
    # no-op (every ALTER is column-presence guarded).
    conn = sqlite3.connect(":memory:")
    apply_schema(conn)
    cols_first = _generation_cols(conn)
    apply_schema(conn)  # second pass must not raise (no duplicate-column ALTER)
    cols_second = _generation_cols(conn)
    assert cols_first == cols_second
    assert read_schema_version(conn) == SCHEMA_VERSION
    conn.close()


def test_v10_added_columns_are_all_on_generations() -> None:
    assert {t for t, _c, _d in _V10_ADDED_COLUMNS} == {"generations"}
    assert {c for _t, c, _d in _V10_ADDED_COLUMNS} == set(_V10_COLS)
