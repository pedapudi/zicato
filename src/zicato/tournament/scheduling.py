"""Board-unit schedulers for the tournament runner.

The unit of scheduling is a **board unit**: one per board entry. This
module owns the concurrency fan-out over board units — the "tournament
hall" — and the cache-first evaluation of each unit:

* :class:`_IncrementalScorer` — folds a settled board unit's losses into
  a running partial aggregate so the live dashboard climbs as a round
  runs;
* :func:`_run_full_board_unit` / :func:`_run_fast_board_unit` — run one
  entry as a board unit (champion + challenger concurrently in full mode,
  challenger alone in fast mode);
* :func:`_run_board_units_full` / :func:`_run_board_units_fast` share
  :func:`_schedule_board_units` for concurrency and budget enforcement;
* :func:`_run_unit_cache_first` — the single cache-first choke point
  through which EVERY board unit is evaluated;
* :func:`_run_replicate_slots_full` / :func:`_run_replicate_slots_fast` —
  the overlapped replicate-slot schedulers, which run every slot of a
  matchup against ONE semaphore rather than one slot at a time;
* :func:`_run_replicated` — the replication loop that averages N paired
  runs.

Extracted verbatim from :mod:`zicato.tournament.runner`. The board-unit
evaluator runs through ``_run_single``, which stays in the runner and is
the in-place monkeypatch anchor the whole test suite swaps. So
:func:`_run_unit_cache_first` resolves it through the runner module's
namespace (a late attribute access, lazily imported to avoid a cycle)
rather than a bound import — patching ``runner._run_single`` therefore
still drives this scheduler unchanged. runner.py re-exports this module's
public surface so existing ``from zicato.tournament.runner import ...``
imports keep working.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import replace
from pathlib import Path
from typing import Any, TypeVar

from zicato.core import (
    BoardEntry,
    Generation,
    LossProfile,
    RuntimeConfig,
    ScoringWeights,
    Side,
    is_infra_abort_cause,
    run_id_for_unit,
)
from zicato.core.measurement import (
    UNKNOWN_SEED,
    BaseSeed,
    MeasurementDraw,
    validate_measurement_interval,
)
from zicato.runtime.lock import WorkspaceLock
from zicato.tournament.scoring import aggregate_generation_score
from zicato.tournament.scoring import (
    fold_matchup_replicates as _fold_replicate_runs,
)
from zicato.tournament.unit_cache import (
    _persist_unit_loss,
    _record_provenance,
    _resolve_cached_unit,
    _skipped_unit_loss,
    _unit_loss_path,
    _UnitProvenance,
    record_unit_attempt,
)
from zicato.tournament.worker_transport import _runtime_state, _stamp_replicate_index
from zicato.util.async_tasks import gather_owned

log = logging.getLogger("zicato.tournament.runner")

# One board unit's result: a (champion, challenger) pair in full mode, a
# challenger LossProfile in fast mode. The replicate-slot chains schedule
# either without reading it.
_UnitResultT = TypeVar("_UnitResultT")


# A board unit is immutable under a fixed contract, but several matchups of one
# racing rung need the SAME champion unit and look in the same event-loop turn —
# so all of them see a cold cache and all of them launch a worker. This map
# holds, per unit key, an event the running caller sets when its evaluation has
# settled; a caller that finds an entry waits and then re-reads the cache
# instead of launching its own worker. The on-disk cache stays the ONLY source
# of reuse: a waiter reuses a result iff that result was persisted, so an infra
# abort (never cached, by design) is re-attempted rather than fanned out, and
# a failed or cancelled evaluation leaves the waiter a correct MISS.
# Forced reruns wait for the same slot's writer, then execute independently.
_inflight_cacheable_units: dict[tuple[str, str, str, str, int, BaseSeed], asyncio.Event] = {}


def _cacheable_unit_key(
    workspace_root: Path,
    epoch_id: str,
    generation_id: str,
    entry_id: str,
    replicate_index: int,
    base_seed: BaseSeed = UNKNOWN_SEED,
) -> tuple[str, str, str, str, int, BaseSeed]:
    """Return the in-process single-flight key for one cacheable board unit."""
    return (
        str(workspace_root.resolve()),
        epoch_id,
        generation_id,
        entry_id,
        replicate_index,
        base_seed,
    )


async def _run_single(
    *,
    writer: WorkspaceLock,
    adapter: Any,
    generation: Generation,
    entry: BoardEntry,
    weights: ScoringWeights,
    config: RuntimeConfig,
    workspace_root: Path,
    epoch_id: str,
    side: str,
    match_id: str = "",
) -> LossProfile:
    """Evaluate ONE board unit through the runner's ``_run_single``.

    The actual run driver lives in :mod:`zicato.tournament.runner` and is
    the single in-place monkeypatch anchor the test suite swaps via
    ``runner._run_single``. This thin delegator resolves it by ATTRIBUTE
    ACCESS on the runner module object (not a bound import) so a
    ``monkeypatch.setattr(runner, "_run_single", ...)`` still reaches the
    scheduler. The import is function-local so there is no import-time
    cycle — the runner imports this module at load, this module reaches
    back into the runner only when a unit actually runs.
    """
    from zicato.tournament import runner  # noqa: PLC0415

    run_single: Any = runner._run_single
    loss: LossProfile = await run_single(
        writer=writer,
        adapter=adapter,
        generation=generation,
        entry=entry,
        weights=weights,
        config=config,
        workspace_root=workspace_root,
        epoch_id=epoch_id,
        side=side,
        match_id=match_id,
    )
    return loss


class _IncrementalScorer:
    """Folds a board unit's losses into a running partial aggregate ASAP.

    Each board unit (champion + challenger in full mode, challenger-only
    in fast mode) calls :meth:`record` the instant its run(s) settle —
    on the SAME concurrency fan-out as the runs themselves, NOT batched
    after every board has finished. ``record`` accumulates that unit's
    per-entry :class:`LossProfile` instances, re-runs
    :func:`aggregate_generation_score` over everything seen so far, and
    rewrites the running partial aggregate onto the live
    :class:`~zicato.runtime.state.ActiveTournament` record. A reader (the
    dashboard) therefore sees a real server-side ``scalar`` climb as the
    tournament runs rather than 0.00 until the round ends.

    The accumulators are plain lists guarded by an :class:`asyncio.Lock`. The
    lock is not strictly required while the runner stays single-threaded — a
    coroutine body runs uninterrupted between ``await`` points — but it makes
    the read-modify-recompute-persist sequence an explicit critical section, so
    a future move of any part of it onto a thread (or an interleaving ``await``
    added inside ``record``) cannot corrupt the running aggregate. The state
    write is strictly best-effort: a missing runtime-state module or an I/O
    error is swallowed, as every other dashboard-facing write in this module
    is; incremental scoring must never abort a run.
    """

    __slots__ = (
        "_writer",
        "_weights",
        "_champion",
        "_challenger",
        "_lock",
        "_state",
        "_champion_id",
        "_challenger_id",
        "_board_total",
    )

    def __init__(
        self,
        weights: ScoringWeights,
        *,
        writer: WorkspaceLock,
        champion_id: str = "",
        challenger_id: str = "",
        board_total: int = 0,
    ) -> None:
        self._writer = writer
        self._weights = weights
        self._champion: list[LossProfile] = []
        self._challenger: list[LossProfile] = []
        self._lock = asyncio.Lock()
        # The two competitors' generation ids + the board size this unit
        # set runs over — threaded so ``record`` can write the per-generation
        # live PROJECTED standing (``boards_done`` / ``boards_total``)
        # alongside the running partial aggregate. Empty / 0 turns the
        # projected write into a cheap no-op (the gauntlet seed-scoring path
        # has no live envelope to project onto).
        self._champion_id = str(champion_id or "")
        self._challenger_id = str(challenger_id or "")
        self._board_total = int(board_total or 0)
        # Resolve the runtime-state module once; ``None`` turns every
        # persist into a cheap no-op (no-runtime-state environment).
        rt = _runtime_state()
        self._state: Any = rt[0] if rt is not None else None

    async def record(
        self,
        *,
        champion_loss: LossProfile | None = None,
        challenger_loss: LossProfile | None = None,
    ) -> None:
        """Fold one settled board unit's losses into the partial aggregate.

        ``champion_loss`` is ``None`` for a fast-mode unit (challenger
        only). Re-aggregates both sides over everything recorded so far
        and persists the running partial aggregate onto the
        ``ActiveTournament`` record.
        """
        if self._state is None:
            return
        async with self._lock:
            if champion_loss is not None:
                self._champion.append(champion_loss)
            if challenger_loss is not None:
                self._challenger.append(challenger_loss)
            champion_agg = (
                aggregate_generation_score(list(self._champion), self._weights)
                if self._champion
                else None
            )
            challenger_agg = (
                aggregate_generation_score(list(self._challenger), self._weights)
                if self._challenger
                else None
            )
            try:
                self._state.update_tournament_partial_aggregate(
                    self._writer,
                    champion_agg=champion_agg,
                    challenger_agg=challenger_agg,
                )
            except Exception as exc:  # noqa: BLE001 — partial scoring is best-effort
                log.debug("partial-aggregate persist skipped: %s", exc)
            # Live PROJECTED standing per in-flight competitor: the same
            # running aggregate, keyed by generation_id, with the boards-so-
            # far / boards-total progress folded in so the dashboard can mark
            # the row "projected" and grow a scored board-progress sub-bar.
            # Only written when the gen ids were threaded (non-gauntlet /
            # live-envelope path); the seed-scoring path leaves them empty,
            # so this stays byte-identical there.
            #
            # NB (racing per-lane live_progress, B1): this scorer is per-DUEL —
            # a racing rung runs N concurrent champion-vs-challenger duels, each
            # with its OWN scorer, so the champion's ``projected`` row here is
            # re-aggregated over only THIS duel's boards (concurrent duels race
            # to rewrite it). The authoritative per-LANE rung progress is NOT
            # reconstructed from this per-duel map: the racing STRATEGY owns the
            # rung's ``live_progress`` topology and the orchestrator overlays
            # this ``projected`` map onto it per lane (see
            # ``_overlay_projected_live_progress``). Keeping the scorer per-duel
            # (rather than a per-rung union champion scorer) is the lower-risk
            # path — it leaves the gate's per-duel aggregate path and the
            # scoring/gate behaviour untouched; the topology composes additively
            # on top.
            projected: dict[str, dict[str, Any]] = {}
            if self._champion_id and champion_agg is not None:
                projected[self._champion_id] = self._projection_row(
                    champion_agg, len(self._champion)
                )
            if self._challenger_id and challenger_agg is not None:
                projected[self._challenger_id] = self._projection_row(
                    challenger_agg, len(self._challenger)
                )
            if not projected:
                return
            try:
                self._state.update_tournament_projected(self._writer, projected)
            except Exception as exc:  # noqa: BLE001 — projected scoring is best-effort
                log.debug("projected-standing persist skipped: %s", exc)

    def _projection_row(self, agg: dict[str, Any], boards_done: int) -> dict[str, Any]:
        """One ``{scalar, boards_done, boards_total, pass_rate}`` row.

        ``boards_total`` falls back to ``boards_done`` when the board size
        was not threaded (so progress reads as complete rather than a
        misleading 0/0); the dashboard treats ``boards_done < boards_total``
        as still-in-flight.
        """
        total = self._board_total if self._board_total > 0 else int(boards_done)
        return {
            "scalar": float(agg.get("scalar", 0.0)),
            "boards_done": int(boards_done),
            "boards_total": int(total),
            "pass_rate": float(agg.get("pass_rate", 1.0)),
        }


async def _run_full_board_unit(
    *,
    writer: WorkspaceLock,
    adapter: Any,
    parent_gen: Generation,
    child_gen: Generation,
    entry: BoardEntry,
    weights: ScoringWeights,
    config: RuntimeConfig,
    workspace_root: Path,
    epoch_id: str,
    scorer: _IncrementalScorer | None = None,
    match_id: str = "",
    replicate_index: int = 0,
    force_fresh: bool = False,
    parent_force_fresh: bool | None = None,
    provenance: dict[str, _UnitProvenance] | None = None,
) -> tuple[LossProfile, LossProfile]:
    """Run ONE board entry's champion + challenger concurrently.

    A board unit in full mode owns both sides of a single board entry.
    This launches the champion (``parent_gen``) run and the challenger
    (``child_gen``) run **simultaneously** — two :func:`_run_single`
    coroutines started together under one :func:`asyncio.gather` — and
    does not return until BOTH have settled.

    ``force_fresh`` governs the CHILD (challenger) side; ``parent_force_fresh``
    governs the PARENT (champion) side independently, defaulting to
    ``force_fresh`` when ``None`` (the uniform, back-compat behaviour). The
    gauntlet champion is immutable within an epoch, so ``run_tournament``
    passes ``parent_force_fresh=False`` to cache-READ the champion (it was
    scored in a prior round / its seed) while still force-freshing the child
    — except under ``--mode full``, which re-samples BOTH sides for noise.

    The two runs are safely concurrent: :func:`_run_single` spawns each
    in its OWN subprocess worker, each pointed at its OWN per-run
    ephemeral snapshot checkout (a distinct ``ztw-snap-*`` temp tree,
    see :func:`zicato.tournament.worker_transport._checkout_run_snapshot`)
    and writing to a distinct ``run_id`` (via ``run_id_for_unit``; the two
    generations differ). So nothing — snapshot checkout,
    ``active_runs`` state file, ``loss.json`` — is shared between the
    champion and challenger of the same entry.

    ``return_exceptions=True`` keeps a failing side from cancelling its
    in-flight sibling mid-subprocess (which would orphan a worker and
    skip its ``finally`` cleanup); both sides are allowed to finish, and
    only then is a champion-side failure (then a challenger-side one)
    re-raised. Returns ``(parent_loss, child_loss)``.

    ``scorer`` — when supplied — is folded the instant THIS unit's two
    runs settle, BEFORE the unit returns. Scoring therefore happens on
    the same concurrency fan-out as the runs: a finished board's score
    materialises while sibling boards are still running, rather than
    being batched after every board completes. Folding is skipped only
    when a side raised — the failing unit is re-raised to the caller
    instead, which treats it as a hard tournament error.
    """
    # The champion (parent) side may be cache-read independently of the
    # challenger: ``parent_force_fresh`` defaults to the shared ``force_fresh``
    # (uniform behaviour) but ``run_tournament`` overrides it to False so the
    # immutable champion is reused rather than re-run every round.
    validate_measurement_interval(replicate_index, 1)
    effective_parent_force_fresh = force_fresh if parent_force_fresh is None else parent_force_fresh
    parent_result, child_result = await gather_owned(
        _run_unit_cache_first(
            writer=writer,
            adapter=adapter,
            generation=parent_gen,
            entry=entry,
            weights=weights,
            config=config,
            workspace_root=workspace_root,
            epoch_id=epoch_id,
            side=Side.PARENT,
            replicate_index=replicate_index,
            match_id=match_id,
            force_fresh=effective_parent_force_fresh,
            provenance=provenance,
        ),
        _run_unit_cache_first(
            writer=writer,
            adapter=adapter,
            generation=child_gen,
            entry=entry,
            weights=weights,
            config=config,
            workspace_root=workspace_root,
            epoch_id=epoch_id,
            side=Side.CHILD,
            replicate_index=replicate_index,
            match_id=match_id,
            force_fresh=force_fresh,
            provenance=provenance,
        ),
        return_exceptions=True,
    )
    # Surface a champion-side failure first, then a challenger-side one —
    # both runs have already settled (their workers + cleanup finished).
    if isinstance(parent_result, BaseException):
        raise parent_result
    if isinstance(child_result, BaseException):
        raise child_result
    # Score this board unit the instant it settles — concurrently with
    # the sibling board units still running — so the dashboard's partial
    # aggregate reflects a finished board ASAP rather than at round end.
    if scorer is not None:
        await scorer.record(champion_loss=parent_result, challenger_loss=child_result)
    return parent_result, child_result


async def _run_fast_board_unit(
    *,
    writer: WorkspaceLock,
    adapter: Any,
    child_gen: Generation,
    entry: BoardEntry,
    weights: ScoringWeights,
    config: RuntimeConfig,
    workspace_root: Path,
    epoch_id: str,
    scorer: _IncrementalScorer | None = None,
    match_id: str = "",
    replicate_index: int = 0,
    force_fresh: bool = False,
    provenance: dict[str, _UnitProvenance] | None = None,
) -> LossProfile:
    """Run ONE board entry as a fast-mode board unit (challenger only).

    The fast-mode twin of :func:`_run_full_board_unit`: fast mode reuses
    the champion's cached aggregate, so a unit is the challenger run
    alone. ``scorer`` — when supplied — is folded the instant the run
    settles, BEFORE the unit returns, so a finished board's score
    materialises while sibling boards are still in flight.

    The side label is ``child`` for the rare case an ActiveTournament file
    does exist, and a benign no-op otherwise.
    """
    validate_measurement_interval(replicate_index, 1)
    child_loss = await _run_unit_cache_first(
        writer=writer,
        adapter=adapter,
        generation=child_gen,
        entry=entry,
        weights=weights,
        config=config,
        workspace_root=workspace_root,
        epoch_id=epoch_id,
        side=Side.CHILD,
        replicate_index=replicate_index,
        match_id=match_id,
        force_fresh=force_fresh,
        provenance=provenance,
    )
    if scorer is not None:
        await scorer.record(challenger_loss=child_loss)
    return child_loss


def _skip_unit_side(
    *,
    generation: Generation,
    entry: BoardEntry,
    weights: ScoringWeights,
    match_id: str,
    workspace_root: Path,
    epoch_id: str,
    replicate_index: int,
    side_force_fresh: bool,
    provenance: dict[str, _UnitProvenance] | None,
    base_seed: BaseSeed = UNKNOWN_SEED,
) -> tuple[LossProfile, bool]:
    """Record ONE side of an un-launched board unit under a spent budget.

    A unit ALREADY in the cache costs nothing, so it is reused verbatim (a
    budget never clobbers a good result and the cache stays consistent). A
    genuine miss records a scheduling omission in an attempt file. The
    omission does not populate the measurement cache or count as a fresh run.
    Returns ``(loss, was_skipped)`` for the scheduler's skip tally.
    """
    cached = (
        None
        if side_force_fresh
        else _resolve_cached_unit(
            workspace_root=workspace_root,
            epoch_id=epoch_id,
            generation_id=generation.id,
            entry_id=entry.id,
            replicate_index=replicate_index,
            base_seed=base_seed,
        )
    )
    if cached is not None:
        _record_provenance(provenance, generation.id, cached=True)
        return cached, False
    loss = _skipped_unit_loss(
        generation=generation,
        entry=entry,
        epoch_id=epoch_id,
        match_id=match_id,
    )
    record_unit_attempt(
        workspace_root=workspace_root,
        epoch_id=epoch_id,
        generation_id=generation.id,
        entry_id=entry.id,
        replicate_index=replicate_index,
        loss=loss,
        base_seed=base_seed,
    )
    return loss, True


def _token_budget_spent(config: RuntimeConfig) -> bool:
    """Whether the round's token ledger (when bound) is exhausted. LATCHING.

    The per-round token budget's would-launch check
    (:attr:`~zicato.core.runtime.RuntimeConfig.max_tokens_per_round`): the
    schedulers consult it between board units / replicate slots and stop
    LAUNCHING once the budget is spent — never mid-unit. ``None`` (the
    knob off — the default) is always ``False`` with no ledger even
    consulted, so the un-opted-in path is byte-identical. A ``True``
    latches the ledger's ``clipped`` flag, which is how the orchestrator
    learns the round was token-clipped (the health finding).
    """
    ledger = config.token_ledger
    if ledger is None:
        return False
    return bool(ledger.check_and_clip())


def _effective_unit_semaphore(
    unit_semaphore: asyncio.Semaphore | None, config: RuntimeConfig
) -> asyncio.Semaphore:
    """Resolve the concurrency gate for a board-unit runner.

    Returns the caller-supplied ``unit_semaphore`` when present — the
    cross-matchup case, where one semaphore is shared across every matchup
    of a round so the round runs under ONE global concurrency cap. When it
    is ``None`` (every direct / gauntlet caller) a fresh
    ``Semaphore(config.parallelism)`` is minted, so each runner caps its own
    concurrency.
    """
    if unit_semaphore is not None:
        return unit_semaphore
    return asyncio.Semaphore(config.parallelism)


async def _schedule_board_units(
    *,
    board: list[BoardEntry],
    config: RuntimeConfig,
    match_id: str,
    run_unit: Callable[[BoardEntry], Awaitable[_UnitResultT]],
    skip_unit: Callable[[BoardEntry], tuple[_UnitResultT, bool]],
    matchup_deadline: float | None = None,
    unit_semaphore: asyncio.Semaphore | None = None,
) -> tuple[list[_UnitResultT], int]:
    """Schedule entries in board order and settle all admitted work before raising.

    Without a deadline, check tokens after acquiring each concurrency permit.
    A deadline admits whole batches and checks both budgets between batches.
    Existing measurements remain reusable after either budget expires.
    """
    from zicato.telemetry.meta_loop import SPAN_MATCHUP, meta_span

    semaphore = _effective_unit_semaphore(unit_semaphore, config)
    skipped = 0

    async def bounded(entry: BoardEntry) -> _UnitResultT:
        nonlocal skipped
        async with (
            meta_span(
                entry.id, kind=SPAN_MATCHUP, meta={"entry_id": entry.id, "match_id": match_id}
            ),
            semaphore,
        ):
            if matchup_deadline is None and _token_budget_spent(config):
                result, _ = skip_unit(entry)
                skipped += 1
                return result
            return await run_unit(entry)

    batch_size = max(1, config.parallelism) if matchup_deadline is not None else max(1, len(board))
    results: list[_UnitResultT] = []
    budget_spent = False
    for start in range(0, len(board), batch_size):
        batch = board[start : start + batch_size]
        if matchup_deadline is not None and not budget_spent:
            budget_spent = time.monotonic() >= matchup_deadline or _token_budget_spent(config)
        if budget_spent:
            for entry in batch:
                result, was_skipped = skip_unit(entry)
                results.append(result)
                skipped += int(was_skipped)
            continue
        settled = await gather_owned(*(bounded(entry) for entry in batch), return_exceptions=True)
        for settled_result in settled:
            if isinstance(settled_result, BaseException):
                raise settled_result
            results.append(settled_result)
    return results, skipped


async def _run_board_units_full(
    *,
    writer: WorkspaceLock,
    adapter: Any,
    parent_gen: Generation,
    child_gen: Generation,
    board: list[BoardEntry],
    weights: ScoringWeights,
    config: RuntimeConfig,
    workspace_root: Path,
    epoch_id: str,
    match_id: str = "",
    replicate_index: int = 0,
    force_fresh: bool = False,
    parent_force_fresh: bool | None = None,
    provenance: dict[str, _UnitProvenance] | None = None,
    matchup_deadline: float | None = None,
    unit_semaphore: asyncio.Semaphore | None = None,
) -> tuple[dict[str, LossProfile], dict[str, LossProfile]]:
    """Measure both candidates with shared concurrency and budget scheduling."""
    validate_measurement_interval(replicate_index, 1)
    scorer = _IncrementalScorer(
        weights,
        writer=writer,
        champion_id=parent_gen.id,
        challenger_id=child_gen.id,
        board_total=len(board),
    )
    effective_parent_force_fresh = force_fresh if parent_force_fresh is None else parent_force_fresh

    async def run_unit(entry: BoardEntry) -> tuple[LossProfile, LossProfile]:
        return await _run_full_board_unit(
            writer=writer,
            adapter=adapter,
            parent_gen=parent_gen,
            child_gen=child_gen,
            entry=entry,
            weights=weights,
            config=config,
            workspace_root=workspace_root,
            epoch_id=epoch_id,
            scorer=scorer,
            match_id=match_id,
            replicate_index=replicate_index,
            force_fresh=force_fresh,
            parent_force_fresh=parent_force_fresh,
            provenance=provenance,
        )

    def skip_unit(entry: BoardEntry) -> tuple[tuple[LossProfile, LossProfile], bool]:
        parent_loss, parent_skipped = _skip_unit_side(
            generation=parent_gen,
            entry=entry,
            weights=weights,
            match_id=match_id,
            workspace_root=workspace_root,
            epoch_id=epoch_id,
            replicate_index=replicate_index,
            side_force_fresh=effective_parent_force_fresh,
            provenance=provenance,
            base_seed=config.seed,
        )
        child_loss, child_skipped = _skip_unit_side(
            generation=child_gen,
            entry=entry,
            weights=weights,
            match_id=match_id,
            workspace_root=workspace_root,
            epoch_id=epoch_id,
            replicate_index=replicate_index,
            side_force_fresh=force_fresh,
            provenance=provenance,
            base_seed=config.seed,
        )
        return (parent_loss, child_loss), parent_skipped or child_skipped

    results, skipped = await _schedule_board_units(
        board=board,
        config=config,
        match_id=match_id,
        run_unit=run_unit,
        skip_unit=skip_unit,
        matchup_deadline=matchup_deadline,
        unit_semaphore=unit_semaphore,
    )
    if skipped:
        log.warning(
            "matchup %s: evaluation budget reached; skipped %d/%d board units; "
            "unstarted attempts are recorded and the aggregate is incomplete",
            match_id or "(untagged)",
            skipped,
            len(board),
        )
    return (
        {entry.id: result[0] for entry, result in zip(board, results, strict=True)},
        {entry.id: result[1] for entry, result in zip(board, results, strict=True)},
    )


async def _run_board_units_fast(
    *,
    writer: WorkspaceLock,
    adapter: Any,
    child_gen: Generation,
    board: list[BoardEntry],
    weights: ScoringWeights,
    config: RuntimeConfig,
    workspace_root: Path,
    epoch_id: str,
    match_id: str = "",
    replicate_index: int = 0,
    force_fresh: bool = False,
    provenance: dict[str, _UnitProvenance] | None = None,
    unit_semaphore: asyncio.Semaphore | None = None,
) -> dict[str, LossProfile]:
    """Measure the challenger while the caller retains the champion's score."""
    validate_measurement_interval(replicate_index, 1)
    scorer = _IncrementalScorer(
        weights,
        writer=writer,
        challenger_id=child_gen.id,
        board_total=len(board),
    )

    async def run_unit(entry: BoardEntry) -> LossProfile:
        return await _run_fast_board_unit(
            writer=writer,
            adapter=adapter,
            child_gen=child_gen,
            entry=entry,
            weights=weights,
            config=config,
            workspace_root=workspace_root,
            epoch_id=epoch_id,
            scorer=scorer,
            match_id=match_id,
            replicate_index=replicate_index,
            force_fresh=force_fresh,
            provenance=provenance,
        )

    def skip_unit(entry: BoardEntry) -> tuple[LossProfile, bool]:
        return _skip_unit_side(
            generation=child_gen,
            entry=entry,
            weights=weights,
            match_id=match_id,
            workspace_root=workspace_root,
            epoch_id=epoch_id,
            replicate_index=replicate_index,
            side_force_fresh=force_fresh,
            provenance=provenance,
            base_seed=config.seed,
        )

    results, skipped = await _schedule_board_units(
        board=board,
        config=config,
        match_id=match_id,
        run_unit=run_unit,
        skip_unit=skip_unit,
        unit_semaphore=unit_semaphore,
    )
    if skipped:
        log.warning(
            "matchup %s: evaluation budget reached; skipped %d/%d challenger board units; "
            "unstarted attempts are recorded and the aggregate is incomplete",
            match_id or "(untagged)",
            skipped,
            len(board),
        )
    return {entry.id: result for entry, result in zip(board, results, strict=True)}


