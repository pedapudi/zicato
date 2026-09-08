"""Confirm promotion with uncertainty in the challenger-minus-champion strength.

The Bradley--Terry fit estimates the probability that one contestant wins a
duel. Confirmation requires a positive lower bound for the fitted strength
difference. Its normal interval includes covariance and allocates the allowed
probability tail across the planned candidate family and confirmation looks.
The approximation requires independent observations and must be checked with
unchanged-system controls and planted improvements.

The scalar gate must first approve the challenger using selection observations.
Those observations never enter confirmation: racing rungs can overlap, and
selecting their winner conditions on their outcomes. Only separately identified
confirmation draws of the fixed crowning pair enter the inferential fit.
Confirmation can hold that
promotion while collecting fresh evidence, or finish inconclusive when its
budget is exhausted. Individual 95% strength intervals remain diagnostics;
the difference interval determines confirmation. An absent probability
threshold disables confirmation; the shared scoring default includes it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from statistics import NormalDist
from typing import Any, Literal

from zicato.core.measurement import EVIDENCE_REPLICATE_BASE as EVIDENCE_REPLICATE_BASE
from zicato.core.measurement import MeasurementDraw
from zicato.core.tournament import (
    DEFAULT_PROMOTE_CONFIDENCE_THRESHOLD as DEFAULT_PROMOTE_CONFIDENCE_THRESHOLD,
)
from zicato.core.tournament import DEFAULT_REPLICATE_BUDGET as DEFAULT_REPLICATE_BUDGET
from zicato.core.tournament import ConfirmationStatus
from zicato.core.tournament import (
    read_promote_confidence_threshold as read_promote_confidence_threshold,
)
from zicato.core.tournament import read_replicate_budget as read_replicate_budget
from zicato.selection.rating import RatingFit, fit_bradley_terry, prob_stronger
from zicato.selection.standings_ext import audit_duels
from zicato.selection.strategy import MatchupResult

#: Confirmation requires at least three independent resolved duels for the
#: fixed crowning pair. This minimum evidence policy applies in addition to
#: the adjusted strength-difference interval. Below it, required confirmation
#: remains incomplete and cannot authorize promotion.
MIN_CREDIBLE_DUELS: int = 3


#: Individual displayed strength intervals use the two-sided 95% normal level.
CI_Z: float = 1.959963984540054
#: Its positive lower bound corresponds to a one-sided probability of 0.975.
#: Confirmation divides this tail across planned candidates and refits.
MIN_PROMOTE_PROBABILITY: float = 0.975

#: The verdict literal this module emits. ``"rejected"`` is included only so a
#: caller can pass through a gate-reject unchanged; this module never *produces*
#: a reject (the guard can only ever hold a promotion, never force one).
EvidenceDecision = Literal["promoted", "deferred", "rejected", "inconclusive"]


@dataclass(frozen=True, slots=True)
class RatingCI:
    """One contestant's Bradley--Terry strength with a confidence interval."""

    generation_id: str
    theta: float
    se: float
    ci_lo: float
    ci_hi: float


@dataclass(frozen=True, slots=True)
class StrengthDifference:
    """Strength difference with its applied normal interval and comparison count."""

    mean: float
    se: float
    ci_lo: float
    ci_hi: float
    confidence_level: float
    comparison_count: int

    @property
    def p_stronger(self) -> float:
        return prob_stronger(self.mean, self.se, 0.0, 0.0)

    def clears(self, threshold: float) -> bool:
        """Require a positive interval and the configured probability bar."""
        return self.ci_lo > 0.0 and self.p_stronger >= threshold


