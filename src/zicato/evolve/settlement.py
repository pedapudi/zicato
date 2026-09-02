"""Commit one field round, publish its views, and close it.

The round pipeline's decide phase. :func:`settle_field_round` is the whole
of it, in four steps that always run in this order and nowhere else:

* :func:`_build_field_settlement` turns the crowning into one terminal
  :class:`OutcomeRecord` per applied challenger;
* :func:`_commit_field_settlement` records the complete receipt, then commits
  outcomes, lineage, the champion pointer, journals, and the settled bracket;
* :func:`_publish_field_observations` publishes the Pareto observation and live
  dashboard envelope after the canonical commit;
* :func:`_close_field_round` runs the round epilogue and returns the
  round's summary.

The values passed between the steps — :class:`RoundSettlement` and the
post-promotion hook failure — stay inside the module, because nothing
outside it can observe a round mid-settlement.

Every write in this phase happens on the post-holdout, post-integrity,
post-override truth, so no durable store can describe a crowning the
champion pointer contradicts.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any, Literal
from uuid import uuid4

from zicato.core.types import (
    MatchOutcome,
    OutcomeRecord,
    TournamentDecision,
)
from zicato.evolve.dashboard_projection import (
    _field_tournament_record,
    _serialise_rounds,
    _serialise_standings,
    _settle_active_tournament,
)
from zicato.evolve.decision_support import (
    _generalization_fields_from_scalars,
    _token_clip_state,
)
from zicato.evolve.field_candidates import CandidateField
from zicato.evolve.field_execution import FieldExecution
from zicato.evolve.gate import FieldVerdict, _first_aggregate_for
from zicato.evolve.generation_phase import FieldRound
from zicato.evolve.lifecycle_services import _beat, _now_iso
from zicato.evolve.pareto import record_round_frontier
from zicato.evolve.persist import _round_epilogue
from zicato.evolve.placebo import is_placebo_experiment
from zicato.evolve.promote_hook import fire_on_promote
from zicato.evolve.propose_apply import _maybe_run_placebo_arm_gauntlet
from zicato.evolve.round_api import EvolveRoundOutcome
from zicato.evolve.round_reporting import _promoted_entry_regressions
from zicato.evolve.settlement_recovery import (
    SETTLEMENT_INTENT_FORMAT_VERSION,
    commit_field_settlement,
    record_promotion_hook_delivery,
)
from zicato.selection.strategy import SelectionDecision
from zicato.util import best_effort
from zicato.workspace import generation_round_number

if TYPE_CHECKING:
    from zicato.evolve.propose_apply import _AppliedChallenger

log = logging.getLogger("zicato.orchestrator")


@dataclass(frozen=True, slots=True)
class CandidateSettlement:
    """The terminal outcome for one applied challenger."""

    challenger: _AppliedChallenger
    outcome: OutcomeRecord


@dataclass(frozen=True, slots=True)
class RoundSettlement:
    """The complete decision that one persistence tail commits.

    ``champion_scalar`` and ``crowned_scalar`` are the standings-derived
    scalars the round summary falls back to when no crowning duel ran; a
    round that crowned nothing reports the champion's own scalar on both
    sides.
    """

    decision: SelectionDecision
    primary_promoted_generation_id: str | None
    promoted_generation_ids: tuple[str, ...]
    candidates: tuple[CandidateSettlement, ...]
    champion_scalar: float = 0.0
    crowned_scalar: float = 0.0


def ordered_promotions(primary: str | None, promoted: set[str]) -> tuple[str, ...]:
    """Return the primary champion first, followed by other promoted ids."""

    if primary is None:
        return tuple(sorted(promoted))
    return (primary, *sorted(promoted - {primary}))


def _publish_field_observations(
    field_round: FieldRound,
    candidates: CandidateField,
    execution: FieldExecution,
    verdict: FieldVerdict,
) -> None:
    """Publish observational views after canonical settlement completes.

    The Pareto frontier row (docs/design/PARETO-FRONTIER.md) names the
    round's champion — the crowned generation when the field crowned one,
    else the incumbent — and names the placebo ids explicitly, because a
    field round can carry the random-baseline arm inside its slate and a
    no-op re-emission of the champion must never land on the record.
    Record-only: it never fails a round.

    The live envelope is settled with the resolved rounds and standings so
    the dashboard sees the final topology until the next round starts. The
    durable field record has already landed through the replayable settlement
    commit before this function runs.
    """

    record_round_frontier(
        workspace_root=field_round.workspace_root,
        epoch_id=field_round.epoch_id,
        round_index=field_round.round_index,
        weights=field_round.weights,
        champion_generation_id=verdict.promoted_id or field_round.parent_id,
        aggregates=execution.aggregates,
        placebo_generation_ids=[
            c.generation_id for c in candidates.challengers if is_placebo_experiment(c.experiment)
        ],
        round_log=field_round.round_log,
    )
    _settle_active_tournament(
        field_round.workspace_root,
        tournament_id=candidates.tournament_id,
        epoch_id=field_round.epoch_id,
        structure=field_round.tournament_spec.structure,
        structure_params=dict(field_round.tournament_spec.params),
        competitors=candidates.competitors,
        strategy=field_round.strategy,
        decision=verdict.effective_decision,
        round_index=field_round.round_index,
        total_rounds=field_round.total_rounds,
        field_status=candidates.field_status,
    )


def _match_records(decision: Any, generation_ids: list[str]) -> dict[str, list[MatchOutcome]]:
    """Collect each challenger's per-duel record from the settled bracket."""

    matches_by_gen: dict[str, list[MatchOutcome]] = {gid: [] for gid in generation_ids}
    for mr in decision.matchups:
        delta = mr.outcome.delta_scalar
        winner = mr.lower_scalar_id()
        if mr.left_id in matches_by_gen:
            matches_by_gen[mr.left_id].append(
                MatchOutcome(
                    match_id=mr.matchup_id,
                    opponent=mr.right_id,
                    won=(winner == mr.left_id),
                    delta_scalar=-delta,
                )
            )
        if mr.right_id in matches_by_gen:
            matches_by_gen[mr.right_id].append(
                MatchOutcome(
                    match_id=mr.matchup_id,
                    opponent=mr.left_id,
                    won=(winner == mr.right_id),
                    delta_scalar=delta,
                )
            )
    return matches_by_gen


