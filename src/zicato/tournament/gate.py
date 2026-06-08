"""The promote gate: decide whether a child generation supersedes its parent.

Three rules, applied in order:

1. **Scalar margin.** The child's combined scalar must beat the parent's
   by at least :attr:`ScoringWeights.promote_margin`. The scalar is
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

If no rule rejects, the gate promotes — UNLESS a holdout-confirmation
step is supplied (OVERFITTING.md §1/§12 #1, §13). When the caller passes
a held-out slice's parent/child aggregates, a train-measured win must
*also* confirm on the holdout: the challenger's holdout scalar may not
regress past ``promote_margin`` versus the champion's, and the holdout
must not show a pass-rate regression under the SAME
:attr:`ScoringWeights.pass_rate_monotonicity_scope` the train slice uses
(per-entry on both sides, or aggregate on both). A failed confirmation
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

The gate uses ``decision="deferred"`` ONLY when called explicitly by a
caller who has decided neither rule cleanly fired — the function in
this module returns ``"promoted"`` or ``"rejected"``. (Deferral is a
runner-level concept, kept in the :class:`TournamentDecision` literal
type so callers can pre-merge their own deferral logic without a
schema bump.)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from zicato.core import ScoringWeights, TournamentDecision

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
    """

    decision: TournamentDecision
    reason: str
    delta_scalar: float
    delta_pass_rate: float


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
    """
    if weights.pass_rate_monotonicity_scope == "aggregate":
        parent_pass = _mean_score(parent_agg)
        child_pass = _mean_score(child_agg)
        delta = child_pass - parent_pass
        if delta < -PASS_RATE_MONOTONICITY_TOLERANCE:
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
    # :func:`_regressed_entries`).
    regressed = _regressed_entries(parent_agg, child_agg)
    if regressed:
        return f"{prefix}pass-rate regression on entries: " + ", ".join(regressed)
    return ""


def _holdout_confirms(
    holdout_parent_agg: dict[str, Any],
    holdout_child_agg: dict[str, Any],
    weights: ScoringWeights,
) -> str:
    """Return ``""`` when the holdout confirms the win, else a reason.

    Reuses the same machinery the train slice is gated by — the
    ``promote_margin`` regression band on the holdout scalar, and the
    pass-rate monotonicity check on the holdout's per-entry rows — but in
    *confirmation* form: the challenger must merely *not regress* on the
    holdout. Concretely it rejects when

    * the challenger's holdout loss rose past the champion's by more than
      ``promote_margin`` (a real holdout regression, not noise), OR
    * the challenger regressed on pass-rate monotonicity (reusing
      :func:`_pass_rate_regression_reason`, gated by
      ``pass_rate_monotonicity`` so operators who disabled it on the train
      side disable it here too, and honoring the SAME
      ``pass_rate_monotonicity_scope`` so train and holdout apply one
      consistent policy — per-entry on both sides, or aggregate on both).

    The holdout is never asked to clear ``promote_margin`` in the
    *improving* direction — a train-measured win that merely holds flat on
    the holdout is a confirmation, not a failure. This is the asymmetry
    that makes the holdout a guard against board-memorization rather than a
    second, stricter promotion bar.
    """
    parent_scalar = float(holdout_parent_agg["scalar"])
    child_scalar = float(holdout_child_agg["scalar"])
    # A holdout regression: the challenger's holdout loss rose past the
    # champion's by more than the noise band. (delta > +margin ⇒ regressed.)
    if child_scalar - parent_scalar > weights.promote_margin:
        return (
            f"holdout_not_confirmed: holdout loss rose by "
            f"{child_scalar - parent_scalar:.6f} "
            f"(champion {parent_scalar:.6f} -> challenger {child_scalar:.6f}); "
            f"a train-measured win must hold within {weights.promote_margin:.6f} "
            f"on the holdout slice"
        )
    if weights.pass_rate_monotonicity:
        reason = _pass_rate_regression_reason(
            holdout_parent_agg,
            holdout_child_agg,
            weights,
            prefix="holdout_not_confirmed: holdout ",
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

    # Rule 1: scalar margin. The scalar is a LOSS — lower is better — so
    # a promotion needs the child's loss to drop by at least
    # ``promote_margin``: ``child_scalar <= parent_scalar - promote_margin``.
    # The rejection reason must (a) state the REAL delta (child minus
    # parent), (b) name ``promote_margin`` as the *promotion threshold*,
    # not as the observed gap, and (c) distinguish a child that improved
    # but not enough ("near-miss") from a child that is outright worse
    # ("regressed").
    if child_scalar > parent_scalar - weights.promote_margin:
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
        return GateOutcome(
            decision="rejected",
            reason=verdict,
            delta_scalar=delta_scalar,
            delta_pass_rate=delta_pass_rate,
        )

    # Rule 2: pass-rate monotonicity. The scope decides what "regressed"
    # means — per_entry (every parent-passed entry must still pass) or
    # aggregate (the overall pass-rate may not drop). Both branches share
    # the same reason-builder so the gate and the holdout stay symmetric.
    if weights.pass_rate_monotonicity:
        pass_reason = _pass_rate_regression_reason(parent_agg, child_agg, weights)
        if pass_reason:
            return GateOutcome(
                decision="rejected",
                reason=pass_reason,
                delta_scalar=delta_scalar,
                delta_pass_rate=delta_pass_rate,
            )

    # Rule 3: per-namespace monotonicity. Applied last so the scalar
    # margin and the entry-pass-rate guard fire first when they apply;
    # we still cite EVERY regressing namespace in the reason so the
    # journal records the full picture (not just the first one).
    regressed_ns = _regressed_namespaces(parent_agg, child_agg, weights)
    if regressed_ns:
        reason = "monotonicity_regression on namespace=" + ", ".join(regressed_ns)
        return GateOutcome(
            decision="rejected",
            reason=reason,
            delta_scalar=delta_scalar,
            delta_pass_rate=delta_pass_rate,
        )

    # Holdout confirmation (OVERFITTING.md §12 #1). The three train rules
    # cleared; if the caller split out a holdout, the win must also confirm
    # there. An empty / absent holdout skips this step entirely so behavior
    # is byte-identical to the pre-split gate. Deltas stay train-side.
    if holdout_parent_agg is not None and holdout_child_agg is not None:
        holdout_reason = _holdout_confirms(holdout_parent_agg, holdout_child_agg, weights)
        if holdout_reason:
            return GateOutcome(
                decision="rejected",
                reason=holdout_reason,
                delta_scalar=delta_scalar,
                delta_pass_rate=delta_pass_rate,
            )

    return GateOutcome(
        decision="promoted",
        reason="",
        delta_scalar=delta_scalar,
        delta_pass_rate=delta_pass_rate,
    )


__all__ = [
    "GateOutcome",
    "NAMESPACE_MONOTONICITY_TOLERANCE",
    "PASS_RATE_MONOTONICITY_TOLERANCE",
    "PER_ENTRY_SCORE_MONOTONICITY_TOLERANCE",
    "evaluate_gate",
    "holdout_confirms",
]
