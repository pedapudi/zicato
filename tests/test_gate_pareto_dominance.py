"""Tests for rule 1a of the promote gate: Pareto dominance.

Rule 1a is active only when :attr:`ScoringWeights.pareto_objectives`
declares a profile. The rule compares the challenger to the champion on
the declared axes. Each axis is lower-is-better.

The verdict of the rule controls the remainder of the gate:

* ``dominated`` — the gate rejects the challenger immediately.
* ``dominates`` — the gate does not apply the scalar margin of rule 1.
  Rules 2 and 3 still apply.
* ``incomparable`` — the gate uses the scalar margin to break the tie.
* ``unranked`` — the gate uses the scalar rules without a change.

For the rule and its sequence, see the module docstring of
``zicato.tournament.gate``. For the read-side projection that uses the
same axis vocabulary, see ``tests/test_dashboard_server.py``.
"""

from __future__ import annotations

from zicato.core import ScoringWeights
from zicato.tournament.gate import (
    TournamentDecision,
    evaluate_gate,
    objective_vector,
    pareto_comparison,
)


def _agg(
    *,
    scalar: float,
    pass_rate: float = 1.0,
    drift_loss_mean: float | None = None,
    namespace_aggregates: dict[str, float] | None = None,
) -> dict[str, object]:
    """Build a minimal aggregate dict that the gate accepts."""
    return {
        "drift_loss_mean": scalar if drift_loss_mean is None else drift_loss_mean,
        "pass_rate": pass_rate,
        "expectation_count": 0,
        "entry_count": 0,
        "scalar": scalar,
        "per_entry": {},
        "namespace_aggregates": namespace_aggregates or {},
    }


def _weights(profile: dict[str, str], **kw: object) -> ScoringWeights:
    """Build weights with a declared profile and no rule 2 / rule 3 guards.

    The other rules are off. Thus each test shows the effect of rule 1a
    only.
    """
    params: dict[str, object] = {
        "pareto_objectives": profile,
        "pass_rate_monotonicity": False,
        "namespace_monotonicity": {},
    }
    params.update(kw)
    return ScoringWeights(**params)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# The projection — objective_vector
# ---------------------------------------------------------------------------


def test_objective_vector_reads_each_axis_kind() -> None:
    """The projection supports drift, quality, and namespace axes."""
    agg = _agg(scalar=0.4, pass_rate=0.75, namespace_aggregates={"cost:": 0.2})
    vector = objective_vector(agg, ["drift_loss", "quality_loss", "namespace:cost:"])
    assert vector == {
        "drift_loss": 0.4,
        "quality_loss": 0.25,
        "namespace:cost:": 0.2,
    }


def test_objective_vector_omits_an_axis_that_the_aggregate_lacks() -> None:
    """A missing axis is absent from the vector. It does not become zero.

    Zero is the best value on a lower-is-better axis. If the projection
    used zero, a generation that never measured an axis would dominate a
    generation that did measure it.
    """
    agg = _agg(scalar=0.4, namespace_aggregates={})
    assert objective_vector(agg, ["namespace:cost:"]) == {}


# ---------------------------------------------------------------------------
# The comparison — pareto_comparison
# ---------------------------------------------------------------------------


def test_comparison_reports_dominates_when_one_axis_improves_and_none_worsen() -> None:
    parent = _agg(scalar=0.5, namespace_aggregates={"cost:": 0.3})
    child = _agg(scalar=0.4, namespace_aggregates={"cost:": 0.3})
    weights = _weights({"drift_loss": "Drift", "namespace:cost:": "Cost"})

    verdict, improved, worsened = pareto_comparison(parent, child, weights)

    assert verdict == "dominates"
    assert improved == ["drift_loss"]
    assert worsened == []


def test_comparison_reports_dominated_when_one_axis_worsens_and_none_improve() -> None:
    parent = _agg(scalar=0.4, namespace_aggregates={"cost:": 0.3})
    child = _agg(scalar=0.5, namespace_aggregates={"cost:": 0.3})
    weights = _weights({"drift_loss": "Drift", "namespace:cost:": "Cost"})

    verdict, _improved, worsened = pareto_comparison(parent, child, weights)

    assert verdict == "dominated"
    assert worsened == ["drift_loss"]


def test_comparison_reports_incomparable_when_the_challenger_trades() -> None:
    """One axis improves and one axis worsens. No point dominates."""
    parent = _agg(scalar=0.5, namespace_aggregates={"cost:": 0.2})
    child = _agg(scalar=0.4, namespace_aggregates={"cost:": 0.6})
    weights = _weights({"drift_loss": "Drift", "namespace:cost:": "Cost"})

    verdict, improved, worsened = pareto_comparison(parent, child, weights)

    assert verdict == "incomparable"
    assert improved == ["drift_loss"]
    assert worsened == ["namespace:cost:"]


def test_comparison_reports_dominated_when_each_axis_holds_flat() -> None:
    """No axis improves. Thus the challenger does not dominate."""
    parent = _agg(scalar=0.4)
    child = _agg(scalar=0.4)
    weights = _weights({"drift_loss": "Drift"})

    verdict, improved, worsened = pareto_comparison(parent, child, weights)

    assert verdict == "dominated"
    assert improved == []
    assert worsened == []


