"""Evaluate and durably settle one strategy-driven evolve round."""

# ruff: noqa: E402
from __future__ import annotations

import asyncio
import logging
import time  # noqa: F401  — kept as the ``orch.time`` clock seam (see __all__)
from typing import Any

from zicato.core.types import (
    Experiment,
    Generation,
    OutcomeRecord,
    TournamentDecision,
)
from zicato.evolve import generation_phase
from zicato.evolve.candidate_batch import produce_candidate_batch
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
from zicato.evolve.ingest import _cache_gen_score
from zicato.evolve.lifecycle_services import (
    _beat,
    _now_iso,
)
from zicato.evolve.pareto import record_round_frontier
from zicato.evolve.persist import (
    _finalize_generation,
    _persist_rejected_round,
    _rejected_proposer_experiment,
    _round_epilogue,
)
from zicato.evolve.placebo import is_placebo_experiment
from zicato.evolve.promote_hook import fire_on_promote
from zicato.evolve.propose_apply import (
    _AppliedChallenger,
    _maybe_run_placebo_arm_gauntlet,
    _mint_placebo_challenger,
    _propose_and_apply_challenger,
    _trim_reason,
)
from zicato.runtime.control_consumer import (
    GateOverride,
    claim_field_gate_overrides,
)
from zicato.util import best_effort

log = logging.getLogger("zicato.orchestrator")

from zicato.evolve.decision_support import (
    _count_infra_aborted_runs,
    _defer_round_infra_outage,
    _field_failure_summary,
    _generalization_fields_from_scalars,
    _token_clip_state,
)
from zicato.evolve.round_api import EvolveRoundOutcome
from zicato.evolve.round_reporting import (
    _emit_gate_evaluated,
    _emit_harness_loaded,
    _emit_tournament_units,
    _promoted_entry_regressions,
    _RoundLogEmitter,
)
from zicato.evolve.settlement import (
    CandidateSettlement,
    RoundSettlement,
    ordered_promotions,
)


class _InfrastructureRoundDeferred(RuntimeError):
    """Signal that endpoint failures made the current evaluation invalid."""

    def __init__(self, aborted_runs: int, threshold: int) -> None:
        super().__init__("infrastructure-abort threshold reached")
        self.aborted_runs = aborted_runs
        self.threshold = threshold


