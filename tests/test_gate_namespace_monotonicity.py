"""Tests for the per-namespace monotonicity rule in the promote gate.

Covers the third gate rule introduced alongside the multi-objective
scoring surface. The rule enforces "no regression on this axis" for
namespaces whose :attr:`ScoringWeights.namespace_monotonicity` flag is
``True``, irrespective of whether the combined scalar improved.

For the rule's docstring + sign convention, see
``zicato.tournament.gate``. For the aggregator side, see
``tests/test_scoring_multi_objective.py``.
"""

from __future__ import annotations

from zicato.core import ScoringWeights
from zicato.tournament.gate import GateOutcome, evaluate_gate


def _agg(
    *,
    scalar: float,
    pass_rate: float = 1.0,
    per_entry: dict[str, dict[str, object]] | None = None,
    namespace_aggregates: dict[str, float] | None = None,
) -> dict[str, object]:
    """Build a minimal aggregate dict the gate accepts."""
    return {
        "drift_loss_mean": scalar,
        "pass_rate": pass_rate,
        "expectation_count": len(per_entry or {}),
        "entry_count": len(per_entry or {}),
        "scalar": scalar,
        "per_entry": per_entry or {},
        "namespace_aggregates": namespace_aggregates or {},
    }


# ---------------------------------------------------------------------------
# Cases where namespace monotonicity FIRES
# ---------------------------------------------------------------------------


def test_one_namespace_fails_others_fine_rejects_cited_namespace() -> None:
    """One regressing namespace causes rejection with that namespace named."""
    parent = _agg(
        scalar=2.0,
        namespace_aggregates={
            "drift:": 1.0,
            "rubric:": -4.0,
            "schema:": 0.0,
        },
    )
    # Rubric weighted aggregate worsened (less negative = lower quality).
    child = _agg(
        scalar=1.0,
        namespace_aggregates={
            "drift:": 0.5,
            "rubric:": -2.0,  # weighted; means raw rubric dropped 4 → 2
            "schema:": 0.0,
        },
    )
    outcome = evaluate_gate(parent, child, ScoringWeights())
    assert isinstance(outcome, GateOutcome)
    assert outcome.decision == "rejected"
    assert "monotonicity_regression on namespace=" in outcome.reason
    assert "rubric:" in outcome.reason


def test_schema_namespace_regression_rejects() -> None:
    """Introducing schema failures rejects even if drift and rubric improve."""
    parent = _agg(
        scalar=5.0,
        namespace_aggregates={
            "drift:": 1.0,
            "rubric:": -4.0,
            "schema:": 0.0,
        },
    )
    child = _agg(
        scalar=4.0,
        namespace_aggregates={
            "drift:": 0.5,
            "rubric:": -5.0,
            "schema:": 5.0,  # new schema failures
        },
    )
    outcome = evaluate_gate(parent, child, ScoringWeights())
    assert outcome.decision == "rejected"
    assert "schema:" in outcome.reason


def test_multiple_namespaces_fail_all_listed() -> None:
    """Multiple regressing namespaces are listed alphabetically in the reason."""
    parent = _agg(
        scalar=2.0,
        namespace_aggregates={
            "drift:": 1.0,
            "rubric:": -4.0,
            "schema:": 0.0,
        },
    )
    child = _agg(
        scalar=1.5,
        namespace_aggregates={
            "drift:": 0.5,
            "rubric:": -2.0,  # regression
            "schema:": 3.0,  # regression
        },
    )
    outcome = evaluate_gate(parent, child, ScoringWeights())
    assert outcome.decision == "rejected"
    # Sorted alphabetically, rubric: comes before schema:, and each is cited
    # with the two weighted aggregates the rule compared (issue #129).
    assert outcome.reason.index("rubric:") < outcome.reason.index("schema:")
    assert "rubric: (champion -4.000000 -> challenger -2.000000)" in outcome.reason
    assert "schema: (champion 0.000000 -> challenger 3.000000)" in outcome.reason


