"""Pins for issue #120 — the parsimony term charges the file, not the edit.

:func:`zicato.scoring.diff_complexity.diff_size` counts ``added`` as the line
count of each patch's ``new_content`` and hard-codes ``removed`` to ``0``. For a
``kind="span"`` mutation point the replacement really is the edit, so that is
roughly right. For a ``kind="file"`` point the replacement is the WHOLE FILE:
every proposal re-emits the template it was required to preserve and pays for
all of it, turning
:attr:`~zicato.core.ScoringWeights.diff_complexity_weight` into a flat toll on
proposing at all.

The term is opt-in (``diff_complexity_weight`` defaults to ``0.0``), so this
bites only contracts that turned parsimony on — but for those it can exceed
``promote_margin`` by two orders of magnitude and reads as an honest regression.

Every experiment here is built with the real
:class:`~zicato.core.types.Experiment` / :class:`~zicato.core.types.Patch`
signatures and scored through the real
:func:`~zicato.scoring.builtins.diff_complexity_component`, so a pin fails on
the number the loop would actually charge.
"""

from __future__ import annotations

import pytest

from zicato.core import DriftCount, LossProfile, ScoringWeights
from zicato.core.types import Experiment, HypothesisSpec, Patch
from zicato.scoring.builtins import diff_complexity_component
from zicato.scoring.diff_complexity import diff_size
from zicato.tournament.gate import evaluate_gate
from zicato.tournament.scoring import aggregate_generation_score

#: The 37-line whole-file mutation point from the issue. A ``kind="file"``
#: point hands the proposer the entire file and takes the entire file back, so
#: this is what ``new_content`` looks like for ANY proposal against it —
#: including one that changes nothing.
_TEMPLATE_LINES: tuple[str, ...] = tuple(f"template line {i}" for i in range(37))
_TEMPLATE = "\n".join(_TEMPLATE_LINES)

#: The issue's configuration.
_WEIGHTS = ScoringWeights(
    drift_weight=0.0,
    pass_weight=1.0,
    promote_margin=0.01,
    diff_complexity_weight=0.02,
)


def _experiment(*contents: str) -> Experiment:
    """A real experiment whose patches replace a whole-file mutation point."""
    return Experiment(
        id="exp",
        epoch_id="e0",
        generation_id="v1",
        parent_generation_id="v0",
        proposed_at="2026-07-29T00:00:00Z",
        hypothesis=HypothesisSpec(
            core_idea="rewrite the template",
            modulating=(),
            why="the board asks for it",
            expected_drift_movements=(),
            expected_pass_rate_delta="+0.0",
        ),
        patches=tuple(
            Patch(
                id=f"p{i}",
                mutation_id="whole_file_point",
                op="replace",
                new_content=content,
                new_numeric=None,
                new_enum=None,
                rationale="r",
            )
            for i, content in enumerate(contents)
        ),
        outcome=None,
    )


def _loss(entry_id: str, *, passed: bool) -> LossProfile:
    return LossProfile(
        run_id=f"run-{entry_id}",
        entry_id=entry_id,
        generation_id="v1",
        epoch_id="e0",
        drift_counts=(DriftCount(kind="off_topic", severity="info", count=0),),
        plan_revisions=0,
        task_failure_ratio=0.0,
        runtime_ms=1000,
        wall_clock_budget_exceeded=False,
        expectation_result=None,
        drift_loss=0.0,
        pass_fail=passed,
        score=None,
        metrics=None,
    )


@pytest.mark.xfail(
    strict=True,
    reason=(
        "issue #120: diff_size counts every line of the replacement, so a "
        "whole-file patch that re-emits the parent verbatim is charged for the "
        "entire file (0.76 at diff_complexity_weight=0.02) instead of for the "
        "zero lines it actually changed"
    ),
)
def test_verbatim_whole_file_reemit_costs_nothing() -> None:
    """Re-emitting the parent's file unchanged is an empty edit and must be free.

    This is the pure toll: the proposal alters nothing, so its description
    length relative to the parent is zero and MDL charges zero. Today it is
    charged ``0.02 * (37 added + 1 patch) == 0.76`` — 76x the contract's
    ``promote_margin`` of 0.01 — purely for the template it was required to
    keep.
    """
    charge = diff_complexity_component(_WEIGHTS, diff_size(_experiment(_TEMPLATE)))
    assert charge is not None, "precondition: the parsimony term is active in this contract"
    assert charge <= _WEIGHTS.promote_margin


@pytest.mark.xfail(
    strict=True,
    reason=(
        "issue #120: diff_size hard-codes removed=0, so an edit that deletes "
        "lines reports no removals and the MDL proxy cannot see deletion cost"
    ),
)
def test_deleting_lines_from_a_whole_file_point_reports_removed_lines() -> None:
    """A whole-file patch that drops 20 of 37 lines has to report removals.

    The replacement keeps the first 17 lines and drops the rest. ``added``
    should then be 0 and ``removed`` 20; today the size reads
    ``{"added": 17, "removed": 0}`` — the exact inversion, charging for the
    lines that survived and nothing for the ones destroyed.
    """
    shrunk = "\n".join(_TEMPLATE_LINES[:17])
    assert diff_size(_experiment(shrunk))["removed"] == 20


@pytest.mark.xfail(
    strict=True,
    reason=(
        "issue #120(b): the insufficient-improvement reason reports only the "
        "total delta, so a rejection caused entirely by the parsimony toll is "
        "worded identically to a genuine regression on the board"
    ),
)
def test_rejection_reason_decomposes_the_delta_by_component() -> None:
    """The reason must name the components, so a parsimony toll is legible.

    A challenger that wins two entries out of twelve (``-0.1667`` on the pass
    component) but pays ``+0.76`` for re-emitting the template nets ``+0.5933``
    and is rejected. The message says only that the loss rose — the operator
    cannot tell from it that the board improved and the toll swamped it.
    """
    parent = aggregate_generation_score(
        [_loss(f"e{i}", passed=(i >= 5)) for i in range(12)], _WEIGHTS
    )
    child = aggregate_generation_score(
        [_loss(f"e{i}", passed=(i >= 3)) for i in range(12)],
        _WEIGHTS,
        diff_size(_experiment(_TEMPLATE)),
    )
    outcome = evaluate_gate(parent, child, _WEIGHTS)
    assert outcome.decision == "rejected", "precondition: the toll rejects a real two-entry win"
    assert "diff_complexity" in outcome.reason
