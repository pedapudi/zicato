"""Omitted historical defaults retain identity without authorizing changed inputs."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from zicato.epoch.contract import ContractInputs
from zicato.epoch.execution import (
    ExecutionContractError,
    capture_execution_bindings,
    load_epoch_execution_contract,
)


@pytest.fixture
def recorded_epoch(tmp_path):
    workspace = tmp_path / ".zicato"
    directory = workspace / "epochs" / "recorded"
    directory.mkdir(parents=True)
    scoring = json.loads(
        (Path(__file__).parent / "fixtures/recorded-gauntlet-scoring.json").read_text()
    )["scoring"]
    (directory / "scoring.json").write_text(json.dumps(scoring))
    (directory / "board.jsonl").write_text(
        '{"id":"entry","kind":"single_turn","input":"Check the result",'
        '"wall_clock_budget_seconds":60}\n'
    )
    (directory / "brief.md").write_text("Improve the measured result.\n")
    inputs = ContractInputs(
        board_path=directory / "board.jsonl",
        brief_path=directory / "brief.md",
        scoring_path=directory / "scoring.json",
        entrypoint="",
        mutable_trees=(),
    )
    bindings, _ = capture_execution_bindings(inputs)
    (directory / "execution.json").write_bytes(bindings)
    # The retained fixture canonicalized under evaluator revision 2.
    (directory / "config.json").write_text(
        json.dumps(
            {
                "id": "recorded",
                "name": "recorded",
                "created_at": "2026-09-01T00:00:00Z",
                "board_path": "board.jsonl",
                "brief_path": "brief.md",
                "scoring": scoring,
                "contract_hash": "39ee4295201ed742c544938b59575e1a958ffee7aeff42c3e54414df79b5c4af",
                "implementation_identity": {"zicato_evaluator_revision": 2},
            }
        )
    )
    return workspace, directory, scoring


@pytest.mark.parametrize("omit", [("increment",), ("ceiling",), ("increment", "ceiling")])
def test_absent_recorded_defaults_preserve_selected_identity_and_bytes(recorded_epoch, omit):
    workspace, directory, scoring = recorded_epoch
    full = load_epoch_execution_contract(workspace, "recorded", workspace_config={})
    if "increment" in omit:
        assert scoring["overfitting"]["ladder"].pop("noise_scale") == 0.0
    if "ceiling" in omit:
        assert scoring["overfitting"].pop("max_generations_per_contract") is None
    (directory / "scoring.json").write_text(json.dumps(scoring))
    original = {path: path.read_bytes() for path in directory.iterdir()}

    sparse = load_epoch_execution_contract(workspace, "recorded", workspace_config={})

    assert sparse.contract_hash == full.contract_hash
    assert sparse.scoring == full.scoring
    assert {path: path.read_bytes() for path in directory.iterdir()} == original


@pytest.mark.parametrize(
    "changed", ["increment", "ceiling", "margin", "board", "brief", "revision"]
)
def test_recorded_omission_never_hides_changed_explicit_inputs(
    recorded_epoch, monkeypatch, changed
):
    workspace, directory, scoring = recorded_epoch
    scoring["overfitting"]["ladder"].pop("noise_scale")
    scoring["overfitting"].pop("max_generations_per_contract")
    if changed == "increment":
        scoring["overfitting"]["ladder"]["noise_scale"] = 0.01
    elif changed == "ceiling":
        scoring["overfitting"]["max_generations_per_contract"] = 9
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
        monkeypatch.setattr("zicato.epoch.contract.ZICATO_EVALUATOR_REVISION", 3)
    (directory / "scoring.json").write_text(json.dumps(scoring))
    original = {path: path.read_bytes() for path in directory.iterdir()}

    with pytest.raises(ExecutionContractError, match="recorded contract"):
        load_epoch_execution_contract(workspace, "recorded", workspace_config={})

    assert {path: path.read_bytes() for path in directory.iterdir()} == original
