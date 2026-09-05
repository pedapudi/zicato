"""Assemble the candidate field one round's tournament runs over.

The round pipeline's propose-and-apply phase.  It publishes each candidate
slot to the dashboard as the slot forms, requests the strategy's declared
field width from :func:`produce_candidate_batch`, settles the two ways a
field can fail to yield a runnable tournament, and appends the optional
random-baseline placebo arm.  It returns either the field the tournament
will run over or the round's terminal outcome, already persisted.

Field width one — the gauntlet — differs from a wider field in two rules,
both of them stated here:

* a single slot that exhausted its proposer retries settles as a
  validation-rejection round rather than an all-failed field, because there
  is a proposer error to record and no sibling to compare it against;
* the random-baseline placebo arm rides INSIDE a wider field's slate, and
  runs as a separate duel after settlement when the field holds one
  challenger (:func:`zicato.evolve.propose_apply
  ._maybe_run_placebo_arm_gauntlet`), because a one-slot slate has no room
  for it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from functools import partial
from typing import Any

from zicato.core.types import Generation
from zicato.evolve import generation_phase
from zicato.evolve.candidate_batch import CandidateRejection, produce_candidate_batch
from zicato.evolve.dashboard_projection import (
    _field_entries,
    _publish_active_tournament,
)
from zicato.evolve.decision_support import _field_failure_summary
from zicato.evolve.generation_phase import FieldRound
from zicato.evolve.lifecycle_services import _now_iso
from zicato.evolve.persist import (
    _persist_rejected_round,
    _rejected_proposer_experiment,
    deferred_infra_proposer_outage,
    infrastructure_outage,
)
from zicato.evolve.propose_apply import (
    _AppliedChallenger,
    _mint_placebo_challenger,
    _trim_reason,
)
from zicato.evolve.round_api import EvolveRoundOutcome
from zicato.util import best_effort
from zicato.workspace import generation_round_number

log = logging.getLogger("zicato.orchestrator")


@dataclass(slots=True)
class _ProposingSlots:
    """The live per-slot status of a candidate field while it is forming."""

    tournament_id: str = ""
    status_by_generation: dict[str, dict[str, Any]] = dataclass_field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CandidateField:
    """The applied challengers one round's tournament runs over.

    ``challengers`` are the slots that proposed, validated, and applied
    cleanly, in field order, including the random-baseline placebo arm when
    the cadence fielded one.  ``field_status`` carries one record per
    attempted slot, applied and rejected alike, for the dashboard's
    proposing tracker.  ``competitors`` and ``tournament_id`` are the
    envelope identity every projection of this round is written under, and
    ``first_challenger_id`` keys the durable field-tournament record so a
    multi-round epoch keeps one snapshot per round.
    """

    challengers: tuple[_AppliedChallenger, ...]
    field_status: list[dict[str, Any]]
    base_generation_id: str
    by_id: dict[str, _AppliedChallenger]
    champion: Generation
    diff_sizes: dict[str, dict[str, int]]
    resume_cache: bool
    competitors: list[dict[str, Any]]
    tournament_id: str
    first_challenger_id: str

    def generation(self, generation_id: str) -> Generation:
        """Resolve a competitor id to the generation record a duel loads."""

        if generation_id == self.champion.id:
            return self.champion
        return self.by_id[generation_id].generation


def _publish_proposing_field(
    field_round: FieldRound,
    *,
    tournament_id: str,
    field_status: list[dict[str, Any]],
) -> None:
    """Publish the round envelope while the candidate field is still forming.

    The field has no bracket yet, so the envelope carries the champion alone
    as its competitor set and every slot's status beside it.  That is what
    lets the dashboard show each slot before the tournament starts, and what
    lets a field where nothing applied read as "N proposed, 0 applied"
    rather than as an empty idle state.
    """

    from zicato.runtime.state import TournamentPhase  # noqa: PLC0415

    champion_only = [{"generation_id": field_round.parent_id, "seed": 1, "role": "champion"}]
    _publish_active_tournament(
        field_round.workspace_root,
        tournament_id=tournament_id,
        epoch_id=field_round.epoch_id,
        structure=field_round.tournament_spec.structure,
        structure_params=dict(field_round.tournament_spec.params),
        competitors=champion_only,
        round_index=field_round.round_index,
        total_rounds=field_round.total_rounds,
        field_status=field_status,
        phase=TournamentPhase.PROPOSING,
        entries=_field_entries(champion_only),
    )


def _publish_proposing_slot(
    field_round: FieldRound,
    slots: _ProposingSlots,
    record: dict[str, Any],
) -> None:
    """Republish the forming field with one candidate slot's latest status."""

    generation_id = str(record.get("generation_id", ""))
    if not slots.tournament_id:
        slots.tournament_id = f"tourn_{field_round.epoch_id}_{generation_id}"
    slots.status_by_generation[generation_id] = dict(record)
    _publish_proposing_field(
        field_round,
        tournament_id=slots.tournament_id,
        field_status=list(slots.status_by_generation.values()),
    )


