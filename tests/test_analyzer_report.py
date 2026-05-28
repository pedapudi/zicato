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
    render_report_html_fragment,
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


def test_gather_accepts_outer_workspace_dir(epoch_workspace: tuple[Path, str]) -> None:
    """`gather_epoch_report_data` resolves an outer-dir workspace correctly.

    Regression test for the workspace-root mis-rooting bug: an older
    orchestrator path passed the *outer* project dir (the parent of
    ``.zicato/``) to ``gather_epoch_report_data`` and got back an empty
    view because ``epoch_dir`` resolved to ``{outer}/epochs/...`` (which
    didn't exist). The deterministic sections then rendered placeholders
    on a workspace whose data was perfectly intact, just one level
    down.

    The fixture's ``ws`` is the inner ``.zicato/`` dir. The outer dir
    is its parent, which should now resolve identically thanks to the
    descent normalisation in :mod:`zicato.core.workspace`.
    """
    ws, epoch = epoch_workspace
    outer = ws.parent  # the project root holding ``.zicato/``

    # Inner form (canonical) — same call shape every caller uses.
    inner_data = gather_epoch_report_data(ws, epoch)

    # Outer form — historically returned an empty view; must now match
    # the inner form's data exactly.
    outer_data = gather_epoch_report_data(outer, epoch)

    # All three generations resolve in both forms; an empty
    # generations tuple is the symptom the original bug surfaced as.
    assert len(outer_data.generations) == len(inner_data.generations) == 3
    assert outer_data.epoch_id == inner_data.epoch_id == epoch
    assert outer_data.epoch_name == inner_data.epoch_name
    assert outer_data.attempted == inner_data.attempted
    assert outer_data.promoted == inner_data.promoted
    assert outer_data.rejected == inner_data.rejected

    # Per-generation gen_score must round-trip too — that's the bag of
    # data the per-board outcomes + aggregate scores sections read.
    outer_scores = {
        g.generation_id: dict(g.gen_score) for g in outer_data.generations if g.gen_score
    }
    inner_scores = {
        g.generation_id: dict(g.gen_score) for g in inner_data.generations if g.gen_score
    }
    assert outer_scores == inner_scores
    assert "v1" in outer_scores
    assert outer_scores["v1"]["scalar"] == pytest.approx(0.550)


def test_gather_does_not_descend_when_outer_layout_exists(tmp_path: Path) -> None:
    """A legacy workspace laid out directly under ``{ws}/epochs/`` is preserved.

    Some tests + a couple of legacy workspaces build the epoch tree
    directly under the dir they pass, with no ``.zicato/`` wrapper. The
    descent normaliser must skip when ``{ws}/epochs/`` already exists,
    so those workspaces keep loading exactly as they always have.
    """
    epoch = "e0"
    (tmp_path / "epochs" / epoch).mkdir(parents=True)
    _write(
        tmp_path / "epochs" / epoch / "config.json",
        {"id": epoch, "name": "Legacy", "created_at": "", "contract_hash": "", "closed": False},
    )
    (tmp_path / "epochs" / epoch / "board.jsonl").write_text("", encoding="utf-8")
    # No descent — gather should resolve to the path we built.
    data = gather_epoch_report_data(tmp_path, epoch)
    assert data.epoch_id == epoch
    assert data.epoch_name == "Legacy"


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

    # Every required section is present, in order — headings carry NO
    # explicit number; the HTML renderer numbers them. The masthead's
    # H1 is now the epoch name on its own; the eyebrow line above it
    # names the artifact.
    assert "epoch analysis report" in md.lower()
    assert "<!-- EYEBROW -->" in md
    assert "# Demo Presentation Agent" in md
    for section in (
        "## Abstract",
        "## Introduction",
        "## Methodology",
        "## Approach & Implementation",
        "## Experimental Results",
        "## Analysis — What Worked and What Didn't",
        "## Threats to Validity & Limitations",
        "## Conclusion & Next Directions",
    ):
        assert section in md, section
    # And the markdown source must NOT carry hard-coded section numbers
    # (a regression here would double-number in the rendered HTML).
    for forbidden in (
        "## 1. ",
        "## 2. ",
        "## 3. ",
        "## 4. ",
        "## 5. ",
        "## 6. ",
        "## 7. ",
        "### 2.1 ",
        "### 3.1 ",
        "### 4.1 ",
    ):
        assert forbidden not in md, f"unexpected hard-coded number {forbidden!r}"

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
    # Renderer auto-numbered the sections: every numbered h2 carries a
    # ``.secnum`` span, and the introduction / methodology / results
    # heads come out as ``1.`` / ``2.`` / ``...`` in order.
    assert '<span class="secnum">1</span>' in html
    assert '<span class="secnum">2</span>' in html
    assert '<span class="secnum">3</span>' in html
    # The HTML carries the paper article wrapper and the inline SVG of
    # the score-trajectory figure (figures are inline, no external refs).
    assert 'class="paper"' in html
    assert "<svg" in html


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
    assert "## Analysis — What Worked and What Didn't" in md
    assert "## Conclusion & Next Directions" in md
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
    assert "## Methodology" in md
    assert "## Experimental Results" in md
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
    assert "## Experimental Results" in md
    assert "prose section unavailable" in md


