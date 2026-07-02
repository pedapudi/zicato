"""Scaffolded contracts — ``zicato init`` + the builder's blank draft.

Both scaffolds write/open the SAME full effective recommended contract
(:func:`zicato.core.scoring_config.recommended_scaffold_weights`): racing
field 4 / eta 2 / board_fraction 0.4, two averaged replicates per duel,
and the evidence gate enabled EXPLICITLY (threshold 0.8, budget 32) — the
gate is opt-in in code, so the scaffold is where an operator sees and
prices it.
"""

from __future__ import annotations

import json
from pathlib import Path

from zicato.cli.init_cmd import initialize_workspace
from zicato.core.scoring_config import recommended_scaffold_weights
from zicato.selection.evidence_gate import (
    read_promote_confidence_threshold,
    read_replicate_budget,
)


def test_recommended_scaffold_weights_shape() -> None:
    w = recommended_scaffold_weights()
    t = w.tournament_structure
    assert t.structure == "racing"
    assert t.params["field_size"] == 4
    assert t.params["eta"] == 2
    assert t.params["board_fraction"] == 0.4
    assert t.params["replicates"] == 2
    # The evidence gate is enabled EXPLICITLY with an honest budget.
    assert read_promote_confidence_threshold(t.params) == 0.8
    assert read_replicate_budget(t.params) == 32
    # Noise-aware dataclass defaults ride along.
    assert w.proposer_quality.best_of_n == 3
    assert w.overfitting.min_board_size_for_split == 6
    # Candidate screening is enabled EXPLICITLY (2-entry rotating train
    # panel per slate candidate) — like the evidence gate, the in-code
    # default is OFF, so the scaffold is where an operator sees and
    # prices it.
    assert w.proposer_quality.screen_entries == 2
    assert w.proposer_quality.screen_veto_only is False


def test_init_writes_full_effective_scoring_scaffold(tmp_path: Path) -> None:
    workspace = tmp_path / ".zicato"
    initialize_workspace(workspace, instance_id="t")

    scaffold = tmp_path / "scoring.json"
    assert scaffold.exists()
    raw = json.loads(scaffold.read_text())
    # The FULL effective contract: every top-level field is spelled out by
    # the field-enumerating serializer, not just the overrides.
    for key in (
        "drift_weight",
        "pass_weight",
        "promote_margin",
        "tournament",
        "overfitting",
        "proposer_quality",
    ):
        assert key in raw, key
    params = raw["tournament"]["params"]
    assert raw["tournament"]["structure"] == "racing"
    assert params["field_size"] == 4
    assert params["replicates"] == 2
    assert params["promote_confidence_threshold"] == 0.8
    assert params["promote_confidence_replicates"] == 32
    assert raw["proposer_quality"]["best_of_n"] == 3
    assert raw["proposer_quality"]["screen_entries"] == 2
    assert raw["overfitting"]["min_board_size_for_split"] == 6

    # The generated file round-trips to exactly the recommended weights.
    from zicato.epoch.lifecycle import _scoring_from_dict

    assert _scoring_from_dict(raw) == recommended_scaffold_weights()


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
    from zicato.builder.draft import TournamentDraft

    workspace = tmp_path / ".zicato"
    workspace.mkdir()
    # No epoch, no scoring anywhere → the blank draft degrades to the
    # recommended scaffold contract, not the bare gauntlet.
    draft = TournamentDraft.from_workspace(workspace)
    assert draft.scoring == recommended_scaffold_weights()
    assert draft.scoring.tournament_structure.structure == "racing"
