"""Canonical records require explicit supported integer format stamps."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from zicato.core.types import Experiment, HypothesisSpec
from zicato.epoch._storage import RECORD_FORMAT_VERSION, RecordFormatError
from zicato.epoch.journal import read_experiment, write_experiment
from zicato.epoch.lifecycle import _scoring_from_dict, load_epoch, new_epoch
from zicato.epoch.lineage import append_to_lineage, load_lineage

# ---------------------------------------------------------------------------
# Canonical JSON records: format_version stamp + refusal
# ---------------------------------------------------------------------------


def _experiment(epoch_id: str, generation_id: str) -> Experiment:
    return Experiment(
        id=f"exp_{epoch_id}_{generation_id}",
        epoch_id=epoch_id,
        generation_id=generation_id,
        parent_generation_id="v0",
        proposed_at="2026-07-01T00:00:00+00:00",
        hypothesis=HypothesisSpec(
            core_idea="idea",
            modulating=("m1",),
            why="why",
            expected_metric_movements=(),
            expected_pass_rate_delta="0.0",
            risks="",
        ),
        patches=(),
        outcome=None,
    )


class TestExperimentFormatVersion:
    def test_write_stamps_and_read_roundtrips(self, tmp_path: Path) -> None:
        write_experiment(tmp_path, "e1", "v1", _experiment("e1", "v1"))
        exp_path = tmp_path / "epochs" / "e1" / "generations" / "v1" / "experiment.json"
        body = json.loads(exp_path.read_text())
        assert body["format_version"] == RECORD_FORMAT_VERSION == 1
        loaded = read_experiment(tmp_path, "e1", "v1")
        assert loaded.generation_id == "v1"

    def test_absent_version_is_refused(self, tmp_path: Path) -> None:
        write_experiment(tmp_path, "e1", "v1", _experiment("e1", "v1"))
        exp_path = tmp_path / "epochs" / "e1" / "generations" / "v1" / "experiment.json"
        body = json.loads(exp_path.read_text())
        del body["format_version"]
        exp_path.write_text(json.dumps(body))
        with pytest.raises(RecordFormatError):
            read_experiment(tmp_path, "e1", "v1")

    def test_future_version_refuses_with_clear_error(self, tmp_path: Path) -> None:
        write_experiment(tmp_path, "e1", "v1", _experiment("e1", "v1"))
        exp_path = tmp_path / "epochs" / "e1" / "generations" / "v1" / "experiment.json"
        body = json.loads(exp_path.read_text())
        body["format_version"] = 2
        exp_path.write_text(json.dumps(body))
        with pytest.raises(RecordFormatError) as excinfo:
            read_experiment(tmp_path, "e1", "v1")
        assert "experiment.json" in str(excinfo.value)
        assert "format_version 2" in str(excinfo.value)


def _bootstrap_epoch(tmp_path: Path) -> tuple[Path, str]:
    workspace = tmp_path / ".zicato"
    workspace.mkdir()
    board = tmp_path / "board.jsonl"
    board.write_text(
        json.dumps(
            {
                "id": "e_a",
                "kind": "single_turn",
                "wall_clock_budget_seconds": 60,
                "input": "hi",
            }
        )
        + "\n"
    )
    brief = tmp_path / "brief.md"
    brief.write_text("# brief\n")
    cfg = new_epoch(
        workspace,
        name="fv",
        board_source=board,
        brief_source=brief,
        weights=_scoring_from_dict({}),
        auto_close_previous=False,
    )
    return workspace, cfg.id


class TestEpochConfigFormatVersion:
    def test_write_stamps_and_read_roundtrips(self, tmp_path: Path) -> None:
        workspace, epoch_id = _bootstrap_epoch(tmp_path)
        config_path = workspace / "epochs" / epoch_id / "config.json"
        body = json.loads(config_path.read_text())
        assert body["format_version"] == 1
        assert load_epoch(workspace, epoch_id).id == epoch_id

    def test_absent_version_is_refused(self, tmp_path: Path) -> None:
        workspace, epoch_id = _bootstrap_epoch(tmp_path)
        config_path = workspace / "epochs" / epoch_id / "config.json"
        body = json.loads(config_path.read_text())
        del body["format_version"]
        config_path.write_text(json.dumps(body))
        with pytest.raises(RecordFormatError):
            load_epoch(workspace, epoch_id)

    def test_future_version_refuses_with_clear_error(self, tmp_path: Path) -> None:
        workspace, epoch_id = _bootstrap_epoch(tmp_path)
        config_path = workspace / "epochs" / epoch_id / "config.json"
        body = json.loads(config_path.read_text())
        body["format_version"] = 99
        config_path.write_text(json.dumps(body))
        with pytest.raises(RecordFormatError) as excinfo:
            load_epoch(workspace, epoch_id)
        assert "config.json" in str(excinfo.value)


class TestLineageFormatVersion:
    def test_save_stamps_and_load_roundtrips(self, tmp_path: Path) -> None:
        from zicato.core.types import Generation

        gen = Generation(
            id="v0",
            epoch_id="e1",
            parent_id=None,
            snapshot_root=tmp_path / "snap",
            created_at="2026-07-01T00:00:00+00:00",
            promoted=True,
        )
        append_to_lineage(tmp_path, "e1", gen, None)
        raw = json.loads((tmp_path / "lineage.json").read_text())
        assert raw["format_version"] == 1
        data = load_lineage(tmp_path).to_dict()
        assert data["epochs"][0]["id"] == "e1"

    def test_absent_version_is_refused(self, tmp_path: Path) -> None:
        (tmp_path / "lineage.json").write_text(json.dumps({"epochs": []}))
        with pytest.raises(RecordFormatError):
            load_lineage(tmp_path)

    @pytest.mark.parametrize("version", [None, True, 1.0, "1"])
    def test_noninteger_version_is_refused(self, tmp_path: Path, version: object) -> None:
        path = tmp_path / "lineage.json"
        path.write_text(json.dumps({"format_version": version, "epochs": []}))
        with pytest.raises(RecordFormatError, match="expected integer 1"):
            load_lineage(tmp_path)

    def test_future_version_refuses_loudly_not_empty(self, tmp_path: Path) -> None:
        # An INTACT record from a newer zicato must refuse — collapsing to
        # the empty DAG would silently drop history.
        (tmp_path / "lineage.json").write_text(
            json.dumps({"format_version": 3, "epochs": [{"id": "e1", "generations": []}]})
        )
        with pytest.raises(RecordFormatError) as excinfo:
            load_lineage(tmp_path).to_dict()
        assert "lineage.json" in str(excinfo.value)
