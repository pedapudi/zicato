"""The orchestrator-side driver that walks a strategy to a decision.

:func:`resolve_tournament` is the structure-swappable replacement for
steps 2-5 of the historical ``evolve_once``: request the field, seed the
strategy, then run scheduled matchups until the strategy resolves, and
return the crowned :class:`SelectionDecision`.

The driver is intentionally thin and IO-shaped only through its two
injected callables, so it is fully unit-testable with synthetic
``request_field`` / ``run_matchup`` stubs (no real tournament runs).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from zicato.selection.evidence_gate import (
    EvidenceVerdict,
    closest_ci_duel,
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
#: not yet decisive, the driver spends a replicate on the closest-CI duel
#: through this callable, refits, and rechecks. ``None`` (the default) disables
#: the loop entirely — the pre-gate then defers/inconclusive on its current
#: evidence without scheduling any new duel.
#:
#: CONTRACT: every call must return an INDEPENDENT fresh draw of the pair,
#: under a matchup id that is unique within the audit. The orchestrator's
#: implementations satisfy both by running each evidence replicate ``j`` at
#: the reserved replicate index
#: :data:`~zicato.selection.evidence_gate.EVIDENCE_REPLICATE_BASE` ``+ j``
#: (both sides drawn fresh — never a cache replay of the canonical
#: replicate-0 slots) and encoding that index in the matchup id
#: (``bt-replicate:r{index}:{left}:{right}``). The driver refuses to append a
#: result whose matchup id already appears in the audit: identical data
#: re-presented to the fit would shrink the Bradley--Terry SE by repetition
#: alone, letting duplicate duels "separate" CIs without new evidence.
ReplicateDuel = Callable[[str, str], Awaitable[MatchupResult]]

log = logging.getLogger("zicato.selection.driver")

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
#: ``live_standings()``) DURING the run, not just at settle. Best-effort
#: by contract — a publish failure must never abort the resolution — so
#: the driver swallows nothing itself; the callback owns its own safety.
ProgressHook = Callable[[SelectionStrategy], None]


@dataclass(frozen=True, slots=True)
class EvidencePreGate:
    """Opt-in Bradley--Terry promotion pre-gate config for the driver.

    Passed to :func:`resolve_tournament` only when the operator set
    ``params["promote_confidence_threshold"]`` (resolved by the orchestrator via
    :func:`zicato.selection.evidence_gate.read_promote_confidence_threshold`).
    When ``None`` (the default), the driver's behaviour is byte-identical to
    today — no pre-gate, no replication, no dead-letter.

    Fields
    ------
    threshold:
        The probability bar ``P(theta_child > theta_champion)`` must reach.
    replicate_budget:
        How many extra closest-CI replicates the defer→replicate loop may spend
        before going terminal (``inconclusive``).
    """

    threshold: float
    replicate_budget: int


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

    ``on_progress`` (optional) is invoked right after the batch is
    scheduled (the strategy's ``_pending`` is populated, so
    ``live_rounds()`` carries the in-flight matchups) so the caller can
    publish the live structure WHILE the round runs. It is a no-op when
    omitted, preserving the historical signature for the driver's unit
    tests.

    ``pre_gate`` (optional, opt-in) runs the Bradley--Terry "crown on
    evidence" pre-gate AFTER the strategy resolves a ``"promoted"`` decision:
    the crowning win is held unless the fitted rating clears the confidence
    threshold AND the CIs separate. While it defers and ``replicate_duel`` is
    supplied with budget remaining, the driver spends a replicate on the
    closest-CI duel, refits, and rechecks (the defer→replicate loop). With
    ``pre_gate`` ``None`` the resolution is byte-identical to today — no
    pre-gate is consulted and the strategy's decision is returned verbatim.

    Each batch runs under the caller's concurrency (the same semaphore the
    runner already uses, applied inside ``run_matchup``); the driver only
    fans them out with :func:`asyncio.gather`.
    """
    champion, challengers = await request_field(strategy.field_size())
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
        results = await asyncio.gather(*(run_matchup(m) for m in batch))
        for result in results:
            strategy.record_result(result)

    decision = strategy.champion()
    if pre_gate is None:
        return decision
    return await _apply_pre_gate(
        decision,
        champion=champion,
        pre_gate=pre_gate,
        replicate_duel=replicate_duel,
        on_inconclusive=on_inconclusive,
    )