async def _run_unit_cache_first(
    *,
    writer: WorkspaceLock,
    adapter: Any,
    generation: Generation,
    entry: BoardEntry,
    weights: ScoringWeights,
    config: RuntimeConfig,
    workspace_root: Path,
    epoch_id: str,
    side: str,
    replicate_index: int = 0,
    match_id: str = "",
    force_fresh: bool = False,
    provenance: dict[str, _UnitProvenance] | None = None,
) -> LossProfile:
    """Cache-first wrapper around :func:`_run_single` for ONE board unit.

    The single choke point through which EVERY board unit — champion and
    challenger, every structure (gauntlet / racing / swiss / elim /
    round-robin), every round — is evaluated. Before executing the unit
    it consults :func:`_resolve_cached_unit`:

    * HIT → the persisted per-replicate result is reused; ``_run_single``
      is NOT called (no agent run);
    * MISS → ``_run_single`` runs the unit once, and the result is
      persisted via :func:`_persist_unit_loss` so the next need is a hit.

    ``force_fresh`` (the ``--mode full`` semantics) bypasses the cache
    read: the unit is always re-run and re-persisted (noise re-sampling /
    debugging). The cache is otherwise always-on — ``fast`` (the default)
    is simply "do not force fresh".

    Several matchups of one round can need the SAME unit concurrently (a
    racing rung's shared champion), and a cold cache answers MISS to all
    of them. Such callers are coalesced: the first evaluates, the rest
    wait for it and re-read the cache. Reuse still comes from the cache
    alone, never from the running caller's in-memory result — so an infra
    abort (deliberately never persisted) is re-attempted by the waiter
    rather than fanned out across the rung, and a failed or cancelled
    evaluation leaves the waiter a correct MISS to run itself.
    Forced reruns serialize writes to the same seed and draw, then execute
    independently. Different seeds own different files and may run together.

    ``provenance`` (when supplied) accumulates the per-generation
    cached-vs-fresh tally for the round. It counts what each caller DID:
    a coalesced waiter reuses a persisted result and launches no worker,
    so it counts as cached — the tally stays a count of evaluations
    performed rather than of requests made.
    """

    entry = _stamp_replicate_index([entry], replicate_index)[0]
    validate_measurement_interval(replicate_index, 1)

    async def _evaluate() -> LossProfile:
        return await _run_unit_after_cache_miss(
            writer=writer,
            adapter=adapter,
            generation=generation,
            entry=entry,
            weights=weights,
            config=config,
            workspace_root=workspace_root,
            epoch_id=epoch_id,
            side=side,
            replicate_index=replicate_index,
            match_id=match_id,
            provenance=provenance,
        )

    def _cached() -> LossProfile | None:
        if force_fresh:
            return None
        return _resolve_cached_unit(
            workspace_root=workspace_root,
            epoch_id=epoch_id,
            generation_id=generation.id,
            entry_id=entry.id,
            replicate_index=replicate_index,
            base_seed=config.seed,
        )

    cached = _cached()
    if cached is not None:
        _record_provenance(provenance, generation.id, cached=True)
        return cached

    # Cold cache. Another caller in this process may already be evaluating this
    # exact unit (the racing rung's shared champion); wait for it and re-read
    # rather than launching a duplicate worker. Its result counts as a genuine
    # cache hit — this caller ran nothing — but ONLY once it is on disk: a
    # settled evaluation that persisted nothing (an infra abort, a failure, a
    # cancellation) leaves the cache cold, so the loop falls through and this
    # caller becomes the one that evaluates. The re-check is a plain loop over
    # ``get`` because a settling caller pops its key before setting the event.
    key = _cacheable_unit_key(
        workspace_root, epoch_id, generation.id, entry.id, replicate_index, config.seed
    )
    while (settled := _inflight_cacheable_units.get(key)) is not None:
        await settled.wait()
        cached = _cached()
        if cached is not None:
            _record_provenance(provenance, generation.id, cached=True)
            return cached

    # No await between the miss above and claiming the key, so exactly one
    # caller per unit key evaluates and the rest wait.
    settled = asyncio.Event()
    _inflight_cacheable_units[key] = settled
    try:
        return await _evaluate()
    finally:
        # Release the waiters on EVERY exit — return, raise, or cancellation —
        # so a failed evaluation can never strand a sibling matchup.
        _inflight_cacheable_units.pop(key, None)
        settled.set()


