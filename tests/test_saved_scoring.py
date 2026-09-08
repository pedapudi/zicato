"""Supported defaults retain identity without authorizing changed saved inputs."""

from __future__ import annotations

import json

import pytest

from zicato.core.scoring_config import ProposerQualityConfig, ScoringWeights
from zicato.core.tournament import TournamentStructure
from zicato.epoch.execution import ExecutionContractError, load_epoch_execution_contract
from zicato.epoch.lifecycle import new_epoch


@pytest.fixture
def saved_epoch(tmp_path):
    workspace = tmp_path / ".zicato"
    board = tmp_path / "board.jsonl"
    board.write_text(
        '{"id":"entry","kind":"single_turn","input":"Check the result",'
        '"wall_clock_budget_seconds":60}\n'
    )
    brief = tmp_path / "brief.md"
    brief.write_text("Improve the measured result.\n")
    weights = ScoringWeights(
        tournament_structure=TournamentStructure.gauntlet(),
        proposer_quality=ProposerQualityConfig(screen_entries=0),
    )
    epoch = new_epoch(workspace, "supported", board, brief, weights)
    directory = workspace / "epochs" / epoch.id
    return workspace, epoch.id, directory, json.loads((directory / "scoring.json").read_text())


@pytest.mark.parametrize("omit", [("threshold",), ("ceiling",), ("threshold", "ceiling")])
def test_omitted_supported_defaults_keep_selected_identity_and_bytes(saved_epoch, omit):
    workspace, epoch_id, directory, scoring = saved_epoch
    full = load_epoch_execution_contract(workspace, epoch_id)
    if "threshold" in omit:
        assert scoring["overfitting"]["ladder"].pop("threshold") is None
    if "ceiling" in omit:
        assert scoring["experimental"].pop("max_generations_per_contract") is None
    (directory / "scoring.json").write_text(json.dumps(scoring))
    original = {path: path.read_bytes() for path in directory.iterdir() if path.is_file()}

    sparse = load_epoch_execution_contract(workspace, epoch_id)

    assert sparse.contract_hash == full.contract_hash
    assert sparse.scoring == full.scoring
    assert {path: path.read_bytes() for path in original} == original


@pytest.mark.parametrize(
    "changed", ["threshold", "ceiling", "margin", "board", "brief", "revision"]
)
def test_saved_defaults_never_hide_changed_explicit_inputs(saved_epoch, monkeypatch, changed):
    from zicato.epoch import contract

    workspace, epoch_id, directory, scoring = saved_epoch
    scoring["overfitting"]["ladder"].pop("threshold")
    scoring["experimental"].pop("max_generations_per_contract")
    if changed == "threshold":
        scoring["overfitting"]["ladder"]["threshold"] = 0.01
    elif changed == "ceiling":
        scoring["experimental"]["max_generations_per_contract"] = 9
    elif changed == "margin":
        scoring["promote_margin"] = 0.2
    elif changed == "board":
        board_path = directory / "board.jsonl"
        board = json.loads(board_path.read_text())
        board["input"] = "Check another result"
        board_path.write_text(json.dumps(board) + "\n")
    elif changed == "brief":
        (directory / "brief.md").write_text("Prefer an unrelated outcome.\n")
    else:
        monkeypatch.setattr(
            contract, "ZICATO_EVALUATOR_REVISION", contract.ZICATO_EVALUATOR_REVISION + 1
        )
    (directory / "scoring.json").write_text(json.dumps(scoring))
    original = {path: path.read_bytes() for path in directory.iterdir() if path.is_file()}

    with pytest.raises(ExecutionContractError, match="recorded contract"):
        load_epoch_execution_contract(workspace, epoch_id)

    assert {path: path.read_bytes() for path in original} == original
