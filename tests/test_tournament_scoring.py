"""Tests for ``zicato.tournament.scoring``."""

from __future__ import annotations

from zicato.core import (
    DriftCount,
    ExpectationResult,
    LossProfile,
    ScoringWeights,
)
from zicato.tournament.scoring import (
    aggregate_generation_score,
    per_run_drift_loss,
)


def _make_loss(
    entry_id: str,
    *,
    drift_loss: float,
    pass_fail: bool | None,
) -> LossProfile:
    """Construct a minimal :class:`LossProfile` for tests."""
    expectation = (
        ExpectationResult(kind="predicate", passed=bool(pass_fail))
        if pass_fail is not None
        else None
    )
    return LossProfile(
        run_id=f"run-{entry_id}",
        entry_id=entry_id,
        generation_id="v0",
        epoch_id="e0",
        drift_counts=(DriftCount(kind="off_topic", severity="info", count=0),),
        plan_revisions=0,
        task_failure_ratio=0.0,
        runtime_ms=1000,
        wall_clock_budget_exceeded=False,
        expectation_result=expectation,
        drift_loss=drift_loss,
        pass_fail=pass_fail,
    )


def test_per_run_drift_loss_returns_loss_profile_value() -> None:
    """The canonical re-derivation today simply mirrors ``LossProfile.drift_loss``."""
    loss = _make_loss("a", drift_loss=1.25, pass_fail=True)
    weights = ScoringWeights()
    assert per_run_drift_loss(loss, weights) == 1.25


def test_aggregate_empty_returns_neutral_summary() -> None:
    """An empty list aggregates to a non-penalizing neutral summary."""
    agg = aggregate_generation_score([], ScoringWeights())
    assert agg["drift_loss_mean"] == 0.0
    assert agg["pass_rate"] == 1.0
    assert agg["expectation_count"] == 0
    assert agg["entry_count"] == 0
    assert agg["scalar"] == 0.0
    assert agg["per_entry"] == {}


def test_aggregate_mixed_pass_fail_and_none_expectations() -> None:
    """Mixed entries: per-entry visible, pass_rate computed only over expectation entries."""
    losses = [
        _make_loss("a", drift_loss=0.0, pass_fail=True),
        _make_loss("b", drift_loss=2.0, pass_fail=False),
        _make_loss("c", drift_loss=1.0, pass_fail=None),
    ]
    weights = ScoringWeights(pass_weight=2.0)

    agg = aggregate_generation_score(losses, weights)

    # Means and counts
    assert agg["entry_count"] == 3
    assert agg["expectation_count"] == 2
    assert agg["drift_loss_mean"] == (0.0 + 2.0 + 1.0) / 3.0
    assert agg["pass_rate"] == 0.5

    # Scalar combines weighted drift and (1 - pass_rate)
    expected_scalar = 1.0 * agg["drift_loss_mean"] + 2.0 * (1.0 - 0.5)
    assert agg["scalar"] == expected_scalar

    # Per-entry mapping preserves raw signals. ``score`` is the uniform
    # continuous outcome (bool -> 1.0/0.0; None for a no-expectation entry).
    assert agg["per_entry"]["a"] == {
        "drift_loss": 0.0,
        "failure": 0.0,
        "pass_fail": True,
        "score": 1.0,
    }
    assert agg["per_entry"]["b"] == {
        "drift_loss": 2.0,
        "failure": 0.0,
        "pass_fail": False,
        "score": 0.0,
    }
    assert agg["per_entry"]["c"] == {
        "drift_loss": 1.0,
        "failure": 0.0,
        "pass_fail": None,
        "score": None,
    }


def test_aggregate_with_no_expectations_yields_pass_rate_one() -> None:
    """When no entries carry expectations, pass rate is 1.0 (no penalty)."""
    losses = [
        _make_loss("a", drift_loss=1.0, pass_fail=None),
        _make_loss("b", drift_loss=3.0, pass_fail=None),
    ]
    weights = ScoringWeights()

    agg = aggregate_generation_score(losses, weights)

    assert agg["expectation_count"] == 0
    assert agg["pass_rate"] == 1.0
    # scalar reduces to the drift channel alone
    assert agg["scalar"] == weights.namespace_weights["drift:"] * agg["drift_loss_mean"]


def test_aggregate_scalar_uses_supplied_weights() -> None:
    """Confirm the scalar uses both the drift coefficient and pass_weight."""
    losses = [
        _make_loss("a", drift_loss=4.0, pass_fail=False),
        _make_loss("b", drift_loss=4.0, pass_fail=False),
    ]
    weights = ScoringWeights(namespace_weights={"drift:": 0.25, "failure:": 1.0}, pass_weight=10.0)

    agg = aggregate_generation_score(losses, weights)

    # drift_loss_mean = 4.0, pass_rate = 0.0, (1 - pass_rate) = 1.0
    assert agg["drift_loss_mean"] == 4.0
    assert agg["pass_rate"] == 0.0
    assert agg["scalar"] == 0.25 * 4.0 + 10.0 * 1.0