async def _run_unit_after_cache_miss(
    *,
    writer: WorkspaceLock,
    adapter: Any,
    generation: Generation,
    entry: BoardEntry,
    weights: ScoringWeights,
    config: RuntimeConfig,
    workspace_root: Path,
    epoch_id: str,
    side: str,
    replicate_index: int,
    match_id: str,
    provenance: dict[str, _UnitProvenance] | None,
) -> LossProfile:
    """Run and persist one board unit after cache reuse has been ruled out."""

    validate_measurement_interval(replicate_index, 1)
    from zicato.telemetry.meta_loop import SPAN_WORKER, meta_span  # noqa: PLC0415
    from zicato.tournament.artifacts import archive_unit_artifacts  # noqa: PLC0415

    archive_unit_artifacts(
        _unit_loss_path(
            workspace_root,
            epoch_id,
            generation.id,
            entry.id,
            replicate_index,
            base_seed=config.seed,
        )
    )

    # Worker span: the parent-side lifecycle of ONE subprocess run (only on a
    # cache MISS — a hit above ran no worker). Its goldfive session id is
    # stamped on close so a harmonograf user can cross-jump into the run's own
    # trace (HARMONOGRAF.md §7). Nests under the matchup span via the ambient
    # context var.
    run_id = run_id_for_unit(generation.id, entry.id, replicate_index, base_seed=config.seed)
    async with meta_span(
        run_id,
        kind=SPAN_WORKER,
        meta={"run_id": run_id, "side": side, "entry_id": entry.id},
    ) as _worker_span:
        loss = await _run_single(
            writer=writer,
            adapter=adapter,
            generation=generation,
            entry=entry,
            weights=weights,
            config=config,
            workspace_root=workspace_root,
            epoch_id=epoch_id,
            side=side,
            match_id=match_id,
        )
        _worker_span.set(adk_session_id=str(getattr(loss, "adk_session_id", "") or ""))
    # Per-round token accounting: every FRESH run — and only a
    # fresh run; a cache hit returned above spends nothing — folds its
    # opportunistic token count into the round's ledger. This is the ONE
    # choke point every board unit (champion, challenger, screen, evidence
    # replicate) already routes through, so the tally spans the round.
    loss = replace(
        loss, measurement=MeasurementDraw.from_index(replicate_index, base_seed=config.seed)
    )
    if loss.execution_started is None and not is_infra_abort_cause(loss.abort_cause):
        loss = replace(loss, execution_started=True)
    if config.token_ledger is not None:
        config.token_ledger.add(loss.tokens_spent)
    # Do NOT cache an INFRA abort (a parent/supervisor kill or a worker
    # crash). Persisting its worst-case loss would make it a permanent cache
    # HIT for the rest of the epoch, poisoning this unit's score off a single
    # transient blip — only ``--mode full`` would ever re-attempt it. A
    # genuine wall-clock-budget exhaustion IS cached (re-running re-hits the
    # same budget), and a cleanly-reduced run (no abort_cause) always is.
    # Skipping the persist leaves the next need a correct MISS, so re-running
    # re-attempts the unit. The provenance still counts it as a fresh (run,
    # not reused) evaluation so the journal's fast/full accounting is honest.
    if is_infra_abort_cause(loss.abort_cause):
        log.info(
            "run %s/%s r%d aborted by infra (%s); NOT caching — re-running "
            "will re-attempt the unit",
            generation.id,
            entry.id,
            replicate_index,
            loss.abort_cause,
        )
        # The profile is discarded for scoring, but the EXECUTION happened.
        # Keep it as an attempt record so the re-attempt that follows is
        # readable as a retry rather than as the unit's only run.
        record_unit_attempt(
            workspace_root=workspace_root,
            epoch_id=epoch_id,
            generation_id=generation.id,
            entry_id=entry.id,
            replicate_index=replicate_index,
            loss=loss,
        )
    else:
        _persist_unit_loss(
            workspace_root=workspace_root,
            epoch_id=epoch_id,
            generation_id=generation.id,
            entry_id=entry.id,
            replicate_index=replicate_index,
            loss=loss,
        )
    _record_provenance(provenance, generation.id, cached=False)
    return loss


