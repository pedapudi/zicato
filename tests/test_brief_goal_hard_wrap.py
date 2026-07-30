"""Triage pin for the truncated epoch objective (issue #107).

``zicato.query.epoch_view._distill_brief_goal`` documents "the summary is
always a sentence" but returns the first PHYSICAL LINE of the ``## Goal``
section. Every shipped example brief is hard-wrapped, so the dashboard's
objective callout renders truncated out of the box — sometimes mid-word, at
a dangling hyphen.

Display-only: no effect on scoring, gating or promotion.
"""

from __future__ import annotations

import pytest

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


@pytest.mark.xfail(
    strict=True,
    reason=(
        "issue #107: _distill_brief_goal returns the first physical line, so a "
        "hard-wrapped goal renders truncated mid-word"
    ),
)
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


@pytest.mark.xfail(
    strict=True,
    reason="issue #107: accumulation must stop at the list item, not run into it",
)
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
