"""zicato.board — board JSONL IO, expectation matchers, scripted driver.

This package wires three concerns that all live behind the
:class:`~zicato.core.BoardEntry` contract:

* :mod:`zicato.board.jsonl` — round-trip serialization of a
  :class:`~zicato.core.BoardEntry` list to / from JSONL on disk, plus
  small append / remove helpers used by the ``zicato board`` CLI.
* :mod:`zicato.board.matchers` — async dispatcher over the five
  :class:`~zicato.core.Expectation` kinds, returning a uniform
  :class:`~zicato.core.ExpectationResult`.
* :mod:`zicato.board.scripted` — driver that walks a
  ``multi_turn_scripted`` entry's scripted user turns against an inner
  harness and accumulates a :class:`~zicato.core.RunResult`.

The package re-exports the symbols downstream code (the runner, the
reducer, the CLI) actually imports; the submodules stay importable for
the test suite.
"""

from __future__ import annotations

from zicato.board.jsonl import (
    append_entry,
    load_board,
    remove_entry,
    save_board,
)
from zicato.board.matchers import evaluate_expectation
from zicato.board.scripted import ScriptedMultiTurnDriver

__all__ = [
    "append_entry",
    "load_board",
    "remove_entry",
    "save_board",
    "evaluate_expectation",
    "ScriptedMultiTurnDriver",
]
