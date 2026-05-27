"""Tests for the index read helpers (:mod:`zicato.index.query`) and ``zicato reindex``."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from click.testing import CliRunner

from zicato.cli.commands.reindex import reindex_cmd
from zicato.core.types import Generation, ScoringWeights
from zicato.core.workspace import events_jsonl_path, loss_profile_path
from zicato.epoch.journal import write_experiment
from zicato.epoch.lifecycle import new_epoch
from zicato.epoch.lineage import append_to_lineage
from zicato.index.ingest import rebuild_index
from zicato.index.query import (
    IndexNotBuiltError,
    all_epochs,
    experiments_for_epoch,
    generations_for_epoch,
    index_counts,
    index_schema_version,
    loss_profiles_for_generation,
    metric_counts_for_run,
    open_index,
    runs_for_generation,
    tournaments_for_epoch,
)
from zicato.index.schema import SCHEMA_VERSION
from zicato.telemetry.reducer import write_loss_profile
from zicato.testing.fixtures import (
    make_experiment,
    make_loss_profile,
    make_outcome_record,
    make_synthetic_events_jsonl,
)


def _build_indexed_workspace(tmp_path: Path) -> tuple[Path, Path, str]:
    """Build + index a small workspace; return ``(workspace, db_path, epoch_id)``."""
    ws = tmp_path / ".zicato"
    board = tmp_path / "board.jsonl"
    board.write_text(
        '{"id": "e1", "kind": "single_turn", "wall_clock_budget_seconds": 60, "input": "hi"}\n',
        encoding="utf-8",
    )
    rubric = tmp_path / "rubric.md"
    rubric.write_text("# rubric\n", encoding="utf-8")
    cfg = new_epoch(ws, "alpha", board, rubric, ScoringWeights())
    eid = cfg.id

    g0 = Generation(
        id="v0",
        epoch_id=eid,
        parent_id=None,
        snapshot_root=Path("/tmp/snap/v0"),
        created_at="2026-01-01T00:00:00Z",
        promoted=True,
    )
    g1 = Generation(
        id="v1",
        epoch_id=eid,
        parent_id="v0",
        snapshot_root=Path("/tmp/snap/v1"),
        created_at="2026-01-02T00:00:00Z",
        promoted=True,
    )
    append_to_lineage(ws, eid, g0, None)
    append_to_lineage(ws, eid, g1, "v0")

    exp = make_experiment(
        epoch_id=eid,
        generation_id="v1",
        parent_generation_id="v0",
        outcome=make_outcome_record(),
    )
    write_experiment(ws, eid, "v1", exp)

    for gid in ("v0", "v1"):
        profile = make_loss_profile(
            run_id=f"run_{gid}_e1",
            entry_id="e1",
            generation_id=gid,
            epoch_id=eid,
            drift_loss=1.5,
        )
        write_loss_profile(profile, loss_profile_path(ws, eid, gid, "e1"))
        make_synthetic_events_jsonl(
            events_jsonl_path(ws, eid, gid, "e1"),
            drift_events=[("off_topic", "info")],
        )

    db = rebuild_index(ws)
    return ws, db, eid


# ---------------------------------------------------------------------------
# open_index
# ---------------------------------------------------------------------------


def test_open_index_returns_row_factory_connection(tmp_path: Path) -> None:
    _, db, _ = _build_indexed_workspace(tmp_path)
    conn = open_index(db)
    try:
        assert isinstance(conn, sqlite3.Connection)
        row = conn.execute("SELECT epoch_id FROM epochs LIMIT 1").fetchone()
        # row_factory = sqlite3.Row -> name-addressable.
        assert row["epoch_id"]
    finally:
        conn.close()


def test_open_index_uses_wal_mode(tmp_path: Path) -> None:
    _, db, _ = _build_indexed_workspace(tmp_path)
    conn = open_index(db)
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"
    finally:
        conn.close()


def test_open_index_missing_db_raises_clear_error(tmp_path: Path) -> None:
    missing = tmp_path / "nope.db"
    with pytest.raises(IndexNotBuiltError) as exc:
        open_index(missing)
    # The error message must point the operator at the fix.
    assert "zicato reindex" in str(exc.value)


def test_index_not_built_error_is_filenotfound_subclass() -> None:
    # So existing ``except FileNotFoundError`` handlers keep working.
    assert issubclass(IndexNotBuiltError, FileNotFoundError)


def test_index_schema_version_reports_current(tmp_path: Path) -> None:
    _, db, _ = _build_indexed_workspace(tmp_path)
    assert index_schema_version(db) == SCHEMA_VERSION


def test_index_schema_version_none_when_missing(tmp_path: Path) -> None:
    assert index_schema_version(tmp_path / "absent.db") is None


# ---------------------------------------------------------------------------
# Selectors
# ---------------------------------------------------------------------------


def test_all_epochs_returns_rows(tmp_path: Path) -> None:
    _, db, eid = _build_indexed_workspace(tmp_path)
    epochs = all_epochs(db)
    assert len(epochs) == 1
    assert epochs[0]["epoch_id"] == eid


def test_generations_for_epoch_returns_rows(tmp_path: Path) -> None:
    _, db, eid = _build_indexed_workspace(tmp_path)
    gens = generations_for_epoch(db, eid)
    assert [g["generation_id"] for g in gens] == ["v0", "v1"]
    assert gens[1]["parent_generation_id"] == "v0"


def test_runs_for_generation_returns_rows(tmp_path: Path) -> None:
    _, db, eid = _build_indexed_workspace(tmp_path)
    runs = runs_for_generation(db, eid, "v1")
    assert len(runs) == 1
    assert runs[0]["run_id"] == "run_v1_e1"


def test_loss_profiles_for_generation_returns_rows(tmp_path: Path) -> None:
    _, db, eid = _build_indexed_workspace(tmp_path)
    profiles = loss_profiles_for_generation(db, eid, "v0")
    assert len(profiles) == 1
    assert profiles[0]["drift_loss"] == pytest.approx(1.5)


def test_metric_counts_for_run_returns_rows(tmp_path: Path) -> None:
    _, db, _ = _build_indexed_workspace(tmp_path)
    metrics = metric_counts_for_run(db, "run_v0_e1")
    drift = [m for m in metrics if m["namespace"] == "drift"]
    assert len(drift) == 1
    assert drift[0]["name"] == "drift:off_topic"
    assert drift[0]["severity"] == "info"


def test_experiments_for_epoch_returns_rows(tmp_path: Path) -> None:
    _, db, eid = _build_indexed_workspace(tmp_path)
    exps = experiments_for_epoch(db, eid)
    assert len(exps) == 1
    assert exps[0]["generation_id"] == "v1"


def test_tournaments_for_epoch_returns_rows(tmp_path: Path) -> None:
    _, db, eid = _build_indexed_workspace(tmp_path)
    tournaments = tournaments_for_epoch(db, eid)
    assert len(tournaments) == 1
    assert tournaments[0]["decision"] == "promoted"


def test_selectors_tolerate_missing_db(tmp_path: Path) -> None:
    # Every selector returns an empty list rather than raising when the
    # index has never been built.
    absent = tmp_path / "absent.db"
    assert all_epochs(absent) == []
    assert generations_for_epoch(absent, "e") == []
    assert runs_for_generation(absent, "e", "v0") == []
    assert loss_profiles_for_generation(absent, "e", "v0") == []
    assert metric_counts_for_run(absent, "r") == []
    assert experiments_for_epoch(absent, "e") == []
    assert tournaments_for_epoch(absent, "e") == []


def test_index_counts_tolerates_missing_db(tmp_path: Path) -> None:
    counts = index_counts(tmp_path / "absent.db")
    assert counts["epochs"] == 0
    assert counts["runs"] == 0
    assert set(counts) == {
        "epochs",
        "generations",
        "experiments",
        "patches",
        "runs",
        "loss_profiles",
        "metric_counts",
        "tournaments",
        "judge_losses",
    }


# ---------------------------------------------------------------------------
# zicato reindex CLI
# ---------------------------------------------------------------------------


def test_reindex_cli_smoke(tmp_path: Path) -> None:
    ws, _, _ = _build_indexed_workspace(tmp_path)
    runner = CliRunner()
    result = runner.invoke(reindex_cmd, ["--workspace", str(ws)])
    assert result.exit_code == 0, result.output
    assert "Rebuilt index" in result.output
    # The summary reports the indexed counts.
    assert "1 epochs" in result.output
    assert "2 generations" in result.output
    assert "2 runs" in result.output


def test_reindex_cli_creates_db(tmp_path: Path) -> None:
    ws = tmp_path / ".zicato"
    ws.mkdir()
    runner = CliRunner()
    result = runner.invoke(reindex_cmd, ["--workspace", str(ws)])
    assert result.exit_code == 0, result.output
    assert (ws / "index.db").exists()


def test_reindex_cli_registered_on_root() -> None:
    # The auto-discovered CLI root must surface the reindex command.
    from zicato.cli.discovery import build_cli_root

    root = build_cli_root()
    assert "reindex" in root.commands