def test_comparison_reports_unranked_when_no_axis_is_on_both_sides() -> None:
    """The profile names an axis that the aggregates do not report."""
    parent = _agg(scalar=0.5)
    child = _agg(scalar=0.4)
    weights = _weights({"namespace:latency:": "Latency"})

    verdict, improved, worsened = pareto_comparison(parent, child, weights)

    assert verdict == "unranked"
    assert improved == []
    assert worsened == []


# ---------------------------------------------------------------------------
# The gate decision
# ---------------------------------------------------------------------------


def test_gate_rejects_a_dominated_challenger_and_names_the_axis() -> None:
    """The reason gives the label of the operator and both values."""
    parent = _agg(scalar=0.4, namespace_aggregates={"cost:": 0.2})
    child = _agg(scalar=0.5, namespace_aggregates={"cost:": 0.2})
    weights = _weights({"drift_loss": "Drift", "namespace:cost:": "Cost"})

    outcome = evaluate_gate(parent, child, weights)

    assert outcome.decision == TournamentDecision.REJECTED
    assert "pareto_dominated" in outcome.reason
    assert "Drift" in outcome.reason
    assert "Cost" not in outcome.reason


def test_gate_rejects_a_challenger_that_holds_flat_on_each_objective() -> None:
    parent = _agg(scalar=0.4)
    child = _agg(scalar=0.4)
    weights = _weights({"drift_loss": "Drift"})

    outcome = evaluate_gate(parent, child, weights)

    assert outcome.decision == TournamentDecision.REJECTED
    assert "held flat" in outcome.reason


def test_gate_promotes_a_dominating_challenger_below_the_scalar_margin() -> None:
    """This is the primary effect of rule 1a.

    The challenger improves each declared objective, but the improvement
    is less than ``promote_margin``. Rule 1 alone would reject it. The
    operator declared the objectives. Thus the gate promotes it.
    """
    parent = _agg(scalar=0.400, namespace_aggregates={"cost:": 0.30})
    child = _agg(scalar=0.399, namespace_aggregates={"cost:": 0.29})
    weights = _weights(
        {"drift_loss": "Drift", "namespace:cost:": "Cost"},
        promote_margin=0.05,
    )

    # Without a profile the same duel is a reject: the margin is not met.
    bare = ScoringWeights(
        promote_margin=0.05,
        pass_rate_monotonicity=False,
        namespace_monotonicity={},
    )
    assert evaluate_gate(parent, child, bare).decision == TournamentDecision.REJECTED

    assert evaluate_gate(parent, child, weights).decision == TournamentDecision.PROMOTED


def test_gate_uses_the_scalar_margin_to_break_a_trade() -> None:
    """The challenger trades one objective for another.

    Dominance cannot rank a trade. Thus rule 1 decides. Here the scalar
    does not improve by the margin, so the gate rejects the challenger.
    """
    parent = _agg(scalar=0.40, namespace_aggregates={"cost:": 0.20})
    child = _agg(scalar=0.39, namespace_aggregates={"cost:": 0.60})
    weights = _weights(
        {"drift_loss": "Drift", "namespace:cost:": "Cost"},
        promote_margin=0.05,
    )

    outcome = evaluate_gate(parent, child, weights)

    assert outcome.decision == TournamentDecision.REJECTED
    assert "pareto_dominated" not in outcome.reason


def test_rule_2_still_vetoes_a_dominating_challenger() -> None:
    """Rule 1a does not disable the quality guard.

    The challenger improves the declared drift objective, but the
    pass-rate falls. Rule 2 rejects it.
    """
    parent = _agg(scalar=0.50, pass_rate=1.0)
    child = _agg(scalar=0.40, pass_rate=0.5)
    weights = ScoringWeights(
        pareto_objectives={"drift_loss": "Drift"},
        pass_rate_monotonicity=True,
        pass_rate_monotonicity_scope="aggregate",
        namespace_monotonicity={},
    )

    outcome = evaluate_gate(parent, child, weights)

    assert outcome.decision == TournamentDecision.REJECTED
    assert "pass" in outcome.reason.lower()


# ---------------------------------------------------------------------------
# The default stays inert
# ---------------------------------------------------------------------------


def test_an_empty_profile_leaves_the_decision_unchanged() -> None:
    """The default profile is empty. Thus rule 1a does not run.

    The two contracts are equal except for the empty profile. Thus each
    decision and each reason must agree.
    """
    parent = _agg(scalar=0.40)
    for child_scalar in (0.30, 0.399, 0.40, 0.50):
        child = _agg(scalar=child_scalar)
        without = evaluate_gate(
            parent,
            child,
            ScoringWeights(pass_rate_monotonicity=False, namespace_monotonicity={}),
        )
        with_empty = evaluate_gate(
            parent,
            child,
            ScoringWeights(
                pareto_objectives={},
                pass_rate_monotonicity=False,
                namespace_monotonicity={},
            ),
        )
        assert without.decision == with_empty.decision
        assert without.reason == with_empty.reason
