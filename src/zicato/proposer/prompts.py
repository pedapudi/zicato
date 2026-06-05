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
  description, and an embedded one-shot example. The proposer-brief body
  is spliced in verbatim so the operator's free-form guidance reaches
  the model.
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

from zicato.core.types import MutationPoint, Pattern, PriorExperiment, ProposerSkill

#: Hard ceiling on a single mutation point's rendered content. A
#: ``replace`` patch MUST faithfully reproduce every part of the span it
#: is not changing — imports, markers, indentation — so the proposer
#: needs the *full* current content, never a truncated preview. This
#: ceiling exists only as a runaway-context guard for a pathologically
#: large span; it is generous enough that every real mutation point
#: (a prompt body, a docstring, a kwarg literal) is shown in full.
_MUTATION_CONTENT_LIMIT_CHARS = 8000


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
  appear in the supplied manifest. MUST NOT appear in the proposer
  brief's forbidden-edits list.
- "op" — one of "replace", "set_numeric", "set_enum".
- "new_content" (string): required when op is "replace"; forbidden
  otherwise. A "span" point is a single string literal (a prompt body,
  a tool docstring, a kwarg value). For an op="replace" on a span,
  "new_content" MUST be ONLY the replacement text for that one string
  literal — the prose / docstring body itself. Do NOT restate the
  surrounding code: no function signature, no ``import`` lines, no
  ``# zicato:mutable`` marker comment, no other mutation points. The
  harness owns the literal's quoting and indentation; you only supply
  the inner text. Emitting surrounding code here will drop imports and
  markers and the patch will be rejected. The mutation point's "current
  content" block shows you the full span you are replacing — match its
  scope exactly.
- "new_numeric" (number): required when op is "set_numeric"; forbidden
  otherwise. Must fall inside any numeric range declared in the
  mutation point's metadata ("min" / "max" keys).
- "new_enum" (string): required when op is "set_enum"; forbidden
  otherwise. Must appear in any enum domain declared in the mutation
  point's metadata ("enum" key, comma-separated).
- "rationale" (string): one sentence explaining why this specific patch
  is being applied. Joined with the broader hypothesis in the journal
  but stored per-patch.

Style — formatting expectations for "new_content":
- Break "new_content" prose into lines of roughly 80-100 characters
  using real newline characters (encoded as "\\n" inside the JSON
  string). Long unbroken single-line prompts are unreadable in the
  patch-diff view and are a known reviewer-friction point.
- Break at natural boundaries — sentence ends, clause boundaries
  before conjunctions, after a colon introducing a list — not in the
  middle of a placeholder like ``{{agent_list}}`` or an identifier.
- Do NOT add a leading or trailing blank line. Do NOT indent the
  lines — the harness re-anchors indentation when it splices the
  replacement back into the surrounding code. You only supply the
  inner text of the span.
- This style applies to any op="replace" patch whose "new_content" is
  longer than ~120 characters of prose. Short prompts (a one-line
  instruction, an enum value, a short docstring) stay on one line.

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
      "new_content": "You are the router. Route the user message to one of {{agent_list}}.\\nDo not include preambles, greetings, or explanations.\\nRespond with only the chosen agent name.",
      "rationale": "Removing preamble license should cut off_topic events at their most common entry point."
    }}
  ]
}}

Proposer brief (operator-edited guidance for this epoch):

{brief_text}
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


#: Detector ``detail`` keys that leak per-entry IDENTITY to the proposer —
#: the precise information an adversarial optimizer needs to special-case a
#: named board entry (OVERFITTING.md §1.3, §11.2-3). When the proposer's
#: visibility is restricted, these keys are removed from the rendered
#: ``detail`` and replaced by a board-wide count/rate that steers a
#: *general* fix without naming the entry. ``affected_entry_ids`` is a
#: comma-joined id list; ``entry_id`` / ``task_id`` / ``agent`` are single
#: named identities. Everything else (rates, counts, thresholds) is already
#: aggregate and renders verbatim.
_LEAKY_DETAIL_KEYS: frozenset[str] = frozenset(
    {"affected_entry_ids", "entry_id", "task_id", "agent"}
)