async def _apply_pre_gate(
    decision: SelectionDecision,
    *,
    champion: Contestant,
    pre_gate: EvidencePreGate,
    replicate_duel: ReplicateDuel | None,
    on_inconclusive: OnInconclusive | None,
) -> SelectionDecision:
    """Run the pre-gate over a decision, returning only the folded decision.

    Thin wrapper over :func:`confirm_promotion_with_evidence` for
    :func:`resolve_tournament`, which does not consume the resolution
    object itself (the orchestrator's ``on_inconclusive`` callback carries
    it on the one terminal that needs recording).
    """
    confirmed, _resolution = await confirm_promotion_with_evidence(
        decision,
        champion=champion,
        pre_gate=pre_gate,
        replicate_duel=replicate_duel,
        on_inconclusive=on_inconclusive,
    )
    return confirmed


async def confirm_promotion_with_evidence(
    decision: SelectionDecision,
    *,
    champion: Contestant,
    pre_gate: EvidencePreGate,
    replicate_duel: ReplicateDuel | None,
    on_inconclusive: OnInconclusive | None = None,
) -> tuple[SelectionDecision, EvidenceResolution | None]:
    """Run the Bradley--Terry pre-gate (+ defer→replicate loop) over a decision.

    Only a ``"promoted"`` decision with an identified crowning challenger is
    eligible — a reject / defer / no-promotion passes straight through (the
    pre-gate can only hold a promotion, never force one). The crowning pair is
    ``(champion, promoted_generation_id)``; its evidence is the whole accumulated
    duel audit (``decision.matchups``), which the loop extends in place by
    replicating the closest-CI duel and re-fitting.

    The loop has two phases that share one replicate budget:

    * **Bootstrap.** A structure like the gauntlet produces a single crowning
      duel — below :data:`~zicato.selection.evidence_gate.MIN_CREDIBLE_DUELS`,
      so there is no trustworthy fit yet. When a ``replicate_duel`` runner is
      supplied with budget remaining, the loop replicates the crowning pair up
      to the credibility floor before judging. With no runner / no budget it
      passes the gate verdict through unchanged (no fit to override it — safe).
    * **Refine.** Once credible, the verdict gates: ``promoted`` (CIs cleared)
      terminates with the crown; ``deferred`` spends another closest-CI
      replicate and refits; budget exhausted with overlapping CIs terminates
      ``inconclusive``.

    Returns ``(decision, resolution)``. The decision carries the pre-gate's
    verdict folded in: ``promoted`` (cleared on evidence) or a terminal hold —
    ``deferred`` (the closed enum's token for "kept for analysis, lineage head
    unchanged") on an inconclusive duel. The accumulated audit (including any
    replicate duels) is stamped on ``matchups`` so the journal records the
    full evidence trail. ``resolution`` is the terminal
    :class:`EvidenceResolution` (verdict + CI history) when a credible
    terminal was reached, or ``None`` on a pass-through (a non-promote
    decision, or a fit that never reached credibility) — the caller journals
    it as the round's evidence block. On an inconclusive terminal the
    ``on_inconclusive`` callback additionally receives the same resolution so
    the orchestrator can write the dead-letter record.

    This is the shared crowning-confirmation used by BOTH selection shapes:
    :func:`resolve_tournament` calls it (via :func:`_apply_pre_gate`) after a
    multi-challenger structure resolves, and the orchestrator's gauntlet path
    calls it directly on its single crowning duel — the same
    defer→replicate→inconclusive adjudication regardless of structure.
    """
    promoted_id = decision.promoted_generation_id
    if decision.decision != "promoted" or promoted_id is None:
        return decision, None

    parent_id = champion.generation_id
    audit: list[MatchupResult] = list(decision.matchups)
    ci_history: list[dict[str, Any]] = []
    replicates_spent = 0
    # The audit must only ever accumulate DISTINCT draws: the replicate ids
    # encode the reserved replicate index (see the ReplicateDuel contract),
    # so a duplicate id is the same draw re-presented — appending it would
    # shrink the fitted SE by pure repetition, never by new evidence.
    seen_matchup_ids = {r.matchup_id for r in audit}

    while True:
        verdict = evidence_verdict(
            "promoted",
            decision.reason,
            audit=audit,
            parent_id=parent_id,
            child_id=promoted_id,
            threshold=pre_gate.threshold,
            replicate_budget=pre_gate.replicate_budget,
            replicates_spent=replicates_spent,
        )
        ci_history.append(
            {
                "p_stronger": verdict.p_stronger,
                "ci_overlap": verdict.ci_overlap,
                "replicates_spent": replicates_spent,
            }
        )

        # A credible, already-decisive verdict (promoted) terminates here.
        if verdict.credible and verdict.decision != "deferred":
            return _finalize(decision, verdict, audit, ci_history, promoted_id, on_inconclusive)

        # Either not yet credible (bootstrap toward the floor) or credibly
        # deferred (refine toward separation): both want one more replicate.
        # Without a runner or budget, or a closest-CI duel to spend on, we
        # cannot gather more evidence.
        has_budget = replicate_duel is not None and replicates_spent < pre_gate.replicate_budget
        candidate = (
            closest_ci_duel(audit, restrict_to=(parent_id, promoted_id)) if has_budget else None
        )
        if replicate_duel is None or candidate is None:
            if not verdict.credible:
                # Never reached the credibility floor → no trustworthy fit to
                # override the gate; the strategy's promotion stands verbatim.
                return decision, None
            # Credible but unresolved with no way to spend more budget ⇒ the
            # hold is terminal: a dead-letter inconclusive, not a dangling defer.
            terminal = replace(verdict, decision="inconclusive")
            return _finalize(decision, terminal, audit, ci_history, promoted_id, on_inconclusive)

        extra = await replicate_duel(candidate.left_id, candidate.right_id)
        # The spend is counted regardless: the budget bounds duels RUN, and
        # skipping the count on a duplicate would loop forever against a
        # runner that keeps replaying one draw.
        replicates_spent += 1
        if extra.matchup_id in seen_matchup_ids:
            log.warning(
                "evidence pre-gate: replicate duel returned an already-audited "
                "draw (matchup_id %r) — not appended to the Bradley--Terry "
                "audit; identical data must never separate CIs",
                extra.matchup_id,
            )
            continue
        seen_matchup_ids.add(extra.matchup_id)
        audit.append(extra)


