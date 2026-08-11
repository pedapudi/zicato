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


def test_gate_rejects_nonfinite_scalar_evidence() -> None:
    """A corrupt scalar cannot make a worse challenger bypass Rule 1."""
    outcome = evaluate_gate(
        {"scalar": 1.0},
        {"scalar": math.nan},
        ScoringWeights(pass_rate_monotonicity=False, namespace_monotonicity={}),
    )

    assert outcome.decision == "rejected"
    assert outcome.reason == (
        "invalid scalar evidence: champion and challenger scalars must both be finite"
    )
