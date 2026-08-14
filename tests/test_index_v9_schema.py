"""v9 schema: abort-cause provenance column + migration.

The abort-cause-provenance feature adds one TEXT column to the
``loss_profiles`` table — ``abort_cause`` (WHY a synthesised aborted profile
was recorded: ``budget_exhausted`` for a genuine wall-clock exhaustion, vs the
infra causes ``parent_kill`` / ``gone_no_result`` / ``nonzero_exit:{code}`` /
``prepare_failed`` / ``result_unreadable``). This pins the fresh-build shape,
the SCHEMA_VERSION bump, and the additive v8 -> v9 in-place migration (the same
pattern as the earlier waves): an existing v8 database gains the column as NULL
on open; a full rebuild re-applies the v9 CREATE TABLE.
"""

from __future__ import annotations

import sqlite3

from zicato.index.schema import (
    _V9_ADDED_COLUMNS,
    SCHEMA_VERSION,
    apply_schema,
    read_schema_version,
)

_V9_COLS = ("abort_cause",)


def _loss_profile_cols(conn: sqlite3.Connection) -> set[str]:
    return {r[1] for r in conn.execute("PRAGMA table_info(loss_profiles)")}


def test_schema_version_is_at_least_nine() -> None:
    # The abort-cause-provenance column landed at SCHEMA_VERSION 9.
    assert SCHEMA_VERSION >= 9


def test_fresh_build_has_v9_columns() -> None:
    conn = sqlite3.connect(":memory:")
    apply_schema(conn)
    assert read_schema_version(conn) >= 9
    cols = _loss_profile_cols(conn)
    for c in _V9_COLS:
        assert c in cols
    conn.close()


def test_pre_v9_database_is_detected_as_stale() -> None:
    # A pre-v9 index stamped at v8 must read back below SCHEMA_VERSION so a
    # consumer detects it as stale and asks the operator to run
    # ``zicato repair index`` (which rebuilds with the new column).
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA user_version = 8")
    conn.commit()
    assert read_schema_version(conn) < SCHEMA_VERSION
    conn.close()


def test_v8_database_migrates_in_place_to_v9() -> None:
    conn = sqlite3.connect(":memory:")
    # A v8-shaped loss_profiles table (no abort_cause column) stamped v8.
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

    # The v8 -> current migration steps through v9, so the v9 column is present
    # and the file ends stamped at the current version.
    assert read_schema_version(conn) == SCHEMA_VERSION
    cols = _loss_profile_cols(conn)
    for c in _V9_COLS:
        assert c in cols
    conn.close()


def test_v9_added_columns_are_all_on_loss_profiles() -> None:
    assert {t for t, _c, _d in _V9_ADDED_COLUMNS} == {"loss_profiles"}
    assert {c for _t, c, _d in _V9_ADDED_COLUMNS} == set(_V9_COLS)