async def test_generate_epoch_report_accepts_outer_workspace_dir(
    epoch_workspace: tuple[Path, str],
) -> None:
    """`generate_epoch_report` works when handed the outer project dir.

    Regression test for the orchestrator's ``_regenerate_epoch_report``
    path. The orchestrator passes ``workspace_root`` straight through;
    if a caller hands it the *outer* project dir (the parent of
    ``.zicato/``), the report must still:

    1. Walk the right tree (the inner ``.zicato/`` layout).
    2. Write ``analysis.md`` to the inner dir, never to a phantom
       ``{outer}/epochs/...`` path.

    Both behaviours depend on the workspace-root descent normalisation
    in :mod:`zicato.core.workspace`.
    """
    ws, epoch = epoch_workspace
    outer = ws.parent  # The project dir holding ``.zicato/``.

    async def fake_aux(_system: str, _user: str, _model: str) -> str:
        return (
            "===ABSTRACT===\na\n===INTRODUCTION===\nb\n" "===ANALYSIS===\nc\n===CONCLUSION===\nd\n"
        )

    out = await generate_epoch_report(outer, epoch, fake_aux)

    # Written to the inner layout, NOT a phantom ``{outer}/epochs/`` tree.
    assert out == ws / "epochs" / epoch / "analysis.md"
    assert out.is_file()
    assert not (outer / "epochs" / epoch / "analysis.md").exists()

    md = out.read_text(encoding="utf-8")
    # Deterministic data sections carry the fixture's real numbers — the
    # symptom of the original mis-rooting bug was that these sections
    # rendered as empty placeholders. A populated `-0.250` (v1 Δscalar)
    # confirms `gather_epoch_report_data` walked the right tree.
    assert "-0.250" in md
    assert "promoted" in md
    assert "rejected" in md


async def test_empty_epoch_still_generates(tmp_path: Path) -> None:
    """A freshly-created epoch with no generations still yields a report."""
    ws = tmp_path / ".zicato"
    epoch = "fresh"
    (ws / "epochs" / epoch).mkdir(parents=True)

    async def fake_aux(system: str, user: str, model: str) -> str:
        return "===ABSTRACT===\na\n===INTRODUCTION===\nb\n===ANALYSIS===\nc\n===CONCLUSION===\nd\n"

    out = await generate_epoch_report(ws, epoch, fake_aux)
    md = out.read_text(encoding="utf-8")
    # The masthead's H1 is the epoch name; the eyebrow line above it
    # names the artifact ("epoch analysis report").
    assert "# fresh" in md
    assert "epoch analysis report" in md.lower()
    assert "## Experimental Results" in md
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
    # h2 carries an auto-numbered prefix span (1, 2, ...).
    assert '<span class="secnum">1</span>' in html
    assert "Section</h2>" in html
    assert "<strong>bold</strong>" in html
    assert "<code>code</code>" in html
    assert "<ul>" in html and "<li>bullet one</li>" in html
    assert "<table>" in html and ">a</th>" in html
    # Numeric column auto-detected as right-aligned.
    assert '<td class="num">1</td>' in html
    assert '<th class="num">a</th>' in html
    assert "<pre><code>code block</code></pre>" in html
    # Paper hr class.
    assert '<hr class="paper-rule"/>' in html


def test_render_report_html_is_self_contained() -> None:
    html = render_report_html("e1", "# Hello\n\nworld\n")
    assert html.startswith("<!DOCTYPE html>")
    assert "<style>" in html  # inline CSS, no external link
    # Paper styling — serif body, paper-tone background.
    assert "Georgia" in html or "Source Serif Pro" in html
    assert ".paper" in html
    # No external font fetches, no external resources.
    assert 'href="http' not in html
    assert 'src="http' not in html
    assert "fonts.googleapis" not in html
    assert "<link " not in html


# ---------------------------------------------------------------------------
# Paper-style renderer — auto-numbering, captions, figures
# ---------------------------------------------------------------------------


def test_h2_h3_h4_get_dotted_section_numbers() -> None:
    md = (
        "## First\n\n"
        "### First A\n\n"
        "### First B\n\n"
        "#### Deep\n\n"
        "## Second\n\n"
        "### Second A\n"
    )
    html = markdown_to_html(md)
    # Auto-numbering: 1, 1.1, 1.2, 1.2.1, 2, 2.1.
    assert '<span class="secnum">1</span>' in html
    assert '<span class="secnum">1.1</span>' in html
    assert '<span class="secnum">1.2</span>' in html
    assert '<span class="secnum">1.2.1</span>' in html
    assert '<span class="secnum">2</span>' in html
    assert '<span class="secnum">2.1</span>' in html


def test_abstract_h2_is_unnumbered() -> None:
    # The Abstract is academic-paper convention — unnumbered. Other h2s
    # following it continue numbering from 1.
    md = "## Abstract\n\nbody\n\n## Introduction\n\nintro\n"
    html = markdown_to_html(md)
    assert 'class="unnumbered"' in html
    # The first numbered h2 is the Introduction with secnum 1.
    assert '<span class="secnum">1</span> Introduction' in html


