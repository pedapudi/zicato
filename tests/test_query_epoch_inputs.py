"""Composed views retain the selected epoch and one observation of each input."""

from __future__ import annotations

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from threading import Barrier

import pytest

from tests._workspace_support import (
    experiment_record,
    seed_index,
    workspace,
    write_epoch,
    write_generation,
    write_json,
    write_lineage,
    write_workspace_config,
)
from zicato.mutation.markers import active_syntax_table
from zicato.query import WorkspacePaths, build_epoch_view
from zicato.query.candidate_view import build_candidate_dossier
from zicato.query.inputs import EpochInputs
from zicato.query.judge_view import build_environment
from zicato.query.runtime_view import RuntimeInputs, build_snapshot, derive_liveness
from zicato.runtime.state import ActiveRun, Heartbeat, write_active_run, write_heartbeat


def test_home_champion_uses_selected_epoch_and_served_evidence(tmp_path: Path) -> None:
    """Repeated generation names obey the server-owned, epoch-scoped decision rule."""
    layout = workspace(tmp_path)
    lineage = []
    ratings = []
    for epoch_id, promoted, rating in (("earlier", False, 1200.0), ("selected", True, 1800.0)):
        write_epoch(layout, epoch_id, current=epoch_id == "earlier")
        write_generation(layout, epoch_id, "v0")
        write_generation(
            layout,
            epoch_id,
            "v1",
            experiment=experiment_record("v1", epoch_id=epoch_id, parent_generation_id="v0"),
        )
        lineage.append(
            {
                "id": epoch_id,
                "generations": [
                    {"id": "v0", "parent_id": None, "promoted": True},
                    {"id": "v1", "parent_id": "v0", "promoted": promoted},
                ],
            }
        )
        ratings.append(
            {
                "epoch_id": epoch_id,
                "generation_id": "v1",
                "elo": rating,
                "elo_se": 20,
                "elo_games": 8,
            }
        )
    write_lineage(layout, {"epochs": lineage})
    seed_index(layout, {"generations": ratings})
    paths = WorkspacePaths(layout.root)
    epoch = build_epoch_view(paths, "selected")
    assert epoch["champion_record"]["elo"] == 1800.0


def test_champion_without_index_has_unavailable_rating(tmp_path: Path) -> None:
    layout = workspace(tmp_path)
    write_epoch(layout, "selected")
    write_generation(layout, "selected", "v0")
    write_lineage(
        layout,
        {
            "epochs": [
                {
                    "id": "selected",
                    "generations": [{"id": "v0", "parent_id": None, "promoted": True}],
                }
            ]
        },
    )
    epoch = build_epoch_view(WorkspacePaths(layout.root), "selected")
    assert epoch["champion_record"]["generation_id"] == "v0"
    assert epoch["champion_record"]["elo"] is None
    assert epoch["champion_record"]["elo_se"] is None
    assert epoch["champion_record"]["elo_games"] is None


def test_environment_shares_runtime_observations_during_rewrite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = workspace(tmp_path)
    write_epoch(layout, "selected", current=True)
    write_epoch(layout, "other")
    write_workspace_config(layout, {"adapter": {"mutable_trees": [str(tmp_path / "source")]}})
    now = datetime.now(UTC)
    stamp = now.isoformat()
    heartbeat = Heartbeat(123, "observed", stamp, stamp, epoch_id="selected", phase="proposer")
    write_heartbeat(layout.root, heartbeat)
    run = ActiveRun("run", 123, stamp, stamp, 60, stamp, "", "task", "v0", "selected")
    write_active_run(layout.root, run)
    paths = WorkspacePaths(layout.root)
    replacements = {
        layout.current_epoch_marker: "other",
        paths.heartbeat: replace(heartbeat, instance_id="replaced", phase="evolve:done").to_dict(),
        paths.active_runs_dir / "run.json": replace(
            run, last_progress="2000-01-01T00:00:00Z"
        ).to_dict(),
    }
    counts: Counter[Path] = Counter()
    original = Path.read_text

    def rewrite_after_read(path: Path, *args: object, **kwargs: object) -> str:
        text = original(path, *args, **kwargs)
        if path in replacements:
            counts[path] += 1
            if counts[path] == 1:
                value = replacements[path]
                if isinstance(value, str):
                    path.write_text(value)
                else:
                    write_json(path, value)
        return text

    monkeypatch.setattr(Path, "read_text", rewrite_after_read)
    response = build_environment(paths)
    assert response["epoch_id"] == "selected"
    assert response["workspace"]["board_path"] == str(layout.board("selected"))
    assert response["workspace"]["instance_id"] == "observed"
    assert response["heartbeat"]["instance_id"] == "observed"
    assert response["active_runs"][0]["last_progress"] == stamp
    assert response["liveness"]["state"] == "live"
    assert counts == Counter({path: 1 for path in replacements})
    following = build_environment(paths)
    assert following["epoch_id"] == "other"
    assert following["heartbeat"]["instance_id"] == "replaced"
    assert following["liveness"]["state"] == "settled"