def strength_difference(
    rating: RatingFit,
    child_id: str,
    parent_id: str,
    *,
    threshold: float = MIN_PROMOTE_PROBABILITY,
    comparison_count: int = 1,
) -> StrengthDifference:
    """Allocate the probability tail across planned comparisons and refits.

    The baseline is the positive end of a two-sided 95% interval. Bonferroni
    allocation bounds repeated looks and candidate selection when each normal
    tail approximation is calibrated; it does not make that approximation exact.
    """
    if comparison_count < 1:
        raise ValueError("comparison_count must be positive")
    mean, se = rating.difference(child_id, parent_id)
    tail = min(1.0 - threshold, 1.0 - MIN_PROMOTE_PROBABILITY) / comparison_count
    z = -NormalDist().inv_cdf(tail)
    return StrengthDifference(
        mean, se, mean - z * se, mean + z * se, 1.0 - 2.0 * tail, comparison_count
    )


@dataclass(frozen=True, slots=True)
class EvidenceAttempt:
    """One returned or failed draw, including why it can contribute to the fit."""

    matchup_id: str
    left_id: str
    right_id: str
    eligibility: str
    budget_spent: int
    delta_scalar: float | None = None
    reason: str = ""
    measurement_draw: MeasurementDraw | None = None


@dataclass(frozen=True, slots=True)
class EvidenceVerdict:
    """The pre-gate's verdict for a crowning pair, plus the evidence behind it.

    Fields
    ------
    decision:
        ``"promoted"`` | ``"deferred"`` | ``"inconclusive"`` | ``"rejected"``.
        ``"rejected"`` only ever appears when the caller passed a gate-reject
        through unchanged (the pre-gate is consulted only on a gate-promote).
    reason:
        Human-readable explanation mirroring the gate's reason discipline.
    credible:
        ``True`` once the pair cleared :data:`MIN_CREDIBLE_DUELS`. When
        ``False`` the configured requirement is incomplete and cannot promote.
    champion, challenger:
        The two :class:`RatingCI` rows (``None`` when the fit could not place
        that contestant — e.g. it never appeared in the audit).
    p_stronger:
        ``P(theta_challenger > theta_champion)`` under the fit, or ``None``.
    threshold:
        The probability bar this verdict was judged against.
    ci_overlap:
        Overlap of the individual displayed intervals; diagnostic only.
    difference:
        The covariance-aware contrast, applied interval level, and planned
        comparison count used for confirmation.
    replicates_spent, n_duels:
        Audit size markers for the dashboard rating block.
    """

    decision: EvidenceDecision
    reason: str
    credible: bool
    champion: RatingCI | None
    challenger: RatingCI | None
    p_stronger: float | None
    threshold: float
    ci_overlap: bool
    replicates_spent: int = 0
    n_duels: int = 0
    difference: StrengthDifference | None = None
    confirmation_status: ConfirmationStatus = ConfirmationStatus.INCOMPLETE
    champion_id: str = ""
    challenger_id: str = ""
    attempts: tuple[EvidenceAttempt, ...] = ()


def _rating_ci(rating: Mapping[str, tuple[float, float]], gid: str) -> RatingCI | None:
    """Build a :class:`RatingCI` for ``gid`` from a fitted rating, or ``None``."""
    if gid not in rating:
        return None
    theta, se = rating[gid]
    half = CI_Z * se
    return RatingCI(
        generation_id=gid,
        theta=theta,
        se=se,
        ci_lo=theta - half,
        ci_hi=theta + half,
    )


def _ci_overlap(a: RatingCI, b: RatingCI) -> bool:
    """True when two confidence intervals overlap (closed)."""
    return a.ci_lo <= b.ci_hi and b.ci_lo <= a.ci_hi


def _count_pair_duels(audit: Sequence[MatchupResult], parent_id: str, child_id: str) -> int:
    """Count resolved (non-tie) duels between exactly ``parent_id``/``child_id``."""
    n = 0
    for r in audit:
        ids = {r.left_id, r.right_id}
        if ids != {parent_id, child_id} or not r.execution_complete:
            continue
        if r.outcome.delta_scalar != 0.0:
            n += 1
    return n


