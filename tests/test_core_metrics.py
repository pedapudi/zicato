"""Tests for the generalised metric model: MetricCount + LossProfile.unified_metrics().

These tests exercise the namespace-aware metric surface introduced as a
back-compat superset of the original drift-only types. Drift remains
the canonical namespace; the new code must accept arbitrary other
namespaces (cost, rubric, latency, schema, output, ...) without
disturbing any drift-side invariant.
"""

from __future__ import annotations

import dataclasses

import pytest

from zicato.core import (
    DriftCount,
    ExpectationResult,
    ExpectedDriftMovement,
    ExpectedMetricMovement,
    HypothesisSpec,
    LossProfile,
    MetricCount,
    MetricMovementActual,
    OutcomeRecord,
)

# ---------------------------------------------------------------------------
# MetricCount construction + DriftCount round-trip
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


def test_metric_count_from_drift_count_round_trip() -> None:
    dc = DriftCount(kind="off_topic", severity="warning", count=3)
    mc = MetricCount.from_drift_count(dc)
    assert mc.name == "drift:off_topic"
    assert mc.severity == "warning"
    assert mc.count == 3.0
    # Round-tripping the kind is namespace-prefix-stable.
    assert mc.name.startswith("drift:")
    assert mc.name.split(":", 1)[1] == dc.kind


def test_metric_count_is_frozen() -> None:
    mc = MetricCount(name="cost:tokens_spent", count=100.0)
    with pytest.raises(dataclasses.FrozenInstanceError):
        mc.count = 0.0  # type: ignore[misc]


# ---------------------------------------------------------------------------
# LossProfile.unified_metrics — back-compat shape
# ---------------------------------------------------------------------------


