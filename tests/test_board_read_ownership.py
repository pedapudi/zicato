"""Canonical board acceptance and one-observation query projections."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from zicato.epoch._storage import RecordError
from zicato.query import WorkspacePaths, build_epoch_view, build_search_results
from zicato.query.epoch_view import build_epochs_summary
from zicato.workspace import WorkspaceLayout, read_board
from zicato.workspace_loader import load_current_brief


@pytest.fixture
def workspace(tmp_path: Path) -> WorkspaceLayout:
    layout = WorkspaceLayout.from_root(tmp_path / ".zicato")
    layout.epoch_dir("e0").mkdir(parents=True)
    (layout.root / "current_epoch").write_text("e0")
    layout.board("e0").write_text(
        '{"board_meta": true, "disable_drift": ["tool_error"], "extension": 4}\n'
        '{"id": "sample", "kind": "single_turn", "input": "Task", "budget_s": 1, '
        '"judges": [{"name": "sample_judge", "mode": "inline", '
        '"body": "Check the answer", "severity": "warning"}]}\n'
    )
    return layout


@pytest.mark.parametrize("tail", ["{broken\n", "[]\n", '{"id":"incomplete"}\n'])
def test_present_invalid_board_is_not_a_shorter_board(
    workspace: WorkspaceLayout, tail: str
) -> None:
    board = workspace.board("e0")
    board.write_text(board.read_text() + tail)
    with pytest.raises(RecordError, match="line 3"):
        read_board(workspace, "e0")
    view = build_epoch_view(WorkspacePaths(workspace.root), "e0")
    assert "board" not in view
    assert "board_meta" not in view
    assert "board_judges" not in view
    assert "line 3" in view["unreadable"]


@pytest.mark.parametrize("reader", [build_epoch_view, build_search_results])
def test_board_is_read_once_per_response(workspace: WorkspaceLayout, monkeypatch, reader) -> None:
    original = Path.read_text
    reads = []

    def read(path, *args, **kwargs):
        if path == workspace.board("e0"):
            reads.append(path)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    paths = WorkspacePaths(workspace.root)
    view = reader(paths, "sample") if reader is build_search_results else reader(paths, "e0")
    if reader is build_search_results:
        assert view["entries"] == [{"id": "sample", "match_kind": "exact"}]
        assert view["judges"] == [{"name": "sample_judge", "match_kind": "substring"}]
    else:
        assert view["board"][0]["entry_id"] == "sample"
        assert view["board_meta"] == {"disable_drift": ["tool_error"], "judge_only": False}
        assert list(view["board_judges"]) == ["sample"]
    assert len(reads) == 1


def test_accepted_rows_preserve_source_values(workspace: WorkspaceLayout) -> None:
    path = workspace.board("e0")
    before = path.read_bytes()
    assert read_board(workspace, "e0") == [json.loads(line) for line in before.splitlines()]
    assert path.read_bytes() == before
    assert read_board(workspace, "absent") is None
    path.write_text("")
    assert read_board(workspace, "e0") == []


def test_retired_filename_does_not_supply_missing_brief(workspace: WorkspaceLayout) -> None:
    (workspace.epoch_dir("e0") / "rubric.md").write_text("## Goal\n\nObsolete guidance.\n")
    with pytest.raises(FileNotFoundError):
        load_current_brief(workspace.root)
    paths = WorkspacePaths(workspace.root)
    assert build_epoch_view(paths, "e0")["brief"] == ""
    assert build_epochs_summary(paths) == [{"epoch_id": "e0", "goal": None}]
    workspace.brief("e0").write_text("## Goal\n\nAccepted guidance.\n")
    assert load_current_brief(workspace.root).text == "## Goal\n\nAccepted guidance.\n"
    assert build_epochs_summary(paths) == [{"epoch_id": "e0", "goal": "Accepted guidance."}]


def test_retired_filename_does_not_change_default_contract_source(tmp_path: Path) -> None:
    from zicato.epoch.contract import default_contract_paths

    (tmp_path / "rubric.md").write_text("Obsolete guidance")
    assert default_contract_paths(tmp_path / ".zicato")["brief_path"] == tmp_path / "brief.md"


def test_candidate_facets_share_the_accepted_board(tmp_path: Path, monkeypatch) -> None:
    from tests.test_dashboard_server import _build_facet_workspace
    from zicato.query import build_per_entry_for_generation

    root = tmp_path / ".zicato"
    root.mkdir()
    _build_facet_workspace(root, [("sample", True, 0.6, 0.2)], {"sample": ["facet:quality"]})
    original = Path.read_text
    reads = []

    def read(path, *args, **kwargs):
        if path.name == "board.jsonl":
            reads.append(path)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    view = build_per_entry_for_generation(WorkspacePaths(root), "2026-05-16_e0", "v1")
    assert view["entries"][0]["facets"] == ["quality"]
    assert view["facet_scores"]["facets"]["quality"]["entry_count"] == 1
    assert len(reads) == 1
