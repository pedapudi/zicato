# ruff: noqa: E501
# One model-visible line in the metric-targets block — the worked
# "metric_name" example a proposer copies — exceeds the project line limit.
# Wrapping it would change what the model reads, so the file is exempted
# rather than the example split.
"""The blocks a proposal episode's evidence is rendered from.

Every channel the round assembles reaches the model through one of the
renderers here, and every band, bucket and aggregation the
restricted-visibility envelope requires is applied at this boundary — see
``docs/design/OVERFITTING.md`` §11. A channel that renders raw per-entry
material here has leaked, whatever the caller intended.

Each renderer returns the empty string for its no-data case, which is the
sentinel meaning "omit the section entirely". That convention is what
makes a round that opts into none of the optional channels render the
three blocks that are always present and nothing else.

The one prompt this module still templates whole is the LLM-guided
recombination merge (:data:`MERGE_SYSTEM_PROMPT_TEMPLATE`), which is a
single JSON answer rather than an episode. The episode's own instructions
and task are assembled in :mod:`zicato.proposer.foe_request`, from the
blocks here.
"""

from __future__ import annotations

import json
import re
import textwrap
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

from zicato.analyzer.outcome_marginals import OutcomeMarginalSummary
from zicato.analyzer.process_exemplars import ProcessExemplar
from zicato.core.drift_kinds import GOLDFIVE_DRIFT_KINDS
from zicato.core.types import MutationPoint, Patch, Pattern, PriorExperiment, ProposerSkill
from zicato.proposer.calibration import CalibrationSummary
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


@dataclass(frozen=True, slots=True)
class ScoredTarget:
    """One target the contract actually scores, with its resolved weight.

    ``weight`` is the coefficient the scoring path resolves for this target,
    signed as the scalar sees it: positive means a higher value costs more
    (the scalar is a LOSS), negative means a higher value is better. Only the
    RATIO between weights inside one channel is ever rendered — the raw
    coefficient stays orchestrator-side, because handing the proposer the
    objective function invites optimising the shape of the score rather than
    the behaviour the board measures (dev-guide ``05-proposer.md §5.8``).
    """

    name: str
    weight: float


@dataclass(frozen=True, slots=True)
class MetricPriorities:
    """What the active contract scores, split into non-comparable channels.

    The proposer chooses what to work on each round; the operator already
    answered that question by setting the scoring weights. This is that answer
    in a shape the prompt can render: per channel, the targets whose weight is
    non-zero, ordered by weight magnitude, highest first.

    The channels are PEERS, never one ranking. Drift kinds live inside
    ``drift_loss_mean`` and are then scaled by ``namespace_weights["drift:"]``;
    judges are scaled by ``namespace_weights["judge:"]``; the pass term is its
    own bounded ``pass_weight * (1 - mean_score)``; every other namespace
    coefficient is calibrated to that namespace's own units (``cost:`` counts
    tokens in the thousands, ``rubric:`` is a quality score with a NEGATIVE
    weight). A single cross-channel ranking would imply a comparability that
    does not exist, so banding happens strictly within a channel.

    A zero-weight target is ABSENT rather than annotated — the convention the
    scoring side already holds to for ``diff_complexity``
    (:func:`zicato.scoring.builtins.builtin_scalar`): at zero the term is
    exactly absent, no key and no addition.

    Fields
    ------
    judges:
        Declared board judges (board ``JudgeSpec.name`` ∪ ``per_judge_weights``
        keys) at ``per_judge_weights.get(name, default_judge_weight)``, times
        ``namespace_weights["judge:"]`` — judges are their own channel, so a
        zeroed ``drift:`` coefficient leaves them ranked.
    drift_kinds:
        Built-in goldfive drift kinds at ``per_kind_weights.get(kind, 1.0)``
        times ``namespace_weights["drift:"]``.
    pass_rate_weight:
        ``pass_weight``. Named as a real target: once zero-weight entries are
        dropped, a pass-rate-only contract has an empty target vocabulary but
        still owes a mandatory movement, and the validator accepts
        ``pass_rate`` only because it accepts any unprefixed string.
    namespace_metrics:
        The remaining scored metric namespaces — ``failure:``, ``runtime:``,
        ``cost:``, ``rubric:``, ... — at ``namespace_weights[namespace]``.
        ``drift:`` and ``judge:`` are excluded because the two channels above
        already name them per kind and per judge. Names are DATA-DRIVEN from the
        round's own loss profiles (:meth:`~zicato.core.loss.LossProfile
        .unified_metrics`), so a round with no losses yet carries the
        namespace prefixes alone (``cost:``, ``rubric:``) rather than
        inventing metric names.
    """

    judges: tuple[ScoredTarget, ...] = ()
    drift_kinds: tuple[ScoredTarget, ...] = ()
    pass_rate_weight: float = 0.0
    namespace_metrics: tuple[ScoredTarget, ...] = ()

    def is_empty(self) -> bool:
        """Whether the contract scores nothing this block could name."""
        return not (
            self.judges or self.drift_kinds or self.pass_rate_weight or self.namespace_metrics
        )


