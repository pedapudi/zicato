"""_sqlite — extracted from zicato.dashboard.state_reader (pure move)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# SQLite analytical index — bracket / matchup / health
# ---------------------------------------------------------------------------


class _IndexAbsent(Exception):
    """``index.db`` does not exist on disk."""


def _open_index(path: Path) -> sqlite3.Connection:
    if not path.exists():
        raise _IndexAbsent
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _query(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...]) -> list[sqlite3.Row]:
    try:
        return list(conn.execute(sql, params))
    except sqlite3.Error:
        return []


def _opt_json(value: Any) -> Any:
    if not isinstance(value, str):
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return None