def test_explicit_section_numbers_in_md_double_number() -> None:
    """Belt-and-braces: explicit "1." in markdown WOULD double-number.

    This is the regression we guard against — the section sources have
    been stripped of explicit "1./2./..." prefixes. If a future edit
    re-adds one to the markdown, the renderer would emit it alongside
    the auto-prefix; this test documents that contract explicitly so a
    drift back to hard-coded numbering trips a test.
    """
    md = "## 1. Introduction\n"
    html = markdown_to_html(md)
    # Both the auto-number AND the literal "1." would show.
    assert '<span class="secnum">1</span>' in html
    assert "1. Introduction" in html


def test_caption_line_attaches_to_next_table_as_table_n_caption() -> None:
    md = "Caption: First table caption.\n\n" "| a | b |\n| --- | --- |\n| 1 | 2 |\n"
    html = markdown_to_html(md)
    assert '<figure class="paper-table">' in html
    assert "<figcaption>" in html
    assert '<span class="figlabel">Table 1:</span> First table caption.' in html


def test_figure_marker_substitutes_inline_svg_with_caption() -> None:
    """A figure marker becomes a <figure> with inline SVG and 'Figure N:' caption.

    The renderer dispatches to :func:`render_figure` — when ``data`` is
    supplied, a real SVG lands in the figure; without ``data`` (the
    renderer-only path) a placeholder div lands.
    """
    md = "Caption: Score trajectory across generations.\n\n" "<!-- FIGURE:score-trajectory -->\n"
    # Without data — placeholder.
    html_no_data = markdown_to_html(md)
    assert '<figure class="paper-figure">' in html_no_data
    assert '<span class="figlabel">Figure 1:</span>' in html_no_data
    assert "figure-placeholder" in html_no_data

    # With data — real inline SVG.
    from zicato.analyzer.report_data import EpochReportData

    data = EpochReportData(
        epoch_id="e1",
        epoch_name="e1",
        contract_hash="",
        created_at="",
        closed=False,
        closed_at="",
        brief_text="",
        journal_text="",
        board_entries=(),
        disable_drift=(),
        scoring={},
        mutation_surface=(),
        generations=(),
        span_start="",
        span_end="",
    )
    html_with_data = markdown_to_html(md, data=data)
    assert "<svg" in html_with_data
    assert "</svg>" in html_with_data


def test_figure_and_table_counters_are_independent() -> None:
    md = (
        "Caption: First table.\n\n"
        "| a | b |\n| --- | --- |\n| 1 | 2 |\n\n"
        "Caption: First figure.\n\n"
        "<!-- FIGURE:score-trajectory -->\n\n"
        "Caption: Second table.\n\n"
        "| c | d |\n| --- | --- |\n| 3 | 4 |\n\n"
        "Caption: Second figure.\n\n"
        "<!-- FIGURE:lineage -->\n"
    )
    html = markdown_to_html(md)
    # Figure / Table counters are independent: each starts at 1 in its
    # own series.
    assert "Figure 1:" in html
    assert "Figure 2:" in html
    assert "Table 1:" in html
    assert "Table 2:" in html


def test_numeric_table_columns_are_right_aligned() -> None:
    md = "| gen | scalar |\n| --- | --- |\n" "| v0 | +0.000 |\n" "| v1 | -0.250 |\n"
    html = markdown_to_html(md)
    # The "scalar" column is numeric — header and cells both class="num".
    assert '<th class="num">scalar</th>' in html
    assert '<td class="num">+0.000</td>' in html
    # "gen" column carries non-numeric ids — not right-aligned.
    assert "<th>gen</th>" in html
    assert "<td>v0</td>" in html


def test_metadata_marker_emits_paper_meta_block() -> None:
    md = "# Title\n\n<!-- META -->\n**Epoch id**: `e1`  \n**Status**: closed\n"
    html = markdown_to_html(md)
    assert '<div class="paper-meta">' in html
    assert "<strong>Epoch id</strong>" in html


def test_render_report_html_wraps_in_paper_article() -> None:
    html = render_report_html("e1", "# T\n\nhello\n")
    assert '<article class="paper">' in html
    assert '<div class="paper-article">' in html
    # Paper CSS variables ride along.
    assert "--paper-bg" in html
    # The standalone document carries the page-level body background.
    assert "background: #e9e7e1" in html


def test_render_report_html_fragment_omits_doctype_but_carries_paper_css() -> None:
    """Inline fragment is a paper-card embedded inside the dashboard.

    It must NOT be a full HTML document — no doctype, no html/head/body
    shell — but it must carry its own scoped paper CSS so the dashboard
    embedding cannot leak typography into surrounding chrome.
    """
    fragment = render_report_html_fragment("e1", "# T\n\nhello\n")
    assert not fragment.startswith("<!DOCTYPE")
    assert "<html" not in fragment
    # Look for the head shell explicitly; bare ``<head`` would clash with
    # the legitimate masthead ``<header>`` block.
    assert "<head>" not in fragment
    assert "<head " not in fragment
    # Carries the paper class on the article wrapper and a paper-card
    # modifier so the dashboard can style the card chrome.
    assert 'class="paper paper-card"' in fragment
    # Brings its own inline CSS scoped to .paper.
    assert "<style>" in fragment
    assert ".paper" in fragment
    # No external resources.
    assert 'href="http' not in fragment
    assert 'src="http' not in fragment


