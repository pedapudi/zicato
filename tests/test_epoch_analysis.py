"""Tests for :mod:`zicato.epoch.analysis`."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests._workspace_support import experiment_record
from zicato.core.patterns import Pattern
from zicato.core.types import ScoringWeights
from zicato.core.workspace import (
    analysis_path,
    experiment_json_path,
)
from zicato.epoch import generate_analysis, new_epoch
from zicato.epoch.analysis import REQUIRED_SECTIONS
from zicato.epoch.round_patterns import write_round_patterns
from zicato.workspace import WorkspaceLayout


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
    from tests._workspace_support import write_json

    write_json(
        experiment_json_path(workspace, cfg.id, "v1"),
        experiment_record(
            "v1",
            epoch_id=cfg.id,
            decision="promoted",
            hypothesis={"core_idea": "Improve routing."},
        ),
    )

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
    assert f"**Epoch id**: `{cfg.id}`" in out.read_text()

    # System prompt requests the structured sections.
    for section in REQUIRED_SECTIONS:
        assert section in captured["system"]
    # User prompt includes the journal text.
    assert "Improve routing" in captured["user"]
    # User prompt has the epoch id heading.
    assert f"Epoch under review: {cfg.id}" in captured["user"]


async def test_generate_analysis_inlines_experiments(
    workspace: Path, board_file: Path, rubric_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from zicato.analyzer import report_data
    from zicato.epoch import analysis

    reads: list[str] = []
    original = analysis.read_epoch_experiments

    def read_once(root: Path, epoch_id: str):
        reads.append(epoch_id)
        return original(root, epoch_id)

    monkeypatch.setattr(analysis, "read_epoch_experiments", read_once)
    monkeypatch.setattr(report_data, "read_epoch_experiments", read_once)
    cfg = new_epoch(workspace, "beta", board_file, rubric_file, ScoringWeights())
    # Drop an experiment.json under generations/v1.
    epath = experiment_json_path(workspace, cfg.id, "v1")
    epath.parent.mkdir(parents=True, exist_ok=True)
    epath.write_text(
        json.dumps(
            experiment_record(
                id="exp_beta_v1",
                epoch_id=cfg.id,
                generation_id="v1",
                parent_generation_id="v0",
                proposed_at="2026-04-08T10:00:00+00:00",
                hypothesis={
                    "core_idea": "Tighten the writer prompt.",
                    "modulating": ["writer.instruction"],
                    "why": "Off-topic drift dominates.",
                },
                outcome={"tournament_decision": "promoted"},
            )
        )
    )

    captured: dict[str, str] = {}

    async def stub_call(system: str, user: str, model: str) -> str:
        captured["user"] = user
        return "# Epoch analysis: ok"

    await generate_analysis(workspace, cfg.id, stub_call)
    assert "exp_beta_v1" in captured["user"]
    assert "Tighten the writer prompt." in captured["user"]
    assert reads == [cfg.id], "narrative and HTML share the same experiment observations"


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
    """Every round's pattern record reaches the prompt, oldest round first."""
    cfg = new_epoch(workspace, "delta", board_file, rubric_file, ScoringWeights())
    layout = WorkspaceLayout.from_root(workspace)
    write_round_patterns(
        layout.round_patterns(cfg.id, 0),
        parent_generation_id="v0",
        patterns=[
            Pattern(
                id="p1",
                kind="drift_metric_frequency",
                summary="goal drift dominates",
                detail={"kind": "goal_drift", "share": "0.800"},
                affected_mutation_ids=("instr",),
                severity="warning",
            )
        ],
    )
    write_round_patterns(layout.round_patterns(cfg.id, 1), parent_generation_id="v1", patterns=[])
    layout.round_patterns(cfg.id, 2).parent.mkdir(parents=True)
    layout.round_patterns(cfg.id, 2).write_text("{}", encoding="utf-8")

    seen: dict[str, str] = {}

    async def stub_call(system: str, user: str, model: str) -> str:
        seen["user"] = user
        return "# Epoch analysis"

    await generate_analysis(workspace, cfg.id, stub_call)
    patterns_section = seen["user"].split("## Patterns\n", 1)[1]
    assert patterns_section.index("### Round 0 (parent generation v0)") < patterns_section.index(
        "### Round 1 (parent generation v1)"
    )
    assert (
        "- warning drift_metric_frequency: goal drift dominates; kind=goal_drift, "
        "share=0.800; mutation points: instr"
    ) in patterns_section
    assert "### Round 1 (parent generation v1)\n- (no patterns detected)" in patterns_section
    assert "### Round 2\n(pattern record unreadable:" in patterns_section


async def test_generate_analysis_omits_patterns_when_no_round_recorded_them(
    workspace: Path, board_file: Path, rubric_file: Path
) -> None:
    cfg = new_epoch(workspace, "foxtrot", board_file, rubric_file, ScoringWeights())
    seen: dict[str, str] = {}

    async def stub_call(system: str, user: str, model: str) -> str:
        seen["user"] = user
        return "# Epoch analysis"

    await generate_analysis(workspace, cfg.id, stub_call)
    assert "## Patterns" not in seen["user"]


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
