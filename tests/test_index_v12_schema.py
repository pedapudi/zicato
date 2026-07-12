"""v12 schema: the ``generations.elo_se`` rating-uncertainty column.

The Bradley--Terry rating fold (SELECTION-THEORY.md §7.1;
FUNCTIONALITY-RECOMMENDATIONS.md §5) yields a standard error per generation
alongside the rating itself. v12 adds ``generations.elo_se`` (REAL) — derived,
read-only, visibility-only: the rating and its uncertainty never gate
promotion. This pins the fresh-build shape, the SCHEMA_VERSION bump, and the
additive v11 -> v12 in-place migration (the same pattern as the earlier
waves): an existing v11 database gains the column as NULL on open (uncertainty
not yet computed — the next ``zicato reindex`` derives it); a full rebuild
re-applies the v12 CREATE TABLE.

Also pins the fold's stale-schema guard: on a database that has ``elo`` but
not yet ``elo_se`` (a v10/v11 file opened without a migrating write), the fold
still writes the two older columns and skips the SE rather than failing.
"""

from __future__ import annotations

import sqlite3

from zicato.index.elo import fold_elo_into_index
from zicato.index.schema import (
    _V12_ADDED_COLUMNS,
    SCHEMA_VERSION,
    apply_schema,
    read_schema_version,
)


def _generation_cols(conn: sqlite3.Connection) -> set[str]:
    return {r[1] for r in conn.execute("PRAGMA table_info(generations)")}


def test_schema_version_is_at_least_twelve() -> None:
    assert SCHEMA_VERSION >= 12


def test_fresh_build_has_elo_se_column() -> None:
    conn = sqlite3.connect(":memory:")
    apply_schema(conn)
    assert read_schema_version(conn) >= 12
    assert "elo_se" in _generation_cols(conn)
    conn.close()


def test_pre_v12_database_is_detected_as_stale() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA user_version = 11")
    conn.commit()
    assert read_schema_version(conn) < SCHEMA_VERSION
    conn.close()


def test_v11_database_migrates_in_place_to_v12() -> None:
    conn = sqlite3.connect(":memory:")
    # A v11-shaped generations table (elo/elo_games present, no elo_se).
    conn.execute(
        "CREATE TABLE generations (epoch_id TEXT, generation_id TEXT, "
        "parent_generation_id TEXT, promoted INTEGER, created_at TEXT, "
        "round_index INTEGER, elo REAL, elo_games INTEGER, "
        "PRIMARY KEY (epoch_id, generation_id))"
    )
    conn.execute(
        "INSERT INTO generations(epoch_id, generation_id, parent_generation_id, "
        "promoted, created_at, round_index, elo, elo_games) "
        "VALUES('e', 'v0', NULL, 1, 't', 0, 1512.0, 3)"
    )
    conn.execute("PRAGMA user_version = 11")
    conn.commit()

    apply_schema(conn)

    assert read_schema_version(conn) == SCHEMA_VERSION
    assert "elo_se" in _generation_cols(conn)
    # The pre-existing row survives with the new column NULL (uncertainty not
    # yet computed) and the older rating cells untouched.
    row = conn.execute(
        "SELECT elo, elo_se, elo_games FROM generations WHERE generation_id = 'v0'"
    ).fetchone()
    assert row == (1512.0, None, 3)
    conn.close()


def test_v10_database_migrates_through_to_v12() -> None:
    # A pre-existing v10 database (elo columns, no reflection tables, no
    # elo_se) migrates through v11 (whole-table adds) to v12 in one open.
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE generations (epoch_id TEXT, generation_id TEXT, "
        "parent_generation_id TEXT, promoted INTEGER, created_at TEXT, "
        "round_index INTEGER, elo REAL, elo_games INTEGER, "
        "PRIMARY KEY (epoch_id, generation_id))"
    )
    conn.execute("PRAGMA user_version = 10")
    conn.commit()

    apply_schema(conn)

    assert read_schema_version(conn) == SCHEMA_VERSION
    assert "elo_se" in _generation_cols(conn)
    # The v11 whole-table adds landed via the CREATE TABLE IF NOT EXISTS pass.
    tables = {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    }
    assert {"reflections", "judge_scorecards"} <= tables
    conn.close()


