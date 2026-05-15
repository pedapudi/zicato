"""Tests for the answer-leak heuristic.

The heuristic is intentionally trigger-happy. These tests pin the
patterns that MUST fire and a small set of clean-prose negatives that
must not.
"""

from __future__ import annotations

import pytest

from zicato.emulator.answer_leak import LEAK_PATTERNS, check_answer_leak


# ---------------------------------------------------------------------------
# Positive cases — leakage MUST be flagged.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Sure, the answer is 42.",
        "I think you should output a JSON like below.",
        "The correct output is: hello world",
        "I expected output similar to this.",
        "The schema is { ... }.",
        "```\nprint('hi')\n```",
        "```json\n{\"k\": 1}\n```",
        '{"goal": "x"}',
        "[1, 2, 3]",
        "  {\n  \"answer\": 1\n}",
    ],
)
def test_check_answer_leak_flags_obvious_leakage(text: str) -> None:
    result = check_answer_leak(text)
    assert result is not None, f"expected leak detection for {text!r}"


def test_check_answer_leak_is_case_insensitive() -> None:
    assert check_answer_leak("THE ANSWER IS 7") is not None
    assert check_answer_leak("You Should Output something") is not None


def test_check_answer_leak_pattern_names_in_message() -> None:
    msg = check_answer_leak("the answer is 7")
    assert msg is not None
    assert "the answer is" in msg


# ---------------------------------------------------------------------------
# Negative cases — clean prose MUST pass.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Hi, I'm not sure what you mean — could you explain?",
        "I just want a laptop for travel. I haven't picked a budget yet.",
        "Honestly, I don't know. What do you recommend?",
        "Could you tell me more before I decide?",
        "Yes please, that sounds good. I'll go with that one.",
    ],
)
def test_check_answer_leak_passes_clean_prose(text: str) -> None:
    assert check_answer_leak(text) is None


def test_leak_patterns_export_is_a_tuple_of_strings() -> None:
    # Pinning the export shape — downstream callers (e.g. the driver)
    # treat it as a tuple of regex strings.
    assert isinstance(LEAK_PATTERNS, tuple)
    for pat in LEAK_PATTERNS:
        assert isinstance(pat, str)
    # Sanity: the core set we promised in the contract is there.
    for required in (r"```", r"the answer is", r"you should output"):
        assert required in LEAK_PATTERNS
