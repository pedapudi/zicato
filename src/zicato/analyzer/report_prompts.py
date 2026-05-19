"""System + user prompts for the epoch analysis report's prose sections.

The comprehensive epoch report is a hybrid artifact. The data-bearing
sections are templated deterministically from the workspace
(:mod:`zicato.analyzer.report_sections`); the *interpretive* sections —
the Abstract, the Introduction prose, the What-Worked/What-Didn't
Analysis, and the Conclusion — are written by the auxiliary LLM.

This module renders the prompt for that single bounded LLM call. The
user prompt hands the model the complete structured data view plus the
already-rendered deterministic sections, and asks for exactly four
fenced prose blocks. The model is told, emphatically, never to invent
or restate a number — the deterministic sections own every figure.
"""

from __future__ import annotations

import json

from zicato.analyzer.report_data import EpochReportData

#: The four prose blocks the LLM must return, in this order, each on its
#: own labelled fence. Parsed back out by :mod:`zicato.analyzer.report`.
PROSE_BLOCK_LABELS: tuple[str, ...] = (
    "ABSTRACT",
    "INTRODUCTION",
    "ANALYSIS",
    "CONCLUSION",
)


REPORT_SYSTEM_PROMPT = """\
You are the lead author of an academic-style technical report on one
epoch of an automated multi-agent-harness improvement campaign. The
report reads like an ACM-journal paper: precise, cogent, evidence-led.

You are writing ONLY the interpretive prose sections. The data-bearing
sections — Methodology, every Experimental Results table, the score
trajectory — are already rendered deterministically from the workspace
and will be inserted around your prose. You will be shown them.

Hard rules:
- NEVER invent, estimate, or restate a numeric value. Every number
  lives in the deterministic sections. Refer to trends and directions
  ("the scalar fell across the promoted lineage", "two of three
  hypotheses regressed"); do not reproduce specific figures.
- Ground every claim in the structured data you are given. Cite
  generation ids (e.g. `v3`) when discussing specific generations.
- Be specific and concrete. Prefer observations over generalities.
- Do not write section headings — the caller supplies them. Do not
  write Methodology, Results, or Threats prose; those are not yours.

Return EXACTLY four blocks, each fenced like this and in this order:

===ABSTRACT===
<one tight paragraph: the epoch's aim and its findings so far>
===INTRODUCTION===
<2-4 paragraphs: the inner multi-agent harness under improvement, the
epoch's goal as expressed in the proposer brief, and the motivation>
===ANALYSIS===
<3-6 paragraphs of interpretation: which proposer hypotheses moved the
loss as predicted versus regressed and why; cross-generation patterns;
the proposer's hit rate; what the evidence does and does not support>
===CONCLUSION===
<1-3 paragraphs: what this epoch has established so far, and concrete,
specific directions for the next generation or the next epoch>

Emit nothing outside the four fenced blocks.
"""


def _data_digest(data: EpochReportData) -> str:
    """Build a compact JSON digest of the structured epoch data.

    This is the factual substrate the LLM interprets. It mirrors the
    deterministic sections exactly — same numbers, same ids — so the
    model never has a reason to compute or guess a value.
    """
    digest = {
        "epoch_id": data.epoch_id,
        "epoch_name": data.epoch_name,
        "status": "closed" if data.closed else "in_progress",
        "generations_attempted": data.attempted,
        "promoted": data.promoted,
        "rejected": data.rejected,
        "deferred": data.deferred,
        "final_cumulative_scalar": round(data.final_scalar, 4),
        "board_entry_count": len(data.board_entries),
        "board_entries": [
            {
                "id": e.id,
                "kind": e.kind,
                "weight": e.weight,
                "expectation_kind": e.expectation_kind,
                "judges": list(e.judges),
            }
            for e in data.board_entries
        ],
        "scoring": data.scoring,
        "mutation_surface_size": len(data.mutation_surface),
        "generations": [
            {
                "id": g.generation_id,
                "parent": g.parent_generation_id,
                "is_baseline": g.is_baseline,
                "decision": g.decision,
                "core_idea": g.core_idea,
                "why": g.why,
                "risks": g.risks,
                "expected_pass_rate_delta": g.expected_pass_rate_delta,
                "expected_drift_movements": list(g.expected_drift_movements),
                "scalar_score_delta": round(g.scalar_score_delta, 4),
                "drift_loss_delta": round(g.drift_loss_delta, 4),
                "pass_rate_delta": round(g.pass_rate_delta, 4),
                "cumulative_scalar": round(g.cumulative_scalar, 4),
                "rejection_reason": g.rejection_reason,
                "drift_movements": [dict(m) for m in g.drift_movements],
                "metric_movements": [dict(m) for m in g.metric_movements],
                "patches": [dict(p) for p in g.patches],
            }
            for g in data.generations
        ],
    }
    return json.dumps(digest, indent=2, sort_keys=True, default=str)


