"""Tests for draft init-from-workspace, diff_vs_live, and apply write path."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from zicato.builder import operations as ops
from zicato.builder.draft import DraftStore, TournamentDraft
from zicato.cli.common import write_workspace_config
from zicato.core.types import BoardEntry, ScoringWeights
from zicato.epoch.contract import compute_contract_hash, resolve_contract_inputs
from zicato.epoch.lifecycle import current_epoch_id, load_epoch, new_epoch


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    """A workspace with a registered contract + one open epoch.

    Mirrors the ``init → register → epoch new`` flow: the live contract
    source files sit next to the ``.zicato`` dir and are recorded under
    the workspace config's ``contract`` block, so ``apply`` and the
    contract resolver agree on where the live contract lives.
    """
    ws = tmp_path / ".zicato"
    ws.mkdir()

    board = tmp_path / "board.jsonl"
    board.write_text(
        '{"id": "e1", "kind": "single_turn", "budget_s": 60, "input": "hi"}\n'
        '{"id": "e2", "kind": "single_turn", "budget_s": 60, "input": "bye"}\n',
        encoding="utf-8",
    )
    brief = tmp_path / "brief.md"
    brief.write_text("# Brief\n\nsteer toward concrete deltas\n", encoding="utf-8")
    scoring = tmp_path / "scoring.json"
    scoring.write_text(json.dumps({"drift_weight": 1.0, "promote_margin": 0.01}), encoding="utf-8")

    # Workspace config with the contract block + harness identity.
    write_workspace_config(
        ws,
        {
            "instance_id": "default",
            "adk_entrypoint": "pkg.mod:agent",
            "mutable_trees": [str(tmp_path / "src")],
            "source_roots": [str(tmp_path / "src")],
            "contract": {
                "board_path": str(board.resolve()),
                "rubric_path": str(brief.resolve()),
                "scoring_path": str(scoring.resolve()),
            },
        },
    )

    new_epoch(
        workspace_root=ws,
        name="alpha",
        board_source=board,
        brief_source=brief,
        weights=ScoringWeights(),
        entrypoint="pkg.mod:agent",
        mutable_trees=(str(tmp_path / "src"),),
    )
    return ws


def test_from_workspace_prefills_from_live_contract(workspace: Path) -> None:
    draft = TournamentDraft.from_workspace(workspace)
    assert {e.id for e in draft.entries} == {"e1", "e2"}
    assert "concrete deltas" in draft.brief
    assert draft.proposer_path is None
    assert isinstance(draft.scoring, ScoringWeights)


def test_to_dict_is_json_serializable(workspace: Path) -> None:
    draft = TournamentDraft.from_workspace(workspace)
    snapshot = draft.to_dict()
    json.dumps(snapshot)  # must not raise
    assert "scoring" in snapshot
    assert "board" in snapshot
    assert "holdout" in snapshot


def test_diff_vs_live_clean_when_unchanged(workspace: Path) -> None:
    draft = TournamentDraft.from_workspace(workspace)
    diff = draft.diff_vs_live(workspace)
    assert diff.rolls_epoch is False
    assert diff.to_dict()["changed_components"] == []


def test_diff_vs_live_flags_structure_and_scoring(workspace: Path) -> None:
    draft = TournamentDraft.from_workspace(workspace)
    ops.set_structure(draft, "swiss")
    diff = draft.diff_vs_live(workspace)
    changed = set(diff.to_dict()["changed_components"])
    assert "structure" in changed
    assert "scoring" in changed  # structure rides inside scoring
    assert diff.rolls_epoch is True


def test_diff_vs_live_flags_board_and_brief(workspace: Path) -> None:
    draft = TournamentDraft.from_workspace(workspace)
    ops.set_brief(draft, "completely different brief prose")
    ops.edit_board_entry(
        draft,
        BoardEntry(id="e3", kind="single_turn", wall_clock_budget_seconds=60, input="new"),
    )
    changed = set(draft.diff_vs_live(workspace).to_dict()["changed_components"])
    assert {"board", "brief"} <= changed


def test_apply_dry_run_writes_nothing(workspace: Path) -> None:
    board_path = workspace.parent / "board.jsonl"
    before = board_path.read_text(encoding="utf-8")

    draft = TournamentDraft.from_workspace(workspace)
    ops.set_structure(draft, "swiss")
    ops.set_brief(draft, "dry run brief")
    result = ops.apply(draft, workspace, confirm=False)

    assert result.confirmed is False
    assert result.rolled is False
    assert result.new_contract_hash  # predicted hash is present
    # Nothing on disk changed.
    assert board_path.read_text(encoding="utf-8") == before
    epoch_before = current_epoch_id(workspace)
    # The epoch did not roll (still the same current epoch).
    assert current_epoch_id(workspace) == epoch_before


def test_apply_confirm_writes_contract_and_rolls_on_next_resolve(workspace: Path) -> None:
    scoring_path = workspace.parent / "scoring.json"
    epoch_id = current_epoch_id(workspace)
    assert epoch_id is not None
    stored_hash = load_epoch(workspace, epoch_id).contract_hash

    draft = TournamentDraft.from_workspace(workspace)
    ops.set_structure(draft, "swiss")
    ops.set_param(draft, "field_size", 4)
    result = ops.apply(draft, workspace, confirm=True)

    assert result.confirmed is True
    assert result.rolled is True
    assert "structure" in result.components_changed

    # The live scoring.json now carries the swiss structure.
    live_scoring = json.loads(scoring_path.read_text(encoding="utf-8"))
    assert live_scoring["tournament"]["structure"] == "swiss"

    # The re-derived live contract hash differs from the epoch's stored
    # hash — proof the auto-epoch machinery WILL roll on the next resolve.
    new_hash = compute_contract_hash(resolve_contract_inputs(workspace))
    assert new_hash == result.new_contract_hash
    assert new_hash != stored_hash


def test_apply_confirm_no_change_does_not_roll(workspace: Path) -> None:
    draft = TournamentDraft.from_workspace(workspace)
    result = ops.apply(draft, workspace, confirm=True)
    assert result.confirmed is True
    assert result.rolled is False
    assert result.components_changed == ()


def test_draft_store_isolates_sessions(workspace: Path) -> None:
    store = DraftStore()
    a = store.get("session-a", workspace)
    b = store.get("session-b", workspace)
    ops.set_structure(a, "racing")
    # b is a distinct draft, still on the live (gauntlet) structure.
    assert a.scoring.tournament_structure.structure == "racing"
    assert b.scoring.tournament_structure.structure == "gauntlet"
    # Re-fetching session-a returns the SAME mutated draft.
    assert store.get("session-a", workspace) is a
