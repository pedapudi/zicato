"""Triage pin for the truncated epoch objective (issue #107).

``zicato.query.epoch_view._distill_brief_goal`` used to return the first
PHYSICAL LINE of the ``## Goal`` section while its docstring promised a
sentence. Every shipped example brief is hard-wrapped, so the dashboard's
objective callout rendered truncated out of the box — sometimes mid-word, at
a dangling hyphen. It now accumulates the whole first prose PARAGRAPH,
joining hard-wrapped lines hyphen-aware and stopping at the next block.

Display-only: no effect on scoring, gating or promotion.
"""

from __future__ import annotations

from zicato.query.epoch_view import _distill_brief_goal

# The shipped presentation brief, verbatim: wrapped at ~74 columns with a
# hyphen-split word across the break ("multi-" / "agent").
_HARD_WRAPPED_BRIEF = """# Epoch e0 — presentation agent baseline

## Goal

Produce coherent, structured presentation outputs from the vendored multi-
agent tree in `agent/`. Specifically:

- Final outputs should describe a presentation in slide-shaped chunks.

## Preferred edits
"""


def test_hard_wrapped_goal_joins_the_whole_paragraph_hyphen_aware() -> None:
    """The paragraph must be reassembled, with hyphen-aware joining.

    A naive ``" ".join(lines)`` would produce ``"multi- agent"``; the join
    must close a hard-wrapped word instead.
    """
    goal = _distill_brief_goal(_HARD_WRAPPED_BRIEF)
    assert goal is not None
    assert "multi-agent" in goal
    assert "multi- agent" not in goal
    assert goal.startswith("Produce coherent, structured presentation outputs")


def test_accumulation_stops_at_the_next_block() -> None:
    """A blank line, heading or list item closes the paragraph."""
    goal = _distill_brief_goal(_HARD_WRAPPED_BRIEF)
    assert goal is not None
    assert "slide-shaped" not in goal, "the bullet list must not be absorbed"
    assert "Specifically" in goal, "the paragraph's own tail must survive"


def test_soft_wrapped_goal_is_unchanged() -> None:
    """The already-correct case the #107 fix must leave byte-identical."""
    brief = "## Goal\n\nMake the agent stay on topic.\n\n## Style\n"
    assert _distill_brief_goal(brief) == "Make the agent stay on topic."


def test_no_goal_section_still_returns_none() -> None:
    assert _distill_brief_goal("## Style\n\nBe terse.\n") is None
    assert _distill_brief_goal("") is None


def test_publication_masthead_distils_the_same_goal_as_the_dashboard() -> None:
    """The analyzer's masthead goal must not be a second implementation.

    ``analyzer.report_data`` carried its own copy of this distillation, which
    is how #107 outlived its first fix: the dashboard learned to reassemble a
    wrapped paragraph while the publication masthead still rendered the
    shipped goal cut mid-word at the dangling hyphen.
    """
    from pathlib import Path

    from zicato.analyzer.report_data import _distill_brief_goal as _masthead_goal

    shipped = Path(__file__).resolve().parents[1] / (
        "examples/zicato_examples/target_1_presentation/rubric.md"
    )
    brief = shipped.read_text(encoding="utf-8")

    dashboard = _distill_brief_goal(brief)
    assert dashboard is not None
    assert _masthead_goal(brief) == dashboard
    # The symptom itself: neither surface may end at the wrap hyphen.
    assert not dashboard.endswith("multi-")
    assert "multi-agent" in dashboard


def test_a_numbered_list_is_a_block_not_prose() -> None:
    """An ordered list is a list — accumulating it yields a run-on sentence."""
    brief = "## Goal\n\n1. first item\n2. second item\n\n## Style\n"
    assert _distill_brief_goal(brief) is None

    closed = "## Goal\n\nHold the line.\n1. first item\n\n## Style\n"
    assert _distill_brief_goal(closed) == "Hold the line."


def test_a_hyphen_used_as_punctuation_is_not_a_wrapped_word() -> None:
    """Only a hyphen a word character precedes closes across the wrap."""
    punct = "## Goal\n\nreach for this -\nnamely speed.\n\n## Style\n"
    assert _distill_brief_goal(punct) == "reach for this - namely speed."

    wrapped = "## Goal\n\nreach for well-\nknown speed.\n\n## Style\n"
    assert _distill_brief_goal(wrapped) == "reach for well-known speed."
