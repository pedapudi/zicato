"""The SQLite analytical index: read-only connections and tolerant row reads.

One connection lifecycle for every reader, plus the accessors that read a
column a stale index may not carry.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
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


@contextmanager
def open_index_ro(path: Path) -> Iterator[sqlite3.Connection]:
    """THE one index-connection lifecycle for every reader.

    Opens ``index.db`` read-only (URI ``mode=ro``) with the ``sqlite3.Row``
    factory and guarantees the close. Raises :class:`_IndexAbsent` when the
    file does not exist, so each caller degrades to its own empty shape.

    Never ``sqlite3.connect()`` an index path directly in a reader — a bare
    connect defaults to WRITE mode and contends for the write lock with the
    ingest writer. The ``judge_view`` search scan is the heaviest such
    reader and goes through this helper for that reason.
    """
    conn = _open_index(path)
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def open_index_ro_or_none(path: Path) -> Iterator[sqlite3.Connection | None]:
    """Best-effort variant of :func:`open_index_ro` — yields ``None``.

    For the readers that degrade FIELD-BY-FIELD rather than whole-payload:
    the workspace and ledger scans keep rendering rows with ``None`` scalars
    when the index is absent. An absent or unopenable index yields ``None``,
    so the body keeps its ``if conn is not None`` structure without a
    hand-rolled open, guard, and close block.
    """
    try:
        conn = _open_index(path)
    except (_IndexAbsent, sqlite3.Error):
        yield None
        return
    try:
        yield conn
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Tolerant row accessors — THE declared set for schema-additive columns
# ---------------------------------------------------------------------------
#
# The analytical index is migrated forward additively; a stale (pre-migration
# or hand-built fixture) database may lack a column a newer reader selects.
# Every reader tolerates that absence through these four accessors — absence
# reads as ``None`` / ``False``, never an error — so an old index loads
# unchanged instead of blanking a whole payload.


def _row_keys(row: Any) -> Any:
    """The row's column names, or ``()`` for a non-Row (tuple fixture) value."""
    try:
        return row.keys()
    except AttributeError:
        return ()


def _rget(row: Any, key: str) -> Any:
    """``row[key]`` when the column exists, else ``None`` (stale index)."""
    return row[key] if key in _row_keys(row) else None


def _opt_str(row: Any, key: str) -> str | None:
    """A non-empty string column, or ``None`` (absent column / NULL / '')."""
    if key not in _row_keys(row):
        return None
    value = row[key]
    return value if isinstance(value, str) and value else None


def _row_bool(row: Any, key: str) -> bool:
    """A boolean column read tolerantly: absent column / NULL ⇒ ``False``."""
    if key not in _row_keys(row):
        return False
    return bool(row[key]) if row[key] is not None else False


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
