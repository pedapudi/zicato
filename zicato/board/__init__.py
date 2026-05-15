"""zicato.board — board JSONL IO, expectation matchers, scripted driver.

This package wires the concerns that all live behind the
:class:`~zicato.core.BoardEntry` contract:

* :mod:`zicato.board.jsonl` — round-trip serialization of a
  :class:`~zicato.core.BoardEntry` list to / from JSONL on disk, plus
  small append / remove helpers used by the ``zicato board`` CLI.
* :mod:`zicato.board.matchers` — async dispatcher over the
  :class:`~zicato.core.Expectation` kinds, returning a uniform
  :class:`~zicato.core.ExpectationResult`.
* :mod:`zicato.board.scripted` — driver that walks a
  ``multi_turn_scripted`` entry's scripted user turns against an inner
  harness and accumulates a :class:`~zicato.core.RunResult`.
* :mod:`zicato.board.builder` — programmatic :class:`Board` + :class:`Entry`
  builder API for assembling boards in Python.
* :mod:`zicato.board.predicates` — :class:`Predicate` / :class:`Rubric`
  factory helpers that produce well-formed
  :class:`~zicato.core.Expectation` instances.

The package re-exports the symbols downstream code (the runner, the
reducer, the CLI, board authors) actually imports; the submodules stay
importable for the test suite.
"""

from __future__ import annotations

from zicato.board.builder import Board, Entry
from zicato.board.jsonl import (
    append_entry,
    load_board,
    remove_entry,
    save_board,
)
from zicato.board.matchers import evaluate_expectation
from zicato.board.predicates import Predicate, Rubric
from zicato.board.scripted import ScriptedMultiTurnDriver

__all__ = [
    "append_entry",
    "load_board",
    "remove_entry",
    "save_board",
    "evaluate_expectation",
    "ScriptedMultiTurnDriver",
    "Board",
    "Entry",
    "Predicate",
    "Rubric",
]
