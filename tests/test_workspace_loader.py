"""Tests for :mod:`zicato.workspace_loader`."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from zicato.core.types import ScoringWeights
from zicato.epoch.lifecycle import new_epoch
from zicato.workspace_loader import (
    load_current_board,
    load_current_epoch_config,
    load_current_rubric,
    load_current_scoring,
    load_workspace_config,
)


@pytest.fixture()
def fresh_workspace(tmp_path: Path) -> tuple[Path, str]:
    """Build a workspace with one epoch ready for loading."""
    workspace = tmp_path / ".zicato"
    workspace.mkdir(parents=True)
    (workspace / "config.json").write_text(
        json.dumps({"instance_id": "test", "created_at": "2026-05-14T00:00:00Z"})
    )

    board_src = tmp_path / "board.jsonl"
    board_src.write_text(
        json.dumps(
            {
                "id": "entry_a",
                "kind": "single_turn",
                "wall_clock_budget_seconds": 60,
                "input": "hello",
            }
        )
        + "\n"
    )
    rubric_src = tmp_path / "rubric.md"
    rubric_src.write_text("# Rubric\n- Be careful.\n")

    weights = ScoringWeights(drift_weight=1.5)
    cfg = new_epoch(
        workspace,
        name="alpha",
        board_source=board_src,
        rubric_source=rubric_src,
        weights=weights,
        auto_close_previous=False,
    )
    return workspace, cfg.id


def test_load_workspace_config_reads_json(fresh_workspace: tuple[Path, str]) -> None:
    workspace, _ = fresh_workspace
    cfg = load_workspace_config(workspace)
    assert cfg["instance_id"] == "test"


def test_load_workspace_config_missing_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="zicato init"):
        load_workspace_config(tmp_path / "missing")


def test_load_workspace_config_malformed_raises(tmp_path: Path) -> None:
    workspace = tmp_path / ".zicato"
    workspace.mkdir()
    (workspace / "config.json").write_text("not json{")
    with pytest.raises(ValueError, match="could not parse"):
        load_workspace_config(workspace)


def test_load_current_epoch_config_uses_marker(
    fresh_workspace: tuple[Path, str],
) -> None:
    workspace, epoch_id = fresh_workspace
    cfg = load_current_epoch_config(workspace)
    assert cfg.id == epoch_id
    assert cfg.scoring.drift_weight == 1.5


def test_load_current_epoch_missing_marker_raises(tmp_path: Path) -> None:
    workspace = tmp_path / ".zicato"
    workspace.mkdir()
    with pytest.raises(FileNotFoundError, match="zicato epoch new"):
        load_current_epoch_config(workspace)


def test_load_current_board_returns_entries(
    fresh_workspace: tuple[Path, str],
) -> None:
    workspace, _ = fresh_workspace
    board = load_current_board(workspace)
    assert len(board) == 1
    assert board[0].id == "entry_a"


def test_load_current_scoring_round_trips_weights(
    fresh_workspace: tuple[Path, str],
) -> None:
    workspace, _ = fresh_workspace
    weights = load_current_scoring(workspace)
    assert weights.drift_weight == 1.5


def test_load_current_rubric_parses_markdown(
    fresh_workspace: tuple[Path, str],
) -> None:
    workspace, _ = fresh_workspace
    rubric = load_current_rubric(workspace)
    assert "Be careful." in rubric.text
