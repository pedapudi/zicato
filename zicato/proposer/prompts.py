# ruff: noqa: E501
# This module is a prompt template — several lines inside the embedded
# one-shot JSON example exceed the project line limit by design. Breaking
# the example across lines would change what the model sees, so the
# whole file is exempted from E501 rather than splitting prompt content.
"""System + user prompt templates for the structured proposer.

The proposer is asked to emit a single JSON object containing a typed
hypothesis and a list of patches. The schema description in the system
prompt is verbose by design — LLM compliance with JSON-only output
improves materially when the prompt is explicit about what counts as a
valid response and shows a one-shot worked example.

Two layers of templating:

* :data:`SYSTEM_PROMPT_TEMPLATE` — operator-tone scaffolding, schema
  description, and an embedded one-shot example. The rubric body is
  spliced in verbatim so the operator's free-form guidance reaches the
  model.
* :data:`USER_PROMPT_TEMPLATE` — per-round payload: loss summary,
  observed patterns, mutation-point manifest. The body is filled in by
  the orchestrator at call time.

Rendering helpers (:func:`render_pattern_block`,
:func:`render_mutation_block`, :func:`render_system_prompt`,
:func:`render_user_prompt`) keep the formatting logic out of the
orchestrator.
"""

from __future__ import annotations

import textwrap
from collections.abc import Iterable

from zicato.core.types import MutationPoint, Pattern

_MUTATION_PREVIEW_CHARS = 240


SYSTEM_PROMPT_TEMPLATE = """\
You are a careful improvement-proposer for a multi-agent system. Your
job is to look at how the current generation of the inner harness has
been performing on the evaluation board, decide what to change next,
and emit a single JSON object describing one experiment.

You will receive in the user message:
- A short human-readable summary of the current generation's loss.
- A list of cross-run patterns observed by automated detectors. Each
  pattern carries a kind, a summary, structured detail, and a suggested
  set of mutation-point ids that might be relevant.
- A list of mutation points available for edit. Each mutation point has
  a stable id, a kind ("span" or "file"), a file path, a snippet of its
  current content, and optional metadata constraints (numeric ranges,
  enum domains, required placeholders). ONLY these ids are valid patch
  targets.

You will return a single JSON object (no surrounding prose, no
markdown fences) with two top-level keys: "hypothesis" and "patches".

The "hypothesis" object MUST contain:
- "core_idea" (string): one sentence describing what is being modulated.
- "modulating" (array of strings, non-empty): the mutation-point ids
  this hypothesis is touching. Every id MUST exist in the supplied
  manifest.
- "why" (string): pattern-driven rationale — why you believe this edit
  will move the loss in the expected direction.
- "expected_drift_movements" (array, optional): per-drift-kind
  directional predictions. Each entry is an object with:
    * "kind" (string) — a registered goldfive drift-kind string
      (e.g. "off_topic", "looping_reasoning", "tool_error").
    * "direction" — one of "decrease", "increase", "neutral",
      "decrease_or_neutral", "increase_or_neutral".
    * "magnitude" — one of "small", "medium", "large".
  Include only kinds you are making claims about; silence implies
  "no claim".
- "expected_metric_movements" (array, optional): per-namespaced-metric
  directional predictions. Generalises expected_drift_movements to
  arbitrary metric namespaces beyond drift (cost, rubric, latency,
  output, schema, ...). Each entry is an object with:
    * "metric_name" (string) — a namespaced metric name like
      "drift:off_topic", "cost:tokens_spent", "rubric:slide_structure",
      "latency:p95_turn_ms", or "schema:failures".
    * "direction" — same enum as expected_drift_movements.
    * "magnitude" — same enum as expected_drift_movements.
  Either expected_drift_movements OR expected_metric_movements (or
  both) MUST be present and non-empty. Prefer expected_metric_movements
  for cost / rubric / latency / schema / output objectives.
- "expected_pass_rate_delta" (string): predicted change in the board-
  wide pass rate as free text (e.g. "+0.05 to +0.15"). Free text
  is intentional — express the uncertainty band naturally.
- "risks" (string, optional): one-paragraph description of failure
  modes you anticipate and any mitigations baked into the patches.

The "patches" array MUST contain at least one patch object. Each patch
has:
- "mutation_id" (string): the id of the target mutation point. MUST
  appear in the supplied manifest. MUST NOT appear in the rubric's
  forbidden-edits list.
- "op" — one of "replace", "set_numeric", "set_enum".
- "new_content" (string): required when op is "replace"; forbidden
  otherwise.
- "new_numeric" (number): required when op is "set_numeric"; forbidden
  otherwise. Must fall inside any numeric range declared in the
  mutation point's metadata ("min" / "max" keys).
- "new_enum" (string): required when op is "set_enum"; forbidden
  otherwise. Must appear in any enum domain declared in the mutation
  point's metadata ("enum" key, comma-separated).
- "rationale" (string): one sentence explaining why this specific patch
  is being applied. Joined with the broader hypothesis in the journal
  but stored per-patch.

A response that is NOT a single JSON object matching this schema will
be rejected and you will be asked to retry. Do not wrap the JSON in
markdown code fences. Do not preface the JSON with prose. The first
character of your response MUST be "{{" and the last MUST be "}}".

One-shot example of a valid response:

{{
  "hypothesis": {{
    "core_idea": "Tighten the router's instruction to stop relaying off-topic preambles.",
    "modulating": ["router__system_prompt"],
    "why": "Pattern 'drift_kind_frequency' shows off_topic dominates this generation; the router's system prompt invites unbounded preambles.",
    "expected_drift_movements": [
      {{"kind": "off_topic", "direction": "decrease", "magnitude": "medium"}},
      {{"kind": "looping_reasoning", "direction": "neutral", "magnitude": "small"}}
    ],
    "expected_pass_rate_delta": "+0.05 to +0.10",
    "risks": "Tightening may suppress legitimate clarifying preambles; if pass rate regresses, the next round can relax the constraint."
  }},
  "patches": [
    {{
      "mutation_id": "router__system_prompt",
      "op": "replace",
      "new_content": "You are the router. Route the user message to one of {{agent_list}}. Do not include preambles, greetings, or explanations.",
      "rationale": "Removing preamble license should cut off_topic events at their most common entry point."
    }}
  ]
}}

Rubric (operator-edited guidance for this epoch):

{rubric_text}
"""


