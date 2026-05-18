"""Tests for :func:`render_metric_movement_table` — the generalised
namespace-aware version of :func:`render_drift_kind_movement_table`."""

from __future__ import annotations

from zicato.core.types import (
    DriftMovementActual,
    Experiment,
    Generation,
    MetricMovementActual,
)
from zicato.epoch.analysis import (
    render_drift_kind_movement_table,
    render_metric_movement_table,
    render_tournament_outcomes_section,
)
from zicato.testing.fixtures import (
    make_experiment,
    make_generation,
    make_hypothesis_spec,
    make_outcome_record,
)


def _baseline(gid: str = "v0") -> Generation:
    return make_generation(id=gid, parent_id=None, promoted=True)


def _child(gid: str, parent_id: str, promoted: bool = True) -> Generation:
    return make_generation(id=gid, parent_id=parent_id, promoted=promoted)


def _experiment(
    gid: str,
    parent_id: str,
    *,
    drift_movements: tuple[DriftMovementActual, ...] = (),
    metric_movements: tuple[MetricMovementActual, ...] = (),
    decision: str = "promoted",
) -> Experiment:
    outcome = make_outcome_record(
        tournament_decision=decision,
        scalar_score_delta=-0.10,
        drift_movements=drift_movements,
        metric_movements=metric_movements,
    )
    return make_experiment(
        id=f"exp_{gid}",
        generation_id=gid,
        parent_generation_id=parent_id,
        hypothesis=make_hypothesis_spec(),
        outcome=outcome,
    )


def _drift_mv(kind: str, frm: float, to: float) -> DriftMovementActual:
    return DriftMovementActual(kind=kind, from_rate=frm, to_rate=to, hypothesis_match=True)


def _metric_mv(name: str, frm: float, to: float) -> MetricMovementActual:
    return MetricMovementActual(
        metric_name=name, from_value=frm, to_value=to, hypothesis_match=True
    )


# ---------------------------------------------------------------------------
# Drift-namespace path keeps the legacy header / column shape
# ---------------------------------------------------------------------------


def test_drift_namespace_filter_renders_legacy_drift_table() -> None:
    gens = [_baseline("v0"), _child("v1", "v0")]
    exps = [_experiment("v1", "v0", drift_movements=(_drift_mv("off_topic", 1.0, 0.5),))]
    out = render_metric_movement_table(gens, exps, namespace_filter="drift:")
    # Legacy header uses ``drift_kind`` + ``_rate`` columns.
    assert "drift_kind" in out
    assert "v0_rate" in out
    assert "final_rate" in out
    # Namespace prefix is stripped in the display column.
    assert "off_topic" in out
    assert "drift:off_topic" not in out


def test_drift_kind_movement_table_back_compat_wrapper_matches_legacy() -> None:
    """The back-compat wrapper produces output equivalent to filtering on drift."""
    gens = [_baseline("v0"), _child("v1", "v0")]
    exps = [_experiment("v1", "v0", drift_movements=(_drift_mv("off_topic", 1.0, 0.5),))]
    legacy = render_drift_kind_movement_table(gens, exps)
    filtered = render_metric_movement_table(gens, exps, namespace_filter="drift:")
    assert legacy == filtered


# ---------------------------------------------------------------------------
# Mixed-namespace path with namespace_filter=None
# ---------------------------------------------------------------------------


def test_no_namespace_filter_shows_all_namespaces() -> None:
    gens = [_baseline("v0"), _child("v1", "v0")]
    exps = [
        _experiment(
            "v1",
            "v0",
            drift_movements=(_drift_mv("off_topic", 1.0, 0.5),),
            metric_movements=(
                _metric_mv("cost:tokens_spent", 2000.0, 1500.0),
                _metric_mv("rubric:slide_structure", 3.0, 4.0),
                _metric_mv("latency:p95_turn_ms", 1800.0, 1500.0),
            ),
        )
    ]
    out = render_metric_movement_table(gens, exps, namespace_filter=None)
    assert "metric" in out  # Header label.
    assert "v0_value" in out
    assert "final_value" in out
    # Every namespaced metric surfaces (with its full prefixed name).
    assert "drift:off_topic" in out
    assert "cost:tokens_spent" in out
    assert "rubric:slide_structure" in out
    assert "latency:p95_turn_ms" in out


def test_cost_namespace_filter_shows_only_cost() -> None:
    gens = [_baseline("v0"), _child("v1", "v0")]
    exps = [
        _experiment(
            "v1",
            "v0",
            drift_movements=(_drift_mv("off_topic", 1.0, 0.5),),
            metric_movements=(
                _metric_mv("cost:tokens_spent", 2000.0, 1500.0),
                _metric_mv("rubric:slide_structure", 3.0, 4.0),
            ),
        )
    ]
    out = render_metric_movement_table(gens, exps, namespace_filter="cost:")
    # Cost metric present with prefix stripped.
    assert "tokens_spent" in out
    # Drift / rubric metrics excluded.
    assert "off_topic" not in out
    assert "slide_structure" not in out


def test_empty_when_no_movements_in_namespace() -> None:
    gens = [_baseline("v0"), _child("v1", "v0")]
    exps = [
        _experiment(
            "v1",
            "v0",
            drift_movements=(_drift_mv("off_topic", 1.0, 0.5),),
        )
    ]
    # Drift namespace has movements → non-empty.
    assert render_metric_movement_table(gens, exps, namespace_filter="drift:") != ""
    # Cost namespace doesn't → empty.
    assert render_metric_movement_table(gens, exps, namespace_filter="cost:") == ""


# ---------------------------------------------------------------------------
# render_tournament_outcomes_section — drift table + non-drift table coexist
# ---------------------------------------------------------------------------


def test_tournament_section_keeps_legacy_drift_subsection_heading() -> None:
    """Back-compat: when only drift movements are present the section
    renders with the historical heading."""
    gens = [_baseline("v0"), _child("v1", "v0")]
    exps = [_experiment("v1", "v0", drift_movements=(_drift_mv("off_topic", 1.0, 0.5),))]
    out = render_tournament_outcomes_section(gens, exps)
    assert "### Drift-kind movements across the promoted lineage" in out
    # No non-drift section when no non-drift movements.
    assert "### Metric movements (non-drift namespaces)" not in out


def test_tournament_section_adds_non_drift_subsection_when_metric_movements_present() -> None:
    gens = [_baseline("v0"), _child("v1", "v0")]
    exps = [
        _experiment(
            "v1",
            "v0",
            drift_movements=(_drift_mv("off_topic", 1.0, 0.5),),
            metric_movements=(_metric_mv("cost:tokens_spent", 2000.0, 1500.0),),
        )
    ]
    out = render_tournament_outcomes_section(gens, exps)
    # Drift subsection still present (back-compat).
    assert "### Drift-kind movements across the promoted lineage" in out
    # Non-drift subsection added.
    assert "### Metric movements (non-drift namespaces)" in out
    assert "cost:tokens_spent" in out
    # The cost metric is NOT mistakenly duplicated into the drift table.
    # (The drift table only contains drift:* entries — namespace_filter="drift:".)
