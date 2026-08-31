"""The served ``analysis.html`` comes from one renderer at every epoch phase.

``epochs/{id}/analysis.html`` is the document ``file://`` readers and the
dashboard's static fallback open. Two passes write it — the per-round
refresh (driven by the round epilogue and by the evolve loop) and the write
at epoch close — and both hand the markdown to
:func:`zicato.analyzer.report.render_report_html`. These tests pin that: in
each phase the file on disk is byte-identical to that renderer's output over
the ``analysis.md`` beside it, so the served page cannot depend on which
phase last wrote it.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from zicato.analyzer.report import render_report_html
from zicato.analyzer.report_data import gather_epoch_report_data
from zicato.core.types import ScoringWeights
from zicato.core.workspace import analysis_path, experiment_json_path
from zicato.epoch import new_epoch
from zicato.epoch.lifecycle import close_epoch, close_epoch_async
from zicato.evolve.round_reporting import _regenerate_epoch_report

EPOCH_NAME = "one-renderer"


async def _unused_aux(system: str, user: str, model: str) -> str:  # pragma: no cover
    raise AssertionError("the deterministic refresh must not call the auxiliary LLM")


async def _stub_aux(system: str, user: str, model: str) -> str:
    """A retrospective narrative carrying every section the close pass expects."""
    return (
        "## Headline movements\n\n- The challenger held.\n\n"
        "## Hypotheses that held\n\n- Tightening the prompt cut off-topic drift.\n\n"
        "## Hypotheses that didn't\n\n- None.\n\n"
        "## Surface still open at epoch close\n\n- The retrieval step.\n\n"
        "## Recommended focus for next epoch\n\n- Retrieval.\n"
    )


@pytest.fixture()
def epoch(tmp_path: Path) -> tuple[Path, str]:
    """A workspace holding one open epoch with a settled promoted challenger."""
    ws = tmp_path / ".zicato"
    ws.mkdir()
    board = tmp_path / "board.jsonl"
    board.write_text(
        '{"id": "e1", "kind": "single_turn", "wall_clock_budget_seconds": 60, '
        '"input": "hi", "weight": 1.0}\n',
        encoding="utf-8",
    )
    brief = tmp_path / "brief.md"
    brief.write_text("## Goal\n\nCut off-topic drift.\n", encoding="utf-8")
    cfg = new_epoch(ws, EPOCH_NAME, board, brief, ScoringWeights())

    for generation_id, payload in (
        ("v0", {"hypothesis": {"core_idea": "baseline"}}),
        (
            "v1",
            {
                "hypothesis": {
                    "core_idea": "tighten the prompt",
                    "expected_pass_rate_delta": "+0.05 to +0.15",
                },
                "outcome": {
                    "ran_at": "2026-08-30T00:00:01Z",
                    "pass_rate_delta": 0.10,
                    "drift_loss_delta": -0.20,
                    "scalar_score_delta": -0.20,
                    "tournament_decision": "promoted",
                    "drift_movements": [
                        {
                            "kind": "off_topic",
                            "from_rate": 0.40,
                            "to_rate": 0.20,
                            "hypothesis_match": True,
                        }
                    ],
                },
            },
        ),
    ):
        path = experiment_json_path(ws, cfg.id, generation_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "id": f"exp-{generation_id}",
                    "epoch_id": cfg.id,
                    "generation_id": generation_id,
                    "parent_generation_id": "" if generation_id == "v0" else "v0",
                    "proposed_at": "2026-08-30T00:00:00Z",
                    **payload,
                }
            ),
            encoding="utf-8",
        )
    return ws, cfg.id


def _served(ws: Path, epoch_id: str) -> str:
    return analysis_path(ws, epoch_id).with_suffix(".html").read_text(encoding="utf-8")


def _rendered_from_the_markdown(ws: Path, epoch_id: str) -> str:
    """What the analyzer's renderer produces from the ``analysis.md`` on disk."""
    report_md = analysis_path(ws, epoch_id).read_text(encoding="utf-8")
    return render_report_html(epoch_id, report_md, data=gather_epoch_report_data(ws, epoch_id))


async def test_round_refresh_serves_the_renderers_output(epoch: tuple[Path, str]) -> None:
    """After a round, the file is the renderer's output over the report beside it."""
    ws, epoch_id = epoch
    await _regenerate_epoch_report(ws, epoch_id, _unused_aux, "")

    served = _served(ws, epoch_id)
    assert served == _rendered_from_the_markdown(ws, epoch_id)
    assert served.startswith("<!DOCTYPE html>")
    assert '<article class="paper">' in served


async def test_close_serves_the_same_renderers_output(epoch: tuple[Path, str]) -> None:
    """The at-close document comes from the renderer the round refresh used."""
    ws, epoch_id = epoch
    await _regenerate_epoch_report(ws, epoch_id, _unused_aux, "")
    in_progress = _served(ws, epoch_id)

    await close_epoch_async(ws, epoch_id=epoch_id, aux_call_llm=_stub_aux)

    at_close = _served(ws, epoch_id)
    assert at_close == _rendered_from_the_markdown(ws, epoch_id)
    # The close pass replaces the report, so the two documents differ in
    # content; both carry the one renderer's envelope.
    assert at_close != in_progress
    for document in (in_progress, at_close):
        assert document.startswith("<!DOCTYPE html>")
        assert '<article class="paper">' in document
        assert f"zicato — epoch {epoch_id} analysis report" in document
        # House style for the served document: no emoji decision markers.
        for codepoint in ("✅", "❌", "\U0001f389", "\U0001f4ca"):
            assert codepoint not in document


def test_at_close_document_carries_the_report_figures(epoch: tuple[Path, str]) -> None:
    """The retrospective's figure markers resolve to the analyzer's inline SVG."""
    ws, epoch_id = epoch
    close_epoch(ws, epoch_id=epoch_id, aux_call_llm=_stub_aux)

    report_md = analysis_path(ws, epoch_id).read_text(encoding="utf-8")
    assert "<!-- FIGURE:lineage -->" in report_md
    assert "<!-- FIGURE:score-trajectory -->" in report_md

    served = _served(ws, epoch_id)
    assert served.count("<svg") >= 2
    assert "<!-- FIGURE:" not in served


def test_close_without_an_auxiliary_llm_serves_the_renderers_output(
    epoch: tuple[Path, str],
) -> None:
    """The no-LLM close path writes a stub report and renders it the same way."""
    ws, epoch_id = epoch
    close_epoch(ws, epoch_id=epoch_id, aux_call_llm=None)

    assert "this is a stub" in analysis_path(ws, epoch_id).read_text(encoding="utf-8")
    assert _served(ws, epoch_id) == _rendered_from_the_markdown(ws, epoch_id)


def test_only_the_analyzer_renders_the_epoch_html() -> None:
    """No second renderer of ``analysis.html`` ships alongside the analyzer's."""
    assert importlib.util.find_spec("zicato.epoch.html_report") is None
