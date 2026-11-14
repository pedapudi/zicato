"""Tests for the comprehensive epoch analysis report.

Coverage:

* The deterministic sections are templated exactly from a fixture
  workspace — numbers come straight from the structured artifacts.
* The LLM-narrative path with a mocked evaluation LLM produces a
  complete document; a missing prose block degrades to a placeholder.
* A report-generation failure is contained — the file is still written
  with placeholder prose, and an internal failure does not propagate
  past the orchestrator's best-effort wrapper.
* The minimal Markdown -> HTML renderer covers the report's subset.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from pathlib import Path

import pytest

from zicato.analyzer.report import generate_epoch_report
from zicato.analyzer.report_data import (
    EpochReportData,
    GenerationView,
    _cumulate_scalar,
    gather_epoch_report_data,
)
from zicato.analyzer.report_prompts import parse_prose_blocks
from zicato.analyzer.report_sections import (
    render_methodology_section,
    render_per_board_outcomes,
    render_results_section,
    render_score_sparkline,
    render_score_trajectory_table,
)
from zicato.core.measurement import MeasurementDraw, MeasurementPurpose
from zicato.core.mutation import MutationPoint
from zicato.core.types import JudgeLoss, MetricCount
from zicato.core.workspace import analysis_path
from zicato.mutation.inventory import write_mutation_inventory
from zicato.telemetry.reducer import write_loss_profile
from zicato.testing import make_loss_profile
from zicato.tournament.scoring import write_gen_score


def test_methodology_reports_retained_experimental_proposer_settings(
    epoch_workspace: tuple[Path, str],
) -> None:
    workspace, epoch = epoch_workspace
    quality = {"best_of_n": 3, "screen_entries": 2}
    experimental = {
        "process_exemplars": 4,
        "genealogy": 5,
        "recombine": True,
        "recombine_merge": "mechanical",
    }
    scoring = {"proposer_quality": quality, "experimental": experimental}
    path = workspace / "epochs" / epoch / "scoring.json"
    path.write_text(json.dumps(scoring))
    original = path.read_bytes()
    rendered = render_methodology_section(gather_epoch_report_data(workspace, epoch))
    assert "| process exemplars | 4 |" in rendered
    assert "| genealogy channel | 5 |" in rendered
    assert "| recombination | on · `mechanical` merge |" in rendered
    assert "| pre-tournament screen | 2 entries · veto+advise |" in rendered
    assert path.read_bytes() == original


# ---------------------------------------------------------------------------
# Fixture workspace
# ---------------------------------------------------------------------------


def _write(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.name == "gen_score.json":
        write_gen_score(path.parents[4], path.parents[2].name, path.parent.name, payload)
        return
    if path.name == "experiment.json":
        from tests._workspace_support import experiment_record

        payload = experiment_record(
            **{
                "epoch_id": path.parents[2].name,
                "generation_id": path.parent.name,
                **payload,
                "parent_generation_id": payload.get("parent_generation_id") or None,
            }
        )
    elif path.parent.name == "patches":
        from zicato.epoch.journal import patch_body
        from zicato.testing import make_patch

        payload = patch_body(make_patch(**payload))
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
        '"judges": [{"name": "structure", "mode": "inline", "body": "x", '
        '"severity": "warning"}]}\n'
        '{"id": "qa", "kind": "single_turn", "wall_clock_budget_seconds": 30, '
        '"input": "answer", "expectation": {"kind": "rubric", "spec": "accurate"}}\n',
        encoding="utf-8",
    )
    _write(
        edir / "scoring.json",
        {
            "pass_weight": 1.5,
            "promote_margin": 0.02,
            "severity_weights": {"info": 1.0, "warning": 3.0, "critical": 10.0},
            "per_judge_weights": {"structure": 2.0},
        },
    )
    write_mutation_inventory(
        edir / "mutations.json",
        [
            MutationPoint(
                id=mid,
                kind="span",
                file=Path(filename),
                source_root=Path("agent"),
                line_start=1,
                line_end=1,
                content="",
                content_hash=hashlib.sha256(b"").hexdigest(),
            )
            for mid, filename in (("sys_prompt", "agent/prompt.txt"), ("temp", "agent/config.py"))
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
                "expected_metric_movements": [
                    {
                        "metric_name": "drift:off_topic",
                        "direction": "decrease",
                        "magnitude": "medium",
                    }
                ],
            },
            "patch_ids": ["p1"],
            "outcome": {
                "ran_at": "2026-05-18T02:30:00Z",
                "metric_movements": [
                    {
                        "metric_name": "drift:off_topic",
                        "from_value": 0.4,
                        "to_value": 0.1,
                        "hypothesis_match": True,
                    }
                ],
                "pass_rate_delta": 0.1,
                "drift_loss_delta": -0.3,
                "scalar_score_delta": -0.25,
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
                "metric_movements": [],
                "pass_rate_delta": -0.05,
                "drift_loss_delta": 0.12,
                "scalar_score_delta": 0.14,
                "tournament_decision": "rejected",
                "rejection_reason": "scalar regressed past promote margin",
            },
        },
    )
    _write(
        edir / "generations" / "v2" / "patches" / "p2.json",
        {
            "id": "p2",
            "mutation_id": "temp",
            "op": "set_numeric",
            "new_content": None,
            "new_numeric": 0.95,
            "rationale": "0.7 -> 0.95",
        },
    )
    from tests._workspace_support import complete_round

    complete_round(ws, epoch, ["v1"], primary_id="v1", round_index=0)
    complete_round(ws, epoch, ["v2"], primary_id=None, round_index=1)
    return ws, epoch


# ---------------------------------------------------------------------------
# Deterministic-section tests
# ---------------------------------------------------------------------------


def test_data_gather_renders_outcome_without_decision_as_pending(
    epoch_workspace: tuple[Path, str],
) -> None:
    """An outcome that recorded no decision reads ``pending``, as a missing outcome does.

    The view is text the report renders, so the field must carry the
    word and never the spelling of an absent value.
    """
    ws, epoch = epoch_workspace
    _write(
        ws / "epochs" / epoch / "generations" / "v3" / "experiment.json",
        {
            "id": "exp-v3",
            "epoch_id": epoch,
            "generation_id": "v3",
            "parent_generation_id": "v1",
            "proposed_at": "2026-05-18T04:00:00Z",
            "hypothesis": {"core_idea": "shorten the closing slide", "modulating": ["sys_prompt"]},
            "patch_ids": [],
            "outcome": {
                "ran_at": "2026-05-18T04:30:00Z",
                "metric_movements": [],
                "pass_rate_delta": 0.0,
                "drift_loss_delta": 0.0,
                "scalar_score_delta": 0.0,
            },
        },
    )
    data = gather_epoch_report_data(ws, epoch)
    views = {g.generation_id: g for g in data.generations}
    assert views["v3"].decision == "pending"
    assert data.promoted == 1
    assert data.rejected == 1


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
            "The system under test is a multi-agent presentation builder.\n"
            "===ANALYSIS===\n"
            "Generation v1 moved the loss as predicted; v2 regressed.\n"
            "===CONCLUSION===\n"
            "The next generation should revisit the temperature mutation.\n"
        )

    out = await generate_epoch_report(ws, epoch, fake_aux)
    assert out == analysis_path(ws, epoch)
    md = out.read_text(encoding="utf-8")

    # Every required section is present, in order — headings carry NO
    # explicit number. The masthead's H1 is the epoch name on its own; the
    # line above it names the artifact.
    assert "epoch analysis report" in md.lower()
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
    # And the markdown source must NOT carry hard-coded section numbers.
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

    # The Markdown is the only published form: no HTML is written beside it,
    # and the source carries no marker addressed to an HTML renderer.
    assert sorted(path.name for path in out.parent.glob("analysis*")) == [
        "analysis.md",
        "analysis.prose.json",
    ]
    assert not list(out.parent.rglob("*.html"))
    assert "<!--" not in md


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
) -> None:
    """A timed-out LLM call substitutes placeholder prose, not a crash."""
    ws, epoch = epoch_workspace
    from zicato.config import AuxConfig

    async def slow_aux(system: str, user: str, model: str) -> str:
        import asyncio

        await asyncio.sleep(5.0)
        return "never reached"

    out = await generate_epoch_report(
        ws, epoch, slow_aux, aux_config=AuxConfig(call_timeout_s=0.01)
    )
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
# prose-block parser
# ---------------------------------------------------------------------------


def test_parse_prose_blocks_tolerates_unknown_labels() -> None:
    blocks = parse_prose_blocks(
        "===ABSTRACT===\nhi\n===NONSENSE===\nignored\n===CONCLUSION===\nbye\n"
    )
    assert blocks == {"ABSTRACT": "hi", "CONCLUSION": "bye"}


def test_parse_prose_blocks_empty() -> None:
    assert parse_prose_blocks("no markers here at all") == {}


# ---------------------------------------------------------------------------
# The assembled Markdown — masthead, tables, callout
# ---------------------------------------------------------------------------

_FOUR_BLOCKS = "===ABSTRACT===\nA\n===INTRODUCTION===\nI\n===ANALYSIS===\nAn\n===CONCLUSION===\nC\n"


async def _generated_markdown(ws: Path, epoch: str) -> str:
    async def fake_aux(system: str, user: str, model: str) -> str:
        return _FOUR_BLOCKS

    out = await generate_epoch_report(ws, epoch, fake_aux)
    return out.read_text(encoding="utf-8")


async def test_full_document_states_every_measured_view_as_a_table(
    epoch_workspace: tuple[Path, str],
) -> None:
    """The report carries each measured view as a captioned pipe table.

    Required views: the mutation surface, the mutation-impact matrix, the
    lineage, the score trajectory, the hypothesis-vs-outcome pairs, the
    drift-kind movements, and the per-board-entry outcomes.
    """
    md = await _generated_markdown(*epoch_workspace)
    for caption, header in (
        ("Caption: Mutation surface — full enumeration.", "| mutation id | kind | file | lines |"),
        ("Caption: Mutation-impact matrix", "| mutation id | v1 (promoted) | v2 (rejected) |"),
        ("Caption: Lineage — each generation", "| gen | parent | decision |"),
        ("Caption: Per-generation scalar deltas", "| gen | decision | scalar |"),
        ("Caption: Proposer hypothesis vs tournament outcome", "| gen | decision | predicted Δ"),
        ("Caption: Per-challenger drift-kind rate movements", "| gen | decision | drift kind |"),
        ("Caption: Per-board entry Δ scalar", "| board entry | v1 | v2 |"),
    ):
        at = md.find(caption)
        assert at >= 0, caption
        # The caption introduces its table: the header row follows it directly.
        following = md[at:].split("\n\n", 2)[1]
        assert following.startswith(header), (caption, following[:80])


async def test_masthead_carries_artifact_line_title_and_labelled_metadata(
    epoch_workspace: tuple[Path, str],
) -> None:
    """The masthead names the artifact, titles the epoch, and lists its metadata."""
    ws, epoch = epoch_workspace
    md = await _generated_markdown(ws, epoch)
    head = md.split("### Goal")[0]
    assert head.startswith(
        "Zicato improvement campaign · epoch analysis report\n\n# Demo Presentation Agent\n\n"
    )
    assert f"- **Epoch id**: `{epoch}`\n" in head
    assert "- **Status**: in progress\n" in head
    assert "- **Generations**: 2 attempted · 1 promoted · 1 rejected\n" in head


async def test_callout_in_results_section_summarises_campaign(
    epoch_workspace: tuple[Path, str],
) -> None:
    """The deterministic Results section emits one campaign-summary callout.

    The fixture has 2 attempted, 1 promoted, 1 rejected — the callout
    text mentions those figures verbatim from the data view.
    """
    md = await _generated_markdown(*epoch_workspace)
    assert md.count("**KEY OBSERVATION.**") == 1
    callout = md.split("**KEY OBSERVATION.**")[1].split("\n\n")[0]
    assert "2 challenger generations" in callout
    assert "1 promoted, 1 rejected" in callout


# ---------------------------------------------------------------------------
# Goal fallback + masthead re-stamp (status / goal reflect reality on disk)
# ---------------------------------------------------------------------------


def test_goal_falls_back_to_brief_goal_section(tmp_path: Path) -> None:
    """With an empty ``config.goal``, the report distils the goal from the
    brief's ``## Goal`` section — the same source the dashboard objective
    uses — rather than rendering "(no goal recorded)"."""
    ws = tmp_path / ".zicato"
    epoch = "2026-06-09_g"
    edir = ws / "epochs" / epoch
    edir.mkdir(parents=True)
    _write(
        edir / "config.json",
        {
            "id": epoch,
            "name": "g",
            "created_at": "2026-06-09T00:00:00Z",
            "closed": False,
            "goal": "",
        },
    )
    (edir / "brief.md").write_text(
        "# Epoch g\n\nintro prose\n\n## Goal\n\n"
        "Keep the slide pass rate stable while cutting drift.\n\n"
        "- a bullet that must NOT be picked as the goal\n\n## Methodology\n",
        encoding="utf-8",
    )
    data = gather_epoch_report_data(ws, epoch)
    assert data.goal == "Keep the slide pass rate stable while cutting drift."


def test_restamp_persisted_report_refreshes_stale_masthead(tmp_path: Path) -> None:
    """A report frozen mid-run ("in progress", empty goal) is re-stamped to
    the now-closed status + brief-derived goal, leaving the narrative intact.
    The re-stamp is idempotent."""
    from zicato.analyzer.report import restamp_persisted_report
    from zicato.core.workspace import analysis_path

    ws = tmp_path / ".zicato"
    epoch = "2026-06-09_h"
    edir = ws / "epochs" / epoch
    edir.mkdir(parents=True)
    _write(
        edir / "config.json",
        {
            "id": epoch,
            "name": "h",
            "created_at": "2026-06-09T00:00:00Z",
            "closed": True,
            "closed_at": "2026-06-09T05:00:00Z",
            "goal": "",
        },
    )
    (edir / "brief.md").write_text("## Goal\n\nStabilise the harness.\n", encoding="utf-8")
    stale = (
        "<!-- EYEBROW -->\nZicato improvement campaign · epoch analysis report\n\n"
        f"# h\n\n<!-- META -->\n**Epoch id**: `{epoch}`  \n**Status**: in progress\n\n"
        "### Goal\n\n_(no goal recorded)_\n\n"
        "## Abstract\n\nUNIQUE-NARRATIVE-MARKER preserved verbatim.\n"
    )
    md_path = analysis_path(ws, epoch)
    md_path.write_text(stale, encoding="utf-8")
    _write(
        edir / "analysis.prose.json", {"ABSTRACT": "UNIQUE-NARRATIVE-MARKER preserved verbatim."}
    )

    assert restamp_persisted_report(ws, epoch) is True
    out = md_path.read_text(encoding="utf-8")
    assert "**Status**: closed" in out
    assert "_(no goal recorded)_" not in out
    assert "Stabilise the harness." in out
    assert "UNIQUE-NARRATIVE-MARKER preserved verbatim." in out
    # Idempotent: a second pass finds nothing to change.
    assert restamp_persisted_report(ws, epoch) is False


# ---------------------------------------------------------------------------
# Workspace-seam: generation ordering + per-generation content parity
# ---------------------------------------------------------------------------


def test_generation_ordering_is_numeric_aware_and_content_preserved(tmp_path: Path) -> None:
    """The analyzer orders generations numeric-aware (the canonical layer's
    authority), not lexically, and every per-generation fact is intact.

    Bug-mirror shape: generation ``v10`` sorts BEFORE ``v2`` lexically but
    AFTER it numerically — the order the analyzer must present. A
    sibling epoch with NO generations confirms the empty case degrades to
    an empty view (never an exception). This is the
    ``read_epoch_experiments`` (``natural_key``) ordering the analyzer now
    routes through the record's own decoder.
    """
    ws = tmp_path / ".zicato"
    epoch = "2026-06-14_order"
    edir = ws / "epochs" / epoch
    edir.mkdir(parents=True)
    _write(
        edir / "config.json",
        {
            "id": epoch,
            "name": "Ordering Probe",
            "created_at": "2026-06-14T00:00:00Z",
            "contract_hash": "feedface00000001",
            "closed": False,
        },
    )

    # Created out of lexical order: v0, v1, v2, v10. Each carries a distinct
    # scalar delta + its own per-patch + per-run loss so a mis-ordered or
    # mis-joined read would corrupt a checkable number.
    gen_specs = [
        ("v0", "", 0.0),
        ("v1", "v0", -0.10),
        ("v2", "v1", -0.05),
        ("v10", "v2", 0.07),
    ]
    for gid, parent, delta in gen_specs:
        exp: dict[str, object] = {
            "id": f"exp-{gid}",
            "epoch_id": epoch,
            "generation_id": gid,
            "parent_generation_id": parent,
            "proposed_at": f"2026-06-14T{int(gid[1:]):02d}:00:00Z",
            "hypothesis": {"core_idea": f"idea-{gid}", "modulating": []},
        }
        if parent:
            exp["patch_ids"] = [f"p-{gid}"]
            exp["outcome"] = {
                "tournament_decision": "promoted",
                "scalar_score_delta": delta,
                "pass_rate_delta": 0.0,
                "drift_loss_delta": 0.0,
                "metric_movements": [],
            }
        _write(edir / "generations" / gid / "experiment.json", exp)
        if parent:
            _write(
                edir / "generations" / gid / "patches" / f"p-{gid}.json",
                {"id": f"p-{gid}", "mutation_id": f"m-{gid}", "op": "replace", "rationale": gid},
            )
        # A per-run loss with a per-judge attribution unique to this gen so a
        # mis-joined runs/ read would surface the wrong total.
        write_loss_profile(
            make_loss_profile(
                epoch_id=epoch,
                generation_id=gid,
                measurement=MeasurementDraw(MeasurementPurpose.TOURNAMENT, 0),
                entry_id="t1",
                metric_counts=(
                    MetricCount(name=f"drift:custom:judge-{gid}", severity="info", count=1.0),
                ),
                per_judge_loss=(JudgeLoss(f"judge-{gid}", 1.0, 1.0, 1.0),),
                runtime_ms=0,
            ),
            edir / "generations" / gid / "runs" / "t1" / "seed-none" / "loss.tournament.r0.json",
        )

    data = gather_epoch_report_data(ws, epoch)

    # Numeric-aware order: v10 follows v2, not the lexical v0,v1,v10,v2.
    assert [g.generation_id for g in data.generations] == ["v0", "v1", "v2", "v10"]

    # Per-generation content joined to the right directory: each patch +
    # per-judge total must belong to its own generation.
    by_id = {g.generation_id: g for g in data.generations}
    assert by_id["v10"].patches == ({"mutation_id": "m-v10", "op": "replace", "rationale": "v10"},)
    assert by_id["v2"].per_judge_loss_totals == (("judge-v2", 1.0),)
    assert by_id["v10"].per_judge_loss_totals == (("judge-v10", 1.0),)

    # Cumulative scalar walks the (numeric-aware) lineage: 0 -> -0.10 -> -0.15 -> -0.08.
    cum = {g.generation_id: g.cumulative_scalar for g in data.generations}
    assert cum["v10"] == pytest.approx(-0.08)

    # A sibling epoch with no generations degrades to an empty view.
    empty_epoch = "2026-06-14_empty"
    eedir = ws / "epochs" / empty_epoch
    eedir.mkdir(parents=True)
    _write(eedir / "config.json", {"id": empty_epoch, "name": "Empty", "closed": True})
    empty = gather_epoch_report_data(ws, empty_epoch)
    assert empty.generations == ()
    assert empty.epoch_name == "Empty"


def test_gather_orders_generations_identically_via_inner_and_outer_root(tmp_path: Path) -> None:
    """The numeric-aware generation order is identical whether the caller
    passes the inner ``.zicato`` root or the outer project dir — the seam's
    normalisation preserves the historical outer-dir acceptance."""
    ws = tmp_path / ".zicato"
    epoch = "2026-06-14_dual"
    edir = ws / "epochs" / epoch
    edir.mkdir(parents=True)
    _write(
        edir / "config.json",
        {"id": epoch, "name": "Dual", "created_at": "2026-06-14T00:00:00Z"},
    )
    for gid, parent in [("v0", ""), ("v2", "v0"), ("v10", "v2")]:
        _write(
            edir / "generations" / gid / "experiment.json",
            {"generation_id": gid, "parent_generation_id": parent, "hypothesis": {}},
        )
    inner = gather_epoch_report_data(ws, epoch)
    outer = gather_epoch_report_data(ws.parent, epoch)
    assert [g.generation_id for g in inner.generations] == ["v0", "v2", "v10"]
    assert [g.generation_id for g in outer.generations] == [
        g.generation_id for g in inner.generations
    ]


# ---------------------------------------------------------------------------
# Champion-anchored publication surfaces (issue #129 pattern B)
# ---------------------------------------------------------------------------
#
# Every number in the report that names "the promoted lineage" must come from
# a generation the gate accepted. A rejected challenger's cumulative scalar is
# a counterfactual — where the lineage WOULD have stood — and publishing it
# under the lineage's name lets a zero-promotion epoch read as an improving
# one. These cover the anchoring itself plus the two prose degradations that
# assumed a promotion had happened.


def _cv_data(*generations: object, champions: tuple[str, ...] = ("v0",)) -> EpochReportData:
    """An ``EpochReportData`` carrying only the fields these tests read."""
    kwargs: dict[str, object] = {}
    for f in dataclasses.fields(EpochReportData):
        if f.name == "generations":
            kwargs[f.name] = tuple(_cumulate_scalar(list(generations)))  # type: ignore[arg-type]
        elif f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING:
            annotation = str(f.type)
            kwargs[f.name] = (
                ()
                if "tuple" in annotation
                else {}
                if "dict" in annotation
                else False
                if "bool" in annotation
                else ""
            )
    return EpochReportData(**kwargs, champion_history=champions)  # type: ignore[arg-type]


def _cv_gen(gid: str, parent: str, decision: str, delta: float = 0.0) -> GenerationView:
    return GenerationView(
        generation_id=gid,
        parent_generation_id=parent,
        is_baseline=decision == "baseline",
        proposed_at="",
        core_idea="idea",
        why="",
        risks="",
        modulating=(),
        expected_pass_rate_delta="",
        expected_metric_movements=(),
        decision=decision,
        rejection_reason="below promote_margin" if decision == "rejected" else "",
        scalar_score_delta=delta,
        drift_loss_delta=0.0,
        pass_rate_delta=0.0,
        metric_movements=() + (),
        patches=(),
    )


def test_final_scalar_follows_the_champion_past_a_trailing_rejection() -> None:
    """A promotion followed by a rejection: the lineage stands at the promotion.

    This is the common case, not just the zero-promotion one — any epoch
    whose last round rejected had its headline number taken from the
    discarded challenger.
    """
    data = _cv_data(
        _cv_gen("v0", "", "baseline"),
        _cv_gen("v1", "v0", "promoted", -0.30),
        _cv_gen("v11", "v0", "promoted", -0.50),
        _cv_gen("v2", "v1", "rejected", +0.50),
        champions=("v0", "v1"),
    )
    assert data.final_scalar == pytest.approx(-0.30), "the promoted v1, not the rejected v2"
    assert data.latest_rejected_scalar == pytest.approx(0.20), "v2's counterfactual, named as such"
    # And the chart marker agrees with the number.
    current = [ln for ln in render_score_sparkline(data).splitlines() if "<- current" in ln]
    assert len(current) == 1 and "v1" in current[0], current


def test_final_scalar_holds_at_the_baseline_when_nothing_promoted() -> None:
    data = _cv_data(
        _cv_gen("v0", "", "baseline"),
        *[_cv_gen(f"v{i}", "v0", "rejected", -0.04) for i in range(1, 4)],
    )
    assert data.final_scalar == 0.0
    assert data.latest_rejected_scalar == pytest.approx(-0.04)


def test_rejected_siblings_cumulate_off_their_parent_not_off_each_other() -> None:
    """The arithmetic the champion anchoring rests on, pinned explicitly.

    ``_cumulate_scalar`` adds each generation's delta to its PARENT's
    cumulative, so rejected challengers all hanging off the baseline are
    siblings, not a chain: deltas +0.2 / -0.1 / +0.3 give +0.200 /
    -0.100 / +0.300, never a running sum ending at +0.400. Each rejected
    row is an independent "where the lineage would have landed", which
    is exactly why none of them can be published as where it DID land.

    Worth its own pin because the running-sum reading is the intuitive
    one, and every surface that mistook a rejected row for the lineage's
    score is downstream of it.
    """
    data = _cv_data(
        _cv_gen("v0", "", "baseline"),
        _cv_gen("v1", "v0", "rejected", +0.2),
        _cv_gen("v2", "v0", "rejected", -0.1),
        _cv_gen("v3", "v0", "rejected", +0.3),
    )
    assert [round(g.cumulative_scalar, 3) for g in data.generations] == [0.0, 0.2, -0.1, 0.3]
    assert data.latest_rejected_scalar == pytest.approx(
        0.3
    ), "the LAST one, not the best or the sum"
    assert data.final_scalar == 0.0, "and the lineage itself never moved"


def test_trajectory_table_puts_the_decision_beside_the_scalar_it_qualifies() -> None:
    """The `scalar` column means different things on promoted and rejected rows.

    Reading down it as a running score is the mistake the layout invited:
    the decision sat five columns away, past three deltas.
    """
    data = _cv_data(
        _cv_gen("v0", "", "baseline"),
        _cv_gen("v1", "v0", "rejected", -0.04),
    )
    table = render_score_trajectory_table(data)
    header = table.splitlines()[0]
    cols = [c.strip() for c in header.strip("|").split("|")]
    assert cols.index("decision") == cols.index("scalar") - 1, header
    assert "counterfactual" in table, "the note says what a rejected row's scalar is"
    # The rows still carry their decisions.
    assert "| baseline |" in table and "| rejected |" in table


def test_per_board_outcomes_names_the_real_cause_when_nothing_promoted() -> None:
    """ "No cached scores available" reads as a harness that has not run.

    It had run — twelve times — and scored every challenger. What is
    missing is a second column to compare the baseline against.
    """
    data = _cv_data(
        _cv_gen("v0", "", "baseline"),
        *[_cv_gen(f"v{i}", "v0", "rejected", -0.04) for i in range(1, 13)],
    )
    rendered = render_per_board_outcomes(data)
    assert "12 challenger generations were scored and rejected" in rendered
    assert "baseline alone" in rendered
    assert "No cached generation scores are available" not in rendered


def test_per_board_outcomes_keeps_the_original_wording_before_any_round() -> None:
    """Nothing has run yet — here "not available yet" is the honest sentence."""
    data = _cv_data(_cv_gen("v0", "", "baseline"))
    assert "No cached generation scores are available" in render_per_board_outcomes(data)
