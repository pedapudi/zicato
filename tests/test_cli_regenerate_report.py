"""Tests for the ``zicato repair report`` subcommand.

The command re-renders ``epochs/{id}/analysis.md`` against the current
on-disk data — primarily a backfill entrypoint for workspaces whose
report was written by an older orchestrator that mis-rooted the
workspace path (the dashboard's per-board outcomes + aggregate scores
sections rendered empty even though the data was there).

These tests pin:

* The happy path: a populated epoch under ``{ws}/.zicato/epochs/{id}/``,
  invoked with both ``--workspace`` forms (outer + inner), produces a
  report whose deterministic sections carry the fixture's real numbers.
* The ``--no-llm`` switch substitutes placeholder prose but still
  re-renders the deterministic figures + tables.
* A missing workspace surfaces as a clean ``ClickException`` rather
  than a traceback.
"""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from zicato.cli.commands.regenerate_report import regenerate_report_cmd


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _build_populated_workspace(outer: Path) -> tuple[Path, str]:
    """Build a minimal populated workspace and return ``(.zicato, epoch_id)``.

    Mirrors the fixture in ``tests/test_analyzer_report.py`` but trimmed
    to the bare minimum the report renderer needs to template a non-
    empty data section.
    """

    ws = outer / ".zicato"
    epoch = "2026-05-18_demo"
    edir = ws / "epochs" / epoch
    edir.mkdir(parents=True)

    _write_json(
        ws / "config.json",
        {"instance_id": "default"},
    )
    _write_json(
        edir / "config.json",
        {
            "id": epoch,
            "name": "CLI Backfill Smoke",
            "created_at": "2026-05-18T00:00:00Z",
            "contract_hash": "abc",
            "closed": False,
        },
    )
    (edir / "brief.md").write_text("brief", encoding="utf-8")
    (edir / "board.jsonl").write_text(
        '{"id": "only", "kind": "single_turn", "wall_clock_budget_seconds": 60, '
        '"input": "x", "expectation": {"kind": "predicate", "spec": "p"}}\n',
        encoding="utf-8",
    )
    _write_json(edir / "scoring.json", {"promote_margin": 0.02})
    (edir / "journal.md").write_text("## v1\nokay\n", encoding="utf-8")

    # v0 baseline.
    _write_json(
        edir / "generations" / "v0" / "experiment.json",
        {
            "id": "exp-v0",
            "epoch_id": epoch,
            "generation_id": "v0",
            "parent_generation_id": "",
            "hypothesis": {"core_idea": "baseline"},
        },
    )
    # v1 promoted, with a recognisable scalar delta.
    _write_json(
        edir / "generations" / "v1" / "experiment.json",
        {
            "id": "exp-v1",
            "epoch_id": epoch,
            "generation_id": "v1",
            "parent_generation_id": "v0",
            "hypothesis": {"core_idea": "tighten the prompt"},
            "outcome": {
                "ran_at": "2026-05-18T01:00:00Z",
                "scalar_score_delta": -0.250,
                "drift_loss_delta": -0.30,
                "pass_rate_delta": 0.10,
                "tournament_decision": "promoted",
                "rejection_reason": "",
                "drift_movements": [],
            },
        },
    )
    _write_json(
        edir / "generations" / "v1" / "gen_score.json",
        {"generation_id": "v1", "scalar": 0.55, "drift_loss_mean": 0.2, "pass_rate": 0.9},
    )
    (ws / "current_epoch").write_text(epoch, encoding="utf-8")
    return ws, epoch


def test_regenerate_report_inner_workspace(tmp_path: Path) -> None:
    """Passing ``.zicato/`` regenerates the report in place."""
    ws, epoch = _build_populated_workspace(tmp_path)
    runner = CliRunner()
    result = runner.invoke(
        regenerate_report_cmd,
        ["--workspace", str(ws), "--epoch", epoch, "--no-llm"],
    )
    assert result.exit_code == 0, result.output

    md_path = ws / "epochs" / epoch / "analysis.md"
    assert md_path.is_file()
    md = md_path.read_text(encoding="utf-8")
    # Deterministic-section signal: v1's Δscalar is templated from
    # ``experiment.json``. A regression in workspace-root resolution
    # would render this as a "no data" placeholder.
    assert "-0.250" in md
    assert "promoted" in md


def test_regenerate_report_outer_workspace(tmp_path: Path) -> None:
    """Passing the outer project dir also regenerates the report.

    Regression test for the original bug — an operator pointing the
    backfill at the outer dir would historically have written nothing
    (or written into ``{outer}/epochs/...``). The descent normalisation
    must keep the output inside ``.zicato/``.
    """
    ws, epoch = _build_populated_workspace(tmp_path)
    outer = tmp_path

    runner = CliRunner()
    result = runner.invoke(
        regenerate_report_cmd,
        ["--workspace", str(outer), "--epoch", epoch, "--no-llm"],
    )
    assert result.exit_code == 0, result.output

    # Written into ``.zicato/`` — never a phantom ``{outer}/epochs/``.
    inner_md = ws / "epochs" / epoch / "analysis.md"
    assert inner_md.is_file()
    assert not (outer / "epochs" / epoch / "analysis.md").exists()
    md = inner_md.read_text(encoding="utf-8")
    assert "-0.250" in md


def test_regenerate_report_resolves_epoch_from_marker(tmp_path: Path) -> None:
    """No ``--epoch`` flag uses the ``current_epoch`` marker."""
    ws, epoch = _build_populated_workspace(tmp_path)
    runner = CliRunner()
    result = runner.invoke(
        regenerate_report_cmd,
        ["--workspace", str(ws), "--no-llm"],
    )
    assert result.exit_code == 0, result.output
    assert (ws / "epochs" / epoch / "analysis.md").is_file()


def test_regenerate_report_missing_workspace(tmp_path: Path) -> None:
    """A missing workspace surfaces a clean error, not a traceback."""
    runner = CliRunner()
    result = runner.invoke(
        regenerate_report_cmd,
        ["--workspace", str(tmp_path / "missing"), "--epoch", "x", "--no-llm"],
    )
    assert result.exit_code != 0
    assert "No workspace config" in result.output
