"""v8 schema: per-round champion-eval provenance columns + migration.

The champion-eval-provenance feature adds two TEXT columns to the
``tournaments`` table — ``champion_eval_mode`` (how the champion side was
evaluated that round) and ``champion_run_ref`` (a best-effort pointer at
the champion's per-round run/output). This pins the fresh-build shape, the
SCHEMA_VERSION bump, and the additive v7 -> v8 in-place migration (the
same pattern as the earlier waves): an existing v7 database gains the
columns as NULL on open; a full rebuild re-applies the v8 CREATE TABLE.
"""

from __future__ import annotations

import sqlite3

from zicato.index.schema import (
    _V8_ADDED_COLUMNS,
    SCHEMA_VERSION,
    apply_schema,
    read_schema_version,
)

_V8_COLS = (
    "champion_eval_mode",
    "champion_run_ref",
)


def _tournament_cols(conn: sqlite3.Connection) -> set[str]:
    return {r[1] for r in conn.execute("PRAGMA table_info(tournaments)")}


def test_schema_version_is_at_least_eight() -> None:
    # The champion-eval-provenance columns landed at SCHEMA_VERSION 8.
    assert SCHEMA_VERSION >= 8


def test_fresh_build_has_v8_columns() -> None:
    conn = sqlite3.connect(":memory:")
    apply_schema(conn)
    assert read_schema_version(conn) >= 8
    cols = _tournament_cols(conn)
    for c in _V8_COLS:
        assert c in cols
    conn.close()


def test_pre_v8_database_is_detected_as_stale() -> None:
    # A pre-v8 index stamped at v7 must read back below SCHEMA_VERSION so a
    # consumer detects it as stale and asks the operator to run
    # ``zicato reindex`` (which rebuilds with the new columns).
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA user_version = 7")
    conn.commit()
    assert read_schema_version(conn) < SCHEMA_VERSION
    conn.close()


def test_v7_database_migrates_in_place_to_v8() -> None:
    conn = sqlite3.connect(":memory:")
    # A v7-shaped tournaments table (no champion-eval columns) stamped v7.
    conn.execute(
        "CREATE TABLE tournaments (tournament_id TEXT PRIMARY KEY, epoch_id TEXT, "
        "parent_generation_id TEXT, child_generation_id TEXT, decision TEXT, "
        "parent_scalar REAL, child_scalar REAL, delta_scalar REAL, rejection_reason TEXT, "
        "ran_at TEXT, structure TEXT, structure_params_json TEXT, competitors_json TEXT, "
        "rounds_json TEXT, standings_json TEXT, field_status_json TEXT)"
    )
    conn.execute("PRAGMA user_version = 7")
    conn.commit()

    apply_schema(conn)

    # The v7 -> current migration steps through v8, so the v8 columns are
    # present and the file ends stamped at the current version.
    assert read_schema_version(conn) == SCHEMA_VERSION
    cols = _tournament_cols(conn)
    for c in _V8_COLS:
        assert c in cols
    conn.close()


def test_v8_added_columns_are_all_on_tournaments() -> None:
    assert {t for t, _c, _d in _V8_ADDED_COLUMNS} == {"tournaments"}
    assert {c for _t, c, _d in _V8_ADDED_COLUMNS} == set(_V8_COLS)
