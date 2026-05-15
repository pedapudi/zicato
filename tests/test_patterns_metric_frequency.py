"""Tests for the generalised :func:`detect_metric_frequency` detector and
its namespace-specific wrappers (cost, rubric, drift)."""

from __future__ import annotations

from zicato.core import BoardEntry, DriftCount, LossProfile, MetricCount
from zicato.patterns import (
    DetectorInput,
    detect_cost_outliers,
    detect_drift_kind_frequency,
    detect_metric_frequency,
    detect_rubric_score_movement,
)


def _entry(entry_id: str = "e1") -> BoardEntry:
    return BoardEntry(
        id=entry_id,
        kind="single_turn",
        wall_clock_budget_seconds=60,
        input="hi",
    )


def _loss(
    *,
    run_id: str,
    entry_id: str = "e1",
    drift_counts: tuple[DriftCount, ...] = (),
    metric_counts: tuple[MetricCount, ...] = (),
) -> LossProfile:
    return LossProfile(
        run_id=run_id,
        entry_id=entry_id,
        generation_id="g0",
        epoch_id="ep0",
        drift_counts=drift_counts,
        plan_revisions=0,
        task_failure_ratio=0.0,
        runtime_ms=1000,
        wall_clock_budget_exceeded=False,
        expectation_result=None,
        drift_loss=0.0,
        pass_fail=None,
        metric_counts=metric_counts,
    )


# ---------------------------------------------------------------------------
# Back-compat: detect_drift_kind_frequency is now a wrapper but identical shape
# ---------------------------------------------------------------------------


def test_drift_namespace_wrapper_matches_legacy_shape() -> None:
    """detect_drift_kind_frequency emits ``drift_kind_frequency`` patterns
    with the historical detail keys (``drift_kind``, ``hits``, ...)."""
    losses = [
        _loss(
            run_id=f"r{i}",
            drift_counts=(DriftCount(kind="off_topic", severity="warning", count=1),)
            if i < 3
            else (),
        )
        for i in range(10)
    ]
    inp = DetectorInput(losses=losses, entries={"e1": _entry()}, events_paths={})

    legacy = detect_drift_kind_frequency(inp)
    generic = detect_metric_frequency(inp, namespace="drift:")

    assert len(legacy) == 1
    assert legacy[0].kind == "drift_kind_frequency"
    # Back-compat detail keys.
    assert legacy[0].detail["drift_kind"] == "off_topic"
    assert legacy[0].detail["hits"] == "3"
    assert legacy[0].detail["max_severity"] == "warning"

    # The generic version emits the same logical findings but with the
    # default ``drift_metric_frequency`` Pattern.kind.
    assert len(generic) == 1
    assert generic[0].kind == "drift_metric_frequency"
    assert generic[0].detail["metric_name"] == "drift:off_topic"


# ---------------------------------------------------------------------------
# Cost namespace
# ---------------------------------------------------------------------------


def test_detect_metric_frequency_cost_namespace_finds_synthetic_outliers() -> None:
    """``namespace="cost:"`` catches a cost metric firing in >=20% of runs."""
    losses = []
    for i in range(10):
        mc = (
            (MetricCount(name="cost:tokens_spent", count=5000.0),)
            if i < 4
            else (MetricCount(name="cost:tokens_spent", count=0.0),)
        )
        losses.append(_loss(run_id=f"r{i}", metric_counts=mc))
    inp = DetectorInput(losses=losses, entries={"e1": _entry()}, events_paths={})

    patterns = detect_metric_frequency(inp, namespace="cost:")
    assert len(patterns) == 1
    pat = patterns[0]
    assert pat.kind == "cost_metric_frequency"
    assert pat.detail["metric_name"] == "cost:tokens_spent"
    assert pat.detail["hits"] == "4"
    assert pat.detail["run_count"] == "10"
    # Cost metrics have no severity → fallback to "info".
    assert pat.detail["max_severity"] == "info"


def test_detect_cost_outliers_namespace_wrapper_matches() -> None:
    losses = [
        _loss(
            run_id=f"r{i}",
            metric_counts=(MetricCount(name="cost:llm_calls", count=2.0),) if i < 3 else (),
        )
        for i in range(10)
    ]
    inp = DetectorInput(losses=losses, entries={"e1": _entry()}, events_paths={})
    patterns = detect_cost_outliers(inp)
    assert len(patterns) == 1
    assert patterns[0].kind == "cost_metric_frequency"
    assert patterns[0].detail["metric_name"] == "cost:llm_calls"