def _finalize(
    decision: SelectionDecision,
    verdict: EvidenceVerdict,
    audit: list[MatchupResult],
    ci_history: list[dict[str, Any]],
    promoted_id: str,
    on_inconclusive: OnInconclusive | None,
) -> tuple[SelectionDecision, EvidenceResolution | None]:
    """Fold a terminal pre-gate verdict into the crowned decision.

    A ``promoted`` verdict keeps the crown; ``inconclusive`` maps to the
    closed enum's ``DEFERRED`` token (the experiment is kept for analysis, the
    lineage head unchanged) and fires ``on_inconclusive`` so the caller records
    the dead-letter entry. A ``credible=False`` pass-through keeps the original
    decision verbatim (no fit to override it). Returns the decision paired
    with the terminal :class:`EvidenceResolution` (``None`` on pass-through).
    """
    from zicato.core import TournamentDecision  # noqa: PLC0415

    if not verdict.credible:
        # No trustworthy fit ⇒ the strategy's decision stands unchanged.
        return decision, None

    resolution = EvidenceResolution(verdict=verdict, ci_history=tuple(ci_history))

    if verdict.decision == "promoted":
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
    "resolve_tournament",
    "confirm_promotion_with_evidence",
    "RequestField",
    "RunMatchup",
    "ReplicateDuel",
    "OnInconclusive",
    "ProgressHook",
    "EvidencePreGate",
    "EvidenceResolution",
    "rating_block",
]