def _crowning_duel_deltas(raw_crowning: Any, parent_id: str) -> tuple[float, float]:
    """The crowning duel's pass-rate and drift-loss deltas, challenger-first.

    The runner reports its result with ``parent_*`` describing the duel's
    LEFT side, which is the champion by the strategy's convention but need
    not be, so the orientation is resolved from the ids rather than assumed.
    Both deltas are then stated as challenger minus champion, matching the
    scalar delta's sign convention.
    """

    if raw_crowning is None:
        return 0.0, 0.0
    champion_is_parent_side = raw_crowning.parent_generation_id == parent_id
    pass_rate_delta = float(raw_crowning.outcome.delta_pass_rate) * (
        1.0 if champion_is_parent_side else -1.0
    )
    champion_drift = float(
        (raw_crowning.parent_agg if champion_is_parent_side else raw_crowning.child_agg).get(
            "drift_loss_mean", 0.0
        )
    )
    challenger_drift = float(
        (raw_crowning.child_agg if champion_is_parent_side else raw_crowning.parent_agg).get(
            "drift_loss_mean", 0.0
        )
    )
    return pass_rate_delta, challenger_drift - champion_drift


def _build_field_settlement(
    field_round: FieldRound,
    candidates: CandidateField,
    execution: FieldExecution,
    verdict: FieldVerdict,
) -> RoundSettlement:
    """Turn the crowning into one terminal outcome per applied challenger.

    The crowning challenger — the survivor that reached the final
    champion-gate duel — carries the Ladder/holdout evidence block, the
    per-generation train/holdout/gap fields, and the crowning duel's own
    pass-rate and drift-loss deltas.  A holdout-demoted crown carries the
    ``holdout_not_confirmed`` reason from the confirmation step rather than
    the strategy's crowning reason.  Every other challenger is a dead bracket
    branch: no holdout block, and the strategy's reason.

    An operator override on a generation makes its verdict explicit — the
    reject reason carries the override note, and a forced promote clears it.
    """

    decision = execution.decision
    rank_by_id = {s.generation_id: s.rank for s in decision.standings}
    matches_by_gen = _match_records(decision, [c.generation_id for c in candidates.challengers])
    champion_agg = _first_aggregate_for(field_round.parent_id, decision)
    parent_scalar = float(champion_agg.get("scalar", 0.0)) if champion_agg else 0.0

    settlements: list[CandidateSettlement] = []
    crowned_scalar = parent_scalar
    for challenger in candidates.challengers:
        gid = challenger.generation_id
        # A generation is crowned if it is in the (possibly multi-element)
        # promoted set — see `_apply_field_overrides` for the no-override
        # single-promotion invariant (the set is exactly ``{promoted_id}``).
        is_crowned = gid in verdict.promoted_ids
        is_crowning_challenger = gid == verdict.crowning_challenger_id
        gid_override = verdict.overrides.get(gid)
        if is_crowned:
            gen_decision = TournamentDecision.PROMOTED
        elif is_crowning_challenger and decision.decision == "deferred":
            gen_decision = TournamentDecision.DEFERRED
        else:
            gen_decision = TournamentDecision.REJECTED
        agg = _first_aggregate_for(gid, decision)
        gen_scalar = float(agg.get("scalar", 0.0)) if agg else 0.0
        if gid == verdict.promoted_id:
            crowned_scalar = gen_scalar
        if is_crowning_challenger:
            rejection_reason = (
                ""
                if is_crowned
                else (verdict.reason_override if verdict.reason_override else decision.reason)
            )
            holdout_block = verdict.holdout_block
            # Pair the crowning duel's TRAIN scalar with its HOLDOUT scalar so
            # the gap is measured on the same duel (falls back to the standings
            # aggregate only if the crowning train scalar is somehow absent).
            crown_train = (
                verdict.crowning_challenger_train_scalar
                if verdict.crowning_challenger_train_scalar is not None
                else gen_scalar
            )
            gen_fields = _generalization_fields_from_scalars(
                crown_train, verdict.holdout_child_scalar
            )
            pass_rate_delta, drift_loss_delta = _crowning_duel_deltas(
                execution.raw_results.get(decision.crowning_matchup_id),
                field_round.parent_id,
            )
        else:
            rejection_reason = "" if is_crowned else decision.reason
            holdout_block = None
            gen_fields = _generalization_fields_from_scalars(gen_scalar, None)
            pass_rate_delta = 0.0
            drift_loss_delta = 0.0
        operator_override = gid_override is not None
        operator_override_reason = gid_override.reason if gid_override is not None else ""
        if gid_override is not None:
            rejection_reason = f"operator override: {gid_override.reason}" if not is_crowned else ""
        settlements.append(
            CandidateSettlement(
                challenger=challenger,
                outcome=OutcomeRecord(
                    ran_at=_now_iso(),
                    drift_movements=(),
                    pass_rate_delta=pass_rate_delta,
                    drift_loss_delta=drift_loss_delta,
                    scalar_score_delta=gen_scalar - parent_scalar,
                    tournament_decision=gen_decision,
                    rejection_reason=rejection_reason,
                    operator_override=operator_override,
                    operator_override_reason=operator_override_reason,
                    structure=field_round.tournament_spec.structure,
                    final_rank=rank_by_id.get(gid),
                    match_record=tuple(matches_by_gen.get(gid, ())),
                    champion_eval_mode=execution.champion_eval_mode,
                    holdout=holdout_block,
                    train_loss=gen_fields["train_loss"],
                    holdout_loss=gen_fields["holdout_loss"],
                    generalization_gap=gen_fields["generalization_gap"],
                    evidence=execution.gate_evidence if is_crowning_challenger else None,
                ),
            )
        )
    return RoundSettlement(
        decision=verdict.effective_decision,
        primary_promoted_generation_id=verdict.promoted_id,
        promoted_generation_ids=ordered_promotions(verdict.promoted_id, verdict.promoted_ids),
        candidates=tuple(settlements),
        champion_scalar=parent_scalar,
        crowned_scalar=crowned_scalar,
    )