def test_candidate_reuses_record_and_contract_after_interleaved_rewrite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = workspace(tmp_path)
    write_epoch(layout, "selected", scoring={"promote_margin": 0.25}, current=True)
    write_generation(layout, "selected", "v0")
    body = experiment_record(
        "v1",
        epoch_id="selected",
        parent_generation_id="v0",
        hypothesis={"core_idea": "Observed hypothesis", "why": "Measured reason"},
        outcome={"operator_override": True, "operator_override_reason": "Observed reason"},
    )
    write_generation(layout, "selected", "v1", experiment=body)
    watched = {
        layout.epoch_config("selected"),
        layout.scoring("selected"),
        layout.experiment("selected", "v0"),
        layout.experiment("selected", "v1"),
    }
    counts: Counter[Path] = Counter()
    original = Path.read_text

    def rewrite_after_read(path: Path, *args: object, **kwargs: object) -> str:
        text = original(path, *args, **kwargs)
        if path in watched:
            counts[path] += 1
            if counts[path] == 1:
                if path == layout.scoring("selected"):
                    write_json(path, {"promote_margin": 0.75})
                elif path == layout.experiment("selected", "v1"):
                    write_json(
                        path,
                        experiment_record("v1", epoch_id="selected", parent_generation_id="v9"),
                    )
        return text

    monkeypatch.setattr(Path, "read_text", rewrite_after_read)
    response = build_candidate_dossier(WorkspacePaths(layout.root), "selected", "v1")
    assert response["parent"] == "v0"
    gate = response["gates"][0]["gate"]
    assert gate["margin"] == 0.25
    assert gate["override"]["reason"] == "Observed reason"
    assert counts == Counter({path: 1 for path in watched})
    following = build_candidate_dossier(WorkspacePaths(layout.root), "selected", "v1")
    assert following["parent"] == "v9"
    assert following["gates"][0]["gate"]["margin"] == 0.75


def test_captured_absence_and_copies_do_not_reload_or_leak(tmp_path: Path) -> None:
    layout = workspace(tmp_path)
    layout.epoch_dir("selected").mkdir(parents=True)
    paths = WorkspacePaths(layout.root)
    epoch = EpochInputs.capture(paths, "selected")
    runtime = RuntimeInputs.capture(paths)
    write_epoch(layout, "selected", scoring={"promote_margin": 0.75}, current=True)
    write_generation(layout, "selected", "v0")
    assert epoch.config.copy() is None
    assert epoch.scoring.copy() is None
    assert epoch.experiment("v0") is None
    assert runtime.epoch_id is None
    assert derive_liveness(paths, inputs=runtime) == {"state": "settled"}
    captured = EpochInputs.capture(paths, "selected")
    copy = captured.experiment("v0")
    assert copy is not None
    copy["generation_id"] = "v9"
    assert captured.experiment("v0")["generation_id"] == "v0"
    with pytest.raises(ValueError, match="different workspace or epoch"):
        captured.check(paths, "other")


@pytest.mark.parametrize("build", [build_environment, build_snapshot])
def test_response_keeps_absent_epoch_when_marker_appears_during_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, build: object
) -> None:
    layout = workspace(tmp_path)
    write_epoch(layout, "selected")
    original = Path.read_text
    reads = 0

    def create_after_missing_read(path: Path, *args: object, **kwargs: object) -> str:
        nonlocal reads
        if path == layout.current_epoch_marker:
            reads += 1
            try:
                return original(path, *args, **kwargs)
            except FileNotFoundError:
                path.write_text("selected")
                raise
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", create_after_missing_read)
    response = build(WorkspacePaths(layout.root))
    assert response["epoch_id"] is None
    if "epoch" in response:
        assert response["epoch"] == {"epoch_id": None}
    else:
        assert response["workspace"]["board_path"] is None
    assert reads == 1


