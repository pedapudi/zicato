"""Tests for the zicato analytical index schema (:mod:`zicato.index.schema`)."""

from __future__ import annotations

import sqlite3

from zicato.index.schema import (
    SCHEMA_VERSION,
    apply_schema,
    read_schema_version,
)

# The full set of tables the shared contract pins. R9-2 and the Rust
# supervisor query these by name — the test asserts they all exist so a
# rename is caught here before it breaks a sibling.
_EXPECTED_TABLES = {
    "epochs",
    "generations",
    "experiments",
    "patches",
    "runs",
    "loss_profiles",
    "metric_counts",
    "tournaments",
    "judge_losses",
}

_EXPECTED_INDEXES = {
    "idx_runs_gen",
    "idx_loss_gen",
    "idx_metric_run",
    "idx_judge_losses_run",
    "idx_runs_tournament",
    "idx_loss_tournament",
    "idx_epochs_parent",
}


def _table_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {r[0] for r in rows}


def _index_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'").fetchall()
    return {r[0] for r in rows}


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return [r[1] for r in rows]


def test_apply_schema_creates_every_table() -> None:
    conn = sqlite3.connect(":memory:")
    apply_schema(conn)
    assert _EXPECTED_TABLES.issubset(_table_names(conn))
    conn.close()


def test_apply_schema_creates_secondary_indexes() -> None:
    conn = sqlite3.connect(":memory:")
    apply_schema(conn)
    assert _EXPECTED_INDEXES.issubset(_index_names(conn))
    conn.close()


def test_apply_schema_stamps_user_version_pragma() -> None:
    conn = sqlite3.connect(":memory:")
    apply_schema(conn)
    assert read_schema_version(conn) == SCHEMA_VERSION
    conn.close()


def test_schema_version_present_in_meta_table() -> None:
    conn = sqlite3.connect(":memory:")
    apply_schema(conn)
    row = conn.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()
    assert row is not None
    assert int(row[0]) == SCHEMA_VERSION
    conn.close()


def test_fresh_database_reports_version_zero() -> None:
    # A SQLite file that was never schema-applied defaults user_version
    # to 0; SCHEMA_VERSION starts at 1 so the two never collide.
    conn = sqlite3.connect(":memory:")
    assert read_schema_version(conn) == 0
    conn.close()


def test_apply_schema_is_idempotent() -> None:
    conn = sqlite3.connect(":memory:")
    apply_schema(conn)
    # Running it again must not raise (every statement is IF NOT EXISTS
    # and the meta row is upserted).
    apply_schema(conn)
    assert read_schema_version(conn) == SCHEMA_VERSION
    # Still exactly one schema_version row.
    rows = conn.execute("SELECT COUNT(*) FROM schema_meta WHERE key = 'schema_version'").fetchone()
    assert rows[0] == 1
    conn.close()


def test_epochs_columns_match_contract() -> None:
    conn = sqlite3.connect(":memory:")
    apply_schema(conn)
    assert _columns(conn, "epochs") == [
        "epoch_id",
        "contract_hash",
        "created_at",
        "closed",
        "goal",
        "parent_epoch_id",
    ]
    conn.close()


def test_loss_profiles_columns_match_contract() -> None:
    conn = sqlite3.connect(":memory:")
    apply_schema(conn)
    assert _columns(conn, "loss_profiles") == [
        "run_id",
        "epoch_id",
        "generation_id",
        "entry_id",
        "drift_loss",
        "pass_fail",
        "runtime_ms",
        "wall_clock_budget_exceeded",
        "loss_json",
        "tournament_id",
    ]
    conn.close()


def test_metric_counts_columns_match_contract() -> None:
    conn = sqlite3.connect(":memory:")
    apply_schema(conn)
    assert _columns(conn, "metric_counts") == [
        "run_id",
        "namespace",
        "name",
        "severity",
        "count",
    ]
    conn.close()


def test_tournaments_columns_match_contract() -> None:
    conn = sqlite3.connect(":memory:")
    apply_schema(conn)
    assert _columns(conn, "tournaments") == [
        "tournament_id",
        "epoch_id",
        "parent_generation_id",
        "child_generation_id",
        "decision",
        "parent_scalar",
        "child_scalar",
        "delta_scalar",
        "rejection_reason",
        "ran_at",
        # v3: the additive tournament-structure columns.
        "structure",
        "structure_params_json",
        "competitors_json",
        "rounds_json",
        "standings_json",
    ]
    conn.close()


def test_runs_primary_key_is_run_id() -> None:
    conn = sqlite3.connect(":memory:")
    apply_schema(conn)
    info = conn.execute("PRAGMA table_info(runs)").fetchall()
    pk_cols = [r[1] for r in info if r[5]]  # r[5] = pk flag
    assert pk_cols == ["run_id"]
    conn.close()
