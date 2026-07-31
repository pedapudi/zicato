"""Triage pins for issue #123 — the journal truncates the proposer's own reasoning.

``zicato.epoch.journal._render_section`` truncates at WRITE time:

* ``why`` is reduced to its first sentence (``_first_sentence``, journal.py:126);
* ``core_idea`` is reduced to its first physical line (journal.py:117).

``experiment.json`` keeps both fields in full, but no proposer tool exposes
that record: ``DEFAULT_PROPOSER_TOOLS`` reaches prior reasoning only through
``read_journal``, which returns the already-truncated ``journal.md`` verbatim.
So the loss is permanent on the one channel that still carries a rejection
reason when nothing is promoting.

This is NOT the code path issue #107 fixed. #107 rewrote
``zicato.query.epoch_view._distill_brief_goal`` (the epoch BRIEF's goal line,
for the dashboard / analyzer masthead). #123 is the per-generation experiment
record in ``zicato.epoch.journal``. Different module, different input,
different consumer — only the shape rhymes.

FIXED: ``_render_section`` now records ``why`` and ``core_idea`` in full,
and the budget moved to the reader — ``proposer.tools.read_journal`` caps
at ``_JOURNAL_LIMIT_CHARS`` keeping the NEWEST entries. Entries written
before the fix stay truncated on disk; the journal is append-only and
there is nothing left to recover them from.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from zicato.core.types import (
    ExpectedDriftMovement,
    Experiment,
    HypothesisSpec,
    Patch,
)
from zicato.epoch import append_journal_entry, read_journal

# A ``why`` whose first sentence is a 24-character topic sentence and whose
# remaining ~93% carries the three findings the next round actually needs.
_TOPIC_SENTENCE = "This is a baseline round"
_FINDINGS = (
    "Three findings emerged: the router relays off-topic preambles in 12 of "
    "40 entries; the summariser drops its final clause under budget pressure "
    "in 7 of 40; and the judge disagrees with the rubric on tone in 5 of 40. "
    "Each finding is supported by the transcript counts above."
)
_LONG_WHY = f"{_TOPIC_SENTENCE}. {_FINDINGS}"

_MULTILINE_CORE_IDEA = (
    "Tighten the router's instruction to stop relaying off-topic preambles.\n"
    "Also drop the redundant restatement clause the summariser echoes."
)


def _experiment(
    *,
    generation_id: str = "v1",
    core_idea: str = "Tighten the router's instruction.",
    why: str = _LONG_WHY,
) -> Experiment:
    hypothesis = HypothesisSpec(
        core_idea=core_idea,
        modulating=("router.instruction",),
        why=why,
        expected_drift_movements=(
            ExpectedDriftMovement(kind="off_topic", direction="decrease", magnitude="medium"),
        ),
        expected_pass_rate_delta="+0.0 to +0.15",
        risks="May under-route genuinely ambiguous requests.",
    )
    patches = (
        Patch(
            id="p1",
            mutation_id="router.instruction",
            op="replace",
            new_content="new instruction",
            new_numeric=None,
            new_enum=None,
            rationale="stop relaying preambles",
        ),
    )
    return Experiment(
        id=f"exp_issue123_{generation_id}",
        epoch_id="2026-07-31_issue123",
        generation_id=generation_id,
        parent_generation_id="v0",
        proposed_at="2026-07-31T12:00:00+00:00",
        hypothesis=hypothesis,
        patches=patches,
        outcome=None,
    )


@pytest.fixture
def epoch_root(tmp_path: Path) -> tuple[Path, str]:
    ws = tmp_path / ".zicato"
    epoch_id = "2026-07-31_issue123"
    (ws / "epochs" / epoch_id).mkdir(parents=True)
    return ws, epoch_id


def test_journal_preserves_the_whole_why(epoch_root: tuple[Path, str]) -> None:
    """The full ``why`` must survive into ``journal.md``, not just sentence one.

    Truncation belongs at RENDER (a pager, the dashboard, an analysis prompt
    with its own char budget), never at write: ``journal.md`` is the only
    durable surface the proposer can read its own prior reasoning from.
    """
    ws, eid = epoch_root
    append_journal_entry(ws, eid, _experiment())
    text = read_journal(ws, eid)

    assert _TOPIC_SENTENCE in text
    assert "the router relays off-topic preambles in 12 of 40 entries" in text
    assert "the judge disagrees with the rubric on tone in 5 of 40" in text


def test_journal_preserves_a_multi_line_core_idea(epoch_root: tuple[Path, str]) -> None:
    """A ``core_idea`` spanning two lines must not lose its second line.

    The heading may legitimately stay one line — what must not happen is the
    second line vanishing from the record entirely.
    """
    ws, eid = epoch_root
    append_journal_entry(ws, eid, _experiment(core_idea=_MULTILINE_CORE_IDEA))
    text = read_journal(ws, eid)

    assert "Tighten the router's instruction" in text
    assert "redundant restatement clause the summariser echoes" in text


def test_read_journal_tool_caps_its_return() -> None:
    """The proposer-facing ``read_journal`` must bound what it returns.

    Every other unbounded proposer tool already does this
    (``_PARENT_DIFF_LIMIT_CHARS`` = 20_000 with an explicit truncation note),
    and both journal-consuming LLM paths cap independently
    (``epoch.analysis._MAX_JOURNAL_CHARS`` = 60_000,
    ``analyzer.report_data._MAX_JOURNAL_CHARS`` = 40_000). ``read_journal`` is
    the one uncapped reader, which is exactly the surface that grows when the
    write-side truncation above is removed.
    """
    from zicato.proposer import tools

    limit = getattr(tools, "_JOURNAL_LIMIT_CHARS", None)
    assert isinstance(limit, int) and limit > 0


def test_read_journal_cap_keeps_the_newest_whole_entries() -> None:
    """The cap must be TAIL-biased and land on an entry boundary.

    The journal is chronological-append, so the entries a proposer needs
    are the most recent ones. Cutting the head off mid-section would hand
    the model a sentence fragment as its oldest context, so the kept text
    starts at a ``## `` heading and carries a note saying what was dropped.
    """
    from zicato.proposer.tools import _tail_entries

    entries = "".join(f"## v{n} — idea {n}\n\n**why**: {'w' * 200}\n\n" for n in range(60))
    kept = _tail_entries(entries, 2_000)

    assert kept != entries
    assert "## v59 — idea 59" in kept  # newest survives
    assert "## v0 — idea 0" not in kept  # oldest dropped
    body = kept.split("\n\n", 1)[1]
    assert body.startswith("## v")  # whole entry, not a fragment
    assert kept.startswith("[... truncated:")


def test_read_journal_cap_leaves_a_short_journal_untouched() -> None:
    """Under the cap, the text is returned byte-identical — note and all absent."""
    from zicato.proposer.tools import _tail_entries

    short = "## v1 — idea\n\n**why**: because\n"
    assert _tail_entries(short, 2_000) == short


def test_a_multi_line_field_does_not_swallow_the_field_after_it() -> None:
    """A multi-line body must be fenced by a blank line on BOTH sides.

    Regression guard. The first cut of the de-truncation rendered a
    multi-line value as ``**core_idea**:\\n\\n<body>`` with no trailing
    blank line, so the next field sat one line break below the body and
    every markdown renderer folded it into the same paragraph — the
    journal displayed ``line idea **why**: because`` as one run of prose.
    """
    from zicato.epoch.journal import _render_section

    section = _render_section(
        _experiment(core_idea="multi\nline idea", why="because", generation_id="v2")
    )

    assert "**core_idea**:\n\nmulti\nline idea\n\n**why**: because" in section


def test_the_cap_does_not_open_the_window_on_prose_that_looks_like_a_heading() -> None:
    """A ``## `` line inside a ``why`` must not be mistaken for an entry boundary.

    The two halves of issue #123 meet here: the write side now records the
    proposer's prose verbatim, so a hypothesis about a markdown task puts a
    real ``## Approach`` line in ``journal.md``. If the reader's cap anchors
    on a bare ``\\n## `` it opens the proposer's window mid-body, handing the
    next proposer a fragment of someone's reasoning dressed as run history.
    """
    from zicato.epoch.journal import _render_section
    from zicato.proposer.tools import _tail_entries

    journal = "".join(
        _render_section(
            _experiment(
                core_idea=f"idea {n}",
                why="The board wants\n## Approach\nsections graded. " + "z" * 120,
                generation_id=f"v{n}",
            )
        )
        for n in range(1, 12)
    )
    kept = _tail_entries(journal, 900)

    assert kept.startswith("[... truncated:")
    opening_line = kept.split("\n\n", 1)[1].split("\n", 1)[0]
    assert opening_line.startswith("## v"), opening_line
    assert opening_line != "## Approach"
