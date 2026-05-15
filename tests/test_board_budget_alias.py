"""Tests for the ``budget_s`` / ``wall_clock_budget_seconds`` JSONL alias.

The Python builder API (:class:`zicato.board.Entry`) accepts ``budget_s``
as the user-facing field name; the JSONL on-disk format also uses
``budget_s`` for compactness. The dataclass field that backs both is
:attr:`~zicato.core.BoardEntry.wall_clock_budget_seconds`. This module
pins the asymmetry: the writer emits ``budget_s``, the reader accepts
either name.
"""

from __future__ import annotations

import json
from pathlib import Path

from zicato.board import Board, Entry
from zicato.board.jsonl import load_board, save_board
from zicato.core import BoardEntry


def test_save_board_emits_budget_s_short_form(tmp_path: Path) -> None:
    """The writer prefers the short ``budget_s`` field name."""
    board = Board()
    board.add(Entry(id="e1", input="x", budget_s=45))
    path = tmp_path / "b.jsonl"
    board.save(path)

    line = path.read_text(encoding="utf-8").strip()
    row = json.loads(line)
    assert row["budget_s"] == 45
    assert "wall_clock_budget_seconds" not in row


def test_save_board_raw_entries_uses_budget_s(tmp_path: Path) -> None:
    """Hand-constructed BoardEntry instances also write as ``budget_s``."""
    entry = BoardEntry(id="raw", kind="single_turn", wall_clock_budget_seconds=99, input="hi")
    path = tmp_path / "b.jsonl"
    save_board([entry], path)
    row = json.loads(path.read_text(encoding="utf-8").strip())
    assert row["budget_s"] == 99


def test_load_board_accepts_budget_s_alias(tmp_path: Path) -> None:
    """A board file using ``budget_s`` loads correctly."""
    path = tmp_path / "alias.jsonl"
    path.write_text(
        json.dumps({"id": "e1", "kind": "single_turn", "input": "hi", "budget_s": 77}) + "\n",
        encoding="utf-8",
    )
    entries = load_board(path)
    assert len(entries) == 1
    assert entries[0].wall_clock_budget_seconds == 77


def test_load_board_accepts_legacy_long_form(tmp_path: Path) -> None:
    """The reader still accepts ``wall_clock_budget_seconds`` for legacy boards."""
    path = tmp_path / "long.jsonl"
    path.write_text(
        json.dumps(
            {
                "id": "e1",
                "kind": "single_turn",
                "input": "hi",
                "wall_clock_budget_seconds": 88,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    entries = load_board(path)
    assert entries[0].wall_clock_budget_seconds == 88


def test_long_form_wins_when_both_keys_present(tmp_path: Path) -> None:
    """When both keys are present, the canonical long form wins.

    Operators should never write both, but if a hand-edited board file
    carries both we follow the principle of preferring the explicit
    canonical name over the alias.
    """
    path = tmp_path / "both.jsonl"
    path.write_text(
        json.dumps(
            {
                "id": "e1",
                "kind": "single_turn",
                "input": "hi",
                "wall_clock_budget_seconds": 50,
                "budget_s": 999,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    entries = load_board(path)
    assert entries[0].wall_clock_budget_seconds == 50


def test_builder_round_trip_preserves_budget(tmp_path: Path) -> None:
    """Builder → save → load preserves the budget value."""
    board = Board()
    board.add(Entry(id="e1", input="x", budget_s=123))
    path = tmp_path / "b.jsonl"
    board.save(path)
    reloaded = Board.load(path)
    assert reloaded.entries[0].wall_clock_budget_seconds == 123
