"""Tests for the synthetic v0 ``experiment.json`` marker.

Coverage:

* :func:`zicato.epoch.journal.write_seed_experiment` writes a minimal
  experiment for v0 with ``parent_generation_id=None`` (the seed has no
  in-epoch parent), ``outcome=None``, and is idempotent on a second call.
  A legacy on-disk ``""`` still reads back as ``None``.
* :func:`zicato.evolve.round_baseline._ensure_baseline_snapshot` invokes the
  seed writer so a freshly-materialised v0 carries the marker.
* :func:`zicato.analyzer.report_data.gather_epoch_report_data` returns
  v0 in its generations tuple when the marker is present;
  :func:`zicato.analyzer.report_sections._promoted_lineage` walks
  ``v0 -> v1`` and the per-board outcomes table renders non-empty
  content (the bug-reproduction case before the fix).
* The ``zicato repair v0-baseline`` CLI subcommand backfills a
  pre-existing workspace whose v0 directory has no marker, and is a
  no-op on re-run.
"""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from zicato.analyzer.report_data import gather_epoch_report_data
from zicato.analyzer.report_sections import (
    _promoted_lineage,
    render_per_board_outcomes,
)
from zicato.cli.commands.repair_v0_baseline import repair_v0_baseline_cmd
from zicato.epoch.journal import write_seed_experiment

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _bootstrap_workspace_without_v0_marker(tmp_path: Path) -> tuple[Path, str]:
    """Create a workspace whose v0 directory exists but has no experiment.json.

    Mirrors the on-disk shape of a workspace that predates the v0
    bootstrap write (the t6 reproduction): the v0 generation has its
    snapshot dir and a gen_score.json, but no experiment.json. v1 is a
    promoted challenger; v2 is a rejected challenger.
    """
    ws = tmp_path / ".zicato"
    epoch = "2026-05-20_demo"
    edir = ws / "epochs" / epoch
    edir.mkdir(parents=True)

    _write_json(
        edir / "config.json",
        {
            "id": epoch,
            "name": "Demo",
            "created_at": "2026-05-20T00:00:00Z",
            "contract_hash": "feedface00000001",
            "closed": False,
        },
    )
    (edir / "brief.md").write_text("baseline brief", encoding="utf-8")
    (edir / "board.jsonl").write_text(
        '{"id": "qa", "kind": "single_turn", "wall_clock_budget_seconds": 30, '
        '"input": "answer", "expectation": {"kind": "rubric", "spec": "accurate"}}\n',
        encoding="utf-8",
    )
    _write_json(edir / "scoring.json", {"pass_weight": 1.0})

    # v0 — seed, no experiment.json (the bug-reproduction shape).
    v0_dir = edir / "generations" / "v0"
    (v0_dir / "snapshot").mkdir(parents=True)
    (v0_dir / "snapshot" / "agent.py").write_text("# seed\n", encoding="utf-8")
    _write_json(
        v0_dir / "gen_score.json",
        {"generation_id": "v0", "scalar": 1.000, "drift_loss_mean": 0.500, "pass_rate": 0.80},
    )

    # v1 — promoted challenger.
    _write_json(
        edir / "generations" / "v1" / "experiment.json",
        {
            "id": "exp-v1",
            "epoch_id": epoch,
            "generation_id": "v1",
            "parent_generation_id": "v0",
            "proposed_at": "2026-05-20T01:00:00Z",
            "hypothesis": {
                "core_idea": "tighten the prompt",
                "modulating": [],
                "why": "",
                "expected_pass_rate_delta": "+0.05",
            },
            "patch_ids": [],
            "outcome": {
                "ran_at": "2026-05-20T01:30:00Z",
                "drift_movements": [],
                "pass_rate_delta": 0.05,
                "drift_loss_delta": -0.10,
                "scalar_score_delta": -0.150,
                "tournament_decision": "promoted",
                "rejection_reason": "",
            },
        },
    )
    _write_json(
        edir / "generations" / "v1" / "gen_score.json",
        {"generation_id": "v1", "scalar": 0.850, "drift_loss_mean": 0.400, "pass_rate": 0.85},
    )

    # v2 — rejected challenger of v1.
    _write_json(
        edir / "generations" / "v2" / "experiment.json",
        {
            "id": "exp-v2",
            "epoch_id": epoch,
            "generation_id": "v2",
            "parent_generation_id": "v1",
            "proposed_at": "2026-05-20T02:00:00Z",
            "hypothesis": {
                "core_idea": "noisy variation",
                "modulating": [],
                "why": "",
                "expected_pass_rate_delta": "+0.0",
            },
            "patch_ids": [],
            "outcome": {
                "ran_at": "2026-05-20T02:30:00Z",
                "drift_movements": [],
                "pass_rate_delta": -0.10,
                "drift_loss_delta": 0.15,
                "scalar_score_delta": 0.200,
                "tournament_decision": "rejected",
                "rejection_reason": "regressed past margin",
            },
        },
    )

    return ws, epoch


# ---------------------------------------------------------------------------
# write_seed_experiment
# ---------------------------------------------------------------------------


