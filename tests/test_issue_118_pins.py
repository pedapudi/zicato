"""Pins for issue #118 — ``promote_margin`` doubles as the holdout tolerance.

The gate reads ONE knob for two decisions calibrated against two different
slice sizes:

* :func:`zicato.tournament.gate.evaluate_gate` Rule 1 thresholds the TRAIN
  delta at ``weights.promote_margin``;
* :func:`zicato.tournament.gate._holdout_confirms` thresholds the HOLDOUT
  regression at the same ``weights.promote_margin``.

On a pass-dominated board a slice's scalar moves in ``1/N`` steps, so the two
uses pull the knob in opposite directions and the feasible window can be empty.
Both pins here are built from aggregates the REAL scorer produces
(:func:`~zicato.tournament.scoring.aggregate_generation_score` over
:class:`~zicato.core.LossProfile` rows), not hand-written dicts, so a pin can
only fail because the gate decided that way.

The second pin records a constraint the issue does not state: the holdout also
runs the pass-rate monotonicity rule, which has no tolerance knob at all. A
``holdout_margin`` field alone would satisfy pin 1 and leave pin 2 red.
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


#: A pass-dominated contract: ``drift_weight == 0`` leaves the scalar equal to
#: the slice's failing fraction, so a slice of N entries moves in 1/N steps.
def _weights(margin: float, scope: str = "aggregate") -> ScoringWeights:
    return ScoringWeights(
        drift_weight=0.0,
        pass_weight=1.0,
        promote_margin=margin,
        pass_rate_monotonicity_scope=scope,  # type: ignore[arg-type]
    )


#: Dense margin sweep over the whole plausible range, including the two
#: closed-form bounds the issue names (1/6 and 2/12) exactly.
_MARGIN_SWEEP: tuple[float, ...] = tuple(
    sorted({i / 1000.0 for i in range(0, 501)} | {1.0 / 6.0, 2.0 / 12.0, 1.0 / 12.0})
)


@pytest.mark.xfail(
    strict=True,
    reason=(
        "issue #118: promote_margin is both the train threshold and the holdout "
        "tolerance, so on a 12-train / 6-holdout pass-dominated board no value "
        "both admits the best achievable two-entry train win and tolerates one "
        "holdout entry regressing"
    ),
)
def test_some_margin_admits_the_best_train_win_while_tolerating_one_holdout_entry() -> None:
    """A 12/6 board must have at least ONE promotable ``promote_margin``.

    Train: the board's best achievable single-round result — a two-entry win
    (parent fails 5 of 12, child fails 3 of 12), delta ``-2/12``.
    Holdout: exactly one entry regresses (parent fails 1 of 6, child fails 2),
    delta ``+1/6`` — the smallest holdout movement the board can express.

    Promotion needs ``margin <= 2/12`` (Rule 1) and tolerating the holdout
    needs ``margin >= 1/6`` (the confirmation step). ``2/12 == 1/6`` exactly in
    the reals, so the window is a single point — and float rounding of
    ``1 - 7/12`` closes even that, leaving it empty.
    """
    promoting = [
        margin
        for margin in _MARGIN_SWEEP
        if evaluate_gate(
            _slice("t", 12, 5, _weights(margin)),
            _slice("t", 12, 3, _weights(margin)),
            _weights(margin),
            holdout_parent_agg=_slice("h", 6, 1, _weights(margin)),
            holdout_child_agg=_slice("h", 6, 2, _weights(margin)),
        ).decision
        == "promoted"
    ]
    assert promoting, (
        "no promote_margin in [0.0, 0.5] promotes a two-entry train win on a "
        "12-entry train slice while tolerating one regressing entry on a "
        "6-entry holdout slice"
    )


@pytest.mark.xfail(
    strict=True,
    reason=(
        "issue #118 (residual the issue does not name): the holdout also applies "
        "pass-rate monotonicity, which has no tolerance knob, so a single "
        "holdout entry flipping pass->fail rejects at EVERY margin under both "
        "scopes — a holdout_margin field alone would not make this board usable"
    ),
)
@pytest.mark.parametrize("scope", ["aggregate", "per_entry"])
def test_some_margin_tolerates_one_holdout_flip_given_an_unambiguous_train_win(
    scope: str,
) -> None:
    """With the train win made unambiguous, one holdout flip must be tolerable.

    The train side is a FOUR-entry win (parent fails 6 of 12, child fails 2),
    delta ``-4/12``, so Rule 1 clears comfortably for any margin below 1/3 and
    cannot be what rejects. The holdout still regresses by exactly one entry.

    Raising the margin past ``1/6`` clears the holdout's scalar tolerance — and
    then :func:`~zicato.tournament.gate._pass_rate_regression_reason` rejects
    instead, under either scope, because the holdout's pass-rate rule carries
    only :data:`~zicato.tournament.gate.PASS_RATE_MONOTONICITY_TOLERANCE`
    (``1e-9``) / :data:`~zicato.tournament.gate.PER_ENTRY_SCORE_MONOTONICITY_TOLERANCE`
    (``0.02``), neither of which an operator can widen to one entry in six.
    """
    promoting = [
        margin
        for margin in _MARGIN_SWEEP
        if margin < 1.0 / 3.0
        and evaluate_gate(
            _slice("t", 12, 6, _weights(margin, scope)),
            _slice("t", 12, 2, _weights(margin, scope)),
            _weights(margin, scope),
            holdout_parent_agg=_slice("h", 6, 1, _weights(margin, scope)),
            holdout_child_agg=_slice("h", 6, 2, _weights(margin, scope)),
        ).decision
        == "promoted"
    ]
    assert promoting, (
        f"scope={scope}: no promote_margin tolerates one regressing holdout entry "
        "even when the train win is four entries wide"
    )
