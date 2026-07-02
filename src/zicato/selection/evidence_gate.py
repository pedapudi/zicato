"""Crown on evidence, not a point estimate — the Bradley--Terry pre-gate.

The opt-in uncertainty pre-gate of ``docs/design/FUNCTIONALITY-RECOMMENDATIONS.md``
§5 / ``docs/design/SELECTION-THEORY.md`` §7.1, raised from the single-shot
``uncertainty_gate`` guard (:mod:`zicato.selection.standings_ext`) to a full
**defer → replicate → refit** schedule with a genuine terminal third state.

Where the legacy guard answers a yes/no "is the crowning win within rating
noise?", this module answers the operator's real question — *"is there enough
evidence to crown, and if not, what is the cheapest duel to replicate to find
out?"* — by reading the fitted Bradley--Terry strengths AND their confidence
intervals:

* :func:`evidence_verdict` fits BT over the strategy's already-measured duel
  audit and returns one of three verdicts for the crowning pair:

  - ``"promoted"`` — ``P(theta_child > theta_champion) >= threshold`` AND the
    two rating CIs are *separated* (no overlap). Crown on evidence.
  - ``"deferred"`` — the probability bar is unmet OR the CIs still overlap, and
    there is replicate budget left to spend. Hold and replicate.
  - ``"inconclusive"`` — the budget is exhausted and the CIs still overlap. A
    terminal state recorded in the dead-letter queue
    (:mod:`zicato.selection.dead_letter`); nothing is silently dropped.

  A fit is only trusted once the pair has at least :data:`MIN_CREDIBLE_DUELS`
  resolved duels — the Fisher-information SE blows up below that, so a guard
  built on it would defer (or crown) on noise. Below the minimum the verdict is
  the gate's own (no evidence to override it).

* :func:`closest_ci_duel` is the schedule: of all candidate duels, the one whose
  two contestants have the *smallest* CI gap (the most-overlapping, least-
  resolved pairing) is the cheapest replicate to sharpen the fit. The driver
  spends each defer's replicate there, refits, and rechecks.

Everything here is **pure** and **opt-in**: with
``params["promote_confidence_threshold"]`` unset,
:func:`read_promote_confidence_threshold` returns ``None`` and no pre-gate
runs. The gate is deliberately NOT on by default — it is a **soundness**
device, not a power device. Measured on the two-contestant crowning pair
(the Tier-2 power harness): the gate blocks 100% of A/A false promotes, but
its CIs separate only after an UNBROKEN win streak of ~37 duels (mixed
records never separate), so a small default budget would freeze every true
promotion at ``inconclusive`` and a converging budget costs ~32×2×board
fresh runs per crowning. Decision **power** is bought with per-duel
replication (the ``replicates`` knob, default 2) and a margin calibrated
above the measured A/A noise floor (:mod:`zicato.tournament.calibration`);
the scaffolded contracts (``zicato init`` / the builder) enable the gate
EXPLICITLY with an honest budget, so operators see the cost they are
opting into. Both selection shapes reach it when enabled — the
multi-challenger driver and the gauntlet crowning duel.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from zicato.selection.rating import fit_bradley_terry, prob_stronger
from zicato.selection.standings_ext import audit_duels
from zicato.selection.strategy import MatchupResult

#: The minimum number of resolved duels for the SAME pair before its
#: Bradley--Terry fit is credible enough to gate on. The Fisher-information
#: standard error is dominated by the prior (and thus enormous) at ``n < 3``,
#: so a CI / probability computed there would defer (or crown) on noise rather
#: than measurement. Below this the verdict falls back to the gate's own and
#: the rating block is reported with ``present`` but ``credible=False``.
MIN_CREDIBLE_DUELS: int = 3

#: Replicate-index base for the pre-gate's evidence duels. Evidence replicate
#: ``j`` runs the crowning pair at replicate index ``EVIDENCE_REPLICATE_BASE
#: + j`` — a RESERVED per-unit cache slot — so each replicate draws BOTH
#: sides (champion AND challenger) fresh instead of replaying the canonical
#: replicate-0 sample the tournament already scored: identical data repeated
#: through the fit would shrink the Bradley--Terry SE by repetition alone
#: (fast mode), and a force-fresh re-run at slot 0 would clobber the child's
#: canonical ``loss.json`` that reindex/crash-resume key on (full mode).
#: Reserved far above every sibling base so the slots can never collide:
#: real duel replicates count up from 0, A/A calibration draws at 1000
#: (:data:`zicato.tournament.calibration.CALIBRATION_REPLICATE_BASE`), the
#: contract pre-flight at 2000
#: (:data:`zicato.epoch.preflight.PREFLIGHT_REPLICATE_BASE`), and the
#: pre-tournament candidate screen at 3000
#: (:data:`zicato.epoch.screen.SCREEN_REPLICATE_BASE`; its
#: confirm-before-veto re-run at 3001).
EVIDENCE_REPLICATE_BASE: int = 4000

#: The half-width multiplier turning a Bradley--Terry standard error into a
#: confidence interval ``theta ± Z * se``. ``1.96`` is the 95% normal quantile
#: — the same level the ``prob_stronger`` probability is naturally read at, so
#: "P >= 0.95 AND CIs clear" is one coherent confidence statement rather than
#: two unrelated bars.
CI_Z: float = 1.959963984540054

#: The default replicate budget for the defer→replicate loop when
#: ``promote_confidence_replicates`` is unset. A small budget: the unit cache
#: makes each extra replicate cheap, but a near-tie that will not separate
#: should reach ``inconclusive`` quickly rather than burn the round's
#: wall-clock budget.
DEFAULT_REPLICATE_BUDGET: int = 3

#: The RECOMMENDED probability bar — the value the scaffolded contracts
#: (``zicato init`` / the builder's blank draft) write explicitly when they
#: enable the gate. NOT applied when the param is absent (the gate is opt-in;
#: see the module docstring for the measured soundness-vs-power tradeoff).
#: ``0.8`` is deliberately below the 0.95 the CI level speaks at: the
#: CI-separation requirement is the sharp half of the test, and the
#: probability bar mostly guards against a fit whose point estimates favour
#: the challenger while the evidence is thin.
DEFAULT_PROMOTE_CONFIDENCE_THRESHOLD: float = 0.8

#: The verdict literal this module emits. ``"rejected"`` is included only so a
#: caller can pass through a gate-reject unchanged; this module never *produces*
#: a reject (the guard can only ever hold a promotion, never force one).
EvidenceDecision = Literal["promoted", "deferred", "rejected", "inconclusive"]


def read_promote_confidence_threshold(params: Mapping[str, Any]) -> float | None:
    """The opt-in ``promote_confidence_threshold``, or ``None`` when absent.

    Reads ``params["promote_confidence_threshold"]`` — the probability bar a
    promotion must clear under the Bradley--Terry pre-gate: crown only if
    ``P(theta_child > theta_champion)`` reaches it AND the rating CIs clear.
    Absent / explicit ``null`` / ``0`` / non-numeric / outside ``(0, 1)`` ⇒
    ``None`` (no pre-gate). The gate is deliberately OPT-IN — see the module
    docstring for the measured soundness-vs-power tradeoff; the scaffolded
    contracts enable it explicitly with an honest replicate budget. Like
    every guard here, a bad value safely degrades to "no pre-gate".

    Lives in the opaque ``TournamentStructure.params`` map — NOT on
    :class:`~zicato.core.ScoringWeights` — precisely because an absent param
    adds nothing to the contract canonical form, so the contract hash (and the
    whole parity surface) is byte-identical when the operator does not opt in.
    """
    raw = params.get("promote_confidence_threshold", None)
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value <= 0.0 or value >= 1.0:
        return None
    return value


def read_replicate_budget(params: Mapping[str, Any]) -> int:
    """The defer→replicate budget for the pre-gate loop.

    Reads ``params["promote_confidence_replicates"]`` — how many extra
    closest-CI replicates the driver may spend chasing separation before the
    verdict goes terminal (``inconclusive``). Absent / non-integer / negative ⇒
    :data:`DEFAULT_REPLICATE_BUDGET`. Zero is honoured (defer once, then go
    inconclusive immediately) so an operator can disable replication while still
    using the deferred verdict.
    """
    raw = params.get("promote_confidence_replicates", None)
    if raw is None:
        return DEFAULT_REPLICATE_BUDGET
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_REPLICATE_BUDGET
    if value < 0:
        return DEFAULT_REPLICATE_BUDGET
    return value


@dataclass(frozen=True, slots=True)
class RatingCI:
    """One contestant's Bradley--Terry strength with a confidence interval."""

    generation_id: str
    theta: float
    se: float
    ci_lo: float
    ci_hi: float


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
        ``False`` the verdict is the caller's gate verdict unchanged — there is
        no trustworthy fit to override it.
    champion, challenger:
        The two :class:`RatingCI` rows (``None`` when the fit could not place
        that contestant — e.g. it never appeared in the audit).
    p_stronger:
        ``P(theta_challenger > theta_champion)`` under the fit, or ``None``.
    threshold:
        The probability bar this verdict was judged against.
    ci_overlap:
        ``True`` when the two CIs overlap (the duel is not yet separated).
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
        if ids != {parent_id, child_id}:
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
) -> EvidenceVerdict:
    """The Bradley--Terry pre-gate verdict for a crowning duel.

    Only ever consulted when the gate has already said ``"promoted"`` — a
    non-promote verdict passes straight through (the pre-gate can hold a
    promotion, never force one, so the protected-incumbent invariant strictly
    strengthens).

    Crowns (``"promoted"``) only when BOTH:

    * ``P(theta_child > theta_parent) >= threshold`` (confidence the child is
      stronger), AND
    * the two rating CIs are *separated* (the strength estimates do not
      overlap — the duel is resolved, not a noisy near-tie).

    Otherwise it ``"deferred"`` while replicate budget remains, or goes terminal
    ``"inconclusive"`` once the budget is spent and the CIs still overlap. Below
    :data:`MIN_CREDIBLE_DUELS` resolved duels for the pair the fit is not
    trustworthy, so the verdict is the gate's own (``credible=False``).

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
    )

    # The pre-gate only ever holds a promotion. A reject / defer passes through.
    if gate_decision != "promoted":
        return base

    # Not enough evidence for a credible fit ⇒ no override; the gate stands.
    if not duels or n_pair < MIN_CREDIBLE_DUELS:
        return base

    rating = fit_bradley_terry(duels)
    champ_ci = _rating_ci(rating, parent_id)
    chal_ci = _rating_ci(rating, child_id)
    if champ_ci is None or chal_ci is None:
        # The fit could not place one side — never invent a hold from absence.
        return EvidenceVerdict(
            decision="promoted",
            reason=gate_reason,
            credible=False,
            champion=champ_ci,
            challenger=chal_ci,
            p_stronger=None,
            threshold=threshold,
            ci_overlap=False,
            replicates_spent=replicates_spent,
            n_duels=n_pair,
        )

    p = prob_stronger(chal_ci.theta, chal_ci.se, champ_ci.theta, champ_ci.se)
    overlap = _ci_overlap(champ_ci, chal_ci)
    cleared = p >= threshold and not overlap

    if cleared:
        decision: EvidenceDecision = "promoted"
        reason = gate_reason
    elif replicates_spent < replicate_budget:
        decision = "deferred"
        reason = (
            f"deferred: crowning win not yet decisive — "
            f"P(theta_child > theta_champion)={p:.3f} vs threshold {threshold:.2f}"
            f"{', CIs overlap' if overlap else ''}; "
            f"replicate the closest duel "
            f"({replicates_spent}/{replicate_budget} spent)"
        )
    else:
        decision = "inconclusive"
        reason = (
            f"inconclusive: rating CIs still overlap after exhausting the "
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
    )


@dataclass(frozen=True, slots=True)
class CandidateDuel:
    """A pairing the driver may replicate, with its current CI gap.

    ``ci_gap`` is the signed separation between the two contestants' CIs:
    negative / zero ⇒ overlapping (the more negative, the deeper the overlap);
    positive ⇒ already separated. The closest-to-resolve duel — the cheapest
    replicate to sharpen — is the one with the *smallest* gap.
    """

    left_id: str
    right_id: str
    ci_gap: float


def closest_ci_duel(
    audit: Sequence[MatchupResult],
    *,
    restrict_to: tuple[str, str] | None = None,
) -> CandidateDuel | None:
    """The duel whose contestants' CIs are closest — the cheapest replicate.

    Fits Bradley--Terry over the audit, then scores every distinct pairing that
    actually appears by its CI gap (``argmin`` over the gap). The most-
    overlapping, least-resolved pairing is the one a replicate sharpens most, so
    the driver spends each defer's replicate there. ``restrict_to`` pins the
    schedule to a single pairing (the crowning pair) when the operator only
    wants to resolve the champion-vs-challenger duel; ``None`` considers the
    whole field. Returns ``None`` when no fittable pairing exists.
    """
    duels = audit_duels(audit)
    if not duels:
        return None
    rating = fit_bradley_terry(duels)

    pairs: dict[frozenset[str], tuple[str, str]] = {}
    for r in audit:
        if r.left_id == r.right_id:
            continue
        if r.outcome.delta_scalar == 0.0:
            continue
        key = frozenset({r.left_id, r.right_id})
        if restrict_to is not None and key != frozenset(restrict_to):
            continue
        pairs.setdefault(key, (r.left_id, r.right_id))

    best: CandidateDuel | None = None
    for left_id, right_id in pairs.values():
        a = _rating_ci(rating, left_id)
        b = _rating_ci(rating, right_id)
        if a is None or b is None:
            continue
        # Gap between the two intervals on the theta axis: the separation
        # between the lower edge of the higher CI and the upper edge of the
        # lower CI. Negative ⇒ they overlap; smaller ⇒ closer to a tie.
        if a.theta >= b.theta:
            gap = a.ci_lo - b.ci_hi
        else:
            gap = b.ci_lo - a.ci_hi
        cand = CandidateDuel(left_id=left_id, right_id=right_id, ci_gap=gap)
        if (
            best is None
            or cand.ci_gap < best.ci_gap
            or (
                cand.ci_gap == best.ci_gap
                and (cand.left_id, cand.right_id) < (best.left_id, best.right_id)
            )
        ):
            best = cand
    return best


def rating_block(verdict: EvidenceVerdict) -> dict[str, Any]:
    """Project an :class:`EvidenceVerdict` to the dashboard ``gate.rating`` shape.

    The single serializer shared by the driver (which stamps it on the journal)
    and the dashboard reader (which echoes the same shape from disk), so the two
    can never drift. ``present`` is always ``True`` here — the absence case
    (``present=False``) is produced by the reader, not by a verdict that exists.
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
        "credible": verdict.credible,
        "champion": _ci(verdict.champion),
        "challenger": _ci(verdict.challenger),
        "p_stronger": verdict.p_stronger,
        "threshold": verdict.threshold,
        "decision": verdict.decision,
        "ci_overlap": verdict.ci_overlap,
        "replicates_spent": verdict.replicates_spent,
        "n_duels": verdict.n_duels,
    }


@dataclass(frozen=True, slots=True)
class ReplicationOutcome:
    """The terminal result of the driver's defer→replicate loop.

    ``verdict`` is the final :class:`EvidenceVerdict`; ``ci_history`` is the
    per-step ``p_stronger`` / ``ci_overlap`` trace the loop produced (one entry
    per refit), so the dashboard can show the duel converging (or failing to).
    """

    verdict: EvidenceVerdict
    ci_history: tuple[dict[str, Any], ...] = field(default_factory=tuple)


__all__ = [
    "EVIDENCE_REPLICATE_BASE",
    "MIN_CREDIBLE_DUELS",
    "CI_Z",
    "DEFAULT_PROMOTE_CONFIDENCE_THRESHOLD",
    "DEFAULT_REPLICATE_BUDGET",
    "EvidenceDecision",
    "RatingCI",
    "EvidenceVerdict",
    "CandidateDuel",
    "ReplicationOutcome",
    "read_promote_confidence_threshold",
    "read_replicate_budget",
    "evidence_verdict",
    "closest_ci_duel",
    "rating_block",
]
