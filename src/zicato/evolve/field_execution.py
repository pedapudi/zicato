"""Run one round's tournament: every scheduled duel, under one strategy.

The round pipeline's run phase.  It opens the round's live and durable
tournament envelopes, hands the strategy the champion and the applied
field, runs every matchup the strategy schedules through the canonical
board-unit runner and the unchanged promotion gate, republishes the live
structure as the bracket fills, and returns the strategy's crowned
decision together with the round-level tallies the later phases read.

Nothing here re-decides a duel.  The gate's verdict travels back to the
strategy untouched; the optional Bradley--Terry pre-gate may hold a crowning
promote and spend extra replicates, and that is the one place a decision is
revisited before settlement.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from functools import partial
from typing import TYPE_CHECKING, Any

from zicato.evolve.dashboard_projection import (
    _clear_active_tournament,
    _field_entries,
    _overlay_projected_live_progress,
    _overlay_projected_standings,
    _persist_field_tournament,
    _publish_active_tournament,
    _serialise_rounds,
    _serialise_standings,
)
from zicato.evolve.decision_support import (
    _count_infra_aborted_runs,
    _defer_round_infra_outage,
)
from zicato.evolve.field_candidates import CandidateField
from zicato.evolve.gate import _resolve_round_champion_mode
from zicato.evolve.generation_phase import FieldRound
from zicato.evolve.ingest import _cache_gen_score
from zicato.evolve.lifecycle_services import _beat
from zicato.evolve.round_api import EvolveRoundOutcome
from zicato.evolve.round_reporting import (
    _emit_gate_evaluated,
    _emit_harness_loaded,
    _emit_tournament_units,
)
from zicato.util import best_effort

if TYPE_CHECKING:
    from zicato.selection.driver import EvidenceResolution
    from zicato.selection.strategy import Contestant, Matchup, MatchupResult

log = logging.getLogger("zicato.orchestrator")


class _InfrastructureRoundDeferred(RuntimeError):
    """Signal that endpoint failures made the current evaluation invalid."""

    def __init__(self, aborted_runs: int, threshold: int) -> None:
        super().__init__("infrastructure-abort threshold reached")
        self.aborted_runs = aborted_runs
        self.threshold = threshold


@dataclass(slots=True)
class RoundTallies:
    """What every matchup of one round accumulates into the round's account.

    ``champion_cached_units`` / ``champion_fresh_units`` are the CHAMPION's
    cached-versus-fresh board-unit counts.  The round-level champion-eval
    mode is attributed to the champion specifically: a
    challenger-versus-challenger duel runs both sides fresh because they are
    new generations and must run, which says nothing about champion reuse.

    ``aggregates`` holds the per-generation aggregate the Pareto frontier
    record reads, filled from the same dicts :func:`_cache_gen_score`
    persists and only when ``cache_scores`` is set, so an evidence-gate
    replicate duel's single draw can never overwrite the round-scored
    aggregate (docs/design/PARETO-FRONTIER.md §6).  ``raw_results`` keeps
    each duel's full :class:`~zicato.tournament.runner.TournamentResult` for
    the crowning duel's per-entry evidence.
    """

    champion_cached_units: int = 0
    champion_fresh_units: int = 0
    infra_aborted_runs: int = 0
    aggregates: dict[str, dict[str, Any]] = dataclass_field(default_factory=dict)
    raw_results: dict[str, Any] = dataclass_field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class FieldExecution:
    """One resolved tournament: its crowned decision and its measurements.

    ``decision`` is the strategy's own crowning, before holdout
    confirmation, integrity blocking, and operator overrides revise it.
    ``gate_evidence`` is the Bradley--Terry rating block when the pre-gate
    ran, ``None`` otherwise.  ``champion_eval_mode`` is the round-level
    cache-reuse provenance recorded on every challenger's outcome.
    """

    decision: Any
    gate_evidence: dict[str, Any] | None
    aggregates: dict[str, dict[str, Any]]
    raw_results: dict[str, Any]
    champion_eval_mode: str


async def request_field(candidates: CandidateField, _n: int) -> tuple[Contestant, list[Contestant]]:
    """Hand the strategy the champion and the applied challenger field."""

    from zicato.selection.strategy import Contestant  # noqa: PLC0415

    champion = Contestant(generation_id=candidates.champion.id, role="champion")
    challengers = [
        Contestant(
            generation_id=c.generation_id,
            role="challenger",
            snapshot_root=c.snapshot_root,
            experiment=c.experiment,
        )
        for c in candidates.challengers
    ]
    return champion, challengers


async def run_field_matchup(
    field_round: FieldRound,
    candidates: CandidateField,
    tallies: RoundTallies,
    unit_semaphore: asyncio.Semaphore,
    matchup: Matchup,
    *,
    replicate_base: int = 0,
    cache_scores: bool = True,
) -> MatchupResult:
    """Run one duel through the board-unit runner and the unchanged gate.

    ``replicate_base`` and ``cache_scores`` exist for the evidence pre-gate's
    replicate duels only: a reserved replicate base keeps evidence draws off
    the canonical cache slots, and ``cache_scores=False`` keeps a single
    evidence draw's aggregates from overwriting the round-scored
    ``gen_score.json`` the fast-mode champion reuse reads.  Every strategy
    matchup uses the defaults.
    """

    from zicato.selection.strategy import MatchupResult  # noqa: PLC0415
    from zicato.tournament.runner import run_matchup  # noqa: PLC0415

    _beat(
        field_round.beater,
        epoch_id=field_round.epoch_id,
        generation_id=matchup.right.generation_id,
        round_index=field_round.round_index,
        phase=f"tournament:round_{field_round.round_index}:{matchup.matchup_id}",
    )
    result = await run_matchup(
        adapter=field_round.adapter,
        left_gen=candidates.generation(matchup.left.generation_id),
        right_gen=candidates.generation(matchup.right.generation_id),
        # Internal selection scores on the TRAIN slice only (the holdout
        # is confirmation-only, never consulted to pick the leader). A racing
        # rung's ``board_subset`` is intersected against the train board
        # inside ``run_matchup``. Empty holdout ⇒ ``train_board`` IS the
        # full board, so no entry is excluded.
        board=field_round.train_board,
        weights=field_round.weights,
        config=field_round.config,
        workspace_root=field_round.workspace_root,
        epoch_id=field_round.epoch_id,
        board_subset=matchup.board_subset,
        replicates=matchup.replicates,
        replicate_base=replicate_base,
        disable_drift=field_round.disable_drift,
        judge_only=field_round.judge_only,
        fast=field_round.fast_mode or candidates.resume_cache,
        round_index=field_round.round_index,
        total_rounds=field_round.total_rounds,
        match_id=matchup.matchup_id,
        # Opt-in wall-clock cap on this duel's TOTAL board-unit execution.
        # None (the default for every structure that does not set it) keeps
        # the run uncapped; a racing rung may pin
        # it to bound a full-board grind (see Matchup.matchup_budget_seconds).
        matchup_budget_seconds=matchup.matchup_budget_seconds,
        # One shared semaphore across every matchup of this round, so all
        # concurrently-scheduled matchups draw from ONE global concurrency
        # cap rather than each minting its own ``Semaphore(parallelism)``.
        unit_semaphore=unit_semaphore,
        left_diff_size=candidates.diff_sizes.get(matchup.left.generation_id),
        right_diff_size=candidates.diff_sizes.get(matchup.right.generation_id),
    )
    tallies.raw_results[matchup.matchup_id] = result
    infra_threshold = int(getattr(field_round.config, "infra_abort_round_threshold", 0) or 0)
    if infra_threshold > 0:
        tallies.infra_aborted_runs += _count_infra_aborted_runs(result)
        if tallies.infra_aborted_runs >= infra_threshold:
            raise _InfrastructureRoundDeferred(tallies.infra_aborted_runs, infra_threshold)
    # Attribute the CHAMPION's cached-vs-fresh board-unit tally for this
    # matchup (if the champion played in it).
    champ_prov = result.unit_provenance.get(field_round.parent_id)
    if champ_prov is not None:
        tallies.champion_cached_units += champ_prov.cached
        tallies.champion_fresh_units += champ_prov.fresh
    # Cache both sides' aggregates for fast-mode reuse. Skipped for evidence
    # replicate duels (``cache_scores=False``): one reserved-slot draw
    # must not overwrite the round-scored aggregates.
    if cache_scores:
        # Every matchup appends its own line to the archive beside the
        # canonical file, so the within-round measurements the last
        # matchup's write shadows are still on disk (issue #122).
        _cache_gen_score(
            field_round.workspace_root,
            field_round.epoch_id,
            matchup.left.generation_id,
            result.parent_agg,
            round_index=field_round.round_index,
        )
        _cache_gen_score(
            field_round.workspace_root,
            field_round.epoch_id,
            matchup.right.generation_id,
            result.child_agg,
            round_index=field_round.round_index,
        )
        tallies.aggregates[matchup.left.generation_id] = result.parent_agg
        tallies.aggregates[matchup.right.generation_id] = result.child_agg
    # Onto the round's durable event log: this matchup's board units and
    # gate verdict, each NAMING its generation — a field round settles
    # several matchups into one log, so unscoped units and gates would be
    # indistinguishable.
    _emit_tournament_units(
        field_round.round_log,
        result,
        parent_generation_id=matchup.left.generation_id,
        child_generation_id=matchup.right.generation_id,
        matchup_id=matchup.matchup_id,
    )
    _emit_harness_loaded(
        field_round.round_log, field_round.workspace_root, field_round.epoch_id, result
    )
    _emit_gate_evaluated(
        field_round.round_log,
        result.outcome,
        parent_agg=result.parent_agg,
        child_agg=result.child_agg,
        weights=field_round.weights,
        generation_id=matchup.right.generation_id,
        opponent_generation_id=matchup.left.generation_id,
        matchup_id=matchup.matchup_id,
    )
    return MatchupResult(
        matchup_id=matchup.matchup_id,
        left_id=matchup.left.generation_id,
        right_id=matchup.right.generation_id,
        left_agg=result.parent_agg,
        right_agg=result.child_agg,
        outcome=result.outcome,
        stage_index=matchup.stage_index,
        bracket_slot=matchup.bracket_slot,
    )


def publish_live_structure(
    field_round: FieldRound, candidates: CandidateField, strategy: Any
) -> None:
    """Republish the envelope with the structure as it stands mid-round.

    Each time the driver schedules a batch of pending matchups, the envelope
    carries the settled rounds plus the in-flight round (matches with
    ``winner: null`` and ``pending: true``) and the standings so far.  That
    is what lets the dashboard's bracket, ladder, and funnel exist DURING the
    run instead of reading "being seeded" until settle.  The serialisation
    goes through the SAME :func:`_serialise_rounds` / :func:`_serialise_standings`
    the settle and durable-record producers use, so the shapes are
    byte-compatible.  Best-effort: :func:`_publish_active_tournament` never
    raises, so a publish failure cannot abort the resolution.
    """

    live_rounds = _serialise_rounds(strategy.live_rounds())
    # Overlay the runner's authoritative per-board ``projected`` map (the
    # scorer's domain) onto the racing rung's per-lane ``live_progress``
    # topology (the strategy's domain): the strategy publishes which lanes
    # are racing + their board-slice totals; the scorer publishes each
    # lane's live ``boards_done`` + streaming ``projected_scalar``. The
    # two compose here so the rung carries one authoritative per-lane
    # progress map the dashboard consumes directly.
    _overlay_projected_live_progress(live_rounds, field_round.workspace_root)
    live_standings = _overlay_projected_standings(
        _serialise_standings(strategy.live_standings()),
        live_rounds,
        field_round.workspace_root,
        field_round.tournament_spec.structure,
    )
    _publish_active_tournament(
        field_round.workspace_root,
        tournament_id=candidates.tournament_id,
        epoch_id=field_round.epoch_id,
        structure=field_round.tournament_spec.structure,
        structure_params=dict(field_round.tournament_spec.params),
        competitors=candidates.competitors,
        round_index=field_round.round_index,
        total_rounds=field_round.total_rounds,
        field_status=candidates.field_status,
        rounds=live_rounds,
        standings=live_standings,
        entries=_field_entries(candidates.competitors, live_standings),
    )


def record_inconclusive_duel(
    field_round: FieldRound, candidates: CandidateField, resolution: EvidenceResolution
) -> None:
    """Record an unresolved crowning duel to the dead-letter queue.

    Best-effort: a write failure must not abort the round.
    """

    from zicato.selection.dead_letter import (  # noqa: PLC0415
        InconclusiveRecord,
        record_inconclusive,
    )
    from zicato.selection.evidence_gate import rating_block  # noqa: PLC0415

    verdict = resolution.verdict
    challenger_id = (
        verdict.challenger.generation_id
        if verdict.challenger is not None
        else candidates.first_challenger_id
    )
    champion_id = (
        verdict.champion.generation_id if verdict.champion is not None else field_round.parent_id
    )
    with best_effort(
        "dead-letter inconclusive record",
        on_error=lambda exc: log.debug("dead-letter record skipped: %s", exc),
    ):
        record_inconclusive(
            field_round.workspace_root,
            InconclusiveRecord(
                generation_id=challenger_id,
                champion_id=champion_id,
                epoch_id=field_round.epoch_id,
                rating=rating_block(verdict),
                ci_history=resolution.ci_history,
                reason=verdict.reason,
            ),
        )


def _open_tournament_envelopes(field_round: FieldRound, candidates: CandidateField) -> None:
    """Publish the live envelope and OPEN the durable field record.

    The runtime ``active_tournament`` envelope is ephemeral: cleared on a
    crash, overwritten next round.  Only the durable
    ``tournaments/field-*.json`` record is queryable by the index, by
    ``zicato repair index``, and by any external consumer.  Opening it here
    in ``in_progress`` state — with the competitor field and proposing status
    but no resolved bracket yet — means the in-flight round is visible to
    EVERY store the moment its challengers are minted rather than only at
    settle (issue #16).  The settle write upserts this same record, keyed on
    the same tournament id, so the open and the settle compose idempotently
    and a resume that re-opens an existing ``in_progress`` record neither
    duplicates nor corrupts it.
    """

    from zicato.runtime import progress_log  # noqa: PLC0415

    _publish_active_tournament(
        field_round.workspace_root,
        tournament_id=candidates.tournament_id,
        epoch_id=field_round.epoch_id,
        structure=field_round.tournament_spec.structure,
        structure_params=dict(field_round.tournament_spec.params),
        competitors=candidates.competitors,
        round_index=field_round.round_index,
        total_rounds=field_round.total_rounds,
        field_status=candidates.field_status,
        entries=_field_entries(candidates.competitors),
    )
    # Progress transition: the field tournament started executing. One
    # round-level append (NOT per matchup) so the liveness seq advances on
    # genuine progress. Best-effort — never abort the round on a log write.
    with best_effort(
        "progress-log field tournament-start",
        on_error=lambda exc: log.debug("progress-log field tournament-start skipped: %s", exc),
    ):
        progress_log.append_progress(field_round.workspace_root, progress_log.TOURNAMENT_START)
    _persist_field_tournament(
        field_round.workspace_root,
        field_tournament_id=f"{field_round.epoch_id}:field:{candidates.first_challenger_id}",
        first_challenger_id=candidates.first_challenger_id,
        epoch_id=field_round.epoch_id,
        structure=field_round.tournament_spec.structure,
        structure_params=dict(field_round.tournament_spec.params),
        competitors=candidates.competitors,
        rounds=[],
        standings=[],
        field_status=candidates.field_status or [],
        decision=None,
        state="in_progress",
    )


def _emit_evidence_trail(
    field_round: FieldRound, candidates: CandidateField, evidence: Any
) -> dict[str, Any]:
    """Record the pre-gate's refit trail and return its rating block.

    Matchup events prove that each replicate ran; these events preserve the
    statistical state produced after every refit, for confirmed and
    inconclusive terminals alike.  Scoped to the challenger the refits are
    about — two challengers can reach an evidence terminal in one round, and
    the id is what keeps their trails apart.
    """

    from zicato.selection.evidence_gate import rating_block  # noqa: PLC0415

    gate_evidence = dict(rating_block(evidence.verdict))
    gate_evidence["ci_history"] = [dict(row) for row in evidence.ci_history]
    challenger_id = (
        evidence.verdict.challenger.generation_id
        if evidence.verdict.challenger is not None
        else candidates.first_challenger_id
    )
    for ci_row in evidence.ci_history:
        field_round.round_log.emit(
            "evidence_replicated",
            {"ci_state": dict(ci_row)},
            {"generation_id": challenger_id},
        )
    return gate_evidence


async def execute_field_tournament(
    field_round: FieldRound, candidates: CandidateField
) -> FieldExecution | EvolveRoundOutcome:
    """Drive the strategy to a crowned decision over the applied field.

    Returns a :class:`FieldExecution` for a tournament that resolved.  A
    round the endpoint-outage circuit deferred is terminal instead: the
    outcome returned is already recorded, nothing was journaled, and the
    caller returns it unchanged.  Any other failure mid-resolution clears the
    live "running" envelope — a dashboard must not show a stuck tournament —
    and re-raises.
    """

    from zicato.selection import EvidencePreGate, evaluate_tournament  # noqa: PLC0415
    from zicato.selection.driver import make_evidence_replicate_duel  # noqa: PLC0415
    from zicato.selection.evidence_gate import (  # noqa: PLC0415
        read_promote_confidence_threshold,
        read_replicate_budget,
    )

    tallies = RoundTallies()
    # One semaphore for the whole round. A strategy may schedule several
    # matchups concurrently (the driver fans the batch out under one
    # ``asyncio.gather``). Without a shared gate each matchup would mint its
    # own ``Semaphore(parallelism)``, so N concurrent matchups could run
    # ``N × parallelism`` board units at once — overshooting the operator's
    # parallelism intent and the LLM endpoint's concurrency. Sized as a
    # single matchup's cap would be, so a round with one matchup draws the
    # same cap it would alone.
    unit_semaphore = asyncio.Semaphore(max(1, int(field_round.config.parallelism)))
    run_one = partial(run_field_matchup, field_round, candidates, tallies, unit_semaphore)

    _open_tournament_envelopes(field_round, candidates)

    # Opt-in Bradley--Terry promotion pre-gate (crown on evidence). When
    # ``promote_confidence_threshold`` is set in the structure params, the
    # driver holds a crowning promote until the fitted rating clears the
    # confidence bar AND the CIs separate, spending closest-CI replicates in
    # between (the defer→replicate loop). Unset leaves the strategy's
    # decision unchanged.
    pre_gate: EvidencePreGate | None = None
    replicate_duel = None
    on_inconclusive = None
    bt_threshold = read_promote_confidence_threshold(field_round.tournament_spec.params)
    if bt_threshold is not None:
        pre_gate = EvidencePreGate(
            threshold=bt_threshold,
            replicate_budget=read_replicate_budget(field_round.tournament_spec.params),
        )
        # Each extra crowning-pair duel runs through the SAME board-unit
        # runner and gate every other duel uses, so a replicate is scored
        # identically to the original duel. The reserved slot, the matchup
        # id that encodes it, and the score-cache suppression belong to the
        # factory rather than to this round — one implementation of the
        # ReplicateDuel contract, exercised by the decision-procedure
        # oracle at the same seam production drives it from.
        replicate_duel = make_evidence_replicate_duel(run_one)
        on_inconclusive = partial(record_inconclusive_duel, field_round, candidates)

    try:
        evaluation = await evaluate_tournament(
            field_round.strategy,
            request_field=partial(request_field, candidates),
            run_matchup=run_one,
            on_progress=partial(publish_live_structure, field_round, candidates),
            pre_gate=pre_gate,
            replicate_duel=replicate_duel,
            on_inconclusive=on_inconclusive,
        )
    except _InfrastructureRoundDeferred as deferred:
        _clear_active_tournament(field_round.workspace_root)
        return _defer_round_infra_outage(
            workspace_root=field_round.workspace_root,
            epoch_id=field_round.epoch_id,
            parent_id=field_round.parent_id,
            next_id=candidates.base_generation_id,
            board=field_round.board,
            round_index=field_round.round_index,
            infra_aborted=deferred.aborted_runs,
            infra_threshold=deferred.threshold,
            beater=field_round.beater,
            round_log=field_round.round_log,
        )
    except Exception:
        _clear_active_tournament(field_round.workspace_root)
        raise

    gate_evidence: dict[str, Any] | None = None
    if evaluation.evidence is not None:
        gate_evidence = _emit_evidence_trail(field_round, candidates, evaluation.evidence)
    return FieldExecution(
        decision=evaluation.decision,
        gate_evidence=gate_evidence,
        aggregates=tallies.aggregates,
        raw_results=tallies.raw_results,
        # Resolve a single round-level champion-eval provenance from the
        # CHAMPION's accumulated cached-vs-fresh board-unit tally. ``full``
        # when fast was not requested; ``fast`` when the champion was reused
        # for every board unit it needed (no fresh champion run this round);
        # ``fast-degraded`` when the champion had to run live at least once
        # (the seed/first champion, or a not-yet-covered subset) to seed the
        # cache. Recorded on each challenger's OutcomeRecord for the journal
        # — never a contract input.
        champion_eval_mode=_resolve_round_champion_mode(
            tallies.champion_cached_units,
            tallies.champion_fresh_units,
            fast_requested=field_round.fast_mode,
        ),
    )


__all__ = [
    "FieldExecution",
    "RoundTallies",
    "execute_field_tournament",
    "publish_live_structure",
    "record_inconclusive_duel",
    "request_field",
    "run_field_matchup",
]