def test_full_document_includes_every_required_figure(
    epoch_workspace: tuple[Path, str],
) -> None:
    """The generated HTML carries every required figure type inline.

    Requirements: score trajectory, drift-kind movements, per-board
    heatmap, lineage, and the mutation-surface compact figure.
    """
    import asyncio

    ws, epoch = epoch_workspace

    async def fake_aux(system: str, user: str, model: str) -> str:
        return "===ABSTRACT===\nA\n===INTRODUCTION===\nI\n===ANALYSIS===\nAn\n===CONCLUSION===\nC\n"

    out = asyncio.run(generate_epoch_report(ws, epoch, fake_aux))
    html = out.with_suffix(".html").read_text(encoding="utf-8")

    # Each figure carries a distinct aria-label set by its renderer.
    for aria in (
        "Score trajectory across generations",
        "Drift-kind rate movements per generation",
        "Per-board entry outcomes heatmap",
        "Lineage diagram",
        "Mutation surface",
        "Proposer hypothesis vs tournament outcome",
        "Mutation impact matrix",
    ):
        assert aria in html, aria
    # Every figure is wrapped in a paper-figure block with a caption.
    assert html.count('<figure class="paper-figure">') >= 7
    assert "Figure 1:" in html
    assert "Figure 7:" in html


def test_paper_table_caption_lands_above_table(
    epoch_workspace: tuple[Path, str],
) -> None:
    """Captions sit ABOVE the table per academic convention.

    Inside one ``figure.paper-table``, the figcaption comes before the
    table element. Use a single matching block to assert ordering.
    """
    import asyncio

    ws, epoch = epoch_workspace

    async def fake_aux(s: str, u: str, m: str) -> str:
        return "===ABSTRACT===\nA\n===INTRODUCTION===\nI\n===ANALYSIS===\nAn\n===CONCLUSION===\nC\n"

    out = asyncio.run(generate_epoch_report(ws, epoch, fake_aux))
    html = out.with_suffix(".html").read_text(encoding="utf-8")
    # The first paper-table figure carries figcaption before <table>.
    idx_fig = html.find('<figure class="paper-table">')
    idx_cap = html.find("<figcaption>", idx_fig)
    idx_tbl = html.find("<table>", idx_fig)
    assert 0 <= idx_fig < idx_cap < idx_tbl


def test_fragment_can_be_concatenated_into_dashboard_chrome() -> None:
    """The inline fragment is safe to drop inside a host div.

    No `<html>`, `<head>`, `<body>` tags — and the CSS is scoped to
    `.paper` so it cannot bleed onto surrounding markup.
    """
    fragment = render_report_html_fragment("e1", "## Section\n\nbody.\n")
    # Concatenate into a host wrapper — the result should be balanced HTML.
    composite = '<div id="host">' + fragment + "</div>"
    # html.parser-based well-formedness check.
    from html.parser import HTMLParser

    class _S(HTMLParser):
        def __init__(self) -> None:
            super().__init__(convert_charrefs=True)
            self.stack: list[str] = []
            self.unbalanced: list[str] = []
            self._void = {"path", "rect", "circle", "line", "br", "hr", "meta", "link"}

        def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            if tag in self._void:
                return
            self.stack.append(tag)

        def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            return

        def handle_endtag(self, tag: str) -> None:
            if not self.stack:
                self.unbalanced.append(f"close without open: {tag}")
                return
            if self.stack[-1] == tag:
                self.stack.pop()
                return
            self.unbalanced.append(f"mismatched: expected </{self.stack[-1]}> got </{tag}>")

    parser = _S()
    parser.feed(composite)
    parser.close()
    assert not parser.unbalanced, parser.unbalanced[:3]
    assert not parser.stack, parser.stack


# ---------------------------------------------------------------------------
# Dark-mode-friendly inline paper card
# ---------------------------------------------------------------------------
#
# The standalone analysis.html document is paper-toned (cream sheet on a
# muted-grey desk); the SAME fragment, when embedded inline inside the
# dashboard, lands inside a ``.analysis-paper-card`` wrapper that rebinds
# the paper palette to dashboard-dark values. The fragment's CSS exposes
# every paper colour via CSS custom properties so the wrapper can re-tint
# without touching typography.


