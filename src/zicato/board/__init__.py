"""zicato.board — the typed board-authoring API.

This package is the operator-facing surface for assembling evaluation
boards. Its vocabulary is two families of check:

* **OUTCOME checks** — graded after a run finishes. Authored with
  :class:`Predicate` (deterministic matchers) and :class:`Rubric`
  (LLM-as-judge grading); both compile to a
  :class:`~zicato.core.Expectation`.
* **PROCESS checks** — observed while a run is still in flight. Authored
  with :class:`Judge`; compile to a :class:`~zicato.core.JudgeSpec`.

Every choice field across the API is a typed enum
(:class:`~zicato.core.ExpectationKind`, :class:`~zicato.core.OutputScope`,
:class:`~zicato.core.JudgeMode`, and goldfive's
:class:`~goldfive.DriftKind` / :class:`~goldfive.DriftSeverity`) — there
are no bare magic strings at any call site.

Submodules
----------

* :mod:`zicato.board.jsonl` — round-trip serialization of a
  :class:`~zicato.core.BoardEntry` list to / from JSONL on disk
  (including board-level ``disable_drift`` metadata), plus small append /
  remove helpers used by the ``zicato board`` CLI.
* :mod:`zicato.board.matchers` — async dispatcher over the five OUTCOME
  :class:`~zicato.core.ExpectationKind` matchers, returning a uniform
  :class:`~zicato.core.ExpectationResult`.
* :mod:`zicato.board.scripted` — driver that walks a
  ``multi_turn_scripted`` entry's scripted user turns against an system
  under test and accumulates a :class:`~zicato.core.RunResult`.
* :mod:`zicato.board.builder` — programmatic :class:`Board` + :class:`Entry`
  builder API for assembling boards in Python.
* :mod:`zicato.board.predicates` — :class:`Predicate` / :class:`Rubric`
  OUTCOME-check factory helpers.
* :mod:`zicato.board.judges` — :class:`Judge` PROCESS-check factory
  helpers.
* :mod:`zicato.board.rubric` — built-in LLM-as-judge implementation of
  the :attr:`~zicato.core.ExpectationKind.RUBRIC` expectation kind.

The package re-exports the symbols downstream code (the runner, the
reducer, the CLI, board authors) actually imports; the submodules stay
importable for the test suite.
"""

from __future__ import annotations

from zicato.board.builder import Board, Entry
from zicato.board.jsonl import (
    append_entry,
    load_board,
    load_board_with_meta,
    remove_entry,
    save_board,
)
from zicato.board.judges import Judge
from zicato.board.matchers import evaluate_expectation
from zicato.board.predicates import Predicate, Rubric
from zicato.board.rubric import evaluate_rubric_judge
from zicato.board.scripted import ScriptedMultiTurnDriver

__all__ = [
    "append_entry",
    "load_board",
    "load_board_with_meta",
    "remove_entry",
    "save_board",
    "evaluate_expectation",
    "evaluate_rubric_judge",
    "ScriptedMultiTurnDriver",
    "Board",
    "Entry",
    "Predicate",
    "Rubric",
    "Judge",
]
