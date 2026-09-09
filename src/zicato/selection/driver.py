"""Drive a selection strategy through matchups and evidence confirmation.

The driver receives candidate production and matchup execution as injected
callables. It owns only strategy progression and the optional evidence gate,
which keeps it testable without a workspace or live harness.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from zicato.core.measurement import (
    MeasurementDraw,
    MeasurementPurpose,
    validate_measurement_count,
)
from zicato.core.tournament import ConfirmationStatus
from zicato.selection.evidence_gate import (
    EvidenceAttempt,
    EvidenceVerdict,
    evidence_verdict,
    rating_block,
)
from zicato.selection.strategy import (
    Contestant,
    Matchup,
    MatchupResult,
    SelectionDecision,
    SelectionStrategy,
)
from zicato.util.async_tasks import gather_owned

#: ``request_field(n)`` resolves the champion contestant and applies ``n``
#: challenger experiments into fresh snapshots, returning
#: ``(champion, challengers)``.
RequestField = Callable[[int], Awaitable[tuple[Contestant, Sequence[Contestant]]]]

#: ``run_matchup(m)`` runs one duel to a :class:`MatchupResult` (ending in
#: the unchanged ``evaluate_gate``).
RunMatchup = Callable[[Matchup], Awaitable[MatchupResult]]

#: ``replicate_duel(left_id, right_id)`` runs ONE extra duel between an
#: already-seeded pair, to a :class:`MatchupResult`. Used only by the opt-in
#: Bradley--Terry pre-gate's defer→replicate loop: when a crowning promote is
#: not yet decisive, the driver spends a replicate on the crowning pair
#: through this callable, refits, and rechecks. ``None`` (the default) disables
#: the loop entirely — the pre-gate then defers/inconclusive on its current
#: evidence without scheduling any new duel.
#:
#: Each request must identify an independent confirmation draw on both sides.
#: The factory advances the local draw under ``evidence_confirmation`` and
#: assigns a unique matchup id. The driver rejects repeated matchup ids and
#: measurement identities as evidence. Replaying a sample must not narrow
#: confidence intervals without an additional independent observation.

ReplicateDuel = Callable[[str, str], Awaitable[MatchupResult]]

#: Run one matchup starting at ``first_measurement``. The confirmation
#: factory chooses its purpose, local draw, and matchup id, and disables
#: aggregate caching so confirmation preserves the tournament score.

RunReservedMatchup = Callable[..., Awaitable[MatchupResult]]

log = logging.getLogger("zicato.selection.driver")


def make_evidence_replicate_duel(run_reserved_matchup: RunReservedMatchup) -> ReplicateDuel:
    """Build a callable that requests independent confirmation draws.

    Each call advances the draw within the confirmation purpose and disables
    aggregate score caching. Confirmation cannot overwrite the tournament
    measurements that selected the candidate."""
    replicates_run = 0

    async def _replicate_duel(left_id: str, right_id: str) -> MatchupResult:
        nonlocal replicates_run
        replicate_slot = MeasurementDraw(MeasurementPurpose.CONFIRMATION, replicates_run)

        replicates_run += 1
        return await run_reserved_matchup(
            Matchup(
                matchup_id=f"confirmation:r{replicate_slot.draw}:{left_id}:{right_id}",
                left=Contestant(generation_id=left_id, role="champion"),
                right=Contestant(generation_id=right_id, role="challenger"),
            ),
            first_measurement=replicate_slot,
            cache_scores=False,
        )

    return _replicate_duel


#: ``on_inconclusive(resolution)`` is called once, at the moment the pre-gate
#: reaches the terminal ``inconclusive`` state, with the full
#: :class:`EvidenceResolution` (verdict + CI history). The orchestrator wires
#: this to the dead-letter writer (:mod:`zicato.selection.dead_letter`) so the
#: unresolved duel is recorded; ``None`` (the default) drops it on the floor,
#: which is fine for the driver's own unit tests. Best-effort by contract — a
#: write failure must not abort the resolution.
OnInconclusive = Callable[["EvidenceResolution"], None]

#: ``on_progress(strategy)`` is called once the strategy is seeded and
#: again each time a batch of pending matchups has been scheduled — i.e.
#: whenever the strategy's live (in-flight) view may have changed. The
#: orchestrator uses it to publish the live ``active_tournament`` envelope
#: with the in-flight bracket/ladder (``strategy.live_rounds()`` /
#: ``live_standings()``) DURING the run rather than just at settle. Best-effort
#: by contract — a publish failure must never abort the resolution — so
#: the driver swallows nothing itself; the callback owns its own safety.
ProgressHook = Callable[[SelectionStrategy], None]


@dataclass(frozen=True, slots=True)
class EvidencePreGate:
    """Opt-in Bradley--Terry promotion pre-gate config for the driver.

    Passed to :func:`evaluate_tournament` only when the operator set
    ``params["promote_confidence_threshold"]`` (resolved by the orchestrator via
    :func:`zicato.selection.evidence_gate.read_promote_confidence_threshold`).
    When ``None``, no evidence pre-gate, replication, or dead-letter write is
    requested.

    Fields
    ------
    threshold:
        The probability bar ``P(theta_child > theta_champion)`` must reach.
    replicate_budget:
        How many extra crowning-pair replicates the defer→replicate loop may spend
        before going terminal (``inconclusive``).
    """

    threshold: float
    replicate_budget: int

    def __post_init__(self) -> None:
        validate_measurement_count(self.replicate_budget, allow_empty=True)


@dataclass(frozen=True, slots=True)
class EvidenceResolution:
    """The pre-gate's terminal output (returned alongside the decision).

    ``verdict`` is the final :class:`EvidenceVerdict`; ``ci_history`` is the
    per-refit ``p_stronger`` / ``ci_overlap`` trace the defer→replicate loop
    produced (one entry per check, oldest first), so the journal / dashboard can
    show the duel converging — or terminally failing to.
    """

    verdict: EvidenceVerdict
    ci_history: tuple[dict[str, Any], ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class TournamentEvaluation:
    """A resolved tournament decision and its optional gate evidence."""

    decision: SelectionDecision
    evidence: EvidenceResolution | None = None


async def resolve_tournament(
    strategy: SelectionStrategy,
    *,
    request_field: RequestField,
    run_matchup: RunMatchup,
    on_progress: ProgressHook | None = None,
    pre_gate: EvidencePreGate | None = None,
    replicate_duel: ReplicateDuel | None = None,
    on_inconclusive: OnInconclusive | None = None,
) -> SelectionDecision:
    """Drive ``strategy`` from a fresh field to a crowned decision.

    1. ``request_field(strategy.field_size())`` resolves the champion and
       the applied challenger field.
    2. ``strategy.seed(...)`` initialises bracket state.
    3. Loop: ``strategy.next_matchups()`` → run the batch concurrently →
       ``strategy.record_result(...)`` for each, until
       ``strategy.resolved()`` or the strategy schedules nothing.
    4. Return ``strategy.champion()``.

    ``on_progress`` is invoked right after the batch is
    scheduled (the strategy's ``_pending`` is populated, so
    ``live_rounds()`` carries the in-flight matchups) so the caller can
    publish the live structure while the round runs. It is a no-op when
    omitted.

    ``pre_gate`` runs the optional Bradley--Terry "crown on
    evidence" pre-gate AFTER the strategy resolves a ``"promoted"`` decision:
    the crowning win is held unless the fitted rating clears the confidence
    threshold and its adjusted difference interval excludes zero. While it defers
    and ``replicate_duel`` is supplied with budget remaining, the driver spends a replicate on the
    crowning pair, refits, and rechecks (the defer→replicate loop). With
    ``pre_gate`` set to ``None`` returns the strategy's decision verbatim.

    Each batch runs under the caller's concurrency (the same semaphore the
    runner already uses, applied inside ``run_matchup``); the driver only
    fans them out with :func:`asyncio.gather`.
    """
    return (
        await evaluate_tournament(
            strategy,
            request_field=request_field,
            run_matchup=run_matchup,
            on_progress=on_progress,
            pre_gate=pre_gate,
            replicate_duel=replicate_duel,
            on_inconclusive=on_inconclusive,
        )
    ).decision


async def evaluate_tournament(
    strategy: SelectionStrategy,
    *,
    request_field: RequestField,
    run_matchup: RunMatchup,
    on_progress: ProgressHook | None = None,
    pre_gate: EvidencePreGate | None = None,
    replicate_duel: ReplicateDuel | None = None,
    on_inconclusive: OnInconclusive | None = None,
) -> TournamentEvaluation:
    """Drive a strategy and retain evidence used to confirm its crown."""

    planned_candidates = strategy.field_size()
    champion, challengers = await request_field(planned_candidates)
    strategy.seed(champion, list(challengers))
    while not strategy.resolved():
        batch = strategy.next_matchups()
        if not batch:
            break
        # The pending batch is now reflected in the strategy's live view;
        # publish it before the (potentially long) matchup runs so the
        # dashboard's bracket/ladder/funnel exists live with winner=null.
        if on_progress is not None:
            on_progress(strategy)
        results = await gather_owned(*(run_matchup(m) for m in batch))
        for result in results:
            strategy.record_result(result)

    decision = strategy.champion()
    if pre_gate is None:
        return TournamentEvaluation(decision)
    confirmed, evidence = await confirm_promotion_with_evidence(
        decision,
        champion=champion,
        pre_gate=pre_gate,
        replicate_duel=replicate_duel,
        on_inconclusive=on_inconclusive,
        planned_candidates=planned_candidates,
    )
    return TournamentEvaluation(confirmed, evidence)


async def confirm_promotion_with_evidence(
    decision: SelectionDecision,
    *,
    champion: Contestant,
    pre_gate: EvidencePreGate,
    replicate_duel: ReplicateDuel | None,
    on_inconclusive: OnInconclusive | None = None,
    planned_candidates: int = 1,
) -> tuple[SelectionDecision, EvidenceResolution | None]:
    """Confirm a proposed promotion, retaining every attempt and its eligibility.

    Required but unresolved confirmation terminates inconclusive and preserves
    the champion. The planned candidate family and replicate budget remain fixed
    throughout confirmation. Strategy results select the pair and remain in the
    audit but never supply inferential observations. Each attempted draw consumes
    budget, including a duplicate, tie, incomplete measurement, or runner failure. Replayed and
    unusable observations never contribute to fitted evidence. Generation and
    draw identity conservatively reserve every entry in that draw: even disjoint
    board subsets cannot present the same draw as another independent sample.
    Historical purpose/draw records without a selected seed cannot establish
    independent measurement identity.
    """
    promoted_id = decision.promoted_generation_id
    if decision.decision != "promoted" or promoted_id is None:
        return decision, None

    parent_id = champion.generation_id
    observed_matchups: list[MatchupResult] = []
    audit: list[MatchupResult] = []
    attempts: list[EvidenceAttempt] = []
    ci_history: list[dict[str, Any]] = []
    seen_matchup_ids: set[str] = set()
    seen_draws: set[tuple[str, MeasurementDraw]] = set()

    def retain(result: MatchupResult, *, budget_spent: int) -> None:
        observed_matchups.append(result)
        measurement = result.measurement_draw
        units = (
            {(gid, measurement) for gid in (result.left_id, result.right_id)}
            if measurement
            else set()
        )
        if not budget_spent:
            eligibility = "selection_only"
        elif result.matchup_id in seen_matchup_ids:
            eligibility = "duplicate"
        elif budget_spent and {result.left_id, result.right_id} != {parent_id, promoted_id}:
            eligibility = "unexpected_pair"
        elif not result.execution_complete:
            eligibility = "incomplete"
        elif not math.isfinite(result.outcome.delta_scalar):
            eligibility = "nonfinite"
        elif result.left_id == result.right_id:
            eligibility = "self_comparison"
        elif measurement is None:
            eligibility = "missing_provenance"
        elif measurement.purpose != MeasurementPurpose.CONFIRMATION:
            eligibility = "wrong_purpose"
        elif units & seen_draws:
            eligibility = "repeated_measurement"
        elif result.outcome.delta_scalar == 0.0:
            eligibility = "tie"
        else:
            eligibility = "eligible"
        attempts.append(
            EvidenceAttempt(
                matchup_id=result.matchup_id,
                left_id=result.left_id,
                right_id=result.right_id,
                eligibility=eligibility,
                budget_spent=budget_spent,
                delta_scalar=(
                    result.outcome.delta_scalar
                    if math.isfinite(result.outcome.delta_scalar)
                    else None
                ),
                reason=result.outcome.reason,
                measurement_draw=measurement,
            )
        )
        seen_matchup_ids.add(result.matchup_id)
        # Reserve identities even for ties or unusable draws. Admission order
        # follows the fixed draw schedule and never selects a favorable reuse.
        seen_draws.update(units)
        if eligibility in {"eligible", "tie"}:
            audit.append(result)

    for observed in decision.matchups:
        retain(observed, budget_spent=0)
    replicates_spent = 0
    while True:
        verdict = replace(
            evidence_verdict(
                "promoted",
                decision.reason,
                audit=audit,
                parent_id=parent_id,
                child_id=promoted_id,
                threshold=pre_gate.threshold,
                replicate_budget=pre_gate.replicate_budget,
                replicates_spent=replicates_spent,
                planned_candidates=planned_candidates,
            ),
            attempts=tuple(attempts),
        )
        ci_history.append(
            {
                "p_stronger": verdict.p_stronger,
                "ci_overlap": verdict.ci_overlap,
                "replicates_spent": replicates_spent,
                "confirmation_status": verdict.confirmation_status,
                "n_duels": verdict.n_duels,
            }
        )
        if verdict.confirmation_status == ConfirmationStatus.SATISFIED:
            return _finalize(
                decision, verdict, observed_matchups, ci_history, promoted_id, on_inconclusive
            )

        if replicate_duel is None or replicates_spent >= pre_gate.replicate_budget:
            cause = "runner unavailable" if replicate_duel is None else "replicate budget exhausted"
            terminal = replace(
                verdict,
                decision="inconclusive",
                reason=f"confirmation incomplete: {cause}; {verdict.n_duels} resolved pair duels",
            )
            return _finalize(
                decision, terminal, observed_matchups, ci_history, promoted_id, on_inconclusive
            )

        # The crowning pair is known before a fit exists. Ties and incomplete
        # observations cannot prevent a fresh attempt to measure that pair.
        replicates_spent += 1
        try:
            extra = await replicate_duel(parent_id, promoted_id)
        except Exception as exc:  # cancellation remains a BaseException
            log.exception("confirmation runner failed")
            attempts.append(
                EvidenceAttempt(
                    matchup_id="",
                    left_id=parent_id,
                    right_id=promoted_id,
                    eligibility="error",
                    budget_spent=1,
                    reason=type(exc).__name__,
                )
            )
            terminal = replace(
                verdict,
                decision="inconclusive",
                reason=f"confirmation incomplete: runner failed ({type(exc).__name__})",
                replicates_spent=replicates_spent,
                attempts=tuple(attempts),
            )
            ci_history.append({**ci_history[-1], "replicates_spent": replicates_spent})
            return _finalize(
                decision, terminal, observed_matchups, ci_history, promoted_id, on_inconclusive
            )
        retain(extra, budget_spent=1)


def _finalize(
    decision: SelectionDecision,
    verdict: EvidenceVerdict,
    audit: list[MatchupResult],
    ci_history: list[dict[str, Any]],
    promoted_id: str,
    on_inconclusive: OnInconclusive | None,
) -> tuple[SelectionDecision, EvidenceResolution | None]:
    """Fold confirmed or inconclusive evidence into the terminal selection.

    Satisfied confirmation retains the crown. Inconclusive confirmation records
    a deferred decision, preserves every accepted observation, and invokes the
    dead-letter callback with all attempts and confidence checks.
    """
    from zicato.core import TournamentDecision  # noqa: PLC0415

    resolution = EvidenceResolution(verdict=verdict, ci_history=tuple(ci_history))

    if verdict.confirmation_status == ConfirmationStatus.SATISFIED:
        return (
            replace(
                decision,
                promoted_generation_id=promoted_id,
                decision=TournamentDecision.PROMOTED,
                reason=verdict.reason,
                matchups=tuple(audit),
            ),
            resolution,
        )

    # Inconclusive terminal: lineage head unchanged, recorded to dead-letter.
    if on_inconclusive is not None:
        on_inconclusive(resolution)
    return (
        replace(
            decision,
            promoted_generation_id=None,
            decision=TournamentDecision.DEFERRED,
            reason=verdict.reason,
            matchups=tuple(audit),
        ),
        resolution,
    )


__all__ = [
    "evaluate_tournament",
    "resolve_tournament",
    "confirm_promotion_with_evidence",
    "RequestField",
    "RunMatchup",
    "ReplicateDuel",
    "RunReservedMatchup",
    "make_evidence_replicate_duel",
    "OnInconclusive",
    "ProgressHook",
    "EvidencePreGate",
    "EvidenceResolution",
    "TournamentEvaluation",
    "rating_block",
]