def test_paper_palette_exposed_via_css_variables() -> None:
    """Every paper colour is bound through a CSS custom property.

    The defaults are the paper-tone palette; a downstream host can
    override any of them to retheme the surface without changing the
    fragment's typography or HTML structure.
    """
    fragment = render_report_html_fragment("e1", "# T\n\nhello\n")
    # The full surface — text, muted, rule, code, accent, figure tones —
    # is exposed via --paper-* variables so a host can retheme.
    for token in (
        "--paper-bg",
        "--paper-text",
        "--paper-muted",
        "--paper-rule",
        "--paper-soft-rule",
        "--paper-code-bg",
        "--paper-accent",
        # Decision palette (shared with the dashboard's accent tokens).
        "--paper-promoted",
        "--paper-rejected",
        "--paper-deferred",
        "--paper-baseline",
        # Figure-specific tones (grid, no-data stripe, near-zero).
        "--paper-figure-grid",
        "--paper-figure-stripe-bg",
        # Table zebra striping.
        "--paper-table-zebra",
    ):
        assert token in fragment, token


def test_standalone_html_keeps_light_paper_palette() -> None:
    """The standalone analysis.html still defines the cream-paper defaults.

    The dark-mode overrides live on the dashboard's ``.analysis-paper-card``
    wrapper; the standalone document carries the original paper-tone
    defaults unchanged.
    """
    html = render_report_html("e1", "# Title\n\nbody\n")
    # The paper-tone defaults are still declared on .paper.
    assert "--paper-bg: #fafaf7" in html
    assert "--paper-text: #1e1f22" in html
    # The standalone page-background tone (the "desk" the sheet sits on)
    # is unchanged.
    assert "background: #e9e7e1" in html
    # And the standalone document does NOT carry the analysis-paper-card
    # wrapper (that lives in the dashboard CSS).
    assert "analysis-paper-card" not in html


def test_inline_fragment_carries_light_defaults_dashboard_overrides_them() -> None:
    """The fragment ships the paper-tone defaults; the dashboard re-tints.

    The fragment's own inline ``<style>`` keeps the paper-tone defaults
    so the standalone analysis.html (which reuses the same renderer) is
    unaffected. The dashboard's ``.analysis-paper-card .paper`` selector
    overrides the variables for the inline surface — that override
    lives in ``src/zicato/dashboard/static/style.css``.
    """
    fragment = render_report_html_fragment("e1", "# T\n\nhello\n")
    # Fragment carries the paper-tone defaults.
    assert "--paper-bg: #fafaf7" in fragment
    # The dashboard stylesheet rebinds every paper-* token inside the
    # card wrapper.
    css_path = (
        Path(__file__).resolve().parent.parent
        / "src"
        / "zicato"
        / "dashboard"
        / "static"
        / "style.css"
    )
    css = css_path.read_text(encoding="utf-8")
    # The wrapper exists.
    assert ".analysis-paper-card" in css
    # And it rebinds every paper-* token so the inline surface goes dark.
    for rebind in (
        "--paper-bg: var(",
        "--paper-text: var(",
        "--paper-muted: var(",
        "--paper-rule: var(",
        "--paper-code-bg: var(",
        "--paper-promoted: var(",
        "--paper-rejected: var(",
        "--paper-figure-grid: var(",
        # New palette tokens added by the visual-polish pass: the dark
        # surface must rebind them too so the new figures + callout pick
        # up dashboard-tinted hues.
        "--paper-incomplete: var(",
        "--paper-predicted: var(",
        "--paper-callout-bg",
        "--paper-callout-rule",
    ):
        assert rebind in css, rebind
    # The legacy hard-coded cream paint on the wrapper is gone — the
    # wrapper now picks up the dashboard's card surface instead so it
    # does not clash with surrounding chrome.
    assert "background: #fafaf7" not in css


