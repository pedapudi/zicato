"""Tests for :mod:`zicato.epoch.analysis`."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from zicato.core.types import ScoringWeights
from zicato.core.workspace import (
    analysis_path,
    experiment_json_path,
    journal_path,
)
from zicato.epoch import generate_analysis, new_epoch
from zicato.epoch.analysis import REQUIRED_SECTIONS


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / ".zicato"
    ws.mkdir()
    return ws


@pytest.fixture()
def rubric_file(tmp_path: Path) -> Path:
    p = tmp_path / "rubric.md"
    p.write_text("# Rubric\n")
    return p


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_generate_analysis_writes_file(
    workspace: Path, board_file: Path, rubric_file: Path
) -> None:
    cfg = new_epoch(workspace, "alpha", board_file, rubric_file, ScoringWeights())
    # Seed a journal so the prompt has something to chew on.
    journal_path(workspace, cfg.id).write_text("## v1 — Improve routing.\n**outcome**: promoted\n")

    captured: dict[str, str] = {}

    async def stub_call(system: str, user: str, model: str) -> str:
        captured["system"] = system
        captured["user"] = user
        captured["model"] = model
        return (
            f"# Epoch analysis: {cfg.id}\n\n"
            "## Headline movements\n- A\n\n"
            "## Hypotheses that held\n- B\n\n"
            "## Hypotheses that didn't\n- C\n\n"
            "## Surface still open at epoch close\n- D\n\n"
            "## Recommended focus for next epoch\n- E\n"
        )

    out = await generate_analysis(workspace, cfg.id, stub_call)
    assert out == analysis_path(workspace, cfg.id)
    assert out.read_text().startswith(f"# Epoch analysis: {cfg.id}")

    # System prompt requests the structured sections.
    for section in REQUIRED_SECTIONS:
        assert section in captured["system"]
    # User prompt includes the journal text.
    assert "Improve routing" in captured["user"]
    # User prompt has the epoch id heading.
    assert f"Epoch under review: {cfg.id}" in captured["user"]


async def test_generate_analysis_inlines_experiments(
    workspace: Path, board_file: Path, rubric_file: Path
) -> None:
    cfg = new_epoch(workspace, "beta", board_file, rubric_file, ScoringWeights())
    # Drop an experiment.json under generations/v1.
    epath = experiment_json_path(workspace, cfg.id, "v1")
    epath.parent.mkdir(parents=True, exist_ok=True)
    epath.write_text(
        json.dumps(
            {
                "id": "exp_beta_v1",
                "epoch_id": cfg.id,
                "generation_id": "v1",
                "parent_generation_id": "v0",
                "proposed_at": "2026-04-08T10:00:00+00:00",
                "hypothesis": {
                    "core_idea": "Tighten the writer prompt.",
                    "modulating": ["writer.instruction"],
                    "why": "Off-topic drift dominates.",
                },
                "outcome": {"tournament_decision": "promoted"},
            }
        )
    )

    captured: dict[str, str] = {}

    async def stub_call(system: str, user: str, model: str) -> str:
        captured["user"] = user
        return "# Epoch analysis: ok"

    await generate_analysis(workspace, cfg.id, stub_call)
    assert "exp_beta_v1" in captured["user"]
    assert "Tighten the writer prompt." in captured["user"]


async def test_generate_analysis_handles_missing_journal(
    workspace: Path, board_file: Path, rubric_file: Path
) -> None:
    cfg = new_epoch(workspace, "gamma", board_file, rubric_file, ScoringWeights())

    seen: dict[str, str] = {}

    async def stub_call(system: str, user: str, model: str) -> str:
        seen["user"] = user
        return "# Epoch analysis"

    out = await generate_analysis(workspace, cfg.id, stub_call)
    assert out.exists()
    assert "(no journal entries)" in seen["user"]


async def test_generate_analysis_includes_patterns_when_present(
    workspace: Path, board_file: Path, rubric_file: Path
) -> None:
    cfg = new_epoch(workspace, "delta", board_file, rubric_file, ScoringWeights())
    pdir = workspace / "epochs" / cfg.id / "patterns"
    pdir.mkdir()
    (pdir / "round_001.json").write_text(json.dumps([{"id": "p1", "kind": "drift_kind_frequency"}]))

    seen: dict[str, str] = {}

    async def stub_call(system: str, user: str, model: str) -> str:
        seen["user"] = user
        return "# Epoch analysis"

    await generate_analysis(workspace, cfg.id, stub_call)
    assert "drift_kind_frequency" in seen["user"]
    assert "## Patterns" in seen["user"]


async def test_generate_analysis_propagates_model(
    workspace: Path, board_file: Path, rubric_file: Path
) -> None:
    cfg = new_epoch(workspace, "echo", board_file, rubric_file, ScoringWeights())

    seen: dict[str, str] = {}

    async def stub_call(system: str, user: str, model: str) -> str:
        seen["model"] = model
        return "# Epoch analysis"

    await generate_analysis(workspace, cfg.id, stub_call, model="some-model")
    assert seen["model"] == "some-model"
