"""Scalar aggregation is a function of its inputs, not of the interpreter.

CPython 3.12 changed the builtin ``sum`` over floats to compensated
(Neumaier) summation. Ten 0.1s sum to 1.0 under 3.12 and to
0.9999999999999999 under 3.11 — so an aggregate built on the builtin
``sum`` is a function of the interpreter as well as the data. That reached
the served scalars: the same board scored 0.4 under 3.12 and
0.39999999999999997 under 3.11, and the parity golden for the
gauntlet-fast lane could only be green on one of them.

Every float total on the aggregation path now uses :func:`math.fsum`,
which returns the correctly-rounded exact sum. These tests drive the
PRODUCTION seam, never ``math.fsum`` directly, with two witnesses:

* :data:`_TENTH` ten times — the version witness. Exactly 1.0 under
  ``math.fsum`` and under 3.12's builtin, 0.9999999999999999 under 3.11's.
  These cases catch the regression that actually happened, and they catch
  it only on 3.11 — which is precisely why the parity gates run on both.
* :data:`_EXACTNESS_WITNESS` — the strategy witness. 3.12's compensated
  summation is accurate but not exactly rounded, so this vector separates
  ``math.fsum`` from the builtin on BOTH interpreters. These cases fail
  everywhere the moment an aggregate stops being an exact sum.
"""

from __future__ import annotations

import math

from zicato.core.types import DriftCount, LossProfile, ScoringWeights
from zicato.scoring.builtins import builtin_drift_loss, builtin_scalar

# Two aggregators share the name; both are on the served path, so both are
# driven here and each is qualified by the layer it belongs to.
from zicato.telemetry.scoring import aggregate_generation_score as telemetry_aggregate
from zicato.tournament.scoring import aggregate_generation_score

#: Ten of these sum to exactly 1.0, and to 0.9999999999999999 left to
#: right on an interpreter without compensated summation. The canonical
#: witness for the difference.
_TENTH = 0.1
_TEN = 10

#: A vector whose exact sum no inexact strategy reaches: the builtin sums
#: it to 1e16 on both 3.11 and 3.12, while the exact sum is
#: 1.0000000000000002e+16. Adversarial by design — its job is to pin the
#: summation STRATEGY, the way a collision fixture pins a hash.
_EXACTNESS_WITNESS = (1e16, 0.3, 0.7, 1e-16)

#: …and one whose builtin sum depends on the order it is presented in
#: (1.7 forward, 1.7000000000000002 reversed). An exactly-rounded sum has
#: no such freedom.
_ORDER_WITNESS = (1e16, -1e16, 1.0, 0.7, 1e-16)


def _loss(entry_id: str, drift_loss: float, *, pass_fail: bool | None = True) -> LossProfile:
    return LossProfile(
        run_id=f"r-{entry_id}",
        entry_id=entry_id,
        generation_id="v1",
        epoch_id="e0",
        drift_counts=(),
        plan_revisions=0,
        task_failure_ratio=0.0,
        runtime_ms=0,
        wall_clock_budget_exceeded=False,
        expectation_result=None,
        drift_loss=drift_loss,
        pass_fail=pass_fail,
    )


def test_ten_tenths_sum_to_one_through_the_generation_aggregate() -> None:
    """The mean of ten 0.1s is exactly 0.1 — not 0.09999999999999999."""
    losses = [_loss(f"e{i}", _TENTH) for i in range(_TEN)]

    agg = aggregate_generation_score(losses, ScoringWeights())

    assert agg["drift_loss_mean"] == _TENTH
    # The sum itself, recovered from the mean, is exactly 1.0.
    assert agg["drift_loss_mean"] * _TEN == 1.0


def test_ten_tenths_sum_to_one_through_the_telemetry_aggregate() -> None:
    losses = [_loss(f"e{i}", _TENTH) for i in range(_TEN)]

    drift_loss_mean, pass_rate = telemetry_aggregate(losses, ScoringWeights())

    assert drift_loss_mean == _TENTH
    assert pass_rate == 1.0


def test_the_scalar_is_the_exact_sum_of_its_components() -> None:
    """``scalar`` equals the exact sum of ``scalar_components``.

    The contract the scalar's provenance rests on: an operator reading the
    components must be able to add them up and land on the scalar. A naive
    sum of ten equal namespace terms does not.
    """
    components = {f"ns{i}": _TENTH for i in range(_TEN)}

    assert builtin_scalar(1.0, {f"ns{i}:": _TENTH for i in range(_TEN)}, ScoringWeights()) == (
        math.fsum(components.values())
    )


def test_the_per_run_drift_loss_is_the_exact_sum_of_its_counts() -> None:
    """Ten drift events at weight 0.1 charge exactly 1.0."""
    weights = ScoringWeights(severity_weights={"info": _TENTH}, plan_revision_weight=0.0)
    counts = tuple(DriftCount(kind=f"k{i}", severity="info", count=1) for i in range(_TEN))

    loss = builtin_drift_loss(drift_counts=counts, plan_revisions=0, weights=weights)

    assert loss == 1.0


def test_the_drift_mean_is_the_exactly_rounded_sum() -> None:
    """The aggregate is the exact sum of its terms on every interpreter.

    The ten-tenths cases above separate 3.11 from 3.12; this one separates
    an exact sum from ANY inexact one, so it fails on both interpreters the
    moment a builtin ``sum`` or a running accumulator comes back.
    """
    losses = [_loss(f"e{i}", value) for i, value in enumerate(_EXACTNESS_WITNESS)]

    agg = aggregate_generation_score(losses, ScoringWeights())

    expected = math.fsum(_EXACTNESS_WITNESS) / len(_EXACTNESS_WITNESS)
    assert agg["drift_loss_mean"] == expected


def test_the_drift_mean_does_not_depend_on_entry_order() -> None:
    """Reordering the same board's losses cannot move the aggregate.

    Order-independence is what makes the aggregate a function of the board
    rather than of the iteration that happened to produce it. The builtin
    sums this witness to 1.7 forward and 1.7000000000000002 reversed.
    """
    forward = [_loss(f"e{i}", value) for i, value in enumerate(_ORDER_WITNESS)]
    reversed_ = list(reversed(forward))

    weights = ScoringWeights()
    assert (
        aggregate_generation_score(forward, weights)["drift_loss_mean"]
        == aggregate_generation_score(reversed_, weights)["drift_loss_mean"]
    )