def _overlap_replicate_slots(config: RuntimeConfig, matchup_deadline: float | None) -> bool:
    """Whether a matchup's replicate slots may run overlapped.

    The barrier between two replicate slots carries exactly one decision:
    whether to LAUNCH the next slot at all. Two knobs make that decision —
    the matchup wall-clock deadline and the per-round token budget — and
    each reads what the previous slot spent, so each needs the previous
    slot to have settled first. With neither knob engaged every slot runs
    in full, the boundary carries no decision, and the slots may overlap so
    a permit freed by a finished unit is taken by the next slot's unit
    instead of idling until the whole slot drains.

    With either budget enabled, slots run sequentially so each launch check
    observes completed spend. Missing units become unstarted attempts.
    """
    return matchup_deadline is None and config.token_ledger is None


async def _run_entry_replicate_chains(
    *,
    slot_boards: list[list[BoardEntry]],
    match_id: str,
    replicate_base: int,
    semaphore: asyncio.Semaphore,
    run_unit: Callable[[BoardEntry, int], Awaitable[_UnitResultT]],
) -> list[list[_UnitResultT]]:
    """Run every entry's replicate slots as one chain, all chains at once.

    The fan-out is inverted relative to the sequential path: instead of one
    gather per replicate slot over the entries, there is one chain per
    ENTRY, and a chain runs that entry's slots in order. Every unit still
    takes ``semaphore``, so ``parallelism`` board units remain the ceiling —
    the overlap changes WHICH unit takes a freed permit, never how many
    exist.

    Running an entry's own slots in order is the ordering rule the overlap
    is built on: no unit of ``(entry, slot N+1)`` starts before
    ``(entry, slot N)`` has settled. It keeps a matchup's in-flight units
    distinct in ``(generation, entry, replicate)`` — the key the run
    identity, the unit cache, and the in-process coalescing map all use —
    and it costs nothing the sequential path did not already cost: both cap
    the concurrency at ``min(parallelism, board size)``.

    ``slot_boards`` holds one board per slot (the replicate index stamped
    onto each entry's context), all in the SAME entry order;
    ``slot_boards[offset][position]`` is therefore entry ``position`` as
    slot ``offset`` must run it. ``run_unit`` receives that entry and its
    absolute replicate index (``replicate_base + offset``).

    A failing unit ends its own chain and is re-raised — in board order —
    only after every chain has settled (``return_exceptions=True``), never
    by cancelling a sibling chain: a cancelled sibling would orphan a
    subprocess worker mid-run and skip its cleanup.

    Returns the results ENTRY-major (``[position][offset]``); the caller
    transposes to whatever shape it folds.
    """
    from zicato.telemetry.meta_loop import SPAN_MATCHUP, meta_span  # noqa: PLC0415

    board_size = len(slot_boards[0]) if slot_boards else 0

    async def _chain(position: int) -> list[_UnitResultT]:
        results: list[_UnitResultT] = []
        for offset, slot_board in enumerate(slot_boards):
            entry = slot_board[position]
            # Matchup span before the semaphore, so the gap to the first
            # worker child reads as the queue wait (HARMONOGRAF.md §7) — the
            # same bracket the sequential schedulers put around a unit.
            _mu_meta = {"entry_id": entry.id, "match_id": match_id}
            async with (
                meta_span(entry.id, kind=SPAN_MATCHUP, meta=_mu_meta),
                semaphore,
            ):
                results.append(await run_unit(entry, replicate_base + offset))
        return results

    chains = await gather_owned(
        *(_chain(position) for position in range(board_size)),
        return_exceptions=True,
    )
    settled: list[list[_UnitResultT]] = []
    for chain in chains:
        if isinstance(chain, BaseException):
            raise chain
        settled.append(chain)
    return settled


