"""Board entry projections after canonical whole-file acceptance."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from zicato.board.jsonl import load_board_rows


def iter_board_rows(path: Path) -> list[dict[str, Any]]:
    """Return accepted entry rows, preserving source fields and extensions."""
    return [row for row in load_board_rows(path) or [] if row.get("board_meta") is not True]


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
