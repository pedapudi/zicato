"""Tests for the answer-leak heuristic.

The heuristic targets genuine answer-shape content (JSON structures,
code fences, explicit answer-disclosure phrases).  It must NOT fire on
benign formatting that reasoning models legitimately produce, such as
bracketed prefaces (``[Thinking about this]``) or markdown lists.
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
        '```json\n{"k": 1}\n```',
        '{"goal": "x"}',
        "[1, 2, 3]",
        '  {\n  "answer": 1\n}',
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
# JSON-array leak: genuine JSON arrays MUST still be caught.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        # Numeric arrays — direct answer leakage
        "[1, 2, 3]",
        "  [42]",
        "[-1, -2, -3]",
        # String arrays — option lists or expected output
        '["option_a", "option_b"]',
        # Object arrays — structured answer keys
        '[{"answer": "yes"}, {"answer": "no"}]',
        # Boolean / null arrays
        "[true, false]",
        "[null]",
        # Nested arrays
        "[[1, 2], [3, 4]]",
        # Array embedded after prose (multiline: array on its own line)
        'Here is what you should return:\n["foo", "bar"]',
    ],
)
def test_json_array_leak_still_caught(text: str) -> None:
    """A genuine JSON-array-shaped line must still trigger the heuristic."""
    result = check_answer_leak(text)
    assert result is not None, f"expected leak detection for JSON array {text!r}"


# ---------------------------------------------------------------------------
# Bracketed-preface false-positive regression.
#
# Reasoning models commonly produce output that starts with a bracketed
# phrase (e.g. "[Looking at this slide]") followed by normal prose.
# The heuristic must NOT fire on these inputs.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        # Picky-stakeholder persona — the specific case that caused the
        # live-run false positive.
        "[Looking at your latest draft] The framing on slide 3 feels too vague.",
        "[Reviewing the deck] I need concrete Q3 revenue numbers, not estimates.",
        "[Considering the metrics] Could you add the YoY comparison?",
        # Generic bracketed prefaces a reasoning model might produce
        "[Thinking through this carefully] I need more information first.",
        "[Picky stakeholder] Could you be more specific about the KPIs?",
        "[Polite but direct] That slide still doesn't address my earlier feedback.",
        # Leading bracket that closes before end-of-line, all prose after
        "[Note] The deck looks better now, but slide 5 still needs work.",
        # Multi-line: bracketed preface on first line, no JSON anywhere
        "[Reviewing] Slide 2 is good.\nSlide 3 still needs revision.\nPlease update.",
    ],
)
def test_bracketed_preface_does_not_false_positive(text: str) -> None:
    """Bracketed human-style prefaces must not trigger the leak heuristic."""
    result = check_answer_leak(text)
    assert result is None, (
        f"false positive: heuristic incorrectly flagged a benign bracketed "
        f"preface.\nInput: {text!r}\nResult: {result!r}"
    )


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
