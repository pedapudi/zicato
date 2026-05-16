"""Tests for the programmatic Board / Entry builder API.

These tests pin the auto-detect behavior of :class:`zicato.board.Entry`,
the ergonomics of :class:`zicato.board.Board`, and the JSONL round-trip
that lets the builder hand off to the on-disk format used by the CLI.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from goldfive import DriftKind, DriftSeverity

from zicato.board import Board, Entry, Judge, Predicate, Rubric
from zicato.core import (
    BoardEntry,
    Expectation,
    ExpectationKind,
    JudgeSpec,
    ScriptedTurn,
    UserPersona,
)

# ---------------------------------------------------------------------------
# Entry: auto-detect by kind
# ---------------------------------------------------------------------------


def test_entry_with_input_only_is_single_turn() -> None:
    """``Entry(input=...)`` with no other discriminant infers ``single_turn``."""
    entry = Entry(id="e1", input="What is 2+2?", budget_s=60)
    assert isinstance(entry, BoardEntry)
    assert entry.kind == "single_turn"
    assert entry.input == "What is 2+2?"
    assert entry.wall_clock_budget_seconds == 60
    assert entry.turns is None
    assert entry.user_persona is None


def test_entry_with_turns_list_of_strings_is_scripted() -> None:
    """``Entry(turns=["...", "..."])`` infers ``multi_turn_scripted``."""
    entry = Entry(id="e1", turns=["first user msg", "second user msg"], budget_s=120)
    assert entry.kind == "multi_turn_scripted"
    assert entry.turns == (
        ScriptedTurn(user="first user msg"),
        ScriptedTurn(user="second user msg"),
    )
    # max_turns auto-fills to len(turns).
    assert entry.max_turns == 2


def test_entry_with_turns_already_scripted_turn_instances_is_scripted() -> None:
    """``Entry(turns=[ScriptedTurn(...), ...])`` is accepted unchanged."""
    turns = [ScriptedTurn(user="a"), ScriptedTurn(user="b")]
    entry = Entry(id="e1", turns=turns, budget_s=120)
    assert entry.kind == "multi_turn_scripted"
    assert entry.turns == tuple(turns)


def test_entry_with_persona_is_emulated() -> None:
    """``Entry(persona=UserPersona(...))`` infers ``multi_turn_emulated``."""
    persona = UserPersona(goal="g", constraints="c", stop_when="s")
    entry = Entry(id="e1", persona=persona, budget_s=300)
    assert entry.kind == "multi_turn_emulated"
    assert entry.user_persona == persona
    # max_turns auto-defaults to a sane non-None for emulated.
    assert entry.max_turns is not None
    assert entry.max_turns > 0


def test_entry_with_adversarial_agent_spec_is_synthetic_adversarial() -> None:
    """``Entry(input=..., adversarial_agent_spec=...)`` infers the synthetic kind."""
    entry = Entry(
        id="e1",
        input="break the agent",
        adversarial_agent_spec="tests.fakes.bad_agent",
        required_drift_kinds=("off_topic",),
        budget_s=60,
    )
    assert entry.kind == "synthetic_adversarial"
    assert entry.adversarial_agent_spec == "tests.fakes.bad_agent"
    assert entry.required_drift_kinds == ("off_topic",)


def test_entry_with_explicit_synthetic_clean_kind() -> None:
    """``Entry(input=..., kind="synthetic_clean")`` produces a synthetic_clean entry."""
    entry = Entry(id="e1", input="a known-clean question", kind="synthetic_clean", budget_s=30)
    assert entry.kind == "synthetic_clean"
    assert entry.input == "a known-clean question"


# ---------------------------------------------------------------------------
# Entry: validation / error paths
# ---------------------------------------------------------------------------


def test_entry_rejects_input_plus_turns() -> None:
    """Supplying both ``input`` and ``turns`` is an error with a clear message."""
    with pytest.raises(ValueError, match="mutually exclusive"):
        Entry(id="e1", input="hi", turns=["x"])


def test_entry_rejects_input_plus_persona() -> None:
    """``input`` and ``persona`` are mutually exclusive."""
    persona = UserPersona(goal="g", constraints="c", stop_when="s")
    with pytest.raises(ValueError, match="mutually exclusive"):
        Entry(id="e1", input="hi", persona=persona)


def test_entry_rejects_turns_plus_persona() -> None:
    """Only one of turns / persona / adversarial_agent_spec is allowed."""
    persona = UserPersona(goal="g", constraints="c", stop_when="s")
    with pytest.raises(ValueError, match="cannot supply more than one"):
        Entry(id="e1", turns=["x"], persona=persona)


def test_entry_rejects_when_no_discriminant_supplied() -> None:
    """A bare ``Entry(id=...)`` with no discriminant is rejected."""
    with pytest.raises(ValueError, match="must supply one of"):
        Entry(id="e1")


def test_entry_rejects_empty_turns_list() -> None:
    """An empty turns sequence is rejected at the builder."""
    with pytest.raises(ValueError, match="must be non-empty"):
        Entry(id="e1", turns=[])


def test_entry_rejects_unknown_explicit_kind() -> None:
    """An unrecognized explicit ``kind`` is rejected."""
    with pytest.raises(ValueError, match="not a recognized BoardEntryKind"):
        Entry(id="e1", input="x", kind="not_a_real_kind")


def test_entry_adversarial_without_input_is_rejected() -> None:
    """``adversarial_agent_spec`` alone (no ``input``) is rejected."""
    with pytest.raises(ValueError, match="requires 'input'"):
        Entry(
            id="e1",
            adversarial_agent_spec="tests.fakes.bad",
            required_drift_kinds=("off_topic",),
        )


# ---------------------------------------------------------------------------
# Entry: budget_s alias and Expectation pass-through
# ---------------------------------------------------------------------------


def test_entry_budget_s_stored_as_wall_clock_budget_seconds() -> None:
    """``budget_s`` is stored on the dataclass field ``wall_clock_budget_seconds``."""
    entry = Entry(id="e1", input="x", budget_s=42)
    assert entry.wall_clock_budget_seconds == 42


def test_entry_evaluate_attaches_expectation() -> None:
    """``evaluate=Predicate.contains(...)`` becomes ``entry.expectation``."""
    entry = Entry(id="e1", input="x", evaluate=Predicate.contains("ok"))
    assert entry.expectation == Expectation(kind=ExpectationKind.EXPECTED_TEXT, spec="ok")


def test_entry_evaluate_with_rubric_score() -> None:
    """Rubric.score expectations attach correctly."""
    exp = Rubric.score("score clarity", threshold=7.0, scale=(0.0, 10.0))
    entry = Entry(id="e1", input="x", evaluate=exp)
    assert entry.expectation is not None
    assert entry.expectation.kind is ExpectationKind.RUBRIC


# ---------------------------------------------------------------------------
# Entry: judges (PROCESS checks)
# ---------------------------------------------------------------------------


def test_entry_with_no_judges_defaults_empty() -> None:
    """An entry built without ``judges`` carries an empty judges tuple."""
    entry = Entry(id="e1", input="x", budget_s=30)
    assert entry.judges == ()


def test_entry_judges_attach_to_board_entry() -> None:
    """``judges=[Judge.custom(...), ...]`` becomes ``entry.judges``."""
    j1 = Judge.custom("on_task", "stays on task", severity=DriftSeverity.WARNING)
    j2 = Judge.python("no_pii", "proj.judges.pii", severity=DriftSeverity.CRITICAL)
    entry = Entry(id="e1", input="x", judges=[j1, j2], budget_s=30)
    assert entry.judges == (j1, j2)


def test_entry_judges_accept_tuple() -> None:
    """A tuple of judges is accepted as well as a list."""
    j = Judge.custom("on_task", "stays on task", severity=DriftSeverity.INFO)
    entry = Entry(id="e1", input="x", judges=(j,), budget_s=30)
    assert entry.judges == (j,)


def test_entry_judges_reject_non_judge_spec() -> None:
    """A non-:class:`JudgeSpec` element in ``judges`` is rejected."""
    with pytest.raises(ValueError, match="JudgeSpec"):
        Entry(id="e1", input="x", judges=["not a judge"], budget_s=30)  # type: ignore[list-item]


def test_entry_evaluate_and_judges_are_independent() -> None:
    """An entry may carry both an OUTCOME expectation and PROCESS judges."""
    entry = Entry(
        id="e1",
        input="x",
        evaluate=Predicate.contains("ok"),
        judges=[Judge.custom("on_task", "stays on task", severity=DriftSeverity.WARNING)],
        budget_s=30,
    )
    assert entry.expectation is not None
    assert len(entry.judges) == 1


# ---------------------------------------------------------------------------
# Board: container behavior
# ---------------------------------------------------------------------------


def test_board_add_accepts_entry_factory_result() -> None:
    """Board.add accepts an Entry-built BoardEntry."""
    board = Board()
    board.add(Entry(id="e1", input="hi", budget_s=30))
    assert len(board.entries) == 1
    assert board.entries[0].id == "e1"


def test_board_add_accepts_raw_board_entry() -> None:
    """Board.add also accepts a hand-constructed BoardEntry."""
    board = Board()
    raw = BoardEntry(id="raw1", kind="single_turn", wall_clock_budget_seconds=10, input="x")
    board.add(raw)
    assert board.entries == [raw]


def test_board_add_rejects_non_board_entry() -> None:
    """Board.add rejects arbitrary objects with a TypeError."""
    board = Board()
    with pytest.raises(TypeError, match="BoardEntry"):
        board.add("not an entry")  # type: ignore[arg-type]


def test_board_add_rejects_duplicate_id() -> None:
    """Board.add catches duplicate ids at append time."""
    board = Board()
    board.add(Entry(id="dup", input="x", budget_s=10))
    with pytest.raises(ValueError, match="already present"):
        board.add(Entry(id="dup", input="y", budget_s=10))


def test_board_add_returns_self_for_chaining() -> None:
    """Board.add returns the board so callers can chain."""
    board = Board()
    out = board.add(Entry(id="e1", input="x", budget_s=10)).add(
        Entry(id="e2", input="y", budget_s=10)
    )
    assert out is board
    assert [e.id for e in board.entries] == ["e1", "e2"]


# ---------------------------------------------------------------------------
# Board: save / load JSONL round-trip
# ---------------------------------------------------------------------------


def test_board_save_load_round_trip(tmp_path: Path) -> None:
    """A board with mixed entry kinds round-trips through JSONL."""
    persona = UserPersona(goal="g", constraints="c", stop_when="s")
    board = Board()
    board.add(
        Entry(
            id="single",
            input="hi",
            evaluate=Predicate.contains("ok"),
            budget_s=30,
            tags=("smoke",),
        )
    )
    board.add(Entry(id="scripted", turns=["a", "b"], budget_s=120))
    board.add(Entry(id="emulated", persona=persona, budget_s=200))

    path = tmp_path / "b.jsonl"
    board.save(path)

    reloaded = Board.load(path)
    assert reloaded.entries == board.entries


def test_board_disable_drift_defaults_empty() -> None:
    """A bare :class:`Board` has an empty ``disable_drift`` tuple."""
    assert Board().disable_drift == ()


def test_board_disable_drift_round_trips(tmp_path: Path) -> None:
    """Board-level ``disable_drift`` survives a save / load cycle."""
    board = Board(disable_drift=(DriftKind.OFF_TOPIC, DriftKind.BLOCKED))
    board.add(Entry(id="e1", input="x", budget_s=30))
    path = tmp_path / "b.jsonl"
    board.save(path)

    reloaded = Board.load(path)
    assert reloaded.disable_drift == (DriftKind.OFF_TOPIC, DriftKind.BLOCKED)
    assert reloaded.entries == board.entries


def test_board_save_load_round_trip_with_judges(tmp_path: Path) -> None:
    """A board whose entries carry PROCESS judges round-trips through JSONL."""
    board = Board()
    board.add(
        Entry(
            id="judged",
            input="x",
            judges=[
                Judge.custom("on_task", "stays on task", severity=DriftSeverity.WARNING),
                Judge.python("no_pii", "proj.judges.pii", severity=DriftSeverity.CRITICAL),
            ],
            budget_s=30,
        )
    )
    path = tmp_path / "b.jsonl"
    board.save(path)

    reloaded = Board.load(path)
    assert reloaded.entries == board.entries
    assert isinstance(reloaded.entries[0].judges[0], JudgeSpec)
