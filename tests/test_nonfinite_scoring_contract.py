"""Regression coverage for non-finite scoring-contract and gate inputs."""

from __future__ import annotations

import json
import math
from collections.abc import Callable

import pytest

from zicato.core import ScoringWeights
from zicato.core.scoring_config import LadderConfig, OverfittingConfig
from zicato.tournament.gate import evaluate_gate
from zicato.workspace_loader import scoring_weights_from_dict


@pytest.mark.parametrize(
    "factory",
    [
        pytest.param(lambda: ScoringWeights(promote_margin=math.nan), id="promote-margin"),
        pytest.param(
            lambda: ScoringWeights(overfitting=OverfittingConfig(holdout_fraction=math.nan)),
            id="holdout-fraction",
        ),
        pytest.param(
            lambda: ScoringWeights(
                overfitting=OverfittingConfig(ladder=LadderConfig(noise_scale=math.inf))
            ),
            id="ladder-noise-scale",
        ),
        pytest.param(
            lambda: ScoringWeights(namespace_weights={"drift:": math.inf}),
            id="namespace-weight",
        ),
    ],
)
def test_scoring_contract_rejects_nonfinite_knobs(factory: Callable[[], ScoringWeights]) -> None:
    """Every evaluation-contract numeric path rejects NaN / infinity at construction."""
    with pytest.raises(ValueError, match="must be finite"):
        factory()


def test_scoring_config_loader_rejects_nan_promotion_margin() -> None:
    """JSON's permissive NaN spelling cannot enter a frozen scoring contract."""
    raw = json.loads('{"promote_margin": NaN}')

    with pytest.raises(ValueError, match="promote_margin must be finite"):
        scoring_weights_from_dict(raw)


def test_scoring_config_accepts_an_int_too_large_for_float() -> None:
    """An oversized JSON integer is a clean ValueError, not an OverflowError.

    ``float(10**400)`` raises ``OverflowError``; an ``int`` is finite by
    construction, so the range check below is what must reject this one.
    """
    with pytest.raises(ValueError, match="budget must be >= 0"):
        LadderConfig(budget=-(10**400))
    assert LadderConfig(budget=10**400).budget == 10**400


# --- the gate: every rule is a REJECTION condition, so a NaN SKIPS it --------
#
# Each case below promoted a strictly worse challenger with the empty reason
# that means "clean win" before this guard existed.

_CLEAN = ScoringWeights(pass_rate_monotonicity=False, namespace_monotonicity={})


def test_gate_rejects_nonfinite_scalar_evidence() -> None:
    """Rule 1: a corrupt scalar cannot carry a worse challenger past the margin."""
    outcome = evaluate_gate({"scalar": 0.1}, {"scalar": math.nan}, _CLEAN)

    assert outcome.decision == "rejected"
    assert outcome.reason == "invalid evidence: challenger scalar must be finite"


def test_gate_rejects_a_nonfinite_mean_score() -> None:
    """Rule 2 (aggregate scope): a corrupt mean score cannot skip the pass-rate rule."""
    weights = ScoringWeights(
        pass_rate_monotonicity=True,
        pass_rate_monotonicity_scope="aggregate",
        namespace_monotonicity={},
    )
    outcome = evaluate_gate(
        {"scalar": 0.1, "mean_score": 1.0},
        {"scalar": 0.05, "mean_score": math.nan},
        weights,
    )

    assert outcome.decision == "rejected"
    assert outcome.reason == "invalid evidence: challenger mean_score must be finite"


def test_gate_flags_a_namespace_whose_aggregate_is_nonfinite() -> None:
    """Rule 3: an unreadable namespace is a regression, not a clean pass."""
    weights = ScoringWeights(
        pass_rate_monotonicity=False,
        namespace_weights={"drift:": 1.0},
        namespace_monotonicity={"drift:": True},
    )
    outcome = evaluate_gate(
        {"scalar": 0.1, "namespace_aggregates": {"drift:": 0.1}},
        {"scalar": 0.05, "namespace_aggregates": {"drift:": math.nan}},
        weights,
    )

    assert outcome.decision == "rejected"
    assert "drift:" in outcome.reason


def test_gate_rejects_a_nonfinite_holdout_scalar() -> None:
    """The holdout veto must not confirm every train win once its scalar is corrupt."""
    outcome = evaluate_gate(
        {"scalar": 0.1},
        {"scalar": 0.05},
        _CLEAN,
        holdout_parent_agg={"scalar": 0.1},
        holdout_child_agg={"scalar": math.nan},
    )

    assert outcome.decision == "rejected"
    assert outcome.reason == (
        "holdout_not_confirmed: invalid evidence: holdout challenger scalar must be finite"
    )


def test_gate_reports_attributable_regressions_on_the_nonfinite_path() -> None:
    """The #130 invariant: EVERY early return carries the same attributable set."""
    parent = {
        "scalar": 0.1,
        "per_entry": {"e1": {"pass_fail": True}, "e2": {"pass_fail": True}},
    }
    child = {
        "scalar": math.nan,
        "per_entry": {"e1": {"pass_fail": False}, "e2": {"pass_fail": True}},
    }

    outcome = evaluate_gate(parent, child, _CLEAN)

    assert outcome.decision == "rejected"
    assert outcome.attributable_regressions == ("e1",)