def test_namespace_regression_rejects_even_when_scalar_improves() -> None:
    """The namespace rule fires even when the overall scalar comfortably improves."""
    parent = _agg(
        scalar=10.0,
        namespace_aggregates={"rubric:": -5.0},
    )
    child = _agg(
        scalar=1.0,  # huge scalar improvement
        namespace_aggregates={"rubric:": -2.0},  # but rubric dropped
    )
    outcome = evaluate_gate(parent, child, ScoringWeights())
    assert outcome.decision == "rejected"
    assert "rubric:" in outcome.reason


# ---------------------------------------------------------------------------
# Cases where namespace monotonicity does NOT fire
# ---------------------------------------------------------------------------


def test_all_namespaces_fine_scalar_margin_met_promotes() -> None:
    """No namespace regresses and scalar improves enough → promoted."""
    parent = _agg(
        scalar=2.0,
        namespace_aggregates={
            "drift:": 1.0,
            "rubric:": -4.0,
            "schema:": 0.0,
        },
    )
    child = _agg(
        scalar=1.0,
        namespace_aggregates={
            "drift:": 0.5,
            "rubric:": -5.0,  # better
            "schema:": 0.0,  # tied
        },
    )
    outcome = evaluate_gate(parent, child, ScoringWeights())
    assert outcome.decision == "promoted"
    assert outcome.reason == ""


def test_namespace_monotonicity_unset_skips_check() -> None:
    """A namespace whose flag is missing is not gated for monotonicity."""
    parent = _agg(
        scalar=2.0,
        namespace_aggregates={"cost:": 1.0},
    )
    # cost worsens significantly but cost is not in namespace_monotonicity
    # by default → no rejection on that axis.
    child = _agg(
        scalar=1.0,
        namespace_aggregates={"cost:": 100.0},
    )
    outcome = evaluate_gate(parent, child, ScoringWeights())
    assert outcome.decision == "promoted"


def test_namespace_monotonicity_false_skips_check() -> None:
    """Drift defaults to monotonicity=False so drift regression doesn't gate."""
    parent = _agg(
        scalar=2.0,
        namespace_aggregates={"drift:": 0.0},
    )
    child = _agg(
        scalar=1.0,
        namespace_aggregates={"drift:": 5.0},  # drift worsened
    )
    # Drift's flag is False by default → no rejection on the drift axis.
    outcome = evaluate_gate(parent, child, ScoringWeights())
    assert outcome.decision == "promoted"


def test_zero_weight_namespace_skipped_even_when_monotonicity_true() -> None:
    """A namespace with zero weight has no defined direction → rule skipped."""
    parent = _agg(
        scalar=2.0,
        namespace_aggregates={"output:": 0.0},
    )
    child = _agg(
        scalar=1.0,
        namespace_aggregates={"output:": 0.0},
    )
    weights = ScoringWeights(
        namespace_weights={"drift:": 1.0, "failure:": 1.0, "output:": 0.0},
        namespace_monotonicity={"output:": True},  # explicitly enabled
    )
    outcome = evaluate_gate(parent, child, weights)
    assert outcome.decision == "promoted"


def test_namespace_missing_from_both_aggregates_skipped() -> None:
    """No data on either side → namespace cannot be judged, so it is skipped."""
    parent = _agg(scalar=2.0, namespace_aggregates={})
    child = _agg(scalar=1.0, namespace_aggregates={})
    outcome = evaluate_gate(parent, child, ScoringWeights())
    assert outcome.decision == "promoted"


def test_pass_rate_regression_takes_precedence_over_namespace() -> None:
    """Both rules fire — the pass-rate rule fires first by ordering."""
    parent = _agg(
        scalar=2.0,
        pass_rate=1.0,
        per_entry={"a": {"pass_fail": True}},
        namespace_aggregates={"rubric:": -4.0},
    )
    child = _agg(
        scalar=1.0,
        pass_rate=0.0,
        per_entry={"a": {"pass_fail": False}},
        namespace_aggregates={"rubric:": -2.0},  # also a regression
    )
    outcome = evaluate_gate(parent, child, ScoringWeights())
    assert outcome.decision == "rejected"
    # Pass-rate regression rule fires before the namespace rule.
    assert "pass-rate regression on entries" in outcome.reason
    assert "monotonicity_regression on namespace=" not in outcome.reason
