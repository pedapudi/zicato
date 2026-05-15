"""Tests for ``zicato.tournament.gate``."""

from __future__ import annotations

import pytest

from zicato.core import ScoringWeights
from zicato.tournament.gate import GateOutcome, evaluate_gate


def _agg(
    *,
    scalar: float,
    pass_rate: float = 1.0,
    per_entry: dict[str, dict[str, object]] | None = None,
) -> dict[str, object]:
    """Build a minimal aggregate dict that the gate accepts."""
    return {
        "drift_loss_mean": scalar,  # not inspected by the gate; mirror is fine
        "pass_rate": pass_rate,
        "expectation_count": len(per_entry or {}),
        "entry_count": len(per_entry or {}),
        "scalar": scalar,
        "per_entry": per_entry or {},
    }


def test_gate_promotes_when_child_beats_parent_by_margin() -> None:
    parent = _agg(scalar=1.0, pass_rate=0.5, per_entry={"a": {"pass_fail": True}})
    child = _agg(scalar=0.5, pass_rate=1.0, per_entry={"a": {"pass_fail": True}})
    weights = ScoringWeights(promote_margin=0.1)

    outcome = evaluate_gate(parent, child, weights)

    assert isinstance(outcome, GateOutcome)
    assert outcome.decision == "promoted"
    assert outcome.reason == ""
    assert outcome.delta_scalar == -0.5
    assert outcome.delta_pass_rate == 0.5


def test_gate_rejects_when_margin_insufficient() -> None:
    parent = _agg(scalar=1.0)
    child = _agg(scalar=0.995)  # better, but by less than the 0.01 margin
    weights = ScoringWeights(promote_margin=0.01)

    outcome = evaluate_gate(parent, child, weights)

    assert outcome.decision == "rejected"
    assert "insufficient margin" in outcome.reason
    # Deltas reported regardless of decision
    assert outcome.delta_scalar == pytest.approx(-0.005)


def test_gate_rejects_when_child_ties_parent() -> None:
    parent = _agg(scalar=1.0)
    child = _agg(scalar=1.0)
    weights = ScoringWeights(promote_margin=0.01)

    outcome = evaluate_gate(parent, child, weights)

    assert outcome.decision == "rejected"
    assert "insufficient margin" in outcome.reason


def test_gate_rejects_on_pass_rate_regression_even_when_scalar_improves() -> None:
    """The strict-monotonicity rule overrides scalar improvement."""
    parent = _agg(
        scalar=2.0,
        pass_rate=1.0,
        per_entry={
            "a": {"pass_fail": True},
            "b": {"pass_fail": True},
        },
    )
    child = _agg(
        scalar=0.5,
        pass_rate=0.5,
        per_entry={
            "a": {"pass_fail": True},
            "b": {"pass_fail": False},  # regression here
        },
    )
    weights = ScoringWeights(promote_margin=0.01, pass_rate_monotonicity=True)

    outcome = evaluate_gate(parent, child, weights)

    assert outcome.decision == "rejected"
    assert "pass-rate regression on entries" in outcome.reason
    assert "b" in outcome.reason


def test_gate_lists_multiple_regressed_entries_alphabetically() -> None:
    parent = _agg(
        scalar=2.0,
        per_entry={
            "alpha": {"pass_fail": True},
            "bravo": {"pass_fail": True},
            "charlie": {"pass_fail": True},
        },
    )
    child = _agg(
        scalar=0.5,
        per_entry={
            "alpha": {"pass_fail": False},
            "bravo": {"pass_fail": True},
            "charlie": {"pass_fail": None},  # also counts as a regression
        },
    )
    outcome = evaluate_gate(parent, child, ScoringWeights())

    assert outcome.decision == "rejected"
    # Entries listed in sorted order
    assert "alpha, charlie" in outcome.reason


def test_gate_ignores_pass_rate_regression_when_monotonicity_disabled() -> None:
    """When the operator opts out, scalar improvement alone is enough."""
    parent = _agg(
        scalar=2.0,
        pass_rate=1.0,
        per_entry={"a": {"pass_fail": True}},
    )
    child = _agg(
        scalar=0.5,
        pass_rate=0.0,
        per_entry={"a": {"pass_fail": False}},
    )
    weights = ScoringWeights(
        promote_margin=0.01,
        pass_rate_monotonicity=False,
    )

    outcome = evaluate_gate(parent, child, weights)

    assert outcome.decision == "promoted"
    assert outcome.reason == ""


def test_gate_does_not_penalize_entries_parent_failed() -> None:
    """A child that improves a parent-failed entry is fine; only parent-pass→child-fail trips the gate."""
    parent = _agg(
        scalar=2.0,
        per_entry={
            "a": {"pass_fail": True},
            "b": {"pass_fail": False},
        },
    )
    child = _agg(
        scalar=0.5,
        per_entry={
            "a": {"pass_fail": True},
            "b": {"pass_fail": False},  # still failing — not a regression
        },
    )
    outcome = evaluate_gate(parent, child, ScoringWeights())
    assert outcome.decision == "promoted"


def test_gate_treats_missing_child_entry_as_regression() -> None:
    """An entry the parent passed that the child did not run regresses the gate."""
    parent = _agg(
        scalar=2.0,
        per_entry={
            "a": {"pass_fail": True},
            "b": {"pass_fail": True},
        },
    )
    child = _agg(
        scalar=0.5,
        per_entry={"a": {"pass_fail": True}},  # b absent
    )
    outcome = evaluate_gate(parent, child, ScoringWeights())
    assert outcome.decision == "rejected"
    assert "b" in outcome.reason
