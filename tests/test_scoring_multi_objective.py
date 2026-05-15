"""Tests for the multi-objective scoring surface.

Covers :func:`aggregate_namespaced_metrics` and the namespace-aware
extensions to :func:`aggregate_generation_score`. See
``tests/test_gate_namespace_monotonicity.py`` for the gate-side tests
of the same surface.
"""

from __future__ import annotations

import pytest

from zicato.core import (
    DriftCount,
    ExpectationResult,
    LossProfile,
    MetricCount,
    ScoringWeights,
)
from zicato.tournament.scoring import (
    aggregate_generation_score,
    aggregate_namespaced_metrics,
)


def _make_loss(
    entry_id: str,
    *,
    drift_loss: float = 0.0,
    pass_fail: bool | None = None,
    metric_counts: tuple[MetricCount, ...] = (),
) -> LossProfile:
    """Construct a minimal :class:`LossProfile` for these tests."""
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
        metric_counts=metric_counts,
    )


# ---------------------------------------------------------------------------
# aggregate_namespaced_metrics
# ---------------------------------------------------------------------------


def test_aggregate_namespaced_metrics_on_mixed_namespaces() -> None:
    """Mixed cost / rubric / schema metrics produce expected weighted means."""
    losses = [
        _make_loss(
            "a",
            drift_loss=1.0,
            metric_counts=(
                MetricCount(name="cost:tokens", count=1000.0),
                MetricCount(name="rubric:quality", count=4.0),
                MetricCount(name="schema:failures", count=0.0),
            ),
        ),
        _make_loss(
            "b",
            drift_loss=3.0,
            metric_counts=(
                MetricCount(name="cost:tokens", count=2000.0),
                MetricCount(name="rubric:quality", count=2.0),
                MetricCount(name="schema:failures", count=2.0),
            ),
        ),
    ]
    weights = ScoringWeights()  # ships with the namespace-weight defaults

    agg = aggregate_namespaced_metrics(losses, weights)

    # drift uses drift-loss mean, weighted by namespace_weights["drift:"]
    assert agg["drift:"] == pytest.approx(1.0 * (1.0 + 3.0) / 2.0)
    # cost: mean = 1500, weight = 0.001
    assert agg["cost:"] == pytest.approx(0.001 * 1500.0)
    # rubric: mean = 3.0, weight = -1.0
    assert agg["rubric:"] == pytest.approx(-1.0 * 3.0)
    # schema: mean = 1.0, weight = 5.0
    assert agg["schema:"] == pytest.approx(5.0 * 1.0)


def test_aggregate_namespaced_metrics_zero_weight_means_zero_contribution() -> None:
    """A namespace with weight 0 returns an aggregate of 0 regardless of value."""
    losses = [
        _make_loss(
            "a",
            metric_counts=(MetricCount(name="output:chars", count=12345.0),),
        ),
    ]
    weights = ScoringWeights()  # output: defaults to 0.0
    agg = aggregate_namespaced_metrics(losses, weights)
    assert agg["output:"] == 0.0


def test_aggregate_namespaced_metrics_negative_weight_lowers_scalar() -> None:
    """Negative namespace_weight means higher metric → lower scalar contribution."""
    losses = [
        _make_loss(
            "a",
            metric_counts=(MetricCount(name="rubric:quality", count=5.0),),
        ),
        _make_loss(
            "b",
            metric_counts=(MetricCount(name="rubric:quality", count=5.0),),
        ),
    ]
    weights = ScoringWeights()  # rubric: weight = -1.0
    agg = aggregate_namespaced_metrics(losses, weights)
    assert agg["rubric:"] == pytest.approx(-5.0)


def test_aggregate_namespaced_metrics_empty_input_returns_known_namespaces_at_zero() -> None:
    """Empty losses yield every weight-declared namespace at zero aggregate."""
    weights = ScoringWeights()
    agg = aggregate_namespaced_metrics([], weights)
    # Every namespace declared in defaults is present and zero.
    for ns in weights.namespace_weights:
        assert agg[ns] == 0.0


