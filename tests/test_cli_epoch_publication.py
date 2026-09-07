"""Explicit epoch creation owns capture and recoverable live-source adoption."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from zicato.cli.commands.epoch import epoch_grp
from zicato.core.types import ScoringWeights
from zicato.epoch import lifecycle
from zicato.epoch.contract import compute_contract_hash, resolve_contract_inputs
from zicato.epoch.publication import EpochPublication, epoch_publication_path
from zicato.runtime.lock import WorkspaceLock, WorkspaceLockHeld, acquire_workspace_lock
from zicato.workspace.config_io import write_workspace_config
from zicato.workspace.contract_publication import read_contract_publication


@pytest.fixture
def contract_files(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    root = tmp_path / ".zicato"
    root.mkdir()
    write_workspace_config(root, {"generation_source_backend": "directory"})
    supplied = tmp_path / "supplied"
    supplied.mkdir()
    board = supplied / "board.jsonl"
    board.write_text(
        '{"id":"entry","kind":"single_turn",' '"wall_clock_budget_seconds":60,"input":"hi"}\n'
    )
    brief = supplied / "brief.md"
    brief.write_text("Improve measured behavior.\n")
    scoring = supplied / "scoring.json"
    scoring.write_text('{"pass_weight":2.0}\n')
    return root, board, brief, scoring


def invoke_new(root: Path, board: Path, brief: Path, scoring: Path | None, name="explicit"):
    args = ["new", name, "--workspace", str(root), "--board", str(board), "--brief", str(brief)]
    if scoring is not None:
        args += ["--scoring", str(scoring)]
    return CliRunner().invoke(epoch_grp, args)


def test_held_writer_refuses_before_reading_supplied_scoring(contract_files) -> None:
    root, board, brief, scoring = contract_files
    scoring.write_text("invalid JSON")
    before = (root / "config.json").read_bytes()
    with acquire_workspace_lock(root, "competing-invocation"):
        result = invoke_new(root, board, brief, scoring)
    assert isinstance(result.exception, WorkspaceLockHeld), repr(result.exception)
    assert (root / "config.json").read_bytes() == before
    assert lifecycle.current_epoch_id(root) is None


def test_lifecycle_borrows_writer_without_releasing_it(contract_files) -> None:
    root, board, brief, _ = contract_files
    with acquire_workspace_lock(root, "explicit-epoch") as writer:
        cfg = lifecycle.new_epoch(root, "borrowed", board, brief, ScoringWeights(), writer=writer)
        assert lifecycle.current_epoch_id(root) == cfg.id
        with pytest.raises(WorkspaceLockHeld):
            acquire_workspace_lock(root, "competing-invocation")


def test_supplied_edits_after_capture_do_not_change_adopted_contract(
    contract_files, monkeypatch
) -> None:
    root, board, brief, scoring = contract_files
    accepted_brief = brief.read_bytes()
    original = lifecycle.new_epoch

    def change_supplied(*args, **kwargs):
        cfg = original(*args, **kwargs)
        with pytest.raises(WorkspaceLockHeld):
            acquire_workspace_lock(root, "competing-invocation")
        brief.write_text("An unrelated later edit.\n")
        return cfg

    monkeypatch.setattr(lifecycle, "new_epoch", change_supplied)
    result = invoke_new(root, board, brief, scoring)
    assert result.exit_code == 0, repr(result.exception)
    inputs = resolve_contract_inputs(root)
    cfg = lifecycle.load_epoch(root, lifecycle.current_epoch_id(root))
    assert inputs.brief_path.read_bytes() == accepted_brief
    assert cfg.brief_path.read_bytes() == accepted_brief
    assert cfg.contract_hash == compute_contract_hash(inputs)


def test_relative_brief_destination_keeps_hash_identity(contract_files, monkeypatch) -> None:
    root, board, brief, scoring = contract_files
    monkeypatch.chdir(root.parent)
    config = json.loads((root / "config.json").read_text())
    config["contract"] = {"brief_path": "live-brief.md"}
    write_workspace_config(root, config)
    result = invoke_new(root, board, brief, scoring)
    assert result.exit_code == 0, repr(result.exception)
    inputs = resolve_contract_inputs(root)
    assert inputs.brief_path == root.parent / "live-brief.md"
    cfg = lifecycle.load_epoch(root, lifecycle.current_epoch_id(root))
    assert cfg.contract_hash == compute_contract_hash(inputs)


def test_default_scoring_is_also_adopted(contract_files) -> None:
    root, board, brief, _ = contract_files
    (root.parent / "scoring.json").write_text('{"pass_weight":19.0}\n')
    result = invoke_new(root, board, brief, None)
    assert result.exit_code == 0, repr(result.exception)
    cfg = lifecycle.load_epoch(root, lifecycle.current_epoch_id(root))
    assert cfg.scoring.pass_weight == ScoringWeights().pass_weight
    assert cfg.contract_hash == compute_contract_hash(resolve_contract_inputs(root))
    assert (root.parent / "scoring.json").read_text() == "{}\n"


def fail_after(monkeypatch, owner, name):
    original = getattr(owner, name)

    def interrupted(*args, **kwargs):
        original(*args, **kwargs)
        raise OSError("publication interrupted")

    monkeypatch.setattr(owner, name, interrupted)


@pytest.mark.parametrize("resume", ["explicit", "evolve"])
@pytest.mark.parametrize(
    "boundary", ["intent", "partial-adoption", "adoption", "directory", "marker"]
)
def test_interrupted_creation_recovers_captured_contract(
    contract_files, monkeypatch, boundary, resume
) -> None:
    from zicato.contract_draft import publication
    from zicato.evolve.epoching import ensure_epoch_for_contract

    root, board, brief, scoring = contract_files
    accepted = (board.read_bytes(), brief.read_bytes())
    previous = lifecycle.new_epoch(root, "previous", board, brief, ScoringWeights())
    boundaries = {
        "intent": (EpochPublication, "write"),
        "partial-adoption": (publication, "atomic_write_text"),
        "adoption": (publication, "publish_prepared_contract"),
        "directory": (lifecycle, "publish_directory"),
        "marker": (lifecycle, "switch_epoch"),
    }
    with monkeypatch.context() as fault:
        fail_after(fault, *boundaries[boundary])
        result = invoke_new(root, board, brief, scoring)
    assert isinstance(result.exception, OSError), repr(result.exception)
    pending = EpochPublication.read(root)
    assert pending is not None
    if boundary in {"intent", "partial-adoption", "adoption"}:
        assert lifecycle.current_epoch_id(root) == previous.id
        assert not lifecycle.load_epoch(root, previous.id).closed
    completed = read_contract_publication(root)
    # Retained bytes make recovery independent of supplied files, even their existence.
    board.unlink()
    brief.write_text("Supplied content changed after the interrupted command.\n")
    scoring.write_text("invalid JSON")
    if resume == "explicit":
        result = invoke_new(root, board, brief, scoring)
        assert result.exit_code == 0, repr(result.exception)
    else:
        with acquire_workspace_lock(root, "evolve-recovery") as writer:
            epoch_id = asyncio.run(
                ensure_epoch_for_contract(root, auto_epoch=True, aux_call_llm=None, writer=writer)
            )
        assert epoch_id == pending.epoch_id
    assert lifecycle.current_epoch_id(root) == pending.epoch_id
    assert len(lifecycle.list_epochs(root)) == 2
    cfg = lifecycle.load_epoch(root, pending.epoch_id)
    inputs = resolve_contract_inputs(root)
    assert (cfg.board_path.read_bytes(), cfg.brief_path.read_bytes()) == accepted
    assert (inputs.board_path.read_bytes(), inputs.brief_path.read_bytes()) == accepted
    assert cfg.contract_hash == compute_contract_hash(inputs)
    assert cfg.scoring.pass_weight == 2.0
    assert inputs.scoring_path.read_bytes() == b'{"pass_weight":2.0}\n'
    assert lifecycle.load_epoch(root, previous.id).closed
    assert EpochPublication.read(root) is None
    if completed is not None:
        assert read_contract_publication(root)["revision"] == completed["revision"]


@pytest.mark.parametrize(
    "invalid", ["name", "board", "scoring", "missing-source", "duplicate-source"]
)
def test_rejected_creation_preserves_live_contract_and_predecessor(contract_files, invalid) -> None:
    root, board, brief, scoring = contract_files
    assert invoke_new(root, board, brief, scoring, "previous").exit_code == 0
    previous = lifecycle.current_epoch_id(root)
    name = "explicit"
    if invalid == "name":
        name = "!!!"
    elif invalid == "board":
        board.write_text("invalid JSON")
    elif invalid == "scoring":
        scoring.write_text('{"pass_weight":"wrong type"}')
    else:
        config = json.loads((root / "config.json").read_text())
        sources = [root.parent / "one" / "source", root.parent / "two" / "source"]
        if invalid == "duplicate-source":
            for source in sources:
                source.mkdir(parents=True)
        config["mutable_trees"] = [str(source) for source in sources]
        write_workspace_config(root, config)
    paths = [root / "config.json", root / "epochs" / previous / "config.json"]
    paths += [root.parent / filename for filename in ("board.jsonl", "brief.md", "scoring.json")]
    before = {path: path.read_bytes() for path in paths}
    result = invoke_new(root, board, brief, scoring, name)
    assert result.exit_code != 0
    assert {path: path.read_bytes() for path in paths} == before
    assert lifecycle.current_epoch_id(root) == previous
    assert not epoch_publication_path(root).exists()
    assert len(lifecycle.list_epochs(root)) == 1


def test_recovery_preserves_intervening_live_edit(contract_files, monkeypatch) -> None:
    root, board, brief, scoring = contract_files
    with monkeypatch.context() as fault:
        fail_after(fault, EpochPublication, "write")
        assert invoke_new(root, board, brief, scoring).exit_code != 0
    live_brief = root.parent / "brief.md"
    live_brief.write_text("An intervening live edit.\n")
    result = invoke_new(root, board, brief, scoring)
    assert isinstance(result.exception, ValueError)
    assert "conflicts with edited files: brief" in str(result.exception)
    assert live_brief.read_text() == "An intervening live edit.\n"
    assert lifecycle.current_epoch_id(root) is None
    assert EpochPublication.read(root) is not None


@pytest.mark.parametrize("invalid", ["inspection", "other-workspace"])
def test_borrowed_writer_requires_actual_workspace_ownership(contract_files, invalid) -> None:
    root, board, brief, _ = contract_files
    owned_root = root if invalid == "inspection" else root.parent / "other"
    with acquire_workspace_lock(owned_root, "owner") as owner:
        writer = WorkspaceLock.from_dict(owner.to_dict()) if invalid == "inspection" else owner
        with pytest.raises((ValueError, RuntimeError)):
            lifecycle.new_epoch(root, "invalid", board, brief, ScoringWeights(), writer=writer)
        with pytest.raises(WorkspaceLockHeld):
            acquire_workspace_lock(owned_root, "competitor")
    assert lifecycle.current_epoch_id(root) is None


@pytest.mark.parametrize("damage", ["revision", "workspace", "digest", "board", "identity"])
def test_epoch_refuses_invalid_adoption_before_recording_intent(
    contract_files, monkeypatch, damage
) -> None:
    root, board, brief, scoring = contract_files
    original = lifecycle.new_epoch
    before = (root / "config.json").read_bytes()

    def corrupted(*args, **kwargs):
        record = json.loads(kwargs["contract_adoption"])
        writes = record["writes"]
        if damage == "revision":
            record["revision"] = "invalid revision"
        elif damage == "workspace":
            writes[-1]["path"] = str(root.parent / "other" / "config.json")
        elif damage == "digest":
            writes[0]["accepted_sha256"] = "0" * 64
        elif damage == "board":
            assert '"hi"' in writes[0]["text"]
            writes[0]["text"] = writes[0]["text"].replace('"hi"', '"different task"')
            writes[0]["accepted_sha256"] = hashlib.sha256(writes[0]["text"].encode()).hexdigest()
        else:
            config = json.loads(writes[-1]["text"])
            config["contract"]["proposer_static_checks"] = ["ruff"]
            writes[-1]["text"] = json.dumps(config)
            writes[-1]["accepted_sha256"] = hashlib.sha256(writes[-1]["text"].encode()).hexdigest()
        kwargs["contract_adoption"] = json.dumps(record)
        return original(*args, **kwargs)

    monkeypatch.setattr(lifecycle, "new_epoch", corrupted)
    result = invoke_new(root, board, brief, scoring)
    assert isinstance(result.exception, ValueError), repr(result.exception)
    assert (root / "config.json").read_bytes() == before
    assert lifecycle.current_epoch_id(root) is None
    assert not epoch_publication_path(root).exists()
    assert read_contract_publication(root) is None


def test_epoch_recovery_does_not_replace_another_pending_adoption(contract_files, monkeypatch):
    from uuid import uuid4

    from zicato.epoch.publication import prepared_directory
    from zicato.storage import atomic_write_json
    from zicato.workspace.contract_publication import contract_publication_path

    root, board, brief, scoring = contract_files
    with monkeypatch.context() as fault:
        fail_after(fault, EpochPublication, "write")
        assert invoke_new(root, board, brief, scoring).exit_code != 0
    operation = EpochPublication.read(root)
    retained = prepared_directory(root, operation.prepared_directory)
    other = json.loads((retained / "contract_adoption.json").read_text())
    other["revision"] = str(uuid4())
    atomic_write_json(contract_publication_path(root), other)
    with acquire_workspace_lock(root, "epoch-recovery") as writer:
        with pytest.raises(ValueError, match="another contract publication is pending"):
            lifecycle.recover_epoch_publication(root, writer=writer)
    assert read_contract_publication(root) == other
    assert lifecycle.current_epoch_id(root) is None


def test_completed_adoption_is_not_replayed_over_a_later_edit(contract_files, monkeypatch):
    from zicato.contract_draft import publication

    root, board, brief, scoring = contract_files
    with monkeypatch.context() as fault:
        fail_after(fault, publication, "publish_prepared_contract")
        assert invoke_new(root, board, brief, scoring).exit_code != 0
    completed = read_contract_publication(root)
    assert completed["state"] == "complete"
    live_brief = root.parent / "brief.md"
    live_brief.unlink()
    with acquire_workspace_lock(root, "epoch-recovery") as writer:
        with pytest.raises(ValueError, match="conflicts with edited files: brief"):
            lifecycle.recover_epoch_publication(root, writer=writer)
    assert not live_brief.exists()
    assert read_contract_publication(root) == completed
    assert lifecycle.current_epoch_id(root) is None