def _assert_crowning_agrees(settlement: RoundSettlement, candidates: CandidateField) -> None:
    """Refuse to persist a bracket the champion pointer would contradict.

    The durable bracket and the champion state MUST agree (issue #20).  A
    settled bracket that records ``promoted`` with a promoted generation the
    champion pointer and lineage never advance to — or the inverse — is a
    silent correctness bug, so this fails loudly before lineage is written.
    The persisted decision is the post-holdout truth and the primary promoted
    id is what drives lineage and ``current_generation``; the two are the
    same value by construction, and this guard makes that contract explicit
    and catches any future code path that lets them drift apart.
    """

    bracket_promoted = settlement.decision.decision == "promoted"
    if bracket_promoted != (settlement.primary_promoted_generation_id is not None):
        raise RuntimeError(
            "crowning invariant violated: settled bracket decision "
            f"{settlement.decision.decision!r} (promoted_generation_id="
            f"{settlement.decision.promoted_generation_id!r}) disagrees with the "
            "champion to be crowned "
            f"({settlement.primary_promoted_generation_id!r}); refusing to persist a "
            "bracket the champion pointer / lineage contradict"
        )
    decision_primary = settlement.decision.promoted_generation_id or None
    if decision_primary != settlement.primary_promoted_generation_id:
        raise RuntimeError(
            "crowning invariant violated: settled bracket names primary champion "
            f"{decision_primary!r}, but settlement would advance "
            f"{settlement.primary_promoted_generation_id!r}"
        )
    if (
        settlement.primary_promoted_generation_id is not None
        and settlement.primary_promoted_generation_id not in candidates.by_id
    ):
        raise RuntimeError(
            "crowning invariant violated: settled bracket promotes "
            f"{settlement.primary_promoted_generation_id!r} but no such challenger "
            "applied this round; "
            "refusing to advance the champion to a generation with no snapshot"
        )