def test_aggregate_namespaced_metrics_unnamespaced_entries_ignored() -> None:
    """MetricCount entries without a colon prefix do not affect aggregates."""
    losses = [
        _make_loss(
            "a",
            metric_counts=(
                MetricCount(name="no_namespace_metric", count=999.0),
                MetricCount(name="cost:tokens", count=1000.0),
            ),
        ),
    ]
    weights = ScoringWeights()
    agg = aggregate_namespaced_metrics(losses, weights)
    # Only the namespaced metric is reflected.
    assert agg["cost:"] == pytest.approx(0.001 * 1000.0)
    # The unnamespaced metric is not promoted to any namespace key.
    assert "no_namespace_metric" not in agg
    assert "no_namespace_metric:" not in agg


def test_aggregate_namespaced_metrics_drift_namespace_uses_drift_loss_not_counts() -> None:
    """The drift namespace aggregate mirrors weighted drift_loss_mean."""
    losses = [
        _make_loss("a", drift_loss=2.0),
        _make_loss("b", drift_loss=4.0),
    ]
    weights = ScoringWeights()
    agg = aggregate_namespaced_metrics(losses, weights)
    # drift namespace weight is 1.0; mean drift_loss is 3.0
    assert agg["drift:"] == pytest.approx(3.0)


# ---------------------------------------------------------------------------
# aggregate_generation_score — extended return shape
# ---------------------------------------------------------------------------


def test_aggregate_generation_score_returns_namespace_aggregates_key() -> None:
    """The summary dict surfaces the namespace aggregates dict."""
    losses = [_make_loss("a", drift_loss=1.0, pass_fail=True)]
    agg = aggregate_generation_score(losses, ScoringWeights())
    assert "namespace_aggregates" in agg
    assert "drift:" in agg["namespace_aggregates"]


def test_aggregate_generation_score_scalar_components_sum_to_scalar() -> None:
    """scalar_components values sum exactly to the reported scalar."""
    losses = [
        _make_loss(
            "a",
            drift_loss=2.0,
            pass_fail=False,
            metric_counts=(
                MetricCount(name="cost:tokens", count=1000.0),
                MetricCount(name="rubric:quality", count=4.0),
                MetricCount(name="schema:failures", count=1.0),
            ),
        ),
    ]
    agg = aggregate_generation_score(losses, ScoringWeights())
    total = sum(agg["scalar_components"].values())
    assert total == pytest.approx(agg["scalar"])


def test_aggregate_generation_score_scalar_components_has_named_keys() -> None:
    """Component names include 'drift', 'pass', and one per non-drift namespace."""
    losses = [
        _make_loss(
            "a",
            drift_loss=1.0,
            pass_fail=True,
            metric_counts=(
                MetricCount(name="cost:tokens", count=100.0),
                MetricCount(name="rubric:quality", count=3.0),
            ),
        ),
    ]
    agg = aggregate_generation_score(losses, ScoringWeights())
    components = agg["scalar_components"]
    assert "drift" in components
    assert "pass" in components
    assert "cost" in components
    assert "rubric" in components
    # The "drift:" namespace contribution is folded into the "drift"
    # component, not duplicated.
    assert "drift:" not in components


def test_aggregate_generation_score_back_compat_when_no_metric_counts() -> None:
    """With no extra metric_counts the scalar matches the legacy formula."""
    losses = [
        _make_loss("a", drift_loss=4.0, pass_fail=False),
        _make_loss("b", drift_loss=4.0, pass_fail=False),
    ]
    weights = ScoringWeights(drift_weight=0.25, pass_weight=10.0)
    agg = aggregate_generation_score(losses, weights)
    # drift_loss_mean = 4.0, pass_rate = 0.0 → scalar = 0.25*4 + 10*1
    assert agg["drift_loss_mean"] == 4.0
    assert agg["pass_rate"] == 0.0
    expected_legacy = 0.25 * 4.0 + 10.0 * 1.0
    assert agg["scalar"] == pytest.approx(expected_legacy)