async def _run_replicate_slots_full(
    *,
    writer: WorkspaceLock,
    adapter: Any,
    parent_gen: Generation,
    child_gen: Generation,
    board: list[BoardEntry],
    weights: ScoringWeights,
    config: RuntimeConfig,
    workspace_root: Path,
    epoch_id: str,
    match_id: str,
    replicate_base: int,
    replicate_count: int,
    force_fresh: bool,
    parent_force_fresh: bool | None,
    provenance: dict[str, _UnitProvenance] | None,
    unit_semaphore: asyncio.Semaphore | None,
) -> list[tuple[dict[str, LossProfile], dict[str, LossProfile]]]:
    """Run every full-mode replicate slot of a matchup, slots overlapped.

    The overlapped counterpart of calling :func:`_run_board_units_full`
    once per slot. ONE semaphore and ONE scorer span all the slots:

    * the semaphore, because once the slots overlap a per-slot semaphore
      would let R slots run ``R × parallelism`` units at once;
    * the scorer, because two scorers writing disjoint subsets of the same
      live partial aggregate make the dashboard scalar jump up and down.

    The shared scorer projects over ``len(board) * replicate_count`` units,
    so its live scalar is the aggregate OF ALL REPLICATE UNITS SEEN SO FAR,
    not of the settled fold: the fold (:func:`_average_losses`) folds an
    entry's replicates first — majority-voting ``pass_fail`` — and only
    then aggregates. The live ``pass_rate`` therefore approaches the mean
    of the per-replicate verdicts rather than the settled majority vote,
    and does not converge to the settled number. That is deliberate: the
    live projection is a progress signal, the settled fold is the
    authority, and the racing path already tolerates the same looseness in
    its per-duel scorers.

    Returns the per-slot ``(left_losses, right_losses)`` maps in SLOT
    order — replicate 0 first — which is what the fold's
    representative-replicate rule reads.
    """
    validate_measurement_interval(replicate_base, replicate_count)
    semaphore = _effective_unit_semaphore(unit_semaphore, config)
    scorer = _IncrementalScorer(
        weights,
        writer=writer,
        champion_id=parent_gen.id,
        challenger_id=child_gen.id,
        board_total=len(board) * replicate_count,
    )
    slot_boards = [
        _stamp_replicate_index(board, replicate_base + offset) for offset in range(replicate_count)
    ]

    async def _unit(entry: BoardEntry, replicate_index: int) -> tuple[LossProfile, LossProfile]:
        return await _run_full_board_unit(
            writer=writer,
            adapter=adapter,
            parent_gen=parent_gen,
            child_gen=child_gen,
            entry=entry,
            weights=weights,
            config=config,
            workspace_root=workspace_root,
            epoch_id=epoch_id,
            scorer=scorer,
            match_id=match_id,
            replicate_index=replicate_index,
            force_fresh=force_fresh,
            parent_force_fresh=parent_force_fresh,
            provenance=provenance,
        )

    chains = await _run_entry_replicate_chains(
        slot_boards=slot_boards,
        match_id=match_id,
        replicate_base=replicate_base,
        semaphore=semaphore,
        run_unit=_unit,
    )

    runs: list[tuple[dict[str, LossProfile], dict[str, LossProfile]]] = []
    for offset in range(replicate_count):
        left_losses: dict[str, LossProfile] = {}
        right_losses: dict[str, LossProfile] = {}
        for entry, chain in zip(board, chains, strict=True):
            left_losses[entry.id], right_losses[entry.id] = chain[offset]
        runs.append((left_losses, right_losses))
    return runs