USER_PROMPT_TEMPLATE = """\
## Current loss summary
{current_loss_summary}

## Patterns observed (advisory; you may address none, some, or all)
{pattern_block}

## Mutation points (only these ids are valid patch targets)
{mutation_block}

Propose ONE experiment now. Respond with the JSON object only — no
surrounding prose, no markdown fences. The first character of your
response MUST be "{{" and the last MUST be "}}".
"""


def render_pattern_block(patterns: Iterable[Pattern]) -> str:
    """Render the list of patterns into the user-prompt block.

    Empty pattern lists render as a one-line "(no patterns)" notice so
    the model sees an explicit signal rather than a blank section.
    """

    lines: list[str] = []
    items = list(patterns)
    if not items:
        return "(no patterns detected in the current generation)"
    for p in items:
        affected = ", ".join(p.affected_mutation_ids) if p.affected_mutation_ids else "—"
        detail_parts = [f"{k}={v}" for k, v in sorted(p.detail.items())]
        detail = "; ".join(detail_parts) if detail_parts else "—"
        lines.append(
            f"- id={p.id} kind={p.kind} severity={p.severity}\n"
            f"  summary: {p.summary}\n"
            f"  detail: {detail}\n"
            f"  affected_mutation_ids: {affected}"
        )
    return "\n".join(lines)


def _preview_content(content: str) -> str:
    """Trim long mutation-point content for the prompt block.

    Multi-line content keeps its line breaks but is truncated to
    :data:`_MUTATION_PREVIEW_CHARS`. The model gets enough surface to
    reason about the edit without bloating the context.
    """

    if len(content) <= _MUTATION_PREVIEW_CHARS:
        return content
    return content[: _MUTATION_PREVIEW_CHARS - 1].rstrip() + "…"


def render_mutation_block(mutations: Iterable[MutationPoint]) -> str:
    """Render the mutation-point manifest into the user-prompt block."""

    lines: list[str] = []
    items = list(mutations)
    if not items:
        return "(no mutation points available)"
    for mp in items:
        meta_keys = sorted(mp.metadata.keys())
        meta_render = "; ".join(f"{k}={mp.metadata[k]}" for k in meta_keys) if meta_keys else "—"
        snippet = _preview_content(mp.content)
        # Indent multi-line content under a "content:" lead-in.
        indented = textwrap.indent(snippet, "    ")
        lines.append(
            f"- id={mp.id} kind={mp.kind} file={mp.file} "
            f"lines={mp.line_start}-{mp.line_end}\n"
            f"  metadata: {meta_render}\n"
            f"  content:\n{indented}"
        )
    return "\n".join(lines)


def render_system_prompt(rubric_text: str) -> str:
    """Build the system prompt with the rubric body spliced in.

    The rubric body is inserted verbatim so the operator's prose
    guidance reaches the model alongside the structured forbidden /
    preferred lists.
    """

    return SYSTEM_PROMPT_TEMPLATE.format(rubric_text=rubric_text.strip() or "(empty)")


def render_user_prompt(
    *,
    current_loss_summary: str,
    patterns: Iterable[Pattern],
    mutations: Iterable[MutationPoint],
    feedback: str = "",
) -> str:
    """Build the user prompt for one proposer call.

    Parameters
    ----------
    current_loss_summary:
        Short free-text summary of the previous generation's losses.
    patterns:
        Iterable of :class:`Pattern` to surface to the proposer.
    mutations:
        Iterable of :class:`MutationPoint` — the valid patch targets.
    feedback:
        Optional retry feedback. When non-empty, an extra section is
        prepended explaining the previous parse failure so the model
        can correct itself.
    """

    body = USER_PROMPT_TEMPLATE.format(
        current_loss_summary=current_loss_summary.strip() or "(no loss summary)",
        pattern_block=render_pattern_block(patterns),
        mutation_block=render_mutation_block(mutations),
    )
    if feedback:
        prefix = (
            "## Previous attempt was rejected\n"
            "Your previous response failed to parse. Reason:\n\n"
            f"    {feedback}\n\n"
            "Re-emit a single JSON object that conforms to the schema.\n\n"
        )
        return prefix + body
    return body


__all__ = [
    "SYSTEM_PROMPT_TEMPLATE",
    "USER_PROMPT_TEMPLATE",
    "render_pattern_block",
    "render_mutation_block",
    "render_system_prompt",
    "render_user_prompt",
]