def evidence_verdict(
    gate_decision: str,
    gate_reason: str,
    *,
    audit: Sequence[MatchupResult],
    parent_id: str,
    child_id: str,
    threshold: float,
    replicate_budget: int,
    replicates_spent: int = 0,
    planned_candidates: int = 1,
) -> EvidenceVerdict:
    """Fit already-admitted independent confirmation observations.

    The driver establishes draw provenance, excludes selection observations,
    and rejects repeated measurements before calling this mathematical rule.

    Only ever consulted when the gate has already said ``"promoted"`` — a
    non-promote verdict passes straight through (the pre-gate can hold a
    promotion, never force one, so the protected-incumbent invariant strictly
    strengthens).

    Crowns (``"promoted"``) only when BOTH:

    * ``P(theta_child > theta_parent) >= threshold`` (confidence the child is
      stronger), AND
    * the lower bound of the strength difference is positive after allocating
      the probability tail across ``planned_candidates * (replicate_budget + 1)``.
      The planned family is fixed before candidate outcomes are observed.

    Otherwise it ``"deferred"`` while replicate budget remains, or goes terminal
    ``"inconclusive"`` once the budget is spent without confirming the difference. Below
    :data:`MIN_CREDIBLE_DUELS` resolved duels for the pair the fit is not
    trustworthy, so confirmation stays incomplete (``credible=False``).

    The returned :class:`EvidenceVerdict` always carries the full rating block
    (both CIs, ``p_stronger``, ``ci_overlap``) so the journal / dashboard can
    render the evidence regardless of which way it went.
    """
    duels = audit_duels(audit)
    n_pair = _count_pair_duels(audit, parent_id, child_id)
    base = EvidenceVerdict(
        decision=gate_decision,  # type: ignore[arg-type]
        reason=gate_reason,
        credible=False,
        champion=None,
        challenger=None,
        p_stronger=None,
        threshold=threshold,
        ci_overlap=False,
        replicates_spent=replicates_spent,
        n_duels=n_pair,
        champion_id=parent_id,
        challenger_id=child_id,
    )

    # The pre-gate only ever holds a promotion. A reject / defer passes through.
    if gate_decision != "promoted":
        return base

    # An enabled requirement cannot authorize promotion before it is credible.
    if not duels or n_pair < MIN_CREDIBLE_DUELS:
        return replace(
            base,
            decision="deferred" if replicates_spent < replicate_budget else "inconclusive",
            reason=(
                f"confirmation incomplete: {n_pair} resolved pair duels; "
                f"at least {MIN_CREDIBLE_DUELS} required"
            ),
        )

    rating = fit_bradley_terry(duels)
    champ_ci = _rating_ci(rating, parent_id)
    chal_ci = _rating_ci(rating, child_id)
    if champ_ci is None or chal_ci is None:
        return replace(
            base,
            decision="deferred" if replicates_spent < replicate_budget else "inconclusive",
            reason="confirmation incomplete: the fit could not place both contestants",
            champion=champ_ci,
            challenger=chal_ci,
        )

    difference = strength_difference(
        rating,
        child_id,
        parent_id,
        threshold=threshold,
        comparison_count=planned_candidates * (replicate_budget + 1),
    )
    p = difference.p_stronger
    overlap = _ci_overlap(champ_ci, chal_ci)
    cleared = difference.clears(threshold)

    if cleared:
        decision: EvidenceDecision = "promoted"
        reason = gate_reason
    elif replicates_spent < replicate_budget:
        decision = "deferred"
        reason = (
            f"deferred: crowning win not yet decisive — "
            f"P(theta_child > theta_champion)={p:.3f} vs threshold {threshold:.2f}"
            f"; strength difference {difference.confidence_level:.3%} interval "
            f"[{difference.ci_lo:.3f}, {difference.ci_hi:.3f}]; "
            f"repeat the selected candidate against the champion "
            f"({replicates_spent}/{replicate_budget} spent)"
        )
    else:
        decision = "inconclusive"
        reason = (
            f"inconclusive: strength difference remains unconfirmed after exhausting the "
            f"{replicate_budget}-replicate budget — "
            f"P(theta_child > theta_champion)={p:.3f}; recorded to the "
            f"dead-letter queue, champion stands"
        )

    return EvidenceVerdict(
        decision=decision,
        reason=reason,
        credible=True,
        champion=champ_ci,
        challenger=chal_ci,
        p_stronger=p,
        threshold=threshold,
        ci_overlap=overlap,
        replicates_spent=replicates_spent,
        n_duels=n_pair,
        difference=difference,
        confirmation_status=(
            ConfirmationStatus.SATISFIED if cleared else ConfirmationStatus.INCOMPLETE
        ),
        champion_id=parent_id,
        challenger_id=child_id,
    )


