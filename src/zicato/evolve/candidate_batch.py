"""Produce the candidate batch consumed by every tournament structure."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from zicato.core.types import Generation, OutcomeRecord, PriorExperiment, TournamentDecision
from zicato.evolve import generation_phase
from zicato.evolve.generation_phase import PreparedRound
from zicato.evolve.ingest import _ingest_experiment_into_index, _load_prior_experiments
from zicato.evolve.lifecycle_services import _now_iso
from zicato.evolve.propose_apply import (
    CandidateAttempt,
    _AppliedChallenger,
    _diversity_signature,
    _mint_challenger_field,
    _propose_and_apply_challenger,
    _trim_reason,
)
from zicato.evolve.round_context import _recombine_pair_for_slot
from zicato.util import best_effort
from zicato.workspace import generation_round_number

if TYPE_CHECKING:
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
    generation_id: str,
    reason: str,
    detail: str = "",
) -> None:
    """Settle a diversity rejection on its already-persisted experiment."""

    from zicato.epoch import update_experiment_outcome  # noqa: PLC0415

    full_reason = f"{reason}: {detail}" if detail else reason
    with best_effort(f"persist soft-reject outcome for {generation_id}"):
        update_experiment_outcome(
            prepared.workspace_root,
            prepared.epoch_id,
            generation_id,
            OutcomeRecord(
                ran_at=_now_iso(),
                drift_movements=(),
                pass_rate_delta=0.0,
                drift_loss_delta=0.0,
                scalar_score_delta=0.0,
                tournament_decision=TournamentDecision.REJECTED,
                rejection_reason=full_reason,
            ),
        )
        _ingest_experiment_into_index(
            prepared.workspace_root,
            prepared.epoch_id,
            generation_id,
        )


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
            cross_epoch=prepared.weights.experiment_memory.cross_epoch,
        )
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

    def _emit_status(record: dict[str, Any]) -> None:
        if on_status is None:
            return
        with best_effort(
            "candidate-batch status publish",
            on_error=lambda exc: log.debug("candidate status publish skipped: %s", exc),
        ):
            on_status(record)

    for offset in range(requested_size):
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

        attempt: CandidateAttempt = await produce_one(
            workspace_root=workspace_root,
            epoch_id=epoch_id,
            parent_id=parent_id,
            next_id=next_id,
            mutations=list(prepared.mutations),
            patterns=list(prepared.patterns),
            brief=prepared.brief,
            loss_summary=prepared.loss_summary,
            auxiliary_call_llm=prepared.config.effective_proposer_call_llm(),
            auxiliary_model=(
                prepared.config.proposer_model
                or str(prepared.workspace_config.get("auxiliary_model", ""))
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
            metric_priorities=prepared.metric_priorities,
            process_exemplars=prepared.process_exemplars,
            genealogy=prepared.genealogy,
            calibration=prepared.calibration,
            on_status=on_status,
            round_emitter=prepared.round_log,
            screen_candidates=prepared.screen_candidates,
            recombine_pair=_recombine_pair_for_slot(prepared.recombine_pair, offset),
            resume_experiment=resume_experiment,
        )
        challenger = attempt.challenger
        status = attempt.status
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

        mint = _mint_challenger_field(
            challenger.experiment,
            sibling_signatures,
            accepted_mutation_sets,
            diversity_tolerance,
        )
        if mint.action == "reject_duplicate":
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
                next_id,
                "field_diversity_duplicate",
                "duplicates an in-flight sibling (same mutation ids + core idea)",
            )
        elif mint.action == "reject_overlap":
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
                next_id,
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