def test_epoch_overview_uses_one_generation_observation_for_its_components(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = workspace(tmp_path)
    write_epoch(layout, "selected", scoring={"promote_margin": 0.25}, current=True)
    write_generation(layout, "selected", "v0")
    write_lineage(
        layout,
        {
            "epochs": [
                {
                    "id": "selected",
                    "generations": [{"id": "v0", "parent_id": None, "promoted": True}],
                }
            ]
        },
    )
    watched = {
        layout.experiment("selected", "v0"),
        layout.epoch_config("selected"),
        layout.scoring("selected"),
    }
    original = Path.read_text
    counts: Counter[Path] = Counter()

    def replace_after_read(path: Path, *args: object, **kwargs: object) -> str:
        text = original(path, *args, **kwargs)
        if path in watched:
            counts[path] += 1
            if path == layout.experiment("selected", "v0"):
                write_json(
                    path,
                    experiment_record(
                        "v0", epoch_id="selected", proposed_at="2030-01-01T00:00:00Z"
                    ),
                )
        return text

    monkeypatch.setattr(Path, "read_text", replace_after_read)
    response = build_epoch_view(WorkspacePaths(layout.root), "selected")
    assert response["champion_record"]["created_at"] == response["experiments"][0]["proposed_at"]
    assert response["champion_record"]["created_at"] != "2030-01-01T00:00:00Z"
    assert counts == Counter({path: 1 for path in watched})


def test_runtime_capture_shares_timestamp_fallback_with_liveness(tmp_path: Path) -> None:
    layout = workspace(tmp_path)
    write_heartbeat(layout.root, Heartbeat(123, "observed", "", "", phase="proposer"))
    response = build_environment(WorkspacePaths(layout.root))
    assert response["heartbeat"]["last_heartbeat"] == response["liveness"]["last_heartbeat"]
    assert response["liveness"]["state"] == "live"
    assert response["liveness"]["last_heartbeat"]


def test_mutation_count_uses_selected_epoch_without_changing_process_syntax(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = workspace(tmp_path)
    source = tmp_path / "source"
    source.mkdir()
    (source / "prompt.specimen").write_text('# zicato:mutable:file id="prompt"\nUseful prompt\n')
    adapter = {
        "kind": "import",
        "factory": "tests._stub_adapter:make_stub_adapter",
        "mutable_trees": [str(source)],
    }
    write_workspace_config(layout, {"adapter": adapter})
    write_epoch(
        layout,
        "declared",
        scoring={
            "mutation_surface": {".specimen": {"leaders": ["#"]}},
            "proposer_quality": {"best_of_n": 2},
        },
        current=True,
    )
    write_epoch(layout, "undeclared", scoring={})
    other = workspace(tmp_path / "other")
    write_workspace_config(other, {"adapter": adapter})
    write_epoch(other, "selected", scoring={}, current=True)
    paths = WorkspacePaths(layout.root)
    process_table = dict(active_syntax_table())
    from zicato.mutation import enumerator

    original = enumerator.enumerate_mutations
    ready = Barrier(2)

    def overlap(roots, **kwargs):
        ready.wait(timeout=5)
        return original(roots, **kwargs)

    monkeypatch.setattr(enumerator, "enumerate_mutations", overlap)
    with ThreadPoolExecutor(max_workers=2) as pool:
        declared = pool.submit(build_environment, paths)
        undeclared = pool.submit(build_environment, WorkspacePaths(other.root))
        assert declared.result(timeout=10)["workspace"]["mutation_point_count"] == 1
        assert undeclared.result(timeout=10)["workspace"]["mutation_point_count"] == 0
    monkeypatch.setattr(enumerator, "enumerate_mutations", original)
    layout.current_epoch_marker.write_text("undeclared")
    assert build_environment(paths)["workspace"]["mutation_point_count"] == 0
    assert dict(active_syntax_table()) == process_table