def _transport_only_field_trail(
    rejections: tuple[CandidateRejection, ...],
) -> list[str] | None:
    """The field's whole attempt trail when EVERY slot died on the transport.

    ``None`` — the round is about its proposals and must keep rejecting —
    whenever any slot leaves evidence that is not a transport failure. Two
    shapes qualify: a slot the endpoint answered, whose attempt trail names a
    parse or post-apply-validation failure the round can judge; and a slot
    that carries no :class:`~zicato.proposer.proposer.ProposerError` at all,
    which failed before it accumulated an attempt trail and so says nothing
    about the endpoint either way.

    Returning the trail rather than a bare flag is what lets the deferral
    quote the endpoint's own words: nothing else survives the round, because
    no experiment is written.
    """
    from zicato.proposer.proposer import ProposerError  # noqa: PLC0415

    trail: list[str] = []
    for rejection in rejections:
        error = rejection.proposer_error
        if not isinstance(error, ProposerError):
            return None
        trail.extend(error.attempts)
    return trail if infrastructure_outage(trail) else None


async def _settle_field_that_produced_nothing(
    field_round: FieldRound,
    *,
    base_id: str,
    rejections: tuple[CandidateRejection, ...],
    field_status: list[dict[str, Any]],
) -> EvolveRoundOutcome:
    """Settle a round whose every candidate slot failed to apply.

    A field whose whole attempt trail is transport failures did not measure
    anything, so it DEFERS rather than rejects (see
    :func:`_transport_only_field_trail`).  That is checked first and for
    every field shape, because the harm is the same at any size: a rejection
    the endpoint caused counts toward the consecutive-rejection breaker and
    reads back as evidence about the proposer.

    Otherwise a single slot that exhausted its proposer retries carries a
    :class:`~zicato.proposer.proposer.ProposerError` with its attempt trail,
    so it settles through the shared validation-rejection tail and keeps that
    trail.  Every other exhausted field records a rejection-shaped outcome
    directly, so the round still produces a clean return value and the loop
    continues.
    """

    parent_id = field_round.parent_id
    transport_trail = _transport_only_field_trail(rejections)
    if transport_trail is not None:
        # Publish the slots first: the deferral writes no experiment and no
        # generation, so the forming field is the only surface that shows
        # what each slot hit.
        _publish_proposing_field(
            field_round,
            tournament_id=f"tourn_{field_round.epoch_id}_{base_id}",
            field_status=field_status,
        )
        return deferred_infra_proposer_outage(
            workspace_root=field_round.workspace_root,
            epoch_id=field_round.epoch_id,
            parent_id=parent_id,
            next_id=base_id,
            attempts=transport_trail,
            round_index=field_round.round_index,
            beater=field_round.beater,
            round_log=field_round.round_log,
        )
    if field_round.field_size == 1 and rejections:
        from zicato.proposer.proposer import ProposerError  # noqa: PLC0415

        rejection = rejections[0]
        if isinstance(rejection.proposer_error, ProposerError):
            error = rejection.proposer_error
            rejected_experiment = _rejected_proposer_experiment(
                field_round.epoch_id,
                parent_id,
                base_id,
                error,
            )
            return await _persist_rejected_round(
                workspace_root=field_round.workspace_root,
                epoch_id=field_round.epoch_id,
                parent_id=parent_id,
                next_id=base_id,
                experiment=rejected_experiment,
                validation_errors=list(error.attempts),
                proposer_retries_exhausted=True,
                board=field_round.board,
                round_index=field_round.round_index,
                evaluation_call_llm=field_round.evaluation_call_llm,
                evaluation_model=field_round.evaluation_model,
                beater=field_round.beater,
                round_log=field_round.round_log,
            )
    # Still persist the field-status so the dashboard's proposing-step
    # tracker reads "N proposed · 0 applied — all rejected" rather than an
    # empty idle state.
    #
    # The reason folds in the per-slot failures, so a field where every slot
    # hit the same parse error (a broken proposer prompt) reads differently
    # from one where each slot hit a different error (an unreachable mutable
    # surface). The journal keeps this string verbatim, so a distinction
    # dropped here is unrecoverable (issue #129).
    breakdown = _field_failure_summary(field_status)
    all_failed_reason = "multi-challenger field: no challenger applied cleanly"
    if breakdown:
        all_failed_reason += f" ({breakdown})"
    _publish_proposing_field(
        field_round,
        tournament_id=f"tourn_{field_round.epoch_id}_{base_id}",
        field_status=field_status,
    )
    # The round's terminal decision + close — the per-challenger
    # proposal_attempted failures were emitted as they settled.
    field_round.round_log.emit(
        "decision_recorded",
        {
            "decision": "rejected",
            "provenance": {
                "structure": field_round.tournament_spec.structure,
                "reason": all_failed_reason,
                "parent_generation_id": parent_id,
                "promoted_generation_id": None,
            },
        },
    )
    field_round.round_log.emit("round_closed")
    return EvolveRoundOutcome(
        parent_generation_id=parent_id,
        proposed_generation_id="",
        tournament_decision="rejected",
        rejection_reason=all_failed_reason,
        parent_scalar=0.0,
        child_scalar=0.0,
        delta_scalar=0.0,
    )


