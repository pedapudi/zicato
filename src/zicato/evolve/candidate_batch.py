"""Produce the candidate batch consumed by every tournament structure."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from zicato.core.types import (
    Experiment,
    Generation,
    OutcomeRecord,
    PriorExperiment,
    TournamentDecision,
)
from zicato.evolve import generation_phase
from zicato.evolve.generation_phase import PreparedRound
from zicato.evolve.ingest import _ingest_experiment_into_index, _load_prior_experiments
from zicato.evolve.lifecycle_services import _now_iso
from zicato.evolve.operator_slot import (
    OperatorCandidateRejected,
    mint_operator_experiment,
    minted_operator_queue_ids,
)
from zicato.evolve.propose_apply import (
    CandidateAttempt,
    _AppliedChallenger,
    _diversity_signature,
    _mint_challenger_field,
    _propose_and_apply_challenger,
    _trim_reason,
)
from zicato.evolve.round_context import _recombine_pair_for_slot
from zicato.models_config import load_models_config
from zicato.runtime.operator_queue import (
    PendingOperatorRound,
    QueuedCandidate,
    QueuedRound,
    consume_operator_round,
    next_operator_round,
)
from zicato.util import best_effort
from zicato.workspace import generation_round_number

if TYPE_CHECKING:
    from zicato.mutation.policy import MutationPolicy
    from zicato.runtime.resume import ResumePlan

log = logging.getLogger("zicato.orchestrator")


@dataclass(frozen=True, slots=True)
class CandidateRejection:
    """One candidate slot that did not enter the tournament."""

    generation_id: str
    reason: str
    status: dict[str, Any]
    proposer_error: Exception | None = None


@dataclass(frozen=True, slots=True)
class CandidateBatch:
    """The incumbent, applied challengers, and rejected candidate slots."""

    incumbent: Generation
    requested_size: int
    base_generation_id: str
    challengers: tuple[_AppliedChallenger, ...]
    rejections: tuple[CandidateRejection, ...]
    field_status: tuple[dict[str, Any], ...]
    resumed_generation_ids: frozenset[str] = frozenset()


def _persist_soft_reject(
    prepared: PreparedRound,
    challenger: _AppliedChallenger,
    reason: str,
    detail: str = "",
) -> None:
    """Settle a diversity rejection in outcome, lineage, and index."""

    from zicato.epoch import append_to_lineage, update_experiment_outcome  # noqa: PLC0415

    generation_id = challenger.generation_id
    full_reason = f"{reason}: {detail}" if detail else reason
    update_experiment_outcome(
        prepared.workspace_root,
        prepared.epoch_id,
        generation_id,
        OutcomeRecord(
            ran_at=_now_iso(),
            metric_movements=(),
            pass_rate_delta=0.0,
            drift_loss_delta=0.0,
            scalar_score_delta=0.0,
            tournament_decision=TournamentDecision.REJECTED,
            rejection_reason=full_reason,
        ),
    )
    append_to_lineage(
        prepared.workspace_root,
        prepared.epoch_id,
        replace(challenger.generation, promoted=False),
        parent_id=prepared.parent_generation.id,
        rejection_reason=full_reason,
    )
    _ingest_experiment_into_index(
        prepared.workspace_root,
        prepared.epoch_id,
        generation_id,
    )


def _take_operator_round(prepared: PreparedRound, slots: int) -> PendingOperatorRound | None:
    """The queued operator round this round takes, or ``None``."""
    workspace_root = prepared.workspace_root
    epoch_id = prepared.epoch_id
    try:
        pending = next_operator_round(
            workspace_root,
            epoch_id,
            slots=slots,
            minted=lambda: minted_operator_queue_ids(workspace_root, epoch_id),
        )
    except OSError as exc:
        log.warning("operator round queue is unreadable (%s); the round proposes", exc)
        return None
    if pending is not None:
        queued = pending.queued
        log.info(
            "round %d takes operator round %s by %s: %d candidate(s)%s",
            prepared.round_index,
            queued.queue_id,
            queued.author,
            len(queued.candidates),
            " (exclusive)" if queued.exclusive else "",
        )
    return pending


def _mint_operator_slot(
    prepared: PreparedRound,
    queued: QueuedRound,
    candidate: QueuedCandidate,
    policy: MutationPolicy,
    next_id: str,
    seed: int,
) -> Experiment | CandidateRejection:
    """Mint one queued candidate for its slot, or reject it with its findings."""
    from zicato.epoch.genstore import default_generation_store  # noqa: PLC0415

    try:
        return mint_operator_experiment(
            genstore=default_generation_store(prepared.workspace_root),
            epoch_id=prepared.epoch_id,
            parent_id=prepared.parent_generation.id,
            next_id=next_id,
            round_index=prepared.round_index,
            queued=queued,
            candidate=candidate,
            policy=policy,
            mutations=prepared.mutations,
            custom_judge_names=prepared.custom_judge_names,
            adapter=prepared.adapter,
        )
    except OperatorCandidateRejected as exc:
        findings = list(exc.findings) or ["the operator candidate cannot be minted"]
    log.warning(
        "operator candidate for %s (round %s) cannot enter round %d: %s",
        next_id,
        queued.queue_id,
        prepared.round_index,
        "; ".join(findings),
    )
    if prepared.round_log is not None:
        prepared.round_log.emit(
            "proposal_attempted", {"errors": tuple(findings)}, {"generation_id": next_id}
        )
    reason = _trim_reason(findings[-1], limit=160)
    status = {
        "generation_id": next_id,
        "status": "rejected",
        "reason": reason,
        "attempts": 1,
        "attempt_reasons": [_trim_reason(finding) for finding in findings],
        "hypothesis": _trim_reason(str(candidate.hypothesis.get("core_idea", ""))),
        "seed": seed,
    }
    return CandidateRejection(generation_id=next_id, reason=reason, status=status)


async def produce_candidate_batch(
    prepared: PreparedRound,
    requested_size: int,
    *,
    resume_plan: ResumePlan | None = None,
    on_status: Callable[[dict[str, Any]], None] | None = None,
    produce_one: Any = _propose_and_apply_challenger,
) -> CandidateBatch:
    """Propose, validate, apply, persist, and admit a batch of challengers.

    Proposal sampling inside best-of-N remains owned by the proposer.  This
    function owns tournament field width: a gauntlet requests one candidate,
    while field strategies request their declared number of candidates.

    The oldest queued operator round, if any, fills the first slots with its
    candidates in place of proposal episodes
    (:mod:`zicato.runtime.operator_queue`). An exclusive round has no other
    slots, so it runs no proposal episode. The field-diversity soft-reject
    never compares two operator candidates: the operator chose them on
    purpose. A round that resumes a candidate in place takes no queued round.
    """

    if requested_size < 1:
        raise ValueError("candidate batch size must be at least one")

    workspace_root = prepared.workspace_root
    epoch_id = prepared.epoch_id
    parent_id = prepared.parent_generation.id
    if (
        requested_size == 1
        and resume_plan is not None
        and resume_plan.resumes_in_place
        and resume_plan.resume_generation_id is not None
    ):
        base_id = resume_plan.resume_generation_id
    else:
        base_id = generation_phase.next_generation_id(workspace_root, epoch_id)
    base_n = generation_round_number(base_id)

    settled_prior = tuple(
        _load_prior_experiments(
            workspace_root,
            epoch_id,
            cross_epoch=prepared.weights.experimental.cross_epoch_memory,
            outcome_feedback=prepared.weights.experimental.outcome_feedback,
            train_entry_ids=frozenset(entry.id for entry in prepared.train_board),
            writer=prepared.writer,
        )
    )
    from zicato.mutation.policy import MutationPolicy  # noqa: PLC0415

    policy = MutationPolicy.capture(
        prepared.parent_generation.snapshot_root,
        prepared.mutations,
        prepared.brief.forbidden_ids,
        enumeration_roots=generation_phase.mutable_trees(
            prepared.adapter, prepared.parent_generation.snapshot_root
        ),
    )
    siblings: list[PriorExperiment] = []
    sibling_signatures: list[tuple[frozenset[str], str]] = []
    accepted_mutation_sets: list[frozenset[str]] = []
    diversity_tolerance = getattr(prepared.config, "diversity_tolerance", None)
    diversity_soft_rejected = 0
    applied: list[_AppliedChallenger] = []
    rejections: list[CandidateRejection] = []
    field_status: list[dict[str, Any]] = []
    resumed_ids: set[str] = set()
    resumes_in_place = resume_plan is not None and resume_plan.resumes_in_place
    operator = None if resumes_in_place else _take_operator_round(prepared, requested_size)
    operator_candidates = operator.queued.candidates if operator is not None else ()
    slot_count = (
        len(operator_candidates)
        if operator is not None and operator.queued.exclusive
        else requested_size
    )
    operator_findings: list[str] = []
    operator_applied = 0

    def _emit_status(record: dict[str, Any]) -> None:
        if on_status is None:
            return
        with best_effort(
            "candidate-batch status publish",
            on_error=lambda exc: log.debug("candidate status publish skipped: %s", exc),
        ):
            on_status(record)

    for offset in range(slot_count):
        next_id = f"v{base_n + offset}" if base_n is not None else base_id
        resume_experiment = None
        if (
            offset == 0
            and requested_size == 1
            and resume_plan is not None
            and resume_plan.resumes_in_place
            and resume_plan.resume_generation_id == next_id
        ):
            resume_experiment = resume_plan.resume_experiment

        operator_slot: dict[str, Any] = {}
        if operator is not None and offset < len(operator_candidates):
            minted = _mint_operator_slot(
                prepared, operator.queued, operator_candidates[offset], policy, next_id, offset + 2
            )
            if isinstance(minted, CandidateRejection):
                operator_findings.append(f"{next_id}: {minted.reason}")
                _emit_status(minted.status)
                field_status.append(minted.status)
                rejections.append(minted)
                continue
            operator_slot["operator_experiment"] = minted

        attempt: CandidateAttempt = await produce_one(
            writer=prepared.writer,
            workspace_root=workspace_root,
            epoch_id=epoch_id,
            parent_id=parent_id,
            next_id=next_id,
            mutations=list(prepared.mutations),
            mutation_policy=policy,
            patterns=list(prepared.patterns),
            brief=prepared.brief,
            loss_summary=prepared.loss_summary,
            evaluation_call_llm=prepared.config.effective_proposer_call_llm(),
            evaluation_model=(
                prepared.config.proposer_model
                or load_models_config(prepared.workspace_config).evaluation.model
                or ""
            ),
            max_proposer_retries=prepared.max_proposer_retries,
            beater=prepared.beater,
            round_index=prepared.round_index,
            meta_loop_emitter=prepared.meta_loop_emitter,
            seed=offset + 2,
            custom_judge_names=prepared.custom_judge_names,
            prior_experiments=settled_prior + tuple(siblings),
            proposer_agent=prepared.proposer_agent,
            restrict_visibility=prepared.weights.overfitting.restrict_proposer_visibility,
            failure_profile=prepared.failure_profile,
            insights=prepared.insights,
            metric_priorities=prepared.metric_priorities,
            process_exemplars=prepared.process_exemplars,
            genealogy=prepared.genealogy,
            calibration=prepared.calibration,
            on_status=on_status,
            round_emitter=prepared.round_log,
            screen_candidates=prepared.screen_candidates,
            recombine_pair=_recombine_pair_for_slot(prepared.recombine_pair, offset),
            resume_experiment=resume_experiment,
            **operator_slot,
        )
        challenger = attempt.challenger
        status = attempt.status
        if operator_slot and challenger is None:
            operator_findings.append(f"{next_id}: {status.get('reason', '')}")
        if challenger is None:
            field_status.append(status)
            rejections.append(
                CandidateRejection(
                    generation_id=next_id,
                    reason=str(status.get("reason", "")),
                    status=status,
                    proposer_error=attempt.proposer_error,
                )
            )
            continue

        mint = (
            None
            if operator_slot
            else _mint_challenger_field(
                challenger.experiment,
                sibling_signatures,
                accepted_mutation_sets,
                diversity_tolerance,
            )
        )
        if mint is not None and mint.action == "reject_duplicate":
            hyp = challenger.experiment.hypothesis
            status = {
                "generation_id": next_id,
                "status": "rejected",
                "reason": "field_diversity_duplicate",
                "attempts": int(status.get("attempts", 0)),
                "attempt_reasons": [
                    "duplicates an in-flight sibling (same modulating ids + core idea); "
                    "soft-rejected to keep the field diverse"
                ],
                "hypothesis": _trim_reason(hyp.core_idea),
                "seed": offset + 2,
            }
            if diversity_tolerance is not None:
                status["diversity_status"] = "soft_rejected"
                status["diversity_tolerance"] = diversity_tolerance
                diversity_soft_rejected += 1
            _emit_status(status)
            _persist_soft_reject(
                prepared,
                challenger,
                "field_diversity_duplicate",
                "duplicates an in-flight sibling (same mutation ids + core idea)",
            )
        elif mint is not None and mint.action == "reject_overlap":
            assert diversity_tolerance is not None
            peer_id = (
                applied[mint.overlap_peer_index].generation_id
                if 0 <= mint.overlap_peer_index < len(applied)
                else ""
            )
            hyp = challenger.experiment.hypothesis
            status = {
                "generation_id": next_id,
                "status": "rejected",
                "reason": "field_diversity_overlap",
                "diversity_status": "soft_rejected",
                "attempts": int(status.get("attempts", 0)),
                "attempt_reasons": [
                    f"mutation-id overlap {mint.overlap:.3f} with sibling "
                    f"{peer_id or '(accepted)'} exceeds diversity_tolerance "
                    f"{diversity_tolerance:.3f}; soft-rejected to keep the field diverse"
                ],
                "hypothesis": _trim_reason(hyp.core_idea),
                "seed": offset + 2,
                "overlap": round(mint.overlap, 6),
                "overlap_peer": peer_id,
                "diversity_tolerance": diversity_tolerance,
            }
            diversity_soft_rejected += 1
            _emit_status(status)
            _persist_soft_reject(
                prepared,
                challenger,
                "field_diversity_overlap",
                f"overlap {mint.overlap:.3f} with sibling {peer_id or '(accepted)'} "
                f"exceeds diversity_tolerance {diversity_tolerance:.3f}",
            )
        else:
            if diversity_tolerance is not None:
                status = {
                    **status,
                    "diversity_status": "applied",
                    "diversity_tolerance": diversity_tolerance,
                }
            field_status.append(status)
            applied.append(challenger)
            if operator_slot:
                operator_applied += 1
            if attempt.resumed:
                resumed_ids.add(next_id)
            hyp = challenger.experiment.hypothesis
            accepted_mutation_sets.append(frozenset(hyp.modulating))
            siblings.append(
                PriorExperiment(
                    generation_id=next_id,
                    epoch_id=epoch_id,
                    core_idea=hyp.core_idea,
                    modulating=tuple(hyp.modulating),
                    decision="in_flight",
                    rejection_reason="",
                    scalar_score_delta=None,
                    same_contract=True,
                )
            )
            sibling_signatures.append(_diversity_signature(challenger.experiment))
            continue

        field_status.append(status)
        rejections.append(CandidateRejection(next_id, str(status["reason"]), status))

    if operator is not None and not operator_applied:
        # No candidate of the queued round has a generation, so no settlement
        # will consume it: consume it now, with each slot's findings.
        consume_operator_round(
            workspace_root,
            operator.queued.queue_id,
            reason="rejected before the tournament: " + "; ".join(operator_findings),
        )

    if diversity_tolerance is not None and diversity_soft_rejected:
        log.info(
            "candidate batch soft-rejected %d challenger(s) for field-diversity "
            "overlap (tolerance %.3f); %d kept",
            diversity_soft_rejected,
            diversity_tolerance,
            len(applied),
        )

    return CandidateBatch(
        incumbent=prepared.parent_generation,
        requested_size=requested_size,
        base_generation_id=base_id,
        challengers=tuple(applied),
        rejections=tuple(rejections),
        field_status=tuple(field_status),
        resumed_generation_ids=frozenset(resumed_ids),
    )


__all__ = ["CandidateBatch", "CandidateRejection", "produce_candidate_batch"]
