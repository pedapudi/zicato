"""Pins for issue #130 — attributable per-entry regression is not gated.

Rules 2 and 3 of :func:`zicato.tournament.gate.evaluate_gate` cover part of the
issue's ask already, so these pins target only the residual — the regression
shapes that survive both rules and reach a PROMOTED verdict, permanently baking
the loss into the lineage:

* under ``pass_rate_monotonicity_scope="aggregate"``, a net-positive challenger
  may trade entries freely, which is the documented purpose of that scope but
  also exactly the masking the issue describes;
* under the DEFAULT ``per_entry`` scope, the rule reads only
  :func:`~zicato.tournament.gate._row_score` — the entry's ``score`` /
  ``pass_fail``. The per-entry row also carries ``drift_loss``, and nothing
  reads it, so an entry whose quality collapses while it still PASSES regresses
  invisibly. Rule 3 cannot catch it either: it compares per-namespace MEANS, so
  an improvement elsewhere hides it.

Both pins assert on the ``GateOutcome.attributable_regressions`` field rather
than on ``reason``. That is deliberate: the empty-reason-on-promote invariant
is load-bearing for consumers that treat a non-empty reason as a rejection, so
a warn-by-default report must not travel in ``reason`` — and the first pin
keeps asserting that ``reason`` is still exactly empty on the promotion.

Aggregates come from the real :func:`~zicato.tournament.scoring.aggregate_generation_score`
over :class:`~zicato.core.LossProfile` rows.
"""

from __future__ import annotations

from typing import Any

import pytest

from zicato.core import DriftCount, LossProfile, ScoringWeights
from zicato.tournament.gate import evaluate_gate
from zicato.tournament.scoring import aggregate_generation_score


def _loss(entry_id: str, *, passed: bool, drift_loss: float = 0.0) -> LossProfile:
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
        drift_loss=drift_loss,
        pass_fail=passed,
        score=None,
        metrics=None,
    )


def _regressions(outcome: Any) -> tuple[str, ...] | None:
    """The proposed per-entry regression report, or ``None`` when absent."""
    reported = getattr(outcome, "attributable_regressions", None)
    return None if reported is None else tuple(reported)


def test_promotion_that_breaks_an_entry_names_it_under_aggregate_scope() -> None:
    """Netting +3 while breaking e0 must still report e0.

    The parent passes e0 and e4; the child passes everything except e0. Net
    pass-rate moves 2/5 -> 4/5, so Rule 1 clears and the aggregate scope
    deliberately permits the trade. The verdict is correct — the issue does not
    dispute it — but e0 has now failed permanently and nothing says so.
    """
    weights = ScoringWeights(
        drift_weight=1.0,
        pass_weight=1.0,
        promote_margin=0.01,
        pass_rate_monotonicity_scope="aggregate",
    )
    parent = aggregate_generation_score(
        [
            _loss("e0", passed=True),
            _loss("e1", passed=False),
            _loss("e2", passed=False),
            _loss("e3", passed=False),
            _loss("e4", passed=True),
        ],
        weights,
    )
    child = aggregate_generation_score(
        [
            _loss("e0", passed=False),
            _loss("e1", passed=True),
            _loss("e2", passed=True),
            _loss("e3", passed=True),
            _loss("e4", passed=True),
        ],
        weights,
    )
    outcome = evaluate_gate(parent, child, weights)
    assert outcome.decision == "promoted", "precondition: the aggregate trade promotes"
    assert outcome.reason == "", "the empty-reason-on-promote invariant must hold"
    assert _regressions(outcome) == ("e0",)


def test_per_entry_quality_collapse_on_a_still_passing_entry_is_reported() -> None:
    """A 6x drift blowup on e0, masked by an improvement on e1, must be reported.

    This one survives the DEFAULT ``per_entry`` scope, so it is not a
    scope-choice tradeoff — it is a blind spot. Both entries pass under both
    generations, so ``_row_score`` returns ``1.0`` on all four sides and Rule 2
    sees nothing. The drift namespace mean falls 0.50 -> 0.40, so Rule 3 sees an
    improvement. The scalar falls 0.50 -> 0.40, so Rule 1 promotes. Meanwhile
    e0's own drift loss went from 0.10 to 0.60 and is now permanent.

    The per-entry row already carries ``drift_loss`` on both sides — the
    evidence is in hand and simply unread.
    """
    weights = ScoringWeights(drift_weight=1.0, pass_weight=1.0, promote_margin=0.01)
    parent = aggregate_generation_score(
        [
            _loss("e0", passed=True, drift_loss=0.10),
            _loss("e1", passed=True, drift_loss=0.90),
        ],
        weights,
    )
    child = aggregate_generation_score(
        [
            _loss("e0", passed=True, drift_loss=0.60),
            _loss("e1", passed=True, drift_loss=0.20),
        ],
        weights,
    )
    outcome = evaluate_gate(parent, child, weights)
    assert outcome.decision == "promoted", "precondition: the masked trade promotes"
    assert parent["per_entry"]["e0"]["drift_loss"] == pytest.approx(0.10)
    assert child["per_entry"]["e0"]["drift_loss"] == pytest.approx(0.60)
    assert _regressions(outcome) == ("e0",)
