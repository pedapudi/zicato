"""Experimental settings use one declared configuration location."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import fields

import pytest

from zicato.core.scoring_config import (
    ExperimentalConfig,
    ScoringWeights,
)
from zicato.workspace_loader import scoring_weights_from_dict


@pytest.mark.parametrize(
    "recorded, name, value",
    [
        ({"proposer_quality": {"process_exemplars": 2}}, "process_exemplars", 2),
        ({"proposer_quality": {"recombine": True}}, "recombine", True),
        ({"proposer_quality": {"recombine_merge": "llm"}}, "recombine_merge", "llm"),
        ({"proposer_quality": {"genealogy": 3}}, "genealogy", 3),
        ({"proposer_quality": {"calibration_feedback": 4}}, "calibration_feedback", 4),
        ({"overfitting": {"random_baseline_every_n": 2}}, "random_baseline_every_n", 2),
        ({"overfitting": {"max_generations_per_contract": 9}}, "max_generations_per_contract", 9),
        ({"diff_complexity_weight": 0.2}, "diff_complexity_weight", 0.2),
        ({"diff_complexity_ceiling": 10.0}, "diff_complexity_ceiling", 10.0),
        ({"experiment_memory": {"cross_epoch": True}}, "cross_epoch_memory", True),
        (
            {"tournament": {"params": {"rating": "bradley_terry"}}},
            "standing_rating",
            "bradley_terry",
        ),
        ({"tournament": {"params": {"resolver": "ranked_pairs"}}}, "resolver", "ranked_pairs"),
    ],
)
def test_experimental_setting_uses_its_declared_location(recorded, name, value):
    original = deepcopy(recorded)
    with pytest.raises(ValueError):
        scoring_weights_from_dict(recorded)
    authored = scoring_weights_from_dict(
        {
            "experimental": {name: value},
            "tournament": {"structure": "gauntlet", "params": {}},
            "proposer_quality": {"screen_entries": 0},
        }
    )
    assert recorded == original
    assert ScoringWeights.from_json(authored.to_json()) == authored
    assert getattr(authored.experimental, name) == value


def test_recommended_settings_keep_experiments_inactive_and_screening_enabled():
    recommended = ScoringWeights()
    assert recommended.experimental == ExperimentalConfig()
    assert recommended.proposer_quality.screen_entries > 0
    assert "experiment_memory" not in {field.name for field in fields(ScoringWeights)}
    assert recommended.overfitting.restrict_proposer_visibility is True
