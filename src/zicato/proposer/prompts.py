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

import re
import textwrap
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING

from zicato.analyzer.outcome_marginals import OutcomeMarginalSummary
from zicato.analyzer.process_exemplars import ProcessExemplar
from zicato.core.drift_kinds import GOLDFIVE_DRIFT_KINDS
from zicato.core.types import MutationPoint, Patch, Pattern, PriorExperiment, ProposerSkill
from zicato.proposer.genealogy import GenealogyItem
from zicato.proposer.recombine import RecombinationPair

if TYPE_CHECKING:  # pragma: no cover - typing-only import
    from zicato.index.query import MutationTrackRecord

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
  IMPORTANT — declared board judges: to predict a DECLARED BOARD JUDGE
  moving (a judge named in this board's scoring), set "metric_name" to
  the judge's BARE name (e.g. "file_findability") — NOT "drift:<name>",
  "custom:<name>", or "drift:custom:<name>". The user message lists this
  board's declared judges and the valid built-in drift kinds under
  "## Valid expectation targets"; reference only names from that section.
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

## Valid expectation targets (what a hypothesis movement may reference)
{metric_targets_block}

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


def render_mutation_track_annotation(record: MutationTrackRecord) -> str:
    """Render one mutation point's track record as a compact advisory line.

    The proposer-facing surface of the mutation-point fertility map
    (:func:`zicato.index.query.mutation_point_track_record`). BANDED and
    aggregate-only, inside the restricted-visibility envelope:

    * the touch / promotion counts are board-anonymous aggregates (they
      count experiments, exactly like the pattern block's
      ``entries_affected=N``);
    * the Δscalar summary is coarsened through :func:`_bucket_scalar_delta`
      — ``improved`` / ``flat`` / ``regressed`` bands, never the exact
      experiment-level delta (the same memorization-resistance treatment
      the experiment-memory digest applies; OVERFITTING.md §11.4);
    * recency is a coarse ``recent`` / ``stale`` flag (the query's
      documented last-K window), never a round number.

    HONESTY (load-bearing): the line ALWAYS reads "experiments touching
    this point" and names how many of them also touched other points
    (multi-patch experiments confound per-point credit) — it is never
    phrased as the point's causal effect.
    """
    n = record.experiments_touching
    parts = [f"touched:{n}", f"promoted:{record.promoted}/{n}"]
    if (
        record.delta_min is not None
        and record.delta_median is not None
        and record.delta_max is not None
    ):
        parts.append(
            f"Δscalar[best:{_bucket_scalar_delta(record.delta_min)} "
            f"median:{_bucket_scalar_delta(record.delta_median)} "
            f"worst:{_bucket_scalar_delta(record.delta_max)}]"
        )
    parts.append("recent" if record.recent_touching > 0 else "stale")
    if record.confounded_experiments > 0:
        basis = (
            f"{record.confounded_experiments}/{n} also touched other points — " "credit confounded"
        )
    else:
        basis = "each touched only this point"
    return f"{' '.join(parts)} (experiments touching this point; {basis}; not causal)"


def render_mutation_block(
    mutations: Iterable[MutationPoint],
    track_records: Mapping[str, MutationTrackRecord] | None = None,
) -> str:
    """Render the mutation-point manifest into the user-prompt block.

    ``track_records`` — when supplied — annotates each manifest entry that
    has one with its compact, banded track-record line
    (:func:`render_mutation_track_annotation`): advisory context on how
    experiments touching that point have fared this epoch. ``None`` / empty
    (the default, and every point without a record) renders the manifest
    byte-identically to before the surface existed.
    """

    lines: list[str] = []
    items = list(mutations)
    if not items:
        return "(no mutation points available)"
    records = track_records or {}
    for mp in items:
        meta_keys = sorted(mp.metadata.keys())
        meta_render = "; ".join(f"{k}={mp.metadata[k]}" for k in meta_keys) if meta_keys else "—"
        record = records.get(mp.id)
        track_line = (
            f"  track record: {render_mutation_track_annotation(record)}\n"
            if record is not None
            else ""
        )
        content = _render_content(mp.content)
        # Indent the full content under a "current content:" lead-in so
        # the model can see exactly what it is replacing.
        indented = textwrap.indent(content, "    ")
        lines.append(
            f"- id={mp.id} kind={mp.kind} file={mp.file} "
            f"lines={mp.line_start}-{mp.line_end}\n"
            f"  metadata: {meta_render}\n"
            f"{track_line}"
            f"  current content (full — a `replace` MUST preserve every part "
            f"you are not changing):\n{indented}"
        )
    return "\n".join(lines)


def render_metric_targets_block(custom_judge_names: Iterable[str] = ()) -> str:
    """Render the board's valid metric-movement targets for the user prompt.

    Enumerates, for THIS board, exactly what a hypothesis movement may
    reference so the proposer can write a movement that validates without
    guessing:

    * the declared board judges (e.g. ``file_findability``) — addressed by
      their BARE name in ``expected_metric_movements[].metric_name`` (NOT a
      ``drift:custom:<name>`` mangle), since a custom judge emits its
      goldfive signal under the single ``"custom"`` drift kind but is named
      by its own judge name in a hypothesis; and
    * the valid built-in goldfive drift kinds — addressed as
      ``drift:<kind>``.

    The judge names are passed in by the orchestrator from the active
    contract (board ``JudgeSpec.name`` ∪ ``per_judge_weights`` keys); the
    drift-kind set is the registered goldfive mirror. The validator in
    :func:`zicato.proposer.structured.parse_experiment_json` accepts exactly
    these forms, so the prompt and the gate agree by construction. An empty
    judge set renders an explicit "(this board declares no custom judges)"
    notice so the model sees the absence as a signal rather than a gap.
    """

    judges = sorted({str(n) for n in custom_judge_names if str(n)})
    drift_kinds = ", ".join(sorted(GOLDFIVE_DRIFT_KINDS))
    if judges:
        judges_line = ", ".join(judges)
        judge_example = judges[0]
        judges_section = (
            f"Declared board judges (THIS board): {judges_line}\n"
            "  To predict a declared board judge moving, add an entry to\n"
            '  "expected_metric_movements" whose "metric_name" is the judge\'s\n'
            "  BARE name — NOT a namespaced or prefixed form. Correct:\n"
            f'    {{"metric_name": "{judge_example}", "direction": "increase", "magnitude": "medium"}}\n'
            f'  WRONG (these will be rejected as written): "drift:{judge_example}",\n'
            f'  "drift:custom:{judge_example}", "custom:{judge_example}". Use the\n'
            "  bare judge name."
        )
    else:
        judges_section = (
            "Declared board judges (THIS board): (this board declares no custom judges)"
        )
    return (
        f"{judges_section}\n\n"
        "Valid built-in drift kinds — reference these in a movement as\n"
        '"drift:<kind>" (e.g. {"metric_name": "drift:off_topic", ...} or, in\n'
        'expected_drift_movements, {"kind": "off_topic", ...}):\n'
        f"  {drift_kinds}\n\n"
        "Other metric namespaces (cost / rubric / latency / schema / output)\n"
        'are accepted as-is, e.g. "cost:tokens_spent", "rubric:slide_structure".'
    )


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


#: Band edges for a rate in ``[0, 1]`` (a fraction-of-runs marginal). The
#: proposer is given a COARSE band, never the exact rate, so it cannot read
#: the board's round-over-round response surface off a failure-mode number
#: (the same memorization-resistance rationale as :func:`_bucket_scalar_delta`,
#: OVERFITTING.md §11.4). Edges are deliberately wide: "none" (essentially
#: never), then ~10% steps surfaced as approximate labels. The label carries
#: a ``~`` to signal it is approximate.
def _band_rate(rate: float) -> str:
    """Coarsen a fraction-of-runs rate to a memorization-resistant band.

    Returns an approximate label like ``~20%`` (rounded to the nearest 10%)
    or ``none`` for an essentially-zero rate and ``~all`` for an
    essentially-one rate. The proposer keeps the actionable "how often does
    this failure happen" signal without the exact per-round number that would
    let it climb the board's response surface rather than true quality.
    """
    if rate <= 0.0:
        return "none"
    if rate >= 0.999:
        return "~all"
    # Round to the nearest 10% step; clamp a tiny-but-nonzero rate up to the
    # smallest visible band so "it happens, rarely" never reads as "never".
    pct = round(rate * 10) * 10
    if pct <= 0:
        pct = 10
    if pct >= 100:
        return "~all"
    return f"~{pct}%"


#: Band edges for a quality value in ``[0, 1]`` (recall / precision / score).
#: A coarse three-band label — ``low`` / ``medium`` / ``high`` — with the
#: midpoint of the band shown approximately, mirroring the issue's worked
#: example (``recall: medium (~0.6)``). The exact mean never reaches the
#: model, only the band, so no fine-grained response surface leaks.
def _band_quality(value: float) -> str:
    """Coarsen a ``[0, 1]`` quality mean to a ``low``/``medium``/``high`` band.

    Returns a band label plus an approximate band-representative value (e.g.
    ``low (~0.3)``) so the proposer can read the magnitude qualitatively
    without the exact mean. The thirds split — ``< 1/3`` low, ``< 2/3``
    medium, else high — matches the issue example's ``recall: medium (~0.6)``
    / ``precision: low (~0.3)`` framing.
    """
    if value < 1.0 / 3.0:
        return "low (~0.3)"
    if value < 2.0 / 3.0:
        return "medium (~0.6)"
    return "high (~0.9)"


def render_failure_mode_profile(summary: OutcomeMarginalSummary) -> str:
    """Render the bucketed, identity-free outcome-marginal profile body.

    Capability 2 of issue #18 (item 7). Produces the compact, train-slice-
    only, BUCKETED ``Failure-mode profile`` block the proposer reads to
    target *why* answers are wrong (over-retrieval vs misses vs empty
    answers), not just *that* a scalar moved. Every number is banded — rates
    through :func:`_band_rate`, quality means through :func:`_band_quality` —
    so no exact per-run value and no round-over-round response surface leaks
    (OVERFITTING.md §11.4). The summary itself carries only marginal rates
    (no entry id, question, or output token), so the rendered block is
    board-anonymous by construction.

    An empty summary (no train-slice runs — see
    :meth:`~zicato.analyzer.outcome_marginals.OutcomeMarginalSummary.is_empty`)
    returns the EMPTY STRING — the proposer-side sentinel for "omit this
    section entirely", exactly as the insights / prior-experiments / pattern
    blocks behave. That is what keeps the proposer prompt byte-identical to
    today when no outcome data is present.
    """
    if summary.is_empty():
        return ""

    lines: list[str] = []

    # The recall/precision decomposition line — the most actionable signal,
    # present only when Capability 1's per-entry metrics carried it. It tells
    # over-retrieval (precision down) from misses (recall down) apart and is
    # annotated with the dominant failure direction.
    if summary.recall_mean is not None and summary.precision_mean is not None:
        recall_band = _band_quality(summary.recall_mean)
        precision_band = _band_quality(summary.precision_mean)
        decomposition = f"- recall: {recall_band} | precision: {precision_band}"
        # The directional read: precision materially below recall ⇒
        # over-retrieval; recall materially below precision ⇒ misses.
        gap = summary.recall_mean - summary.precision_mean
        if gap > 0.15:
            decomposition += "   => over-retrieves"
        elif gap < -0.15:
            decomposition += "   => misses relevant items"
        lines.append(decomposition)

    if summary.over_retrieval_rate is not None:
        lines.append(
            f"- over-retrieval (precision<0.5): {_band_rate(summary.over_retrieval_rate)} of runs"
        )

    # Generic, board-agnostic failure modes — one compact line.
    generic_parts = [
        f"empty / terse answers: {_band_rate(summary.empty_rate + summary.terse_rate)}",
        f"looping: {_band_rate(summary.looping_rate)}",
    ]
    lines.append("- " + " | ".join(generic_parts))

    # Pass-rate / score bands, when present — the binary + continuous outcome
    # bands, banded so the exact aggregate never leaks.
    outcome_parts: list[str] = []
    if summary.pass_rate is not None:
        outcome_parts.append(f"pass-rate: {_band_rate(summary.pass_rate)}")
    if summary.mean_score is not None:
        outcome_parts.append(f"mean score: {_band_quality(summary.mean_score)}")
    if outcome_parts:
        lines.append("- " + " | ".join(outcome_parts))

    # Operator-contributed marginals (already sanitized + numeric). Render
    # each as its own banded line, sorted for a stable block.
    for name in sorted(summary.operator_marginals):
        rate = summary.operator_marginals[name]
        lines.append(f"- {name}: {_band_rate(rate)} of runs")

    return "\n".join(lines)


def render_process_exemplars(exemplars: Iterable[ProcessExemplar]) -> str:
    """Render redacted process-exemplar windows into the prompt block body.

    The prompt-side surface of the opt-in process-exemplar channel
    (``docs/design/PROCESS-EXEMPLARS.md``;
    :func:`zicato.analyzer.process_exemplars.extract_process_exemplars`).
    Each exemplar renders as one bullet — the pattern it illustrates plus
    its anchor label — followed by the window's events, one line each:
    signed relative offset (the anchor is ``0``; never an absolute
    sequence number), the payload case name, and the already-redacted
    ``key=value`` fields the extractor's field policy admitted. This
    function performs NO redaction of its own — every byte it renders was
    already passed through the extractor's mechanical rules (allowlist,
    anonymization, truncation, identity scrub); it only formats.

    An empty iterable returns the EMPTY STRING — the proposer-side
    sentinel for "omit this section entirely", exactly as the
    failure-mode profile behaves — so a knob-off round renders a
    byte-identical prompt.
    """
    items = list(exemplars)
    if not items:
        return ""
    total = len(items)
    blocks: list[str] = []
    for i, ex in enumerate(items, start=1):
        lines = [
            f"- exemplar {i}/{total} — pattern {ex.pattern_kind} ({ex.anchor_label}):",
        ]
        for ev in ex.events:
            parts = []
            for name, value in ev.fields:
                # Quote free-text values (they carry spaces) so field
                # boundaries stay legible; closed-vocabulary values render
                # bare. Purely cosmetic — the content is already redacted.
                rendered = f'"{value}"' if re.search(r"\s", value) else value
                parts.append(f"{name}={rendered}")
            offset = f"{ev.offset:+d}" if ev.offset != 0 else " 0"
            line = f"    {offset} {ev.case}"
            if parts:
                line = f"{line} {' '.join(parts)}"
            lines.append(line)
        blocks.append("\n".join(lines))
    return "\n".join(blocks)


def render_genealogy_block(items: Iterable[GenealogyItem]) -> str:
    """Render sampled genealogy items into the prompt block body.

    The prompt-side surface of the opt-in genealogy channel (WS-GENE;
    ``docs/design/PROPOSER.md`` §2.7;
    :func:`zicato.proposer.genealogy.sample_genealogy`). Groups the items into
    the champion-spine PARENTS ("build on these") and the diverse rejected
    INSPIRATIONS ("a different framing may clear the gate"), each rendered as a
    two-line entry: the generation id + kind + banded whole-candidate outcome +
    the redacted patch summary (targeted mutation ids, op kinds, size band, and
    a capped excerpt of the proposer's OWN diff text), then the indented core
    idea. This function performs NO redaction of its own — the sampler already
    banded the outcome and capped the excerpt (no entry ids, no per-entry
    results, no exact deltas ever reach it); it only formats.

    An empty iterable returns the EMPTY STRING — the proposer-side sentinel for
    "omit this section entirely", exactly as the failure-mode and
    process-exemplar blocks behave — so a ``genealogy = 0`` round renders a
    byte-identical prompt.
    """
    items_list = list(items)
    if not items_list:
        return ""

    def _entry(item: GenealogyItem) -> str:
        ps = item.patch_summary
        ids = ", ".join(ps.mutation_ids) if ps.mutation_ids else "—"
        ops = "/".join(ps.op_kinds) if ps.op_kinds else "—"
        head = f"- {item.generation_id}"
        if item.banded_outcome:
            head = f"{head} Δscalar={item.banded_outcome}"
        head = f"{head}  [{ids}] {ops} · {ps.size_band} edit"
        lines = [head, f"    {item.core_idea}"]
        if ps.diff_excerpt:
            lines.append(f"    diff: {ps.diff_excerpt}")
        return "\n".join(lines)

    parents = [it for it in items_list if it.kind == "parent"]
    inspirations = [it for it in items_list if it.kind == "inspiration"]

    blocks: list[str] = []
    if parents:
        body = "\n".join(_entry(it) for it in parents)
        blocks.append("Champion lineage (promoted ancestors — build on what worked):\n" f"{body}")
    if inspirations:
        body = "\n".join(_entry(it) for it in inspirations)
        blocks.append(
            "Rejected candidates worth re-framing (diverse ideas that did not "
            f"clear the gate as tried):\n{body}"
        )
    return "\n\n".join(blocks)


def _band_prediction_accuracy(accuracy: float) -> str:
    """Coarsen a hypothesis prediction-accuracy fraction to a calibration band.

    Returns ``low`` / ``medium`` / ``high`` over the same thirds split as
    :func:`_band_quality`, WITHOUT the approximate representative value — the
    accuracy is a calibration meta-signal, not a per-entry board number, but
    it is banded anyway so no exact round-over-round value leaks (the same
    memorization-resistance discipline as the rest of the restricted memory;
    OVERFITTING.md §11.4 / FUNCTIONALITY-RECOMMENDATIONS.md §4.2).
    """
    if accuracy < 1.0 / 3.0:
        return "low"
    if accuracy < 2.0 / 3.0:
        return "medium"
    return "high"


def _render_prior_experiment_line(pe: PriorExperiment, *, restrict: bool = False) -> str:
    """Render one prior experiment as a two-line compact entry.

    The first line carries the verdict, Δscalar (omitted for an in-flight
    sibling or a cross-contract entry whose number does not transfer), the
    bracketed targeted mutation-point ids (plus the symbolic rejection
    reason for a rejected entry), and — when the experiment was graded — the
    proposer's hypothesis prediction-accuracy as an advisory ``prediction``
    calibration band; the second line is the indented core idea. See
    ``docs/design/EXPERIMENT-MEMORY.md`` §3.5 and
    ``docs/design/FUNCTIONALITY-RECOMMENDATIONS.md`` §4.2.

    When ``restrict`` is ``True``, the fine-grained ``Δscalar`` number is
    coarsened to an ``improved`` / ``flat`` / ``regressed`` bucket
    (OVERFITTING.md §11.4) so the proposer cannot read the board's exact
    response surface round-over-round. When ``False`` the precise number
    renders verbatim, byte-for-byte as before this lever existed. The
    prediction-accuracy is ALWAYS banded to ``low``/``medium``/``high``
    regardless of ``restrict`` — it is a calibration meta-signal, never an
    exact board number, so it carries no per-entry surface to leak.
    """
    ids = ", ".join(pe.modulating) if pe.modulating else "—"
    verdict = pe.decision.upper().replace("_", "-")
    # A cross-contract entry is labelled with its SOURCE epoch so the
    # provenance is unmistakable; a same-contract entry renders its bare
    # generation id, byte-identical to before cross-epoch memory existed.
    gen_label = pe.generation_id if pe.same_contract else f"{pe.epoch_id}::{pe.generation_id}"
    # A cross-contract entry's Δscalar is measured against a different
    # board and is not comparable, so it is omitted; an in-flight sibling
    # has no outcome yet.
    if pe.scalar_score_delta is not None and pe.same_contract:
        if restrict:
            head = (
                f"- {gen_label} {verdict} "
                f"Δscalar={_bucket_scalar_delta(pe.scalar_score_delta)}  [{ids}]"
            )
        else:
            head = f"- {gen_label} {verdict} Δscalar={pe.scalar_score_delta:+.3f}  [{ids}]"
    else:
        head = f"- {gen_label} {verdict}  [{ids}]"
    if pe.decision == "rejected" and pe.rejection_reason:
        head = f"{head}  ({pe.rejection_reason})"
    # Advisory hypothesis prediction-accuracy (diagnostic, never gates) —
    # banded so the proposer reads its own calibration ("did my last
    # predictions hold?") without an exact, climbable number.
    if pe.prediction_accuracy is not None:
        head = f"{head}  prediction:{_band_prediction_accuracy(pe.prediction_accuracy)}"
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

    Cross-contract entries (``same_contract=False`` — a different epoch
    under the SAME contract hash; EXPERIMENT-MEMORY.md §3.4) render in
    their OWN clearly-separated block after the same-epoch blocks: they
    carry directions (core idea + decision, epoch-tagged), never deltas —
    a Δscalar measured under another epoch does not transfer. A digest
    with no cross-contract entries renders byte-identically to before
    cross-epoch memory existed.
    """
    items = list(prior)
    if not items:
        return ""

    cross = [pe for pe in items if not pe.same_contract]
    same = [pe for pe in items if pe.same_contract]

    promoted = [pe for pe in same if pe.decision == "promoted"]
    rejected = [pe for pe in same if pe.decision == "rejected"]
    in_flight = [pe for pe in same if pe.decision == "in_flight"]
    deferred = [pe for pe in same if pe.decision == "deferred"]

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
    if cross:
        body = "\n".join(_render_prior_experiment_line(pe, restrict=restrict) for pe in cross)
        blocks.append(
            "From PRIOR epochs under the same contract (cross-epoch memory — "
            f"directions only, deltas do not transfer):\n{body}"
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


#: How much of the model's prior raw response to echo back on a repair
#: turn. Enough to show the model the SHAPE of what it produced (the stray
#: ``<think>`` block, the prose preamble, the trailing commentary) without
#: blowing the context budget — the failure mode is almost always visible
#: in the opening few hundred characters.
_FEEDBACK_PRIOR_OUTPUT_LIMIT_CHARS = 800


def _truncate_prior_output(text: str) -> str:
    """Clip a prior raw response for echo-back on a repair turn."""

    clipped = text[:_FEEDBACK_PRIOR_OUTPUT_LIMIT_CHARS]
    if len(text) > _FEEDBACK_PRIOR_OUTPUT_LIMIT_CHARS:
        clipped = clipped.rstrip() + "\n[... truncated ...]"
    return clipped


def render_user_prompt(
    *,
    current_loss_summary: str,
    patterns: Iterable[Pattern],
    mutations: Iterable[MutationPoint],
    feedback: str = "",
    feedback_prior_output: str = "",
    feedback_was_empty: bool = False,
    insights: str = "",
    prior_experiments: Iterable[PriorExperiment] = (),
    restrict_visibility: bool = False,
    custom_judge_names: Iterable[str] = (),
    failure_profile: str = "",
    process_exemplars: str = "",
    genealogy: Iterable[GenealogyItem] = (),
    sample_hint: str = "",
    mutation_track_records: Mapping[str, MutationTrackRecord] | None = None,
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
        Optional retry feedback. When non-empty, a repair section is
        prepended explaining the previous failure so the model can
        correct itself. This turns each retry into a genuine repair turn
        rather than a blind re-ask.
    feedback_prior_output:
        The model's PRIOR raw response, echoed back (truncated) on a
        repair turn so the model can see exactly what it produced — the
        stray ``<think>`` block, the prose preamble, the trailing
        commentary. Only rendered when ``feedback`` is also non-empty.
    feedback_was_empty:
        When ``True``, the prior response was empty (the model most likely
        spent its entire output budget on reasoning). The repair section
        switches to a targeted instruction: skip ALL reasoning and emit
        the JSON object immediately. Only consulted when ``feedback`` is
        non-empty.
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
    custom_judge_names:
        The declared board judges for the active contract (board
        ``JudgeSpec.name`` ∪ ``per_judge_weights`` keys), threaded by the
        orchestrator from the same source it passes to the structured
        validator. Rendered into the ``## Valid expectation targets`` block
        via :func:`render_metric_targets_block` so the proposer is told, for
        THIS board, the exact ``metric_name`` to use for a declared judge
        (its bare name) and the valid built-in drift kinds — keeping the
        prompt and the validator's accepted forms in lockstep. Empty (the
        default) renders an explicit "no custom judges" notice alongside the
        always-present drift-kind enumeration.
    failure_profile:
        Optional pre-rendered, train-slice-only, BUCKETED outcome-marginal
        block (Capability 2 of issue #18 — built by
        :func:`~zicato.proposer.prompts.render_failure_mode_profile` from an
        :class:`~zicato.analyzer.outcome_marginals.OutcomeMarginalSummary`).
        When non-empty, a ``## Failure-mode profile (this round, aggregate —
        train slice)`` section is prepended (after the telemetry insights,
        before ``## What's already been tried``) so the proposer can target
        *why* answers are wrong, not just *that* a scalar moved. The string
        is already board-anonymized + banded by its renderer — this function
        only splices it. Empty (the default) omits the section entirely, so a
        caller that supplies no profile renders a byte-identical prompt to
        before this surface existed.
    process_exemplars:
        Optional pre-rendered, train-slice-only, REDACTED process-exemplar
        block (the opt-in ``proposer_quality.process_exemplars`` channel —
        ``docs/design/PROCESS-EXEMPLARS.md``; built by
        :func:`render_process_exemplars` from the extractor's already-
        redacted windows). When non-empty, a ``## Process exemplars``
        section is spliced in DIRECTLY AFTER the failure-mode profile,
        headed by a banner restating the redaction contract, so the
        proposer can see HOW a detected failure unfolds — never WHICH
        board entry it unfolded on. The string is already redacted by the
        extractor's mechanical rules; this function only splices it.
        Empty (the default — every knob-off round) omits the section
        entirely, rendering a byte-identical prompt to before this
        surface existed.
    genealogy:
        Optional sampled genealogy items (the opt-in
        ``proposer_quality.genealogy`` channel — ``docs/design/PROPOSER.md``
        §2.7; produced by :func:`zicato.proposer.genealogy.sample_genealogy`).
        Rendered here through :func:`render_genealogy_block` and, when the
        result is non-empty, spliced as a ``## Candidate genealogy`` section
        DIRECTLY ABOVE ``## What's already been tried`` so the proposer can
        extend a winning line or re-frame a rejected one (in-context
        evolution). The items are already banded + capped by the sampler (no
        entry ids, no per-entry results, no exact deltas, nothing
        holdout-derived); this function only renders + splices them. Empty (the
        default — every knob-off round) omits the section entirely, rendering a
        byte-identical prompt to before this surface existed.
    sample_hint:
        Optional per-sample edit-class steering line (the best-of-N slate
        diversifier — see :data:`zicato.proposer.best_of_n.EDIT_CLASS_HINTS`).
        When non-empty, an ``## Edit-class hint (this sample)`` section is
        prepended at the very top of the body so each slate slot explores a
        DIFFERENT edit strategy rather than re-rolling one. A STATIC
        instruction string carrying no board identity, so it composes with
        the restricted-visibility envelope untouched. Empty (the default —
        every single-sample call) omits the section entirely, rendering a
        byte-identical prompt to before this surface existed.
    mutation_track_records:
        Optional per-mutation-point track records (the fertility map —
        :func:`zicato.index.query.mutation_point_track_record`, assembled
        best-effort by the orchestrator from the analytical index). Each
        manifest entry with a record gains one compact, BANDED advisory
        line (:func:`render_mutation_track_annotation`) — aggregate counts
        and bucketed Δscalar only, honestly labelled as "experiments
        touching this point" (multi-patch experiments confound credit;
        never causal) — inside the restricted-visibility envelope. ``None``
        / empty (the default) renders a byte-identical manifest to before
        this surface existed.
    """

    body = USER_PROMPT_TEMPLATE.format(
        current_loss_summary=current_loss_summary.strip() or "(no loss summary)",
        metric_targets_block=render_metric_targets_block(custom_judge_names),
        pattern_block=render_pattern_block(patterns, restrict=restrict_visibility),
        mutation_block=render_mutation_block(mutations, track_records=mutation_track_records),
    )
    prior_block = render_prior_experiments_block(prior_experiments, restrict=restrict_visibility)
    if prior_block:
        prior_prefix = (
            "## What's already been tried (this epoch — avoid repeating "
            f"failures, build on wins)\n\n{prior_block}\n\n"
        )
        body = prior_prefix + body
    genealogy_block = render_genealogy_block(genealogy)
    if genealogy_block.strip():
        # Spliced so it lands DIRECTLY ABOVE the experiment-memory block in the
        # final prompt (prefixes stack in reverse prepend order) — the lineage
        # the proposer builds on / diverges from, framing the "what's been
        # tried" list below it. The banner names the channel; the sampler has
        # already banded every outcome and capped every excerpt.
        genealogy_prefix = (
            "## Candidate genealogy (this reign — in-context evolution)\n"
            "Promoted ancestors to build on and diverse rejected ideas to "
            "re-frame. Outcomes are\n"
            "banded (improved / flat / regressed); diffs are the proposer's "
            "own edits, excerpted.\n"
            f"{genealogy_block.strip()}\n\n"
        )
        body = genealogy_prefix + body
    if process_exemplars.strip():
        # Spliced so it lands DIRECTLY AFTER the failure-mode profile in the
        # final prompt (prefixes stack in reverse prepend order). The banner
        # restates the redaction contract so the model reads the windows as
        # anonymized mechanism, not as named board evidence.
        exemplars_prefix = (
            "## Process exemplars (train slice — redacted event windows)\n"
            "Redaction contract (PROCESS-EXEMPLARS.md): entry ids and task "
            "text stripped, task ids\n"
            "anonymized per window, free text truncated, model outputs "
            "withheld. These show HOW a\n"
            "detected failure unfolds — never WHICH board entry it unfolded "
            "on.\n"
            f"{process_exemplars.strip()}\n\n"
        )
        body = exemplars_prefix + body
    if failure_profile.strip():
        failure_prefix = (
            "## Failure-mode profile (this round, aggregate — train slice)\n"
            f"{failure_profile.strip()}\n\n"
        )
        body = failure_prefix + body
    if insights.strip():
        insights_prefix = f"## Recent telemetry insights\n{insights.strip()}\n\n"
        body = insights_prefix + body
    if sample_hint.strip():
        # The best-of-N slate diversifier: this sample's edit-class steering,
        # read first so the slot's strategy frames everything below it.
        hint_prefix = f"## Edit-class hint (this sample)\n{sample_hint.strip()}\n\n"
        body = hint_prefix + body
    if feedback:
        sections = [
            "## Previous attempt was rejected",
            "Your previous response failed to parse. Reason:",
            "",
            f"    {feedback}",
            "",
        ]
        if feedback_was_empty:
            # Targeted variant: an empty response means the model most
            # likely burned its whole output budget on reasoning before
            # ever reaching the JSON. Tell it to skip reasoning entirely.
            sections += [
                "Your previous output was EMPTY — you most likely spent your entire "
                "output budget on reasoning before emitting any JSON.",
                "",
                "Do NOT think step by step. Do NOT emit any <think>/<thinking>/"
                "<reasoning> block. Skip all reasoning and emit the JSON object "
                "IMMEDIATELY as the very first thing you write.",
                "",
            ]
        elif feedback_prior_output.strip():
            echoed = textwrap.indent(_truncate_prior_output(feedback_prior_output), "    ")
            sections += [
                "Your previous output was:",
                "",
                echoed,
                "",
            ]
        sections += [
            "Respond with ONLY the JSON object — no <think>/<thinking>/<reasoning> "
            "blocks, no markdown code fences, no prose before or after it. The "
            'first character of your response MUST be "{" and the last MUST be '
            '"}". The top-level keys must be exactly "hypothesis" and "patches".',
            "",
            "",
        ]
        return "\n".join(sections) + body
    return body


def _render_merge_patches(patches: tuple[Patch, ...]) -> str:
    """One parent's patches rendered for the merge prompt (op + payload + why).

    The payload is the proposer's OWN authored edit (``new_content`` /
    ``new_numeric`` / ``new_enum``) — in-envelope exactly like the genealogy
    channel's diff excerpts (PROPOSER.md §2.6.1). No board identity rides here.
    """
    lines: list[str] = []
    for p in patches:
        if p.op == "replace":
            payload = p.new_content or ""
        elif p.op == "set_numeric":
            payload = str(p.new_numeric)
        else:
            payload = str(p.new_enum or "")
        indented = textwrap.indent(payload, "      ") if "\n" in payload else payload
        lines.append(
            f"    - {p.op} {p.mutation_id}: {indented}\n"
            f"      rationale: {p.rationale or '(no rationale)'}"
        )
    return "\n".join(lines) if lines else "    (no patches)"


def render_recombine_merge_prompt(
    pair: RecombinationPair,
    *,
    brief_text: str,
    mutations: Iterable[MutationPoint] = (),
    custom_judge_names: Iterable[str] = (),
) -> tuple[str, str]:
    """Build the ``(system, user)`` prompt for an LLM-guided recombination merge.

    The prompt-side surface of the WS-MERGE ``recombine_merge = "llm"`` mode
    (PROPOSER.md §2.6.1). The SYSTEM prompt is the SAME schema-carrying
    proposer system prompt (:func:`render_system_prompt` with the epoch brief),
    so the response is a proposal like any other and flows through the normal
    :func:`zicato.proposer.structured.parse_experiment_json` path. The USER
    prompt frames the MERGE task from the envelope-clean :class:`RecombinationPair`:

    * both parents' CORE IDEAS, whole-candidate BANDED outcomes, and PATCHES
      (the ``new_content`` the proposer itself authored — in-envelope);
    * COUNTS-ONLY complementarity (how many train entries each improved, the
      combined improved / regressed counts);
    * the valid expectation targets + the mutation manifest, so the merged
      proposal targets only manifest-valid ids.

    It carries NOTHING the genealogy channel does not already permit: no
    board-entry id, no per-entry result, no exact Δscalar (the builder bands
    the outcomes before construction). Skills are omitted from the system
    prompt (the merge composes already-authored patches, not fresh
    exploration; the wrapper does not hold the resolved skill modules) — the
    epoch brief, which carries the forbidden-edits guidance, IS included.
    """
    system_prompt = render_system_prompt(brief_text)
    targets_block = render_metric_targets_block(custom_judge_names)
    mutation_block = render_mutation_block(mutations)
    a_outcome = pair.a_banded_outcome or "unsettled"
    b_outcome = pair.b_banded_outcome or "unsettled"
    user_prompt = (
        "You are MERGING two rejected complementary improvement proposals into "
        "a single experiment for a multi-agent system. Each was rejected alone "
        "because a parsimony-biased gate discounts one fix at a time, but each "
        "genuinely fixed a DISTINCT slice of the evaluation board. Compose ONE "
        "experiment that captures BOTH parents' improvements.\n\n"
        f"## Parent A (generation {pair.a_generation_id}; outcome: {a_outcome})\n"
        f"core_idea: {pair.a_core_idea}\n"
        f"patches:\n{_render_merge_patches(pair.a_patches)}\n\n"
        f"## Parent B (generation {pair.b_generation_id}; outcome: {b_outcome})\n"
        f"core_idea: {pair.b_core_idea}\n"
        f"patches:\n{_render_merge_patches(pair.b_patches)}\n\n"
        "## Complementarity (counts only)\n"
        f"Parent A improved {pair.a_improved_count} train entr(y/ies); parent B "
        f"improved {pair.b_improved_count}. Together they cover "
        f"{pair.combined_improved_count} distinct entries "
        f"({pair.combined_regressed_count} with an observed single-sample "
        "regression). Their edits may OVERLAP where they touch the SAME "
        "mutation point — resolve those into a single coherent edit that "
        "preserves both parents' intent (a blind concatenation would drop one "
        "side, since the applier is last-wins on a duplicate target).\n\n"
        "## Valid expectation targets (what a hypothesis movement may reference)\n"
        f"{targets_block}\n\n"
        "## Mutation points (only these ids are valid patch targets)\n"
        f"{mutation_block}\n\n"
        "Emit the merged experiment as the JSON object only — no surrounding "
        'prose, no markdown fences. The first character MUST be "{" and the '
        'last MUST be "}".'
    )
    return system_prompt, user_prompt


__all__ = [
    "SYSTEM_PROMPT_TEMPLATE",
    "USER_PROMPT_TEMPLATE",
    "render_failure_mode_profile",
    "render_genealogy_block",
    "render_recombine_merge_prompt",
    "render_metric_targets_block",
    "render_process_exemplars",
    "render_mutation_track_annotation",
    "render_pattern_block",
    "render_mutation_block",
    "render_prior_experiments_block",
    "render_skills_block",
    "render_system_prompt",
    "render_user_prompt",
]
