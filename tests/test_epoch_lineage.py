"""Tests for :mod:`zicato.epoch.lineage`."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from zicato.core.types import Generation, ScoringWeights
from zicato.epoch import (
    append_to_lineage,
    load_lineage,
    new_epoch,
    render_lineage_summary,
)


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / ".zicato"
    ws.mkdir()
    return ws


@pytest.fixture()
def board_file(tmp_path: Path) -> Path:
    p = tmp_path / "board.jsonl"
    p.write_text(
        '{"id": "e1", "kind": "single_turn", ' '"wall_clock_budget_seconds": 60, "input": "hi"}\n'
    )
    return p


@pytest.fixture()
def rubric_file(tmp_path: Path) -> Path:
    p = tmp_path / "rubric.md"
    p.write_text("# Rubric\n")
    return p


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_new_epoch_registers_in_lineage(
    workspace: Path, board_file: Path, rubric_file: Path
) -> None:
    cfg = new_epoch(workspace, "alpha", board_file, rubric_file, ScoringWeights())
    data = load_lineage(workspace)
    assert "epochs" in data
    assert len(data["epochs"]) == 1
    entry = data["epochs"][0]
    assert entry["id"] == cfg.id
    assert entry["name"] == "alpha"
    assert entry["v0_parent"] is None
    assert entry["generations"] == []


def test_new_epoch_records_parent_epoch_id(
    workspace: Path, board_file: Path, rubric_file: Path
) -> None:
    a = new_epoch(workspace, "alpha", board_file, rubric_file, ScoringWeights())
    time.sleep(0.01)
    b = new_epoch(workspace, "beta", board_file, rubric_file, ScoringWeights())
    data = load_lineage(workspace)
    by_id = {e["id"]: e for e in data["epochs"]}
    assert by_id[a.id]["v0_parent"] is None
    assert by_id[b.id]["v0_parent"] == a.id


def test_append_to_lineage_builds_generations(
    workspace: Path, board_file: Path, rubric_file: Path
) -> None:
    cfg = new_epoch(workspace, "alpha", board_file, rubric_file, ScoringWeights())
    g0 = Generation(
        id="v0",
        epoch_id=cfg.id,
        parent_id=None,
        snapshot_root=workspace / "snap0",
        created_at="2026-04-08T10:00:00+00:00",
        promoted=True,
    )
    g1 = Generation(
        id="v1",
        epoch_id=cfg.id,
        parent_id="v0",
        snapshot_root=workspace / "snap1",
        created_at="2026-04-08T11:00:00+00:00",
        promoted=True,
    )
    g2 = Generation(
        id="v2",
        epoch_id=cfg.id,
        parent_id="v1",
        snapshot_root=workspace / "snap2",
        created_at="2026-04-08T12:00:00+00:00",
        promoted=False,
    )
    append_to_lineage(workspace, cfg.id, g0, None)
    append_to_lineage(workspace, cfg.id, g1, "v0")
    append_to_lineage(workspace, cfg.id, g2, "v1")

    data = load_lineage(workspace)
    [entry] = [e for e in data["epochs"] if e["id"] == cfg.id]
    gens = entry["generations"]
    assert [g["id"] for g in gens] == ["v0", "v1", "v2"]
    assert [g["parent_id"] for g in gens] == [None, "v0", "v1"]
    assert [g["promoted"] for g in gens] == [True, True, False]


def test_append_to_lineage_updates_existing_generation(
    workspace: Path, board_file: Path, rubric_file: Path
) -> None:
    cfg = new_epoch(workspace, "alpha", board_file, rubric_file, ScoringWeights())
    g = Generation(
        id="v1",
        epoch_id=cfg.id,
        parent_id="v0",
        snapshot_root=workspace / "snap",
        created_at="2026-04-08T11:00:00+00:00",
        promoted=False,
    )
    append_to_lineage(workspace, cfg.id, g, "v0")
    # Promote later — same id, different promoted bit.
    from dataclasses import replace

    append_to_lineage(workspace, cfg.id, replace(g, promoted=True), "v0")
    data = load_lineage(workspace)
    [entry] = [e for e in data["epochs"] if e["id"] == cfg.id]
    [recorded] = entry["generations"]
    assert recorded["promoted"] is True


def test_append_to_lineage_auto_creates_epoch_entry(workspace: Path) -> None:
    g = Generation(
        id="v0",
        epoch_id="2026-04-08_unregistered",
        parent_id=None,
        snapshot_root=workspace / "snap",
        created_at="2026-04-08T10:00:00+00:00",
        promoted=True,
    )
    append_to_lineage(workspace, "2026-04-08_unregistered", g, None)
    data = load_lineage(workspace)
    assert any(e["id"] == "2026-04-08_unregistered" for e in data["epochs"])


def test_load_lineage_empty(workspace: Path) -> None:
    data = load_lineage(workspace)
    assert data == {"epochs": []}


def test_render_lineage_summary_empty(workspace: Path) -> None:
    summary = render_lineage_summary(workspace)
    assert "no epochs recorded yet" in summary


def test_render_lineage_summary_populated(
    workspace: Path, board_file: Path, rubric_file: Path
) -> None:
    cfg_a = new_epoch(workspace, "alpha", board_file, rubric_file, ScoringWeights())
    g0 = Generation(
        id="v0",
        epoch_id=cfg_a.id,
        parent_id=None,
        snapshot_root=workspace / "s0",
        created_at="2026-04-08T10:00:00+00:00",
        promoted=True,
    )
    g1 = Generation(
        id="v1",
        epoch_id=cfg_a.id,
        parent_id="v0",
        snapshot_root=workspace / "s1",
        created_at="2026-04-08T11:00:00+00:00",
        promoted=True,
    )
    g2 = Generation(
        id="v2",
        epoch_id=cfg_a.id,
        parent_id="v1",
        snapshot_root=workspace / "s2",
        created_at="2026-04-08T12:00:00+00:00",
        promoted=False,
    )
    append_to_lineage(workspace, cfg_a.id, g0, None)
    append_to_lineage(workspace, cfg_a.id, g1, "v0")
    append_to_lineage(workspace, cfg_a.id, g2, "v1")

    summary = render_lineage_summary(workspace)
    assert summary.strip() != ""
    assert "# Lineage" in summary
    assert cfg_a.id in summary
    # Markdown table header.
    assert "| epoch |" in summary
    # Has the parent column showing (root).
    assert "(root)" in summary


def test_append_to_lineage_records_round_index(
    workspace: Path, board_file: Path, rubric_file: Path
) -> None:
    """A generation's birth round is persisted on its lineage row.

    The seed (``v0``) is round 0; challengers carry the round that
    minted them — here ``v1``/``v2`` minted in round 0, ``v3`` in round 1.
    """
    cfg = new_epoch(workspace, "alpha", board_file, rubric_file, ScoringWeights())
    seed = Generation(
        id="v0",
        epoch_id=cfg.id,
        parent_id=None,
        snapshot_root=workspace / "s0",
        created_at="2026-04-08T10:00:00+00:00",
        promoted=True,
    )  # round_index defaults to 0 — the genesis seed.
    c1 = Generation(
        id="v1",
        epoch_id=cfg.id,
        parent_id="v0",
        snapshot_root=workspace / "s1",
        created_at="2026-04-08T11:00:00+00:00",
        round_index=0,
    )
    c2 = Generation(
        id="v2",
        epoch_id=cfg.id,
        parent_id="v0",
        snapshot_root=workspace / "s2",
        created_at="2026-04-08T11:30:00+00:00",
        round_index=0,
    )
    c3 = Generation(
        id="v3",
        epoch_id=cfg.id,
        parent_id="v1",
        snapshot_root=workspace / "s3",
        created_at="2026-04-08T12:00:00+00:00",
        round_index=1,
        promoted=True,
    )
    for g, parent in ((seed, None), (c1, "v0"), (c2, "v0"), (c3, "v1")):
        append_to_lineage(workspace, cfg.id, g, parent)

    data = load_lineage(workspace)
    [entry] = [e for e in data["epochs"] if e["id"] == cfg.id]
    by_id = {g["id"]: g for g in entry["generations"]}
    assert by_id["v0"]["round_index"] == 0
    assert by_id["v1"]["round_index"] == 0
    assert by_id["v2"]["round_index"] == 0
    assert by_id["v3"]["round_index"] == 1


def test_append_to_lineage_round_index_survives_redefence(
    workspace: Path, board_file: Path, rubric_file: Path
) -> None:
    """Re-recording a generation keeps its original BIRTH round.

    A champion minted in round 1 that is re-appended later (a defence /
    promotion update) is never re-stamped with a later round.
    """
    from dataclasses import replace

    cfg = new_epoch(workspace, "alpha", board_file, rubric_file, ScoringWeights())
    g = Generation(
        id="v1",
        epoch_id=cfg.id,
        parent_id="v0",
        snapshot_root=workspace / "snap",
        created_at="2026-04-08T11:00:00+00:00",
        promoted=False,
        round_index=1,
    )
    append_to_lineage(workspace, cfg.id, g, "v0")
    # Re-record on promotion; the in-place update must keep round_index=1.
    append_to_lineage(workspace, cfg.id, replace(g, promoted=True), "v0")
    data = load_lineage(workspace)
    [entry] = [e for e in data["epochs"] if e["id"] == cfg.id]
    [recorded] = entry["generations"]
    assert recorded["promoted"] is True
    assert recorded["round_index"] == 1


def test_close_marks_lineage_closed_at(
    workspace: Path, board_file: Path, rubric_file: Path
) -> None:
    from zicato.epoch import close_epoch

    cfg = new_epoch(workspace, "alpha", board_file, rubric_file, ScoringWeights())
    close_epoch(workspace, cfg.id, aux_call_llm=None)
    data = load_lineage(workspace)
    [entry] = [e for e in data["epochs"] if e["id"] == cfg.id]
    assert entry["closed_at"] != ""