def test_write_seed_experiment_writes_minimal_marker(tmp_path: Path) -> None:
    """The seed writer leaves a parent-less marker with no outcome."""
    ws, epoch = _bootstrap_workspace_without_v0_marker(tmp_path)
    exp_path = ws / "epochs" / epoch / "generations" / "v0" / "experiment.json"
    assert not exp_path.exists()

    wrote = write_seed_experiment(ws, epoch, "v0", proposed_at="2026-05-20T00:00:00Z")
    assert wrote is True
    assert exp_path.exists()

    body = json.loads(exp_path.read_text())
    assert body["generation_id"] == "v0"
    assert body["epoch_id"] == epoch
    assert body["id"] == f"exp_{epoch}_v0"
    # The seed has no in-epoch parent: written as JSON null, not "".
    assert body["parent_generation_id"] is None
    assert body["outcome"] is None
    assert body["hypothesis"]["core_idea"] == "baseline seed"
    assert body["proposed_at"] == "2026-05-20T00:00:00Z"

    # And the round-tripped Experiment carries None, not "".
    from zicato.epoch.journal import read_experiment  # noqa: PLC0415

    exp = read_experiment(ws, epoch, "v0")
    assert exp.parent_generation_id is None


def test_write_seed_experiment_is_idempotent(tmp_path: Path) -> None:
    """A second call is a no-op and does not overwrite an existing marker."""
    ws, epoch = _bootstrap_workspace_without_v0_marker(tmp_path)

    assert write_seed_experiment(ws, epoch, "v0") is True
    # Mutate the on-disk file so we can detect a rewrite.
    exp_path = ws / "epochs" / epoch / "generations" / "v0" / "experiment.json"
    original = exp_path.read_text()
    tampered = original.replace("baseline seed", "tampered marker")
    exp_path.write_text(tampered)

    # Second call: should leave the tampered text in place.
    assert write_seed_experiment(ws, epoch, "v0") is False
    assert exp_path.read_text() == tampered


def test_read_experiment_normalises_legacy_empty_parent(tmp_path: Path) -> None:
    """A legacy on-disk ``parent_generation_id: ""`` reads back as ``None``.

    Pre-migration workspaces wrote the seed's parent as an empty string;
    the reader must normalise that to ``None`` so the in-memory shape is
    uniform regardless of when the file was written.
    """
    from zicato.epoch.journal import read_experiment  # noqa: PLC0415

    ws, epoch = _bootstrap_workspace_without_v0_marker(tmp_path)
    # Hand-write a v0 experiment.json carrying the legacy empty-string
    # sentinel (the shape pre-migration writers produced).
    _write_json(
        ws / "epochs" / epoch / "generations" / "v0" / "experiment.json",
        {
            "id": f"exp_{epoch}_v0",
            "epoch_id": epoch,
            "generation_id": "v0",
            "parent_generation_id": "",
            "proposed_at": "2026-05-20T00:00:00Z",
            "hypothesis": {"core_idea": "baseline seed", "modulating": [], "why": ""},
            "patch_ids": [],
            "outcome": None,
        },
    )

    exp = read_experiment(ws, epoch, "v0")
    assert exp.parent_generation_id is None


# ---------------------------------------------------------------------------
# Analyzer report_data integration
# ---------------------------------------------------------------------------


def test_gather_returns_v0_when_marker_present(tmp_path: Path) -> None:
    """After backfilling v0, the report data loader includes it."""
    ws, epoch = _bootstrap_workspace_without_v0_marker(tmp_path)
    # Before backfill: v0 is dropped because it has no experiment.json.
    pre = gather_epoch_report_data(ws, epoch)
    pre_ids = [g.generation_id for g in pre.generations]
    assert "v0" not in pre_ids
    # Only v1, v2 surface; v0 is missing -> the bug.

    # Backfill and re-read.
    assert write_seed_experiment(ws, epoch, "v0") is True
    post = gather_epoch_report_data(ws, epoch)
    post_ids = [g.generation_id for g in post.generations]
    assert post_ids == ["v0", "v1", "v2"]
    v0_view = next(g for g in post.generations if g.generation_id == "v0")
    assert v0_view.is_baseline is True
    # The seed marker now stores a null parent; the analyzer report view
    # normalises that back to "" so its downstream wire form is unchanged.
    assert v0_view.parent_generation_id == ""
    assert v0_view.decision == "baseline"  # outcome is None for the seed
    # The v0 gen_score is still picked up.
    assert v0_view.gen_score.get("scalar") == 0.500 or v0_view.gen_score.get("scalar") == 1.000


def test_promoted_lineage_walks_v0_v1_after_backfill(tmp_path: Path) -> None:
    """``_promoted_lineage`` anchors at v0 and walks the promoted chain."""
    ws, epoch = _bootstrap_workspace_without_v0_marker(tmp_path)
    assert write_seed_experiment(ws, epoch, "v0") is True

    data = gather_epoch_report_data(ws, epoch)
    chain = _promoted_lineage(data)
    assert [g.generation_id for g in chain] == ["v0", "v1"]