def _bare_profile(**overrides: object) -> LossProfile:
    """Build a minimal :class:`LossProfile` for unified_metrics tests."""
    kwargs: dict[str, object] = {
        "run_id": "r1",
        "entry_id": "e1",
        "generation_id": "v0",
        "epoch_id": "epoch-001",
        "drift_counts": (),
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


def test_unified_metrics_with_only_drift_counts_yields_drift_namespace() -> None:
    profile = _bare_profile(
        drift_counts=(
            DriftCount(kind="off_topic", severity="warning", count=2),
            DriftCount(kind="tool_error", severity="info", count=1),
        ),
    )
    unified = profile.unified_metrics()
    names = [m.name for m in unified]
    assert names == ["drift:off_topic", "drift:tool_error"]
    assert all(m.name.startswith("drift:") for m in unified)
    severities = {m.severity for m in unified}
    assert severities == {"warning", "info"}


def test_unified_metrics_empty_when_no_signals() -> None:
    assert _bare_profile().unified_metrics() == ()


def test_unified_metrics_synthesises_scalar_fields_when_metric_counts_unset() -> None:
    profile = _bare_profile(
        drift_counts=(DriftCount(kind="off_topic", severity="info", count=1),),
        tokens_spent=4242,
        output_chars=1024,
        schema_failures=2,
    )
    names = {m.name: m.count for m in profile.unified_metrics()}
    assert names["drift:off_topic"] == 1.0
    assert names["cost:tokens_spent"] == 4242.0
    assert names["output:chars"] == 1024.0
    assert names["schema:failures"] == 2.0


def test_unified_metrics_with_explicit_metric_counts_includes_all_namespaces() -> None:
    profile = _bare_profile(
        drift_counts=(DriftCount(kind="off_topic", severity="warning", count=1),),
        metric_counts=(
            MetricCount(name="cost:input_tokens", count=1500.0),
            MetricCount(name="cost:output_tokens", count=500.0),
            MetricCount(name="rubric:slide_structure", count=4.0),
            MetricCount(name="latency:p95_turn_ms", count=2400.0),
        ),
    )
    names = {m.name for m in profile.unified_metrics()}
    assert "drift:off_topic" in names
    assert "cost:input_tokens" in names
    assert "cost:output_tokens" in names
    assert "rubric:slide_structure" in names
    assert "latency:p95_turn_ms" in names
    # Distinct namespaces all surfaced.
    namespaces = {m.name.split(":", 1)[0] for m in profile.unified_metrics()}
    assert namespaces == {"drift", "cost", "rubric", "latency"}


def test_unified_metrics_does_not_double_count_when_metric_counts_mirrors_drift() -> None:
    # Reducer is allowed to put the drift entries inside metric_counts as
    # well; unified_metrics() must dedupe on (name, severity).
    profile = _bare_profile(
        drift_counts=(DriftCount(kind="off_topic", severity="warning", count=1),),
        metric_counts=(
            MetricCount(name="drift:off_topic", severity="warning", count=1.0),
            MetricCount(name="cost:tokens_spent", count=200.0),
        ),
    )
    unified = profile.unified_metrics()
    names = [m.name for m in unified]
    # drift:off_topic appears exactly once.
    assert names.count("drift:off_topic") == 1
    assert "cost:tokens_spent" in names


def test_unified_metrics_skips_zero_scalar_fields_when_metric_counts_unset() -> None:
    # Empty scalars should not flood the unified view with zero rows.
    profile = _bare_profile()
    assert profile.unified_metrics() == ()


# ---------------------------------------------------------------------------
# HypothesisSpec — back-compat with old drift movements + new metric movements
# ---------------------------------------------------------------------------


def test_hypothesis_spec_back_compat_default_metric_movements_is_empty() -> None:
    hyp = HypothesisSpec(
        core_idea="Tighten the system prompt.",
        modulating=("router__system_prompt",),
        why="Off-topic dominates.",
        expected_drift_movements=(
            ExpectedDriftMovement(kind="off_topic", direction="decrease", magnitude="small"),
        ),
        expected_pass_rate_delta="+0.05",
    )
    # Old field still works.
    assert hyp.expected_drift_movements[0].kind == "off_topic"
    # New field defaults to empty.
    assert hyp.expected_metric_movements == ()


def test_hypothesis_spec_accepts_metric_movements_alongside_drift() -> None:
    hyp = HypothesisSpec(
        core_idea="Cut token cost by trimming the prompt.",
        modulating=("router__system_prompt",),
        why="Cost dominates value at high token counts.",
        expected_drift_movements=(),
        expected_pass_rate_delta="+0.00",
        expected_metric_movements=(
            ExpectedMetricMovement(
                metric_name="cost:tokens_spent",
                direction="decrease",
                magnitude="medium",
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
# OutcomeRecord — back-compat with drift_movements + new metric_movements
# ---------------------------------------------------------------------------


def test_outcome_record_default_metric_movements_is_empty() -> None:
    outcome = OutcomeRecord(
        ran_at="2026-01-01T00:00:00Z",
        drift_movements=(),
        pass_rate_delta=0.0,
        drift_loss_delta=0.0,
        scalar_score_delta=0.0,
        tournament_decision="deferred",
    )
    assert outcome.metric_movements == ()


def test_outcome_record_accepts_metric_movements() -> None:
    outcome = OutcomeRecord(
        ran_at="2026-01-01T00:00:00Z",
        drift_movements=(),
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


def test_loss_profile_drift_counts_back_compat_still_supports_unset_scalar_fields() -> None:
    # The exact construction pattern used across the existing test suite —
    # building a LossProfile with only drift_counts and no extras — must
    # still work without surfacing the new fields.
    profile = LossProfile(
        run_id="r1",
        entry_id="e1",
        generation_id="v0",
        epoch_id="epoch-001",
        drift_counts=(DriftCount(kind="off_topic", severity="warning", count=2),),
        plan_revisions=1,
        task_failure_ratio=0.0,
        runtime_ms=1234,
        wall_clock_budget_exceeded=False,
        expectation_result=ExpectationResult(kind="regex", passed=True, detail="x"),
        drift_loss=6.0,
        pass_fail=True,
    )
    assert profile.metric_counts == ()
    assert profile.tokens_spent == 0
    assert profile.output_chars == 0
    assert profile.schema_failures == 0