def _append_placebo_arm(
    field_round: FieldRound,
    slots: _ProposingSlots,
    *,
    applied: list[_AppliedChallenger],
    field_status: list[dict[str, Any]],
    base_n: int | None,
) -> None:
    """Field one extra slot as the random-baseline placebo arm when due.

    OVERFITTING.md §12 #7.  Every Nth epoch-cumulative round a wider field
    carries ONE extra slot: a semantics-preserving no-op child of the
    champion, marked in its hypothesis as the baseline arm
    (:mod:`zicato.evolve.placebo`).  It flows through the unchanged strategy
    and gate like any challenger; the gate must reject it, and a promoted
    placebo raises the CRITICAL ``placebo_promoted`` loop-health finding.

    Appended AFTER the all-failed settlement, so a fully-failed proposer
    field keeps its rejection outcome, and appended LAST, so sibling
    diversity, ``first_challenger_id``, and the real challengers' ids are
    untouched.  Best-effort: a placebo mint failure narrows the field back to
    the real challengers.
    """

    every_n = int(getattr(field_round.weights.overfitting, "random_baseline_every_n", 0))
    if field_round.field_size <= 1 or base_n is None or not field_round.mutations:
        return

    from zicato.evolve.placebo import placebo_round_due  # noqa: PLC0415

    if not placebo_round_due(every_n, base_n):
        return
    placebo_id = f"v{base_n + field_round.field_size}"
    with best_effort(
        "random-baseline placebo mint",
        on_error=lambda exc: log.warning("random-baseline placebo skipped: %s", exc),
    ):
        placebo = _mint_placebo_challenger(
            workspace_root=field_round.workspace_root,
            epoch_id=field_round.epoch_id,
            parent_id=field_round.parent_id,
            next_id=placebo_id,
            point=field_round.mutations[0],
            round_index=field_round.round_index,
        )
        applied.append(placebo)
        placebo_status = {
            "generation_id": placebo.generation_id,
            "status": "applied",
            "reason": "random_baseline",
            "attempts": 1,
            "attempt_reasons": [],
            "hypothesis": _trim_reason(placebo.experiment.hypothesis.core_idea),
            "seed": field_round.field_size + 2,
        }
        field_status.append(placebo_status)
        _publish_proposing_slot(field_round, slots, placebo_status)
        log.info(
            "multi-challenger field: %s/%s fielded as the random-baseline "
            "placebo arm (cadence every_n=%d, round %d) — the gate must "
            "reject it",
            field_round.epoch_id,
            placebo.generation_id,
            every_n,
            base_n,
        )


