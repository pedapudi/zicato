"""Tournament run provenance and cross-epoch ancestry in the derived index."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from zicato.core.types import Generation, ScoringWeights
from zicato.core.workspace import loss_profile_path
from zicato.epoch.journal import write_experiment
from zicato.epoch.lifecycle import new_epoch
from zicato.epoch.lineage import append_to_lineage, register_epoch
from zicato.index.ingest import (
    ingest_run,
    rebuild_index,
)
from zicato.index.query import (
    all_epochs,
    epoch_ancestry,
    loss_profiles_for_tournament,
    runs_for_tournament,
)
from zicato.index.schema import (
    apply_schema,
)
from zicato.telemetry.reducer import write_loss_profile
from zicato.testing.fixtures import (
    make_experiment,
    make_loss_profile,
    make_outcome_record,
    make_patch,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {r[1] for r in rows}


def _build_two_epoch_workspace(tmp_path: Path) -> tuple[Path, str, str]:
    """Build a workspace with two epochs in lineage, v0/v1 chains in each.

    Returns ``(workspace_root, first_epoch_id, second_epoch_id)``. The
    second epoch's ``v0_parent`` points at the first epoch — that
    cross-epoch edge is what ``epoch_ancestry`` walks.

    Each epoch carries a ``v1`` whose experiment.json challenges its
    ``v0``, with a resolved ``promoted`` outcome and one synthetic
    run+loss profile per board entry.
    """
    ws = tmp_path / ".zicato"
    board = tmp_path / "board.jsonl"
    board.write_text(
        '{"id": "e1", "kind": "single_turn", ' '"wall_clock_budget_seconds": 60, "input": "hi"}\n',
        encoding="utf-8",
    )
    rubric = tmp_path / "rubric.md"
    rubric.write_text("# rubric\n", encoding="utf-8")

    # First epoch — register without a parent.
    cfg_a = new_epoch(ws, "alpha", board, rubric, ScoringWeights())
    eid_a = cfg_a.id
    _seed_chain(ws, eid_a)

    # Second epoch — register with the first as v0_parent so the
    # cross-epoch ancestry edge is exercised.
    cfg_b = new_epoch(ws, "beta", board, rubric, ScoringWeights())
    eid_b = cfg_b.id
    # ``new_epoch`` auto-closes the previous epoch; explicitly stamp the
    # v0_parent so the lineage entry reflects the fork.
    register_epoch(ws, cfg_b, parent_epoch_id=eid_a)
    _seed_chain(ws, eid_b)

    return ws, eid_a, eid_b


def _seed_chain(ws: Path, epoch_id: str) -> None:
    """Seed a v0 + v1 chain under ``epoch_id`` with one promoted experiment.

    Writes ``experiment.json`` + a run / loss profile on ``v1`` so the
    tournament round and its runs are both materialised on disk.
    """
    g0 = Generation(
        id="v0",
        epoch_id=epoch_id,
        parent_id=None,
        snapshot_root=Path("/tmp/snap/v0"),
        created_at="2026-01-01T00:00:00Z",
        promoted=True,
    )
    g1 = Generation(
        id="v1",
        epoch_id=epoch_id,
        parent_id="v0",
        snapshot_root=Path("/tmp/snap/v1"),
        created_at="2026-01-02T00:00:00Z",
        promoted=True,
    )
    append_to_lineage(ws, epoch_id, g0, None)
    append_to_lineage(ws, epoch_id, g1, "v0")

    exp = make_experiment(
        epoch_id=epoch_id,
        generation_id="v1",
        parent_generation_id="v0",
        patches=(make_patch(id=f"patch_{epoch_id}_v1"),),
        outcome=make_outcome_record(),
    )
    write_experiment(ws, epoch_id, "v1", exp)

    profile = make_loss_profile(
        run_id=f"run_{epoch_id}_v1_e1",
        entry_id="e1",
        generation_id="v1",
        epoch_id=epoch_id,
    )
    write_loss_profile(profile, loss_profile_path(ws, epoch_id, "v1", "e1"))


# ---------------------------------------------------------------------------
# Tournament and ancestry lookup indexes
# ---------------------------------------------------------------------------


def test_tournament_and_ancestry_indexes_exist(tmp_path: Path) -> None:
    """Tournament and ancestry lookup columns have supporting indexes."""
    db = tmp_path / "fresh.db"
    conn = sqlite3.connect(str(db))
    try:
        apply_schema(conn)
        names = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'").fetchall()
        }
        assert {"idx_runs_tournament", "idx_loss_tournament", "idx_epochs_parent"}.issubset(names)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Ingest populates the new columns
# ---------------------------------------------------------------------------


def test_rebuild_index_populates_tournament_id_on_runs(tmp_path: Path) -> None:
    """The full-rebuild path stamps tournament_id on every run with an experiment."""
    ws, eid_a, _ = _build_two_epoch_workspace(tmp_path)
    db = rebuild_index(ws)
    tournament_id = f"{eid_a}:v0->v1"
    runs = runs_for_tournament(db, tournament_id)
    assert len(runs) == 1
    assert runs[0]["run_id"] == f"run_{eid_a}_v1_e1"
    assert runs[0]["tournament_id"] == tournament_id


def test_rebuild_index_populates_tournament_id_on_loss_profiles(tmp_path: Path) -> None:
    """The loss-profiles parallel column lands the same id as runs."""
    ws, eid_a, _ = _build_two_epoch_workspace(tmp_path)
    db = rebuild_index(ws)
    tournament_id = f"{eid_a}:v0->v1"
    losses = loss_profiles_for_tournament(db, tournament_id)
    assert len(losses) == 1
    assert losses[0]["run_id"] == f"run_{eid_a}_v1_e1"
    assert losses[0]["tournament_id"] == tournament_id


def test_rebuild_index_populates_parent_epoch_id_from_lineage(tmp_path: Path) -> None:
    """``v0_parent`` in ``lineage.json`` becomes ``epochs.parent_epoch_id``."""
    ws, eid_a, eid_b = _build_two_epoch_workspace(tmp_path)
    db = rebuild_index(ws)
    by_id = {r["epoch_id"]: r["parent_epoch_id"] for r in all_epochs(db)}
    # First epoch has no parent.
    assert by_id[eid_a] is None
    # Second epoch was registered with the first as v0_parent.
    assert by_id[eid_b] == eid_a


def test_ingest_run_populates_tournament_id(tmp_path: Path) -> None:
    """The live dual-write (ingest_run) also stamps the tournament FK."""
    ws, eid_a, _ = _build_two_epoch_workspace(tmp_path)
    db = ws / "index.db"
    assert not db.exists()
    ingest_run(ws, None, eid_a, "v1", "e1")
    runs = runs_for_tournament(db, f"{eid_a}:v0->v1")
    assert len(runs) == 1
    assert runs[0]["tournament_id"] == f"{eid_a}:v0->v1"


def test_v0_seed_run_has_null_tournament_id(tmp_path: Path) -> None:
    """A run under a generation with no experiment.json has tournament_id NULL.

    The ``v0`` seed in each epoch has no experiment.json — it is a
    champion-only fast-cache run with no tournament round attached.
    The ingest path correctly leaves the column NULL rather than
    inventing a stub tournament id.
    """
    ws, eid_a, _ = _build_two_epoch_workspace(tmp_path)
    # Add a v0 loss profile so a run exists.
    profile = make_loss_profile(
        run_id=f"run_{eid_a}_v0_e1",
        entry_id="e1",
        generation_id="v0",
        epoch_id=eid_a,
    )
    write_loss_profile(profile, loss_profile_path(ws, eid_a, "v0", "e1"))
    db = rebuild_index(ws)
    conn = sqlite3.connect(str(db))
    try:
        row = conn.execute(
            "SELECT tournament_id FROM runs WHERE run_id = ?",
            (f"run_{eid_a}_v0_e1",),
        ).fetchone()
    finally:
        conn.close()
    assert row[0] is None


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------


def test_runs_for_tournament_returns_only_matching_runs(tmp_path: Path) -> None:
    """``runs_for_tournament`` is scoped strictly to one tournament id."""
    ws, eid_a, eid_b = _build_two_epoch_workspace(tmp_path)
    db = rebuild_index(ws)
    a_runs = runs_for_tournament(db, f"{eid_a}:v0->v1")
    b_runs = runs_for_tournament(db, f"{eid_b}:v0->v1")
    assert {r["run_id"] for r in a_runs} == {f"run_{eid_a}_v1_e1"}
    assert {r["run_id"] for r in b_runs} == {f"run_{eid_b}_v1_e1"}


def test_runs_for_tournament_empty_for_unknown_id(tmp_path: Path) -> None:
    """An unknown tournament id collapses to the empty list, not an error."""
    ws, _, _ = _build_two_epoch_workspace(tmp_path)
    db = rebuild_index(ws)
    assert runs_for_tournament(db, "no_such_epoch:v0->v1") == []


def test_epoch_ancestry_walks_back_to_root(tmp_path: Path) -> None:
    """``epoch_ancestry`` yields the chain newest-first, root last."""
    ws, eid_a, eid_b = _build_two_epoch_workspace(tmp_path)
    db = rebuild_index(ws)
    chain = epoch_ancestry(db, eid_b)
    ids = [r["epoch_id"] for r in chain]
    assert ids == [eid_b, eid_a]
    # The root row has parent_epoch_id NULL.
    assert chain[-1]["parent_epoch_id"] is None


def test_epoch_ancestry_root_returns_single_row(tmp_path: Path) -> None:
    """The workspace's first epoch yields a one-row chain (itself)."""
    ws, eid_a, _ = _build_two_epoch_workspace(tmp_path)
    db = rebuild_index(ws)
    chain = epoch_ancestry(db, eid_a)
    ids = [r["epoch_id"] for r in chain]
    assert ids == [eid_a]
    assert chain[0]["parent_epoch_id"] is None