async def _run_replicate_slots_fast(
    *,
    writer: WorkspaceLock,
    adapter: Any,
    child_gen: Generation,
    board: list[BoardEntry],
    weights: ScoringWeights,
    config: RuntimeConfig,
    workspace_root: Path,
    epoch_id: str,
    match_id: str = "",
    replicate_base: int = 0,
    replicate_count: int = 1,
    force_fresh: bool = False,
    provenance: dict[str, _UnitProvenance] | None = None,
    unit_semaphore: asyncio.Semaphore | None = None,
) -> list[dict[str, LossProfile]]:
    """Run every fast-mode replicate slot of a round, slots overlapped.

    The fast-mode twin of :func:`_run_replicate_slots_full`: one
    challenger run per unit, one shared semaphore and one shared scorer
    across the slots, the same per-entry slot chains. The shared semaphore
    matters most here — the fast round supplies none of its own, so a
    per-slot semaphore would be a fresh ``Semaphore(parallelism)`` per
    slot and the overlapped slots would run ``R × parallelism`` units at
    once.

    Returns the per-slot challenger loss maps in SLOT order (replicate 0
    first); see :func:`_run_replicate_slots_full` on what the shared
    scorer's live scalar means.
    """
    validate_measurement_interval(replicate_base, replicate_count)
    semaphore = _effective_unit_semaphore(unit_semaphore, config)
    scorer = _IncrementalScorer(
        weights,
        writer=writer,
        challenger_id=child_gen.id,
        board_total=len(board) * replicate_count,
    )
    slot_boards = [
        _stamp_replicate_index(board, replicate_base + offset) for offset in range(replicate_count)
    ]

    async def _unit(entry: BoardEntry, replicate_index: int) -> LossProfile:
        return await _run_fast_board_unit(
            writer=writer,
            adapter=adapter,
            child_gen=child_gen,
            entry=entry,
            weights=weights,
            config=config,
            workspace_root=workspace_root,
            epoch_id=epoch_id,
            scorer=scorer,
            match_id=match_id,
            replicate_index=replicate_index,
            force_fresh=force_fresh,
            provenance=provenance,
        )

    chains = await _run_entry_replicate_chains(
        slot_boards=slot_boards,
        match_id=match_id,
        replicate_base=replicate_base,
        semaphore=semaphore,
        run_unit=_unit,
    )

    runs: list[dict[str, LossProfile]] = []
    for offset in range(replicate_count):
        losses: dict[str, LossProfile] = {}
        for entry, chain in zip(board, chains, strict=True):
            losses[entry.id] = chain[offset]
        runs.append(losses)
    return runs