def _aggregate_pattern_detail(detail: dict[str, str]) -> list[str]:
    """Sanitize a pattern ``detail`` dict at the render boundary.

    Drops every per-entry IDENTITY key (:data:`_LEAKY_DETAIL_KEYS`) and, in
    its place, appends a single aggregate ``entries_affected=N`` count when
    an entry-id list was present — enough to tell the proposer *how many*
    entries a pattern touches (so it can size a general fix) without ever
    naming *which*. The remaining non-leaky keys (rates, counts,
    thresholds) pass through unchanged.

    No exact failing input strings reach the prompt: the detectors never
    put raw inputs in ``detail`` (they carry ids, counts, rates), so
    stripping the id keys leaves only aggregates — there is no input string
    to withhold here.
    """
    kept = {k: v for k, v in detail.items() if k not in _LEAKY_DETAIL_KEYS}
    parts = [f"{k}={v}" for k, v in sorted(kept.items())]
    # Surface a board-wide count derived from a verbatim entry-id list, when
    # one was present, so "how broad is this pattern" survives the
    # aggregation while "which named entries" does not.
    raw_ids = detail.get("affected_entry_ids")
    if raw_ids:
        n = len([piece for piece in raw_ids.split(",") if piece.strip()])
        parts.append(f"entries_affected={n}")
    return parts


def render_pattern_block(patterns: Iterable[Pattern], *, restrict: bool = False) -> str:
    """Render the list of patterns into the user-prompt block.

    Empty pattern lists render as a one-line "(no patterns)" notice so
    the model sees an explicit signal rather than a blank section.

    When ``restrict`` is ``True`` (the default-on
    :attr:`~zicato.core.types.OverfittingConfig.restrict_proposer_visibility`
    posture), the per-entry IDENTITY keys in each pattern's ``detail`` are
    aggregated to counts/rates via :func:`_aggregate_pattern_detail` so the
    proposer cannot special-case a named board entry — the cheapest strike
    at adversarial Goodhart (OVERFITTING.md §11). When ``restrict`` is
    ``False`` the ``detail`` dict renders verbatim, byte-for-byte as before
    this lever existed.
    """

    lines: list[str] = []
    items = list(patterns)
    if not items:
        return "(no patterns detected in the current generation)"
    for p in items:
        affected = ", ".join(p.affected_mutation_ids) if p.affected_mutation_ids else "—"
        if restrict:
            detail_parts = _aggregate_pattern_detail(dict(p.detail))
        else:
            detail_parts = [f"{k}={v}" for k, v in sorted(p.detail.items())]
        detail = "; ".join(detail_parts) if detail_parts else "—"
        lines.append(
            f"- id={p.id} kind={p.kind} severity={p.severity}\n"
            f"  summary: {p.summary}\n"
            f"  detail: {detail}\n"
            f"  affected_mutation_ids: {affected}"
        )
    return "\n".join(lines)


def _render_content(content: str) -> str:
    """Render a mutation point's current content for the prompt block.

    The full content is shown verbatim: a ``replace`` patch has to
    reproduce every byte of the span it is not editing — the surrounding
    imports stay imports, the ``# zicato:mutable`` marker stays put, the
    indentation is unchanged — and a truncated preview is exactly how a
    proposer ends up dropping the parts it cannot see.

    Only a pathologically large span (well past any real prompt body or
    docstring) is trimmed, and then only to keep one runaway point from
    swallowing the whole context window. The trim is annotated so the
    proposer knows it is NOT seeing the full span and must not emit a
    ``replace`` blindly.
    """

    if len(content) <= _MUTATION_CONTENT_LIMIT_CHARS:
        return content
    head = content[:_MUTATION_CONTENT_LIMIT_CHARS].rstrip()
    return (
        f"{head}\n"
        f"[... truncated: span exceeds {_MUTATION_CONTENT_LIMIT_CHARS} chars; "
        "do not emit a `replace` for this point without the full content ...]"
    )


def render_mutation_block(mutations: Iterable[MutationPoint]) -> str:
    """Render the mutation-point manifest into the user-prompt block."""

    lines: list[str] = []
    items = list(mutations)
    if not items:
        return "(no mutation points available)"
    for mp in items:
        meta_keys = sorted(mp.metadata.keys())
        meta_render = "; ".join(f"{k}={mp.metadata[k]}" for k in meta_keys) if meta_keys else "—"
        content = _render_content(mp.content)
        # Indent the full content under a "current content:" lead-in so
        # the model can see exactly what it is replacing.
        indented = textwrap.indent(content, "    ")
        lines.append(
            f"- id={mp.id} kind={mp.kind} file={mp.file} "
            f"lines={mp.line_start}-{mp.line_end}\n"
            f"  metadata: {meta_render}\n"
            f"  current content (full — a `replace` MUST preserve every part "
            f"you are not changing):\n{indented}"
        )
    return "\n".join(lines)


