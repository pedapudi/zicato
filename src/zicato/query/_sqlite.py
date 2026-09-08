"""Supported read-only index connections and nullable value decoding."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from zicato.index.query import IndexNotBuiltError, open_index

# ---------------------------------------------------------------------------
# SQLite analytical index — bracket / matchup / health
# ---------------------------------------------------------------------------


class _IndexAbsent(Exception):
    """``index.db`` does not exist on disk."""


#: THE ``note`` a reader serves when the analytical index could not answer.
#: It names the repair the operator runs, so a client renders it verbatim
#: rather than deciding for itself what an empty payload means; every reader
#: that degrades on an unusable index serves this exact string, which is what
#: lets a client match on it.
INDEX_NOT_BUILT_NOTE = "index not built; run zicato repair index"


def with_index_not_built_note(payload: dict[str, Any]) -> dict[str, Any]:
    """``payload`` plus :data:`INDEX_NOT_BUILT_NOTE` on its ``note`` field.

    A new dict, so an empty-payload template the caller reuses is not
    mutated. ``note`` lands last, where every degrading reader already put it.
    """
    return {**payload, "note": INDEX_NOT_BUILT_NOTE}


def _open_index(path: Path) -> sqlite3.Connection:
    try:
        return open_index(path)
    except IndexNotBuiltError:
        raise _IndexAbsent from None


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


def _opt_str(row: Any, key: str) -> str | None:
    """A nonempty string column, or None for a null or empty value."""
    value = row[key]
    return value if isinstance(value, str) and value else None


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
