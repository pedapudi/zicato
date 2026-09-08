"""The builder and board JSONL preserve one wall-clock budget field."""

from __future__ import annotations

import json
from pathlib import Path

from zicato.board import Board, Entry
from zicato.board.jsonl import load_board, save_board
from zicato.core import BoardEntry


def test_save_board_emits_wall_clock_budget(tmp_path: Path) -> None:
    """The writer emits the canonical wall-clock budget field."""
    board = Board()
    board.add(Entry(id="e1", input="x", wall_clock_budget_seconds=45))
    path = tmp_path / "b.jsonl"
    board.save(path)

    line = path.read_text(encoding="utf-8").strip()
    row = json.loads(line)
    assert row["wall_clock_budget_seconds"] == 45


def test_save_board_preserves_raw_entry_budget(tmp_path: Path) -> None:
    """Hand-constructed BoardEntry instances also write as ``wall_clock_budget_seconds``."""
    entry = BoardEntry(id="raw", kind="single_turn", wall_clock_budget_seconds=99, input="hi")
    path = tmp_path / "b.jsonl"
    save_board([entry], path)
    row = json.loads(path.read_text(encoding="utf-8").strip())
    assert row["wall_clock_budget_seconds"] == 99


def test_load_board_reads_wall_clock_budget(tmp_path: Path) -> None:
    """The reader preserves the authored wall-clock budget."""
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


def test_builder_round_trip_preserves_budget(tmp_path: Path) -> None:
    """Builder → save → load preserves the budget value."""
    board = Board()
    board.add(Entry(id="e1", input="x", wall_clock_budget_seconds=123))
    path = tmp_path / "b.jsonl"
    board.save(path)
    reloaded = Board.load(path)
    assert reloaded.entries[0].wall_clock_budget_seconds == 123
