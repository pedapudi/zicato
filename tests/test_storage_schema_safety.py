"""Storage schema-safety guards (WS5 items 4-6).

* the analytical index REFUSES to open a database stamped with a NEWER
  ``user_version`` than this build's ``SCHEMA_VERSION`` (an older writer
  must never silently re-stamp a newer database down);
* the canonical JSON records (``experiment.json``, epoch ``config.json``,
  ``lineage.json``) are stamped ``format_version: 1`` at write, treat an
  ABSENT stamp as version 1 at read (pre-stamp workspaces/fixtures keep
  loading), and refuse a FUTURE incompatible version with a clear error —
  no shape-sniffing migration shims.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from zicato.core.types import Experiment, HypothesisSpec
from zicato.epoch._storage import RECORD_FORMAT_VERSION, RecordFormatError
from zicato.epoch.journal import read_experiment, write_experiment
from zicato.epoch.lifecycle import _scoring_from_dict, load_epoch, new_epoch
from zicato.epoch.lineage import append_to_lineage, load_lineage
from zicato.index.schema import (
    SCHEMA_VERSION,
    IndexSchemaNewerError,
    apply_schema,
    read_schema_version,
)

# ---------------------------------------------------------------------------
# Index: refuse-on-newer user_version
# ---------------------------------------------------------------------------


class TestIndexRefuseOnNewer:
    def test_apply_schema_refuses_newer_database(self, tmp_path: Path) -> None:
        db = tmp_path / "index.db"
        conn = sqlite3.connect(str(db))
        try:
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
            with pytest.raises(IndexSchemaNewerError) as excinfo:
                apply_schema(conn)
            message = str(excinfo.value)
            assert f"v{SCHEMA_VERSION + 1}" in message
            assert f"v{SCHEMA_VERSION}" in message
            assert "reindex" in message
            # The newer stamp was NOT overwritten down.
            assert read_schema_version(conn) == SCHEMA_VERSION + 1
        finally:
            conn.close()

    def test_apply_schema_still_carries_older_forward(self, tmp_path: Path) -> None:
        db = tmp_path / "index.db"
        conn = sqlite3.connect(str(db))
        try:
            conn.execute("PRAGMA user_version = 1")
            apply_schema(conn)
            assert read_schema_version(conn) == SCHEMA_VERSION
        finally:
            conn.close()

    def test_rebuild_index_replaces_a_newer_database(self, tmp_path: Path) -> None:
        # The canonical rebuild path deletes the file first, so a newer
        # database is REPLACED (rebuild loses nothing — the index is
        # derived), never downgrade-stamped in place.
        from zicato.index.ingest import rebuild_index

        workspace = tmp_path / ".zicato"
        workspace.mkdir()
        db = workspace / "index.db"
        conn = sqlite3.connect(str(db))
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 7}")
        conn.commit()
        conn.close()
        target = rebuild_index(workspace)
        conn = sqlite3.connect(str(target))
        try:
            assert read_schema_version(conn) == SCHEMA_VERSION
        finally:
            conn.close()


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
            expected_drift_movements=(),
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

    def test_absent_version_reads_as_v1(self, tmp_path: Path) -> None:
        write_experiment(tmp_path, "e1", "v1", _experiment("e1", "v1"))
        exp_path = tmp_path / "epochs" / "e1" / "generations" / "v1" / "experiment.json"
        body = json.loads(exp_path.read_text())
        del body["format_version"]  # a pre-stamp record
        exp_path.write_text(json.dumps(body))
        assert read_experiment(tmp_path, "e1", "v1").generation_id == "v1"

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

    def test_absent_version_reads_as_v1(self, tmp_path: Path) -> None:
        workspace, epoch_id = _bootstrap_epoch(tmp_path)
        config_path = workspace / "epochs" / epoch_id / "config.json"
        body = json.loads(config_path.read_text())
        del body["format_version"]
        config_path.write_text(json.dumps(body))
        assert load_epoch(workspace, epoch_id).id == epoch_id

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
        data = load_lineage(tmp_path)
        assert data["epochs"][0]["id"] == "e1"

    def test_absent_version_reads_as_v1(self, tmp_path: Path) -> None:
        (tmp_path / "lineage.json").write_text(json.dumps({"epochs": []}))
        assert load_lineage(tmp_path)["epochs"] == []

    def test_future_version_refuses_loudly_not_empty(self, tmp_path: Path) -> None:
        # An INTACT record from a newer zicato must refuse — collapsing to
        # the empty DAG would silently drop history.
        (tmp_path / "lineage.json").write_text(
            json.dumps({"format_version": 3, "epochs": [{"id": "e1", "generations": []}]})
        )
        with pytest.raises(RecordFormatError) as excinfo:
            load_lineage(tmp_path)
        assert "lineage.json" in str(excinfo.value)
