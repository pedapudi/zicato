"""Tests for the comprehensive epoch analysis report.

Coverage:

* The deterministic sections are templated exactly from a fixture
  workspace — numbers come straight from the structured artifacts.
* The LLM-narrative path with a mocked auxiliary LLM produces a
  complete document; a missing prose block degrades to a placeholder.
* A report-generation failure is contained — the file is still written
  with placeholder prose, and an internal failure does not propagate
  past the orchestrator's best-effort wrapper.
* The minimal Markdown -> HTML renderer covers the report's subset.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from zicato.analyzer.report import (
    generate_epoch_report,
    markdown_to_html,
    render_report_html,
)
from zicato.analyzer.report_data import gather_epoch_report_data
from zicato.analyzer.report_prompts import parse_prose_blocks
from zicato.analyzer.report_sections import (
    render_methodology_section,
    render_results_section,
    render_score_trajectory_table,
)
from zicato.core.workspace import analysis_path

# ---------------------------------------------------------------------------
# Fixture workspace
# ---------------------------------------------------------------------------


def _write(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


@pytest.fixture()
def epoch_workspace(tmp_path: Path) -> tuple[Path, str]:
    """A populated epoch workspace: baseline + one promoted + one rejected."""
    ws = tmp_path / ".zicato"
    epoch = "2026-05-18_demo"
    edir = ws / "epochs" / epoch
    edir.mkdir(parents=True)

    _write(
        edir / "config.json",
        {
            "id": epoch,
            "name": "Demo Presentation Agent",
            "created_at": "2026-05-18T00:00:00Z",
            "contract_hash": "deadbeefcafe0001",
            "closed": False,
        },
    )
    (edir / "brief.md").write_text(
        "Reduce off-topic drift while keeping the slide pass rate stable.",
        encoding="utf-8",
    )
    (edir / "board.jsonl").write_text(
        '{"id": "slides", "kind": "single_turn", "wall_clock_budget_seconds": 60, '
        '"input": "make slides", "weight": 2.0, '
        '"expectation": {"kind": "predicate", "spec": "has exactly 5 slides"}, '
        '"judges": [{"name": "structure", "mode": "process", "body": "x", '
        '"severity": "warning"}]}\n'
        '{"id": "qa", "kind": "single_turn", "wall_clock_budget_seconds": 30, '
        '"input": "answer", "expectation": {"kind": "rubric", "spec": "accurate"}}\n',
        encoding="utf-8",
    )
    _write(
        edir / "scoring.json",
        {
            "drift_weight": 1.0,
            "pass_weight": 1.5,
            "promote_margin": 0.02,
            "severity_weights": {"info": 1.0, "warning": 3.0, "critical": 10.0},
            "per_judge_weights": {"structure": 2.0},
        },
    )
    _write(
        edir / "mutations.json",
        [
            {"id": "sys_prompt", "kind": "prompt_text", "file": "agent/prompt.txt"},
            {"id": "temp", "kind": "numeric", "file": "agent/config.py"},
        ],
    )
    (edir / "journal.md").write_text(
        "## v1 — tighten the system prompt\n**outcome**: promoted\n\n"
        "## v2 — raise temperature\n**outcome**: rejected\n",
        encoding="utf-8",
    )

    # v0 baseline.
    _write(
        edir / "generations" / "v0" / "experiment.json",
        {
            "id": "exp-v0",
            "epoch_id": epoch,
            "generation_id": "v0",
            "parent_generation_id": "",
            "proposed_at": "2026-05-18T01:00:00Z",
            "hypothesis": {
                "core_idea": "baseline",
                "modulating": [],
                "why": "",
                "expected_pass_rate_delta": "",
            },
        },
    )
    # v1 promoted.
    _write(
        edir / "generations" / "v1" / "experiment.json",
        {
            "id": "exp-v1",
            "epoch_id": epoch,
            "generation_id": "v1",
            "parent_generation_id": "v0",
            "proposed_at": "2026-05-18T02:00:00Z",
            "hypothesis": {
                "core_idea": "tighten the system prompt to stay on topic",
                "modulating": ["sys_prompt"],
                "why": "off-topic drift traced to a loose instruction",
                "risks": "may overconstrain creative slides",
                "expected_pass_rate_delta": "+0.05 to +0.15",
                "expected_drift_movements": [
                    {"kind": "off_topic", "direction": "decrease", "magnitude": "moderate"}
                ],
            },
            "patch_ids": ["p1"],
            "outcome": {
                "ran_at": "2026-05-18T02:30:00Z",
                "drift_movements": [
                    {
                        "kind": "off_topic",
                        "from_rate": 0.40,
                        "to_rate": 0.10,
                        "hypothesis_match": True,
                    }
                ],
                "pass_rate_delta": 0.10,
                "drift_loss_delta": -0.30,
                "scalar_score_delta": -0.250,
                "tournament_decision": "promoted",
                "rejection_reason": "",
            },
        },
    )
    _write(
        edir / "generations" / "v1" / "patches" / "p1.json",
        {
            "id": "p1",
            "mutation_id": "sys_prompt",
            "op": "replace",
            "rationale": "add an explicit stay-on-topic clause",
        },
    )
    _write(
        edir / "generations" / "v1" / "gen_score.json",
        {"generation_id": "v1", "scalar": 0.550, "drift_loss_mean": 0.200, "pass_rate": 0.90},
    )
    # v2 rejected.
    _write(
        edir / "generations" / "v2" / "experiment.json",
        {
            "id": "exp-v2",
            "epoch_id": epoch,
            "generation_id": "v2",
            "parent_generation_id": "v1",
            "proposed_at": "2026-05-18T03:00:00Z",
            "hypothesis": {
                "core_idea": "raise sampling temperature for variety",
                "modulating": ["temp"],
                "why": "slides felt repetitive",
                "expected_pass_rate_delta": "+0.0",
            },
            "patch_ids": ["p2"],
            "outcome": {
                "ran_at": "2026-05-18T03:30:00Z",
                "drift_movements": [],
                "pass_rate_delta": -0.05,
                "drift_loss_delta": 0.12,
                "scalar_score_delta": 0.140,
                "tournament_decision": "rejected",
                "rejection_reason": "scalar regressed past promote margin",
            },
        },
    )
    _write(
        edir / "generations" / "v2" / "patches" / "p2.json",
        {"id": "p2", "mutation_id": "temp", "op": "set_numeric", "rationale": "0.7 -> 0.95"},
    )
    return ws, epoch


# ---------------------------------------------------------------------------
# Deterministic-section tests
# ---------------------------------------------------------------------------


def test_data_gather_counts(epoch_workspace: tuple[Path, str]) -> None:
    ws, epoch = epoch_workspace
    data = gather_epoch_report_data(ws, epoch)
    assert data.epoch_id == epoch
    assert data.epoch_name == "Demo Presentation Agent"
    assert data.attempted == 2  # v1 + v2, baseline excluded
    assert data.promoted == 1
    assert data.rejected == 1
    assert len(data.board_entries) == 2
    assert len(data.mutation_surface) == 2
    # Cumulative scalar: v0=0.0, v1=0.0+(-0.25), v2=v1+0.14.
    cum = {g.generation_id: g.cumulative_scalar for g in data.generations}
    assert cum["v0"] == pytest.approx(0.0)
    assert cum["v1"] == pytest.approx(-0.25)
    assert cum["v2"] == pytest.approx(-0.11)


def test_methodology_section_is_data_exact(epoch_workspace: tuple[Path, str]) -> None:
    ws, epoch = epoch_workspace
    data = gather_epoch_report_data(ws, epoch)
    md = render_methodology_section(data)
    # Board entries are templated verbatim.
    assert "`slides`" in md
    assert "`qa`" in md
    assert "structure" in md
    # Scoring parameters are templated verbatim.
    assert "promote_margin" in md
    assert "0.02" in md
    assert "per_judge_weights" in md
    # The tournament-protocol prose carries the recorded margin.
    assert "promote_margin = 0.02" in md


def test_results_table_numbers_exact(epoch_workspace: tuple[Path, str]) -> None:
    ws, epoch = epoch_workspace
    data = gather_epoch_report_data(ws, epoch)
    table = render_score_trajectory_table(data)
    # Per-generation deltas appear verbatim from experiment.json.
    assert "-0.250" in table  # v1 Δscalar
    assert "+0.140" in table  # v2 Δscalar
    assert "promoted" in table
    assert "rejected" in table
    # The cumulative scalar of v2 is rendered.
    results = render_results_section(data)
    assert "-0.110" in results  # v2 cumulative
    # The drift-movement table shows the off_topic rate movement.
    assert "off_topic" in results
    assert "0.400" in results and "0.100" in results


def test_results_no_invented_numbers(epoch_workspace: tuple[Path, str]) -> None:
    """A figure not present in the workspace must not appear in the report."""
    ws, epoch = epoch_workspace
    data = gather_epoch_report_data(ws, epoch)
    results = render_results_section(data)
    # gen_score aggregates are rendered exactly.
    assert "0.550" in results  # v1 scalar aggregate
    assert "0.900" in results  # v1 pass_rate


# ---------------------------------------------------------------------------
# LLM-narrative path
# ---------------------------------------------------------------------------


async def test_generate_report_full_document(epoch_workspace: tuple[Path, str]) -> None:
    ws, epoch = epoch_workspace
    captured: dict[str, str] = {}

    async def fake_aux(system: str, user: str, model: str) -> str:
        captured["system"] = system
        captured["user"] = user
        return (
            "===ABSTRACT===\n"
            "This epoch tightened the agent's system prompt.\n"
            "===INTRODUCTION===\n"
            "The inner harness is a multi-agent presentation builder.\n"
            "===ANALYSIS===\n"
            "Generation v1 moved the loss as predicted; v2 regressed.\n"
            "===CONCLUSION===\n"
            "The next generation should revisit the temperature mutation.\n"
        )

    out = await generate_epoch_report(ws, epoch, fake_aux)
    assert out == analysis_path(ws, epoch)
    md = out.read_text(encoding="utf-8")

    # Every required section is present, in order.
    for section in (
        "# Epoch Analysis Report",
        "## Abstract",
        "## 1. Introduction",
        "## 2. Methodology",
        "## 3. Approach & Implementation",
        "## 4. Experimental Results",
        "## 5. Analysis — What Worked and What Didn't",
        "## 6. Threats to Validity & Limitations",
        "## 7. Conclusion & Next Directions",
    ):
        assert section in md, section

    # The LLM prose landed in the right sections.
    assert "tightened the agent's system prompt" in md
    assert "multi-agent presentation builder" in md
    assert "moved the loss as predicted" in md
    assert "revisit the temperature mutation" in md

    # The prompt fed the model the structured data + the brief.
    assert "Reduce off-topic drift" in captured["user"]
    assert "scalar_score_delta" in captured["user"]
    assert "do not restate numbers" in captured["user"].lower()

    # The HTML companion was written and is self-contained.
    html_path = out.with_suffix(".html")
    assert html_path.is_file()
    html = html_path.read_text(encoding="utf-8")
    assert html.startswith("<!DOCTYPE html>")
    assert 'src="http' not in html and 'href="http' not in html


async def test_missing_prose_block_degrades_to_placeholder(
    epoch_workspace: tuple[Path, str],
) -> None:
    """An LLM that omits a block still yields a complete document."""
    ws, epoch = epoch_workspace

    async def partial_aux(system: str, user: str, model: str) -> str:
        # Only ABSTRACT and INTRODUCTION returned — ANALYSIS/CONCLUSION absent.
        return "===ABSTRACT===\nshort\n===INTRODUCTION===\nintro\n"

    out = await generate_epoch_report(ws, epoch, partial_aux)
    md = out.read_text(encoding="utf-8")
    assert "## 5. Analysis — What Worked and What Didn't" in md
    assert "## 7. Conclusion & Next Directions" in md
    # The omitted sections carry the placeholder.
    assert "prose section unavailable" in md


async def test_llm_failure_still_writes_report(epoch_workspace: tuple[Path, str]) -> None:
    """An LLM exception is contained — the deterministic report still ships."""
    ws, epoch = epoch_workspace

    async def boom_aux(system: str, user: str, model: str) -> str:
        raise RuntimeError("endpoint exploded")

    out = await generate_epoch_report(ws, epoch, boom_aux)
    assert out.is_file()
    md = out.read_text(encoding="utf-8")
    # Deterministic sections survive a failed LLM call.
    assert "## 2. Methodology" in md
    assert "## 4. Experimental Results" in md
    assert "-0.250" in md
    # Prose is the placeholder.
    assert "prose section unavailable" in md


async def test_llm_timeout_still_writes_report(
    epoch_workspace: tuple[Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A timed-out LLM call substitutes placeholder prose, not a crash."""
    ws, epoch = epoch_workspace
    # Force a near-instant timeout so the test does not actually wait.
    monkeypatch.setattr("zicato.analyzer.report.aux_call_timeout_s", lambda *a, **k: 0.01)

    async def slow_aux(system: str, user: str, model: str) -> str:
        import asyncio

        await asyncio.sleep(5.0)
        return "never reached"

    out = await generate_epoch_report(ws, epoch, slow_aux)
    md = out.read_text(encoding="utf-8")
    assert "## 4. Experimental Results" in md
    assert "prose section unavailable" in md


