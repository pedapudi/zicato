"""Tests for :mod:`zicato.telemetry.scoring`.

Scoring aggregates :class:`LossProfile` instances into the
generation-level scalar. The tests build mock profiles by hand —
no JSONL involved — so they exercise the aggregation math in
isolation from the reducer.
"""

from __future__ import annotations

import pytest

from zicato.core import (
    DriftCount,
    ExpectationResult,
    LossProfile,
    ScoringWeights,
)
from zicato.telemetry import aggregate_generation_score, combined_scalar


def _profile(
    drift_loss: float,
    pass_fail: bool | None,
    entry_id: str = "ent",
) -> LossProfile:
    """Build a minimal LossProfile carrying just drift_loss and pass_fail."""
    return LossProfile(
        run_id=f"r-{entry_id}",
        entry_id=entry_id,
        generation_id="v0",
        epoch_id="ep1",
        drift_counts=(),
        plan_revisions=0,
        task_failure_ratio=0.0,
        runtime_ms=0,
        wall_clock_budget_exceeded=False,
        expectation_result=(
            None if pass_fail is None else ExpectationResult(kind="predicate", passed=pass_fail)
        ),
        drift_loss=drift_loss,
        pass_fail=pass_fail,
    )


def test_aggregate_empty_returns_zero_and_one() -> None:
    """An empty losses list scores 0.0 drift mean and 1.0 pass rate."""
    drift_mean, pass_rate = aggregate_generation_score([], ScoringWeights())
    assert drift_mean == 0.0
    assert pass_rate == 1.0


def test_aggregate_drift_mean() -> None:
    """drift_loss_mean is the arithmetic mean across all profiles."""
    losses = [_profile(2.0, None), _profile(4.0, None), _profile(6.0, None)]
    drift_mean, pass_rate = aggregate_generation_score(losses, ScoringWeights())
    assert drift_mean == pytest.approx(4.0)
    # No expectations → pass_rate falls back to 1.0
    assert pass_rate == 1.0


def test_aggregate_pass_rate_partial_observability() -> None:
    """Profiles with pass_fail=None are excluded from the pass-rate denominator."""
    losses = [
        _profile(1.0, True),
        _profile(1.0, False),
        _profile(1.0, None),  # excluded
        _profile(1.0, True),
    ]
    drift_mean, pass_rate = aggregate_generation_score(losses, ScoringWeights())
    assert drift_mean == pytest.approx(1.0)
    # 2 of 3 observed expectations passed → 2/3
    assert pass_rate == pytest.approx(2.0 / 3.0)


def test_aggregate_pass_rate_all_pass() -> None:
    """All-pass generation has pass_rate 1.0."""
    losses = [_profile(0.0, True), _profile(0.0, True)]
    _, pass_rate = aggregate_generation_score(losses, ScoringWeights())
    assert pass_rate == 1.0


def test_aggregate_pass_rate_all_fail() -> None:
    """All-fail generation has pass_rate 0.0."""
    losses = [_profile(0.0, False), _profile(0.0, False)]
    _, pass_rate = aggregate_generation_score(losses, ScoringWeights())
    assert pass_rate == 0.0


def test_combined_scalar_default_weights() -> None:
    """combined_scalar with default weights is drift_mean + (1 - pass_rate)."""
    weights = ScoringWeights()  # drift: 1.0, pass_weight 1.0
    assert combined_scalar(2.0, 1.0, weights) == pytest.approx(2.0)
    assert combined_scalar(2.0, 0.0, weights) == pytest.approx(3.0)
    assert combined_scalar(0.0, 0.5, weights) == pytest.approx(0.5)


def test_combined_scalar_tunable_weights() -> None:
    """Up-weighting the pass axis penalises failures harder."""
    weights = ScoringWeights(pass_weight=4.0)
    # drift_mean=2, pass_rate=0.5 → 1*2 + 4*(1-0.5) = 4.0
    assert combined_scalar(2.0, 0.5, weights) == pytest.approx(4.0)


