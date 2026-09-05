"""Tests for draft init-from-workspace, diff_vs_live, and apply write path."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from zicato.contract_draft import operations as ops
from zicato.contract_draft.draft import DraftStore, TournamentDraft
from zicato.core.types import BoardEntry, ScoringWeights
from zicato.epoch.contract import compute_contract_hash, resolve_contract_inputs
from zicato.epoch.lifecycle import current_epoch_id, load_epoch, new_epoch
from zicato.workspace.config_io import write_workspace_config


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
    scoring.write_text(json.dumps({"promote_margin": 0.01}), encoding="utf-8")

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
    ops.set_structure(draft, "racing")
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
    ops.set_structure(draft, "racing")
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
    ops.set_structure(draft, "racing")
    ops.set_param(draft, "field_size", 4)
    result = ops.apply(draft, workspace, confirm=True)

    assert result.confirmed is True
    assert result.rolled is True
    assert "structure" in result.components_changed

    # The live scoring.json now carries the swiss structure.
    live_scoring = json.loads(scoring_path.read_text(encoding="utf-8"))
    assert live_scoring["tournament"]["structure"] == "racing"

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


# ---------------------------------------------------------------------------
# board_meta round-trip (the B0 bug fix): a builder apply on a workspace whose
# board carries a board_meta header must never strip disable_drift/judge_only
# from the live contract.
# ---------------------------------------------------------------------------


@pytest.fixture()
def meta_workspace(tmp_path: Path) -> Path:
    """A workspace whose live board carries a non-default board_meta header."""
    ws = tmp_path / ".zicato"
    ws.mkdir()

    board = tmp_path / "board.jsonl"
    board.write_text(
        '{"board_meta": true, "disable_drift": ["off_topic"], "judge_only": true}\n'
        '{"id": "e1", "kind": "single_turn", "budget_s": 60, "input": "hi"}\n'
        '{"id": "e2", "kind": "single_turn", "budget_s": 60, "input": "bye"}\n',
        encoding="utf-8",
    )
    brief = tmp_path / "brief.md"
    brief.write_text("# Brief\n\nsteer\n", encoding="utf-8")
    scoring = tmp_path / "scoring.json"
    scoring.write_text(json.dumps({"promote_margin": 0.01}), encoding="utf-8")

    write_workspace_config(
        ws,
        {
            "instance_id": "default",
            "adk_entrypoint": "pkg.mod:agent",
            "mutable_trees": [],
            "source_roots": [],
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
    )
    return ws


def _first_line(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8").splitlines()[0])


def test_from_workspace_carries_board_meta(meta_workspace: Path) -> None:
    draft = TournamentDraft.from_workspace(meta_workspace)
    assert [str(k) for k in draft.disable_drift] == ["off_topic"]
    assert draft.judge_only is True
    meta = draft.to_dict()["board_meta"]
    assert meta == {"disable_drift": ["off_topic"], "judge_only": True}


def test_board_meta_round_trips_through_any_op_and_apply(meta_workspace: Path) -> None:
    """THE B0 regression: an unrelated op + apply(confirm=True) must preserve
    the board_meta header in the written live board. Before the fix the draft
    loaded via load_current_board (header dropped) and _write_contract saved
    with defaults — silently stripping disable_drift/judge_only."""
    board_path = meta_workspace.parent / "board.jsonl"

    draft = TournamentDraft.from_workspace(meta_workspace)
    ops.set_structure(draft, "racing")  # any op — unrelated to the header
    result = ops.apply(draft, meta_workspace, confirm=True)

    assert result.confirmed is True
    header = _first_line(board_path)
    assert header.get("board_meta") is True
    assert header.get("disable_drift") == ["off_topic"]
    assert header.get("judge_only") is True


def test_meta_board_unchanged_draft_diff_is_clean(meta_workspace: Path) -> None:
    """The canon agrees with the on-disk bytes: an untouched meta-carrying
    draft reports no phantom board change, and applying it does not roll."""
    draft = TournamentDraft.from_workspace(meta_workspace)
    diff = draft.diff_vs_live(meta_workspace)
    assert diff.rolls_epoch is False
    result = ops.apply(draft, meta_workspace, confirm=True)
    assert result.rolled is False


def test_dry_run_hash_equals_confirm_hash_for_meta_board(meta_workspace: Path) -> None:
    """The dry-run's predicted contract hash (temp-dir materialization) must
    equal the confirmed apply's hash for a meta-carrying board — both writers
    thread disable_drift/judge_only through save_board."""
    draft = TournamentDraft.from_workspace(meta_workspace)
    ops.set_structure(draft, "racing")
    predicted = ops.apply(draft, meta_workspace, confirm=False).new_contract_hash
    confirmed = ops.apply(draft, meta_workspace, confirm=True).new_contract_hash
    assert predicted == confirmed


def test_board_meta_change_rolls_epoch_via_file_hash(workspace: Path) -> None:
    """Setting the header on a previously header-free board is a board change:
    the diff flags it, apply reports rolled, and the re-resolved contract hash
    moves off the epoch's stored hash."""
    epoch_id = current_epoch_id(workspace)
    assert epoch_id is not None
    stored_hash = load_epoch(workspace, epoch_id).contract_hash

    draft = TournamentDraft.from_workspace(workspace)
    patch = ops.set_board_meta(draft, disable_drift=["off_topic"], judge_only=True)
    assert patch.changed["disable_drift"]["to"] == ["off_topic"]

    diff = draft.diff_vs_live(workspace)
    assert "board" in diff.to_dict()["changed_components"]

    result = ops.apply(draft, workspace, confirm=True)
    assert result.rolled is True
    new_hash = compute_contract_hash(resolve_contract_inputs(workspace))
    assert new_hash == result.new_contract_hash
    assert new_hash != stored_hash
    header = _first_line(workspace.parent / "board.jsonl")
    assert header.get("board_meta") is True


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