def test_aggregate_generation_score_breakdown_example() -> None:
    """End-to-end example showing scalar_components summing to total."""
    losses = [
        _make_loss(
            "a",
            drift_loss=0.5,
            pass_fail=True,
            metric_counts=(
                MetricCount(name="cost:tokens", count=2000.0),
                MetricCount(name="rubric:quality", count=4.0),
                MetricCount(name="schema:failures", count=0.0),
                MetricCount(name="latency:p95_ms", count=1500.0),
            ),
        ),
        _make_loss(
            "b",
            drift_loss=0.5,
            pass_fail=True,
            metric_counts=(
                MetricCount(name="cost:tokens", count=2000.0),
                MetricCount(name="rubric:quality", count=4.0),
                MetricCount(name="schema:failures", count=0.0),
                MetricCount(name="latency:p95_ms", count=1500.0),
            ),
        ),
    ]
    agg = aggregate_generation_score(losses, ScoringWeights())
    # Components by hand:
    # drift   = 1.0 * 0.5            = 0.5
    # pass    = 1.0 * 0              = 0.0
    # cost    = 0.001 * 2000         = 2.0
    # rubric  = -1.0 * 4             = -4.0
    # schema  = 5.0 * 0              = 0.0
    # latency = 0.0001 * 1500        = 0.15
    # output  = 0.0 * 0              = 0.0
    # total                          = -1.35
    components = agg["scalar_components"]
    assert components["drift"] == pytest.approx(0.5)
    assert components["pass"] == pytest.approx(0.0)
    assert components["cost"] == pytest.approx(2.0)
    assert components["rubric"] == pytest.approx(-4.0)
    assert components["schema"] == pytest.approx(0.0)
    assert components["latency"] == pytest.approx(0.15)
    assert agg["scalar"] == pytest.approx(-1.35)
    assert sum(components.values()) == pytest.approx(agg["scalar"])


def test_end_to_end_aggregator_to_gate_promote_on_two_axes() -> None:
    """Child improves drift AND improves rubric → gate promotes."""
    from zicato.tournament.gate import evaluate_gate

    parent_losses = [
        _make_loss(
            "a",
            drift_loss=2.0,
            pass_fail=True,
            metric_counts=(MetricCount(name="rubric:quality", count=3.0),),
        ),
    ]
    child_losses = [
        _make_loss(
            "a",
            drift_loss=1.0,  # drift improved
            pass_fail=True,
            metric_counts=(MetricCount(name="rubric:quality", count=4.0),),  # rubric improved
        ),
    ]
    weights = ScoringWeights()

    parent_agg = aggregate_generation_score(parent_losses, weights)
    child_agg = aggregate_generation_score(child_losses, weights)
    outcome = evaluate_gate(parent_agg, child_agg, weights)
    assert outcome.decision == "promoted"


def test_end_to_end_aggregator_to_gate_reject_on_rubric_regression() -> None:
    """Child improves drift enough to pass margin but rubric drops → namespace gate rejects."""
    from zicato.tournament.gate import evaluate_gate

    parent_losses = [
        _make_loss(
            "a",
            drift_loss=5.0,
            pass_fail=True,
            metric_counts=(MetricCount(name="rubric:quality", count=4.0),),
        ),
    ]
    # Child trades a large drift improvement for a smaller rubric drop;
    # the combined scalar improves (passes margin) but rubric regresses
    # → the per-namespace monotonicity rule fires.
    child_losses = [
        _make_loss(
            "a",
            drift_loss=1.0,  # huge drift improvement
            pass_fail=True,
            metric_counts=(MetricCount(name="rubric:quality", count=2.0),),  # rubric dropped
        ),
    ]
    weights = ScoringWeights()

    parent_agg = aggregate_generation_score(parent_losses, weights)
    child_agg = aggregate_generation_score(child_losses, weights)
    # Sanity-check the scalar actually improved so the margin rule passes.
    assert child_agg["scalar"] < parent_agg["scalar"] - weights.promote_margin
    outcome = evaluate_gate(parent_agg, child_agg, weights)
    assert outcome.decision == "rejected"
    assert "rubric:" in outcome.reason
    assert "monotonicity_regression on namespace=" in outcome.reason


def test_aggregate_generation_score_includes_observed_unknown_namespaces() -> None:
    """Namespaces not in weights.namespace_weights still appear at zero."""
    losses = [
        _make_loss(
            "a",
            metric_counts=(MetricCount(name="custom_ns:metric", count=42.0),),
        ),
    ]
    # Custom weights mapping has no entry for "custom_ns:".
    weights = ScoringWeights(namespace_weights={"drift:": 1.0})
    agg = aggregate_generation_score(losses, weights)
    # Observed but unweighted → zero contribution; the key is still
    # surfaced for visibility.
    assert agg["namespace_aggregates"]["custom_ns:"] == 0.0
    # Only "drift" and "pass" appear in scalar_components (custom_ns is
    # observed but zero-weighted; we surface it in namespace_aggregates
    # but it adds nothing to the scalar).
    assert "custom_ns" in agg["scalar_components"]
    assert agg["scalar_components"]["custom_ns"] == 0.0
