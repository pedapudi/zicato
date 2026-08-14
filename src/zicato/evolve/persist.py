"""Round-pipeline **persist** stage — the terminal write funnel + round tail.

Split out of :mod:`zicato.orchestrator` as part of the Finding-2 typed
round-pipeline decomposition (``docs/design/REIMPLEMENTATION.md``). This is the
pipeline's *persist* seam: the one write funnel every round's terminal outcome
flows through (:func:`_finalize_generation`), the shared end-of-round tail
(:func:`_round_epilogue`: loop-health + analyzer + epoch report), the
validation-reject tail (:func:`_persist_rejected_round`), and the two synthetic
round-outcome builders for the reject/skip paths.

These five helpers are internal to the evolve loop — nothing outside
:mod:`zicato.orchestrator` referenced them — so this is a pure relocation. The
orchestrator re-imports every name, so its internal call sites (``evolve_once``,
``_evolve_multi_challenger``, the placebo arm) keep resolving unchanged.

Back-edges into the orchestrator that are NOT part of this stage are resolved
through the orchestrator module object at CALL time (the established
``zicato.evolve.*`` idiom): the generation-head writer,
holdout test, so it must be re-read on each call), the health-assessment
helpers, ``_regenerate_epoch_report``, the generation-number helper, and the
``EvolveRoundOutcome`` public dataclass still defined in the orchestrator. The
module logger keeps the ``zicato.orchestrator`` name so records stay
byte-identical.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from zicato.core.types import (
    Experiment,
    Generation,
    OutcomeRecord,
    TournamentDecision,
)
from zicato.evolve.ingest import _ingest_experiment_into_index
from zicato.evolve.lifecycle_services import _beat, _now_iso
from zicato.runtime.heartbeat import HeartbeatBeater
from zicato.util import best_effort

if TYPE_CHECKING:
    from zicato.orchestrator import CallLLM, EvolveRoundOutcome, _RoundLogEmitter
    from zicato.proposer.proposer import ProposerError

log = logging.getLogger("zicato.orchestrator")


def _finalize_generation(
    *,
    workspace_root: Path,
    epoch_id: str,
    generation_id: str,
    outcome: OutcomeRecord,
    lineage_generation: Generation | None = None,
    lineage_parent_id: str | None = None,
    lineage_parent_scalar: float | None = None,
    lineage_child_scalar: float | None = None,
    advance_current_generation: bool = False,
    journal: bool = True,
) -> Experiment:
    """Persist one generation's terminal outcome through every store.

    The ONE write pipeline every round tail flows through:

    1. ``update_experiment_outcome`` — the :class:`OutcomeRecord` lands on
       ``experiment.json`` (the canonical record; a field present on the
       record can no longer be dropped by one tail's hand-rolled copy);
    2. live SQLite index dual-write (best-effort, never aborts the round);
    3. optional lineage upsert (``lineage_generation`` — ``None`` for a
       validation-rejected round that never entered lineage, and for the
       multi-challenger loop which defers lineage until after its crowning
       invariant checks). The settle-time facts ride along: the outcome's
       own ``rejection_reason`` and the duel's two scalars
       (``lineage_parent_scalar`` / ``lineage_child_scalar``, ``None``
       when the caller has no measurement in scope) land on the lineage
       node so the DAG says WHY without a per-generation join against
       ``experiment.json`` (issue #124);
    4. optional champion-marker advance (``advance_current_generation`` —
       the gauntlet's on-promotion step, sequenced between lineage and
       journal exactly as the inline tail wrote them);
    5. optional journal append (``journal=False`` lets the multi-challenger
       path keep its all-outcomes-then-all-journals order).

    Returns the finalised :class:`Experiment` for the caller to journal /
    summarise.
    """
    from zicato.epoch import (  # noqa: PLC0415
        append_journal_entry,
        append_to_lineage,
        update_experiment_outcome,
    )
    from zicato.evolve.generation_phase import set_current_generation  # noqa: PLC0415

    finalised = update_experiment_outcome(workspace_root, epoch_id, generation_id, outcome)
    # Live index dual-write: experiment.json now carries the outcome —
    # refresh the SQLite analytical index entry for it.
    _ingest_experiment_into_index(workspace_root, epoch_id, generation_id)
    if lineage_generation is not None:
        append_to_lineage(
            workspace_root,
            epoch_id,
            lineage_generation,
            parent_id=lineage_parent_id,
            # ``append_to_lineage`` persists the reason only on a settled
            # rejection, so handing it the outcome's reason unconditionally
            # is safe on the promoted path too.
            rejection_reason=outcome.rejection_reason,
            parent_scalar=lineage_parent_scalar,
            child_scalar=lineage_child_scalar,
        )
    if advance_current_generation:
        set_current_generation(workspace_root, epoch_id, generation_id)
    if journal:
        append_journal_entry(workspace_root, epoch_id, finalised)
    return finalised


async def _round_epilogue(
    *,
    workspace_root: Path,
    epoch_id: str,
    board: list[Any],
    round_n: int,
    analyzer_round: int | None,
    mutations: list[Any],
    auxiliary_call_llm: CallLLM,
    auxiliary_model: str,
    meta_loop_emitter: Any,
    run_analyzer: bool = True,
    token_clip: tuple[int, int] | None = None,
    attributable_regressions: dict[str, dict[str, Any]] | None = None,
    on_promote_failure: tuple[str, str, str] | None = None,
) -> tuple[str, bool]:
    """The shared end-of-round tail: loop-health + analyzer + epoch report.

    Near-verbatim duplicated across the gauntlet and multi-challenger paths
    before extraction; now both call here so a new epilogue step can never
    land on one pipeline only. Every step is best-effort by contract:

    * per-round loop-health assessment persisted to
      ``epochs/{epoch}/health/round_{round_n}.json``, with the CRITICAL
      no-signal stderr WARNING;
    * the decision-telemetry analyzer (writes ``insights/round_{N}.md``
      for the next round's proposer) — skipped on the gauntlet's
      validation-reject tail (``run_analyzer=False``), which historically
      never ran it;
    * the comprehensive epoch analysis report regeneration.

    ``token_clip`` — the round's ``(tokens_spent, max_tokens_per_round)``
    pair when the per-round token budget clipped it
    (:func:`_token_clip_state`) — is threaded into the health assessment;
    ``None`` (every unclipped round) is inert. ``attributable_regressions``
    — the per-entry evidence behind a PROMOTED duel's
    :attr:`~zicato.tournament.gate.GateOutcome.attributable_regressions` — is
    threaded the same way and is inert on every other round.
    ``on_promote_failure`` — the ``(adapter_name, generation_id,
    exception_type)`` triple :func:`zicato.evolve.promote_hook.fire_on_promote`
    returns when the round's promotion fired an adapter hook that raised or
    timed out — rides the same rail; ``None`` (every round with no hook, and
    every successful one) is inert.

    Returns ``(health_summary, health_critical)`` for the round outcome.
    """
    from zicato.orchestrator import (  # noqa: PLC0415
        _assess_and_persist_loop_health,
        _regenerate_epoch_report,
        _warn_loop_no_signal,
    )

    health_summary, health_critical = _assess_and_persist_loop_health(
        workspace_root,
        epoch_id,
        round_n,
        board,
        token_clip=token_clip,
        attributable_regressions=attributable_regressions,
        on_promote_failure=on_promote_failure,
    )
    if health_critical:
        _warn_loop_no_signal(epoch_id, round_n, health_summary)

    if run_analyzer:
        with best_effort(
            "decision telemetry analyzer",
            on_error=lambda exc: log.debug("decision telemetry analyzer skipped: %s", exc),
        ):
            from zicato.analyzer import analyze_epoch_telemetry  # noqa: PLC0415

            await analyze_epoch_telemetry(
                workspace_root,
                epoch_id,
                auxiliary_call_llm,
                model=auxiliary_model,
                round_n=analyzer_round,
                # Ground the insight prompt in the agent's REAL mutation
                # surface so the LLM's "Suggested next mutations" section
                # cannot hallucinate mutation target ids that do not exist.
                mutation_ids=[m.id for m in mutations],
                meta_loop_emitter=meta_loop_emitter,
            )

    await _regenerate_epoch_report(workspace_root, epoch_id, auxiliary_call_llm, auxiliary_model)
    return health_summary, health_critical


async def _persist_rejected_round(
    *,
    workspace_root: Path,
    epoch_id: str,
    parent_id: str,
    next_id: str,
    experiment: Experiment,
    validation_errors: list[str],
    proposer_retries_exhausted: bool,
    board: list[Any],
    round_index: int,
    auxiliary_call_llm: CallLLM,
    auxiliary_model: str,
    beater: HeartbeatBeater | None,
    round_log: _RoundLogEmitter | None = None,
) -> EvolveRoundOutcome:
    """Persist a gauntlet round rejected before its tournament ever ran.

    The validation-reject tail: the experiment is written with a rejected
    :class:`OutcomeRecord` describing the validator findings, folded through
    the shared :func:`_finalize_generation` pipeline (no lineage entry — the
    generation never earned one), and the shared :func:`_round_epilogue`
    still runs (minus the analyzer, which this tail historically skipped) so
    a stuck loop surfaces on the dashboard even on early rejections.

    Two distinct symbolic reasons: ``validation_failed`` when a single
    applied patch set failed post-apply validation;
    ``proposer_retries_exhausted`` when the proposer could not produce a
    patch set that survives validation within its bounded retry budget.
    """
    from zicato.epoch import write_experiment  # noqa: PLC0415
    from zicato.evolve.generation_phase import round_number  # noqa: PLC0415
    from zicato.orchestrator import EvolveRoundOutcome  # noqa: PLC0415
    from zicato.runtime import progress_log  # noqa: PLC0415

    write_experiment(workspace_root, epoch_id, next_id, experiment)
    if proposer_retries_exhausted:
        rejection_reason = "proposer_retries_exhausted: " + "; ".join(validation_errors)
    else:
        rejection_reason = "validation_failed: " + "; ".join(validation_errors)
    rejected_outcome = OutcomeRecord(
        ran_at=_now_iso(),
        drift_movements=(),
        pass_rate_delta=0.0,
        drift_loss_delta=0.0,
        scalar_score_delta=0.0,
        tournament_decision=TournamentDecision.REJECTED,
        rejection_reason=rejection_reason,
    )
    _finalize_generation(
        workspace_root=workspace_root,
        epoch_id=epoch_id,
        generation_id=next_id,
        outcome=rejected_outcome,
    )
    # WS8: the validator findings, the terminal decision, and the close.
    if round_log is not None:
        round_log.emit("validation_failed", {"findings": tuple(validation_errors)})
        round_log.emit(
            "decision_recorded",
            {
                "decision": "rejected",
                "provenance": {
                    "reason": rejected_outcome.rejection_reason,
                    "parent_generation_id": parent_id,
                    "promoted_generation_id": None,
                },
            },
        )
    health_summary, health_critical = await _round_epilogue(
        workspace_root=workspace_root,
        epoch_id=epoch_id,
        board=board,
        round_n=round_number(next_id) or round_index,
        analyzer_round=None,
        mutations=[],
        auxiliary_call_llm=auxiliary_call_llm,
        auxiliary_model=auxiliary_model,
        meta_loop_emitter=None,
        run_analyzer=False,
    )
    _beat(
        beater,
        workspace_root=workspace_root,
        progress=progress_log.REJECT,
        epoch_id=epoch_id,
        generation_id=next_id,
        round_index=round_index,
        phase=f"done:round_{round_index}:{next_id}:rejected",
    )
    if round_log is not None:
        round_log.emit("round_closed")
    return EvolveRoundOutcome(
        parent_generation_id=parent_id,
        proposed_generation_id=next_id,
        tournament_decision="rejected",
        rejection_reason=rejected_outcome.rejection_reason,
        parent_scalar=0.0,
        child_scalar=0.0,
        delta_scalar=0.0,
        health_summary=health_summary,
        health_critical=health_critical,
    )


def _rejected_proposer_experiment(
    epoch_id: str,
    parent_generation_id: str,
    generation_id: str,
    error: ProposerError,
) -> Experiment:
    """Build a placeholder experiment for a proposer that exhausted retries.

    When the proposer cannot produce a patch set that survives post-apply
    validation within its bounded retry budget, there is no real
    :class:`Experiment` to journal — but the round must still leave an
    append-only record. This synthesises a minimal experiment whose
    hypothesis carries the per-attempt failure trail and whose patch
    tuple is empty (nothing was successfully applied). The orchestrator
    stamps the rejected :class:`OutcomeRecord` onto it exactly as it
    does for a validator rejection.
    """

    from zicato.core.types import HypothesisSpec  # noqa: PLC0415

    return Experiment(
        id=f"exp_{epoch_id}_{generation_id}",
        epoch_id=epoch_id,
        generation_id=generation_id,
        parent_generation_id=parent_generation_id,
        proposed_at=_now_iso(),
        hypothesis=HypothesisSpec(
            core_idea="proposer exhausted retries without a valid patch set",
            modulating=(),
            why=(
                "Every proposer attempt this round failed parsing or "
                "post-apply validation; see the rejected outcome for the "
                "per-attempt error trail."
            ),
            expected_drift_movements=(),
            expected_pass_rate_delta="0.0",
            risks="; ".join(error.attempts),
        ),
        patches=(),
        outcome=None,
    )


def _skipped_round_outcome(parent_generation_id: str, reason: str) -> EvolveRoundOutcome:
    """Build the synthetic outcome for a round cut short by ``skip_round``.

    Used when an operator queues the control protocol's ``skip_round`` flag
    (RUNTIME-V2.md Phase 2). The round never proposed or ran a tournament,
    so — exactly like :func:`_budget_aborted_outcome` — we fabricate a
    rejection-style outcome. The ``rejection_reason`` is the symbolic
    ``"skip_round"`` token (plus the operator's reason when given) so journal
    readers and the CLI recognise an operator skip distinctly from a budget
    cut or a real gate rejection.
    """
    from zicato.orchestrator import EvolveRoundOutcome  # noqa: PLC0415

    suffix = f": {reason}" if reason else ""
    return EvolveRoundOutcome(
        parent_generation_id=parent_generation_id,
        proposed_generation_id="",
        tournament_decision="rejected",
        rejection_reason=f"skip_round: operator skipped the round{suffix}",
        parent_scalar=0.0,
        child_scalar=0.0,
        delta_scalar=0.0,
    )
