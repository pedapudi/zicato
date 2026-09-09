"""Publication resumes from validated bytes without exposing partial candidates."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from zicato.core.types import ScoringWeights
from zicato.epoch import baseline, lifecycle, lineage
from zicato.epoch.genstore import DirectoryGenerationStore, default_generation_store
from zicato.epoch.git_genstore import GitGenerationStore
from zicato.epoch.publication import BaselineSeed, EpochPublication, prepared_directory
from zicato.evolve.epoching import ensure_epoch_for_contract
from zicato.evolve.generation_phase import current_generation
from zicato.evolve.round_baseline import _ensure_baseline_snapshot
from zicato.runtime.lock import acquire_workspace_lock
from zicato.workspace import WorkspaceLayout, generation_ids
from zicato.workspace.config_io import write_workspace_config


@pytest.fixture
def workspace(tmp_path: Path) -> tuple[Path, Path, Path]:
    root = tmp_path / ".zicato"
    root.mkdir()
    write_workspace_config(root, {"generation_source_backend": "directory"})
    board = tmp_path / "board.jsonl"
    board.write_text(
        '{"id":"entry","kind":"single_turn",' '"wall_clock_budget_seconds":60,"input":"hi"}\n',
        encoding="utf-8",
    )
    source = tmp_path / "source"
    source.mkdir()
    (source / "value.py").write_text("VALUE = 1\n", encoding="utf-8")
    return root, board, source


def create_epoch(root: Path, board: Path, name: str = "initial"):
    return lifecycle.new_epoch(root, name, board, "Improve the measured result.", ScoringWeights())


def fail_after(monkeypatch: pytest.MonkeyPatch, owner, name: str) -> None:
    original = getattr(owner, name)

    def interrupted(*args, **kwargs):
        original(*args, **kwargs)
        raise OSError("publication interrupted")

    monkeypatch.setattr(owner, name, interrupted)


def test_invalid_name_preserves_open_predecessor(workspace) -> None:
    root, board, _ = workspace
    previous = create_epoch(root, board)
    before = (root / "epochs" / previous.id / "config.json").read_bytes()
    with pytest.raises(ValueError, match="empty slug"):
        create_epoch(root, board, "!!!")
    assert lifecycle.current_epoch_id(root) == previous.id
    assert not lifecycle.load_epoch(root, previous.id).closed
    assert (root / "epochs" / previous.id / "config.json").read_bytes() == before


@pytest.mark.parametrize(
    "boundary",
    [
        "intent",
        "directory",
        "lineage",
        "marker",
        "closure",
    ],
)
def test_epoch_recovery_finishes_same_prepared_contract(workspace, monkeypatch, boundary) -> None:
    root, board, source = workspace
    previous = create_epoch(root, board)
    boundaries = {
        "intent": (EpochPublication, "write"),
        "directory": (lifecycle, "publish_directory"),
        "lineage": (lineage, "register_epoch"),
        "marker": (lifecycle, "switch_epoch"),
        "closure": (lifecycle, "_close_epoch_prelude"),
    }
    with acquire_workspace_lock(root, "publication-test") as writer:
        with monkeypatch.context() as fault:
            fail_after(fault, *boundaries[boundary])
            with pytest.raises(OSError, match="publication interrupted"):
                lifecycle._prepare_epoch(
                    root,
                    "measured",
                    board,
                    "Retained brief.",
                    ScoringWeights(),
                    writer=writer,
                    baseline_sources=(source,),
                )
                lifecycle.recover_epoch_publication(root, writer=writer)
        pending = EpochPublication.read(root)
        assert pending is not None
        closed_at = pending.predecessor_closed_at
        assert closed_at
        # The source bytes remain in the prepared tree before the live edit.
        seed = BaselineSeed.read(
            root,
            pending.epoch_id,
            path=(root / "epochs" / pending.epoch_id / "baseline_seed.json")
            if (root / "epochs" / pending.epoch_id).exists()
            else prepared_directory(root, pending.prepared_directory) / "baseline_seed.json",
        )
        assert seed is not None
        retained = baseline.validate_baseline_seed(root, seed)
        assert (retained / "source" / "value.py").read_bytes() == b"VALUE = 1\n"
        (source / "value.py").write_text("VALUE = 2\n")
        board.write_text("changed live board\n")
        result = lifecycle.recover_epoch_publication(root, writer=writer)
        assert result is not None and result.id == pending.epoch_id
        assert result.contract_hash == pending.contract_hash
        assert result.brief_path.read_text() == "Retained brief."
        assert result.board_path.parent == root / "epochs" / result.id
        assert lifecycle.recover_epoch_publication(root, writer=writer) is None
    assert lifecycle.current_epoch_id(root) == pending.epoch_id
    prior = lifecycle.load_epoch(root, previous.id)
    assert prior.closed and prior.closed_at == closed_at
    rows = lineage.load_lineage(root).to_dict()["epochs"]
    assert [row["id"] for row in rows] == [previous.id, pending.epoch_id]


@pytest.mark.parametrize("damage", ["missing", "changed"])
def test_damaged_prepared_source_preserves_previous_epoch(workspace, damage) -> None:
    root, board, source = workspace
    previous = create_epoch(root, board)
    with acquire_workspace_lock(root, "publication-test") as writer:
        operation = lifecycle._prepare_epoch(
            root,
            "measured",
            board,
            "Retained brief.",
            ScoringWeights(),
            writer=writer,
            baseline_sources=(source,),
        )
        seed = BaselineSeed.read(
            root,
            operation.epoch_id,
            path=prepared_directory(root, operation.prepared_directory) / "baseline_seed.json",
        )
        assert seed is not None
        prepared = baseline.validate_baseline_seed(root, seed)
        target = prepared / "source" / "value.py"
        assert target.read_bytes() == (source / "value.py").read_bytes()
        if damage == "missing":
            prepared.rename(prepared.with_name("retained-source"))
        else:
            target.write_text("damaged retained source\n")
        with pytest.raises((FileNotFoundError, ValueError), match="prepared baseline source"):
            lifecycle.recover_epoch_publication(root, writer=writer)
    assert lifecycle.current_epoch_id(root) == previous.id
    assert not lifecycle.load_epoch(root, previous.id).closed
    assert not (root / "epochs" / operation.epoch_id).exists()


@pytest.mark.parametrize("backend", ["directory", "git"])
@pytest.mark.parametrize("boundary", ["source", "lineage", "experiment"])
def test_baseline_recovery_preserves_source_and_surviving_runs(
    workspace, monkeypatch, backend, boundary
) -> None:
    root, board, source = workspace
    epoch = create_epoch(root, board)
    config = {
        "generation_source_backend": backend,
        "adapter": {
            "kind": "import",
            "factory": "tests._stub_adapter:make_stub_adapter",
            "mutable_trees": [str(source)],
        },
    }
    write_workspace_config(root, config)
    factory = DirectoryGenerationStore if backend == "directory" else GitGenerationStore
    boundaries = {
        "source": (factory, "seed_generation"),
        "lineage": (baseline, "append_to_lineage"),
        "experiment": (baseline, "write_seed_experiment"),
    }
    with acquire_workspace_lock(root, "publication-test") as writer:
        with monkeypatch.context() as fault:
            fail_after(fault, *boundaries[boundary])
            with pytest.raises(OSError, match="publication interrupted"):
                _ensure_baseline_snapshot(root, epoch.id, config, writer=writer)
        seed = BaselineSeed.read(root, epoch.id)
        assert seed is not None
        store = factory(root)
        assert store.read_file(epoch.id, "v0", "source/value.py") == b"VALUE = 1\n"
        (source / "value.py").write_text("VALUE = 2\n")
        record = root / "epochs" / epoch.id / "generations" / "v0"
        evaluation = record / "runs" / "entry" / "result.json"
        evaluation.parent.mkdir(parents=True, exist_ok=True)
        evaluation.write_text('{"measured":true}\n')
        _ensure_baseline_snapshot(root, epoch.id, config, writer=writer)
        _ensure_baseline_snapshot(root, epoch.id, config, writer=writer)
    rows = lineage.load_lineage(root).to_dict()["epochs"]
    generations = next(row["generations"] for row in rows if row["id"] == epoch.id)
    assert [row["id"] for row in generations] == ["v0"]
    assert generations[0]["created_at"] == seed.created_at
    assert current_generation(root, epoch.id) == "v0"
    assert (record / "experiment.json").is_file()
    assert store.read_file(epoch.id, "v0", "source/value.py") == b"VALUE = 1\n"
    assert evaluation.read_bytes() == b'{"measured":true}\n'
    assert BaselineSeed.read(root, epoch.id) is None


@pytest.mark.parametrize("backend", ["directory", "git"])
def test_automatic_roll_retains_promoted_source_and_coordinates(workspace, monkeypatch, backend):
    root, board, source = workspace
    brief = root.parent / "brief.md"
    brief.write_text("Initial brief.\n")
    scoring = root.parent / "scoring.json"
    scoring.write_text("{}\n")
    config = {
        "generation_source_backend": backend,
        "adapter": {
            "kind": "import",
            "factory": "tests._stub_adapter:make_stub_adapter",
            "mutable_trees": [str(source)],
        },
        "contract": {
            "board_path": str(board),
            "brief_path": str(brief),
            "scoring_path": str(scoring),
        },
    }
    write_workspace_config(root, config)
    reconciled = []
    with acquire_workspace_lock(root, "publication-test") as writer:
        first = asyncio.run(
            ensure_epoch_for_contract(
                root,
                auto_epoch=True,
                aux_call_llm=None,
                writer=writer,
            )
        )
        _ensure_baseline_snapshot(root, first, config, writer=writer)
        # Contract contents survive in the sealed predecessor before this edit.
        assert lifecycle.load_epoch(root, first).brief_path.read_bytes() == brief.read_bytes()
        brief.write_text("Changed evaluation brief.\n")
        with monkeypatch.context() as fault:
            fail_after(fault, EpochPublication, "write")
            with pytest.raises(OSError, match="publication interrupted"):
                asyncio.run(
                    ensure_epoch_for_contract(
                        root,
                        auto_epoch=True,
                        aux_call_llm=None,
                        writer=writer,
                        before_contract_roll=reconciled.append,
                    )
                )
        assert reconciled == [first]
        assert not lifecycle.load_epoch(root, first).closed
        assert (
            default_generation_store(root).read_file(first, "v0", "source/value.py")
            == b"VALUE = 1\n"
        )
        (source / "value.py").write_text("VALUE = 2\n")
        second = asyncio.run(
            ensure_epoch_for_contract(
                root,
                auto_epoch=True,
                aux_call_llm=None,
                writer=writer,
                before_contract_roll=reconciled.append,
            )
        )
        seed = BaselineSeed.read(root, second)
        assert seed is not None and (seed.source_epoch, seed.source_generation) == (first, "v0")
        assert generation_ids(WorkspaceLayout(root), second) == []
        _ensure_baseline_snapshot(root, second, config, writer=writer)
    assert reconciled == [first]
    assert second != first and lifecycle.load_epoch(root, first).closed
    assert (
        default_generation_store(root).read_file(second, "v0", "source/value.py") == b"VALUE = 1\n"
    )
    row = next(row for row in lineage.load_lineage(root).to_dict()["epochs"] if row["id"] == second)
    assert row["v0_parent"] == f"{first}:v0"
    assert row["generations"][0]["parent_id"] == f"{first}:v0"


@pytest.mark.parametrize("invalid", ["board", "missing-source", "duplicate-source"])
def test_preparation_rejects_invalid_inputs_before_reconciliation(workspace, invalid) -> None:
    root, board, source = workspace
    previous = create_epoch(root, board)
    sources = (source,)
    if invalid == "board":
        assert lifecycle.load_epoch(root, previous.id).board_path.read_bytes() == board.read_bytes()
        board.write_text("invalid board\n")
    elif invalid == "missing-source":
        sources += (source.parent / "missing",)
    else:
        sources += (source,)
    reconciled = []
    with acquire_workspace_lock(root, "publication-test") as writer:
        with pytest.raises((ValueError, FileNotFoundError)):
            lifecycle._prepare_epoch(
                root,
                "measured",
                board,
                "Retained brief.",
                ScoringWeights(),
                writer=writer,
                baseline_sources=sources,
                before_contract_roll=reconciled.append,
            )
    assert reconciled == []
    assert lifecycle.current_epoch_id(root) == previous.id
    assert not lifecycle.load_epoch(root, previous.id).closed
    assert [epoch.id for epoch in lifecycle.list_epochs(root)] == [previous.id]
    assert EpochPublication.read(root) is None


def test_git_seed_recovery_materializes_published_tag_without_reseeding(workspace, monkeypatch):
    root, board, source = workspace
    epoch = create_epoch(root, board)
    config = {
        "generation_source_backend": "git",
        "adapter": {
            "kind": "import",
            "factory": "tests._stub_adapter:make_stub_adapter",
            "mutable_trees": [str(source)],
        },
    }
    write_workspace_config(root, config)
    with acquire_workspace_lock(root, "publication-test") as writer:
        with monkeypatch.context() as fault:

            def unavailable(*args, **kwargs):
                raise OSError("worktree materialization interrupted")

            fault.setattr(GitGenerationStore, "materialize_snapshot", unavailable)
            with pytest.raises(OSError, match="worktree materialization interrupted"):
                _ensure_baseline_snapshot(root, epoch.id, config, writer=writer)
        store = GitGenerationStore(root)
        assert store.has_generation(epoch.id, "v0")
        commit = store._git("rev-parse", f"epoch/{epoch.id}/v0").strip()
        with monkeypatch.context() as fault:

            def no_reseed(*args, **kwargs):
                raise AssertionError("a published generation must not be seeded again")

            fault.setattr(GitGenerationStore, "seed_generation", no_reseed)
            _ensure_baseline_snapshot(root, epoch.id, config, writer=writer)
        assert store._git("rev-parse", f"epoch/{epoch.id}/v0").strip() == commit
        assert store.read_file(epoch.id, "v0", "source/value.py") == b"VALUE = 1\n"
    assert BaselineSeed.read(root, epoch.id) is None


@pytest.mark.parametrize("body", ["null", '{"format_version":1,"epoch_id":"../other"}'])
def test_invalid_publication_record_cannot_change_current_epoch(workspace, body) -> None:
    root, board, _ = workspace
    previous = create_epoch(root, board)
    record = root / "epoch_publication.json"
    assert not record.exists()
    record.write_text(body)
    with acquire_workspace_lock(root, "publication-test") as writer:
        with pytest.raises(ValueError, match="invalid publication record fields"):
            lifecycle.recover_epoch_publication(root, writer=writer)
    assert lifecycle.current_epoch_id(root) == previous.id
    assert not lifecycle.load_epoch(root, previous.id).closed
    assert record.read_text() == body
