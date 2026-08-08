"""Tolerant raw scan of a frozen ``board.jsonl`` (DQ3).

The query layer reads the board for two different reasons — to union the
judge names an epoch declares (``judge_view``) and to slice per-entry
outcomes by tag (``tournament_view``). Neither reader wants
:func:`zicato.board.jsonl.load_board`: that function VALIDATES, so one
stale entry anywhere on the board raises and blanks the whole payload.
A read model must degrade per-row, not per-file (09-dashboard-and-query.md
§9.3.1).

So both readers walk the raw JSONL and skip what they cannot parse. This
module owns that walk once. It never raises: an absent, unreadable, or
non-UTF-8 board yields an empty list, and a malformed or non-object line
is dropped while its siblings survive.

The scan drops the ``board_meta`` header row (it is board-level metadata,
not an entry) and returns every other object verbatim. Callers pick the
fields they need with their own type guards — this module deliberately
knows nothing about the entry schema.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

#: The board-level metadata header key (mirrors
#: ``zicato.board.jsonl._BOARD_META_KEY``). A row carrying it is not an
#: entry. The raw scan does not enforce the "must be first line" rule the
#: validating loader does — a read model reports what is on disk.
BOARD_META_KEY = "board_meta"


def iter_board_rows(path: Path) -> list[dict[str, Any]]:
    """Return the raw entry objects of a ``board.jsonl``, best-effort.

    Every failure mode degrades to a shorter list, never an exception:
    a missing file, an unreadable one, a non-UTF-8 one, a malformed
    line, a non-object line, and the ``board_meta`` header all drop out.
    """
    try:
        text = Path(path).read_text(encoding="utf-8")
    except Exception:  # noqa: BLE001 — best-effort, mirrors sibling readers
        return []
    rows: list[dict[str, Any]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict) or obj.get(BOARD_META_KEY) is True:
            continue
        rows.append(obj)
    return rows


def board_entry_id(row: dict[str, Any]) -> str | None:
    """The entry id of a raw board row, or ``None`` when unusable.

    An entry id keys the ``runs/{entry}/`` directory, so a non-string or
    empty id cannot be joined against anything a reader holds.
    """
    entry_id = row.get("id")
    return entry_id if isinstance(entry_id, str) and entry_id else None


def board_entry_tags(row: dict[str, Any]) -> list[str]:
    """The string tags of a raw board row (non-strings dropped)."""
    tags = row.get("tags")
    if not isinstance(tags, list):
        return []
    return [t for t in tags if isinstance(t, str)]