def _bucket_scalar_delta(delta: float) -> str:
    """Coarsen a fine-grained Δscalar to a memorization-resistant bucket.

    The scalar is a LOSS (lower is better), so a *negative* delta is an
    improvement. Returns ``improved`` / ``flat`` / ``regressed``. The flat
    band is the default promote margin (``0.01``) so a within-noise move
    reads as ``flat`` — the proposer keeps the build-on-wins / avoid-
    failures signal without the exact response-surface gradient that lets
    it climb the *board* rather than true quality (OVERFITTING.md §11.4).
    """
    flat_band = 0.01
    if delta < -flat_band:
        return "improved"
    if delta > flat_band:
        return "regressed"
    return "flat"


def _render_prior_experiment_line(pe: PriorExperiment, *, restrict: bool = False) -> str:
    """Render one prior experiment as a two-line compact entry.

    The first line carries the verdict, Δscalar (omitted for an in-flight
    sibling or a cross-contract entry whose number does not transfer), and
    the bracketed targeted mutation-point ids (plus the symbolic rejection
    reason for a rejected entry); the second line is the indented core
    idea. See ``docs/design/EXPERIMENT-MEMORY.md`` §3.5.

    When ``restrict`` is ``True``, the fine-grained ``Δscalar`` number is
    coarsened to an ``improved`` / ``flat`` / ``regressed`` bucket
    (OVERFITTING.md §11.4) so the proposer cannot read the board's exact
    response surface round-over-round. When ``False`` the precise number
    renders verbatim, byte-for-byte as before this lever existed.
    """
    ids = ", ".join(pe.modulating) if pe.modulating else "—"
    verdict = pe.decision.upper().replace("_", "-")
    # A cross-contract entry's Δscalar is measured against a different
    # board and is not comparable, so it is omitted; an in-flight sibling
    # has no outcome yet.
    if pe.scalar_score_delta is not None and pe.same_contract:
        if restrict:
            head = (
                f"- {pe.generation_id} {verdict} "
                f"Δscalar={_bucket_scalar_delta(pe.scalar_score_delta)}  [{ids}]"
            )
        else:
            head = f"- {pe.generation_id} {verdict} Δscalar={pe.scalar_score_delta:+.3f}  [{ids}]"
    else:
        head = f"- {pe.generation_id} {verdict}  [{ids}]"
    if pe.decision == "rejected" and pe.rejection_reason:
        head = f"{head}  ({pe.rejection_reason})"
    return f"{head}\n    {pe.core_idea}"


def render_prior_experiments_block(
    prior: Iterable[PriorExperiment], *, restrict: bool = False
) -> str:
    """Render the experiment-memory section body, or ``""`` when empty.

    Groups the prior experiments by decision into the three compact
    blocks of ``docs/design/EXPERIMENT-MEMORY.md`` §3.5 — promoted wins
    first (build on these), then rejected failures (do not re-propose
    unless something changed), then in-flight siblings minted this round
    (diversify away from these). An empty input returns the empty string —
    the proposer-side sentinel for "omit this section entirely", exactly
    as the insights and pattern blocks behave.

    The framing is deliberately advisory: it does not constrain the
    proposer (forbidden-ids remains the only hard gate), it surfaces what
    has already been attempted so re-proposing a rejected direction is a
    deliberate choice rather than an accident of amnesia.
    """
    items = list(prior)
    if not items:
        return ""

    promoted = [pe for pe in items if pe.decision == "promoted"]
    rejected = [pe for pe in items if pe.decision == "rejected"]
    in_flight = [pe for pe in items if pe.decision == "in_flight"]
    deferred = [pe for pe in items if pe.decision == "deferred"]

    blocks: list[str] = []
    if promoted:
        body = "\n".join(_render_prior_experiment_line(pe, restrict=restrict) for pe in promoted)
        blocks.append(f"Already promoted (build on these — the direction worked):\n{body}")
    if rejected:
        body = "\n".join(_render_prior_experiment_line(pe, restrict=restrict) for pe in rejected)
        blocks.append(
            f"Already rejected (do NOT re-propose these unless something changed):\n{body}"
        )
    if deferred:
        body = "\n".join(_render_prior_experiment_line(pe, restrict=restrict) for pe in deferred)
        blocks.append(f"Deferred (neither won decisively — weak signal):\n{body}")
    if in_flight:
        body = "\n".join(_render_prior_experiment_line(pe, restrict=restrict) for pe in in_flight)
        blocks.append(
            f"Proposed this round, not yet evaluated (diversify away from these):\n{body}"
        )

    return "\n\n".join(blocks)