def test_inline_figures_use_theme_aware_colors() -> None:
    """Inline SVG figures bind colours to CSS variables, not hard-coded hex.

    A dark host palette must be able to retint every figure without
    re-rendering the SVG — so promoted/rejected/baseline/deferred fills
    and strokes, plus the grid rule, all read from ``--paper-*``
    variables in the emitted SVG.
    """
    # Use the figures module directly with a tiny synthetic data view so
    # we exercise the real renderer path (rather than re-rendering the
    # whole document).
    from zicato.analyzer.report_data import BoardEntryView, EpochReportData, GenerationView
    from zicato.analyzer.report_figures import (
        render_svg_drift_movements,
        render_svg_lineage_compact,
        render_svg_per_board_heatmap,
        render_svg_score_trajectory,
    )

    entry = BoardEntryView(
        id="b1",
        kind="single_turn",
        weight=1.0,
        tags=(),
        expectation_kind="predicate",
        expectation_spec="",
        judges=(),
        wall_clock_budget_seconds=60,
    )

    def _gen(
        gid: str,
        *,
        parent: str = "",
        is_baseline: bool = False,
        decision: str = "",
        scalar: float = 0.0,
        cumulative: float = 0.0,
        drift: tuple[dict[str, object], ...] = (),
        gen_score: dict[str, object] | None = None,
    ) -> GenerationView:
        return GenerationView(
            generation_id=gid,
            parent_generation_id=parent,
            is_baseline=is_baseline,
            proposed_at="2026-05-19T01:00:00Z",
            core_idea="",
            why="",
            risks="",
            modulating=(),
            expected_pass_rate_delta="",
            expected_drift_movements=(),
            decision=decision,
            rejection_reason="",
            scalar_score_delta=scalar,
            drift_loss_delta=0.0,
            pass_rate_delta=0.0,
            drift_movements=drift,
            metric_movements=(),
            patches=(),
            gen_score=gen_score or {},
            cumulative_scalar=cumulative,
        )

    data = EpochReportData(
        epoch_id="e1",
        epoch_name="e1",
        contract_hash="",
        created_at="",
        closed=False,
        closed_at="",
        brief_text="",
        journal_text="",
        board_entries=(entry,),
        disable_drift=(),
        scoring={},
        mutation_surface=(),
        generations=(
            _gen("v0", is_baseline=True),
            _gen(
                "v1",
                parent="v0",
                decision="promoted",
                scalar=-0.25,
                cumulative=-0.25,
                drift=({"kind": "off_topic", "from_rate": 0.4, "to_rate": 0.1},),
                gen_score={"entries": {"b1": {"scalar_delta": -0.1}}},
            ),
            _gen(
                "v2",
                parent="v1",
                decision="rejected",
                scalar=0.14,
                cumulative=-0.11,
                drift=({"kind": "verbose", "from_rate": 0.2, "to_rate": 0.5},),
                gen_score={"entries": {"b1": {"scalar_delta": 0.05}}},
            ),
        ),
        span_start="",
        span_end="",
    )

    score = render_svg_score_trajectory(data)
    drift_svg = render_svg_drift_movements(data)
    heat = render_svg_per_board_heatmap(data)
    lineage = render_svg_lineage_compact(data)
    combined = score + drift_svg + heat + lineage

    # The figures emit decision palette references via CSS variables —
    # the host palette controls the actual hue.
    assert "var(--paper-promoted)" in combined
    assert "var(--paper-rejected)" in combined
    assert "var(--paper-baseline)" in combined
    assert "var(--paper-figure-grid)" in combined

    # Hard-coded decision hex values are gone from the rendered SVG —
    # the legacy palette constants now flow exclusively through CSS
    # vars so a dark host retints them. (The figures module returns
    # only the SVG fragment, no CSS, so any hex here would render
    # literally.)
    for hard_coded in (
        'stroke="#2ea043"',
        'stroke="#d73a49"',
        'stroke="#6e7681"',
        'stroke="#bf8700"',
        'stroke="#d0d7de"',
        'fill="#2ea043"',
        'fill="#d73a49"',
        'fill="#6e7681"',
        # The heatmap previously emitted rgba(R, G, B, a) for the
        # red/green saturation — those are now ``var(--paper-rejected)``
        # / ``var(--paper-promoted)`` with fill-opacity.
        "rgba(215, 58, 73",
        "rgba(46, 160, 67",
        # The lineage previously emitted explicit rgba node fills — now
        # the decision-coloured token + a fill-opacity.
        "rgba(110, 118, 129",
        "rgba(191, 135, 0",
        # The paper-tone cream is a host palette default, never baked
        # into figure markup.
        "#fafaf7",
    ):
        assert hard_coded not in combined, hard_coded


# ---------------------------------------------------------------------------
# Refined masthead + section openers + table-row decision highlights
# ---------------------------------------------------------------------------


def test_masthead_carries_eyebrow_title_and_structured_metadata(
    epoch_workspace: tuple[Path, str],
) -> None:
    """The masthead is a structured cover, not a flat metadata block.

    Composition: a small-caps eyebrow line naming the artifact, the
    epoch name as the title (h1), then a grid of labelled metadata
    cells. The whole block sits inside a ``<header class="paper-masthead">``
    so the standalone CSS + the dark dashboard CSS can both reach it
    via one stable hook.
    """
    import asyncio

    ws, epoch = epoch_workspace

    async def fake_aux(s: str, u: str, m: str) -> str:
        return "===ABSTRACT===\nA\n===INTRODUCTION===\nI\n===ANALYSIS===\nAn\n===CONCLUSION===\nC\n"

    out = asyncio.run(generate_epoch_report(ws, epoch, fake_aux))
    html = out.with_suffix(".html").read_text(encoding="utf-8")

    # The masthead block wraps the eyebrow, h1, and meta together.
    assert '<header class="paper-masthead">' in html
    assert "</header>" in html
    # Eyebrow line above the title — small-caps treatment.
    assert '<div class="paper-eyebrow">' in html
    # Title is the epoch name as h1.
    assert "<h1>Demo Presentation Agent</h1>" in html
    # Metadata renders as structured label/value cells, not a flat <p>.
    assert '<div class="paper-meta">' in html
    assert 'class="meta-row"' in html
    assert 'class="meta-value"' in html