async def _run_replicated(
    *,
    writer: WorkspaceLock,
    adapter: Any,
    left_gen: Generation,
    right_gen: Generation,
    board: list[BoardEntry],
    weights: ScoringWeights,
    config: RuntimeConfig,
    workspace_root: Path,
    epoch_id: str,
    replicates: int,
    replicate_base: int = 0,
    match_id: str = "",
    fast: bool = False,
    matchup_budget_seconds: float | None = None,
    unit_semaphore: asyncio.Semaphore | None = None,
) -> tuple[dict[str, LossProfile], dict[str, LossProfile], str, dict[str, _UnitProvenance]]:
    """Run a paired board ``replicates`` times, averaging per-entry losses.

    The §9-lever-1 replication knob. ``replicates == 1`` is the current
    single-run path (it simply returns ``_run_board_units_full``'s maps
    unchanged). For ``replicates > 1`` the paired board is run N times and
    the per-entry drift losses are averaged BEFORE aggregation, so a noisy
    single run does not decide a duel on its own. Only the scalar-bearing
    ``drift_loss`` is averaged; ``pass_fail`` is taken as the majority
    (true only when a strict majority of replicates passed), which keeps
    the pass-rate monotonicity rule meaningful under replication.

    The board unit is the same unit either way — same subprocess
    isolation, same cache slotting, same failure surfacing — but the slots
    are scheduled one of two ways. With neither budget knob engaged they
    run OVERLAPPED (:func:`_run_replicate_slots_full`): all slots at once
    against one shared semaphore, so a permit a finished unit frees is
    taken by the next slot's unit rather than idling until the whole slot
    drains. With a knob engaged the slots run one at a time, because the
    boundary between them is where the knob's stop-launching decision is
    made — see :func:`_overlap_replicate_slots`.

    Cache-first board-unit evaluation (structure-agnostic)
    ------------------------------------------------------
    Every board unit ``(generation, entry, replicate)`` of BOTH sides
    routes through :func:`_run_unit_cache_first`, so the cache is the
    universal evaluator — not a champion-only shortcut. ``fast`` (the
    default ``--mode fast``) is the always-on cache: a unit already
    persisted under this epoch/contract is reused; a genuine miss runs
    once and is persisted for the next need. The champion (``left``) is
    therefore reused if already evaluated under this epoch (its seed /
    prior-round eval) and evaluated once WITH the field otherwise — and a
    competitor's board run is reused across every pairing/round/structure
    and across multiple ``--rounds`` in the same epoch.

    ``fast=False`` is ``--mode full`` — it forces a fresh evaluation of
    EVERY unit (``force_fresh``), re-running and re-persisting both sides
    regardless of any cache (noise re-sampling / debugging).

    Replicate-aware / incremental: each replicate index keys a distinct
    cache slot, so requesting R replicates when r<R already exist runs
    only the missing ``R-r`` (the cached samples are reused, never
    re-run).

    ``replicate_base`` offsets every slot: replicate ``i`` runs (and
    caches, and stamps its harness noise draw) at index ``replicate_base +
    i``. ``0`` (every tournament matchup) is byte-identical to before the
    parameter existed; the evidence pre-gate passes a RESERVED base
    (:data:`zicato.selection.evidence_gate.EVIDENCE_REPLICATE_BASE`) so its
    extra draws never read or write the canonical replicate-0 slots.

    The returned ``champion_eval_mode`` is derived from the LEFT side's
    cached-vs-fresh provenance, preserving the journal's existing
    vocabulary: ``"full"`` when fast was not requested; ``"fast"`` when
    every left unit was reused from the cache; ``"fast-degraded"`` when
    fast was requested but at least one left unit had to run live (the
    seed/first champion, or a not-yet-covered subset). The right side's
    provenance does not affect the champion-eval label.

    Returns ``(left_losses, right_losses, champion_eval_mode,
    unit_provenance)`` where ``unit_provenance`` is the per-generation
    cached-vs-fresh tally over both sides.
    """
    validate_measurement_interval(replicate_base, replicates)
    force_fresh = not fast
    replicate_count = max(1, replicates)
    provenance: dict[str, _UnitProvenance] = {}

    # Champion-eval provenance is decided from the cache state of the LEFT
    # side BEFORE any unit runs: in fast mode the duel is "fast" iff every
    # left unit (across every replicate slot it needs) is already
    # persisted under this epoch — the champion was evaluated in a prior
    # round / its seed scoring — so no left unit will run live. Otherwise
    # at least one left unit must run → "fast-degraded". Full mode
    # (force_fresh) always re-runs the champion → "full". This snapshot is
    # taken pre-run because a MISS re-persists immediately; reading it
    # afterwards would always look cached.
    if force_fresh:
        mode = "full"
    else:
        left_fully_cached = all(
            _resolve_cached_unit(
                workspace_root=workspace_root,
                epoch_id=epoch_id,
                generation_id=left_gen.id,
                entry_id=entry.id,
                replicate_index=replicate_base + r,
                base_seed=config.seed,
            )
            is not None
            for r in range(replicate_count)
            for entry in board
        )
        mode = "fast" if left_fully_cached else "fast-degraded"

    # Opt-in matchup-level wall-clock cap. The deadline spans ALL replicates
    # (it bounds the TOTAL matchup wall-clock rather than each replicate), so it is
    # computed ONCE here from a monotonic clock. ``None`` ⇒ uncapped: the
    # deadline is never consulted.
    matchup_deadline: float | None = (
        time.monotonic() + matchup_budget_seconds
        if matchup_budget_seconds is not None and matchup_budget_seconds > 0.0
        else None
    )

    runs: list[tuple[dict[str, LossProfile], dict[str, LossProfile]]] = []

    # With more than one slot and neither budget knob engaged, the slots run
    # OVERLAPPED against one shared semaphore: a permit freed by a finished
    # unit goes to the next slot's unit instead of idling until the whole
    # slot drains (see _overlap_replicate_slots for why the sequential loop
    # below is kept for the budgeted paths).
    if replicate_count > 1 and _overlap_replicate_slots(config, matchup_deadline):
        runs = await _run_replicate_slots_full(
            writer=writer,
            adapter=adapter,
            parent_gen=left_gen,
            child_gen=right_gen,
            board=board,
            weights=weights,
            config=config,
            workspace_root=workspace_root,
            epoch_id=epoch_id,
            match_id=match_id,
            replicate_base=replicate_base,
            replicate_count=replicate_count,
            force_fresh=force_fresh,
            parent_force_fresh=None,
            provenance=provenance,
            unit_semaphore=unit_semaphore,
        )
        left_folded, right_folded = _fold_replicate_runs(runs)
        return left_folded, right_folded, mode, provenance

    for replicate_offset in range(replicate_count):
        # Every requested slot contributes either measurements or explicit
        # omissions. The board scheduler records attempts after budget expiry;
        # it launches no missing units and still reuses existing measurements.
        replicate_index = replicate_base + replicate_offset
        left_losses, right_losses = await _run_board_units_full(
            writer=writer,
            adapter=adapter,
            parent_gen=left_gen,
            child_gen=right_gen,
            board=_stamp_replicate_index(board, replicate_index),
            weights=weights,
            config=config,
            workspace_root=workspace_root,
            epoch_id=epoch_id,
            match_id=match_id,
            replicate_index=replicate_index,
            force_fresh=force_fresh,
            provenance=provenance,
            matchup_deadline=matchup_deadline,
            unit_semaphore=unit_semaphore,
        )
        runs.append((left_losses, right_losses))

    left_folded, right_folded = _fold_replicate_runs(runs)
    return left_folded, right_folded, mode, provenance


__all__ = [
    "_IncrementalScorer",
    "_effective_unit_semaphore",
    "_run_board_units_fast",
    "_run_board_units_full",
    "_run_fast_board_unit",
    "_run_full_board_unit",
    "_run_replicate_slots_fast",
    "_run_replicate_slots_full",
    "_run_replicated",
    "_run_unit_cache_first",
]
