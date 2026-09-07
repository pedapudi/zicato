"""Goal extraction preserves Markdown paragraphs and view preview behavior."""

from __future__ import annotations

from pathlib import Path

import pytest

from zicato.analyzer.report_data import gather_epoch_report_data
from zicato.proposer.brief import brief_goal
from zicato.query.epoch_view import build_epochs_summary
from zicato.query.paths import WorkspacePaths

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
    goal = brief_goal(_HARD_WRAPPED_BRIEF)
    assert goal is not None
    assert "multi-agent" in goal
    assert "multi- agent" not in goal
    assert goal.startswith("Produce coherent, structured presentation outputs")


def test_accumulation_stops_at_the_next_block() -> None:
    """A blank line, heading or list item closes the paragraph."""
    goal = brief_goal(_HARD_WRAPPED_BRIEF)
    assert goal is not None
    assert "slide-shaped" not in goal, "the bullet list must not be absorbed"
    assert "Specifically" in goal, "the paragraph's own tail must survive"


def test_soft_wrapped_goal_is_unchanged() -> None:
    """The already-correct case the #107 fix must leave byte-identical."""
    brief = "## Goal\n\nMake the agent stay on topic.\n\n## Style\n"
    assert brief_goal(brief) == "Make the agent stay on topic."


def test_no_goal_section_still_returns_none() -> None:
    assert brief_goal("## Style\n\nBe terse.\n") is None
    assert brief_goal("") is None


@pytest.mark.parametrize(
    ("brief", "expected"),
    [
        ("## Goal\n\n" + "x" * 125, "x" * 120 + "..."),
        ("## Goal\n\nKeep multi-\nagent behavior.\n", "Keep multi-agent behavior."),
        ("## Style\n\nBe terse.\n", None),
    ],
)
def test_report_and_epoch_summary_preserve_goal_previews(
    tmp_path: Path, brief: str, expected: str | None
) -> None:
    epoch = tmp_path / ".zicato" / "epochs" / "measured"
    epoch.mkdir(parents=True)
    (epoch / "brief.md").write_text(brief, encoding="utf-8")
    (epoch / "config.json").write_text('{"id": "measured"}', encoding="utf-8")
    root = epoch.parent.parent
    assert build_epochs_summary(WorkspacePaths(root)) == [
        {"epoch_id": "measured", "goal": expected}
    ]
    assert gather_epoch_report_data(root, "measured").goal == (expected or "")


def test_goal_extraction_preserves_text_beyond_the_display_limit() -> None:
    paragraph = "A" * 140
    assert brief_goal("## Goal\n\n" + paragraph) == paragraph


def test_a_numbered_list_is_a_block_not_prose() -> None:
    """An ordered list is a list — accumulating it yields a run-on sentence."""
    brief = "## Goal\n\n1. first item\n2. second item\n\n## Style\n"
    assert brief_goal(brief) is None

    closed = "## Goal\n\nHold the line.\n1. first item\n\n## Style\n"
    assert brief_goal(closed) == "Hold the line."


def test_a_hyphen_used_as_punctuation_is_not_a_wrapped_word() -> None:
    """Only a hyphen a word character precedes closes across the wrap."""
    punct = "## Goal\n\nreach for this -\nnamely speed.\n\n## Style\n"
    assert brief_goal(punct) == "reach for this - namely speed."

    wrapped = "## Goal\n\nreach for well-\nknown speed.\n\n## Style\n"
    assert brief_goal(wrapped) == "reach for well-known speed."
