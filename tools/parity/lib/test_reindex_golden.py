"""Parity REINDEX-DUMP gate.

The SQLite analytical index is a *pure projection* of the canonical
workspace files (``zicato repair index`` drops ``index.db`` and re-derives every
row). A behavior-preserving refactor must keep that projection
byte-identical.

This gate:

1. Drives the SAME deterministic racing mock evolve as the MOCK-GOLDEN gate
   (so the source workspace is fixed and reproducible — no committed binary
   fixture to drift).
2. Rebuilds the index with :func:`zicato.index.ingest.rebuild_index`.
3. Dumps the database to stable text via the SQL ``iterdump`` (statements in
   deterministic order, every row a literal ``INSERT``).
4. Normalizes the wall-clock / date / uuid noise (see ``normalize.py``).
5. Asserts byte-identity against the committed golden (or rewrites it under
   ``ZICATO_PARITY_UPDATE=1``).

The dump text is the index's full contents — schema DDL + every row of
every table — so a refactor that changes which rows the projection
produces, or any column value, moves these bytes.
"""

from __future__ import annotations

import os
import re
import sqlite3
from pathlib import Path

import pytest
from goldendiff import golden_mismatch_message
from mock_evolve_capture import drive_mock_evolve
from normalize import _EPOCH_DATE_PREFIX, _ISO_TS  # noqa: PLC2701

GOLDEN_PATH = Path(__file__).resolve().parents[1] / "golden" / "reindex_dump.sql"

# A 32-hex token embedded ANYWHERE in a dump line (patch ids surface inside
# INSERT value lists, not as standalone JSON strings, so the whole-token
# rule in normalize.py does not reach them).
_EMBEDDED_UUID = re.compile(r"\b[0-9a-f]{32}\b")

# A quoted SQL string ('' escapes a quote) or a bare REAL literal. SQLite's
# REAL-to-text formatter is not stable across builds — 3.41 switched to the
# shortest round-trippable spelling, so one stored double prints as
# ``-3.999999999999999111e-01`` against an older library and
# ``-0.39999999999999991`` against a newer one. Matching strings FIRST leaves
# quoted payload alone, so only true REAL columns get re-spelled.
_QUOTED_OR_REAL = re.compile(r"'(?:[^']|'')*'|-?\d+\.\d+(?:[eE][+-]?\d+)?")


def _canonicalize_reals(line: str) -> str:
    """Re-spell REAL literals through ``repr(float)``, leaving strings intact."""

    def respell(match: re.Match[str]) -> str:
        token = match.group()
        return token if token.startswith("'") else repr(float(token))

    return _QUOTED_OR_REAL.sub(respell, line)


def _normalize_dump_line(line: str) -> str:
    out = _ISO_TS.sub("<TS>", line)
    out = _EPOCH_DATE_PREFIX.sub("<DATE>", out)
    out = _EMBEDDED_UUID.sub("<HEX32>", out)
    out = _canonicalize_reals(out)
    return out


def _dump_index(db_path: Path) -> str:
    """Return the normalized, deterministic textual dump of the index."""
    conn = sqlite3.connect(str(db_path))
    try:
        lines = list(conn.iterdump())
    finally:
        conn.close()
    # iterdump emits in a deterministic order (sqlite_master order); rows
    # within a table follow rowid order. Normalize each line.
    normalized = [_normalize_dump_line(line) for line in lines]
    return "\n".join(normalized) + "\n"


def test_reindex_dump_golden(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    workspace, _epoch_id = drive_mock_evolve(monkeypatch, tmp_path)

    from zicato.index.ingest import rebuild_index

    db_path = rebuild_index(workspace)
    dump = _dump_index(db_path)

    if os.environ.get("ZICATO_PARITY_UPDATE") == "1":
        GOLDEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN_PATH.write_text(dump, encoding="utf-8")
        return

    assert GOLDEN_PATH.exists(), f"golden missing at {GOLDEN_PATH}; run with ZICATO_PARITY_UPDATE=1"
    expected = GOLDEN_PATH.read_text(encoding="utf-8")
    assert dump == expected, golden_mismatch_message(
        "REINDEX-DUMP drift: the SQLite index projection of the fixture "
        "workspace changed. The index is a pure projection — a "
        "behavior-preserving refactor must keep it byte-identical.",
        expected,
        dump,
        golden_path=str(GOLDEN_PATH),
    )
