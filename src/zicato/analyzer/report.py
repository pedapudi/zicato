"""Publish an epoch report from measured results and recorded interpretation.

Authored paper sections and the close-of-epoch retrospective are stored in
``analysis.prose.json``. Deterministic refresh combines those blocks with the
structured epoch view and its tables to produce ``analysis.md``, the one
published form of the report. The console's Publication view renders that
Markdown. The prose record and the report are replaced atomically and
separately; publication across the two files is not a transaction.

The paper contains Abstract, Introduction, Methodology, Approach &
Implementation, Experimental Results, Statistical Integrity, Proposer
Analytics, Analysis, Threats to Validity & Limitations, and Conclusion.
Recorded retrospective sections and their computed tournament outcomes join
the same assembly.

``report_sections`` templates measured facts from ``EpochReportData``.
``report_prompts`` provides the
bounded evaluation prompt for the paper's four interpretive blocks. A failed
paper-authoring request preserves recorded blocks; absent blocks receive an
unavailable notice. Deterministic refresh makes no evaluation call.

The round hook catches report-generation failures so reporting cannot abort
optimization. Malformed prose and failures reading inputs or writing
Markdown can propagate to the caller. Report
artifacts are separate from per-round proposer feedback in ``insights/``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path

from zicato.analyzer.report_data import EpochReportData, gather_epoch_report_data
from zicato.analyzer.report_prompts import (
    PROSE_BLOCK_LABELS,
    REPORT_SYSTEM_PROMPT,
    parse_prose_blocks,
    render_report_user_prompt,
)
from zicato.analyzer.report_sections import (
    render_approach_section,
    render_methodology_section,
    render_proposer_analytics_section,
    render_results_section,
    render_statistical_integrity_section,
    render_threats_section,
    render_title_block,
)
from zicato.aux_timeout import aux_call_timeout_s
from zicato.core.settings import AuxConfig
from zicato.core.types import Experiment
from zicato.core.workspace import analysis_path
from zicato.storage import atomic_write_json, atomic_write_text

log = logging.getLogger("zicato.analyzer.report")

# A goldfive-compatible evaluation call_llm: (system, user, model) -> str.
_AuxCallLLM = Callable[[str, str, str], Awaitable[str]]

# Placeholder prose used when the LLM omits a block or the call fails.
_MISSING_PROSE = "_(prose section unavailable — the evaluation LLM did not return it this round.)_"


def _placeholder_blocks() -> dict[str, str]:
    """Prose blocks substituted when the evaluation LLM call fails entirely."""
    return {label: _MISSING_PROSE for label in PROSE_BLOCK_LABELS}


def assemble_report_markdown(
    data: EpochReportData,
    prose: dict[str, str],
    deterministic_sections: str,
) -> str:
    """Render recorded interpretation beside deterministic tables."""

    def section(title: str, label: str) -> str:
        body = prose.get(label, _MISSING_PROSE).strip() or _MISSING_PROSE
        return f"## {title}\n\n{body}"

    parts = [
        render_title_block(data),
        section("Abstract", "ABSTRACT"),
        section("Introduction", "INTRODUCTION"),
        deterministic_sections.strip(),
        section("Analysis — What Worked and What Didn't", "ANALYSIS"),
        render_threats_section(data),
        section("Conclusion & Next Directions", "CONCLUSION"),
    ]
    if prose.get("retrospective"):
        parts.append(prose["retrospective"].strip())
    parts.append(
        f"_Report for epoch `{data.epoch_id}`. Tables derive from "
        "the recorded board, scoring, mutations, experiments, and journal. "
        "Interpretive sections contain the recorded evaluation narrative._"
    )
    return "\n\n".join(parts) + "\n"


def _deterministic_sections(data: EpochReportData) -> str:
    """Render the contiguous deterministic block (Methodology .. Results).

    These are the sections that sit between the Introduction and the
    Analysis in the final document. The Title and Threats sections are
    rendered separately because they bracket prose sections.
    """
    return "\n\n".join(
        (
            render_methodology_section(data),
            render_approach_section(data),
            render_results_section(data),
            render_statistical_integrity_section(data),
            render_proposer_analytics_section(data),
        )
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def regenerate_epoch_report_deterministic(
    workspace_root: Path,
    epoch_id: str,
    *,
    authored: dict[str, str] | None = None,
    data: EpochReportData | None = None,
    recorded_experiments: tuple[list[tuple[str, Experiment]], list[str]] | None = None,
) -> bool:
    """Publish the Markdown report from recorded prose and measured results.

    Authored sections are stored directly. Refreshing tables never parses or
    edits model output. A failed model request leaves existing prose intact.
    """
    md_path = analysis_path(workspace_root, epoch_id)
    prose_path = md_path.with_name("analysis.prose.json")
    try:
        prose = json.loads(prose_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        prose = {}
    if not isinstance(prose, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in prose.items()
    ):
        raise ValueError(f"report prose must contain named text sections: {prose_path}")
    if authored:
        prose.update({k: v for k, v in authored.items() if v and v != _MISSING_PROSE})
        atomic_write_json(prose_path, prose)
    if prose.get("retrospective") and recorded_experiments is None:
        from zicato.epoch.journal import read_epoch_experiments

        recorded_experiments = read_epoch_experiments(workspace_root, epoch_id)
    if data is None:
        data = gather_epoch_report_data(
            workspace_root, epoch_id, recorded_experiments=recorded_experiments
        )
    deterministic = _deterministic_sections(data)
    if prose.get("retrospective"):
        from zicato.epoch.analysis import _generations_for, render_tournament_outcomes_section

        assert recorded_experiments is not None
        records, _unreadable = recorded_experiments
        experiments = [experiment for _, experiment in records]
        generations = _generations_for(workspace_root, epoch_id)
        deterministic += "\n\n" + render_tournament_outcomes_section(generations, experiments)
    report_md = assemble_report_markdown(data, prose, deterministic)
    existing = md_path.read_text(encoding="utf-8") if md_path.exists() else ""
    changed = report_md != existing
    if changed:
        atomic_write_text(md_path, report_md, mode=None)
    return changed


def restamp_persisted_report(workspace_root: Path, epoch_id: str) -> bool:
    """Refresh an existing report after the epoch closes."""
    if not analysis_path(workspace_root, epoch_id).exists():
        return False
    return regenerate_epoch_report_deterministic(workspace_root, epoch_id)


async def generate_epoch_report(
    workspace_root: Path,
    epoch_id: str,
    aux_call_llm: _AuxCallLLM,
    model: str = "",
    aux_config: AuxConfig | None = None,
) -> Path:
    """Regenerate the comprehensive epoch analysis report.

    Gather the structured epoch view and deterministic sections, then request
    the four interpretive blocks in one bounded evaluation call. Successful
    blocks update ``analysis.prose.json``. The shared assembler publishes
    the Markdown report from recorded prose and measured results.

    Timeout or authoring failure preserves existing blocks. Missing blocks
    receive unavailable notices. Input and Markdown publication failures can
    propagate to the caller. The round hook catches report errors so reporting
    cannot abort optimization.

    Parameters
    ----------
    workspace_root:
        Absolute path to the ``.zicato/`` workspace root.
    epoch_id:
        The epoch whose report should be regenerated.
    aux_call_llm:
        The EVALUATION LLM callable (see :class:`RuntimeConfig` and the
        collusion guard) — never the target callable.
    model:
        Optional model identifier forwarded verbatim to *aux_call_llm*.

    Returns
    -------
    Path
        Absolute path of the written ``analysis.md``.
    """
    data = gather_epoch_report_data(workspace_root, epoch_id)
    deterministic = _deterministic_sections(data)

    user_prompt = render_report_user_prompt(data, deterministic)
    try:
        response = await asyncio.wait_for(
            aux_call_llm(REPORT_SYSTEM_PROMPT, user_prompt, model),
            timeout=aux_call_timeout_s(aux_config),
        )
        prose = parse_prose_blocks(response)
        if not prose:
            log.debug(
                "epoch report: evaluation LLM returned no parseable prose blocks; "
                "substituting placeholders"
            )
            prose = _placeholder_blocks()
    except TimeoutError:
        log.warning(
            "epoch report: evaluation LLM timed out after %.1fs; "
            "writing report with placeholder prose",
            aux_call_timeout_s(aux_config),
        )
        prose = _placeholder_blocks()
    except Exception as exc:  # noqa: BLE001 — opaque LLM errors are common
        log.warning(
            "epoch report: evaluation LLM call failed (%s: %s); "
            "writing report with placeholder prose",
            type(exc).__name__,
            exc,
        )
        prose = _placeholder_blocks()

    regenerate_epoch_report_deterministic(workspace_root, epoch_id, authored=prose, data=data)
    return analysis_path(workspace_root, epoch_id)


__all__ = [
    "generate_epoch_report",
    "assemble_report_markdown",
    "restamp_persisted_report",
    "regenerate_epoch_report_deterministic",
]