def _band_weight_ratio(weight: float, top: float) -> str:
    """Band one target's weight against the TOP weight in its own channel.

    Weights are unbounded, so the ``[0, 1]`` thirds split :func:`_band_quality`
    uses does not transfer; the ratio to the channel's own maximum does, and it
    is the only thing the proposer can act on anyway. Magnitude only — the sign
    is direction, rendered separately.
    """
    ratio = abs(weight) / top if top else 0.0
    if ratio < 1.0 / 3.0:
        return "low"
    if ratio < 2.0 / 3.0:
        return "medium"
    return "high"


def _render_priority_targets(targets: tuple[ScoredTarget, ...]) -> str:
    """Render one channel's targets, grouped by their band within the channel.

    When every weight in the channel is equal the bands carry no information —
    and that IS the default contract, where ``per_kind_weights`` is empty and
    every kind sits at ``1.0`` — so the channel renders as one flat list and
    says so once, rather than banding every target ``high``.

    Otherwise the targets group under their band rather than taking a line
    each. A contract that raises one kind leaves the other forty at the same
    weight, and forty consecutive identical band labels bury the one target
    the operator actually singled out.
    """
    magnitudes = {round(abs(t.weight), 12) for t in targets}
    if len(magnitudes) <= 1:
        return "  " + ", ".join(t.name for t in targets) + "\n  (all scored equally)"
    top = max(abs(t.weight) for t in targets)
    grouped: dict[str, list[str]] = {}
    for target in targets:
        grouped.setdefault(_band_weight_ratio(target.weight, top), []).append(target.name)
    return "\n".join(
        f"  {band + ':':<8}{', '.join(grouped[band])}"
        for band in ("high", "medium", "low")
        if band in grouped
    )


def _render_namespace_targets(targets: tuple[ScoredTarget, ...]) -> str:
    """Render the namespace channel: direction only, deliberately unbanded.

    A namespace coefficient is calibrated to that namespace's OWN units —
    ``cost:`` counts tokens in the thousands, so its coefficient is small
    precisely because its values are large — which makes the ratio between two
    namespace weights meaningless. Banding them would say "cost barely counts"
    about a term that contributes 1.0 to the scalar for every thousand tokens.
    The sign IS meaningful and is rendered as the direction: negative weights
    mean a higher value is better (rubric scores grow with quality).
    """
    return "\n".join(
        f"  {t.name:<28} {'higher is better' if t.weight < 0 else 'lower is better'}"
        for t in targets
    )


def render_metric_priorities_block(priorities: MetricPriorities) -> str:
    """Render the contract's scored targets, ordered by what it rewards.

    The priority-aware form of :func:`render_metric_targets_block`: the same
    ``## Valid expectation targets`` body (same movement syntax, same worked
    example) with the vocabulary filtered to what the contract actually scores
    and ordered by weight within each channel. A target the contract weights at
    zero is absent, so the proposer cannot spend a round improving something
    that cannot move the result.

    Returns the empty string when the contract scores nothing this block can
    name, which leaves :func:`render_metric_targets_block` on its unfiltered
    membership rendering — an empty section would be worse than a flat list.
    """
    if priorities.is_empty():
        return ""
    sections: list[str] = [
        "The contract WEIGHTS these targets. Prefer the ones it scores highest; a\n"
        "target absent from this section carries ZERO weight, so improving it cannot\n"
        "move the score at all.",
    ]
    if priorities.judges:
        example = priorities.judges[0].name
        sections.append(
            "Declared board judges (THIS board) — reference by the judge's BARE name\n"
            'in "expected_metric_movements"[].metric_name. Correct:\n'
            f'    {{"metric_name": "{example}", "direction": "increase", "magnitude": "medium"}}\n'
            f'  WRONG (these will be rejected as written): "drift:{example}",\n'
            f'  "drift:custom:{example}", "custom:{example}".\n'
            + _render_priority_targets(priorities.judges)
        )
    else:
        sections.append("Declared board judges (THIS board): (none this contract scores)")
    if priorities.drift_kinds:
        sections.append(
            'Built-in drift kinds — reference as "drift:<kind>" (e.g.\n'
            '{"metric_name": "drift:off_topic", ...}) or, in\n'
            'expected_drift_movements, {"kind": "off_topic", ...}:\n'
            + _render_priority_targets(priorities.drift_kinds)
        )
    if priorities.pass_rate_weight:
        sections.append(
            "Board outcome — reference by its bare name:\n"
            "  pass_rate                    scored (the fraction of board entries\n"
            "                               whose expectation is met; higher is better)"
        )
    if priorities.namespace_metrics:
        sections.append(
            'Other metric namespaces — reference as "<namespace>:<metric>". These are\n'
            "separate scalar terms, each calibrated to its own units, so they are\n"
            "listed by direction rather than ranked against each other:\n"
            + _render_namespace_targets(priorities.namespace_metrics)
        )
    return "\n\n".join(sections)