def render_report_user_prompt(
    data: EpochReportData,
    deterministic_sections: str,
) -> str:
    """Assemble the user prompt for the report's prose-writing LLM call.

    Parameters
    ----------
    data:
        The structured epoch view — serialised into a JSON digest so the
        model interprets the exact figures the deterministic sections
        render.
    deterministic_sections:
        The already-rendered deterministic markdown (title, methodology,
        approach, results, threats). Handed to the model verbatim so its
        prose stays consistent with what the reader will see — and so it
        does not re-derive any number.
    """
    chunks: list[str] = []
    chunks.append(f"# Epoch under analysis: {data.epoch_id}")
    chunks.append("")
    chunks.append("## Operator's proposer brief (the epoch's goal)")
    chunks.append("")
    chunks.append("```")
    chunks.append(data.brief_text.strip() or "(no proposer brief recorded)")
    chunks.append("```")
    chunks.append("")
    chunks.append("## Structured epoch data (authoritative — do not restate numbers)")
    chunks.append("")
    chunks.append("```json")
    chunks.append(_data_digest(data))
    chunks.append("```")
    chunks.append("")
    chunks.append("## Running journal")
    chunks.append("")
    chunks.append("```")
    chunks.append(data.journal_text.strip() or "(no journal entries yet)")
    chunks.append("```")
    chunks.append("")
    chunks.append("## Deterministic report sections already rendered (for your context)")
    chunks.append("")
    chunks.append(deterministic_sections.strip())
    chunks.append("")
    chunks.append(
        "Now write the four prose blocks (ABSTRACT, INTRODUCTION, ANALYSIS, "
        "CONCLUSION) per the system prompt. Refer to the deterministic "
        "sections and the structured data; do not reproduce any number."
    )
    return "\n".join(chunks)


def parse_prose_blocks(response: str) -> dict[str, str]:
    """Split an LLM response into its four labelled prose blocks.

    The model is asked to fence each block with ``===LABEL===`` markers.
    This parser is tolerant: any well-formed ``===...===`` line is
    treated as a delimiter (so it never bleeds into a block's text), but
    only the four recognised labels open a named block — an unknown
    label closes the current block and is otherwise ignored. A missing
    block simply does not appear in the result dict; the caller
    substitutes a placeholder for it.
    """
    blocks: dict[str, str] = {}
    current: str | None = None
    buf: list[str] = []

    def _flush() -> None:
        nonlocal current, buf
        if current is not None:
            blocks[current] = "\n".join(buf).strip()
        current = None
        buf = []

    for raw_line in response.splitlines():
        stripped = raw_line.strip()
        is_marker = stripped.startswith("===") and stripped.endswith("===") and len(stripped) > 6
        if is_marker:
            label = stripped.strip("=").strip().upper()
            _flush()
            if label in PROSE_BLOCK_LABELS:
                current = label
            continue
        if current is not None:
            buf.append(raw_line)
    _flush()
    return blocks


__all__ = [
    "PROSE_BLOCK_LABELS",
    "REPORT_SYSTEM_PROMPT",
    "render_report_user_prompt",
    "parse_prose_blocks",
]