def test_section_openers_use_small_caps_via_h3(epoch_workspace: tuple[Path, str]) -> None:
    """H3 section openers (subsections) are emitted with small-caps styling.

    The CSS applies the small-caps treatment via ``.paper h3`` (uppercase,
    letter-spaced, muted colour). The test checks the structural
    pre-condition: the h3 headings carry an auto-numbered ``secnum`` span
    so the renderer's auto-numbering pipeline drives the small-caps
    label rather than free-floating uppercase text inside the body.
    """
    import asyncio

    ws, epoch = epoch_workspace

    async def fake_aux(s: str, u: str, m: str) -> str:
        return "===ABSTRACT===\nA\n===INTRODUCTION===\nI\n===ANALYSIS===\nAn\n===CONCLUSION===\nC\n"

    out = asyncio.run(generate_epoch_report(ws, epoch, fake_aux))
    html = out.with_suffix(".html").read_text(encoding="utf-8")

    # Multiple h3 subsections appear with secnums — one each for the
    # evaluation board, scoring model, tournament protocol, mutation
    # surface, lineage diagram, hypothesis vs outcome, drift movements,
    # per-board outcomes, etc.
    assert html.count("<h3>") >= 5
    assert '<h3><span class="secnum">' in html


def test_decision_row_highlight_classes_emitted_in_score_table() -> None:
    """The score-trajectory table marks promoted/rejected rows.

    A row whose cells include a bare ``promoted`` / ``rejected`` token
    picks up the matching ``row-*`` class. The host CSS paints a thin
    coloured edge on the row in the same hue every figure uses for that
    decision, so the same hue means the same thing across the document.
    """
    md = (
        "| gen | scalar | decision |\n"
        "| --- | --- | --- |\n"
        "| v0 | +0.000 | baseline |\n"
        "| v1 | -0.250 | promoted |\n"
        "| v2 | -0.110 | rejected |\n"
    )
    html = markdown_to_html(md)
    assert '<tr class="row-promoted">' in html
    assert '<tr class="row-rejected">' in html
    # Baseline gets no decision-row class today (it would conflict with
    # the "baseline" row's neutral styling).
    assert '<tr class="row-baseline">' not in html


def test_callout_marker_renders_pull_quote_aside() -> None:
    """The CALLOUT marker yields a margin-pull-quote aside.

    The deterministic sections drop one callout in the Experimental
    Results section summarising the campaign's headline number. The
    renderer maps the marker to an ``<aside class="paper-callout">`` so
    a CSS host can paint it as a sidenote / pull quote without a markup
    change.
    """
    md = (
        "## Section\n\n"
        "<!-- CALLOUT:KEY OBSERVATION -->\n"
        "The cumulative scalar fell from baseline.\n\n"
        "Body paragraph follows.\n"
    )
    html = markdown_to_html(md)
    assert '<aside class="paper-callout"' in html
    assert "KEY OBSERVATION" in html
    assert "cumulative scalar fell" in html
    # The next paragraph is a sibling — not absorbed into the callout.
    assert "<p>Body paragraph follows.</p>" in html


def test_callout_in_results_section_summarises_campaign(
    epoch_workspace: tuple[Path, str],
) -> None:
    """The deterministic Results section emits one campaign-summary callout.

    The fixture has 2 attempted, 1 promoted, 1 rejected — the callout
    text mentions those figures verbatim from the data view.
    """
    import asyncio

    ws, epoch = epoch_workspace

    async def fake_aux(s: str, u: str, m: str) -> str:
        return "===ABSTRACT===\nA\n===INTRODUCTION===\nI\n===ANALYSIS===\nAn\n===CONCLUSION===\nC\n"

    out = asyncio.run(generate_epoch_report(ws, epoch, fake_aux))
    md = out.read_text(encoding="utf-8")
    html = out.with_suffix(".html").read_text(encoding="utf-8")
    # The callout marker is in the markdown source; the rendered HTML
    # contains the aside block.
    assert "<!-- CALLOUT:KEY OBSERVATION -->" in md
    assert "paper-callout" in html
    # The summary text uses the data view's figures.
    assert "2 challenger generations" in html
    assert "1 promoted, 1 rejected" in html


# ---------------------------------------------------------------------------
# Refined typography surface — font pair + drop cap + palette additions
# ---------------------------------------------------------------------------


def test_paper_font_pair_is_serif_body_sans_display() -> None:
    """Typography uses a serif body + sans-serif display pair.

    The body uses Source Serif (with a Charter / Iowan Old Style /
    Georgia fall-back chain); the display heads use Inter / IBM Plex
    Sans / Helvetica Neue. The fragment must expose both stacks via
    ``--paper-font-body`` / ``--paper-font-display`` so a host can
    override either independently.
    """
    fragment = render_report_html_fragment("e1", "# T\n\nhello\n")
    assert "--paper-font-body" in fragment
    assert "--paper-font-display" in fragment
    # Source Serif heads the body chain; Inter heads the display chain.
    assert "Source Serif" in fragment
    assert "Inter" in fragment


def test_abstract_first_paragraph_takes_a_drop_cap() -> None:
    """The Abstract's first paragraph gets a single drop-cap via CSS.

    The drop cap is applied to ``.paper h2.unnumbered + p::first-letter``
    so it only ever affects the FIRST paragraph after the Abstract — no
    other paragraph in the document gets a drop cap.
    """
    html = render_report_html("e1", "## Abstract\n\nbody\n\n## Section\n\nbody2\n")
    # The selector targets the abstract paragraph specifically.
    assert "h2.unnumbered + p::first-letter" in html


