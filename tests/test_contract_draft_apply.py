"""Tests for draft init-from-workspace, diff_vs_live, and apply write path."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from zicato.contract_draft import operations as ops
from zicato.contract_draft.draft import TournamentDraft
from zicato.core.scoring_config import scoring_weights_from_dict
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
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "agent.py").write_text("VALUE = 1\n")

    board = tmp_path / "board.jsonl"
    board.write_text(
        '{"id": "e1", "kind": "single_turn", "wall_clock_budget_seconds": 60, "input": "hi"}\n'
        '{"id": "e2", "kind": "single_turn", "wall_clock_budget_seconds": 60, "input": "bye"}\n',
        encoding="utf-8",
    )
    brief = tmp_path / "brief.md"
    brief.write_text("# Brief\n\nsteer toward concrete deltas\n", encoding="utf-8")
    scoring = tmp_path / "scoring.json"
    initial_scoring = {
        "promote_margin": 0.01,
        "tournament": {"structure": "gauntlet"},
        "proposer_quality": {"screen_entries": 0},
    }
    scoring.write_text(json.dumps(initial_scoring), encoding="utf-8")

    # Workspace config with the contract block + harness identity.
    write_workspace_config(
        ws,
        {
            "instance_id": "default",
            "generation_source_backend": "directory",
            "adapter": {
                "kind": "adk",
                "entrypoint": "pkg.mod:agent",
                "mutable_trees": [str(tmp_path / "src")],
            },
            "contract": {
                "board_path": str(board.resolve()),
                "brief_path": str(brief.resolve()),
                "scoring_path": str(scoring.resolve()),
            },
        },
    )

    new_epoch(
        workspace_root=ws,
        name="alpha",
        board_source=board,
        brief_source=brief,
        weights=scoring_weights_from_dict(initial_scoring),
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


def test_unrelated_draft_edit_preserves_pending_live_contract_changes(workspace: Path) -> None:
    board = workspace.parent / "board.jsonl"
    brief = workspace.parent / "brief.md"
    scoring = workspace.parent / "scoring.json"
    board.write_text(
        '{"board_meta": true, "disable_drift": ["off_topic"], "judge_only": true}\n'
        + board.read_text()
        + '{"id":"pending-task","kind":"single_turn","wall_clock_budget_seconds":60,'
        '"input":"pending task"}\n'
    )
    brief.write_text("# Pending operator brief\n")
    scoring.write_text(json.dumps({"promote_margin": 0.73, "task_failure_weight": 2.0}))

    draft = TournamentDraft.from_workspace(workspace)
    assert {entry.id for entry in draft.entries} == {"e1", "e2", "pending-task"}
    assert draft.brief == brief.read_text()
    assert draft.scoring.promote_margin == 0.73
    assert draft.judge_only is True
    assert [str(kind) for kind in draft.disable_drift] == ["off_topic"]
    assert draft.diff_vs_live(workspace).to_dict()["changed_components"] == []
    ops.set_weights(draft, pass_weight=3.0)
    ops.apply(draft, workspace, confirm=True)

    saved = TournamentDraft.from_workspace(workspace)
    assert {entry.id for entry in saved.entries} == {"e1", "e2", "pending-task"}
    assert saved.brief == "# Pending operator brief\n"
    assert saved.scoring.promote_margin == 0.73
    assert saved.scoring.task_failure_weight == 2.0
    assert saved.scoring.pass_weight == 3.0
    assert saved.judge_only is True


def test_live_contract_draft_loads_before_an_epoch_exists(tmp_path: Path) -> None:
    workspace = tmp_path / ".zicato"
    workspace.mkdir()
    (tmp_path / "board.jsonl").write_text(
        '{"id":"pending-task","kind":"single_turn","wall_clock_budget_seconds":60,"input":"task"}\n'
    )
    (tmp_path / "brief.md").write_text("# First contract\n")
    (tmp_path / "scoring.json").write_text('{"promote_margin":0.73}')
    write_workspace_config(workspace, {})

    draft = TournamentDraft.from_workspace(workspace)
    assert [entry.id for entry in draft.entries] == ["pending-task"]
    assert draft.brief == "# First contract\n"
    assert draft.scoring.promote_margin == 0.73
    assert current_epoch_id(workspace) is None


def test_stale_editing_session_cannot_overwrite_another_apply(workspace: Path) -> None:
    first = TournamentDraft.from_workspace(workspace)
    second = TournamentDraft.from_workspace(workspace)
    ops.set_weights(first, pass_weight=3.0)
    ops.set_brief(second, "stale session brief")
    ops.apply(first, workspace, confirm=True)
    paths = [
        workspace / "config.json",
        *(workspace.parent / name for name in ("board.jsonl", "brief.md", "scoring.json")),
    ]
    before = {path: path.read_bytes() for path in paths}

    with pytest.raises(ValueError, match="scoring"):
        ops.apply(second, workspace, confirm=True)
    assert {path: path.read_bytes() for path in paths} == before


def test_apply_records_the_default_brief_path(workspace: Path) -> None:
    config = json.loads((workspace / "config.json").read_text())
    del config["contract"]["brief_path"]
    write_workspace_config(workspace, config)
    draft = TournamentDraft.from_workspace(workspace)
    ops.set_brief(draft, "# Changed brief\n")
    ops.apply(draft, workspace, confirm=True)
    published = json.loads((workspace / "config.json").read_text())
    assert published["contract"]["brief_path"] == str(workspace.parent / "brief.md")
    assert (workspace.parent / "brief.md").read_text() == "# Changed brief\n"


def test_competing_apply_calls_preserve_the_successful_edit(workspace: Path) -> None:
    from zicato.runtime.lock import WorkspaceLockHeld

    drafts = [TournamentDraft.from_workspace(workspace) for _ in range(2)]
    for index, draft in enumerate(drafts):
        ops.set_brief(draft, f"editing session {index}")
    rendezvous = Barrier(2)

    def apply_session(index: int) -> int | Exception:
        rendezvous.wait(timeout=5)
        try:
            ops.apply(drafts[index], workspace, confirm=True)
            return index
        except (ValueError, WorkspaceLockHeld) as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(apply_session, (0, 1)))
    winners = [result for result in results if isinstance(result, int)]
    assert len(winners) == 1
    assert (workspace.parent / "brief.md").read_text() == f"editing session {winners[0]}"


@pytest.mark.parametrize("component", ["board", "brief", "scoring", "config"])
def test_apply_reports_intervening_manual_edits(workspace: Path, component: str) -> None:
    draft = TournamentDraft.from_workspace(workspace)
    assert draft.source is not None
    target = draft.source.file(component).path
    # A byte edit is a conflict even when JSON whitespace preserves its meaning.
    target.write_text(target.read_text() + "\n")
    before = {file.path: file.path.read_bytes() for file in draft.source.files}
    with pytest.raises(ValueError, match=component):
        ops.apply(draft, workspace, confirm=True)
    assert {path: path.read_bytes() for path in before} == before


def test_apply_detects_a_changed_proposer_skill(workspace: Path) -> None:
    proposer = workspace.parent / "proposal-rules"
    skill = proposer / "skills" / "edit.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: edit\ndescription: Edit instructions\n---\nChange one mutation.\n")
    config = json.loads((workspace / "config.json").read_text())
    config["contract"]["proposer_path"] = str(proposer)
    write_workspace_config(workspace, config)
    draft = TournamentDraft.from_workspace(workspace)
    skill.write_text(skill.read_text() + "Preserve unrelated text.\n")
    with pytest.raises(ValueError, match="proposer"):
        ops.apply(draft, workspace, confirm=True)


@pytest.mark.parametrize("replacement", [1, 2, 3, 4])
def test_interrupted_contract_publication_replays_all_accepted_bytes(
    workspace: Path, monkeypatch: pytest.MonkeyPatch, replacement: int
) -> None:
    from zicato.check import CheckContext
    from zicato.contract_draft import publication
    from zicato.runtime.lock import acquire_workspace_lock
    from zicato.workspace.contract_publication import contract_publication_path

    draft = TournamentDraft.from_workspace(workspace)
    ops.set_brief(draft, "Accepted brief\n")
    ops.set_weights(draft, pass_weight=3.0)
    draft.entries.append(
        BoardEntry(id="accepted", kind="single_turn", wall_clock_budget_seconds=60, input="task")
    )
    draft.proposer_path = workspace.parent / "accepted-proposer"
    draft.proposer_path.mkdir()
    accepted_hash = ops.apply(draft, workspace, confirm=False).new_contract_hash
    epoch_id = current_epoch_id(workspace)
    atomic_write = publication.atomic_write_text
    replacements = 0

    def interrupt_after_replace(path: Path, text: str, **kwargs) -> None:
        nonlocal replacements
        atomic_write(path, text, **kwargs)
        replacements += 1
        if replacements == replacement:
            raise OSError("publication interrupted")

    monkeypatch.setattr(publication, "atomic_write_text", interrupt_after_replace)
    with pytest.raises(OSError, match="publication interrupted"):
        ops.apply(draft, workspace, confirm=True)
    record = json.loads(contract_publication_path(workspace).read_text())
    assert record["state"] == "pending"
    assert replacements == replacement
    for reader in (resolve_contract_inputs, TournamentDraft.from_workspace):
        with pytest.raises(ValueError, match="publication is pending"):
            reader(workspace)
    with pytest.raises(ValueError, match="publication is pending"):
        CheckContext(workspace, live_contract=True)
    monkeypatch.setattr(publication, "atomic_write_text", atomic_write)
    with acquire_workspace_lock(workspace, "contract-recovery") as writer:
        pending_bytes = {
            Path(write["path"]): Path(write["path"]).read_bytes() for write in record["writes"]
        }
        with pytest.raises(ValueError, match="publication is pending"):
            ops.apply(draft, workspace, confirm=False, writer=writer)
        assert {path: path.read_bytes() for path in pending_bytes} == pending_bytes
        assert publication.recover_contract_publication(workspace, writer=writer)
        assert not publication.recover_contract_publication(workspace, writer=writer)
    for write in record["writes"]:
        assert Path(write["path"]).read_bytes() == write["text"].encode()
    assert compute_contract_hash(resolve_contract_inputs(workspace)) == accepted_hash
    assert current_epoch_id(workspace) == epoch_id
    completed = json.loads(contract_publication_path(workspace).read_text())
    assert completed == {"version": 1, "revision": record["revision"], "state": "complete"}


def test_recovery_preserves_edits_made_after_an_interrupted_publication(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from zicato.contract_draft import publication
    from zicato.runtime.lock import acquire_workspace_lock

    draft = TournamentDraft.from_workspace(workspace)
    ops.set_brief(draft, "Accepted brief\n")
    ops.set_weights(draft, pass_weight=3.0)
    atomic_write = publication.atomic_write_text

    def interrupt(path: Path, text: str, **kwargs) -> None:
        atomic_write(path, text, **kwargs)
        raise OSError("publication interrupted")

    monkeypatch.setattr(publication, "atomic_write_text", interrupt)
    with pytest.raises(OSError, match="publication interrupted"):
        ops.apply(draft, workspace, confirm=True)
    (workspace.parent / "brief.md").write_text("Manual edit during recovery\n")
    assert draft.source is not None
    before = {file.path: file.path.read_bytes() for file in draft.source.files}
    monkeypatch.setattr(publication, "atomic_write_text", atomic_write)
    with acquire_workspace_lock(workspace, "contract-recovery") as writer:
        with pytest.raises(ValueError, match="brief"):
            publication.recover_contract_publication(workspace, writer=writer)
    assert {path: path.read_bytes() for path in before} == before


def test_malformed_candidate_is_refused_before_any_contract_publication(workspace: Path) -> None:
    from zicato.workspace.contract_publication import contract_publication_path

    draft = TournamentDraft.from_workspace(workspace)
    draft.entries.append(draft.entries[0])
    assert draft.source is not None
    before = {file.path: file.path.read_bytes() for file in draft.source.files}
    with pytest.raises(ValueError, match="duplicate entry"):
        ops.apply(draft, workspace, confirm=True)
    assert {path: path.read_bytes() for path in before} == before
    assert not contract_publication_path(workspace).exists()


def test_live_check_refuses_a_publication_between_component_reads(workspace: Path) -> None:
    from zicato.check import CheckContext

    with CheckContext(workspace, live_contract=True) as check:
        assert check.config.exists
        draft = TournamentDraft.from_workspace(workspace)
        ops.set_weights(draft, pass_weight=3.0)
        ops.apply(draft, workspace, confirm=True)
        assert "changed during reading" in (check.scoring_error or "")


def test_live_check_refuses_manual_edits_to_previously_read_files(workspace: Path) -> None:
    from zicato.check import CheckContext

    with CheckContext(workspace, live_contract=True) as check:
        assert check.config.exists
        config = workspace / "config.json"
        config.write_text(config.read_text() + "\n")
        assert "changed during reading" in (check.scoring_error or "")


def test_apply_uses_the_forwarded_live_writer(workspace: Path) -> None:
    from zicato.runtime.lock import WorkspaceLockHeld, acquire_workspace_lock

    draft = TournamentDraft.from_workspace(workspace)
    ops.set_brief(draft, "Accepted with shared mutation ownership")
    with acquire_workspace_lock(workspace, "invocation") as writer:
        assert ops.apply(draft, workspace, confirm=True, writer=writer).confirmed
    with pytest.raises(WorkspaceLockHeld):
        ops.apply(draft, workspace, confirm=True, writer=writer)


def test_tournament_edit_preserves_partial_scoring_and_unrelated_parameters(
    workspace: Path,
) -> None:
    from zicato.cli.commands.evolve import _tournament_draft

    scoring = workspace.parent / "scoring.json"
    authored = {
        "pass_weight": 1.3,
        "tournament": {"structure": "racing", "params": {"field_size": 3, "replicates": 2}},
    }
    scoring.write_text(json.dumps(authored))
    draft = _tournament_draft(workspace, "racing", ("replicates=4",))
    expected = {
        "pass_weight": 1.3,
        "tournament": {"structure": "racing", "params": {"field_size": 3, "replicates": 4}},
    }
    assert ops.candidate_scoring(draft) == expected
    ops.apply(draft, workspace, confirm=True)
    assert json.loads(scoring.read_text()) == expected


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
# Publishing a board preserves its disable_drift and judge_only header values.
# ---------------------------------------------------------------------------


@pytest.fixture()
def meta_workspace(tmp_path: Path) -> Path:
    """A workspace whose live board carries a non-default board_meta header."""
    ws = tmp_path / ".zicato"
    ws.mkdir()

    board = tmp_path / "board.jsonl"
    board.write_text(
        '{"board_meta": true, "disable_drift": ["off_topic"], "judge_only": true}\n'
        '{"id": "e1", "kind": "single_turn", "wall_clock_budget_seconds": 60, "input": "hi"}\n'
        '{"id": "e2", "kind": "single_turn", "wall_clock_budget_seconds": 60, "input": "bye"}\n',
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
            "adapter": {"kind": "adk", "entrypoint": "pkg.mod:agent"},
            "contract": {
                "board_path": str(board.resolve()),
                "brief_path": str(brief.resolve()),
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
