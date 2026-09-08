"""Sparse, explicit, and saved scoring use one supported set of defaults."""

from __future__ import annotations

import json

import pytest

from zicato.core.scoring_config import ProposerQualityConfig, ScoringWeights
from zicato.core.tournament import TournamentStructure
from zicato.epoch.contract import scoring_contract_to_canon
from zicato.selection.evidence_gate import read_promote_confidence_threshold, read_replicate_budget
from zicato.workspace.config_inspection import configuration_fields, configuration_scaffold
from zicato.workspace_loader import (
    load_current_scoring,
    scoring_weights_from_dict,
)


@pytest.mark.parametrize("raw", [{}, {"tournament": {}}, {"tournament": {"structure": "racing"}}])
def test_authored_omission_uses_the_shared_recommendation(raw):
    expected = ScoringWeights()
    assert scoring_weights_from_dict(raw) == expected
    expanded = configuration_scaffold(complete=True)["scoring.json"]
    assert scoring_contract_to_canon(scoring_weights_from_dict(raw)) == scoring_contract_to_canon(
        scoring_weights_from_dict(expanded)
    )
    fields = configuration_fields()
    assert fields["scoring.tournament.structure"]["default"] == "racing"
    assert fields["scoring.tournament.params"]["default"] == dict(
        expected.tournament_structure.params
    )


@pytest.mark.parametrize(
    ("tournament", "structure"),
    [({"params": {}}, "racing"), ({"structure": "gauntlet"}, "gauntlet")],
)
def test_explicit_empty_parameters_and_gauntlet_disable_confirmation(tournament, structure):
    weights = scoring_weights_from_dict(
        {"tournament": tournament, "proposer_quality": {"screen_entries": 0}}
    )
    assert weights.tournament_structure.structure == structure
    assert weights.tournament_structure.params == {}
    assert read_promote_confidence_threshold(weights.tournament_structure.params) is None
    assert weights.proposer_quality.screen_entries == 0


@pytest.mark.parametrize("screen_entries", [0, 2])
def test_saved_explicit_screening_retains_its_value_and_identity(screen_entries, tmp_path):
    weights = ScoringWeights(
        tournament_structure=TournamentStructure.gauntlet(),
        proposer_quality=ProposerQualityConfig(screen_entries=screen_entries),
    )
    epoch = tmp_path / "epochs" / "supported"
    epoch.mkdir(parents=True)
    path = epoch / "scoring.json"
    path.write_text(json.dumps(weights.to_json()))
    original = path.read_bytes()
    (tmp_path / "current_epoch").write_text("supported\n")
    selected = load_current_scoring(tmp_path)
    assert selected.proposer_quality.screen_entries == screen_entries
    assert selected.tournament_structure.structure == "gauntlet"
    assert selected.tournament_structure.params == {}
    assert scoring_contract_to_canon(selected) == scoring_contract_to_canon(weights)
    assert path.read_bytes() == original


def test_omitted_confirmation_budget_is_resolved_before_serialization():
    weights = scoring_weights_from_dict(
        {"tournament": {"params": {"promote_confidence_threshold": 0.8}}}
    )
    assert read_replicate_budget(weights.tournament_structure.params) == 32
    expanded = weights.to_json()
    assert expanded["tournament"]["params"]["promote_confidence_replicates"] == 32
    assert ScoringWeights.from_json(expanded) == weights
    assert scoring_contract_to_canon(
        scoring_weights_from_dict(expanded)
    ) == scoring_contract_to_canon(weights)


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({}, {}),
        ({"promote_confidence_threshold": None}, {"promote_confidence_threshold": None}),
        ({"promote_confidence_threshold": 0}, {"promote_confidence_threshold": 0}),
        (
            {"promote_confidence_threshold": 0.8},
            {"promote_confidence_threshold": 0.8, "promote_confidence_replicates": 32},
        ),
        *[
            (
                {"promote_confidence_threshold": 0.8, "promote_confidence_replicates": budget},
                {"promote_confidence_threshold": 0.8, "promote_confidence_replicates": budget},
            )
            for budget in (0, 3, 32)
        ],
    ],
)
def test_typed_epoch_seals_the_callers_effective_confirmation_budget(tmp_path, params, expected):
    from zicato.epoch.execution import load_epoch_execution_contract
    from zicato.epoch.lifecycle import new_epoch

    board = tmp_path / "board.jsonl"
    board.write_text(
        '{"id":"entry","kind":"single_turn","input":"Check the result",'
        '"wall_clock_budget_seconds":60}\n'
    )
    brief = tmp_path / "brief.md"
    brief.write_text("Improve the measured result.\n")
    workspace = tmp_path / ".zicato"
    weights = ScoringWeights(tournament_structure=TournamentStructure("gauntlet", params))
    resolved_params = dict(weights.tournament_structure.params)
    epoch = new_epoch(workspace, "typed-budget", board, brief, weights)
    selected = load_epoch_execution_contract(workspace, epoch.id)
    assert selected.scoring == epoch.scoring
    assert selected.scoring.tournament_structure.params == expected
    assert scoring_weights_from_dict(selected.scoring.to_json()) == selected.scoring
    assert weights.tournament_structure.params == resolved_params