def test_epoch_ancestry_unknown_epoch_returns_empty(tmp_path: Path) -> None:
    """An unknown epoch id collapses to the empty list."""
    ws, _, _ = _build_two_epoch_workspace(tmp_path)
    db = rebuild_index(ws)
    assert epoch_ancestry(db, "does_not_exist") == []


def test_epoch_ancestry_handles_missing_index(tmp_path: Path) -> None:
    """``epoch_ancestry`` against a never-indexed workspace returns ``[]``."""
    # No rebuild_index call; the file does not exist.
    assert epoch_ancestry(tmp_path / "no_such.db", "anything") == []


# ---------------------------------------------------------------------------
# Repair command
# ---------------------------------------------------------------------------


def test_incremental_ingest_carries_tournament_id_after_reingest(tmp_path: Path) -> None:
    """Re-running ingest_run keeps the tournament_id stable (idempotent)."""
    ws, eid_a, _ = _build_two_epoch_workspace(tmp_path)
    ingest_run(ws, None, eid_a, "v1", "e1")
    ingest_run(ws, None, eid_a, "v1", "e1")
    db = ws / "index.db"
    runs = runs_for_tournament(db, f"{eid_a}:v0->v1")
    assert len(runs) == 1
    assert runs[0]["tournament_id"] == f"{eid_a}:v0->v1"


@pytest.mark.parametrize(
    "table,column",
    [
        ("runs", "tournament_id"),
        ("loss_profiles", "tournament_id"),
        ("epochs", "parent_epoch_id"),
    ],
)
def test_optional_lineage_coordinates_are_nullable(tmp_path: Path, table: str, column: str) -> None:
    """All three v2 columns are NULL-able by contract.

    Old rows + champion-only fast-cache runs + first-in-workspace epochs
    have no value for the new columns; the schema must permit NULL.
    """
    db = tmp_path / "fresh.db"
    conn = sqlite3.connect(str(db))
    try:
        apply_schema(conn)
        info = conn.execute(f"PRAGMA table_info({table})").fetchall()
        notnull = {r[1]: r[3] for r in info}  # r[3] is the NOT NULL flag
        assert notnull[column] == 0, f"{table}.{column} should be NULL-able"
    finally:
        conn.close()