async def _commit_field_settlement(
    field_round: FieldRound,
    candidates: CandidateField,
    execution: FieldExecution,
    verdict: FieldVerdict,
    settlement: RoundSettlement,
) -> tuple[str, str, str] | None:
    """Commit the replayable settlement and run the promotion hook.

    The complete decision lands in a settlement receipt before any outcome
    write. The recovery module applies outcomes, lineage, the champion marker,
    journals, the settled bracket, and one reported derived-index refresh in
    that order. Every canonical write is idempotent, so startup can repeat the
    commit without evaluating the tournament again.

    The post-promotion adapter hook (issue #125) fires once, after the
    champion marker advances, for the PRIMARY head only: an operator
    multi-promote marks several candidates promoted in lineage, but
    ``current_generation`` advances to exactly one, and it is that crowning
    the adapter's out-of-tree state has to track.
    """

    _assert_crowning_agrees(settlement, candidates)
    receipt = _field_settlement_receipt(
        field_round,
        candidates,
        execution,
        verdict,
        settlement,
    )
    commit_field_settlement(field_round.workspace_root, receipt)

    on_promote_failure: tuple[str, str, str] | None = None
    promoted_id = settlement.primary_promoted_generation_id
    if promoted_id is not None:
        settlement_id = str(receipt["settlement_id"])
        hook = getattr(field_round.adapter, "on_promote", None)
        adapter_name = str(
            getattr(field_round.adapter, "name", None) or type(field_round.adapter).__name__
        )
        if hook is not None and callable(hook):
            try:
                record_promotion_hook_delivery(
                    field_round.workspace_root,
                    epoch_id=field_round.epoch_id,
                    round_index=field_round.round_index,
                    settlement_id=settlement_id,
                    state="delivery_unknown",
                    adapter_name=adapter_name,
                )
            except Exception as exc:  # noqa: BLE001 — canonical settlement is complete
                log.error(
                    "on_promote hook skipped for %s/%s because its delivery state "
                    "could not be persisted",
                    field_round.epoch_id,
                    promoted_id,
                    exc_info=exc,
                )
                return (adapter_name, promoted_id, type(exc).__name__)
            on_promote_failure = await fire_on_promote(
                field_round.adapter,
                workspace_root=field_round.workspace_root,
                epoch_id=field_round.epoch_id,
                generation_id=promoted_id,
                parent_generation_id=field_round.parent_id,
                snapshot_root=candidates.by_id[promoted_id].snapshot_root,
            )
            delivery_state: Literal["failed", "succeeded"] = (
                "failed" if on_promote_failure is not None else "succeeded"
            )
            failure_type = on_promote_failure[2] if on_promote_failure is not None else ""
            try:
                record_promotion_hook_delivery(
                    field_round.workspace_root,
                    epoch_id=field_round.epoch_id,
                    round_index=field_round.round_index,
                    settlement_id=settlement_id,
                    state=delivery_state,
                    adapter_name=adapter_name,
                    failure_type=failure_type,
                )
            except Exception as exc:  # noqa: BLE001 — unknown is the safe final state
                log.error(
                    "on_promote hook delivery result for %s/%s could not be persisted; "
                    "the receipt remains delivery_unknown and recovery will not retry it",
                    field_round.epoch_id,
                    promoted_id,
                    exc_info=exc,
                )
                if on_promote_failure is None:
                    on_promote_failure = (adapter_name, promoted_id, type(exc).__name__)

    return on_promote_failure


