from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from zicato.evolve.generation_phase import (
    PreparedRound,
    current_generation,
    mutable_trees,
    next_generation_id,
    round_number,
    safe_parent,
    set_current_generation,
)
from zicato.workspace import WorkspaceLayout


def test_prepared_round_is_immutable() -> None:
    session = PreparedRound(
        workspace_root=Path("."),
        workspace_config={},
        epoch_id="e1",
        round_index=2,
        total_rounds=5,
        instance_id="worker",
        parent_generation=object(),
        adapter=object(),
        config=object(),
        weights=object(),
        board=(),
        train_board=(),
        tournament_spec=object(),
        strategy=object(),
        brief=object(),
        mutations=(),
        patterns=(),
        loss_summary="",
        failure_profile="",
        metric_priorities="",
        process_exemplars="",
        genealogy=(),
        calibration=None,
        disable_drift=(),
        judge_only=False,
        fast_mode=False,
        max_proposer_retries=2,
        beater=None,
        meta_loop_emitter=None,
        proposer_agent=object(),
        round_log=object(),
        screen_candidates=None,
        recombine_pair=None,
        custom_judge_names=frozenset(),
    )
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
    (tmp_path / "config.json").write_text(
        '{"generation_source_backend": "directory"}', encoding="utf-8"
    )
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