def render_metric_targets_block(
    custom_judge_names: Iterable[str] = (),
    metric_priorities: str = "",
) -> str:
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

    ``metric_priorities`` is the PRE-RENDERED priority-aware body
    (:func:`render_metric_priorities_block`, built orchestrator-side where the
    frozen weights are in scope). When supplied it REPLACES the membership
    rendering below, because the two answer the same question and the flat one
    answers it wrongly: it presents a zero-weight judge beside a
    quadruple-weight one, and puts a triple-weight drift kind somewhere in 41
    alphabetically-ordered names. Empty (the default, and every caller that
    holds no weights — the standalone ``zicato propose``, tests) keeps the
    membership rendering byte-for-byte.

    Either form is a superset-free view of what
    :func:`~zicato.proposer.structured.parse_experiment_json` accepts: the
    validator's accept-list is unchanged by the priority filter, so dropping a
    zero-weight judge from the prompt can never turn an accepted movement into
    a parse rejection and a burned retry.
    """
    if metric_priorities.strip():
        return metric_priorities.strip()

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
def band_rate(rate: float) -> str:
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
    through :func:`band_rate`, quality means through :func:`_band_quality` —
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
            f"- over-retrieval (precision<0.5): {band_rate(summary.over_retrieval_rate)} of runs"
        )

    # Generic, board-agnostic failure modes — one compact line.
    generic_parts = [
        f"empty / terse answers: {band_rate(summary.empty_rate + summary.terse_rate)}",
        f"looping: {band_rate(summary.looping_rate)}",
    ]
    lines.append("- " + " | ".join(generic_parts))

    # Pass-rate / score bands, when present — the binary + continuous outcome
    # bands, banded so the exact aggregate never leaks.
    outcome_parts: list[str] = []
    if summary.pass_rate is not None:
        outcome_parts.append(f"pass-rate: {band_rate(summary.pass_rate)}")
    if summary.mean_score is not None:
        outcome_parts.append(f"mean score: {_band_quality(summary.mean_score)}")
    if outcome_parts:
        lines.append("- " + " | ".join(outcome_parts))

    # Operator-contributed marginals (already sanitized + numeric). Render
    # each as its own banded line, sorted for a stable block.
    for name in sorted(summary.operator_marginals):
        rate = summary.operator_marginals[name]
        lines.append(f"- {name}: {band_rate(rate)} of runs")

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


def render_calibration_block(summary: CalibrationSummary | None) -> str:
    """Render a calibration summary into the prompt block body.

    The prompt-side surface of the opt-in critic-calibration channel (WS-CAL;
    ``docs/design/PROPOSER.md`` §2.8; produced by
    :func:`zicato.proposer.calibration.sample_calibration`). Renders the
    per-claim-type hit / miss / unresolved COUNTS, the overall calibration
    fraction (``hit / (hit + miss)`` — the proposer's own self-accuracy), and
    up to K recent graded claims as ``GRADE · banded outcome · core idea``.
    This function performs NO redaction of its own — the sampler already banded
    every outcome and capped every core idea (no entry ids, no per-entry
    results, no exact deltas ever reach it); it only formats.

    ``None`` (the sampler's "no graded history" sentinel) returns the EMPTY
    STRING — the proposer-side "omit this section entirely" marker, exactly as
    the genealogy / failure-mode / process-exemplar blocks behave — so a
    ``calibration_feedback = 0`` round (and any round with no graded claims)
    renders a byte-identical prompt.
    """
    if summary is None:
        return ""

    graded = summary.hit_count + summary.miss_count
    if graded == 0:  # defensive — the sampler never returns such a summary
        return ""

    pct = round(summary.calibration_fraction * 100)
    lines = [
        f"Calibration: {pct}% of graded claims called correctly "
        f"({summary.hit_count} hit / {summary.miss_count} miss / "
        f"{summary.unresolved_count} unresolved).",
    ]
    if summary.recent:
        lines.append("Recent graded claims (most recent first):")
        for item in summary.recent:
            verdict = item.grade.upper()
            outcome = item.banded_outcome or "unsettled"
            lines.append(f"- {verdict} · Δscalar {outcome} · {item.core_idea}")
    return "\n".join(lines)


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


#: The system prompt of the one proposal that is still a single JSON
#: answer: the LLM-guided recombination merge (PROPOSER.md §2.6.1). A
#: merge composes two patch sets that were already authored and validated,
#: so it needs the response shape and the epoch's brief rather than the
#: proposal charter a Foe episode runs under. The shape is stated as the
#: parser's own schema, so the two cannot drift.
MERGE_SYSTEM_PROMPT_TEMPLATE = """\
You merge two already-authored improvement proposals for a multi-agent
system into a single experiment. You answer with one JSON object and
nothing else: no surrounding prose, no markdown fences. The first
character of your response MUST be "{{" and the last MUST be "}}".

The object conforms to this JSON Schema:

{schema}

Beyond the schema:
- Every "mutation_id" MUST appear in the mutation manifest the user
  message lists, and MUST NOT appear in the brief's forbidden-edits list.
- At least one of "expected_drift_movements" or
  "expected_metric_movements" MUST be present and non-empty, and every
  name in them MUST come from the user message's valid expectation
  targets. To predict a declared board judge moving, use its BARE name.
- For a "replace" on a span point, "new_content" is the replacement text
  for that one string literal and nothing around it — no signature, no
  import line, no ``zicato:mutable`` marker, no other mutation point. The
  harness owns the literal's quoting and indentation.

Proposer brief (operator-edited guidance for this epoch):

{brief_text}
"""


def render_merge_system_prompt(brief_text: str) -> str:
    """The merge system prompt, with the parser's schema and the brief."""
    from zicato.proposer.structured import EXPERIMENT_JSON_SCHEMA  # noqa: PLC0415

    return MERGE_SYSTEM_PROMPT_TEMPLATE.format(
        schema=json.dumps(EXPERIMENT_JSON_SCHEMA, indent=2, sort_keys=True),
        brief_text=brief_text.strip() or "(no brief)",
    )


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
    metric_priorities: str = "",
) -> tuple[str, str]:
    """Build the ``(system, user)`` prompt for an LLM-guided recombination merge.

    The prompt-side surface of the WS-MERGE ``recombine_merge = "llm"`` mode
    (PROPOSER.md §2.6.1). The SYSTEM prompt is the SAME schema-carrying
    schema-carrying system prompt (:func:`render_merge_system_prompt` with the
    epoch brief), so the response flows through the normal
    :func:`zicato.proposer.structured.parse_experiment_json` path. The USER
    prompt frames the MERGE task from the envelope-clean :class:`RecombinationPair`:

    * both parents' CORE IDEAS, whole-candidate BANDED outcomes, and PATCHES
      (the ``new_content`` the proposer itself authored — in-envelope);
    * COUNTS-ONLY complementarity (how many train entries each improved, the
      combined improved / regressed counts);
    * the valid expectation targets + the mutation manifest, so the merged
      proposal targets only manifest-valid ids. ``metric_priorities`` carries
      the priority-aware form of that block through unchanged, so a merge
      round is steered by the same contract weights a fresh sample is.

    It carries NOTHING the genealogy channel does not already permit: no
    board-entry id, no per-entry result, no exact Δscalar (the builder bands
    the outcomes before construction). Skills are omitted from the system
    prompt (the merge composes already-authored patches, not fresh
    exploration; the wrapper does not hold the resolved skill modules) — the
    epoch brief, which carries the forbidden-edits guidance, IS included.
    """
    system_prompt = render_merge_system_prompt(brief_text)
    targets_block = render_metric_targets_block(custom_judge_names, metric_priorities)
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
    "MERGE_SYSTEM_PROMPT_TEMPLATE",
    "MetricPriorities",
    "ScoredTarget",
    "band_rate",
    "render_calibration_block",
    "render_failure_mode_profile",
    "render_genealogy_block",
    "render_recombine_merge_prompt",
    "render_metric_priorities_block",
    "render_metric_targets_block",
    "render_process_exemplars",
    "render_mutation_track_annotation",
    "render_pattern_block",
    "render_mutation_block",
    "render_prior_experiments_block",
    "render_skills_block",
    "render_merge_system_prompt",
]
