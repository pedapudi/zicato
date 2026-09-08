"""Named measurements and their derived scoring channels."""

from __future__ import annotations

import dataclasses

import pytest

from zicato.core import (
    ExpectedMetricMovement,
    HypothesisSpec,
    LossProfile,
    MetricCount,
    MetricMovementActual,
    OutcomeRecord,
)

# ---------------------------------------------------------------------------
# Named metric construction
# ---------------------------------------------------------------------------


def test_metric_count_defaults_are_empty_severity_and_zero_count() -> None:
    mc = MetricCount(name="cost:input_tokens")
    assert mc.name == "cost:input_tokens"
    assert mc.severity == ""
    assert mc.count == 0.0


def test_metric_count_accepts_float_count() -> None:
    mc = MetricCount(name="rubric:slide_structure", count=3.5)
    assert mc.count == 3.5


def test_metric_count_accepts_drift_severity_buckets() -> None:
    for sev in ("info", "warning", "critical"):
        mc = MetricCount(name="drift:off_topic", severity=sev, count=1.0)  # type: ignore[arg-type]
        assert mc.severity == sev


def test_metric_count_is_frozen() -> None:
    mc = MetricCount(name="cost:tokens_spent", count=100.0)
    with pytest.raises(dataclasses.FrozenInstanceError):
        mc.count = 0.0  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Derived scoring channels
# ---------------------------------------------------------------------------


def _bare_profile(**overrides: object) -> LossProfile:
    """Build a minimal :class:`LossProfile` for scoring_metrics tests."""
    kwargs: dict[str, object] = {
        "run_id": "r1",
        "entry_id": "e1",
        "generation_id": "v0",
        "epoch_id": "epoch-001",
        "metric_counts": (),
        "plan_revisions": 0,
        "task_failure_ratio": 0.0,
        "runtime_ms": 1000,
        "wall_clock_budget_exceeded": False,
        "expectation_result": None,
        "drift_loss": 0.0,
        "pass_fail": None,
    }
    kwargs.update(overrides)
    return LossProfile(**kwargs)  # type: ignore[arg-type]


def test_scoring_metrics_with_only_metric_counts_yields_drift_namespace() -> None:
    profile = _bare_profile(
        metric_counts=(
            MetricCount(name="drift:off_topic", severity="warning", count=2),
            MetricCount(name="drift:tool_error", severity="info", count=1),
        ),
    )
    unified = profile.scoring_metrics()
    names = [m.name for m in unified]
    # The drift entries first, then the always-derived run-outcome channels.
    assert names == [
        "drift:off_topic",
        "drift:tool_error",
        "failure:tasks",
        "failure:not_completed",
        "runtime:seconds",
    ]
    drift = [m for m in unified if m.name.startswith("drift:")]
    assert {m.severity for m in drift} == {"warning", "info"}


def test_scoring_metrics_of_a_clean_run_is_the_derived_channels_at_zero() -> None:
    """A run with no signals still states its outcome, at zero.

    The ``failure:`` and ``runtime:`` members are emitted unconditionally so
    the metric key set does not depend on whether the run went wrong — a
    channel that appears only on failure cannot be aggregated across a
    generation.
    """
    assert {m.name: m.count for m in _bare_profile().scoring_metrics()} == {
        "failure:tasks": 0.0,
        "failure:not_completed": 0.0,
        "runtime:seconds": 1.0,
    }


def test_scoring_metrics_with_explicit_metric_counts_includes_all_namespaces() -> None:
    profile = _bare_profile(
        metric_counts=(
            MetricCount(name="drift:off_topic", severity="warning", count=1),
            MetricCount(name="cost:input_tokens", count=1500.0),
            MetricCount(name="cost:output_tokens", count=500.0),
            MetricCount(name="rubric:slide_structure", count=4.0),
            MetricCount(name="latency:p95_turn_ms", count=2400.0),
        )
    )
    names = {m.name for m in profile.scoring_metrics()}
    assert "drift:off_topic" in names
    assert "cost:input_tokens" in names
    assert "cost:output_tokens" in names
    assert "rubric:slide_structure" in names
    assert "latency:p95_turn_ms" in names
    # Distinct namespaces all surfaced, alongside the derived ones.
    namespaces = {m.name.split(":", 1)[0] for m in profile.scoring_metrics()}
    assert namespaces == {"drift", "cost", "rubric", "latency", "failure", "runtime"}


# ---------------------------------------------------------------------------
# HypothesisSpec — predictions keyed by measured metric name
# ---------------------------------------------------------------------------


def test_hypothesis_spec_accepts_metric_movements_alongside_drift() -> None:
    hyp = HypothesisSpec(
        core_idea="Cut token cost by trimming the prompt.",
        modulating=("router__system_prompt",),
        why="Cost dominates value at high token counts.",
        expected_pass_rate_delta="+0.00",
        expected_metric_movements=(
            ExpectedMetricMovement(
                metric_name="cost:tokens_spent", direction="decrease", magnitude="medium"
            ),
            ExpectedMetricMovement(
                metric_name="rubric:slide_structure",
                direction="increase_or_neutral",
                magnitude="small",
            ),
        ),
    )
    names = {m.metric_name for m in hyp.expected_metric_movements}
    assert names == {"cost:tokens_spent", "rubric:slide_structure"}


# ---------------------------------------------------------------------------
# OutcomeRecord — realized metric movements
# ---------------------------------------------------------------------------


def test_outcome_record_default_metric_movements_is_empty() -> None:
    outcome = OutcomeRecord(
        ran_at="2026-01-01T00:00:00Z",
        metric_movements=(),
        pass_rate_delta=0.0,
        drift_loss_delta=0.0,
        scalar_score_delta=0.0,
        tournament_decision="deferred",
    )
    assert outcome.metric_movements == ()


def test_outcome_record_accepts_metric_movements() -> None:
    outcome = OutcomeRecord(
        ran_at="2026-01-01T00:00:00Z",
        pass_rate_delta=0.0,
        drift_loss_delta=0.0,
        scalar_score_delta=0.0,
        tournament_decision="promoted",
        metric_movements=(
            MetricMovementActual(
                metric_name="cost:tokens_spent",
                from_value=2000.0,
                to_value=1500.0,
                hypothesis_match=True,
                note="trim worked",
            ),
        ),
    )
    assert outcome.metric_movements[0].metric_name == "cost:tokens_spent"
    assert outcome.metric_movements[0].from_value == 2000.0
    assert outcome.metric_movements[0].to_value == 1500.0
