"""Multi-challenger field round strategy."""

# ruff: noqa: E402
from __future__ import annotations

import asyncio
import logging
import time  # noqa: F401  — kept as the ``orch.time`` clock seam (see __all__)
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from zicato.core.types import (
    Experiment,
    Generation,
    OutcomeRecord,
    PriorExperiment,
    TournamentDecision,
)
from zicato.evolve import generation_phase
from zicato.evolve.dashboard_projection import (
    _clear_active_tournament,
    _field_entries,
    _overlay_projected_live_progress,
    _overlay_projected_standings,
    _persist_field_tournament,
    _publish_active_tournament,
    _serialise_rounds,
    _serialise_standings,
    _settle_active_tournament,
)
from zicato.evolve.gate import (
    _apply_field_overrides,
    _confirm_crowning_on_holdout,
    _first_aggregate_for,
    _integrity_block_reason,
    _registered_mutable_trees,
    _resolve_round_champion_mode,
)
from zicato.evolve.ingest import (
    _cache_gen_score,
    _ingest_experiment_into_index,
    _load_prior_experiments,
)
from zicato.evolve.lifecycle_services import (
    _beat,
    _now_iso,
)
from zicato.evolve.pareto import record_round_frontier
from zicato.evolve.persist import (
    _finalize_generation,
    _round_epilogue,
)
from zicato.evolve.placebo import is_placebo_experiment
from zicato.evolve.promote_hook import fire_on_promote
from zicato.evolve.propose_apply import (
    _AppliedChallenger,
    _diversity_signature,
    _mint_challenger_field,
    _mint_placebo_challenger,
    _propose_and_apply_challenger,
    _trim_reason,
)
from zicato.evolve.round_context import (
    _recombine_pair_for_slot,
)
from zicato.runtime.control_consumer import (
    GateOverride,
    claim_field_gate_overrides,
)
from zicato.runtime.heartbeat import HeartbeatBeater
from zicato.util import best_effort

if TYPE_CHECKING:
    # Annotation-only — the proposer module is imported lazily inside
    # ``evolve_once`` (see the module docstring on lazy imports), so its
    # exception type is referenced here purely for type annotations.
    from zicato.proposer.agent import ProposerAgent
    from zicato.proposer.best_of_n import ScreenRunner

log = logging.getLogger("zicato.orchestrator")

CallLLM = Callable[[str, str, str], Awaitable[str]]

from zicato.evolve.decision_support import (
    _field_failure_summary,
    _generalization_fields_from_scalars,
    _token_clip_state,
)
from zicato.evolve.round_api import EvolveRoundOutcome, _declared_custom_judge_names
from zicato.evolve.round_reporting import (
    _emit_gate_evaluated,
    _emit_harness_loaded,
    _emit_tournament_units,
    _RoundLogEmitter,
)