def test_combined_scalar_pass_weight_zero_ignores_pass() -> None:
    """pass_weight=0 makes the scalar drift-only."""
    weights = ScoringWeights(pass_weight=0.0)
    # pass_rate becomes irrelevant
    assert combined_scalar(7.0, 0.0, weights) == pytest.approx(7.0)
    assert combined_scalar(7.0, 1.0, weights) == pytest.approx(7.0)


def test_full_pipeline_mock_losses() -> None:
    """Aggregate then combine: an end-to-end mock profile flow."""
    weights = ScoringWeights(pass_weight=2.0)
    losses = [
        _profile(1.0, True),
        _profile(3.0, False),
        _profile(2.0, None),
    ]
    drift_mean, pass_rate = aggregate_generation_score(losses, weights)
    score = combined_scalar(drift_mean, pass_rate, weights)
    # drift_mean = 2.0; pass_rate = 1/2 = 0.5; score = 1*2 + 2*0.5 = 3.0
    assert drift_mean == pytest.approx(2.0)
    assert pass_rate == pytest.approx(0.5)
    assert score == pytest.approx(3.0)


def test_combined_scalar_smoke_with_drift_count_profiles() -> None:
    """A profile with non-trivial drift_counts still feeds the aggregator correctly."""
    p1 = LossProfile(
        run_id="r1",
        entry_id="ent1",
        generation_id="v0",
        epoch_id="ep1",
        drift_counts=(DriftCount(kind="off_topic", severity="warning", count=2),),
        plan_revisions=1,
        task_failure_ratio=0.0,
        runtime_ms=1000,
        wall_clock_budget_exceeded=False,
        expectation_result=ExpectationResult(kind="predicate", passed=True),
        drift_loss=6.5,
        pass_fail=True,
    )
    p2 = LossProfile(
        run_id="r2",
        entry_id="ent2",
        generation_id="v0",
        epoch_id="ep1",
        drift_counts=(),
        plan_revisions=0,
        task_failure_ratio=0.0,
        runtime_ms=500,
        wall_clock_budget_exceeded=False,
        expectation_result=ExpectationResult(kind="predicate", passed=False),
        drift_loss=0.0,
        pass_fail=False,
    )
    weights = ScoringWeights()
    drift_mean, pass_rate = aggregate_generation_score([p1, p2], weights)
    assert drift_mean == pytest.approx(3.25)
    assert pass_rate == pytest.approx(0.5)
    assert combined_scalar(drift_mean, pass_rate, weights) == pytest.approx(3.25 + 0.5)


# ---------------------------------------------------------------------------
# Custom-judge signal — scoring-layer back-compat
# ---------------------------------------------------------------------------
#
# Custom-judge drift is folded into ``LossProfile.drift_loss`` upstream by the
# reducer (weighted per judge_name via ``ScoringWeights.per_judge_weights``).
# By the time a profile reaches this module its ``drift_loss`` already carries
# that contribution, so the aggregation / combine arithmetic here is unchanged
# in shape — these tests pin that back-compat invariant.


def test_aggregate_treats_custom_judge_drift_loss_like_any_drift_loss() -> None:
    """A profile whose drift_loss includes per-judge-weighted custom-judge
    signal aggregates the same way as any other drift_loss value."""
    # The reducer would have produced these drift_loss values already
    # carrying the per_judge_weights contribution; the scorer just means them.
    losses = [_profile(5.0, None), _profile(15.0, None)]
    drift_mean, pass_rate = aggregate_generation_score(losses, ScoringWeights())
    assert drift_mean == pytest.approx(10.0)
    assert pass_rate == 1.0


def test_combined_scalar_unchanged_with_per_judge_weights_configured() -> None:
    """Setting per_judge_weights on ScoringWeights does not change the
    combined-scalar formula — per_judge_weights is consumed by the reducer,
    not by combined_scalar."""
    plain = ScoringWeights(pass_weight=1.0)
    with_judges = ScoringWeights(
        pass_weight=1.0,
        per_judge_weights={"judge_a": 4.0, "judge_b": 0.5},
        default_judge_weight=2.0,
    )
    # Identical drift_mean / pass_rate inputs → identical scalar regardless
    # of whether per_judge_weights is populated.
    assert combined_scalar(3.0, 0.5, plain) == pytest.approx(combined_scalar(3.0, 0.5, with_judges))
