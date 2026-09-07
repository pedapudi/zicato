"""Authored omission selects one recommendation; recorded contracts retain their meaning."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from zicato.core.scoring_config import ScoringWeights
from zicato.core.tournament import TournamentStructure
from zicato.epoch.contract import _canon_recorded_scoring, scoring_contract_to_canon
from zicato.selection.evidence_gate import read_promote_confidence_threshold, read_replicate_budget
from zicato.workspace.config_inspection import configuration_fields, configuration_scaffold
from zicato.workspace_loader import (
    historical_scoring_weights_from_dict,
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
def test_recorded_explicit_screening_retains_canonical_identity(screen_entries, tmp_path):
    record = json.loads(
        (Path(__file__).parent / "fixtures/recorded-gauntlet-scoring.json").read_text()
    )
    raw = record["scoring"]
    raw["proposer_quality"]["screen_entries"] = screen_entries
    epoch = tmp_path / "epochs" / "recorded"
    epoch.mkdir(parents=True)
    path = epoch / "scoring.json"
    path.write_text(json.dumps(raw))
    original = path.read_bytes()
    (tmp_path / "current_epoch").write_text("recorded\n")
    selected = load_current_scoring(tmp_path)
    assert selected.proposer_quality.screen_entries == screen_entries
    assert selected.tournament_structure.structure == "gauntlet"
    assert selected.tournament_structure.params == {}
    canonical = json.dumps(
        json.loads(_canon_recorded_scoring(path)), sort_keys=True, separators=(",", ":")
    )
    assert (
        hashlib.sha256(canonical.encode()).hexdigest()
        == record["canonical_sha256_by_screen_entries"][str(screen_entries)]
    )
    assert path.read_bytes() == original


def test_recorded_omission_preserves_gauntlet_and_disabled_screening():
    selected = historical_scoring_weights_from_dict({})
    assert selected.tournament_structure.structure == "gauntlet"
    assert selected.tournament_structure.params == {}
    assert selected.proposer_quality.screen_entries == 0
    nested = historical_scoring_weights_from_dict({"tournament": {}, "proposer_quality": {}})
    assert nested == selected


def test_omitted_confirmation_budget_uses_authored_default_and_historical_fallback(tmp_path):
    raw = json.loads(
        (Path(__file__).parent / "fixtures/recorded-gauntlet-scoring.json").read_text()
    )["scoring"]
    raw["tournament"]["params"] = {"promote_confidence_threshold": 0.8}
    recorded = historical_scoring_weights_from_dict(raw)
    assert recorded.tournament_structure.params == raw["tournament"]["params"]
    assert read_replicate_budget(recorded.tournament_structure.params) == 3
    path = tmp_path / "scoring.json"
    path.write_text(json.dumps(raw))
    original = path.read_bytes()
    canonical = json.dumps(
        json.loads(_canon_recorded_scoring(path)), sort_keys=True, separators=(",", ":")
    )
    assert hashlib.sha256(canonical.encode()).hexdigest() == (
        "523cdec3bb1c2b2d76fa78f4ee50bb0ab85874a49b07bff9c6ee9f14b934589a"
    )
    assert path.read_bytes() == original
    authored = scoring_weights_from_dict(recorded.to_json())
    assert read_replicate_budget(authored.tournament_structure.params) == 32
    expanded = authored.to_json()
    assert scoring_contract_to_canon(
        scoring_weights_from_dict(expanded)
    ) == scoring_contract_to_canon(authored)


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({}, {}),
        ({"promote_confidence_threshold": None}, {"promote_confidence_threshold": None}),
        ({"promote_confidence_threshold": 0}, {"promote_confidence_threshold": 0}),
        (
            {"promote_confidence_threshold": 0.8},
            {"promote_confidence_threshold": 0.8, "promote_confidence_replicates": 3},
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
    epoch = new_epoch(workspace, "typed-budget", board, brief, weights)
    selected = load_epoch_execution_contract(workspace, epoch.id, workspace_config={})
    assert selected.scoring == epoch.scoring
    assert selected.scoring.tournament_structure.params == expected
    assert scoring_weights_from_dict(selected.scoring.to_json()) == selected.scoring
    assert weights.tournament_structure.params == params
