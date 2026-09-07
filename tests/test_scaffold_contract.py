"""Initialization and blank drafts resolve the shared scoring defaults."""

from __future__ import annotations

import json
from pathlib import Path

from zicato.cli.init_cmd import initialize_workspace
from zicato.core.scoring_config import ScoringWeights
from zicato.selection.evidence_gate import (
    read_promote_confidence_threshold,
    read_replicate_budget,
)


def test_shared_scoring_defaults() -> None:
    w = ScoringWeights()
    t = w.tournament_structure
    assert t.structure == "racing"
    assert t.params["field_size"] == 4
    assert t.params["eta"] == 2
    assert t.params["board_fraction"] == 0.4
    # The shuffled slice schedule is opt-in EVERYWHERE, including new
    # workspaces: the scaffold must not name it (see TOURNAMENT-STRUCTURES.md).
    assert "slice_schedule" not in t.params
    assert t.params["replicates"] == 2
    # The evidence gate is enabled EXPLICITLY with an honest budget.
    assert read_promote_confidence_threshold(t.params) == 0.8
    assert read_replicate_budget(t.params) == 32
    # Noise-aware dataclass defaults ride along.
    assert w.proposer_quality.best_of_n == 3
    assert w.overfitting.min_board_size_for_split == 6
    assert w.proposer_quality.screen_entries == 2
    assert w.proposer_quality.screen_veto_only is False


def test_init_writes_sparse_scoring_with_the_shared_effective_contract(tmp_path: Path) -> None:
    from zicato.workspace_loader import scoring_weights_from_dict

    workspace = tmp_path / ".zicato"
    initialize_workspace(workspace, instance_id="t")
    raw = json.loads((tmp_path / "scoring.json").read_text())
    assert raw == {}
    assert scoring_weights_from_dict(raw) == ScoringWeights()


def test_init_never_clobbers_an_existing_scoring_json(tmp_path: Path) -> None:
    existing = tmp_path / "scoring.json"
    existing.write_text('{"promote_margin": 0.5}\n')
    workspace = tmp_path / ".zicato"
    initialize_workspace(workspace, instance_id="t")
    assert existing.read_text() == '{"promote_margin": 0.5}\n'
    # Not even with force — the live contract source is the operator's.
    initialize_workspace(workspace, instance_id="t", force=True)
    assert existing.read_text() == '{"promote_margin": 0.5}\n'


def test_builder_blank_draft_opens_on_the_recommended_contract(tmp_path: Path) -> None:
    from zicato.contract_draft.draft import TournamentDraft

    workspace = tmp_path / ".zicato"
    workspace.mkdir()
    draft = TournamentDraft.from_workspace(workspace)
    assert draft.scoring == ScoringWeights()
    assert draft.scoring.tournament_structure.structure == "racing"