def rating_block(verdict: EvidenceVerdict) -> dict[str, Any]:
    """Project an :class:`EvidenceVerdict` to the dashboard ``gate.rating`` shape.

    The single serializer shared by the driver (which stamps it on the journal)
    and the dashboard reader (which echoes the same shape from disk), so the two
    can never drift. ``present`` is always ``True`` here — the absence case
    (``present=False``) is produced by the reader rather than by a verdict that exists.
    """

    def _ci(c: RatingCI | None) -> dict[str, Any] | None:
        if c is None:
            return None
        return {
            "theta": c.theta,
            "se": c.se,
            "ci_lo": c.ci_lo,
            "ci_hi": c.ci_hi,
        }

    return {
        "present": True,
        "evidence_basis": "independent_confirmation",
        "confirmation_status": verdict.confirmation_status,
        "reason": verdict.reason,
        "champion_id": verdict.champion_id,
        "challenger_id": verdict.challenger_id,
        "attempts": [
            {
                "matchup_id": attempt.matchup_id,
                "left_id": attempt.left_id,
                "right_id": attempt.right_id,
                "eligibility": attempt.eligibility,
                "budget_spent": attempt.budget_spent,
                "delta_scalar": attempt.delta_scalar,
                "reason": attempt.reason,
                "measurement_draw": (
                    attempt.measurement_draw.to_json() if attempt.measurement_draw else None
                ),
            }
            for attempt in verdict.attempts
        ],
        "credible": verdict.credible,
        "champion": _ci(verdict.champion),
        "challenger": _ci(verdict.challenger),
        "p_stronger": verdict.p_stronger,
        "threshold": verdict.threshold,
        "decision": verdict.decision,
        "ci_overlap": verdict.ci_overlap,
        "replicates_spent": verdict.replicates_spent,
        "n_duels": verdict.n_duels,
        "difference": (
            {
                "mean": verdict.difference.mean,
                "se": verdict.difference.se,
                "ci_lo": verdict.difference.ci_lo,
                "ci_hi": verdict.difference.ci_hi,
                "confidence_level": verdict.difference.confidence_level,
                "comparison_count": verdict.difference.comparison_count,
            }
            if verdict.difference is not None
            else None
        ),
    }


def disabled_rating_block() -> dict[str, Any]:
    """Record that the contract has no promotion confidence requirement."""
    return {
        "present": False,
        "confirmation_status": ConfirmationStatus.DISABLED,
        "reason": "no promotion confidence threshold in the evaluation contract",
    }


__all__ = [
    "EVIDENCE_REPLICATE_BASE",
    "MIN_CREDIBLE_DUELS",
    "CI_Z",
    "DEFAULT_PROMOTE_CONFIDENCE_THRESHOLD",
    "DEFAULT_REPLICATE_BUDGET",
    "EvidenceDecision",
    "RatingCI",
    "StrengthDifference",
    "strength_difference",
    "EvidenceVerdict",
    "EvidenceAttempt",
    "disabled_rating_block",
    "read_promote_confidence_threshold",
    "read_replicate_budget",
    "evidence_verdict",
    "rating_block",
]
