"""Retiring the fixed release increment must preserve recorded decisions."""

from __future__ import annotations

from copy import deepcopy

import pytest

from zicato.core.configuration import ConfigurationError, dataclass_schema
from zicato.core.scoring_config import ScoringWeights
from zicato.epoch.contract import scoring_to_canon
from zicato.tournament.ladder import LadderState, effective_threshold, query_holdout
from zicato.workspace_loader import historical_scoring_weights_from_dict, scoring_weights_from_dict


@pytest.mark.parametrize("increment", [0.0, 0.05])
def test_authored_ladder_increment_requires_explicit_migration(increment: float) -> None:
    raw = {"promote_margin": 0.1, "overfitting": {"ladder": {"noise_scale": increment}}}
    with pytest.raises(ConfigurationError, match="threshold") as caught:
        scoring_weights_from_dict(raw)
    assert caught.value.path == "scoring.overfitting.ladder.noise_scale"
    schema = dataclass_schema(ScoringWeights)
    ladder = schema["properties"]["overfitting"]["properties"]["ladder"]
    assert "noise_scale" not in ladder["properties"]


@pytest.mark.parametrize("threshold, expected", [(None, 0.15), (0.2, 0.25)])
def test_recorded_increment_executes_the_recorded_release_rule(threshold, expected) -> None:
    raw = {
        "promote_margin": 0.1,
        "overfitting": {"ladder": {"threshold": threshold, "noise_scale": 0.05, "budget": 2}},
    }
    original = deepcopy(raw)
    weights = historical_scoring_weights_from_dict(raw)
    cfg = weights.overfitting.ladder
    assert effective_threshold(cfg, weights) == pytest.approx(expected)
    assert "noise_scale" not in weights.to_json()["overfitting"]["ladder"]
    assert cfg.threshold == pytest.approx(expected)
    state = LadderState.seed(cfg)
    # The first improvement stays below either historical release bar.
    withheld = query_holdout(
        state,
        cfg=cfg,
        weights=weights,
        train_parent_scalar=1.0,
        train_child_scalar=0.95,
        holdout_scalar=0.9,
        holdout_confirmed=False,
    )
    assert withheld.released is False
    assert withheld.confirmed is None
    assert withheld.state.budget_remaining == 1
    # The second improvement clears either bar; its adverse confirmation survives.
    released = query_holdout(
        withheld.state,
        cfg=cfg,
        weights=weights,
        train_parent_scalar=1.0,
        train_child_scalar=0.5,
        holdout_scalar=0.7,
        holdout_confirmed=False,
    )
    assert released.released is True
    assert released.confirmed is False
    assert released.state.budget_remaining == 0
    assert raw == original


def test_supported_sparse_and_expanded_ladder_settings_keep_one_identity() -> None:
    weights = scoring_weights_from_dict({"overfitting": {"ladder": {"threshold": 0.15}}})
    expanded = weights.to_json()
    assert "noise_scale" not in expanded["overfitting"]["ladder"]
    assert scoring_to_canon(scoring_weights_from_dict(expanded)) == scoring_to_canon(weights)
    assert ScoringWeights.from_json(expanded) == weights


@pytest.mark.parametrize(
    "fixture, recorded_hash",
    [
        (
            "recorded_ladder_scoring.json",
            "2ef491225d99e0a1147f369de1ed41401ca4901beea2b455223d2deae2bd3ffd",
        ),
        (
            "recorded_experimental_scoring.json",
            "f58eff7e8f38b85b0a64036540c9f6ee4bb194264bc0bcfb8f72b85e2791ce6d",
        ),
    ],
)
def test_retained_increment_keeps_recorded_identity_and_bytes(
    tmp_path, monkeypatch, fixture, recorded_hash
) -> None:
    import json
    from pathlib import Path

    from zicato.epoch import contract
    from zicato.epoch.contract import ContractInputs, compute_recorded_contract_hash
    from zicato.epoch.execution import (
        ExecutionContractError,
        capture_execution_bindings,
        load_epoch_execution_contract,
    )
    from zicato.epoch.lifecycle import load_epoch

    workspace = tmp_path / ".zicato"
    directory = workspace / "epochs" / "recorded"
    directory.mkdir(parents=True)
    scoring_bytes = (Path(__file__).parent / "data" / fixture).read_bytes()
    (directory / "scoring.json").write_bytes(scoring_bytes)
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
    # Captured from the published schema containing the enabled additive field.
    (directory / "config.json").write_text(
        json.dumps(
            {
                "id": "recorded",
                "name": "recorded",
                "created_at": "2026-09-01T00:00:00Z",
                "board_path": "board.jsonl",
                "brief_path": "brief.md",
                "scoring": json.loads(scoring_bytes),
                "contract_hash": recorded_hash,
                "implementation_identity": {"zicato_evaluator_revision": 1},
            }
        )
    )
    original = {path: path.read_bytes() for path in directory.iterdir()}
    active_revision = contract.ZICATO_EVALUATOR_REVISION
    assert active_revision > 1
    with monkeypatch.context() as historical:
        historical.setattr(contract, "ZICATO_EVALUATOR_REVISION", 1)
        assert compute_recorded_contract_hash(inputs) == recorded_hash
    assert contract.ZICATO_EVALUATOR_REVISION == active_revision
    selected = load_epoch(workspace, "recorded")
    assert selected.contract_hash == recorded_hash
    assert selected.scoring.overfitting.ladder.threshold == pytest.approx(0.15)
    assert ScoringWeights.from_json(json.loads(scoring_bytes)) == selected.scoring
    assert ScoringWeights.from_json(selected.scoring.to_json()) == selected.scoring
    with pytest.raises(ExecutionContractError, match="recorded contract"):
        load_epoch_execution_contract(workspace, "recorded", workspace_config={})
    assert {path: path.read_bytes() for path in directory.iterdir()} == original


def test_partial_record_cannot_guess_the_archived_release_margin() -> None:
    from zicato.workspace_loader import ladder_config_from_dict, overfitting_config_from_dict

    for decode, raw in (
        (ladder_config_from_dict, {"noise_scale": 0.05}),
        (overfitting_config_from_dict, {"ladder": {"noise_scale": 0.05}}),
    ):
        with pytest.raises(ValueError, match="complete scoring record"):
            decode(raw)