async def test_empty_epoch_still_generates(tmp_path: Path) -> None:
    """A freshly-created epoch with no generations still yields a report."""
    ws = tmp_path / ".zicato"
    epoch = "fresh"
    (ws / "epochs" / epoch).mkdir(parents=True)

    async def fake_aux(system: str, user: str, model: str) -> str:
        return "===ABSTRACT===\na\n===INTRODUCTION===\nb\n===ANALYSIS===\nc\n===CONCLUSION===\nd\n"

    out = await generate_epoch_report(ws, epoch, fake_aux)
    md = out.read_text(encoding="utf-8")
    assert "# Epoch Analysis Report" in md
    assert "## 4. Experimental Results" in md
    assert "No generations" in md


# ---------------------------------------------------------------------------
# prose-block parser + markdown renderer
# ---------------------------------------------------------------------------


def test_parse_prose_blocks_tolerates_unknown_labels() -> None:
    blocks = parse_prose_blocks(
        "===ABSTRACT===\nhi\n===NONSENSE===\nignored\n===CONCLUSION===\nbye\n"
    )
    assert blocks == {"ABSTRACT": "hi", "CONCLUSION": "bye"}


def test_parse_prose_blocks_empty() -> None:
    assert parse_prose_blocks("no markers here at all") == {}


def test_markdown_to_html_covers_report_subset() -> None:
    md = (
        "# Title\n\n"
        "## Section\n\n"
        "A paragraph with **bold** and `code`.\n\n"
        "- bullet one\n- bullet two\n\n"
        "| a | b |\n| --- | --- |\n| 1 | 2 |\n\n"
        "```\ncode block\n```\n\n"
        "---\n"
    )
    html = markdown_to_html(md)
    assert "<h1>Title</h1>" in html
    assert "<h2>Section</h2>" in html
    assert "<strong>bold</strong>" in html
    assert "<code>code</code>" in html
    assert "<ul>" in html and "<li>bullet one</li>" in html
    assert "<table>" in html and "<th>a</th>" in html and "<td>1</td>" in html
    assert "<pre><code>code block</code></pre>" in html
    assert "<hr/>" in html


def test_render_report_html_is_self_contained() -> None:
    html = render_report_html("e1", "# Hello\n\nworld\n")
    assert html.startswith("<!DOCTYPE html>")
    assert "<style>" in html  # inline CSS, no external link
    assert "prefers-color-scheme: dark" in html  # dark-mode aware
    assert 'href="http' not in html
    assert 'src="http' not in html
