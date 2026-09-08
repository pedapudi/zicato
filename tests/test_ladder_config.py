"""Unsupported release settings refuse; supported settings round-trip exactly."""

from __future__ import annotations

import pytest

from zicato.core.configuration import ConfigurationError, dataclass_schema
from zicato.core.scoring_config import ScoringWeights
from zicato.epoch.contract import scoring_to_canon
from zicato.workspace_loader import scoring_weights_from_dict


@pytest.mark.parametrize("increment", [0.0, 0.05])
def test_unsupported_ladder_increment_is_refused(increment: float) -> None:
    raw = {"promote_margin": 0.1, "overfitting": {"ladder": {"noise_scale": increment}}}
    with pytest.raises(ConfigurationError) as caught:
        scoring_weights_from_dict(raw)
    assert caught.value.path == "scoring.overfitting.ladder.noise_scale"
    assert caught.value.category == "unknown"
    schema = dataclass_schema(ScoringWeights)
    ladder = schema["properties"]["overfitting"]["properties"]["ladder"]
    assert "noise_scale" not in ladder["properties"]


def test_supported_sparse_and_expanded_ladder_settings_keep_one_identity() -> None:
    weights = scoring_weights_from_dict({"overfitting": {"ladder": {"threshold": 0.15}}})
    expanded = weights.to_json()
    assert "noise_scale" not in expanded["overfitting"]["ladder"]
    assert scoring_to_canon(scoring_weights_from_dict(expanded)) == scoring_to_canon(weights)
    assert ScoringWeights.from_json(expanded) == weights
