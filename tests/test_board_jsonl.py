"""Tests for the JSONL board parser / serializer."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from goldfive import DriftKind, DriftSeverity

from zicato.board.jsonl import (
    append_entry,
    load_board,
    load_board_with_meta,
    remove_entry,
    save_board,
)
from zicato.core.types import (
    BoardEntry,
    Expectation,
    ExpectationKind,
    JudgeMode,
    JudgeSpec,
    OutputScope,
    ScriptedTurn,
    UserPersona,
)


def _write_lines(path: Path, lines: list[str]) -> None:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_load_board_parses_mixed_kinds(tmp_path: Path) -> None:
    """A JSONL file with single-turn, scripted, and emulated rows round-trips."""
    board_file = tmp_path / "board.jsonl"
    rows = [
        {
            "id": "single_one",
            "kind": "single_turn",
            "input": "hello",
            "wall_clock_budget_seconds": 60,
        },
        {
            "id": "scripted_one",
            "kind": "multi_turn_scripted",
            "turns": [{"user": "first"}, {"user": "second"}],
            "max_turns": 4,
            "wall_clock_budget_seconds": 120,
            "tags": ["multi-turn"],
        },
        {
            "id": "emulated_one",
            "kind": "multi_turn_emulated",
            "user_persona": {
                "goal": "Get a review.",
                "constraints": "Be terse.",
                "stop_when": "Agent has answered three questions.",
            },
            "max_turns": 6,
            "wall_clock_budget_seconds": 300,
            "expectation": {
                "kind": "regex",
                "spec": "answered",
                "reads": "conversation_end",
            },
        },
    ]
    _write_lines(board_file, [json.dumps(r) for r in rows])

    entries = load_board(board_file)

    assert [e.id for e in entries] == ["single_one", "scripted_one", "emulated_one"]
    assert entries[0].kind == "single_turn"
    assert entries[0].input == "hello"
    assert entries[1].turns == (ScriptedTurn("first"), ScriptedTurn("second"))
    assert entries[1].max_turns == 4
    assert entries[2].user_persona is not None
    assert entries[2].user_persona.goal == "Get a review."
    assert entries[2].expectation == Expectation(
        kind=ExpectationKind.REGEX, spec="answered", reads=OutputScope.TRANSCRIPT
    )


def test_load_board_tolerates_blank_lines(tmp_path: Path) -> None:
    board_file = tmp_path / "board.jsonl"
    board_file.write_text(
        "\n"
        + json.dumps(
            {
                "id": "x",
                "kind": "single_turn",
                "input": "i",
                "wall_clock_budget_seconds": 30,
            }
        )
        + "\n\n",
        encoding="utf-8",
    )
    entries = load_board(board_file)
    assert len(entries) == 1
    assert entries[0].id == "x"


def test_load_board_rejects_duplicate_ids(tmp_path: Path) -> None:
    board_file = tmp_path / "board.jsonl"
    row = {
        "id": "dup",
        "kind": "single_turn",
        "input": "i",
        "wall_clock_budget_seconds": 30,
    }
    _write_lines(board_file, [json.dumps(row), json.dumps(row)])

    with pytest.raises(ValueError, match="duplicate"):
        load_board(board_file)


def test_load_board_rejects_malformed_line(tmp_path: Path) -> None:
    board_file = tmp_path / "board.jsonl"
    _write_lines(
        board_file,
        [
            json.dumps(
                {
                    "id": "ok",
                    "kind": "single_turn",
                    "input": "i",
                    "wall_clock_budget_seconds": 30,
                }
            ),
            "{ not json",
        ],
    )
    with pytest.raises(ValueError, match="line 2"):
        load_board(board_file)


def test_load_board_rejects_invalid_entry(tmp_path: Path) -> None:
    """Discriminant-validation runs on every row."""
    board_file = tmp_path / "board.jsonl"
    row = {
        # single_turn requires input
        "id": "bad",
        "kind": "single_turn",
        "wall_clock_budget_seconds": 30,
    }
    _write_lines(board_file, [json.dumps(row)])
    with pytest.raises(ValueError, match="invalid entry"):
        load_board(board_file)


# ---------------------------------------------------------------------------
# Enum-token schema validation
# ---------------------------------------------------------------------------


def test_load_board_rejects_unknown_expectation_kind(tmp_path: Path) -> None:
    """An unknown expectation ``kind`` token is rejected, listing valid values."""
    board_file = tmp_path / "board.jsonl"
    _write_lines(
        board_file,
        [
            json.dumps(
                {
                    "id": "e",
                    "kind": "single_turn",
                    "input": "i",
                    "wall_clock_budget_seconds": 30,
                    "expectation": {"kind": "not_a_kind", "spec": "x"},
                }
            )
        ],
    )
    with pytest.raises(ValueError, match="invalid expectation kind.*'regex'"):
        load_board(board_file)


def test_load_board_rejects_unknown_reads_token(tmp_path: Path) -> None:
    """An unknown ``reads`` token is rejected with the valid values listed."""
    board_file = tmp_path / "board.jsonl"
    _write_lines(
        board_file,
        [
            json.dumps(
                {
                    "id": "e",
                    "kind": "single_turn",
                    "input": "i",
                    "wall_clock_budget_seconds": 30,
                    "expectation": {"kind": "regex", "spec": "x", "reads": "halfway"},
                }
            )
        ],
    )
    with pytest.raises(ValueError, match="invalid expectation reads"):
        load_board(board_file)


def test_load_board_rejects_unknown_judge_mode(tmp_path: Path) -> None:
    """An unknown judge ``mode`` token is rejected."""
    board_file = tmp_path / "board.jsonl"
    _write_lines(
        board_file,
        [
            json.dumps(
                {
                    "id": "e",
                    "kind": "single_turn",
                    "input": "i",
                    "wall_clock_budget_seconds": 30,
                    "judges": [{"name": "j", "mode": "telepathy", "body": "b", "severity": "info"}],
                }
            )
        ],
    )
    with pytest.raises(ValueError, match="invalid judge mode"):
        load_board(board_file)


def test_load_board_rejects_unknown_judge_severity(tmp_path: Path) -> None:
    """An unknown judge ``severity`` token is rejected."""
    board_file = tmp_path / "board.jsonl"
    _write_lines(
        board_file,
        [
            json.dumps(
                {
                    "id": "e",
                    "kind": "single_turn",
                    "input": "i",
                    "wall_clock_budget_seconds": 30,
                    "judges": [
                        {"name": "j", "mode": "inline", "body": "b", "severity": "catastrophic"}
                    ],
                }
            )
        ],
    )
    with pytest.raises(ValueError, match="invalid judge severity"):
        load_board(board_file)


def test_load_board_rejects_unknown_disable_drift_kind(tmp_path: Path) -> None:
    """An unknown drift kind in the ``board_meta`` header is rejected."""
    board_file = tmp_path / "board.jsonl"
    _write_lines(
        board_file,
        [
            json.dumps({"board_meta": True, "disable_drift": ["not_a_drift_kind"]}),
            json.dumps(
                {"id": "e", "kind": "single_turn", "input": "i", "wall_clock_budget_seconds": 30}
            ),
        ],
    )
    with pytest.raises(ValueError, match="unknown drift kind.*disable_drift"):
        load_board(board_file)


def test_load_board_rejects_legacy_fires_on(tmp_path: Path) -> None:
    """An old-format board carrying ``fires_on`` fails with a migration error."""
    board_file = tmp_path / "board.jsonl"
    _write_lines(
        board_file,
        [
            json.dumps(
                {
                    "id": "e",
                    "kind": "single_turn",
                    "input": "i",
                    "wall_clock_budget_seconds": 30,
                    "expectation": {"kind": "regex", "spec": "x", "fires_on": "final_output"},
                }
            )
        ],
    )
    with pytest.raises(ValueError, match="fires_on.*rename it to 'reads'"):
        load_board(board_file)


def test_load_board_rejects_board_meta_not_first(tmp_path: Path) -> None:
    """A ``board_meta`` header that is not the first line is rejected."""
    board_file = tmp_path / "board.jsonl"
    _write_lines(
        board_file,
        [
            json.dumps(
                {"id": "e", "kind": "single_turn", "input": "i", "wall_clock_budget_seconds": 30}
            ),
            json.dumps({"board_meta": True, "disable_drift": ["off_topic"]}),
        ],
    )
    with pytest.raises(ValueError, match="must be the first line"):
        load_board(board_file)


# ---------------------------------------------------------------------------
# save / round-trip
# ---------------------------------------------------------------------------


def test_save_board_round_trip(tmp_path: Path) -> None:
    """save_board writes a file load_board can parse back to equal entries."""
    entries = [
        BoardEntry(
            id="single_one",
            kind="single_turn",
            wall_clock_budget_seconds=60,
            input="hello",
            tags=("easy",),
        ),
        BoardEntry(
            id="scripted_one",
            kind="multi_turn_scripted",
            wall_clock_budget_seconds=120,
            weight=1.5,
            turns=(ScriptedTurn("a"), ScriptedTurn("b")),
            max_turns=4,
        ),
        BoardEntry(
            id="emulated_one",
            kind="multi_turn_emulated",
            wall_clock_budget_seconds=240,
            user_persona=UserPersona(goal="g", constraints="c", stop_when="s"),
            max_turns=6,
            expectation=Expectation(
                kind=ExpectationKind.REGEX, spec="answered", reads=OutputScope.TRANSCRIPT
            ),
        ),
    ]
    for entry in entries:
        entry.validate()

    board_file = tmp_path / "board.jsonl"
    save_board(entries, board_file)
    parsed = load_board(board_file)

    assert parsed == entries


def test_save_board_round_trips_judges(tmp_path: Path) -> None:
    """An entry carrying PROCESS judges round-trips through JSONL."""
    entry = BoardEntry(
        id="judged",
        kind="single_turn",
        wall_clock_budget_seconds=60,
        input="hi",
        judges=(
            JudgeSpec(
                name="stays_on_task",
                mode=JudgeMode.INLINE,
                body="never abandons the goal",
                severity=DriftSeverity.WARNING,
            ),
            JudgeSpec(
                name="no_pii",
                mode=JudgeMode.PYTHON,
                body="myproj.judges.pii",
                severity=DriftSeverity.CRITICAL,
            ),
        ),
    )
    entry.validate()
    board_file = tmp_path / "board.jsonl"
    save_board([entry], board_file)

    # The judges array serialises as enum tokens.
    row = json.loads(board_file.read_text(encoding="utf-8").strip())
    assert row["judges"][0]["mode"] == "inline"
    assert row["judges"][0]["severity"] == "warning"
    assert row["judges"][1]["mode"] == "python"

    parsed = load_board(board_file)
    assert parsed == [entry]
    assert parsed[0].judges[0].mode is JudgeMode.INLINE
    assert parsed[0].judges[0].severity is DriftSeverity.WARNING


def test_save_board_round_trips_disable_drift(tmp_path: Path) -> None:
    """Board-level ``disable_drift`` round-trips via the ``board_meta`` header."""
    entry = BoardEntry(id="e", kind="single_turn", wall_clock_budget_seconds=30, input="i")
    board_file = tmp_path / "board.jsonl"
    disable = (DriftKind.OFF_TOPIC, DriftKind.TOOL_ERROR)
    save_board([entry], board_file, disable_drift=disable)

    # First line is the board_meta header carrying enum tokens.
    first_line = board_file.read_text(encoding="utf-8").splitlines()[0]
    header = json.loads(first_line)
    assert header["board_meta"] is True
    assert header["disable_drift"] == ["off_topic", "tool_error"]

    entries, parsed_disable = load_board_with_meta(board_file)
    assert entries == [entry]
    assert parsed_disable == disable


def test_save_board_no_header_when_disable_drift_empty(tmp_path: Path) -> None:
    """A board with no ``disable_drift`` writes no header line."""
    entry = BoardEntry(id="e", kind="single_turn", wall_clock_budget_seconds=30, input="i")
    board_file = tmp_path / "board.jsonl"
    save_board([entry], board_file)
    lines = board_file.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert "board_meta" not in lines[0]


def test_save_board_emits_only_kind_relevant_keys(tmp_path: Path) -> None:
    """Single-turn rows must not contain 'turns' or 'user_persona' keys."""
    entry = BoardEntry(
        id="single_one",
        kind="single_turn",
        wall_clock_budget_seconds=60,
        input="hello",
    )
    board_file = tmp_path / "board.jsonl"
    save_board([entry], board_file)
    line = board_file.read_text(encoding="utf-8").strip()
    row = json.loads(line)
    assert "input" in row
    assert "turns" not in row
    assert "user_persona" not in row
    assert "max_turns" not in row
    # Default fields suppressed
    assert "weight" not in row
    assert "tags" not in row
    assert "context" not in row
    assert "judges" not in row


def test_save_board_rejects_duplicate_ids(tmp_path: Path) -> None:
    entry = BoardEntry(
        id="x",
        kind="single_turn",
        wall_clock_budget_seconds=30,
        input="i",
    )
    with pytest.raises(ValueError, match="duplicate"):
        save_board([entry, entry], tmp_path / "board.jsonl")


def test_append_entry_creates_file(tmp_path: Path) -> None:
    board_file = tmp_path / "board.jsonl"
    entry = BoardEntry(
        id="first",
        kind="single_turn",
        wall_clock_budget_seconds=30,
        input="i",
    )
    append_entry(board_file, entry)
    parsed = load_board(board_file)
    assert parsed == [entry]


def test_append_entry_rejects_duplicate(tmp_path: Path) -> None:
    board_file = tmp_path / "board.jsonl"
    entry = BoardEntry(
        id="dup",
        kind="single_turn",
        wall_clock_budget_seconds=30,
        input="i",
    )
    append_entry(board_file, entry)
    with pytest.raises(ValueError, match="already exists"):
        append_entry(board_file, entry)


def test_remove_entry_drops_matching_row(tmp_path: Path) -> None:
    board_file = tmp_path / "board.jsonl"
    a = BoardEntry(id="a", kind="single_turn", wall_clock_budget_seconds=30, input="i")
    b = BoardEntry(id="b", kind="single_turn", wall_clock_budget_seconds=30, input="j")
    save_board([a, b], board_file)
    remove_entry(board_file, "a")
    parsed = load_board(board_file)
    assert parsed == [b]


def test_remove_entry_preserves_disable_drift_header(tmp_path: Path) -> None:
    """Removing an entry keeps the board-level ``disable_drift`` header."""
    board_file = tmp_path / "board.jsonl"
    a = BoardEntry(id="a", kind="single_turn", wall_clock_budget_seconds=30, input="i")
    b = BoardEntry(id="b", kind="single_turn", wall_clock_budget_seconds=30, input="j")
    disable = (DriftKind.BLOCKED,)
    save_board([a, b], board_file, disable_drift=disable)
    remove_entry(board_file, "a")
    entries, parsed_disable = load_board_with_meta(board_file)
    assert [e.id for e in entries] == ["b"]
    assert parsed_disable == disable


def test_remove_entry_raises_when_id_missing(tmp_path: Path) -> None:
    board_file = tmp_path / "board.jsonl"
    save_board(
        [
            BoardEntry(
                id="only",
                kind="single_turn",
                wall_clock_budget_seconds=30,
                input="i",
            )
        ],
        board_file,
    )
    with pytest.raises(ValueError, match="no entry with id"):
        remove_entry(board_file, "missing")
