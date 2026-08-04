"""v14 schema: the ``ingest_cursors`` self-heal table.

v14 adds one additive table recording, per epoch, the cheap workspace signals
that were true when that epoch was last projected — the single fact the
workspace itself does not carry, and the one that lets ``validate_index``
notice divergence without re-deriving every row (ANALYTICAL-INDEX.md §5.2).

Like the v11 and v13 waves this is a WHOLE NEW TABLE, so the migration needs
no column ALTER: an existing v13 database gains the empty table on open, which
reads as "every epoch diverged" and self-corrects on the first heal. This pins
the fresh-build shape, the SCHEMA_VERSION bump, the in-place migration against
a REAL populated v13 database, and the cross-language lockstep with the Rust
supervisor's ``EXPECTED_SCHEMA_VERSION``.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

from zicato.index.schema import (
    _V14_ADDED_TABLES,
    SCHEMA_VERSION,
    apply_schema,
    read_schema_version,
)


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def _cursor_cols(conn: sqlite3.Connection) -> list[str]:
    return [r[1] for r in conn.execute("PRAGMA table_info(ingest_cursors)")]


def test_schema_version_is_at_least_fourteen() -> None:
    assert SCHEMA_VERSION >= 14


def test_fresh_build_carries_the_cursor_table() -> None:
    conn = sqlite3.connect(":memory:")
    apply_schema(conn)
    assert read_schema_version(conn) >= 14
    assert "ingest_cursors" in _tables(conn)
    conn.close()


def test_the_cursor_columns_are_the_documented_shape() -> None:
    """The four signals plus the observational timestamp, in column order."""
    conn = sqlite3.connect(":memory:")
    apply_schema(conn)
    assert _cursor_cols(conn) == [
        "epoch_id",
        "experiments_count",
        "round_dirs_count",
        "reflections_count",
        "lineage_generations_count",
        "last_ingested_at",
    ]
    conn.close()


def test_pre_v14_database_is_detected_as_stale() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA user_version = 13")
    conn.commit()
    assert read_schema_version(conn) < SCHEMA_VERSION
    conn.close()


def test_a_populated_v13_database_gains_the_table_in_place(tmp_path: Path) -> None:
    """The migration is additive: existing rows survive, the new table lands empty.

    Against a REAL populated database (not an empty one) so the pin catches a
    migration that dropped or rewrote data on the way through.
    """
    db = tmp_path / "index.db"
    conn = sqlite3.connect(str(db))
    apply_schema(conn)
    conn.execute(
        "INSERT INTO epochs(epoch_id, contract_hash, created_at, closed, goal) "
        "VALUES('e1', 'abc', '2026-01-01T00:00:00Z', 0, 'ship it')"
    )
    conn.execute(
        "INSERT INTO generations(epoch_id, generation_id, parent_generation_id, "
        "promoted, created_at, round_index, elo, elo_se, elo_games) "
        "VALUES('e1', 'v1', 'v0', 1, '2026-01-01T00:01:00Z', 1, 1512.5, 40.0, 3)"
    )
    conn.execute("INSERT INTO pareto_frontier(epoch_id, generation_id) VALUES('e1', 'v1')")
    # Drop the v14 table and stamp back to v13 — a genuine pre-v14 file.
    conn.execute("DROP TABLE ingest_cursors")
    conn.execute("PRAGMA user_version = 13")
    conn.commit()
    conn.close()

    conn = sqlite3.connect(str(db))
    apply_schema(conn)

    assert read_schema_version(conn) == SCHEMA_VERSION
    assert "ingest_cursors" in _tables(conn)
    assert conn.execute("SELECT COUNT(*) FROM ingest_cursors").fetchone()[0] == 0
    # Every pre-existing row survives untouched.
    assert conn.execute("SELECT goal FROM epochs WHERE epoch_id = 'e1'").fetchone()[0] == "ship it"
    assert conn.execute(
        "SELECT elo, elo_se, elo_games FROM generations WHERE generation_id = 'v1'"
    ).fetchone() == (1512.5, 40.0, 3)
    assert conn.execute("SELECT COUNT(*) FROM pareto_frontier").fetchone()[0] == 1
    conn.close()


def test_cursors_backfill_on_the_first_heal_after_the_migration(tmp_path: Path) -> None:
    """An empty cursor table reads as wholly diverged, then converges."""
    from zicato.index.ingest import heal_index, rebuild_index, validate_index

    ws = tmp_path / ".zicato"
    (ws / "epochs" / "e1" / "generations" / "v0").mkdir(parents=True)
    (ws / "epochs" / "e1" / "config.json").write_text(
        json.dumps(
            {
                "id": "e1",
                "name": "e1",
                "created_at": "2026-01-01T00:00:00Z",
                "board_path": "board.jsonl",
                "brief_path": "brief.md",
                "scoring": {},
                "closed": False,
                "goal": "",
            }
        ),
        encoding="utf-8",
    )
    (ws / "lineage.json").write_text('{"epochs": []}', encoding="utf-8")
    db = rebuild_index(ws)
    # The epoch must actually be indexed, or the divergence assertions below
    # would pass vacuously against an empty walk.
    assert {r[0] for r in sqlite3.connect(str(db)).execute("SELECT epoch_id FROM epochs")} == {"e1"}

    conn = sqlite3.connect(str(db))
    conn.execute("DROP TABLE ingest_cursors")
    conn.execute("PRAGMA user_version = 13")
    conn.commit()
    conn.close()

    # ``validate_index`` reads through a read-only handle, so the table it
    # cannot find reads as "no cursors" — every epoch diverged.
    assert validate_index(ws) == ("e1",)
    assert heal_index(ws) == ("e1",)
    assert validate_index(ws) == ()


def test_v14_added_tables_shape() -> None:
    assert _V14_ADDED_TABLES == ("ingest_cursors",)


def test_rust_supervisor_schema_version_is_in_lockstep() -> None:
    """The Rust read-only index reader must expect EXACTLY the Python schema.

    The supervisor serves empty for any index whose ``user_version`` differs
    from its ``EXPECTED_SCHEMA_VERSION``, so a Python bump that leaves the Rust
    constant behind silently blinds the whole supervisor read surface. Pinned
    against the source text in each schema wave's own test file so the tripwire
    is impossible to miss while writing the wave.
    """
    rust = Path(__file__).resolve().parents[1] / "crates" / "supervisor" / "src" / "index_db.rs"
    match = re.search(
        r"EXPECTED_SCHEMA_VERSION\s*:\s*\w+\s*=\s*(\d+)", rust.read_text(encoding="utf-8")
    )
    assert match is not None, "EXPECTED_SCHEMA_VERSION not found in index_db.rs"
    assert int(match.group(1)) == SCHEMA_VERSION, (
        f"Rust EXPECTED_SCHEMA_VERSION={match.group(1)} != Python "
        f"SCHEMA_VERSION={SCHEMA_VERSION} — bump them together"
    )