def _field_settlement_receipt(
    field_round: FieldRound,
    candidates: CandidateField,
    execution: FieldExecution,
    verdict: FieldVerdict,
    settlement: RoundSettlement,
) -> dict[str, Any]:
    """Serialize every fact needed to validate, replay, and audit settlement."""
    champion_agg = _first_aggregate_for(field_round.parent_id, execution.decision)
    candidate_records: list[dict[str, Any]] = []
    for candidate in settlement.candidates:
        challenger = candidate.challenger
        generation_id = challenger.generation_id
        aggregate = _first_aggregate_for(generation_id, execution.decision)
        candidate_records.append(
            {
                "experiment_id": challenger.experiment.id,
                "generation_id": generation_id,
                "created_at": challenger.generation.created_at,
                "parent_scalar": (
                    float(champion_agg["scalar"]) if champion_agg is not None else None
                ),
                "child_scalar": float(aggregate["scalar"]) if aggregate is not None else None,
                "outcome": asdict(candidate.outcome),
            }
        )

    field_record = _field_tournament_record(
        field_tournament_id=f"{field_round.epoch_id}:field:{candidates.first_challenger_id}",
        epoch_id=field_round.epoch_id,
        structure=field_round.tournament_spec.structure,
        structure_params=dict(field_round.tournament_spec.params),
        competitors=candidates.competitors,
        rounds=_serialise_rounds(field_round.strategy.rounds()),
        standings=_serialise_standings(settlement.decision.standings),
        field_status=candidates.field_status or [],
        decision=settlement.decision,
        state="settled",
        override_status=verdict.override_provenance or None,
        promoted_generation_ids=(
            sorted(verdict.promoted_ids) if len(settlement.promoted_generation_ids) > 1 else None
        ),
    )
    # The nonce prevents proposer-authored journal prose from forging the
    # replay marker. Persisting it in the intent makes it stable across replay.
    settlement_id = uuid4().hex
    hook = getattr(field_round.adapter, "on_promote", None)
    hook_adapter_name = str(
        getattr(field_round.adapter, "name", None) or type(field_round.adapter).__name__
    )
    hook_is_applicable = (
        settlement.primary_promoted_generation_id is not None
        and hook is not None
        and callable(hook)
    )
    return {
        "format_version": SETTLEMENT_INTENT_FORMAT_VERSION,
        "state": "pending",
        "settlement_id": settlement_id,
        "epoch_id": field_round.epoch_id,
        "round_index": field_round.round_index,
        "primary_promoted_generation_id": settlement.primary_promoted_generation_id,
        "candidates": candidate_records,
        "field_tournament_record": field_record,
        "index_projection": {"state": "pending", "error_type": ""},
        "promotion_hook": {
            "state": "pending" if hook_is_applicable else "not_applicable",
            "adapter_name": hook_adapter_name if hook_is_applicable else "",
            "failure_type": "",
        },
    }