def test_per_board_outcomes_non_empty_after_backfill(tmp_path: Path) -> None:
    """The per-board outcomes section renders concrete aggregate values."""
    ws, epoch = _bootstrap_workspace_without_v0_marker(tmp_path)
    assert write_seed_experiment(ws, epoch, "v0") is True

    data = gather_epoch_report_data(ws, epoch)
    rendered = render_per_board_outcomes(data)
    # Not the empty-chain placeholder.
    assert "No cached generation scores" not in rendered
    # The promoted chain is v0 -> v1, so both columns appear.
    assert "v0" in rendered
    assert "v1" in rendered
    # The v1 scalar is templated verbatim (0.850).
    assert "0.850" in rendered


# ---------------------------------------------------------------------------
# CLI backfill helper
# ---------------------------------------------------------------------------


def test_repair_v0_baseline_cli_writes_missing_marker(tmp_path: Path) -> None:
    """``zicato repair v0-baseline`` writes the marker into an existing v0."""
    ws, epoch = _bootstrap_workspace_without_v0_marker(tmp_path)
    exp_path = ws / "epochs" / epoch / "generations" / "v0" / "experiment.json"
    assert not exp_path.exists()

    runner = CliRunner()
    result = runner.invoke(repair_v0_baseline_cmd, ["--workspace", str(ws)])
    assert result.exit_code == 0, result.output
    assert exp_path.exists()
    assert epoch in result.output

    # Re-run: no-op.
    result2 = runner.invoke(repair_v0_baseline_cmd, ["--workspace", str(ws)])
    assert result2.exit_code == 0, result2.output
    assert "Left 1 epoch(s) untouched" in result2.output


def test_repair_v0_baseline_cli_filters_by_epoch(tmp_path: Path) -> None:
    """``--epoch`` restricts the backfill to one epoch id."""
    ws, epoch = _bootstrap_workspace_without_v0_marker(tmp_path)

    runner = CliRunner()
    result = runner.invoke(
        repair_v0_baseline_cmd,
        ["--workspace", str(ws), "--epoch", "not-real"],
    )
    assert result.exit_code == 0, result.output
    assert "No epoch 'not-real'" in result.output
    # No write happened.
    assert not (ws / "epochs" / epoch / "generations" / "v0" / "experiment.json").exists()


def test_repair_v0_baseline_cli_reports_no_epochs(tmp_path: Path) -> None:
    """An empty workspace yields a clean no-op."""
    ws = tmp_path / ".zicato"
    ws.mkdir()
    runner = CliRunner()
    result = runner.invoke(repair_v0_baseline_cmd, ["--workspace", str(ws)])
    assert result.exit_code == 0, result.output
    assert "No epochs with a v0 directory found" in result.output


# ---------------------------------------------------------------------------
# Orchestrator bootstrap path
# ---------------------------------------------------------------------------


def test_ensure_baseline_snapshot_writes_v0_marker(tmp_path: Path) -> None:
    """Fresh v0 materialisation drops a synthetic experiment.json into v0/."""
    from zicato.core.types import ScoringWeights
    from zicato.epoch.lifecycle import new_epoch
    from zicato.evolve.round_baseline import _ensure_baseline_snapshot

    ws = tmp_path / ".zicato"
    ws.mkdir()
    board_src = tmp_path / "board.jsonl"
    board_src.write_text(
        '{"id": "qa", "kind": "single_turn", "wall_clock_budget_seconds": 30, '
        '"input": "answer", "expectation": {"kind": "rubric", "spec": "x"}}\n',
        encoding="utf-8",
    )
    brief_src = tmp_path / "brief.md"
    brief_src.write_text("# brief\n", encoding="utf-8")

    # Stage a tiny mutable tree the baseline will copy from.
    src_tree = tmp_path / "agent"
    src_tree.mkdir()
    (src_tree / "agent.py").write_text("# stub\n", encoding="utf-8")

    cfg = new_epoch(
        ws,
        name="baseline-marker",
        board_source=board_src,
        brief_source=brief_src,
        weights=ScoringWeights(promote_margin=0.01),
        auto_close_previous=False,
    )
    epoch_id = cfg.id

    # Before _ensure_baseline_snapshot fires the v0 directory is absent.
    v0_dir = ws / "epochs" / epoch_id / "generations" / "v0"
    assert not v0_dir.exists()

    _ensure_baseline_snapshot(ws, epoch_id, {"mutable_trees": [str(src_tree)]})

    # The marker landed alongside the seeded snapshot.
    exp_path = v0_dir / "experiment.json"
    assert exp_path.exists()
    body = json.loads(exp_path.read_text())
    assert body["generation_id"] == "v0"
    assert body["parent_generation_id"] is None
    assert body["outcome"] is None
    assert body["hypothesis"]["core_idea"] == "baseline seed"
    # proposed_at is the synthetic generation's created_at — non-empty.
    assert body["proposed_at"]

    # A second call is a no-op (idempotent guard kicks in twice — the
    # outer return-early because v0 exists, AND the write-seed helper's
    # exists check).
    _ensure_baseline_snapshot(ws, epoch_id, {"mutable_trees": [str(src_tree)]})
    assert json.loads(exp_path.read_text()) == body