def test_cost_metric_frequency_skips_below_threshold() -> None:
    """Only 10% of runs fire → below the 0.20 floor → no Pattern."""
    losses = [
        _loss(
            run_id=f"r{i}",
            metric_counts=(MetricCount(name="cost:tokens_spent", count=100.0),) if i == 0 else (),
        )
        for i in range(10)
    ]
    inp = DetectorInput(losses=losses, entries={"e1": _entry()}, events_paths={})
    assert detect_cost_outliers(inp) == []


# ---------------------------------------------------------------------------
# Rubric namespace
# ---------------------------------------------------------------------------


def test_detect_rubric_score_movement_detects_rubric_metrics() -> None:
    losses = []
    for i in range(10):
        mc = (
            (MetricCount(name="rubric:slide_structure", count=4.0),)
            if i < 5
            else (MetricCount(name="rubric:slide_structure", count=0.0),)
        )
        losses.append(_loss(run_id=f"r{i}", metric_counts=mc))
    inp = DetectorInput(losses=losses, entries={"e1": _entry()}, events_paths={})

    patterns = detect_rubric_score_movement(inp)
    assert len(patterns) == 1
    pat = patterns[0]
    assert pat.kind == "rubric_metric_frequency"
    assert pat.detail["metric_name"] == "rubric:slide_structure"
    assert pat.detail["hits"] == "5"


# ---------------------------------------------------------------------------
# Mixed namespaces — detector returns only the requested namespace
# ---------------------------------------------------------------------------


def test_detect_metric_frequency_filters_to_requested_namespace_only() -> None:
    """When several namespaces are present, the detector returns only its own."""
    losses = []
    for i in range(10):
        losses.append(
            _loss(
                run_id=f"r{i}",
                drift_counts=(DriftCount(kind="off_topic", severity="info", count=1),),
                metric_counts=(
                    MetricCount(name="cost:tokens_spent", count=100.0),
                    MetricCount(name="rubric:slide_structure", count=3.0),
                ),
            )
        )
    inp = DetectorInput(losses=losses, entries={"e1": _entry()}, events_paths={})

    drift_only = detect_metric_frequency(inp, namespace="drift:")
    cost_only = detect_metric_frequency(inp, namespace="cost:")
    rubric_only = detect_metric_frequency(inp, namespace="rubric:")

    # Each detector surfaces only its namespace.
    assert {p.detail["metric_name"] for p in drift_only} == {"drift:off_topic"}
    assert {p.detail["metric_name"] for p in cost_only} == {"cost:tokens_spent"}
    assert {p.detail["metric_name"] for p in rubric_only} == {"rubric:slide_structure"}


def test_detect_metric_frequency_empty_namespace_matches_all() -> None:
    """An empty namespace prefix matches every metric across every namespace."""
    losses = []
    for i in range(10):
        losses.append(
            _loss(
                run_id=f"r{i}",
                drift_counts=(DriftCount(kind="off_topic", severity="info", count=1),),
                metric_counts=(MetricCount(name="cost:tokens_spent", count=10.0),),
            )
        )
    inp = DetectorInput(losses=losses, entries={"e1": _entry()}, events_paths={})

    patterns = detect_metric_frequency(inp, namespace="")
    names = {p.detail["metric_name"] for p in patterns}
    assert "drift:off_topic" in names
    assert "cost:tokens_spent" in names


def test_detect_metric_frequency_returns_empty_on_empty_input() -> None:
    inp = DetectorInput(losses=[], entries={}, events_paths={})
    assert detect_metric_frequency(inp, namespace="cost:") == []
    assert detect_cost_outliers(inp) == []
    assert detect_rubric_score_movement(inp) == []


def test_detect_metric_frequency_ignores_zero_or_negative_counts() -> None:
    """A metric with count <= 0 is not a hit."""
    losses = [
        _loss(
            run_id=f"r{i}",
            metric_counts=(MetricCount(name="cost:tokens_spent", count=0.0),),
        )
        for i in range(10)
    ]
    inp = DetectorInput(losses=losses, entries={"e1": _entry()}, events_paths={})
    assert detect_cost_outliers(inp) == []
