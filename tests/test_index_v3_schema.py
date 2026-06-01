"""v3 schema: the additive tournament-structure columns + migration.

The configurable-tournament-structures feature adds five TEXT columns to
the ``tournaments`` table. This pins the fresh-build shape and the
additive v2 -> v3 in-place migration (the same pattern as v1 -> v2): an
existing v2 database gains the columns as NULL on open; a full rebuild
re-applies the v3 CREATE TABLE.
"""

from __future__ import annotations

import sqlite3

from zicato.index.schema import (
    _V3_ADDED_COLUMNS,
    SCHEMA_VERSION,
    apply_schema,
    read_schema_version,
)

_V3_COLS = (
    "structure",
    "structure_params_json",
    "competitors_json",
    "rounds_json",
    "standings_json",
)


def _tournament_cols(conn: sqlite3.Connection) -> set[str]:
    return {r[1] for r in conn.execute("PRAGMA table_info(tournaments)")}


def test_schema_version_is_three() -> None:
    # The v3 tournament-structure columns landed at SCHEMA_VERSION 3 and
    # remain part of every later version's contract; the current version
    # is at least 3 (a v4 per-board-run provenance wave bumped it further).
    assert SCHEMA_VERSION >= 3


def test_fresh_build_has_v3_columns() -> None:
    conn = sqlite3.connect(":memory:")
    apply_schema(conn)
    assert read_schema_version(conn) >= 3
    cols = _tournament_cols(conn)
    for c in _V3_COLS:
        assert c in cols


def test_v2_database_migrates_in_place_to_v3() -> None:
    conn = sqlite3.connect(":memory:")
    # A v2-shaped tournaments table (no structure columns) stamped v2.
    conn.execute(
        "CREATE TABLE tournaments (tournament_id TEXT PRIMARY KEY, epoch_id TEXT, "
        "parent_generation_id TEXT, child_generation_id TEXT, decision TEXT, "
        "parent_scalar REAL, child_scalar REAL, delta_scalar REAL, rejection_reason TEXT, "
        "ran_at TEXT)"
    )
    conn.execute("PRAGMA user_version = 2")
    conn.commit()

    apply_schema(conn)

    # The v2 -> current migration steps through v3, so the v3 columns
    # are present even though the file ends at the current version.
    assert read_schema_version(conn) >= 3
    cols = _tournament_cols(conn)
    for c in _V3_COLS:
        assert c in cols


def test_v3_added_columns_are_all_on_tournaments() -> None:
    assert {t for t, _c, _d in _V3_ADDED_COLUMNS} == {"tournaments"}
    assert {c for _t, c, _d in _V3_ADDED_COLUMNS} == set(_V3_COLS)
