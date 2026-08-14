from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from zicato.evolve.generation_phase import (
    RoundSession,
    current_generation,
    mutable_trees,
    next_generation_id,
    round_number,
    safe_parent,
    set_current_generation,
)
from zicato.workspace import WorkspaceLayout


def test_round_session_is_immutable() -> None:
    session = RoundSession(Path("."), "e1", 2, 5, "worker", object(), object(), object())
    with pytest.raises(AttributeError):
        session.epoch_id = "e2"  # type: ignore[misc]


def test_round_pipeline_structure_stays_bounded() -> None:
    src = Path(__file__).parents[1] / "src" / "zicato"
    assert len((src / "orchestrator.py").read_text().splitlines()) < 1_000
    for name in (
        "decision_support.py",
        "round_api.py",
        "round_baseline.py",
        "round_prepare.py",
        "round_reporting.py",
    ):
        assert len((src / "evolve" / name).read_text().splitlines()) < 1_000

    entries = {"gauntlet.py": "evolve_once", "field.py": "evolve_field_round"}
    for name, expected in entries.items():
        tree = ast.parse((src / "evolve" / name).read_text())
        public_async = [
            node.name
            for node in tree.body
            if isinstance(node, ast.AsyncFunctionDef) and not node.name.startswith("_")
        ]
        assert public_async == [expected]


def test_generation_head_prefers_marker_then_falls_back_to_highest_vn(tmp_path: Path) -> None:
    layout = WorkspaceLayout.from_root(tmp_path)
    root = layout.generations_dir("e1")
    for name in ("v2", "v10", "named"):
        (root / name).mkdir(parents=True)

    assert current_generation(tmp_path, "e1") == "named"
    set_current_generation(tmp_path, "e1", "v2")
    assert current_generation(tmp_path, "e1") == "v2"
    assert next_generation_id(tmp_path, "e1") == "v11"


def test_generation_helpers_degrade_and_rebase(tmp_path: Path) -> None:
    assert round_number("v12") == 12
    assert round_number("named") is None
    assert safe_parent(tmp_path, None) == ""
    assert safe_parent(tmp_path, "missing") == ""
    snapshot = tmp_path / "snapshot"
    adapter = SimpleNamespace(mutable_subpaths=lambda root: [root / "prompts"])
    assert mutable_trees(adapter, snapshot) == [snapshot / "prompts"]
    assert mutable_trees(object(), snapshot) == [snapshot]
