"""The promote gate: decide whether a child generation supersedes its parent.

Before the three scoring rules, an OPT-IN parsimony CEILING may veto the
candidate outright (OVERFITTING.md §5 / §12 #4 — the ceiling half of the
diff-complexity regularizer):

0. **Diff-complexity ceiling** (when
   :attr:`ScoringWeights.diff_complexity_ceiling` ``> 0`` AND the challenger's
   ``diff_size`` was threaded onto ``child_agg``). The challenger's diff
   complexity (``added + removed + patches`` — the same measure the loss-term
   weight reads) is compared against the ceiling; a diff OVER the ceiling is
   REJECTED with an honest ``"diff_complexity_ceiling: diff complexity N
   exceeds ceiling M"`` reason, regardless of how strongly it improved. This
   is checked FIRST because it is a structural admissibility veto — an
   over-budget edit is inadmissible no matter what it scores. DEFAULT 0.0 =
   OFF: the ceiling is never consulted and the decision is byte-identical to a
   contract without the field (the challenger diff size is threaded only on the
   full A/B promotion path, so — exactly like the loss term — fast-mode and
   multi-challenger matchup scoring never carry it and are untouched).

Then the scoring rules, applied in order:

1a. **Pareto dominance** (OPT-IN; active only when
   :attr:`ScoringWeights.pareto_objectives` declares a profile — an empty
   profile is OFF and every decision below is byte-identical to a contract
   without the field). The child is compared to the parent on the operator's
   declared objective axes, all lower-is-better (see
   :func:`objective_vector`), and the verdict routes the rest of the gate:

   * *dominated* (worsened ≥1 objective, improved none — including the
     held-flat case) → REJECT outright with reason ``pareto_dominated``. No
     weighting of these axes could turn that into a win.
   * *dominates* (improved ≥1, worsened none) → Rule 1's scalar margin is
     SKIPPED and the child proceeds to Rules 2/3. Re-imposing the margin
     would veto, on a weighting the operator declined to use, exactly the
     win they declared they wanted. Rules 2 and 3 still apply, so the
     quality and schema guards keep their veto.
   * *incomparable* (traded — improved some, worsened others) → fall
     through to Rule 1, which becomes the TIEBREAKER. Dominance cannot rank
     a trade; the scalar weights are precisely the operator's stated
     resolution for when their objectives disagree.
   * *unranked* (no axis comparable on both sides) → fall through to the
     scalar rules unchanged.

   This is what makes a declared frontier *pursued* rather than merely
   observed: the axis vocabulary is shared verbatim with the score-trajectory
   projection, so a generation the dashboard shows on the frontier is one this
   gate treats as non-dominated.

1. **Scalar margin.** The child's combined scalar must beat the parent's
   by at least :attr:`ScoringWeights.promote_margin`. Skipped when Rule 1a
   returned *dominates*. The scalar is
   "lower-is-better", so the literal check is::

       child.scalar > parent.scalar - promote_margin  →  reject

   A child whose loss improved but by less than ``promote_margin`` is
   rejected with an ``"insufficient improvement"`` reason; a child whose
   loss actually ROSE is rejected with a ``"challenger regressed"``
   reason. Both reasons state the real child-minus-parent delta.

2. **Pass-rate monotonicity** (when
   :attr:`ScoringWeights.pass_rate_monotonicity` is true). The
   *granularity* is selected by
   :attr:`ScoringWeights.pass_rate_monotonicity_scope`:

   * ``"per_entry"`` (default) — for every entry the parent SCORED, the
     child's continuous score may not drop below the parent's by more
     than :data:`PER_ENTRY_SCORE_MONOTONICITY_TOLERANCE`. A BOOL entry
     the parent passed has score ``1.0``, so the child must still score
     ``1.0`` (within the tiny tolerance) — i.e. it must still pass,
     exactly as before. A continuous entry may dip by the tolerance band
     to absorb small-board scoring jitter. If any tracked entry regressed,
     the gate rejects with the entry ids listed in the reason. Entries the
     parent failed (score ``0.0``) or had no expectation for are not gated.
     The right policy for invariant / regression-suite boards.
   * ``"aggregate"`` — reject only when the child's OVERALL ``mean_score``
     fell below the parent's by more than
     :data:`PASS_RATE_MONOTONICITY_TOLERANCE`. The child may trade
     individual entries as long as the net continuous outcome holds or
     improves. (``mean_score`` equals the binary pass-rate on an all-bool
     board, so this is byte-identical there.) The right policy for sampled
     evaluation boards where individual pass/fail is noisy.

   ``off`` is expressed by ``pass_rate_monotonicity=False`` rather than a
   third scope value, so existing contracts are byte-identical.

3. **Per-namespace monotonicity.** For each namespace whose flag in
   :attr:`ScoringWeights.namespace_monotonicity` is ``True``, the
   child's per-namespace aggregate may not have moved in the
   namespace's "worse" direction relative to the parent's. The
   direction is encoded by the sign of the namespace's coefficient in
   :attr:`ScoringWeights.namespace_weights`:

   * positive weight → higher aggregate is worse (drift, cost,
     latency, schema);
   * negative weight → higher aggregate is better (rubric);
   * zero weight → no enforced direction (rule skipped for this
     namespace even if monotonicity is requested).

   Because :func:`aggregate_namespaced_metrics` already multiplies
   each namespace's mean by its signed weight, an inspector can ignore
   the sign at gate time and just check whether the child's
   *weighted* aggregate is greater than the parent's: the weight has
   already turned the namespace into a unified lower-is-better axis.
   If any monotonicity-tracked namespace regressed, the gate rejects
   and names every regressing namespace in the reason. Namespaces
   whose flag is missing or ``False`` are not gated this way.

   Note: this rule is ALREADY aggregate-scoped — it compares per-namespace
   *means*, not per-entry pass/fail — so the issue #17 per-entry-vs-
   aggregate scope does NOT apply to it. The analogous knob here would be
   "all-tracked-namespaces combined vs each namespace individually", a
   different axis the operator already controls by choosing which
   namespaces to flag in ``namespace_monotonicity``. A combined-axis scope
   is a documented follow-up, not built here (see SCORING.md §5.2).

   Note also: this rule is independent of Rule 1a. Rule 1a compares the
   axes that the operator declared. This rule compares each namespace with
   a ``True`` flag in ``namespace_monotonicity``. A namespace can be in one
   set, or in both sets, or in neither set. Rule 1a can let a challenger
   continue, but this rule can still reject that challenger.

If no rule rejects, the gate promotes — UNLESS a holdout-confirmation
step is supplied (OVERFITTING.md §1/§12 #1, §13). When the caller passes
a held-out slice's parent/child aggregates, a train-measured win must
*also* confirm on the holdout: the challenger's holdout scalar may not
regress past the HOLDOUT margin
(:func:`effective_holdout_margin` — :attr:`ScoringWeights.holdout_margin`,
falling back to ``promote_margin``) versus the champion's, and the holdout
must not show a pass-rate regression beyond
:attr:`ScoringWeights.holdout_entry_regression_budget` entries under the
SAME :attr:`ScoringWeights.pass_rate_monotonicity_scope` the train slice
uses (per-entry on both sides, or aggregate on both). Both holdout bounds
are separate knobs because the holdout is the SMALLER slice and therefore
the coarser-quantized one; sharing the train knob left real board shapes
with no promotable margin at all (issue #118). At their defaults
(``holdout_margin=None``, budget ``0``) the confirmation is byte-identical
to the single-knob version. A failed confirmation
is just another reason to ``reject`` (reason ``holdout_not_confirmed``);
the champion stands, exactly as on any other reject — the
protected-incumbent invariant is untouched. The holdout is
confirmation-only: it never steers selection or the proposer, and it is
applied AFTER the three train-slice rules so a train reject still fires
first with its specific reason. When no holdout is supplied (the board
was too small to split, or the split is disabled) the step is skipped
entirely and the decision is byte-identical to the pre-split gate.

The :class:`GateOutcome` records the delta values regardless of the
decision, so the journal always has the same shape of evidence to
render whether the experiment passed, failed, or was deferred.

Alongside the verdict — and never as part of it — the outcome carries
``attributable_regressions``: the entries that regressed on their OWN
per-entry evidence, whichever way the duel went (see
:func:`attributable_entry_regressions`). No rule reads the per-entry
``drift_loss`` the aggregates have always carried, so an entry whose quality
collapses while it still PASSES is invisible to all three rules and to the
per-namespace means; and under ``aggregate`` scope a net-positive challenger
may break an entry by design. Both cases bake the loss into the lineage
silently. This reports them and stops there: it is WARN-ONLY, it never vetoes,
and it stays out of ``reason`` so the empty-reason-on-promote invariant holds.

The gate uses ``decision="deferred"`` ONLY when called explicitly by a
caller who has decided neither rule cleanly fired — the function in
this module returns ``"promoted"`` or ``"rejected"``. (Deferral is a
runner-level concept, kept in the :class:`TournamentDecision` literal
type so callers can pre-merge their own deferral logic without a
schema bump.)
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from zicato.core import ScoringWeights, TournamentDecision
from zicato.scoring.diff_complexity import diff_complexity

#: Default tolerance applied to the child-vs-parent delta for a
#: monotonicity-tracked namespace. The check is
#: ``child_weighted > parent_weighted + tolerance`` so a tiny numerical
#: drift does not trip the gate. Operators who want stricter or looser
#: behaviour per namespace can override by computing aggregates
#: themselves; the surface is intentionally simple at this layer.
NAMESPACE_MONOTONICITY_TOLERANCE: float = 0.0

#: Float-noise tolerance applied to the overall pass-rate / mean-score delta
#: under ``aggregate``-scope monotonicity. The check is
#: ``delta < -PASS_RATE_MONOTONICITY_TOLERANCE`` so an aggregate that is
#: *equal* within numerical noise (e.g. ``0.31`` reconstructed two different
#: ways) is treated as "held", not "regressed". A genuine net regression —
#: one whose drop exceeds this band — still rejects. Kept small: pass-rate is
#: a ratio of integer counts, so the only noise here is float
#: division/round-trip, not measurement variance.
PASS_RATE_MONOTONICITY_TOLERANCE: float = 1e-9

#: Tolerance band for a PER-ENTRY continuous-score regression under
#: ``per_entry``-scope monotonicity. A continuous score may *dip* by up to
#: this much on an entry the parent scored higher without tripping the gate —
#: small-board F1 / similarity scores carry measurement jitter, and a knife-
#: edge per-entry rule would make the gate as jumpy as the binary path it
#: replaces. The check is ``child_score < parent_score - tolerance`` ⇒ reject.
#: A BINARY entry the parent passed (score == 1.0) is the limiting case: any
#: child score below ``1.0 - tolerance`` is a regression, so a bool 1.0 -> 0.0
#: flip rejects exactly as the historical "must-still-pass" rule did. Kept
#: small so the bool case stays effectively a strict must-still-pass.
PER_ENTRY_SCORE_MONOTONICITY_TOLERANCE: float = 0.02

#: Ratio limb of the per-entry DRIFT regression band (issue #130). An entry's
#: drift loss counts as regressed only when the child's exceeds this multiple
#: of the parent's — a relative test, because drift loss has no natural scale
#: and the interesting failure is a collapse (0.10 -> 0.60), not a nudge.
ATTRIBUTABLE_DRIFT_RATIO: float = 2.0

#: Absolute limb of the same band. Near zero the ratio limb degenerates —
#: 0.001 -> 0.003 triples and means nothing — so the child must ALSO exceed the
#: parent by this much before the entry is reported. Both limbs must fire:
#: ``child > max(RATIO * parent, parent + ABSOLUTE)``.
ATTRIBUTABLE_DRIFT_ABSOLUTE: float = 0.05


def effective_holdout_margin(weights: ScoringWeights) -> float:
    """The scalar tolerance the HOLDOUT confirmation applies (issue #118).

    :attr:`ScoringWeights.holdout_margin` when the operator set one, else
    :attr:`ScoringWeights.promote_margin` — so a contract that never heard of
    the field behaves exactly as before.

    The two bounds want different values. ``promote_margin`` is calibrated
    against the TRAIN slice and must be small enough for a real win to clear
    Rule 1; the holdout tolerance is calibrated against the HOLDOUT slice and
    must be large enough to absorb that slice's own quantization. A slice of N
    entries moves its scalar in ``1/N`` steps and the holdout is the smaller
    slice by construction, so its steps are the coarser ones — one knob served
    both only by luck of the split. See
    :attr:`ScoringWeights.holdout_margin` for the commensurable-bounds rule of
    thumb (``promote_margin × N_train / N_holdout``).
    """
    margin = weights.holdout_margin
    return float(weights.promote_margin if margin is None else margin)


@dataclass(frozen=True, slots=True)
class GateOutcome:
    """The promote-gate verdict plus the deltas that produced it.

    Fields
    ------
    decision:
        ``"promoted"`` | ``"rejected"`` | ``"deferred"``. The function
        :func:`evaluate_gate` returns ``"promoted"`` or ``"rejected"``
        only; deferral is reserved for higher-level callers.
    reason:
        Human-readable explanation. Empty string when promoted. For
        rejections, identifies which rule fired and (for pass-rate
        regressions) which entries.
    delta_scalar:
        ``child.scalar - parent.scalar``. Negative = improvement.
    delta_pass_rate:
        ``child.pass_rate - parent.pass_rate``. Positive = improvement.
    attributable_regressions:
        Sorted ids of the entries that regressed on their OWN evidence,
        whatever the verdict was (see
        :func:`attributable_entry_regressions`). Populated on BOTH
        decisions and deliberately NOT folded into ``reason``: the
        empty-reason-on-promote invariant is load-bearing for consumers
        that read a non-empty reason as a rejection, and this report is a
        warning, never a veto. Empty by default, so every caller that
        builds a :class:`GateOutcome` by hand is unaffected.
    """

    decision: TournamentDecision
    reason: str
    delta_scalar: float
    delta_pass_rate: float
    attributable_regressions: tuple[str, ...] = ()


def _regressed_namespaces(
    parent_agg: dict[str, Any],
    child_agg: dict[str, Any],
    weights: ScoringWeights,
) -> list[str]:
    """Return namespaces whose monotonicity flag fires (sorted).

    A namespace is considered regressed when its weighted aggregate on
    the child side is strictly worse than the parent's by more than
    :data:`NAMESPACE_MONOTONICITY_TOLERANCE`. "Worse" is namespace-
    dependent in the raw mean view, but because
    :func:`aggregate_namespaced_metrics` already folds the sign of the
    namespace weight into the aggregate, the comparison reduces to:

        child_weighted_aggregate > parent_weighted_aggregate + tolerance

    Namespaces whose weight is zero are skipped — the operator has
    explicitly disabled scoring contribution for them, so it would be
    surprising to gate the promotion on their movement.

    Namespaces named in :attr:`ScoringWeights.namespace_monotonicity`
    but missing from the parent or child aggregates are silently
    skipped — we cannot judge regression without two points to compare.
    """
    parent_ns: dict[str, Any] = parent_agg.get("namespace_aggregates", {}) or {}
    child_ns: dict[str, Any] = child_agg.get("namespace_aggregates", {}) or {}
    regressed: list[str] = []
    for ns, enabled in weights.namespace_monotonicity.items():
        if not enabled:
            continue
        # Skip namespaces whose direction is undefined (zero weight).
        if weights.namespace_weights.get(ns, 0.0) == 0.0:
            continue
        if ns not in parent_ns or ns not in child_ns:
            continue
        parent_val = float(parent_ns[ns])
        child_val = float(child_ns[ns])
        if child_val > parent_val + NAMESPACE_MONOTONICITY_TOLERANCE:
            regressed.append(ns)
    regressed.sort()
    return regressed


#: Float-noise tolerance on ONE declared Pareto objective. An axis moving by
#: less than this counts as HELD, not worsened — the same doctrine as
#: :data:`PASS_RATE_MONOTONICITY_TOLERANCE`, applied per objective. Kept tiny:
#: this absorbs float round-trip through ``gen_score.json``, not measurement
#: variance. Real noise is the ``promote_margin`` tiebreaker's job.
PARETO_OBJECTIVE_TOLERANCE: float = 1e-9


def objective_vector(agg: dict[str, Any], axes: Iterable[str]) -> dict[str, float]:
    """Project one aggregate onto the declared ``axes``, lower-is-better.

    THE axis vocabulary — shared verbatim with the score-trajectory frontier
    (:func:`zicato.query.gate_view._pareto_objectives`) so the gate ranks on
    exactly the axes the dashboard draws. A generation the operator sees on
    the frontier is one this gate would call non-dominated:

    * ``"drift_loss"`` — ``drift_loss_mean``;
    * ``"quality_loss"`` — ``1 - mean_score`` (falling back to ``pass_rate``
      on pre-continuous aggregates, where the two are byte-identical);
    * ``"namespace:<ns>"`` — one ``namespace_aggregates`` entry, already
      sign-folded by its :attr:`ScoringWeights.namespace_weights` coefficient
      and therefore already lower-is-better.

    An axis the aggregate does not supply is OMITTED rather than defaulted:
    zero is the best possible value on a lower-is-better axis, so defaulting
    would let a generation that never measured an axis dominate one that did.
    :func:`pareto_comparison` treats a missing axis as "not comparable", not
    as "won".
    """
    out: dict[str, float] = {}
    ns_agg: dict[str, Any] = agg.get("namespace_aggregates", {}) or {}
    for axis in axes:
        if axis == "drift_loss":
            value = agg.get("drift_loss_mean")
        elif axis == "quality_loss":
            if "mean_score" in agg or "pass_rate" in agg:
                value = 1.0 - _mean_score(agg)
            else:
                value = None
        elif axis.startswith("namespace:"):
            value = ns_agg.get(axis[len("namespace:") :])
        else:
            value = None
        if value is None or isinstance(value, bool):
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if number != number or number in (float("inf"), float("-inf")):  # NaN / inf
            continue
        out[axis] = number
    return out


def pareto_comparison(
    parent_agg: dict[str, Any],
    child_agg: dict[str, Any],
    weights: ScoringWeights,
) -> tuple[str, list[str], list[str]]:
    """Compare two aggregates on the operator's declared objectives.

    Returns ``(verdict, improved_axes, worsened_axes)`` where ``verdict`` is:

    * ``"dominates"`` — the child holds every declared objective (within
      :data:`PARETO_OBJECTIVE_TOLERANCE`) and strictly improves at least one.
      A promotion.
    * ``"dominated"`` — the mirror image: the child worsens at least one
      objective and improves none. An unambiguous reject.
    * ``"incomparable"`` — the child trades: it improves some objectives and
      worsens others. Neither point dominates, so BOTH sit on the frontier and
      dominance alone cannot pick a winner. :func:`evaluate_gate` breaks this
      tie with the scalar, which is exactly the weighting the operator already
      declared for the case where the objectives disagree.
    * ``"unranked"`` — no axis could be compared on both sides (an aggregate
      predating an axis, or a profile naming axes nothing reports). The caller
      falls back to the scalar rules entirely rather than promoting on no
      evidence.

    Every axis is lower-is-better (see :func:`objective_vector`), so
    "improved" means the child's value FELL. Axes present on only one side are
    skipped — see :func:`objective_vector` on why they are not defaulted.
    """
    axes = list(weights.pareto_objectives)
    parent_vec = objective_vector(parent_agg, axes)
    child_vec = objective_vector(child_agg, axes)
    comparable = [axis for axis in axes if axis in parent_vec and axis in child_vec]
    if not comparable:
        return "unranked", [], []

    improved: list[str] = []
    worsened: list[str] = []
    for axis in comparable:
        delta = child_vec[axis] - parent_vec[axis]
        if delta < -PARETO_OBJECTIVE_TOLERANCE:
            improved.append(axis)
        elif delta > PARETO_OBJECTIVE_TOLERANCE:
            worsened.append(axis)
    if improved and not worsened:
        return "dominates", improved, worsened
    if worsened and not improved:
        return "dominated", improved, worsened
    if not improved and not worsened:
        # Held flat on every objective — no strict improvement, so it does not
        # dominate. Same outcome as any other non-improving challenger.
        return "dominated", improved, worsened
    return "incomparable", improved, worsened


def _pareto_axis_evidence(
    parent_agg: dict[str, Any],
    child_agg: dict[str, Any],
    weights: ScoringWeights,
    axes: list[str],
) -> str:
    """``axis (champion X -> challenger Y)`` for each cited axis."""
    parent_vec = objective_vector(parent_agg, axes)
    child_vec = objective_vector(child_agg, axes)
    label_of = weights.pareto_objectives
    parts = []
    for axis in axes:
        label = (label_of.get(axis) or "").strip() or axis
        parts.append(
            f"{label} (champion {parent_vec[axis]:.6f} -> challenger {child_vec[axis]:.6f})"
        )
    return ", ".join(parts)


def _namespace_regression_reason(
    parent_agg: dict[str, Any],
    child_agg: dict[str, Any],
    regressed: list[str],
) -> str:
    """Rule 3's rejection reason, citing each namespace's two aggregates.

    The names alone said WHICH namespaces regressed and never by how much,
    so an operator could not tell a hair over the tolerance from a
    collapse, nor which of three cited namespaces to look at first (issue
    #129). The values are the sign-folded weighted aggregates the rule
    compared — higher is worse in that view, which is why the champion's
    number reads lower than the challenger's on every namespace listed.
    """
    parent_ns: dict[str, Any] = parent_agg.get("namespace_aggregates", {}) or {}
    child_ns: dict[str, Any] = child_agg.get("namespace_aggregates", {}) or {}
    parts = [
        f"{ns} (champion {float(parent_ns[ns]):.6f} -> " f"challenger {float(child_ns[ns]):.6f})"
        for ns in regressed
    ]
    return (
        "monotonicity_regression on namespace="
        + ", ".join(parts)
        + "; a promotion needs each flagged namespace to stay at or below the "
        f"champion's weighted aggregate (tolerance {NAMESPACE_MONOTONICITY_TOLERANCE:g})"
    )


def _row_score(row: dict[str, Any] | None) -> float | None:
    """Read one ``per_entry`` row's continuous outcome in ``[0, 1]``, or ``None``.

    The single reader the per-entry gate scope trusts, and the seam that
    keeps the continuous rule byte-identical to the historical bool rule:

    * a row carrying an explicit ``"score"`` (the new
      :func:`zicato.tournament.scoring.entry_score` output) uses it,
      clamped to ``[0, 1]``; a non-finite score is treated as a miss
      (``0.0``);
    * a row WITHOUT a ``"score"`` key — a pre-score aggregate, or a
      hand-built one — falls back to the binary ``pass_fail`` bit
      (True->1.0, False->0.0). This is what makes an all-bool /
      score-less aggregate score exactly as it did before this field
      existed;
    * a row with neither (``pass_fail is None`` and no score) returns
      ``None`` — no ground truth, excluded from the rule, exactly as the
      historical rule skipped entries the parent had no clean pass for.
    """
    if row is None:
        return None
    if "score" in row and row["score"] is not None:
        value = float(row["score"])
        if value != value or value in (float("inf"), float("-inf")):  # NaN / inf
            return 0.0
        if value < 0.0:
            return 0.0
        if value > 1.0:
            return 1.0
        return value
    pf = row.get("pass_fail")
    if pf is None:
        return None
    return 1.0 if pf else 0.0


def _regressed_entries(parent_agg: dict[str, Any], child_agg: dict[str, Any]) -> list[str]:
    """Return ids whose per-entry outcome regressed beyond tolerance (sorted).

    The per_entry scope under CONTINUOUS scores: for every entry the
    parent scored (``parent_score is not None``), the child's score may
    not drop below ``parent_score - PER_ENTRY_SCORE_MONOTONICITY_TOLERANCE``.
    A child that no longer evaluates a previously-evaluated expectation
    (no row, or ``score``/``pass_fail`` both ``None``) is treated as a
    score of ``0.0`` — a child that drops ground truth is still a
    regression in the operator's view, and the gate flags it (matching the
    historical "child no longer passes" treatment of a vanished row).

    Byte-identical to the historical bool rule on a score-less / all-bool
    board: there a parent-passed entry has ``parent_score == 1.0`` and any
    child below ``1.0 - tolerance`` (i.e. a 1.0 -> 0.0 flip, or a vanished
    row read as 0.0) regresses, while a parent-FAILED entry
    (``parent_score == 0.0``) is never gated because no child score can
    fall below ``0.0 - tolerance``. The small tolerance only loosens the
    purely-continuous case; it cannot make a bool pass->fail flip slip
    through.
    """
    parent_per: dict[str, dict[str, Any]] = parent_agg.get("per_entry", {})
    child_per: dict[str, dict[str, Any]] = child_agg.get("per_entry", {})
    regressed: list[str] = []
    for entry_id, parent_row in parent_per.items():
        parent_score = _row_score(parent_row)
        if parent_score is None:
            continue
        child_score = _row_score(child_per.get(entry_id))
        effective_child = 0.0 if child_score is None else child_score
        if effective_child < parent_score - PER_ENTRY_SCORE_MONOTONICITY_TOLERANCE:
            regressed.append(entry_id)
    regressed.sort()
    return regressed


def _row_drift(row: dict[str, Any] | None) -> float | None:
    """Read one ``per_entry`` row's ``drift_loss``, or ``None`` when absent.

    A non-numeric or missing value reads as ``None`` — no measurement, so no
    comparison — rather than as a zero, which would make "this row carries no
    drift" indistinguishable from "this row measured no drift" and could
    manufacture a regression out of a hand-built aggregate.
    """
    if row is None:
        return None
    value = row.get("drift_loss")
    if value is None or isinstance(value, bool):
        return None
    try:
        drift = float(value)
    except (TypeError, ValueError):
        return None
    if drift != drift:  # NaN
        return None
    return drift


def _drift_regressed_entries(parent_agg: dict[str, Any], child_agg: dict[str, Any]) -> list[str]:
    """Return ids whose per-entry DRIFT loss collapsed (sorted).

    The axis no gate rule reads (issue #130). Rules 1-3 decide on the scalar,
    on per-entry ``score`` / ``pass_fail``, and on per-namespace MEANS
    respectively — so an entry whose drift loss multiplies while it still
    PASSES is invisible to all three, and a simultaneous improvement on
    another entry hides it from the namespace mean as well. The per-entry rows
    already carry ``drift_loss`` on both sides; this simply reads it.

    An entry counts as regressed when

        ``child_drift > max(RATIO * parent_drift, parent_drift + ABSOLUTE)``

    — see :data:`ATTRIBUTABLE_DRIFT_RATIO` / :data:`ATTRIBUTABLE_DRIFT_ABSOLUTE`
    for why both limbs are required. Entries with no drift measurement on
    either side are skipped.
    """
    parent_per: dict[str, dict[str, Any]] = parent_agg.get("per_entry", {})
    child_per: dict[str, dict[str, Any]] = child_agg.get("per_entry", {})
    regressed: list[str] = []
    for entry_id, parent_row in parent_per.items():
        parent_drift = _row_drift(parent_row)
        if parent_drift is None:
            continue
        child_drift = _row_drift(child_per.get(entry_id))
        if child_drift is None:
            continue
        band = max(
            ATTRIBUTABLE_DRIFT_RATIO * parent_drift,
            parent_drift + ATTRIBUTABLE_DRIFT_ABSOLUTE,
        )
        if child_drift > band:
            regressed.append(entry_id)
    regressed.sort()
    return regressed


def attributable_entry_regressions(
    parent_agg: dict[str, Any],
    child_agg: dict[str, Any],
) -> tuple[str, ...]:
    """Return the entries that regressed on their own evidence (sorted).

    The union of the two per-entry axes the aggregates carry: the continuous
    outcome (:func:`_regressed_entries` — the same reader Rule 2's per_entry
    scope uses) and the drift loss (:func:`_drift_regressed_entries`).

    This is OBSERVATION, not policy. It is computed on every duel and reported
    on both verdicts, independent of ``pass_rate_monotonicity`` and its scope:
    a contract that chose ``aggregate`` scope chose to PERMIT entry trades, not
    to stop hearing about them, and an entry broken by a promotion is baked
    into the lineage from that round on. Nothing here rejects — see
    :class:`GateOutcome.attributable_regressions`.
    """
    ids = set(_regressed_entries(parent_agg, child_agg))
    ids.update(_drift_regressed_entries(parent_agg, child_agg))
    return tuple(sorted(ids))


def attributable_regression_detail(
    parent_agg: dict[str, Any],
    child_agg: dict[str, Any],
) -> dict[str, dict[str, float | None]]:
    """Return ``{entry_id: {parent/child score + drift}}`` for the regressions.

    The evidence behind :func:`attributable_entry_regressions`, in the shape
    the health finding's ``detail`` carries: for each reported entry, both
    sides' continuous score and drift loss, with ``None`` where the aggregate
    carried no measurement. Single-sources the computation so the finding can
    never name an entry the gate outcome did not.
    """
    parent_per: dict[str, dict[str, Any]] = parent_agg.get("per_entry", {})
    child_per: dict[str, dict[str, Any]] = child_agg.get("per_entry", {})
    detail: dict[str, dict[str, float | None]] = {}
    for entry_id in attributable_entry_regressions(parent_agg, child_agg):
        detail[entry_id] = {
            "parent_score": _row_score(parent_per.get(entry_id)),
            "child_score": _row_score(child_per.get(entry_id)),
            "parent_drift_loss": _row_drift(parent_per.get(entry_id)),
            "child_drift_loss": _row_drift(child_per.get(entry_id)),
        }
    return detail


def _mean_score(agg: dict[str, Any]) -> float:
    """Read an aggregate's overall continuous outcome, defaulting to ``1.0``.

    The aggregate scope runs on ``mean_score`` — the uniform continuous
    outcome :func:`zicato.tournament.scoring.aggregate_generation_score`
    now reports. Because ``mean_score`` equals ``pass_rate`` on an all-bool
    board, an aggregate that predates the field (or a hand-built one that
    only carries ``pass_rate``) reads identically: the lookup falls back to
    ``pass_rate`` and then to ``1.0`` for a board with no expectations.
    This keeps the aggregate scope byte-identical for all-bool boards while
    letting a graded board's net quality move continuously.
    """
    if "mean_score" in agg:
        return float(agg["mean_score"])
    return float(agg.get("pass_rate", 1.0))


def _pass_rate_regression_reason(
    parent_agg: dict[str, Any],
    child_agg: dict[str, Any],
    weights: ScoringWeights,
    *,
    prefix: str = "",
    entry_budget: int = 0,
) -> str:
    """Return the pass-rate monotonicity reject reason, or ``""`` to allow.

    Honors :attr:`ScoringWeights.pass_rate_monotonicity_scope`:

    * ``per_entry`` — reject when ANY entry the parent passed regressed
      (the historical behaviour; byte-identical reason for the gate when
      ``prefix`` is empty).
    * ``aggregate`` — reject only when the child's OVERALL pass-rate fell
      below the parent's by more than
      :data:`PASS_RATE_MONOTONICITY_TOLERANCE`.

    The caller has already checked that ``pass_rate_monotonicity`` is on.
    ``prefix`` lets the holdout reuse the same wording with its
    ``holdout_not_confirmed: holdout `` lead-in.

    ``entry_budget`` (issue #118) is how many regressed entries to tolerate
    before rejecting. ``0`` — the only value the TRAIN side ever passes — is
    exactly the historical rule under both scopes. The holdout passes
    :attr:`ScoringWeights.holdout_entry_regression_budget`, and the two scopes
    express the same allowance differently: per-entry it is a COUNT, while
    aggregate widens the mean-score band by the movement that many flips
    produce (``budget / entries``), so an operator's budget of 1 means the
    same thing whichever scope the contract pins.
    """
    if weights.pass_rate_monotonicity_scope == "aggregate":
        parent_pass = _mean_score(parent_agg)
        child_pass = _mean_score(child_agg)
        delta = child_pass - parent_pass
        # A budget of N entries is N/scored-entries of mean-score movement on
        # this slice. The denominator must be the SCORED rows, because that is
        # the denominator ``mean_score`` itself uses
        # (:func:`zicato.tournament.scoring.aggregate_generation_score`'s
        # ``score_count``) — counting unscored rows too would silently shrink
        # the band below the one entry the operator asked for. With no scored
        # rows (a hand-built or expectation-free aggregate) there is no scale to
        # convert on, so the budget contributes nothing rather than an
        # arbitrary amount.
        scored = sum(
            1
            for row in (parent_agg.get("per_entry", {}) or {}).values()
            if _row_score(row) is not None
        )
        budget_band = (entry_budget / scored) if (entry_budget > 0 and scored) else 0.0
        if delta < -(PASS_RATE_MONOTONICITY_TOLERANCE + budget_band):
            # Wording kept as "pass-rate" for back-compat with consumers
            # that match this reason text; the quantity is now mean_score,
            # which equals pass_rate on an all-bool board.
            return (
                f"{prefix}pass-rate regression: overall pass-rate fell by "
                f"{-delta:.6f} "
                f"(champion {parent_pass:.6f} -> challenger {child_pass:.6f})"
            )
        return ""
    # per_entry (default): every entry the parent scored must hold within
    # tolerance (a bool entry the parent passed must still pass — see
    # :func:`_regressed_entries`), save for the first ``entry_budget`` of them.
    regressed = _regressed_entries(parent_agg, child_agg)
    if len(regressed) <= entry_budget:
        regressed = []
    if regressed:
        return f"{prefix}pass-rate regression on entries: " + ", ".join(regressed)
    return ""


def _holdout_confirms(
    holdout_parent_agg: dict[str, Any],
    holdout_child_agg: dict[str, Any],
    weights: ScoringWeights,
) -> str:
    """Return ``""`` when the holdout confirms the win, else a reason.

    Reuses the same machinery the train slice is gated by — a scalar
    regression band on the holdout scalar, and the pass-rate monotonicity
    check on the holdout's per-entry rows — but in *confirmation* form: the
    challenger must merely *not regress* on the holdout. Concretely it
    rejects when

    * the challenger's holdout loss rose past the champion's by more than
      :func:`effective_holdout_margin` (a real holdout regression, not
      noise), OR
    * the challenger regressed on pass-rate monotonicity beyond
      :attr:`ScoringWeights.holdout_entry_regression_budget` entries
      (reusing :func:`_pass_rate_regression_reason`, gated by
      ``pass_rate_monotonicity`` so operators who disabled it on the train
      side disable it here too, and honoring the SAME
      ``pass_rate_monotonicity_scope`` so train and holdout apply one
      consistent policy — per-entry on both sides, or aggregate on both).

    Both bounds are the HOLDOUT's own (issue #118). Reusing the train knob
    for the scalar band pulled one number in two directions at once, and the
    pass-rate rule had no operator tolerance at all — only its float-noise
    band — so on a 6-entry holdout a single entry flipping pass→fail
    rejected at every margin. That contradicts this step's own doctrine
    (below): a confirmation that no achievable margin can satisfy is not a
    confirmation, it is a second gate. Both knobs default to exactly the
    historical strictness.

    The holdout is never asked to clear the margin in the *improving*
    direction — a train-measured win that merely holds flat on the holdout
    is a confirmation, not a failure. This is the asymmetry that makes the
    holdout a guard against board-memorization rather than a second,
    stricter promotion bar.
    """
    margin = effective_holdout_margin(weights)
    parent_scalar = float(holdout_parent_agg["scalar"])
    child_scalar = float(holdout_child_agg["scalar"])
    # A holdout regression: the challenger's holdout loss rose past the
    # champion's by more than the noise band. (delta > +margin ⇒ regressed.)
    if child_scalar - parent_scalar > margin:
        return (
            f"holdout_not_confirmed: holdout loss rose by "
            f"{child_scalar - parent_scalar:.6f} "
            f"(champion {parent_scalar:.6f} -> challenger {child_scalar:.6f}); "
            f"a train-measured win must hold within {margin:.6f} "
            f"on the holdout slice"
        )
    if weights.pass_rate_monotonicity:
        reason = _pass_rate_regression_reason(
            holdout_parent_agg,
            holdout_child_agg,
            weights,
            prefix="holdout_not_confirmed: holdout ",
            entry_budget=int(weights.holdout_entry_regression_budget),
        )
        if reason:
            return reason
    return ""


def holdout_confirms(
    holdout_parent_agg: dict[str, Any],
    holdout_child_agg: dict[str, Any],
    weights: ScoringWeights,
) -> str:
    """Public wrapper for the holdout-confirmation check (OVERFITTING.md §12 #1).

    Returns ``""`` when the holdout confirms the train-measured win, or the
    ``holdout_not_confirmed`` reason otherwise. Exposed so the Ladder governor
    (:mod:`zicato.tournament.ladder`) can compute the raw confirmation bit
    *out of band* — the Ladder then decides whether to release that bit this
    round. :func:`evaluate_gate` still calls the same logic internally for the
    non-Ladder (raw Phase-A) path.
    """
    return _holdout_confirms(holdout_parent_agg, holdout_child_agg, weights)


def _component(agg: dict[str, Any], name: str) -> float:
    """Read one named entry of an aggregate's ``scalar_components``, or ``0.0``.

    An absent component is exactly zero by construction: the scoring layer
    omits the key rather than writing a zero when a term is inactive.
    """
    components = agg.get("scalar_components")
    if not isinstance(components, dict):
        return 0.0
    value = components.get(name)
    if value is None or isinstance(value, bool):
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _parsimony_decomposition(
    parent_agg: dict[str, Any],
    child_agg: dict[str, Any],
    weights: ScoringWeights,
    delta_scalar: float,
) -> str:
    """Return the parsimony half of a Rule 1 rejection, or ``""`` when inert.

    Rule 1 compares two scalars, and when the opt-in diff-complexity term is
    active one of those scalars contains a toll the challenger paid for the
    SIZE of its edit rather than for anything it did to the board. A challenger
    that won two entries out of twelve and paid a larger toll for re-emitting a
    whole-file template nets positive and is rejected — with a message that
    says only that the loss rose, which reads as a board regression that never
    happened (issue #120(b)).

    So when the toll MOVED against the challenger, the reason states the split:
    the raw quality delta (the scalars with the parsimony component removed —
    what the board actually said), the toll delta, and, when the raw delta
    alone would have cleared ``promote_margin``, that fact in words. The
    per-side ``{added, removed, patches}`` sizes come from
    :func:`diff_size_evidence`, so the operator can see what the toll was
    charged for.

    Returns ``""`` when the term is off, or when the toll did not move against
    the challenger — at the default ``diff_complexity_weight == 0.0`` no
    aggregate carries the component and the reason is byte-identical to before.
    """
    toll_delta = _component(child_agg, "diff_complexity") - _component(
        parent_agg, "diff_complexity"
    )
    if toll_delta <= 0.0:
        return ""
    raw_delta = delta_scalar - toll_delta
    parts = [f"diff_complexity: raw quality delta {raw_delta:+.6f}, toll {toll_delta:+.6f}"]
    if raw_delta <= -weights.promote_margin:
        parts.append("the raw delta alone would have cleared the promote margin")
    parts.extend(diff_size_evidence(parent_agg, child_agg))
    return " [" + "; ".join(parts) + "]"


def evaluate_gate(
    parent_agg: dict[str, Any],
    child_agg: dict[str, Any],
    weights: ScoringWeights,
    *,
    holdout_parent_agg: dict[str, Any] | None = None,
    holdout_child_agg: dict[str, Any] | None = None,
) -> GateOutcome:
    """Apply the promote gate. See module docstring for the rules.

    ``parent_agg`` / ``child_agg`` are the TRAIN-slice aggregates (or the
    full-board aggregates when the board was not split) — the three rules
    decide on them, and selection / standings read the train scalar. When
    ``holdout_parent_agg`` / ``holdout_child_agg`` are supplied AND the
    train rules would promote, the win must also confirm on the holdout
    (see :func:`_holdout_confirms`); a failure flips the promotion to a
    ``reject`` with reason ``holdout_not_confirmed``. Both holdout
    arguments ``None`` (the small-board / disabled case) skips the step,
    leaving the decision byte-identical to the pre-split gate. The reported
    deltas are always the train-side deltas so the journal's evidence shape
    is unchanged.
    """
    parent_scalar = float(parent_agg["scalar"])
    child_scalar = float(child_agg["scalar"])
    parent_pass = float(parent_agg.get("pass_rate", 1.0))
    child_pass = float(child_agg.get("pass_rate", 1.0))

    delta_scalar = child_scalar - parent_scalar
    delta_pass_rate = child_pass - parent_pass

    # Per-entry regressions attributable to THIS duel, on every path and both
    # verdicts (issue #130). Observation only — it never changes a decision and
    # never enters ``reason``; the round log and the health report are where it
    # surfaces. Computed once here so every early return carries the same set.
    attributable = attributable_entry_regressions(parent_agg, child_agg)

    # Rule 0: diff-complexity ceiling (OPT-IN, default 0.0 = OFF). A structural
    # admissibility veto — a challenger whose diff complexity
    # (``added + removed + patches``) exceeds the ceiling is rejected outright,
    # BEFORE the scoring rules, so the reason names the ceiling rather than a
    # scoring near-miss the over-budget edit may or may not also trip. The
    # challenger ``diff_size`` is present on ``child_agg`` only when the
    # parsimony machinery is active (see ``aggregate_generation_score``); at the
    # default ceiling this branch is skipped and the decision is byte-identical
    # to a contract without the field.
    ceiling = float(weights.diff_complexity_ceiling)
    if ceiling > 0.0:
        diff_size = child_agg.get("diff_size")
        if isinstance(diff_size, dict):
            complexity = diff_complexity(diff_size)
            if complexity > ceiling:
                return GateOutcome(
                    decision=TournamentDecision.REJECTED,
                    reason=(
                        f"diff_complexity_ceiling: diff complexity {complexity:g} "
                        f"exceeds ceiling {ceiling:g}"
                    ),
                    delta_scalar=delta_scalar,
                    delta_pass_rate=delta_pass_rate,
                    attributable_regressions=attributable,
                )

    # Rule 1a: Pareto dominance over the operator's DECLARED objectives
    # (opt-in; empty ``pareto_objectives`` = OFF and every line below is
    # skipped, leaving the decision byte-identical to a contract without the
    # field). When a profile IS declared it supersedes the scalar-margin test
    # as the promotion criterion, because the scalar is one weighting of these
    # same axes and the operator has said the trade-off matters more than that
    # collapse:
    #
    #   * "dominated"    -> reject outright. The challenger worsened at least
    #                       one declared objective and improved none; no
    #                       weighting of the axes could make that a win.
    #   * "dominates"    -> fall through to Rules 2/3. The margin test is NOT
    #                       applied: a strict improvement on every objective
    #                       the operator named is the win they asked for, and
    #                       re-imposing a scalar band would veto it on a
    #                       weighting they declined to use. Rules 2 and 3 still
    #                       run, so quality/schema guards keep their veto.
    #   * "incomparable" -> the challenger traded objectives. Dominance cannot
    #                       rank a trade, so we fall through to Rule 1's scalar
    #                       margin as the TIEBREAKER — precisely the job the
    #                       weights were written for.
    #   * "unranked"     -> no axis comparable on both sides. Fall through to
    #                       the scalar rules; promoting on no evidence would be
    #                       worse than ignoring the profile for this duel.
    pareto_verdict = "unranked"
    if weights.pareto_objectives:
        pareto_verdict, improved_axes, worsened_axes = pareto_comparison(
            parent_agg, child_agg, weights
        )
        if pareto_verdict == "dominated":
            if worsened_axes:
                evidence = _pareto_axis_evidence(parent_agg, child_agg, weights, worsened_axes)
                detail = f"worsened {evidence}"
            else:
                # Held flat on every objective: no strict improvement anywhere.
                detail = "held flat on every declared objective — no strict improvement"
            return GateOutcome(
                decision=TournamentDecision.REJECTED,
                reason=(
                    f"pareto_dominated: {detail}; a promotion needs the challenger to "
                    f"improve at least one declared objective without worsening any "
                    f"(tolerance {PARETO_OBJECTIVE_TOLERANCE:g})"
                ),
                delta_scalar=delta_scalar,
                delta_pass_rate=delta_pass_rate,
                attributable_regressions=attributable,
            )

    # Rule 1: scalar margin. The scalar is a LOSS — lower is better — so
    # a promotion needs the child's loss to drop by at least
    # ``promote_margin``: ``child_scalar <= parent_scalar - promote_margin``.
    # The rejection reason must (a) state the REAL delta (child minus
    # parent), (b) name ``promote_margin`` as the *promotion threshold*,
    # not as the observed gap, and (c) distinguish a child that improved
    # but not enough ("near-miss") from a child that is outright worse
    # ("regressed").
    # A challenger that DOMINATES every declared objective skips this test —
    # see Rule 1a. ``pareto_verdict`` is "unranked" whenever no profile is
    # declared, so this condition is unchanged for every existing contract.
    if pareto_verdict != "dominates" and child_scalar > parent_scalar - weights.promote_margin:
        # delta_scalar = child - parent. Positive => child's loss rose
        # (worse); zero/negative => child improved or tied but by less
        # than the promotion threshold.
        if delta_scalar > 0.0:
            verdict = (
                f"challenger regressed: loss rose by {delta_scalar:.6f} "
                f"(champion {parent_scalar:.6f} -> challenger {child_scalar:.6f}); "
                f"a promotion needs the loss to drop by at least "
                f"{weights.promote_margin:.6f}"
            )
        else:
            improvement = -delta_scalar
            verdict = (
                f"insufficient improvement: loss fell by only "
                f"{improvement:.6f} (champion {parent_scalar:.6f} -> "
                f"challenger {child_scalar:.6f}); a promotion needs a drop "
                f"of at least {weights.promote_margin:.6f}"
            )
        # ...and when an opt-in parsimony toll moved against the challenger,
        # say how much of that delta was the toll rather than the board
        # (issue #120(b)). Inert — and the reason byte-identical — whenever the
        # diff-complexity term is off, which is the default.
        verdict += _parsimony_decomposition(parent_agg, child_agg, weights, delta_scalar)
        return GateOutcome(
            decision=TournamentDecision.REJECTED,
            reason=verdict,
            delta_scalar=delta_scalar,
            delta_pass_rate=delta_pass_rate,
            attributable_regressions=attributable,
        )

    # Rule 2: pass-rate monotonicity. The scope decides what "regressed"
    # means — per_entry (every parent-passed entry must still pass) or
    # aggregate (the overall pass-rate may not drop). Both branches share
    # the same reason-builder so the gate and the holdout stay symmetric.
    if weights.pass_rate_monotonicity:
        pass_reason = _pass_rate_regression_reason(parent_agg, child_agg, weights)
        if pass_reason:
            return GateOutcome(
                decision=TournamentDecision.REJECTED,
                reason=pass_reason,
                delta_scalar=delta_scalar,
                delta_pass_rate=delta_pass_rate,
                attributable_regressions=attributable,
            )

    # Rule 3: per-namespace monotonicity. Applied last so the scalar
    # margin and the entry-pass-rate guard fire first when they apply;
    # we still cite EVERY regressing namespace in the reason so the
    # journal records the full picture (not just the first one).
    regressed_ns = _regressed_namespaces(parent_agg, child_agg, weights)
    if regressed_ns:
        reason = _namespace_regression_reason(parent_agg, child_agg, regressed_ns)
        return GateOutcome(
            decision=TournamentDecision.REJECTED,
            reason=reason,
            delta_scalar=delta_scalar,
            delta_pass_rate=delta_pass_rate,
            attributable_regressions=attributable,
        )

    # Holdout confirmation (OVERFITTING.md §12 #1). The three train rules
    # cleared; if the caller split out a holdout, the win must also confirm
    # there. An empty / absent holdout skips this step entirely so behavior
    # is byte-identical to the pre-split gate. Deltas stay train-side.
    if holdout_parent_agg is not None and holdout_child_agg is not None:
        holdout_reason = _holdout_confirms(holdout_parent_agg, holdout_child_agg, weights)
        if holdout_reason:
            return GateOutcome(
                decision=TournamentDecision.REJECTED,
                reason=holdout_reason,
                delta_scalar=delta_scalar,
                delta_pass_rate=delta_pass_rate,
                attributable_regressions=attributable,
            )

    return GateOutcome(
        decision=TournamentDecision.PROMOTED,
        reason="",
        delta_scalar=delta_scalar,
        delta_pass_rate=delta_pass_rate,
        attributable_regressions=attributable,
    )


def diff_size_evidence(
    parent_agg: dict[str, Any],
    child_agg: dict[str, Any],
) -> list[str]:
    """Return the opt-in ``diff_size:{side}:{added,removed,patches}`` evidence.

    The parsimony / MDL term (OVERFITTING.md §5 / §12 #4) echoes the candidate
    diff size onto an aggregate only when it is active (see
    :func:`zicato.tournament.scoring.aggregate_generation_score`). This formats
    those sizes as gate-evidence strings, one per side that carries one:

        ``diff_size:champion:added=<a>,removed=<r>,patches=<p>``
        ``diff_size:challenger:added=<a>,removed=<r>,patches=<p>``

    Returns ``[]`` when neither aggregate carries a ``diff_size`` — the
    default-off case — so a caller that always asks for the evidence pays
    nothing and surfaces nothing when the term is disabled. The champion
    aggregate ordinarily has no diff size (the runner threads it only for the
    challenger), so this usually yields a single ``challenger`` line; the
    ``champion`` branch is here for symmetry / completeness.
    """
    lines: list[str] = []
    for side, agg in (("champion", parent_agg), ("challenger", child_agg)):
        ds = agg.get("diff_size")
        if not isinstance(ds, dict):
            continue
        added = int(ds.get("added", 0))
        removed = int(ds.get("removed", 0))
        patches = int(ds.get("patches", 0))
        lines.append(f"diff_size:{side}:added={added},removed={removed},patches={patches}")
    return lines


__all__ = [
    "ATTRIBUTABLE_DRIFT_ABSOLUTE",
    "ATTRIBUTABLE_DRIFT_RATIO",
    "GateOutcome",
    "NAMESPACE_MONOTONICITY_TOLERANCE",
    "PASS_RATE_MONOTONICITY_TOLERANCE",
    "PER_ENTRY_SCORE_MONOTONICITY_TOLERANCE",
    "attributable_entry_regressions",
    "attributable_regression_detail",
    "diff_size_evidence",
    "effective_holdout_margin",
    "evaluate_gate",
    "holdout_confirms",
]