async def evolve_field_round(
    prepared: generation_phase.PreparedRound,
    *,
    resume_plan: Any = None,
) -> EvolveRoundOutcome:
    """Run one evolve round under the configured selection strategy.

    Candidate production uses :meth:`SelectionStrategy.field_size`; the
    one-challenger gauntlet requests one candidate and wider structures
    request their declared field. Every scheduled matchup runs through
    :func:`zicato.tournament.runner.run_matchup` and the promotion gate. The
    strategy consumes each gate verdict without re-deciding it. Optional
    Bradley--Terry evidence and holdout confirmation may withhold a crown.
    Settlement then records every applied challenger, advances the primary
    champion when one was promoted, and persists the tournament audit.

    ``fast_mode`` is the runtime cache-first evaluation knob (the ``--mode
    fast`` setting), threaded identically to ``disable_drift`` and
    ``judge_only``. When set, every matchup resolves both competitors through
    the replicate-keyed unit cache and runs only missing slots (see
    :func:`zicato.tournament.runner.run_matchup`). Fast mode is therefore
    structure-independent: it composes with racing, Swiss, elimination, and
    gauntlet matchups without replaying one draw as another replicate. The
    resolved champion-eval mode is recorded in the journal for provenance; it
    is not a contract input, so flipping fast↔full does not roll the epoch.
    """
    from zicato.core.types import MatchOutcome  # noqa: PLC0415
    from zicato.epoch import (  # noqa: PLC0415
        append_journal_entry,
        append_to_lineage,
    )
    from zicato.selection import EvidencePreGate, evaluate_tournament  # noqa: PLC0415
    from zicato.selection.dead_letter import (  # noqa: PLC0415
        InconclusiveRecord,
        record_inconclusive,
    )
    from zicato.selection.driver import (  # noqa: PLC0415
        EvidenceResolution,
        make_evidence_replicate_duel,
    )
    from zicato.selection.evidence_gate import (  # noqa: PLC0415
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

    workspace_root = prepared.workspace_root
    workspace_config = prepared.workspace_config
    epoch_id = prepared.epoch_id
    round_index = prepared.round_index
    total_rounds = prepared.total_rounds
    parent_id = prepared.parent_generation.id
    adapter = prepared.adapter
    config = prepared.config
    weights = prepared.weights
    board = list(prepared.board)
    train_board = list(prepared.train_board)
    tournament_spec = prepared.tournament_spec
    strategy = prepared.strategy
    mutations = list(prepared.mutations)
    disable_drift = prepared.disable_drift
    judge_only = prepared.judge_only
    fast_mode = prepared.fast_mode
    beater = prepared.beater
    meta_loop_emitter = prepared.meta_loop_emitter
    # Narration (a rejected round's summary sentence, the round epilogue) is
    # auxiliary work, not proposing: it describes what the round did rather
    # than generating a candidate. It therefore runs on the auxiliary
    # callable and the auxiliary model id — the two must name the same
    # endpoint, or a workspace with a dedicated proposer engine would send
    # the auxiliary callable a model id it does not serve. The proposer
    # callable is picked separately, where a candidate is actually proposed
    # (``candidate_batch``).
    auxiliary_call_llm = config.auxiliary_call_llm
    auxiliary_model = str(workspace_config.get("auxiliary_model", ""))
    field_n = strategy.field_size()
    # WS8: a direct caller (tests) may not thread the opened emitter; bind
    # one so every emit below is uniformly best-effort. ``evolve_once`` (the
    # production caller) passes the emitter it already opened the round on.
    round_log = prepared.round_log or _RoundLogEmitter(workspace_root, epoch_id, round_index)

    # Publish the candidate batch while it forms so the dashboard can show
    # each slot before the tournament starts.
    live_status: dict[str, dict[str, Any]] = {}
    proposing_tournament_id = ""

    from zicato.runtime.state import TournamentPhase  # noqa: PLC0415

    def _publish_proposing(record: dict[str, Any]) -> None:
        nonlocal proposing_tournament_id
        generation_id = str(record.get("generation_id", ""))
        if not proposing_tournament_id:
            proposing_tournament_id = f"tourn_{epoch_id}_{generation_id}"
        live_status[generation_id] = dict(record)
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
            field_status=list(live_status.values()),
            phase=TournamentPhase.PROPOSING,
            entries=_field_entries(champion_only),
        )

    candidate_batch = await produce_candidate_batch(
        prepared,
        field_n,
        resume_plan=resume_plan,
        on_status=_publish_proposing,
        # Preserve the public monkeypatch anchor used by integration tests.
        produce_one=_propose_and_apply_challenger,
    )
    applied = list(candidate_batch.challengers)
    field_status = list(candidate_batch.field_status)
    base_id = candidate_batch.base_generation_id
    base_n = generation_phase.round_number(base_id)

    if not applied:
        if field_n == 1 and candidate_batch.rejections:
            from zicato.proposer.proposer import ProposerError  # noqa: PLC0415

            rejection = candidate_batch.rejections[0]
            if isinstance(rejection.proposer_error, ProposerError):
                error = rejection.proposer_error
                rejected_experiment = _rejected_proposer_experiment(
                    epoch_id,
                    parent_id,
                    base_id,
                    error,
                )
                return await _persist_rejected_round(
                    workspace_root=workspace_root,
                    epoch_id=epoch_id,
                    parent_id=parent_id,
                    next_id=base_id,
                    experiment=rejected_experiment,
                    validation_errors=list(error.attempts),
                    proposer_retries_exhausted=True,
                    board=board,
                    round_index=round_index,
                    auxiliary_call_llm=auxiliary_call_llm,
                    auxiliary_model=auxiliary_model,
                    beater=beater,
                    round_log=round_log,
                )
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
    # (a fully-failed proposer field keeps its rejection outcome) and
    # appended LAST so sibling diversity, ``first_challenger_id``, and the
    # real challengers' ids are untouched. Best-effort: a placebo mint
    # failure narrows the field back to the real challengers.
    _placebo_every_n = int(getattr(weights.overfitting, "random_baseline_every_n", 0))
    if field_n > 1 and base_n is not None and mutations:
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

    from zicato.scoring.diff_complexity import diff_size  # noqa: PLC0415

    parent_mutation_text = {point.id: point.content for point in mutations}
    candidate_diff_sizes = {
        challenger.generation_id: diff_size(challenger.experiment, parent_mutation_text)
        for challenger in applied
    }
    resume_cache = bool(candidate_batch.resumed_generation_ids)

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
    infra_aborted_round = 0

    # --- Per-generation aggregates for the Pareto frontier record. Filled
    # from the SAME dicts ``_cache_gen_score`` persists and only when
    # ``cache_scores`` is set, so an evidence-gate replicate duel's single
    # draw can never overwrite the round-scored aggregate the record reads
    # (docs/design/PARETO-FRONTIER.md §6).
    field_aggregates: dict[str, dict[str, Any]] = {}
    raw_matchup_results: dict[str, Any] = {}

    # --- Cross-matchup concurrency cap. A strategy may schedule several
    # matchups concurrently (the driver fans the batch out
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
        nonlocal champion_cached_units, champion_fresh_units, infra_aborted_round
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
            # full board, so no entry is excluded.
            board=(train_board if replicate_base > 0 or field_n > 1 or not fast_mode else board),
            weights=weights,
            config=config,
            workspace_root=workspace_root,
            epoch_id=epoch_id,
            board_subset=m.board_subset,
            replicates=m.replicates,
            replicate_base=replicate_base,
            disable_drift=disable_drift,
            judge_only=judge_only,
            fast=fast_mode or resume_cache,
            round_index=round_index,
            total_rounds=total_rounds,
            match_id=m.matchup_id,
            # Opt-in wall-clock cap on this duel's TOTAL board-unit execution.
            # None (the default for every structure that does not set it) keeps
            # the run uncapped; a racing rung may pin
            # it to bound a full-board grind (see Matchup.matchup_budget_seconds).
            matchup_budget_seconds=m.matchup_budget_seconds,
            # One shared semaphore across every matchup of this round, so all
            # concurrently-scheduled matchups draw from ONE global concurrency
            # cap rather than each minting its own ``Semaphore(parallelism)``.
            unit_semaphore=round_unit_semaphore,
            left_diff_size=candidate_diff_sizes.get(m.left.generation_id),
            right_diff_size=candidate_diff_sizes.get(m.right.generation_id),
        )
        raw_matchup_results[m.matchup_id] = result
        infra_threshold = int(getattr(config, "infra_abort_round_threshold", 0) or 0)
        if infra_threshold > 0:
            infra_aborted_round += _count_infra_aborted_runs(result)
            if infra_aborted_round >= infra_threshold:
                raise _InfrastructureRoundDeferred(infra_aborted_round, infra_threshold)
        # Attribute the CHAMPION's cached-vs-fresh board-unit tally for
        # this matchup (if the champion played in it). ``nonlocal`` so the
        # closure mutates the round-level accumulators.
        champ_prov = result.unit_provenance.get(parent_id)
        if champ_prov is not None:
            champion_cached_units += champ_prov.cached
            champion_fresh_units += champ_prov.fresh
        # Cache both sides' aggregates for fast-mode reuse. Skipped for evidence
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
        # Onto the round's durable event log: this matchup's board units and
        # gate verdict, each NAMING its generation — a field round settles
        # several matchups into one log, so unscoped units and gates would be
        # indistinguishable.
        _emit_tournament_units(
            round_log,
            result,
            parent_generation_id=m.left.generation_id,
            child_generation_id=m.right.generation_id,
            matchup_id=m.matchup_id,
        )
        _emit_harness_loaded(round_log, workspace_root, epoch_id, result)
        _emit_gate_evaluated(
            round_log,
            result.outcome,
            parent_agg=result.parent_agg,
            child_agg=result.child_agg,
            weights=weights,
            generation_id=m.right.generation_id,
            opponent_generation_id=m.left.generation_id,
            matchup_id=m.matchup_id,
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
    # between (the defer→replicate loop). Unset leaves the strategy's decision
    # unchanged.
    pre_gate: EvidencePreGate | None = None
    replicate_duel = None
    on_inconclusive = None
    bt_threshold = read_promote_confidence_threshold(tournament_spec.params)
    if bt_threshold is not None:
        pre_gate = EvidencePreGate(
            threshold=bt_threshold,
            replicate_budget=read_replicate_budget(tournament_spec.params),
        )
        # Each extra crowning-pair duel runs through the SAME board-unit
        # runner and gate every other duel uses, so a replicate is scored
        # identically to the original duel. The reserved slot, the matchup
        # id that encodes it, and the score-cache suppression are the
        # factory's, not this round's — one implementation of the
        # ReplicateDuel contract, exercised by the decision-procedure
        # oracle at the same seam production drives it from.
        replicate_duel = make_evidence_replicate_duel(_run_matchup)

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

        on_inconclusive = _on_inconclusive

    # --- Drive the strategy to a crowned decision.
    try:
        evaluation = await evaluate_tournament(
            strategy,
            request_field=_request_field,
            run_matchup=_run_matchup,
            on_progress=_publish_live_structure,
            pre_gate=pre_gate,
            replicate_duel=replicate_duel,
            on_inconclusive=on_inconclusive,
        )
    except _InfrastructureRoundDeferred as deferred:
        _clear_active_tournament(workspace_root)
        return _defer_round_infra_outage(
            workspace_root=workspace_root,
            epoch_id=epoch_id,
            parent_id=parent_id,
            next_id=base_id,
            board=board,
            round_index=round_index,
            infra_aborted=deferred.aborted_runs,
            infra_threshold=deferred.threshold,
            beater=beater,
            round_log=round_log,
        )
    except Exception:
        # A failure mid-resolution leaves no settled bracket — clear the
        # live "running" envelope so the dashboard does not show a stuck
        # tournament, then re-raise.
        _clear_active_tournament(workspace_root)
        raise
    decision = evaluation.decision
    gate_evidence: dict[str, Any] | None = None
    if evaluation.evidence is not None:
        gate_evidence = dict(rating_block(evaluation.evidence.verdict))
        gate_evidence["ci_history"] = [dict(row) for row in evaluation.evidence.ci_history]
        # Preserve the full refit trail for confirmed and inconclusive
        # terminals. Matchup events prove that each replicate ran; these
        # events preserve the statistical state produced after every refit.
        # Scoped to the challenger the refits are about — two challengers can
        # reach an evidence terminal in one round, and the id is what keeps
        # their trails apart. Same derivation as the dead-letter record above.
        _evidence_verdict = evaluation.evidence.verdict
        _evidence_challenger = (
            _evidence_verdict.challenger.generation_id
            if _evidence_verdict.challenger is not None
            else first_challenger_id
        )
        for ci_row in evaluation.evidence.ci_history:
            round_log.emit(
                "evidence_replicated",
                {"ci_state": dict(ci_row)},
                {"generation_id": _evidence_challenger},
            )

    # --- Final champion-gate Ladder-mediated holdout confirmation
    # (OVERFITTING.md §3/§4). The structure resolved its leader on the TRAIN
    # slice and ran ONE crowning champion-vs-survivor duel (also train). If
    # that duel promoted AND a holdout slice exists, the win must ALSO confirm
    # on the holdout through the shared Ladder-mediated machinery and
    # per-epoch ``ladder_state.json`` budget. A released
    # non-confirmation flips the crowning promote to a holdout reject; the
    # champion stands. Empty holdout (small board / split disabled) means no
    # holdout run and no Ladder move. The resulting
    # ``holdout`` block + the challenger's holdout-slice scalar are stamped on
    # the crowned or leading challenger's OutcomeRecord below, so the gap
    # detector and board-status surface consume one record shape.
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
        confirm_holdout=not (field_n == 1 and fast_mode),
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
            {"generation_id": crowning_challenger_id},
        )

    # --- Opt-in integrity blocking modes (default OFF) -------------------
    # Guard the GATE-DECIDED crowning promote before anything persists,
    # using diff containment on the crowned child's snapshot plus the
    # gate-contradiction re-derivation against the
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

    # --- Operator gate overrides -----------------------------------------
    # The structure has settled (train bracket + holdout confirmation) but
    # nothing is persisted yet — the safe point at which an operator's
    # force-promote / force-reject of ANY field candidate overrides the
    # verdict. An override may target a non-winner, the crowned leader, or
    # several candidates. A one-candidate gauntlet is the degenerate case.
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
                "operator_override": bool(override_provenance),
                "operator_override_reason": next(
                    (
                        override.reason
                        for generation_id, override in field_overrides.items()
                        if generation_id == promoted_id or promoted_id is None
                    ),
                    "",
                ),
                "overrides": override_provenance,
            },
        },
    )

    # --- Pareto frontier RECORD (docs/design/PARETO-FRONTIER.md). On the
    # post-holdout and post-override truth, the
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

    # Settle the live envelope with the resolved rounds and standings so the
    # dashboard sees the final topology. The completed envelope remains
    # available until the next round starts. It carries the holdout-resolved
    # decision, so the dashboard never shows a crown that contradicts the
    # champion pointer.
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
    # Durably persist the settled structure. The runtime
    # ``active_tournament`` envelope
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

    candidate_settlements: list[CandidateSettlement] = []
    child_scalar_crown = parent_scalar
    for challenger in applied:
        gid = challenger.generation_id
        # A generation is crowned if it is in the (possibly multi-element)
        # promoted set — see `_apply_field_overrides` for the no-override
        # single-promotion invariant (the set is exactly ``{promoted_id}``).
        is_crowned = gid in promoted_ids
        is_crowning_challenger = gid == crowning_challenger_id
        gid_override = field_overrides.get(gid)
        if is_crowned:
            gen_decision = TournamentDecision.PROMOTED
        elif is_crowning_challenger and decision.decision == "deferred":
            gen_decision = TournamentDecision.DEFERRED
        else:
            gen_decision = TournamentDecision.REJECTED
        agg = _first_aggregate_for(gid, decision)
        gen_scalar = float(agg.get("scalar", 0.0)) if agg else 0.0
        if gid == promoted_id:
            child_scalar_crown = gen_scalar
        # The crowning challenger (the survivor that reached the final
        # champion-gate duel) carries the Ladder/holdout evidence block + the
        # per-generation train/holdout/gap fields. A holdout-demoted crown carries the
        # ``holdout_not_confirmed`` reason from the confirmation step instead
        # of the strategy's crowning reason. Every other challenger (a dead
        # bracket branch) has no holdout block.
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
            raw_crowning = raw_matchup_results.get(decision.crowning_matchup_id)
            if raw_crowning is not None:
                champion_is_parent_side = raw_crowning.parent_generation_id == parent_id
                pass_rate_delta = float(raw_crowning.outcome.delta_pass_rate) * (
                    1.0 if champion_is_parent_side else -1.0
                )
                champion_drift = float(
                    (
                        raw_crowning.parent_agg
                        if champion_is_parent_side
                        else raw_crowning.child_agg
                    ).get("drift_loss_mean", 0.0)
                )
                challenger_drift = float(
                    (
                        raw_crowning.child_agg
                        if champion_is_parent_side
                        else raw_crowning.parent_agg
                    ).get("drift_loss_mean", 0.0)
                )
                drift_loss_delta = challenger_drift - champion_drift
            else:
                pass_rate_delta = 0.0
                drift_loss_delta = 0.0
        else:
            rejection_reason = "" if is_crowned else decision.reason
            holdout_block = None
            gen_fields = _generalization_fields_from_scalars(gen_scalar, None)
            pass_rate_delta = 0.0
            drift_loss_delta = 0.0
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
            pass_rate_delta=pass_rate_delta,
            drift_loss_delta=drift_loss_delta,
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
            evidence=gate_evidence if is_crowning_challenger else None,
        )
        candidate_settlements.append(
            CandidateSettlement(
                challenger=challenger,
                outcome=outcome_record,
            )
        )

    settlement = RoundSettlement(
        decision=effective_decision,
        primary_promoted_generation_id=promoted_id,
        promoted_generation_ids=ordered_promotions(promoted_id, promoted_ids),
        candidates=tuple(candidate_settlements),
    )

    # Outcomes and their index projections become durable before lineage or
    # the champion marker changes. An invariant failure below can therefore
    # never leave a promoted lineage node pointing at an unsettled outcome.
    finalised_by_id: dict[str, Experiment] = {}
    for candidate in settlement.candidates:
        gid = candidate.challenger.generation_id
        finalised_by_id[gid] = _finalize_generation(
            workspace_root=workspace_root,
            epoch_id=epoch_id,
            generation_id=gid,
            outcome=candidate.outcome,
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
    _bracket_promoted = settlement.decision.decision == "promoted"
    if _bracket_promoted != (settlement.primary_promoted_generation_id is not None):
        raise RuntimeError(
            "crowning invariant violated: settled bracket decision "
            f"{settlement.decision.decision!r} (promoted_generation_id="
            f"{settlement.decision.promoted_generation_id!r}) disagrees with the "
            "champion to be crowned "
            f"({settlement.primary_promoted_generation_id!r}); refusing to persist a "
            "bracket the champion pointer / lineage contradict"
        )
    if (
        settlement.primary_promoted_generation_id is not None
        and settlement.primary_promoted_generation_id not in by_id
    ):
        raise RuntimeError(
            "crowning invariant violated: settled bracket promotes "
            f"{settlement.primary_promoted_generation_id!r} but no such challenger "
            "applied this round; "
            "refusing to advance the champion to a generation with no snapshot"
        )

    # --- Lineage: every PROMOTED generation on the spine (promoted=True),
    # every other challenger recorded as a dead branch (rejected child of
    # the champion). An operator multi-promote marks each advanced candidate
    # promoted while current_generation still advances only to the PRIMARY
    # head below.
    for candidate in settlement.candidates:
        challenger = candidate.challenger
        gid = challenger.generation_id
        is_crowned = gid in settlement.promoted_generation_ids
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
    if settlement.primary_promoted_generation_id is not None:
        generation_phase.set_current_generation(
            workspace_root,
            epoch_id,
            settlement.primary_promoted_generation_id,
        )
        # The marker MUST now name the crowned generation — a write that did
        # not stick (e.g. a read-only workspace) would leave a settled
        # ``promoted`` bracket whose champion never advanced. Re-read and
        # raise rather than diverge silently (issue #20 acceptance #3).
        _crowned_head = generation_phase.current_generation(workspace_root, epoch_id)
        if _crowned_head != settlement.primary_promoted_generation_id:
            raise RuntimeError(
                "crowning invariant violated: bracket promoted "
                f"{settlement.primary_promoted_generation_id!r} but current_generation resolves to "
                f"{_crowned_head!r} after the crowning write; the champion "
                "pointer did not advance to the promoted generation"
            )

    # --- Post-promotion adapter hook (#125). One call after the champion
    # marker advances, once per settled
    # promotion. Fires for the PRIMARY head only — an operator
    # multi-promote marks several candidates promoted in lineage, but
    # ``current_generation`` advances to exactly one, and it is that
    # crowning the adapter's out-of-tree state has to track.
    on_promote_failure: tuple[str, str, str] | None = None
    if settlement.primary_promoted_generation_id is not None:
        on_promote_failure = await fire_on_promote(
            adapter,
            workspace_root=workspace_root,
            epoch_id=epoch_id,
            generation_id=settlement.primary_promoted_generation_id,
            parent_generation_id=parent_id,
            snapshot_root=by_id[settlement.primary_promoted_generation_id].snapshot_root,
        )

    # --- Journal: one entry per challenger (crowned + dead branches).
    for candidate in settlement.candidates:
        append_journal_entry(
            workspace_root,
            epoch_id,
            finalised_by_id[candidate.challenger.generation_id],
        )

    if field_n == 1:
        await _maybe_run_placebo_arm_gauntlet(
            workspace_root=workspace_root,
            epoch_id=epoch_id,
            adapter=adapter,
            parent_gen=champion_gen,
            parent_id=parent_id,
            round_id=base_id,
            mutations=mutations,
            board=board,
            weights=weights,
            config=config,
            disable_drift=disable_drift,
            judge_only=judge_only,
            fast_mode=fast_mode,
            round_index=round_index,
            total_rounds=total_rounds,
        )

    # --- Round epilogue: loop health, analyzer, and report.
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
        attributable_regressions=(
            _promoted_entry_regressions(raw_matchup_results[decision.crowning_matchup_id])
            if promoted_id is not None and decision.crowning_matchup_id in raw_matchup_results
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

    # The effective decision is the post-holdout, post-integrity, and
    # post-override truth. Its reason therefore matches the persisted outcome
    # for every rejection path.
    summary_reason = "" if promoted_id is not None else effective_decision.reason
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