async def evolve_field_round(
    *,
    workspace_root: Path,
    epoch_id: str,
    tournament_spec: Any,
    strategy: Any,
    parent_id: str,
    adapter: Any,
    board: list[Any],
    weights: Any,
    brief: Any,
    config: Any,
    mutations: list[Any],
    patterns: list[Any],
    loss_summary: str,
    failure_profile: str,
    process_exemplars: str,
    genealogy: tuple[Any, ...] = (),
    calibration: Any = None,
    disable_drift: tuple[Any, ...],
    judge_only: bool,
    fast_mode: bool,
    auxiliary_call_llm: CallLLM,
    workspace_config: Any,
    max_proposer_retries: int,
    beater: HeartbeatBeater | None,
    round_index: int,
    total_rounds: int,
    meta_loop_emitter: Any,
    proposer_agent: ProposerAgent,
    round_log: _RoundLogEmitter | None = None,
    screen_candidates: ScreenRunner | None = None,
    recombine_pair: Any = None,
) -> EvolveRoundOutcome:
    """Run ONE evolve round under a non-gauntlet tournament structure.

    The structure's :meth:`SelectionStrategy.field_size` challengers are
    proposed and applied (each a lineage child of the current champion),
    then the strategy's matchups are driven through
    :func:`zicato.selection.resolve_tournament`. Each matchup runs via the
    same board-unit runner (:func:`zicato.tournament.runner.run_matchup`)
    and ends in the UNCHANGED promote gate; the strategy reads the gate
    verdict and never re-decides a duel. On resolution the crowned
    generation (if any) advances the champion, every rejected challenger
    is recorded as a dead branch, and the live ``ActiveTournament``
    envelope + per-challenger ``OutcomeRecord`` audit + v3 index columns
    are persisted per ``docs/design/TOURNAMENT-DATA-MODEL.md``.

    ``fast_mode`` is the RUNTIME champion-eval knob (the ``--mode fast``
    setting), threaded identically to ``disable_drift`` / ``judge_only``.
    When set, every matchup reuses the champion's cached per-board
    scalars instead of re-running the champion (see
    :func:`zicato.tournament.runner.run_matchup`), so fast mode is
    structure-agnostic — it composes with racing / swiss / elim exactly
    as it does with the gauntlet. The resolved champion-eval mode is
    RECORDED in the journal for provenance (it is never a contract
    input, so flipping fast↔full does not roll the epoch).
    """
    from zicato.board.split import rotation_seed, split_board  # noqa: PLC0415
    from zicato.core.types import MatchOutcome  # noqa: PLC0415
    from zicato.epoch import (  # noqa: PLC0415
        append_journal_entry,
        append_to_lineage,
        update_experiment_outcome,
    )
    from zicato.selection import EvidencePreGate, resolve_tournament  # noqa: PLC0415
    from zicato.selection.dead_letter import (  # noqa: PLC0415
        InconclusiveRecord,
        record_inconclusive,
    )
    from zicato.selection.driver import EvidenceResolution  # noqa: PLC0415
    from zicato.selection.evidence_gate import (  # noqa: PLC0415
        EVIDENCE_REPLICATE_BASE,
        rating_block,
        read_promote_confidence_threshold,
        read_replicate_budget,
    )
    from zicato.selection.strategy import (  # noqa: PLC0415
        Contestant,
        Matchup,
        MatchupResult,
    )
    from zicato.tournament.runner import confirm_crowning_holdout, run_matchup  # noqa: PLC0415

    auxiliary_call_llm = config.effective_proposer_call_llm()
    auxiliary_model = config.proposer_model or str(workspace_config.get("auxiliary_model", ""))
    field_n = strategy.field_size()
    # WS8: a direct caller (tests) may not thread the opened emitter; bind
    # one so every emit below is uniformly best-effort. ``evolve_once`` (the
    # production caller) passes the emitter it already opened the round on.
    if round_log is None:
        round_log = _RoundLogEmitter(workspace_root, epoch_id, round_index)

    # TRAIN/HOLDOUT split (OVERFITTING.md §3/§4). The structure's internal
    # matchups (swiss rounds, elim nodes, racing rungs) — INCLUDING the final
    # champion-gate duel that decides promotion — score on the TRAIN slice
    # only, so the holdout is never consumed to *pick* the leader (mirrors the
    # gauntlet, which selects on train). The full board is retained for the
    # one Ladder-mediated holdout confirmation run after resolution. When the
    # holdout is empty (small board / split disabled / no tagged entry) the
    # train slice IS the full board, so every matchup runs on the full board
    # and the whole path is byte-identical to today's whole-board behaviour.
    _train_seed = rotation_seed(weights.overfitting, epoch_id)
    _train_ids, _holdout_ids = split_board(board, weights.overfitting, seed=_train_seed)
    _train_id_set = set(_train_ids)
    train_board = [e for e in board if e.id in _train_id_set]

    # --- Propose + apply the N-challenger field. Ids are minted in
    # sequence so each challenger is a distinct vN child of the champion;
    # a proposer that fails for one challenger simply narrows the field.
    applied: list[_AppliedChallenger] = []
    # Per-challenger proposing-step outcomes (applied vs rejected + reason),
    # collected in mint order so the dashboard's proposing-step tracker can
    # render the field forming live and post-hoc. Persisted onto the live
    # ActiveTournament envelope (publish + settle) as ``field_status``.
    field_status: list[dict[str, Any]] = []
    # Mint ids monotonically from the highest existing vN so every
    # challenger gets a distinct id even when a proposer attempt fails
    # before it derives a snapshot (so generation_phase.next_generation_id can't re-pick
    # the same vN). The first id matches what the gauntlet path would mint.
    base_id = generation_phase.next_generation_id(workspace_root, epoch_id)
    base_n = generation_phase.round_number(base_id)
    custom_judge_names = _declared_custom_judge_names(board, weights)
    # Experiment memory: the settled cross-round digest, computed ONCE
    # before the field is minted (it does not change as siblings apply).
    # ``siblings`` accumulates an in-flight ``PriorExperiment`` per
    # successfully-applied challenger so challenger k sees the hypotheses
    # of challengers 0..k-1 this round and can diversify away from them; a
    # challenger whose proposer failed contributes no sibling.
    prior = tuple(
        _load_prior_experiments(
            workspace_root,
            epoch_id,
            cross_epoch=weights.experiment_memory.cross_epoch,
        )
    )
    siblings: list[PriorExperiment] = []
    # Field-diversity constraint (FUNCTIONALITY-RECOMMENDATIONS.md §4.3): the
    # signatures of the siblings already minted this round, so a later
    # challenger that duplicates one (same modulating id-set + core idea) is
    # soft-rejected instead of collapsing the field (EXPERIMENT-MEMORY.md
    # §2.2). Localized to this loop; no change to the SelectionStrategy.
    sibling_signatures: list[tuple[frozenset[str], str]] = []
    # Opt-in field-diversity OVERLAP enforcement (FUNCTIONALITY-
    # RECOMMENDATIONS.md §4.3): when ``config.diversity_tolerance`` is set, a
    # challenger whose targeted-mutation-id set overlaps an already-ACCEPTED
    # sibling's by a Jaccard ratio strictly greater than the tolerance is
    # soft-rejected (it would collapse a field of N into fewer real
    # experiments). ``accepted_mutation_sets`` tracks the mutation-id set of
    # each kept challenger in mint order so the overlap is measured against
    # exactly the slate that will actually run. ``None`` ⇒ enforcement OFF,
    # and every field-status record below stays byte-identical to today (no
    # ``diversity_status`` key is emitted). Enforcement composes with — and
    # runs AFTER — the exact-duplicate soft-reject above.
    diversity_tolerance = getattr(config, "diversity_tolerance", None)
    accepted_mutation_sets: list[frozenset[str]] = []
    diversity_soft_rejected = 0
    # The LIVE field-status map, keyed by generation_id, rewritten as each
    # challenger slot enters ("proposing"), retries, and settles
    # ("applied"/"rejected"). The orchestrator publishes a "proposing"-phase
    # ActiveTournament envelope on every status transition so the dashboard's
    # proposing tracker updates as each challenger is attempted — not only
    # after the whole field is minted. (OBSERVABILITY: the operator's "most
    # of what happens during proposal phase is opaque" pain.)
    live_status: dict[str, dict[str, Any]] = {}
    proposing_tournament_id = f"tourn_{epoch_id}_{base_id}"

    from zicato.runtime.state import TournamentPhase  # noqa: PLC0415

    def _publish_proposing(record: dict[str, Any]) -> None:
        live_status[str(record.get("generation_id", ""))] = dict(record)
        ordered = list(live_status.values())
        champion_only = [{"generation_id": parent_id, "seed": 1, "role": "champion"}]
        _publish_active_tournament(
            workspace_root,
            tournament_id=proposing_tournament_id,
            epoch_id=epoch_id,
            structure=tournament_spec.structure,
            structure_params=dict(tournament_spec.params),
            competitors=champion_only,
            round_index=round_index,
            total_rounds=total_rounds,
            field_status=ordered,
            phase=TournamentPhase.PROPOSING,
            entries=_field_entries(champion_only),
        )

    def _persist_soft_reject(generation_id: str, reason: str, detail: str = "") -> None:
        # A field-diversity soft-reject drops the challenger from the run slate
        # during proposing. Persist a terminal REJECTED outcome onto its
        # ``experiment.json`` (written at proposal with ``outcome=None``) so the
        # canonical generation record — and the round-grouped lineage tree that
        # reads it — show "rejected" consistently with the live proposed-field
        # hero, instead of a stale "pending". The ``detail`` (the overlap ratio +
        # the peer sibling + the tolerance, or the duplicate explanation) is
        # folded into ``rejection_reason`` so the candidate documents WHY it was
        # cut — not just the bare "field_diversity_overlap" code. Mirrors the
        # validator path's ``"<code>: <explanation>"`` reason shape. Best-effort:
        # a missing / locked record must never abort proposing the rest of field.
        full_reason = f"{reason}: {detail}" if detail else reason
        with best_effort(f"persist soft-reject outcome for {generation_id}"):
            update_experiment_outcome(
                workspace_root,
                epoch_id,
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
            _ingest_experiment_into_index(workspace_root, epoch_id, generation_id)

    for offset in range(field_n):
        next_id = f"v{base_n + offset}" if base_n is not None else base_id
        # Seed mirrors competitors_meta: champion is seed 1, challengers
        # follow in mint order (seed 2, 3, …) regardless of apply outcome.
        challenger, status = await _propose_and_apply_challenger(
            workspace_root=workspace_root,
            epoch_id=epoch_id,
            parent_id=parent_id,
            next_id=next_id,
            mutations=mutations,
            patterns=patterns,
            brief=brief,
            loss_summary=loss_summary,
            auxiliary_call_llm=auxiliary_call_llm,
            auxiliary_model=auxiliary_model,
            max_proposer_retries=max_proposer_retries,
            beater=beater,
            round_index=round_index,
            meta_loop_emitter=meta_loop_emitter,
            seed=offset + 2,
            custom_judge_names=custom_judge_names,
            prior_experiments=prior + tuple(siblings),
            proposer_agent=proposer_agent,
            restrict_visibility=weights.overfitting.restrict_proposer_visibility,
            failure_profile=failure_profile,
            process_exemplars=process_exemplars,
            genealogy=genealogy,
            calibration=calibration,
            on_status=_publish_proposing,
            round_emitter=round_log,
            screen_candidates=screen_candidates,
            recombine_pair=_recombine_pair_for_slot(recombine_pair, offset),
        )
        # Field-diversity DECISION (pure — `_mint_challenger_field`): accept
        # the challenger into the run slate, or soft-reject it as an exact
        # duplicate of an in-flight sibling / an over-tolerance overlap with
        # an accepted sibling. The persistence + publish I/O for a rejected
        # slot stays here, separated from the decision.
        mint = (
            _mint_challenger_field(
                challenger.experiment,
                sibling_signatures,
                accepted_mutation_sets,
                diversity_tolerance,
            )
            if challenger is not None
            else None
        )
        if challenger is not None and mint is not None and mint.action == "reject_duplicate":
            # Field-diversity soft-reject: this challenger duplicates an
            # already-minted sibling (same modulating id-set + core idea), so
            # it would collapse the field. Drop it from the run slate and
            # record a legible rejected field-status — the SelectionStrategy
            # still resolves over the distinct challengers that remain.
            hyp = challenger.experiment.hypothesis
            dup_status = {
                "generation_id": challenger.generation_id,
                "status": "rejected",
                "reason": "field_diversity_duplicate",
                "attempts": int(status.get("attempts", 0)) if isinstance(status, dict) else 0,
                "attempt_reasons": [
                    "duplicates an in-flight sibling (same modulating ids + core idea); "
                    "soft-rejected to keep the field diverse"
                ],
                "hypothesis": _trim_reason(hyp.core_idea),
                "seed": offset + 2,
            }
            # The per-slot diversity status is only stamped when overlap
            # enforcement is active, so the default-off path's duplicate record
            # is byte-identical to today.
            if diversity_tolerance is not None:
                dup_status["diversity_status"] = "soft_rejected"
                dup_status["diversity_tolerance"] = diversity_tolerance
                diversity_soft_rejected += 1
            field_status.append(dup_status)
            _publish_proposing(dup_status)
            log.info(
                "multi-challenger field: %s/%s duplicates an in-flight sibling "
                "(modulating=%s); soft-rejected for field diversity",
                epoch_id,
                challenger.generation_id,
                tuple(hyp.modulating),
            )
            _persist_soft_reject(
                challenger.generation_id,
                "field_diversity_duplicate",
                "duplicates an in-flight sibling (same mutation ids + core idea)",
            )
            continue
        # Opt-in overlap soft-reject: when a tolerance is configured, drop a
        # challenger whose mutation-id set overlaps an accepted sibling beyond
        # the ceiling. Skipped entirely (no key emitted) when tolerance is
        # None, so the default path is byte-identical.
        if challenger is not None and mint is not None and mint.action == "reject_overlap":
            overlap, peer_idx = mint.overlap, mint.overlap_peer_index
            assert diversity_tolerance is not None  # narrowed: overlap fires only with a tolerance
            hyp = challenger.experiment.hypothesis
            peer_gid = applied[peer_idx].generation_id if 0 <= peer_idx < len(applied) else ""
            overlap_status = {
                "generation_id": challenger.generation_id,
                "status": "rejected",
                "reason": "field_diversity_overlap",
                "diversity_status": "soft_rejected",
                "attempts": (int(status.get("attempts", 0)) if isinstance(status, dict) else 0),
                "attempt_reasons": [
                    f"mutation-id overlap {overlap:.3f} with sibling "
                    f"{peer_gid or '(accepted)'} exceeds diversity_tolerance "
                    f"{diversity_tolerance:.3f}; soft-rejected to keep the field diverse"
                ],
                "hypothesis": _trim_reason(hyp.core_idea),
                "seed": offset + 2,
                "overlap": round(overlap, 6),
                "overlap_peer": peer_gid,
                "diversity_tolerance": diversity_tolerance,
            }
            field_status.append(overlap_status)
            _publish_proposing(overlap_status)
            diversity_soft_rejected += 1
            log.info(
                "multi-challenger field: %s/%s overlaps sibling %s by %.3f "
                "(> tolerance %.3f); soft-rejected for field diversity",
                epoch_id,
                challenger.generation_id,
                peer_gid,
                overlap,
                diversity_tolerance,
            )
            _persist_soft_reject(
                challenger.generation_id,
                "field_diversity_overlap",
                f"overlap {overlap:.3f} with sibling {peer_gid or '(accepted)'} "
                f"exceeds diversity_tolerance {diversity_tolerance:.3f}",
            )
            continue
        if challenger is not None and diversity_tolerance is not None and isinstance(status, dict):
            # Enforcement active and the challenger is kept: stamp the slot
            # ``applied`` so the per-slot diversity status is explicit. Only
            # written when a tolerance is configured, so the default path's
            # field-status records are untouched.
            status = {
                **status,
                "diversity_status": "applied",
                "diversity_tolerance": diversity_tolerance,
            }
        field_status.append(status)
        if challenger is not None:
            applied.append(challenger)
            accepted_mutation_sets.append(frozenset(challenger.experiment.hypothesis.modulating))
            hyp = challenger.experiment.hypothesis
            siblings.append(
                PriorExperiment(
                    generation_id=challenger.generation_id,
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

    if diversity_tolerance is not None and diversity_soft_rejected:
        log.info(
            "multi-challenger field: %s soft-rejected %d challenger(s) for "
            "field-diversity overlap (tolerance %.3f); %d kept",
            epoch_id,
            diversity_soft_rejected,
            diversity_tolerance,
            len(applied),
        )

    if not applied:
        # The whole field failed to apply — nothing to run. Record a
        # rejection-shaped outcome so the round still produces a clean
        # return value and the loop continues. Still persist the
        # field-status so the dashboard's proposing-step tracker reads
        # "N proposed · 0 applied — all rejected" rather than an empty
        # idle state (the recent all-failed run that prompted this).
        #
        # The reason folds in the per-slot failures built just above. The
        # bare "no challenger applied cleanly" was the same sentence
        # whether every slot hit the same parse error (a broken proposer
        # prompt) or each hit a different one (an unreachable mutable
        # surface) — and the journal keeps this string, so the distinction
        # was lost for good (issue #129).
        breakdown = _field_failure_summary(field_status)
        all_failed_reason = "multi-challenger field: no challenger applied cleanly"
        if breakdown:
            all_failed_reason += f" ({breakdown})"
        champion_only = [{"generation_id": parent_id, "seed": 1, "role": "champion"}]
        _publish_active_tournament(
            workspace_root,
            tournament_id=f"tourn_{epoch_id}_{base_id}",
            epoch_id=epoch_id,
            structure=tournament_spec.structure,
            structure_params=dict(tournament_spec.params),
            competitors=champion_only,
            round_index=round_index,
            total_rounds=total_rounds,
            field_status=field_status,
            phase=TournamentPhase.PROPOSING,
            entries=_field_entries(champion_only),
        )
        # WS8: the round's terminal decision + close — the per-challenger
        # proposal_attempted failures were already emitted as they settled.
        round_log.emit(
            "decision_recorded",
            {
                "decision": "rejected",
                "provenance": {
                    "structure": tournament_spec.structure,
                    "reason": all_failed_reason,
                    "parent_generation_id": parent_id,
                    "promoted_generation_id": None,
                },
            },
        )
        round_log.emit("round_closed")
        return EvolveRoundOutcome(
            parent_generation_id=parent_id,
            proposed_generation_id="",
            tournament_decision="rejected",
            rejection_reason=all_failed_reason,
            parent_scalar=0.0,
            child_scalar=0.0,
            delta_scalar=0.0,
        )

    # --- Optional random-baseline placebo arm (OVERFITTING.md #7) --------
    # Every Nth epoch-cumulative round the field carries ONE extra slot: a
    # semantics-preserving no-op child of the champion, hypothesis marked
    # as the baseline arm (zicato.evolve.placebo). It flows through the
    # unchanged strategy + gate like any challenger; the gate must reject
    # it, and a promoted placebo raises the CRITICAL ``placebo_promoted``
    # loop-health finding. Appended AFTER the all-failed early-return above
    # (a fully-failed proposer field keeps its historical outcome) and
    # appended LAST so sibling diversity, ``first_challenger_id``, and the
    # real challengers' ids are untouched. Best-effort: a placebo mint
    # failure narrows the field back to the real challengers.
    _placebo_every_n = int(getattr(weights.overfitting, "random_baseline_every_n", 0))
    if base_n is not None and mutations:
        from zicato.evolve.placebo import placebo_round_due  # noqa: PLC0415

        if placebo_round_due(_placebo_every_n, base_n):
            _placebo_id = f"v{base_n + field_n}"
            with best_effort(
                "random-baseline placebo mint",
                on_error=lambda exc: log.warning("random-baseline placebo skipped: %s", exc),
            ):
                _placebo = _mint_placebo_challenger(
                    workspace_root=workspace_root,
                    epoch_id=epoch_id,
                    parent_id=parent_id,
                    next_id=_placebo_id,
                    point=mutations[0],
                    round_index=round_index,
                )
                applied.append(_placebo)
                _placebo_status = {
                    "generation_id": _placebo.generation_id,
                    "status": "applied",
                    "reason": "random_baseline",
                    "attempts": 1,
                    "attempt_reasons": [],
                    "hypothesis": _trim_reason(_placebo.experiment.hypothesis.core_idea),
                    "seed": field_n + 2,
                }
                field_status.append(_placebo_status)
                _publish_proposing(_placebo_status)
                log.info(
                    "multi-challenger field: %s/%s fielded as the random-baseline "
                    "placebo arm (cadence every_n=%d, round %d) — the gate must "
                    "reject it",
                    epoch_id,
                    _placebo.generation_id,
                    _placebo_every_n,
                    base_n,
                )

    by_id: dict[str, _AppliedChallenger] = {c.generation_id: c for c in applied}
    champion_gen = Generation(
        id=parent_id,
        epoch_id=epoch_id,
        parent_id=None,
        snapshot_root=generation_phase.snapshot_root(workspace_root, epoch_id, parent_id),
        created_at=_now_iso(),
        promoted=True,
    )

    def _generation_for(gid: str) -> Generation:
        if gid == parent_id:
            return champion_gen
        return by_id[gid].generation

    # --- request_field: hand the strategy the champion + applied field.
    async def _request_field(_n: int) -> tuple[Contestant, list[Contestant]]:
        champion = Contestant(generation_id=parent_id, role="champion")
        challengers = [
            Contestant(
                generation_id=c.generation_id,
                role="challenger",
                snapshot_root=c.snapshot_root,
                experiment=c.experiment,
            )
            for c in applied
        ]
        return champion, challengers

    # --- Champion-eval mode provenance. With the cache-first board-unit
    # runner, ``run_matchup`` reports a per-generation cached-vs-fresh
    # tally (``unit_provenance``) over BOTH sides of each duel. The
    # round-level champion-eval mode is attributed to the CHAMPION
    # (``parent_id``) specifically: a challenger-vs-challenger duel runs
    # challengers fresh (they are new generations and MUST run), which
    # says nothing about champion reuse. We therefore accumulate only the
    # champion's tally across every matchup it appears in — a RUNTIME
    # provenance field, never a contract input.
    champion_cached_units = 0
    champion_fresh_units = 0

    # --- Per-generation aggregates for the Pareto frontier record. Filled
    # from the SAME dicts ``_cache_gen_score`` persists and only when
    # ``cache_scores`` is set, so an evidence-gate replicate duel's single
    # draw can never overwrite the round-scored aggregate the record reads
    # (docs/design/PARETO-FRONTIER.md §6).
    field_aggregates: dict[str, dict[str, Any]] = {}

    # --- Cross-matchup concurrency cap. A non-gauntlet structure schedules
    # SEVERAL matchups of a round concurrently (the driver fans the batch out
    # under one ``asyncio.gather``). Without a shared gate each matchup would
    # mint its own ``Semaphore(parallelism)``, so N concurrent matchups could
    # run ``N × parallelism`` board units at once — overshooting the operator's
    # parallelism intent and the LLM endpoint's concurrency. One semaphore,
    # created here per round and handed to every ``run_matchup``, makes the
    # whole round draw from ONE global cap. Sized exactly as a single
    # matchup's would be, so a round with one matchup is unchanged.
    round_unit_semaphore = asyncio.Semaphore(max(1, int(config.parallelism)))

    # --- run_matchup: one duel via the board-unit runner + unchanged gate.
    # ``replicate_base`` / ``cache_scores`` exist for the evidence pre-gate's
    # replicate duels only: a reserved replicate base keeps evidence draws
    # off the canonical cache slots, and ``cache_scores=False`` keeps a
    # single evidence draw's aggregates from overwriting the round-scored
    # ``gen_score.json`` the fast-mode champion reuse reads. Every strategy
    # matchup uses the defaults, byte-identical to before.
    async def _run_matchup(
        m: Matchup, *, replicate_base: int = 0, cache_scores: bool = True
    ) -> MatchupResult:
        _beat(
            beater,
            epoch_id=epoch_id,
            generation_id=m.right.generation_id,
            round_index=round_index,
            phase=f"tournament:round_{round_index}:{m.matchup_id}",
        )
        result = await run_matchup(
            adapter=adapter,
            left_gen=_generation_for(m.left.generation_id),
            right_gen=_generation_for(m.right.generation_id),
            # Internal selection scores on the TRAIN slice only (the holdout
            # is confirmation-only, never used to pick the leader). A racing
            # rung's ``board_subset`` is intersected against the train board
            # inside ``run_matchup``. Empty holdout ⇒ ``train_board`` IS the
            # full board ⇒ byte-identical to today.
            board=train_board,
            weights=weights,
            config=config,
            workspace_root=workspace_root,
            epoch_id=epoch_id,
            board_subset=m.board_subset,
            replicates=m.replicates,
            replicate_base=replicate_base,
            disable_drift=disable_drift,
            judge_only=judge_only,
            fast=fast_mode,
            round_index=round_index,
            total_rounds=total_rounds,
            match_id=m.matchup_id,
            # Opt-in wall-clock cap on this duel's TOTAL board-unit execution.
            # None (the default for every structure that does not set it) keeps
            # the run uncapped, byte-identical to today; a racing rung may pin
            # it to bound a full-board grind (see Matchup.matchup_budget_seconds).
            matchup_budget_seconds=m.matchup_budget_seconds,
            # One shared semaphore across every matchup of this round, so all
            # concurrently-scheduled matchups draw from ONE global concurrency
            # cap rather than each minting its own ``Semaphore(parallelism)``.
            unit_semaphore=round_unit_semaphore,
        )
        # Attribute the CHAMPION's cached-vs-fresh board-unit tally for
        # this matchup (if the champion played in it). ``nonlocal`` so the
        # closure mutates the round-level accumulators.
        nonlocal champion_cached_units, champion_fresh_units
        champ_prov = result.unit_provenance.get(parent_id)
        if champ_prov is not None:
            champion_cached_units += champ_prov.cached
            champion_fresh_units += champ_prov.fresh
        # Cache both sides' aggregates for fast-mode reuse, mirroring the
        # gauntlet path's _cache_gen_score calls. Skipped for evidence
        # replicate duels (``cache_scores=False``): one reserved-slot draw
        # must not overwrite the round-scored aggregates.
        if cache_scores:
            # Every matchup appends its own line to the archive beside the
            # canonical file, so the within-round measurements the last
            # matchup's write shadows are still on disk (issue #122).
            _cache_gen_score(
                workspace_root,
                epoch_id,
                m.left.generation_id,
                result.parent_agg,
                round_index=round_index,
            )
            _cache_gen_score(
                workspace_root,
                epoch_id,
                m.right.generation_id,
                result.child_agg,
                round_index=round_index,
            )
            field_aggregates[m.left.generation_id] = result.parent_agg
            field_aggregates[m.right.generation_id] = result.child_agg
        # WS8: this matchup's board units + gate verdict onto the round log.
        _emit_tournament_units(round_log, result)
        _emit_harness_loaded(round_log, workspace_root, epoch_id, result)
        _emit_gate_evaluated(
            round_log,
            result.outcome,
            parent_agg=result.parent_agg,
            child_agg=result.child_agg,
            weights=weights,
        )
        return MatchupResult(
            matchup_id=m.matchup_id,
            left_id=m.left.generation_id,
            right_id=m.right.generation_id,
            left_agg=result.parent_agg,
            right_agg=result.child_agg,
            outcome=result.outcome,
            stage_index=m.stage_index,
            bracket_slot=m.bracket_slot,
        )

    # --- Publish the live ActiveTournament envelope before scheduling.
    competitors_meta = [{"generation_id": parent_id, "seed": 1, "role": "champion"}] + [
        {"generation_id": c.generation_id, "seed": i + 2, "role": "challenger"}
        for i, c in enumerate(applied)
    ]
    tournament_id = f"tourn_{epoch_id}_{applied[0].generation_id}"
    _publish_active_tournament(
        workspace_root,
        tournament_id=tournament_id,
        epoch_id=epoch_id,
        structure=tournament_spec.structure,
        structure_params=dict(tournament_spec.params),
        competitors=competitors_meta,
        round_index=round_index,
        total_rounds=total_rounds,
        field_status=field_status,
        entries=_field_entries(competitors_meta),
    )

    # Progress transition: the field tournament started executing. One
    # round-level append (NOT per matchup) so the liveness seq advances on
    # genuine progress. Best-effort — never abort the round on a log write.
    from zicato.runtime import progress_log  # noqa: PLC0415

    with best_effort(
        "progress-log field tournament-start",
        on_error=lambda exc: log.debug("progress-log field tournament-start skipped: %s", exc),
    ):
        progress_log.append_progress(workspace_root, progress_log.TOURNAMENT_START)

    # OPEN the durable field-tournament envelope NOW, before the bracket
    # resolves (issue #16). The runtime ``active_tournament`` envelope above
    # is ephemeral (cleared on crash, overwritten next round); only the
    # durable ``tournaments/field-*.json`` record is queryable by the index,
    # ``zicato repair index``, and any external consumer. Opening it here in
    # ``in_progress`` state — with the competitor field + proposing status
    # but no resolved bracket yet — means the in-flight round is visible to
    # EVERY store the moment its challengers are minted, not only at settle.
    # The settle write below upserts this same record (same tournament_id)
    # to ``settled`` with the resolved bracket; the open + settle compose
    # idempotently, so a resume that re-opens an existing in_progress record
    # neither duplicates nor corrupts it.
    first_challenger_id = applied[0].generation_id
    _persist_field_tournament(
        workspace_root,
        field_tournament_id=f"{epoch_id}:field:{first_challenger_id}",
        first_challenger_id=first_challenger_id,
        epoch_id=epoch_id,
        structure=tournament_spec.structure,
        structure_params=dict(tournament_spec.params),
        competitors=competitors_meta,
        rounds=[],
        standings=[],
        field_status=field_status or [],
        decision=None,
        state="in_progress",
    )

    # --- Live structure publish. Each time the driver schedules a batch
    # of pending matchups, republish the envelope with the LIVE structure:
    # the settled rounds plus the in-flight round (matches with
    # ``winner: null`` + ``pending: true``) and the standings-so-far. This
    # is what lets the dashboard's bracket/ladder/funnel exist DURING the
    # run instead of showing "being seeded" until settle. The serialisation
    # goes through the SAME _serialise_rounds / _serialise_standings the
    # settle + durable-record producers use, so the shapes are
    # byte-compatible. Best-effort — _publish_active_tournament never
    # raises, so a publish failure cannot abort the resolution.
    def _publish_live_structure(strat: Any) -> None:
        live_rounds = _serialise_rounds(strat.live_rounds())
        # Overlay the runner's authoritative per-board ``projected`` map (the
        # scorer's domain) onto the racing rung's per-lane ``live_progress``
        # topology (the strategy's domain): the strategy publishes which lanes
        # are racing + their board-slice totals; the scorer publishes each
        # lane's live ``boards_done`` + streaming ``projected_scalar``. The
        # two compose here so the rung carries one authoritative per-lane
        # progress map the dashboard consumes directly.
        _overlay_projected_live_progress(live_rounds, workspace_root)
        live_standings = _overlay_projected_standings(
            _serialise_standings(strat.live_standings()),
            live_rounds,
            workspace_root,
            tournament_spec.structure,
        )
        _publish_active_tournament(
            workspace_root,
            tournament_id=tournament_id,
            epoch_id=epoch_id,
            structure=tournament_spec.structure,
            structure_params=dict(tournament_spec.params),
            competitors=competitors_meta,
            round_index=round_index,
            total_rounds=total_rounds,
            field_status=field_status,
            rounds=live_rounds,
            standings=live_standings,
            entries=_field_entries(competitors_meta, live_standings),
        )

    # --- Opt-in Bradley--Terry promotion pre-gate (crown on evidence). When
    # ``promote_confidence_threshold`` is set in the structure params, the
    # driver holds a crowning promote until the fitted rating clears the
    # confidence bar AND the CIs separate, spending closest-CI replicates in
    # between (the defer→replicate loop). Unset ⇒ ``pre_gate`` stays ``None``
    # and the resolution is byte-identical to today.
    pre_gate: EvidencePreGate | None = None
    replicate_duel = None
    on_inconclusive = None
    bt_threshold = read_promote_confidence_threshold(tournament_spec.params)
    if bt_threshold is not None:
        pre_gate = EvidencePreGate(
            threshold=bt_threshold,
            replicate_budget=read_replicate_budget(tournament_spec.params),
        )
        evidence_replicates_run = 0

        async def _replicate_duel(left_id: str, right_id: str) -> MatchupResult:
            # One extra crowning-pair duel for the pre-gate's evidence loop,
            # routed through the SAME board-unit runner + gate every other
            # duel uses (so a replicate is scored identically to the original
            # duel) — but at a RESERVED replicate index
            # (EVIDENCE_REPLICATE_BASE + j for evidence replicate j), so each
            # replicate draws BOTH sides fresh instead of cache-replaying (or,
            # in full mode, clobbering) the canonical replicate-0 slots. The
            # matchup id encodes the index; the driver's audit guard keys on
            # it. ``cache_scores=False`` keeps the single-draw aggregates out
            # of the fast-mode ``gen_score.json`` reuse.
            nonlocal evidence_replicates_run
            replicate_slot = EVIDENCE_REPLICATE_BASE + evidence_replicates_run
            evidence_replicates_run += 1
            return await _run_matchup(
                Matchup(
                    matchup_id=f"bt-replicate:r{replicate_slot}:{left_id}:{right_id}",
                    left=Contestant(generation_id=left_id, role="champion"),
                    right=Contestant(generation_id=right_id, role="challenger"),
                ),
                replicate_base=replicate_slot,
                cache_scores=False,
            )

        replicate_duel = _replicate_duel

        def _on_inconclusive(resolution: EvidenceResolution) -> None:
            # Record the unresolved crowning duel to the dead-letter queue so
            # nothing is silently dropped. Best-effort: a write failure must
            # not abort the round.
            verdict = resolution.verdict
            challenger_id = (
                verdict.challenger.generation_id
                if verdict.challenger is not None
                else first_challenger_id
            )
            champion_id = (
                verdict.champion.generation_id if verdict.champion is not None else parent_id
            )
            with best_effort(
                "dead-letter inconclusive record",
                on_error=lambda exc: log.debug("dead-letter record skipped: %s", exc),
            ):
                record_inconclusive(
                    workspace_root,
                    InconclusiveRecord(
                        generation_id=challenger_id,
                        champion_id=champion_id,
                        epoch_id=epoch_id,
                        rating=rating_block(verdict),
                        ci_history=resolution.ci_history,
                        reason=verdict.reason,
                    ),
                )
            # WS8: the ci_state trail per evidence-gate refit. On this path
            # the driver surfaces the resolution only for the inconclusive
            # terminal; a confirmed promote's replicate duels are still
            # traced through the per-matchup unit/gate events above.
            for _ci_row in resolution.ci_history:
                round_log.emit("evidence_replicated", {"ci_state": dict(_ci_row)})

        on_inconclusive = _on_inconclusive

    # --- Drive the strategy to a crowned decision.
    try:
        decision = await resolve_tournament(
            strategy,
            request_field=_request_field,
            run_matchup=_run_matchup,
            on_progress=_publish_live_structure,
            pre_gate=pre_gate,
            replicate_duel=replicate_duel,
            on_inconclusive=on_inconclusive,
        )
    except Exception:
        # A failure mid-resolution leaves no settled bracket — clear the
        # live "running" envelope so the dashboard does not show a stuck
        # tournament, then re-raise.
        _clear_active_tournament(workspace_root)
        raise

    # --- Final champion-gate Ladder-mediated holdout confirmation
    # (OVERFITTING.md §3/§4). The structure resolved its leader on the TRAIN
    # slice and ran ONE crowning champion-vs-survivor duel (also train). If
    # that duel promoted AND a holdout slice exists, the win must ALSO confirm
    # on the holdout — through the SAME Ladder-mediated machinery + the SAME
    # per-epoch ``ladder_state.json`` budget the gauntlet uses. A released
    # non-confirmation flips the crowning promote to a holdout reject; the
    # champion stands. Empty holdout (small board / split disabled) ⇒ no
    # holdout run, no Ladder move ⇒ byte-identical to today. The resulting
    # ``holdout`` block + the challenger's holdout-slice scalar are stamped on
    # the crowned/leading challenger's OutcomeRecord below (same shape as the
    # gauntlet), so #5's gap detector + the board-status surface work for
    # these structures too.
    crowning_confirm = await _confirm_crowning_on_holdout(
        decision=decision,
        parent_id=parent_id,
        champion_gen=champion_gen,
        generation_for=_generation_for,
        adapter=adapter,
        board=board,
        weights=weights,
        config=config,
        workspace_root=workspace_root,
        epoch_id=epoch_id,
        disable_drift=disable_drift,
        judge_only=judge_only,
        fast_mode=fast_mode,
        confirm_fn=confirm_crowning_holdout,
    )
    promoted_id = crowning_confirm.promoted_id
    crowning_reason_override = crowning_confirm.reason_override
    crowning_holdout_block = crowning_confirm.holdout_block
    crowning_holdout_child_scalar = crowning_confirm.holdout_child_scalar
    crowning_challenger_id = crowning_confirm.challenger_id
    crowning_challenger_train_scalar = crowning_confirm.challenger_train_scalar
    # WS8: the crowning holdout release (a populated block always means a
    # holdout existed and was consulted).
    if crowning_holdout_block is not None:
        round_log.emit(
            "holdout_released",
            {"confirmed": bool(crowning_holdout_block.get("confirmed"))},
        )

    # --- Opt-in integrity blocking modes (default OFF) -------------------
    # Guard the GATE-DECIDED crowning promote before anything persists,
    # mirroring the gauntlet's 10b'' block: diff containment on the crowned
    # child's snapshot + the gate-contradiction re-derivation against the
    # crowning duel's delta. Runs BEFORE the operator-override claim below,
    # so an explicit force-promote remains the operator's recorded
    # prerogative. Default-off ⇒ this branch is inert.
    if promoted_id is not None:
        _block_reason = _integrity_block_reason(
            weights=weights,
            parent_snapshot_root=champion_gen.snapshot_root,
            child_snapshot_root=by_id[promoted_id].snapshot_root,
            mutable_trees=_registered_mutable_trees(workspace_config),
            delta_scalar=crowning_confirm.crowning_delta_scalar,
        )
        if _block_reason is not None:
            log.warning(
                "evolve: integrity block — generation %s crowning refused (%s)",
                promoted_id,
                _block_reason,
            )
            promoted_id = None
            crowning_reason_override = _block_reason

    # --- Operator gate override (control protocol) for the FIELD ---------
    # The structure has settled (train bracket + holdout confirmation) but
    # nothing is persisted yet — the safe point at which an operator's
    # force-promote / force-reject of ANY field candidate overrides the
    # verdict. Unlike the gauntlet (one in-flight generation), a field round
    # resolves a whole slate, so an override may target a non-winner, the
    # crowned leader, or SEVERAL candidates (a tie / a multi-promote).
    # `_apply_field_overrides` documents the promoted-SET semantics + the
    # no-override single-promotion byte-identity invariant; every member of
    # the set is marked promoted in lineage while only the PRIMARY head
    # moves the champion pointer (the single-head invariant the downstream
    # guards rely on).
    field_candidate_ids = [c.generation_id for c in applied]
    field_overrides: dict[str, GateOverride] = claim_field_gate_overrides(
        workspace_root, field_candidate_ids
    )
    # The override RE-RESOLUTION is pure (`_apply_field_overrides`): given
    # the claimed overrides + the post-holdout crowning state it derives the
    # promoted set, the primary head, the per-generation provenance, and the
    # EFFECTIVE decision (the post-confirmation/post-override truth every
    # durable store must describe — issue #20). Only the claim above is I/O.
    (
        promoted_id,
        promoted_ids,
        override_provenance,
        effective_decision,
    ) = _apply_field_overrides(
        workspace_root=workspace_root,
        decision=decision,
        promoted_id=promoted_id,
        crowning_reason_override=crowning_reason_override,
        field_overrides=field_overrides,
        structure=tournament_spec.structure,
    )
    # WS8: the round's terminal decision + provenance (operator overrides
    # explicit, never silent) — the post-holdout/post-override truth.
    round_log.emit(
        "decision_recorded",
        {
            "decision": str(effective_decision.decision),
            "provenance": {
                "structure": tournament_spec.structure,
                "reason": effective_decision.reason,
                "parent_generation_id": parent_id,
                "promoted_generation_id": promoted_id,
                "promoted_generation_ids": sorted(promoted_ids),
                "overrides": override_provenance,
            },
        },
    )

    # --- Pareto frontier RECORD (docs/design/PARETO-FRONTIER.md). Same seam
    # as the gauntlet's, on the post-holdout / post-override truth: the
    # champion is the crowned generation when the field crowned one, else the
    # incumbent. A field round can carry the random-baseline placebo INSIDE
    # the slate, so its ids are named explicitly — a no-op re-emission of the
    # champion must never land on the record. Record-only; never fails a round.
    record_round_frontier(
        workspace_root=workspace_root,
        epoch_id=epoch_id,
        round_index=round_index,
        weights=weights,
        champion_generation_id=promoted_id or parent_id,
        aggregates=field_aggregates,
        placebo_generation_ids=[
            c.generation_id for c in applied if is_placebo_experiment(c.experiment)
        ],
        round_log=round_log,
    )

    # Settle the live envelope with the resolved rounds + standings so the
    # dashboard's structure reader sees the final bracket. Unlike the
    # gauntlet path (which clears its transient running record on exit),
    # the multi-challenger envelope is RETAINED with phase="completed":
    # competitors/rounds/standings are the dashboard's only live source for
    # a non-gauntlet field until the next round's tournament starts. Settled
    # with the HOLDOUT-RESOLVED decision so the dashboard never shows a crown
    # the champion pointer contradicts.
    _settle_active_tournament(
        workspace_root,
        tournament_id=tournament_id,
        epoch_id=epoch_id,
        structure=tournament_spec.structure,
        structure_params=dict(tournament_spec.params),
        competitors=competitors_meta,
        strategy=strategy,
        decision=effective_decision,
        round_index=round_index,
        total_rounds=total_rounds,
        field_status=field_status,
    )
    # Durably persist the settled FIELD structure (one record per round's
    # non-gauntlet tournament). The runtime ``active_tournament`` envelope
    # above is EPHEMERAL — it is overwritten by the next round and cleared
    # on a crash — so a completed swiss / elim epoch would render blank from
    # the index alone. The field record carries the same shape the live
    # envelope does, so the dashboard's structure renderers serve the ladder
    # post-run unchanged. Keyed on the round's first applied challenger so a
    # multi-round epoch keeps a snapshot per round. FINALISES the
    # ``in_progress`` envelope opened before resolution (issue #16) — the
    # same tournament_id, so this is an idempotent upsert to ``settled``.
    # Persisted with the HOLDOUT-RESOLVED decision (issue #20).
    _persist_field_tournament(
        workspace_root,
        field_tournament_id=f"{epoch_id}:field:{first_challenger_id}",
        first_challenger_id=first_challenger_id,
        epoch_id=epoch_id,
        structure=tournament_spec.structure,
        structure_params=dict(tournament_spec.params),
        competitors=competitors_meta,
        rounds=_serialise_rounds(strategy.rounds()),
        standings=_serialise_standings(effective_decision.standings),
        field_status=field_status or [],
        decision=effective_decision,
        state="settled",
        # Operator override readback (additive — omitted when no override
        # fired, so a gate-decided field round's record is byte-identical).
        override_status=override_provenance or None,
        promoted_generation_ids=(sorted(promoted_ids) if len(promoted_ids) > 1 else None),
    )

    rank_by_id = {s.generation_id: s.rank for s in decision.standings}
    matches_by_gen: dict[str, list[MatchOutcome]] = {c.generation_id: [] for c in applied}
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

    champion_agg = _first_aggregate_for(parent_id, decision)
    parent_scalar = float(champion_agg.get("scalar", 0.0)) if champion_agg else 0.0

    # Resolve a single round-level champion-eval provenance from the
    # CHAMPION's accumulated cached-vs-fresh board-unit tally. ``full``
    # when fast was not requested; ``fast`` when the champion was reused
    # for every board unit it needed (no fresh champion run this round);
    # ``fast-degraded`` when the champion had to run live at least once
    # (the seed/first champion, or a not-yet-covered subset) to seed the
    # cache. Recorded on each challenger's OutcomeRecord for the journal —
    # never a contract input.
    resolved_champion_eval_mode = _resolve_round_champion_mode(
        champion_cached_units,
        champion_fresh_units,
        fast_requested=fast_mode,
    )

    finalised_by_id: dict[str, Experiment] = {}
    child_scalar_crown = parent_scalar
    for challenger in applied:
        gid = challenger.generation_id
        # A generation is crowned if it is in the (possibly multi-element)
        # promoted set — see `_apply_field_overrides` for the no-override
        # single-promotion invariant (the set is exactly ``{promoted_id}``).
        is_crowned = gid in promoted_ids
        gid_override = field_overrides.get(gid)
        gen_decision = TournamentDecision.PROMOTED if is_crowned else TournamentDecision.REJECTED
        agg = _first_aggregate_for(gid, decision)
        gen_scalar = float(agg.get("scalar", 0.0)) if agg else 0.0
        if gid == promoted_id:
            child_scalar_crown = gen_scalar
        # The crowning challenger (the survivor that reached the final
        # champion-gate duel) carries the Ladder/holdout evidence block + the
        # per-generation train/holdout/gap fields, mirroring the gauntlet's
        # OutcomeRecord. A holdout-demoted crown carries the
        # ``holdout_not_confirmed`` reason from the confirmation step instead
        # of the strategy's crowning reason. Every other challenger (a dead
        # bracket branch) keeps the back-compat defaults (no holdout).
        is_crowning_challenger = gid == crowning_challenger_id
        if is_crowning_challenger:
            rejection_reason = (
                ""
                if is_crowned
                else (crowning_reason_override if crowning_reason_override else decision.reason)
            )
            holdout_block = crowning_holdout_block
            # Pair the crowning duel's TRAIN scalar with its HOLDOUT scalar so
            # the gap is measured on the same duel (falls back to the standings
            # aggregate only if the crowning train scalar is somehow absent).
            crown_train = (
                crowning_challenger_train_scalar
                if crowning_challenger_train_scalar is not None
                else gen_scalar
            )
            gen_fields = _generalization_fields_from_scalars(
                crown_train, crowning_holdout_child_scalar
            )
        else:
            rejection_reason = "" if is_crowned else decision.reason
            holdout_block = None
            gen_fields = _generalization_fields_from_scalars(gen_scalar, None)
        # An operator override on THIS generation makes its verdict explicit
        # (the reject reason carries the override note; a forced promote
        # clears it). Only stamped when an override fired.
        operator_override = gid_override is not None
        operator_override_reason = gid_override.reason if gid_override is not None else ""
        if gid_override is not None:
            rejection_reason = f"operator override: {gid_override.reason}" if not is_crowned else ""
        outcome_record = OutcomeRecord(
            ran_at=_now_iso(),
            drift_movements=(),
            pass_rate_delta=0.0,
            drift_loss_delta=0.0,
            scalar_score_delta=gen_scalar - parent_scalar,
            tournament_decision=gen_decision,
            rejection_reason=rejection_reason,
            operator_override=operator_override,
            operator_override_reason=operator_override_reason,
            structure=tournament_spec.structure,
            final_rank=rank_by_id.get(gid),
            match_record=tuple(matches_by_gen.get(gid, ())),
            champion_eval_mode=resolved_champion_eval_mode,
            holdout=holdout_block,
            train_loss=gen_fields["train_loss"],
            holdout_loss=gen_fields["holdout_loss"],
            generalization_gap=gen_fields["generalization_gap"],
        )
        # Shared outcome→index pipeline. Lineage + journal are deferred to
        # the loops below so the multi-challenger write ORDER is preserved:
        # every outcome persists, THEN the crowning invariant is checked,
        # THEN lineage + the champion marker advance, THEN the journal —
        # an invariant violation must abort before any lineage write.
        finalised_by_id[gid] = _finalize_generation(
            workspace_root=workspace_root,
            epoch_id=epoch_id,
            generation_id=gid,
            outcome=outcome_record,
            journal=False,
        )

    # --- Crowning invariant (issue #20): the durable bracket and the
    # champion state MUST agree. A settled bracket that records ``promoted``
    # with a ``promoted_generation_id`` the champion pointer + lineage never
    # advance to (or the inverse) is a silent correctness bug, so FAIL LOUDLY
    # before writing lineage rather than persisting a contradiction. The
    # persisted ``effective_decision`` is the post-holdout truth; ``promoted_id``
    # is what drives lineage + ``current_generation`` below — these two are the
    # same value by construction, and this guard makes that contract explicit
    # and catches any future code path that lets them drift apart.
    _bracket_promoted = effective_decision.decision == "promoted"
    if _bracket_promoted != (promoted_id is not None):
        raise RuntimeError(
            "crowning invariant violated: settled bracket decision "
            f"{effective_decision.decision!r} (promoted_generation_id="
            f"{effective_decision.promoted_generation_id!r}) disagrees with the "
            f"champion to be crowned ({promoted_id!r}); refusing to persist a "
            "bracket the champion pointer / lineage contradict"
        )
    if promoted_id is not None and promoted_id not in by_id:
        raise RuntimeError(
            "crowning invariant violated: settled bracket promotes "
            f"{promoted_id!r} but no such challenger applied this round; "
            "refusing to advance the champion to a generation with no snapshot"
        )

    # --- Lineage: every PROMOTED generation on the spine (promoted=True),
    # every other challenger recorded as a dead branch (rejected child of
    # the champion). An operator multi-promote marks each advanced candidate
    # promoted while current_generation still advances only to the PRIMARY
    # head below.
    for challenger in applied:
        gid = challenger.generation_id
        is_crowned = gid in promoted_ids
        gen_record = Generation(
            id=gid,
            epoch_id=epoch_id,
            parent_id=parent_id,
            snapshot_root=challenger.snapshot_root,
            created_at=challenger.generation.created_at,
            promoted=is_crowned,
            round_index=challenger.generation.round_index,
        )
        # The settle-time facts, per challenger (issue #124): the reason
        # this one was cut — already computed above, including the
        # holdout-demotion and operator-override phrasings, and read back
        # off the outcome so the DAG and experiment.json cannot disagree —
        # and its own standings scalar against the champion's. ``None``,
        # not 0.0, when a challenger has no aggregate: a zero scalar is a
        # legal measurement.
        settled = finalised_by_id.get(gid)
        gen_agg = _first_aggregate_for(gid, decision)
        lineage_parent_scalar = float(champion_agg["scalar"]) if champion_agg else None
        append_to_lineage(
            workspace_root,
            epoch_id,
            gen_record,
            parent_id=parent_id,
            rejection_reason=(
                settled.outcome.rejection_reason
                if settled is not None and settled.outcome is not None
                else ""
            ),
            parent_scalar=lineage_parent_scalar,
            child_scalar=float(gen_agg["scalar"]) if gen_agg else None,
        )
    if promoted_id is not None:
        generation_phase.set_current_generation(workspace_root, epoch_id, promoted_id)
        # The marker MUST now name the crowned generation — a write that did
        # not stick (e.g. a read-only workspace) would leave a settled
        # ``promoted`` bracket whose champion never advanced. Re-read and
        # raise rather than diverge silently (issue #20 acceptance #3).
        _crowned_head = generation_phase.current_generation(workspace_root, epoch_id)
        if _crowned_head != promoted_id:
            raise RuntimeError(
                "crowning invariant violated: bracket promoted "
                f"{promoted_id!r} but current_generation resolves to "
                f"{_crowned_head!r} after the crowning write; the champion "
                "pointer did not advance to the promoted generation"
            )

    # --- Post-promotion adapter hook (#125). Same seam as the gauntlet's:
    # one statement after the champion marker advanced, once per settled
    # promotion. Fires for the PRIMARY head only — an operator
    # multi-promote marks several candidates promoted in lineage, but
    # ``current_generation`` advances to exactly one, and it is that
    # crowning the adapter's out-of-tree state has to track.
    on_promote_failure: tuple[str, str, str] | None = None
    if promoted_id is not None:
        on_promote_failure = await fire_on_promote(
            adapter,
            workspace_root=workspace_root,
            epoch_id=epoch_id,
            generation_id=promoted_id,
            parent_generation_id=parent_id,
            snapshot_root=by_id[promoted_id].snapshot_root,
        )

    # --- Journal: one entry per challenger (crowned + dead branches).
    for challenger in applied:
        append_journal_entry(workspace_root, epoch_id, finalised_by_id[challenger.generation_id])

    # --- Shared round epilogue (loop-health + analyzer + report) — the
    # same `_round_epilogue` the gauntlet path runs.
    health_summary, health_critical = await _round_epilogue(
        workspace_root=workspace_root,
        epoch_id=epoch_id,
        board=board,
        round_n=generation_phase.round_number(applied[0].generation_id) or round_index,
        analyzer_round=generation_phase.round_number(applied[0].generation_id),
        mutations=mutations,
        auxiliary_call_llm=auxiliary_call_llm,
        auxiliary_model=auxiliary_model,
        meta_loop_emitter=meta_loop_emitter,
        token_clip=_token_clip_state(config),
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
        progress_log.append_progress(workspace_root, progress_log.TOURNAMENT_SETTLE)
    _beat(
        beater,
        workspace_root=workspace_root,
        progress=(
            progress_log.PROMOTE if bookkeeping_decision == "promoted" else progress_log.REJECT
        ),
        epoch_id=epoch_id,
        generation_id=promoted_id or applied[0].generation_id,
        round_index=round_index,
        phase=f"done:round_{round_index}:{tournament_id}:{bookkeeping_decision}",
    )
    round_log.emit("round_closed")

    # The round summary's champion/challenger scalars MUST come from the gate's
    # CROWNING matchup — the same champion-vs-leader duel the rejection_reason is
    # built from — not the per-pairing standings aggregate (`_first_aggregate_for`,
    # which averages across all of the champion's pairings) and not the
    # child-defaults-to-parent fallback (which reports delta 0.0 on a rejection
    # even though the gate measured a real regression — issue #10). Resolve the
    # crowning matchup's champion (the parent side) + challenger scalars; fall
    # back to the aggregate only when no crowning duel ran.
    summary_parent_scalar = parent_scalar
    summary_child_scalar = child_scalar_crown
    summary_child_id = promoted_id or applied[0].generation_id
    crowning = (
        next(
            (m for m in decision.matchups if m.matchup_id == decision.crowning_matchup_id),
            None,
        )
        if decision.crowning_matchup_id
        else None
    )
    if crowning is not None:
        champ_is_left = crowning.left_id == parent_id
        summary_parent_scalar = crowning.left_scalar() if champ_is_left else crowning.right_scalar()
        summary_child_scalar = crowning.right_scalar() if champ_is_left else crowning.left_scalar()
        # On a rejection the "proposed" gen reported is the LEADING challenger that
        # reached the gate (the one the reason is about), not an arbitrary applied[0].
        summary_child_id = promoted_id or (crowning.right_id if champ_is_left else crowning.left_id)

    # A holdout-demoted crown reports the ``holdout_not_confirmed`` cause from
    # the confirmation step, not the strategy's (promote-shaped) crowning
    # reason; every other rejection keeps the strategy's reason.
    summary_reason = (
        ""
        if promoted_id is not None
        else (crowning_reason_override if crowning_reason_override else decision.reason)
    )
    return EvolveRoundOutcome(
        parent_generation_id=parent_id,
        proposed_generation_id=summary_child_id,
        tournament_decision=bookkeeping_decision,
        rejection_reason=summary_reason,
        parent_scalar=summary_parent_scalar,
        child_scalar=summary_child_scalar,
        delta_scalar=summary_child_scalar - summary_parent_scalar,
        health_summary=health_summary,
        health_critical=health_critical,
    )


# ---------------------------------------------------------------------------
# Round-decision + outcome helpers shared by the evolve pipelines
# ---------------------------------------------------------------------------
