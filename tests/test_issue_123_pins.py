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

Every pin is ``xfail(strict=True)``: it must fail today and the marker must be
removed by whoever lands the fix.
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


@pytest.mark.xfail(
    strict=True,
    reason="issue #123: journal.py:126 stores only _first_sentence(why); "
    "the remaining ~93% of the proposer's reasoning never reaches read_journal",
)
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


@pytest.mark.xfail(
    strict=True,
    reason="issue #123: journal.py:117 keeps only splitlines()[0] of core_idea",
)
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


@pytest.mark.xfail(
    strict=True,
    reason="issue #123 budget companion: the read_journal proposer tool returns "
    "journal.md uncapped, so de-truncating the write side must be paired with a "
    "render-side cap (ADJUDICATION: cap value + whether it belongs on the tool)",
)
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
