"""Read rows of the derived index directly, for tests of what ingest wrote."""

from __future__ import annotations

import sqlite3
from pathlib import Path


def index_rows(
    db_path: Path, table: str, *, order_by: str = "rowid", **where: object
) -> list[sqlite3.Row]:
    """Rows of ``table`` whose named columns equal the given values."""
    clause = " AND ".join(f"{column} = ?" for column in where) or "1"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            f"SELECT * FROM {table} WHERE {clause} ORDER BY {order_by}",
            tuple(where.values()),
        ).fetchall()
    finally:
        conn.close()
