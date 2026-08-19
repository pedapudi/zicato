"""Pins for issue #118 — the holdout needs its own bounds, not the train knob.

The gate used to read ONE knob for two decisions calibrated against two
different slice sizes:

* :func:`zicato.tournament.gate.evaluate_gate` Rule 1 thresholds the TRAIN
  delta at ``weights.promote_margin``;
* :func:`zicato.tournament.gate._holdout_confirms` thresholded the HOLDOUT
  regression at the same ``weights.promote_margin``.

On a pass-dominated board a slice's scalar moves in ``1/N`` steps, so the two
uses pull the knob in opposite directions and the feasible window can be
empty. Worse, the holdout also runs the pass-rate monotonicity rule, which
carried no operator tolerance at all — so a single holdout entry flipping
pass->fail rejected at EVERY margin, under both scopes, and a
``holdout_margin`` field alone would not have made such a board usable.

The fix splits both bounds off onto the holdout:
:attr:`~zicato.core.ScoringWeights.holdout_margin` (``None`` ⇒ fall back to
``promote_margin``) and
:attr:`~zicato.core.ScoringWeights.holdout_entry_regression_budget` (``0`` ⇒
today's zero-tolerance rule). These pins assert the board becomes promotable
once an operator sets them, and — deliberately — that NOTHING moves at their
defaults.

Every aggregate here is built by the REAL scorer
(:func:`~zicato.tournament.scoring.aggregate_generation_score` over
:class:`~zicato.core.LossProfile` rows), not hand-written dicts, so a pin can
only fail because the gate decided that way.
"""

from __future__ import annotations

from typing import Any

import pytest

from zicato.core import DriftCount, LossProfile, ScoringWeights
from zicato.tournament.gate import evaluate_gate
from zicato.tournament.scoring import aggregate_generation_score


def _loss(entry_id: str, *, passed: bool) -> LossProfile:
    """One real reducer output with no drift and a bool expectation."""
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


def _slice(prefix: str, total: int, failing: int, weights: ScoringWeights) -> dict[str, Any]:
    """A real slice aggregate: ``total`` entries of which the first ``failing`` fail."""
    losses = [_loss(f"{prefix}{i}", passed=(i >= failing)) for i in range(total)]
    aggregate: dict[str, Any] = aggregate_generation_score(losses, weights)
    return aggregate


#: A pass-dominated contract: a zeroed ``drift:`` channel leaves the scalar equal to
#: the slice's failing fraction, so a slice of N entries moves in 1/N steps.
def _weights(
    margin: float,
    scope: str = "aggregate",
    *,
    holdout_margin: float | None = None,
    holdout_budget: int = 0,
) -> ScoringWeights:
    return ScoringWeights(
        namespace_weights={"drift:": 0.0, "failure:": 1.0},
        pass_weight=1.0,
        promote_margin=margin,
        holdout_margin=holdout_margin,
        holdout_entry_regression_budget=holdout_budget,
        pass_rate_monotonicity_scope=scope,  # type: ignore[arg-type]
    )


#: Dense margin sweep over the whole plausible range, including the two
#: closed-form bounds the issue names (1/6 and 2/12) exactly.
_MARGIN_SWEEP: tuple[float, ...] = tuple(
    sorted({i / 1000.0 for i in range(0, 501)} | {1.0 / 6.0, 2.0 / 12.0, 1.0 / 12.0})
)


def _promotes(weights: ScoringWeights) -> bool:
    """Decide the 12-train / 6-holdout board with the real gate.

    Train: the board's best achievable single-round result — a two-entry win
    (parent fails 5 of 12, child fails 3 of 12), delta ``-2/12``.
    Holdout: exactly one entry regresses (parent fails 1 of 6, child fails 2),
    delta ``+1/6`` — the smallest holdout movement the board can express.
    """
    return (
        evaluate_gate(
            _slice("t", 12, 5, weights),
            _slice("t", 12, 3, weights),
            weights,
            holdout_parent_agg=_slice("h", 6, 1, weights),
            holdout_child_agg=_slice("h", 6, 2, weights),
        ).decision
        == "promoted"
    )