def render_skills_block(skills: Iterable[ProposerSkill]) -> str:
    """Render the proposer's skill modules into a system-prompt section body.

    Each skill becomes a ``### <name> — <description>`` heading followed by
    its body; consecutive skills are separated by a blank line. An empty
    skills iterable returns the empty string — the proposer-side sentinel
    for "omit the skills section entirely", mirroring the insights and
    prior-experiments blocks. A skill with no description renders just its
    name in the heading (no trailing em-dash).
    """
    items = list(skills)
    if not items:
        return ""
    blocks: list[str] = []
    for skill in items:
        heading = f"### {skill.name}"
        if skill.description:
            heading = f"{heading} — {skill.description}"
        blocks.append(f"{heading}\n{skill.body}")
    return "\n\n".join(blocks)


def render_system_prompt(
    brief_text: str,
    skills: tuple[ProposerSkill, ...] = (),
) -> str:
    """Build the system prompt with the proposer-brief body spliced in.

    The proposer-brief body is inserted verbatim so the operator's prose
    guidance reaches the model alongside the structured forbidden /
    preferred lists.

    ``skills`` are the resolved proposer skill modules (see
    :class:`zicato.core.types.ProposerSkill`). When non-empty, a
    ``Proposer skills`` section is appended AFTER the brief block so the
    operator's composable guidance modules reach the model as operating
    procedure for the epoch. Empty (the default) appends nothing — a caller
    that supplies no skills renders a byte-identical prompt to before this
    surface existed, so every standalone caller is unaffected.
    """

    base = SYSTEM_PROMPT_TEMPLATE.format(brief_text=brief_text.strip() or "(empty)")
    skills_block = render_skills_block(skills)
    if not skills_block:
        return base
    return (
        f"{base}\n"
        "Proposer skills (composable guidance modules — follow them as "
        "operating procedure for this epoch):\n\n"
        f"{skills_block}\n"
    )


def render_user_prompt(
    *,
    current_loss_summary: str,
    patterns: Iterable[Pattern],
    mutations: Iterable[MutationPoint],
    feedback: str = "",
    insights: str = "",
    prior_experiments: Iterable[PriorExperiment] = (),
    restrict_visibility: bool = False,
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
    insights:
        Optional markdown body produced by the decision-telemetry
        analyzer (see :func:`zicato.analyzer.load_latest_insights`).
        When non-empty, a ``## Recent telemetry insights`` section is
        prepended to the body so the next round's proposer sees the
        previous round's LLM-summarised observations alongside the
        detector patterns.
    prior_experiments:
        Optional iterable of :class:`PriorExperiment` — the
        experiment-memory digest (settled cross-round history plus this
        round's in-flight siblings) assembled by the orchestrator. When
        non-empty, a ``## What's already been tried`` section is inserted
        after ``## Recent telemetry insights`` and before
        ``## Current loss summary`` so the proposer can avoid repeating
        known failures and build on known wins. Empty (the default) omits
        the section entirely, mirroring the insights and pattern blocks —
        so a caller that supplies no prior experiments renders a
        byte-identical prompt to before this surface existed.
    restrict_visibility:
        When ``True`` (the default-on
        :attr:`~zicato.core.types.OverfittingConfig.restrict_proposer_visibility`
        posture), the pattern block aggregates per-entry identities to
        counts/rates and the experiment-memory Δscalar is coarsened to
        buckets (OVERFITTING.md §11). ``False`` (the default here so call
        sites that have not adopted the flag are unaffected) renders both
        verbatim, byte-for-byte as before this lever existed.
    """

    body = USER_PROMPT_TEMPLATE.format(
        current_loss_summary=current_loss_summary.strip() or "(no loss summary)",
        pattern_block=render_pattern_block(patterns, restrict=restrict_visibility),
        mutation_block=render_mutation_block(mutations),
    )
    prior_block = render_prior_experiments_block(prior_experiments, restrict=restrict_visibility)
    if prior_block:
        prior_prefix = (
            "## What's already been tried (this epoch — avoid repeating "
            f"failures, build on wins)\n\n{prior_block}\n\n"
        )
        body = prior_prefix + body
    if insights.strip():
        insights_prefix = f"## Recent telemetry insights\n{insights.strip()}\n\n"
        body = insights_prefix + body
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
    "render_prior_experiments_block",
    "render_skills_block",
    "render_system_prompt",
    "render_user_prompt",
]