async def assemble_candidate_field(
    field_round: FieldRound,
    *,
    resume_plan: Any,
    produce_one: Any,
) -> CandidateField | EvolveRoundOutcome:
    """Produce the round's applied challengers, or its terminal outcome.

    Returns a :class:`CandidateField` when at least one challenger applied.
    A field that produced none is terminal: the outcome returned is already
    persisted and the round is over, so the caller returns it unchanged.

    ``produce_one`` is the per-slot propose-and-apply callable, passed in by
    the facade so the integration suite's monkeypatch anchor on
    :mod:`zicato.evolve.field` still reaches this call site.
    """

    from zicato.scoring.diff_complexity import diff_size  # noqa: PLC0415

    slots = _ProposingSlots()
    candidate_batch = await produce_candidate_batch(
        field_round.prepared,
        field_round.field_size,
        resume_plan=resume_plan,
        on_status=partial(_publish_proposing_slot, field_round, slots),
        produce_one=produce_one,
    )
    applied = list(candidate_batch.challengers)
    field_status = list(candidate_batch.field_status)
    base_id = candidate_batch.base_generation_id
    base_n = generation_round_number(base_id)

    if not applied:
        return await _settle_field_that_produced_nothing(
            field_round,
            base_id=base_id,
            rejections=candidate_batch.rejections,
            field_status=field_status,
        )

    _append_placebo_arm(
        field_round,
        slots,
        applied=applied,
        field_status=field_status,
        base_n=base_n,
    )

    champion = Generation(
        id=field_round.parent_id,
        epoch_id=field_round.epoch_id,
        parent_id=None,
        snapshot_root=generation_phase.snapshot_root(
            field_round.workspace_root, field_round.epoch_id, field_round.parent_id
        ),
        created_at=_now_iso(),
        promoted=True,
    )
    parent_mutation_text = {point.id: point.content for point in field_round.mutations}
    competitors = [{"generation_id": field_round.parent_id, "seed": 1, "role": "champion"}] + [
        {"generation_id": c.generation_id, "seed": i + 2, "role": "challenger"}
        for i, c in enumerate(applied)
    ]
    return CandidateField(
        challengers=tuple(applied),
        field_status=field_status,
        base_generation_id=base_id,
        by_id={c.generation_id: c for c in applied},
        champion=champion,
        diff_sizes={
            challenger.generation_id: diff_size(challenger.experiment, parent_mutation_text)
            for challenger in applied
        },
        resume_cache=bool(candidate_batch.resumed_generation_ids),
        competitors=competitors,
        tournament_id=f"tourn_{field_round.epoch_id}_{applied[0].generation_id}",
        first_challenger_id=applied[0].generation_id,
    )


__all__ = ["CandidateField", "assemble_candidate_field"]