def test_holdout_bounds_open_a_window_the_shared_knob_could_not() -> None:
    """A 12/6 board must have at least ONE promotable setting.

    With one knob there was none. Promotion needed ``margin <= 2/12`` (Rule 1)
    while tolerating the holdout needed ``margin >= 1/6``; ``2/12 == 1/6``
    exactly in the reals, so the window was a single point that float rounding
    of ``1 - 7/12`` then closed — and past the scalar bound the holdout's
    pass-rate rule rejected anyway.

    Separating the bounds makes the window two-dimensional: the train margin
    stays below ``2/12`` while the holdout margin rises past ``1/6``, and one
    entry of regression budget absorbs the pass-rate flip that no margin could.
    """
    # Short-circuits on the first promotable pair: the sweep is quadratic now
    # that there are two margins, and one witness is the whole claim.
    promoting = next(
        (
            (margin, holdout_margin)
            for margin in _MARGIN_SWEEP
            for holdout_margin in _MARGIN_SWEEP
            if _promotes(_weights(margin, holdout_margin=holdout_margin, holdout_budget=1))
        ),
        None,
    )
    assert promoting, (
        "no (promote_margin, holdout_margin) pair promotes a two-entry train "
        "win on a 12-entry train slice while tolerating one regressing entry "
        "on a 6-entry holdout slice"
    )


def test_the_shared_knob_alone_still_admits_no_margin() -> None:
    """At the DEFAULTS the window is still empty — by design, not by accident.

    ``holdout_margin=None`` and a budget of ``0`` mean "exactly the historical
    strictness", which is what keeps the contract hash and every existing
    epoch unmoved. This pins that the defaults really are inert: the board the
    issue reported stays unpromotable until an operator opts in, so a future
    change cannot quietly loosen the gate for contracts that never asked.

    The pre-flight is what tells the operator this is happening — see
    :func:`zicato.epoch.preflight.holdout_window_note`.
    """
    assert not [margin for margin in _MARGIN_SWEEP if _promotes(_weights(margin))]


@pytest.mark.parametrize("scope", ["aggregate", "per_entry"])
def test_the_regression_budget_tolerates_one_holdout_flip(scope: str) -> None:
    """With the train win unambiguous, one holdout flip must be tolerable.

    The train side is a FOUR-entry win (parent fails 6 of 12, child fails 2),
    delta ``-4/12``, so Rule 1 clears comfortably for any margin below 1/3 and
    cannot be what rejects. The holdout still regresses by exactly one entry.

    Raising ``holdout_margin`` past ``1/6`` clears the scalar tolerance, and
    the entry budget clears
    :func:`~zicato.tournament.gate._pass_rate_regression_reason` — which
    otherwise rejects under either scope, carrying only
    :data:`~zicato.tournament.gate.PASS_RATE_MONOTONICITY_TOLERANCE` (``1e-9``)
    / :data:`~zicato.tournament.gate.PER_ENTRY_SCORE_MONOTONICITY_TOLERANCE`
    (``0.02``), neither of which an operator can widen to one entry in six. The
    budget means the same thing under both scopes: one entry.
    """
    promoting: list[float] = []
    for margin in _MARGIN_SWEEP:
        if margin >= 1.0 / 3.0:
            continue
        weights = _weights(margin, scope, holdout_margin=0.2, holdout_budget=1)
        outcome = evaluate_gate(
            _slice("t", 12, 6, weights),
            _slice("t", 12, 2, weights),
            weights,
            holdout_parent_agg=_slice("h", 6, 1, weights),
            holdout_child_agg=_slice("h", 6, 2, weights),
        )
        if outcome.decision == "promoted":
            promoting.append(margin)
    assert promoting, (
        f"scope={scope}: no promote_margin tolerates one regressing holdout entry "
        "even with a holdout margin of 0.2 and a one-entry regression budget"
    )