def _round_summary(
    field_round: FieldRound,
    candidates: CandidateField,
    execution: FieldExecution,
    verdict: FieldVerdict,
    settlement: RoundSettlement,
) -> tuple[str, float, float]:
    """The round summary's proposed generation and its two scalars.

    The scalars MUST come from the gate's CROWNING matchup — the same
    champion-versus-leader duel the rejection reason is built from.  Two
    other sources are wrong here: the per-pairing standings aggregate
    averages across all of the champion's pairings, and a
    child-defaults-to-parent fallback reports delta 0.0 on a rejection even
    though the gate measured a real regression (issue #10).  The standings
    aggregate is the fallback only when no crowning duel ran.

    On a rejection the proposed generation reported is the LEADING challenger
    that reached the gate — the one the reason is about — rather than an
    arbitrary first slot.
    """

    decision = execution.decision
    crowning = (
        next(
            (m for m in decision.matchups if m.matchup_id == decision.crowning_matchup_id),
            None,
        )
        if decision.crowning_matchup_id
        else None
    )
    if crowning is None:
        return (
            verdict.promoted_id or candidates.first_challenger_id,
            settlement.champion_scalar,
            settlement.crowned_scalar,
        )
    champ_is_left = crowning.left_id == field_round.parent_id
    return (
        verdict.promoted_id or (crowning.right_id if champ_is_left else crowning.left_id),
        crowning.left_scalar() if champ_is_left else crowning.right_scalar(),
        crowning.right_scalar() if champ_is_left else crowning.left_scalar(),
    )