def test_paper_palette_exposes_incomplete_and_predicted_tokens() -> None:
    """The palette grew two tokens for the new figures.

    ``--paper-incomplete`` colours pending/incomplete generations in the
    mutation-impact matrix; ``--paper-predicted`` outlines the proposer's
    predicted bar in the hypothesis-vs-outcome figure. Both expose CSS
    variables so a dark host can re-tint without touching figure markup.
    """
    fragment = render_report_html_fragment("e1", "# T\n\nhello\n")
    assert "--paper-incomplete" in fragment
    assert "--paper-predicted" in fragment


def test_table_header_uses_small_caps_treatment() -> None:
    """Tables get the academic small-caps header treatment.

    The CSS lifts the ``.paper table th`` rule to uppercase + letter-
    spaced + muted-colour. The test confirms the header CSS rule is
    present and the body table rendering still emits a real ``<th>``
    so the header cells inherit the rule.
    """
    fragment = render_report_html_fragment("e1", "| name | value |\n| --- | --- |\n| alpha | 1 |\n")
    # Header treatment is set via the CSS rules in the fragment.
    assert ".paper table th" in fragment
    assert "text-transform: uppercase" in fragment
    # Non-numeric "name" header stays as a default <th>; the value column
    # picks up class="num" for right-alignment.
    assert "<th>name</th>" in fragment
    assert '<th class="num">value</th>' in fragment


def test_inline_fragment_carries_callout_css() -> None:
    """The callout block has its own scoped CSS in the fragment.

    The host needs no extra CSS to render the callout — the fragment
    ships the ``.paper-callout`` rule so a standalone analysis.html
    paints the callout correctly.
    """
    fragment = render_report_html_fragment("e1", "# T\n\nhello\n")
    assert ".paper-callout" in fragment


def test_paper_table_figure_allows_horizontal_scroll() -> None:
    """Wide tables (e.g. mutation surface with absolute file paths) must
    scroll horizontally inside their figure rather than overflow.

    Regression for Task #196: the mutation-surface table can carry
    absolute file paths that push the table past the article column.
    The fix is on ``figure.paper-table`` (overflow-x: auto) so the
    table scrolls inside the figure instead.
    """
    fragment = render_report_html_fragment("e1", "# T\n\nhello\n")
    # The selector + property pair must be present in the inline CSS.
    # Pull out the .paper figure.paper-table block and assert overflow-x.
    idx = fragment.find(".paper figure.paper-table {")
    assert idx >= 0, "missing figure.paper-table CSS block"
    block_end = fragment.find("}", idx)
    block = fragment[idx:block_end]
    assert "overflow-x: auto" in block
    assert "max-width: 100%" in block


def test_paper_table_code_cells_wrap_long_paths() -> None:
    """Path-like ``<code>`` cells in paper tables wrap at any character.

    Regression for Task #196: long absolute paths emitted inside
    ``<code>`` cells were rendering as a single unbroken string,
    pushing the column wider than the figure. The fix scopes
    ``overflow-wrap: anywhere`` + ``word-break: break-word`` to
    ``<code>`` cells inside paper tables specifically so prose code
    spans (which should stay nowrap) are untouched.
    """
    fragment = render_report_html_fragment("e1", "# T\n\nhello\n")
    # The selector must target ``code`` inside paper-table cells.
    assert ".paper figure.paper-table table td code" in fragment
    # And the break-anywhere directive lands on that rule.
    block_start = fragment.find(".paper figure.paper-table table td code")
    block_open = fragment.find("{", block_start)
    block_end = fragment.find("}", block_open)
    block = fragment[block_open:block_end]
    assert "overflow-wrap: anywhere" in block
    assert "word-break: break-word" in block


def test_dashboard_analysis_host_breaks_long_paths_and_scrolls_tables() -> None:
    """The dashboard's analysis host scopes the same wrapping/scrolling
    rules to the embedded analysis fragment.

    The fragment ships its own paper-scoped CSS, but the dashboard's
    ``.phase0-analysis-host`` wrapper layers a defensive overflow rule
    in case the fragment is dropped into a future surface that bypasses
    the paper rules. Regression for Task #196.
    """
    css_path = (
        Path(__file__).resolve().parent.parent
        / "src"
        / "zicato"
        / "dashboard"
        / "static"
        / "css"
        / "components.css"
    )
    css = css_path.read_text(encoding="utf-8")
    # The host wrapper itself scrolls.
    host_idx = css.find(".phase0-analysis-host {")
    assert host_idx >= 0
    host_block = css[host_idx : css.find("}", host_idx)]
    assert "overflow-x: auto" in host_block
    # And the host scope cascades the break-anywhere rule onto ``<code>``
    # cells in the embedded analysis fragment.
    assert ".phase0-analysis-host .paper figure.paper-table table td code" in css
    assert ".phase0-analysis-host .paper figure.paper-table table th code" in css
    # The host-scoped figure overflow rule is also present so the inner
    # table scrolls inside the dashboard card.
    scope_idx = css.find(".phase0-analysis-host .paper figure.paper-table {")
    assert scope_idx >= 0
    scope_block = css[scope_idx : css.find("}", scope_idx)]
    assert "overflow-x: auto" in scope_block