def test_migration_is_idempotent() -> None:
    conn = sqlite3.connect(":memory:")
    apply_schema(conn)
    cols_first = _generation_cols(conn)
    apply_schema(conn)  # second pass must not raise (no duplicate-column ALTER)
    assert _generation_cols(conn) == cols_first
    assert read_schema_version(conn) == SCHEMA_VERSION
    conn.close()


def test_v12_added_columns_shape() -> None:
    assert _V12_ADDED_COLUMNS == (("generations", "elo_se", "REAL"),)


def test_fold_degrades_on_a_schema_missing_elo_se() -> None:
    # The stale-schema guard: a v10/v11-shaped table (elo present, elo_se
    # absent) still folds the two older columns; nothing raises. Mirrors the
    # existing guard for a schema missing ``elo`` entirely (fold is a no-op).
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE generations (epoch_id TEXT, generation_id TEXT, "
        "parent_generation_id TEXT, promoted INTEGER, created_at TEXT, "
        "round_index INTEGER, elo REAL, elo_games INTEGER, "
        "PRIMARY KEY (epoch_id, generation_id))"
    )
    conn.execute(
        "CREATE TABLE tournaments (tournament_id TEXT PRIMARY KEY, epoch_id TEXT, "
        "parent_generation_id TEXT, child_generation_id TEXT, decision TEXT, "
        "delta_scalar REAL, rounds_json TEXT, ran_at TEXT)"
    )
    conn.execute(
        "INSERT INTO generations(epoch_id, generation_id, parent_generation_id, "
        "promoted, created_at) VALUES('e', 'v0', NULL, 0, 't0')"
    )
    conn.execute(
        "INSERT INTO generations(epoch_id, generation_id, parent_generation_id, "
        "promoted, created_at) VALUES('e', 'v1', 'v0', 1, 't1')"
    )
    conn.execute(
        "INSERT INTO tournaments VALUES('e:v0->v1', 'e', 'v0', 'v1', 'promoted', "
        "-0.4, NULL, 't1')"
    )
    conn.commit()

    ratings = fold_elo_into_index(conn)
    assert set(ratings) == {"v0", "v1"}
    row = conn.execute(
        "SELECT elo, elo_games FROM generations WHERE generation_id = 'v1'"
    ).fetchone()
    assert row[0] is not None and row[1] == 1
    conn.close()


def test_fold_is_a_noop_on_a_pre_v10_schema() -> None:
    # The existing guard, re-pinned post-swap: no elo columns at all => the
    # fold writes nothing and returns empty.
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE generations (epoch_id TEXT, generation_id TEXT, "
        "parent_generation_id TEXT, promoted INTEGER, created_at TEXT)"
    )
    conn.commit()
    assert fold_elo_into_index(conn) == {}
    conn.close()


def test_rust_supervisor_schema_version_is_in_lockstep() -> None:
    """The Rust read-only index reader must expect EXACTLY the Python schema.

    The supervisor refuses (serves empty for) any index whose
    ``user_version`` differs from its ``EXPECTED_SCHEMA_VERSION`` — the
    stale-schema guard. That guard silently blinded the supervisor for two
    schema generations (v11, v12) because the constant was pinned at 10 and
    nothing compared it against the PYTHON version. This source-text pin is
    the cross-language tripwire: bumping ``SCHEMA_VERSION`` without bumping
    the Rust constant (or vice versa) reds the suite.
    """
    import re
    from pathlib import Path

    from zicato.index.schema import SCHEMA_VERSION

    rust = Path(__file__).resolve().parents[1] / "crates" / "supervisor" / "src" / "index_db.rs"
    text = rust.read_text(encoding="utf-8")
    # Tolerant of an integer-type rename (i64 -> u32/u64/...) — the pin cares
    # about the VALUE, not the Rust type spelling.
    match = re.search(r"EXPECTED_SCHEMA_VERSION\s*:\s*\w+\s*=\s*(\d+)", text)
    assert match is not None, "EXPECTED_SCHEMA_VERSION not found in index_db.rs"
    assert int(match.group(1)) == SCHEMA_VERSION, (
        f"Rust EXPECTED_SCHEMA_VERSION={match.group(1)} != Python "
        f"SCHEMA_VERSION={SCHEMA_VERSION} — bump them together (the supervisor "
        "refuses mismatched indexes, so drift blinds its read-only surface)"
    )