async def _close_field_round(
    field_round: FieldRound,
    candidates: CandidateField,
    execution: FieldExecution,
    verdict: FieldVerdict,
    settlement: RoundSettlement,
    on_promote_failure: tuple[str, str, str] | None,
) -> EvolveRoundOutcome:
    """Run the round epilogue, close the round's log, and summarise it.

    A one-challenger field runs the random-baseline placebo as a separate
    duel here, because a one-slot slate has no room to carry it; a wider
    field already fielded it inside the slate
    (:mod:`zicato.evolve.field_candidates`).  The placebo duel never advances
    the champion.
    """

    from zicato.runtime import progress_log  # noqa: PLC0415

    decision = execution.decision
    promoted_id = verdict.promoted_id
    first_challenger_id = candidates.first_challenger_id
    if field_round.field_size == 1:
        await _maybe_run_placebo_arm_gauntlet(
            workspace_root=field_round.workspace_root,
            epoch_id=field_round.epoch_id,
            adapter=field_round.adapter,
            parent_gen=candidates.champion,
            parent_id=field_round.parent_id,
            round_id=candidates.base_generation_id,
            mutations=field_round.mutations,
            board=field_round.board,
            weights=field_round.weights,
            config=field_round.config,
            disable_drift=field_round.disable_drift,
            judge_only=field_round.judge_only,
            fast_mode=field_round.fast_mode,
            round_index=field_round.round_index,
            total_rounds=field_round.total_rounds,
        )

    health_summary, health_critical = await _round_epilogue(
        workspace_root=field_round.workspace_root,
        epoch_id=field_round.epoch_id,
        board=field_round.board,
        round_n=generation_round_number(first_challenger_id) or field_round.round_index,
        analyzer_round=generation_round_number(first_challenger_id),
        mutations=field_round.mutations,
        auxiliary_call_llm=field_round.auxiliary_call_llm,
        auxiliary_model=field_round.auxiliary_model,
        meta_loop_emitter=field_round.meta_loop_emitter,
        token_clip=_token_clip_state(field_round.config),
        attributable_regressions=(
            _promoted_entry_regressions(execution.raw_results[decision.crowning_matchup_id])
            if promoted_id is not None and decision.crowning_matchup_id in execution.raw_results
            else None
        ),
        on_promote_failure=on_promote_failure,
    )

    bookkeeping_decision = "promoted" if promoted_id is not None else "rejected"
    # Progress transition: the field tournament settled — record the
    # crowning (TOURNAMENT_SETTLE) then the terminal verdict so the
    # liveness seq lands on a PROMOTE/REJECT at the round's true end.
    with best_effort(
        "progress-log field tournament-settle",
        on_error=lambda exc: log.debug("progress-log field tournament-settle skipped: %s", exc),
    ):
        progress_log.append_progress(field_round.workspace_root, progress_log.TOURNAMENT_SETTLE)
    _beat(
        field_round.beater,
        workspace_root=field_round.workspace_root,
        progress=(
            progress_log.PROMOTE if bookkeeping_decision == "promoted" else progress_log.REJECT
        ),
        epoch_id=field_round.epoch_id,
        generation_id=promoted_id or first_challenger_id,
        round_index=field_round.round_index,
        phase=f"done:round_{field_round.round_index}:{candidates.tournament_id}:"
        f"{bookkeeping_decision}",
    )
    field_round.round_log.emit("round_closed")

    child_id, parent_scalar, child_scalar = _round_summary(
        field_round, candidates, execution, verdict, settlement
    )
    return EvolveRoundOutcome(
        parent_generation_id=field_round.parent_id,
        proposed_generation_id=child_id,
        tournament_decision=bookkeeping_decision,
        rejection_reason=(
            # The effective decision is the post-holdout, post-integrity, and
            # post-override truth, so its reason matches the persisted outcome
            # for every rejection path.
            "" if promoted_id is not None else verdict.effective_decision.reason
        ),
        parent_scalar=parent_scalar,
        child_scalar=child_scalar,
        delta_scalar=child_scalar - parent_scalar,
        health_summary=health_summary,
        health_critical=health_critical,
    )


async def settle_field_round(
    field_round: FieldRound,
    candidates: CandidateField,
    execution: FieldExecution,
    verdict: FieldVerdict,
) -> EvolveRoundOutcome:
    """Commit, publish, and close the round the crowning verdict resolved.

    The round's terminal phase and the last thing that runs: it returns the
    summary the evolve loop reads.
    """

    settlement = _build_field_settlement(field_round, candidates, execution, verdict)
    on_promote_failure = await _commit_field_settlement(
        field_round, candidates, execution, verdict, settlement
    )
    _publish_field_observations(field_round, candidates, execution, verdict)
    return await _close_field_round(
        field_round, candidates, execution, verdict, settlement, on_promote_failure
    )


__all__ = ["ordered_promotions", "settle_field_round"]
