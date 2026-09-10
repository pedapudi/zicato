"""Tournament execution across isolated per-board-unit subprocesses.

Schedulers in :mod:`zicato.tournament.scheduling` apply one concurrency
cap across matchups and route every ``(generation, entry, replicate)``
through the unit cache. A cache miss reaches :mod:`zicato.tournament.worker_execution`, which
creates an ephemeral checkout, passes a replicate-keyed events/loss slot
to ``python -m zicato._tournament_worker``, enforces the budget, and
always cleans up runtime state. Full mode runs both sides concurrently. Fast
mode resolves both competitors through the replicate-keyed cache and runs only
missing units.

Detailed invariants and call topology live in
``docs/dev-guide/06-tournament-and-selection.md``.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from zicato.core import (
    BoardEntry,
    Generation,
    LossProfile,
    RuntimeConfig,
    ScoringWeights,
    Side,
)
from zicato.core.measurement import (
    TOURNAMENT_DRAW,
    MeasurementDraw,
    MeasurementPurpose,
    validate_measurement_count,
)
from zicato.driver_imports import with_workspace_imports
from zicato.runtime.lock import WorkspaceLock

# The HOST-WIDE worker permit (RUNTIME.md §5.5.7): the cross-orchestrator
# bound ``config.parallelism``'s per-process semaphore cannot provide.
from zicato.runtime.writer import workspace_writer
from zicato.tournament.gate import GateOutcome, evaluate_gate

# Governance helpers used by the scheduling boundary.
from zicato.tournament.governance import (
    _ladder_exhausted_outcome,
    _ladder_mediated_outcome,
    _regression_rejection,
    _reserve_ladder_query,
)
from zicato.tournament.ladder import disabled_holdout_record
from zicato.tournament.regression import run_regression_suite
from zicato.tournament.scheduling import (
    _overlap_replicate_slots,
    _run_board_units_fast,
    _run_board_units_full,
    _run_replicate_slots_fast,
    _run_replicated,
)
from zicato.tournament.scoring import aggregate_generation_score
from zicato.tournament.unit_cache import (
    _average_losses,
    _UnitProvenance,
)
from zicato.tournament.worker_execution import drain_worker_cleanup
from zicato.tournament.worker_transport import (
    _now_iso_utc,
    _runtime_state,
    _stamp_disable_drift,
    _stamp_judge_only,
    _stamp_measurement,
)

log = logging.getLogger("zicato.tournament.runner")


@dataclass(frozen=True, slots=True)
class TournamentResult:
    """The full output of one tournament evaluation.

    Carries the parent and child generation ids, both per-generation
    aggregate dicts (see :func:`aggregate_generation_score`), the gate
    outcome, and a per-entry mapping of the two loss profiles for
    journaling. Fully JSON-serializable via
    :func:`dataclasses.asdict` + :func:`json.dumps` with
    ``default=str``.

    ``champion_eval_mode`` records how the champion (parent / ``left``)
    side was evaluated this duel — a RUNTIME provenance field, never a
    contract input:

    * ``"full"`` — the champion was run live (full A/B, or fast was not
      requested);
    * ``"fast"`` — the champion's cached per-board scalars were reused
      and the champion was NOT executed;
    * ``"fast-degraded"`` — fast was requested but no cached champion
      aggregate covered the needed boards, so the champion was run live
      once to seed the cache.

    It carries no weight in the gate and is not folded into the contract
    hash; it exists purely so the journal can attribute champion sample
    freshness + cost per duel.
    """

    parent_generation_id: str
    child_generation_id: str
    parent_agg: dict[str, Any]
    child_agg: dict[str, Any]
    outcome: GateOutcome
    per_entry_losses: dict[str, tuple[LossProfile, LossProfile]]
    champion_eval_mode: str = "full"
    #: Additive per-generation cache provenance for THIS duel: the count
    #: of board units reused from the cache (``cached``) against those
    #: executed afresh (``fresh``), keyed by generation id. Lets a structure-
    #: agnostic caller (the orchestrator) attribute reuse to the CHAMPION
    #: specifically — a generation appears on either side across a
    #: swiss/elim field, and only the champion's reuse drives the
    #: ``champion_eval_mode`` provenance. A gauntlet/ad-hoc caller can
    #: ignore it. Empty when the caller passes no provenance.
    unit_provenance: dict[str, _UnitProvenance] = field(default_factory=dict)

    @property
    def measurement_draw(self) -> MeasurementDraw | None:
        """Return a draw only when every scored entry on both sides proves it.

        Replicate averages discard single-draw provenance. An aggregate cannot
        recover that identity from a matchup name or a requested slot.
        """
        entries = set(self.per_entry_losses)
        if (
            not entries
            or entries != set(self.parent_agg.get("per_entry", {}))
            or entries != set(self.child_agg.get("per_entry", {}))
            or self.parent_agg.get("incomplete_entries")
            or self.child_agg.get("incomplete_entries")
        ):
            return None
        draws: set[MeasurementDraw | None] = set()
        for entry_id, pair in self.per_entry_losses.items():
            for generation_id, loss in zip(
                (self.parent_generation_id, self.child_generation_id), pair, strict=True
            ):
                if (
                    not loss.execution_started
                    or loss.entry_id != entry_id
                    or loss.generation_id != generation_id
                ):
                    return None
                draws.add(loss.measurement)
        return draws.pop() if len(draws) == 1 else None

    #: The Ladder/holdout evidence block for THIS duel (OVERFITTING.md §12 #2),
    #: or ``None`` when no holdout was consulted (a small board, the split
    #: disabled, or a non-full-A/B path that does not gate on the holdout).
    #: The orchestrator copies it verbatim onto the journaled
    #: :class:`~zicato.core.types.OutcomeRecord.holdout`. Shape (stable, read
    #: by the dashboard) is documented at
    #: :func:`zicato.tournament.ladder.holdout_record`.
    holdout: dict[str, Any] | None = None
    #: THIS duel's child (challenger) HOLDOUT-slice scalar, or ``None`` when
    #: there was no holdout to measure (small board / split disabled / a path
    #: that does not run the holdout). The orchestrator pairs it with the
    #: TRAIN-slice ``child_agg["scalar"]`` to journal the per-generation
    #: ``train_loss`` / ``holdout_loss`` / ``generalization_gap``
    #: (OVERFITTING.md §12 #5). Decoupled from the Ladder's release semantics
    #: so the generalization gap is always measurable when a holdout exists.
    holdout_child_scalar: float | None = None


async def _gate_with_regression(
    *,
    parent_agg: dict[str, Any],
    child_agg: dict[str, Any],
    child_snapshot_root: Path,
    weights: ScoringWeights,
    holdout_parent_agg: dict[str, Any] | None = None,
    holdout_child_agg: dict[str, Any] | None = None,
) -> GateOutcome:
    """Apply the promote gate, prefixed by a regression-suite check.

    The regression check is a HARD GATE: when
    :attr:`ScoringWeights.regression_gate_enabled` is true, the child
    snapshot's own test suite runs as a subprocess BEFORE we evaluate
    the scoring gate. Any failure (or timeout) forces the
    :class:`GateOutcome` to ``"rejected"`` with a reason like
    ``"regression suite failed: N tests"`` — regardless of how strongly
    the child improved on drift_loss / pass_rate.

    ``parent_agg`` / ``child_agg`` are the TRAIN-slice aggregates (equal to
    the full-board aggregates when the board was not split). The optional
    ``holdout_parent_agg`` / ``holdout_child_agg`` thread the holdout slice
    into :func:`~zicato.tournament.gate.evaluate_gate` for the
    holdout-confirmation step; both ``None`` skips it (the small-board /
    disabled case) for byte-identical pre-split behaviour.

    The deltas reported on the outcome are still computed against the
    aggregate dicts so the journal can render evidence even when a
    regression-side rejection shadows the scoring signal.
    """
    from zicato.telemetry.meta_loop import SPAN_PHASE, meta_span  # noqa: PLC0415

    # The gate phase span brackets the regression suite + promote gate
    # (HARMONOGRAF.md §7). It nests under the matchup / round span in scope.
    async with meta_span("gate", kind=SPAN_PHASE):
        regression_rule = {
            "id": "regression_suite",
            "label": "Regression suite",
            "status": "skipped",
            "detail": "disabled",
            "fired": False,
        }
        if weights.regression_gate_enabled:
            regression = await run_regression_suite(
                child_snapshot_root,
                test_command=weights.regression_test_command,
                timeout_s=weights.regression_timeout_s,
            )
            regression_rule.update(
                status="pass" if regression.passed else "fail",
                detail=regression.summary,
                fired=not regression.passed,
            )
            if not regression.passed:
                outcome = _regression_rejection(parent_agg, child_agg, regression)
                return replace(
                    outcome,
                    explanation={
                        "decision": outcome.decision,
                        "reason": outcome.reason,
                        "deciding_rule": "regression_suite",
                        "rules": [regression_rule],
                        "margin": weights.promote_margin,
                        "champion_scalar": parent_agg["scalar"],
                        "challenger_scalar": child_agg["scalar"],
                        "delta_scalar": outcome.delta_scalar,
                        "delta_pass_rate": outcome.delta_pass_rate,
                        "regressed_predicate": None,
                        "regressed_namespace": None,
                    },
                )
        outcome = evaluate_gate(
            parent_agg,
            child_agg,
            weights,
            holdout_parent_agg=holdout_parent_agg,
            holdout_child_agg=holdout_child_agg,
        )
        if outcome.explanation is None:
            return outcome
        return replace(
            outcome,
            explanation={
                **outcome.explanation,
                "rules": [
                    regression_rule if item["id"] == "regression_suite" else item
                    for item in outcome.explanation["rules"]
                ],
            },
        )


@with_workspace_imports
async def run_tournament(
    *,
    adapter: Any,
    parent_gen: Generation,
    child_gen: Generation,
    board: list[BoardEntry],
    weights: ScoringWeights,
    config: RuntimeConfig,
    workspace_root: Path,
    epoch_id: str,
    disable_drift: tuple[Any, ...] = (),
    judge_only: bool = False,
    champion_force_fresh: bool = False,
    round_index: int = 0,
    total_rounds: int = 0,
    force_fresh: bool = True,
    child_diff_size: dict[str, int] | None = None,
    replicates: int = 1,
    writer: WorkspaceLock | None = None,
) -> TournamentResult:
    """Run a full A/B tournament. See module docstring.

    ``replicates`` is the §9-lever-1 replication knob on the full A/B path
    (mirroring :func:`run_matchup`): the paired board is run ``replicates``
    times — each replicate on its own per-unit cache slot (the
    ``(generation, entry, replicate)`` key) — and the per-entry drift losses
    are averaged BEFORE aggregation via the same
    :func:`~zicato.tournament.unit_cache._average_losses` the matchup runner
    uses, so a noisy single run does not decide a duel on its own. ``1``
    (this function's own default; the orchestrator threads the structure's
    resolved value) is the single-run path, where a duel rests on one draw
    per side. The champion/child force-fresh semantics below
    apply per replicate slot.

    ``child_diff_size`` is the OPT-IN parsimony / MDL input (OVERFITTING.md §5
    / §12 #4): the challenger generation's ``{added, removed, patches}`` diff
    size (see :func:`zicato.scoring.diff_complexity.diff_size`), threaded by the
    orchestrator from the child experiment's patch records. It folds a
    ``diff_complexity`` component into the CHALLENGER's scalar only when
    ``weights.experimental.diff_complexity_weight > 0``. ``None`` (every caller that does
    not opt in, and any ``diff_complexity_weight == 0.0`` contract) is
    leaves the term out entirely. The champion side never carries it, so the
    gate compares the challenger's diff against a parsimony-free baseline.

    ``force_fresh`` defaults to ``True``, under which the rigorous full A/B
    path re-evaluates BOTH sides from scratch (no cache read) so a ``--mode
    full`` round always re-samples noise. The orchestrator's conservative
    crash-resume (RUNTIME.md §4) passes ``force_fresh=False`` for the one round
    it resumes in place, so the measurement cache reuses every board
    unit the interrupted run already completed and only the unfinished entries
    re-run. Every other caller leaves the default, which re-runs every unit.

    ``disable_drift`` is the board-level drift-suppression set parsed
    from the board's ``board_meta`` header (see
    :func:`zicato.board.jsonl.load_board_with_meta`). It is stamped onto
    every board entry's :attr:`~zicato.core.BoardEntry.context` so it
    threads through to the adapter's judge assembly; an empty tuple (the
    default) leaves the board entries untouched.

    Champion (parent) cache-read
    ----------------------------
    The challenger (child) side is force-fresh here under the default
    (``force_fresh=True``) — a freshly proposed generation has no prior
    evaluation under this contract, so it must run; only the conservative
    crash-resume (``force_fresh=False``) cache-reads the child's already
    completed units (see above). The champion (parent) is IMMUTABLE within
    an epoch, so by
    default (``champion_force_fresh=False``) its per-board units are
    cache-READ: if the champion was already scored this epoch (a prior round
    / its seed-scoring) those results are reused rather than re-running the
    immutable champion every round — the §2-item-3 efficiency win. The first
    time the champion is seen it is a clean MISS and runs once (then caches),
    so a fresh epoch scores the champion once.
    ``champion_force_fresh=True`` re-samples the champion too — the
    ``--mode full`` noise-resampling semantics; fast mode (``run_fast_mode``)
    is unchanged and still reuses the champion's historical aggregate
    wholesale.

    ``round_index`` / ``total_rounds`` are threaded through from the
    orchestrator's evolve loop purely so the published
    :class:`~zicato.runtime.state.ActiveTournament` can tell the
    dashboard "round N of M". They default to ``0`` for callers (older
    tests, ad-hoc invocations) that do not run inside the multi-round
    loop; the runner's behaviour does not otherwise depend on them.
    """
    # Defense-in-depth: the runner re-checks the two-callable invariant.
    # The check happens here (and not just at config construction) so a
    # caller who hand-built a RuntimeConfig can't slip a colluding pair
    # through to the runner.
    validate_measurement_count(replicates)
    async with workspace_writer(
        workspace_root,
        writer=writer,
        instance_id=config.instance_id,
        cleanup=lambda: drain_worker_cleanup(workspace_root),
    ) as writer:
        from zicato.core import assert_distinct_callables  # noqa: PLC0415

        assert_distinct_callables(config.target_call_llm, config.evaluation_call_llm)
        from zicato.epoch.execution import bind_runtime_to_epoch  # noqa: PLC0415

        config = bind_runtime_to_epoch(config, workspace_root, epoch_id)

        # Thread the board-level disable_drift onto each entry's context so
        # the adapter (running in a subprocess worker) can suppress the named
        # built-in judges. A no-op when the board has no board_meta header.
        board = _stamp_disable_drift(board, disable_drift)
        # Same threading for the board-level judge_only flag: the adapter
        # selects no-steering evaluation per entry off this context key. A
        # no-op when judge_only is False (the default), so the steering path
        # stays byte-identical.
        board = _stamp_judge_only(board, judge_only)

        # A Ladder-enabled holdout must not execute as part of the train board:
        # the query charge is persisted only after the train gate says the duel is
        # eligible for confirmation.  Keeping the slices separate also means a
        # train rejection never consults or charges the holdout.
        from zicato.board.split import rotation_seed, split_board  # noqa: PLC0415

        split_seed = rotation_seed(weights.overfitting, epoch_id)
        train_ids, holdout_ids = split_board(board, weights.overfitting, seed=split_seed)
        train_id_set = set(train_ids)
        holdout_id_set = set(holdout_ids)
        train_board = [entry for entry in board if entry.id in train_id_set]
        holdout_board = [entry for entry in board if entry.id in holdout_id_set]

        # Best-effort tournament-state publication for the live dashboard.
        rt = _runtime_state()
        if rt is not None:
            state_mod, _ = rt
            try:
                from zicato.runtime.state import (  # noqa: PLC0415
                    ActiveTournament,
                    ActiveTournamentEntry,
                    RunStatus,
                    TournamentPhase,
                )

                now = _now_iso_utc()
                entries = [
                    ActiveTournamentEntry(entry_id=e.id, side=Side.PARENT, status=RunStatus.QUEUED)
                    for e in board
                ] + [
                    ActiveTournamentEntry(entry_id=e.id, side=Side.CHILD, status=RunStatus.QUEUED)
                    for e in board
                ]
                state_mod.write_active_tournament(
                    writer,
                    ActiveTournament(
                        tournament_id=f"tour-{parent_gen.id}-vs-{child_gen.id}-{now}",
                        parent_generation_id=parent_gen.id,
                        child_generation_id=child_gen.id,
                        epoch_id=epoch_id,
                        started_at=now,
                        entries=entries,
                        phase=TournamentPhase.RUNNING,
                        round_index=round_index,
                        total_rounds=total_rounds,
                    ),
                )
            except Exception:  # noqa: BLE001
                pass

        async def run_board_slice(
            entries: list[BoardEntry],
        ) -> tuple[dict[str, LossProfile], dict[str, LossProfile]]:
            """Evaluate one board slice with the full runner's replicate policy."""
            replicate_count = max(1, replicates)
            replicate_runs: list[tuple[dict[str, LossProfile], dict[str, LossProfile]]] = []
            for draw in range(replicate_count):
                measurement = MeasurementDraw(MeasurementPurpose.TOURNAMENT, draw, config.seed)
                run_parent, run_child = await _run_board_units_full(
                    writer=writer,
                    adapter=adapter,
                    parent_gen=parent_gen,
                    child_gen=child_gen,
                    board=entries,
                    weights=weights,
                    config=config,
                    workspace_root=workspace_root,
                    epoch_id=epoch_id,
                    measurement=measurement,
                    force_fresh=force_fresh,
                    parent_force_fresh=champion_force_fresh,
                )
                replicate_runs.append((run_parent, run_child))
            if replicate_count == 1:
                return replicate_runs[0]
            return (
                _average_losses([run[0] for run in replicate_runs]),
                _average_losses([run[1] for run in replicate_runs]),
            )

        holdout_parent_losses: dict[str, LossProfile] = {}
        holdout_child_losses: dict[str, LossProfile] = {}
        holdout_parent_agg: dict[str, Any] | None = None
        holdout_child_agg: dict[str, Any] | None = None
        holdout_block: dict[str, Any] | None = (
            None if holdout_board else dict(disabled_holdout_record())
        )
        try:
            # Train units execute first.  Only a train promotion can cross the
            # reservation boundary and schedule the holdout slice.
            parent_losses, child_losses = await run_board_slice(train_board)
            parent_agg = aggregate_generation_score(list(parent_losses.values()), weights)
            child_agg = aggregate_generation_score(
                list(child_losses.values()), weights, diff_size=child_diff_size
            )
            train_outcome = await _gate_with_regression(
                parent_agg=parent_agg,
                child_agg=child_agg,
                child_snapshot_root=child_gen.snapshot_root,
                weights=weights,
            )
            outcome = train_outcome

            if train_outcome.decision == "promoted" and holdout_board:
                reservation = None
                ladder_cfg = weights.overfitting.ladder
                if ladder_cfg.enabled:
                    ladder_state, reservation = _reserve_ladder_query(
                        workspace_root, epoch_id, ladder_cfg
                    )
                    if reservation is None:
                        outcome, holdout_block = _ladder_exhausted_outcome(
                            train_outcome=train_outcome,
                            train_child_agg=child_agg,
                            state=ladder_state,
                            weights=weights,
                        )

                if not ladder_cfg.enabled or reservation is not None:
                    holdout_parent_losses, holdout_child_losses = await run_board_slice(
                        holdout_board
                    )
                    holdout_parent_agg = aggregate_generation_score(
                        list(holdout_parent_losses.values()), weights
                    )
                    holdout_child_agg = aggregate_generation_score(
                        list(holdout_child_losses.values()),
                        weights,
                        diff_size=child_diff_size,
                    )
                    outcome, holdout_block = _ladder_mediated_outcome(
                        train_outcome=train_outcome,
                        parent_agg=parent_agg,
                        child_agg=child_agg,
                        holdout_parent_agg=holdout_parent_agg,
                        holdout_child_agg=holdout_child_agg,
                        weights=weights,
                        workspace_root=workspace_root,
                        epoch_id=epoch_id,
                        reservation=reservation,
                    )
        finally:
            if rt is not None:
                state_mod, _ = rt
                try:
                    state_mod.clear_active_tournament(writer)
                except Exception:  # noqa: BLE001
                    pass

        per_entry_losses: dict[str, tuple[LossProfile, LossProfile]] = {}
        all_parent_losses = {**parent_losses, **holdout_parent_losses}
        all_child_losses = {**child_losses, **holdout_child_losses}
        for entry_id, parent_loss in all_parent_losses.items():
            child_loss = all_child_losses.get(entry_id)
            if child_loss is not None:
                per_entry_losses[entry_id] = (parent_loss, child_loss)

        return TournamentResult(
            parent_generation_id=parent_gen.id,
            child_generation_id=child_gen.id,
            parent_agg=parent_agg,
            child_agg=child_agg,
            outcome=outcome,
            per_entry_losses=per_entry_losses,
            champion_eval_mode="full",
            holdout=holdout_block,
            holdout_child_scalar=(
                None
                if holdout_child_agg is None or holdout_child_agg.get("incomplete_entries")
                else float(holdout_child_agg["scalar"])
            ),
        )


@with_workspace_imports
async def run_fast_mode(
    *,
    adapter: Any,
    child_gen: Generation,
    board: list[BoardEntry],
    weights: ScoringWeights,
    config: RuntimeConfig,
    workspace_root: Path,
    epoch_id: str,
    parent_historical_agg: dict[str, Any],
    parent_generation_id: str,
    disable_drift: tuple[Any, ...] = (),
    judge_only: bool = False,
    round_index: int = 0,
    total_rounds: int = 0,
    replicates: int = 1,
    writer: WorkspaceLock | None = None,
) -> TournamentResult:
    """Inline keep/discard against a historical aggregate.

    Runs only the CHILD generation — the champion's cached aggregate is
    reused, which is what makes fast mode cheap. Compares the result
    against the caller-supplied ``parent_historical_agg`` — typically the
    parent's last full-mode aggregate dict cached in the journal. Same
    gate logic, so the decision shape is identical to full mode. Per-entry
    losses contain only the child side; the parent tuple slot is left
    empty by storing the child's loss in both positions IS WRONG — we
    keep parent slot ``None``-equivalent by simply omitting parent
    losses from the per-entry map. (Fast mode has no parent
    per-entry loss profiles to report.)

    ``replicates`` is the §9-lever-1 replication knob, honoured here on the
    CHALLENGER side as :func:`run_matchup` honours it under ``fast=True``: the
    child board is run ``replicates`` times, each replicate on its own per-unit
    cache slot (the ``(generation, entry, replicate)`` key) with its index
    stamped onto each entry's context, and the per-entry losses are folded
    through the same :func:`~zicato.tournament.unit_cache._average_losses`
    every other path uses. ``1`` (this function's own default; the orchestrator
    threads the structure's resolved value) is the single-run path, where a
    duel rests on one draw per side. With no token ledger bound the slots run
    OVERLAPPED against one shared semaphore
    (:func:`~zicato.tournament.scheduling._run_replicate_slots_fast`), as on
    the full path. With one bound they run one at a time. A spent per-round
    token budget records each remaining slot as an unstarted attempt. The fold
    retains these omissions, so an incomplete replicated entry cannot support
    promotion. Omitted slots require fresh execution before cache reuse.

    The asymmetry is deliberate and is NOT variance reduction on both
    sides: the champion remains ONE frozen cached draw no matter how high
    ``replicates`` goes, so the contrast has a replicated challenger
    against an unreplicated champion. That halves the noise the knob was
    bought for rather than removing it — an operator who wants independent
    draws on BOTH sides wants ``--mode full``, and this function logs the
    one-sidedness at WARNING whenever ``replicates > 1`` reaches it. Fast
    mode's own noise hedge remains the evidence gate's crowning
    confirmation.

    The evolve loop does NOT come through here: its fast rounds resolve
    both competitors' replicate slots through the unit cache
    (:func:`run_matchup` under ``fast=True``), so the frozen-champion
    asymmetry — and this warning — belong to the direct callers that keep
    it, which is ``zicato tournament run --mode fast`` and library callers
    holding a cached aggregate.

    ``disable_drift`` is the board-level drift-suppression set, stamped
    onto each board entry's context as in :func:`run_tournament`;
    an empty tuple (the default) leaves the board entries untouched.

    ``round_index`` / ``total_rounds`` are threaded through from the
    orchestrator's evolve loop purely so the published
    :class:`~zicato.runtime.state.ActiveTournament` can tell the
    dashboard "round N of M". They default to ``0`` for callers (older
    tests, ad-hoc invocations) that do not run inside the multi-round
    loop; the runner's behaviour does not otherwise depend on them.

    Mirrors :func:`run_tournament` in publishing an
    :class:`~zicato.runtime.state.ActiveTournament` to the runtime
    state before kicking off any runs and clearing it on exit, so the
    dashboard's Tournament hall renders the live board entries for a
    fast round (otherwise the hall would stay blank). Champion-side
    rows are pre-filled from the cached ``parent_historical_agg["per_entry"]``
    with ``status="cached"`` and the cached per-entry scalar in
    ``loss_summary`` — they had no live run this round, but the
    dashboard can still render the head-to-head delta against the
    challenger's live result. ``partial_champion_agg`` is seeded with
    the cached aggregate so the running partial table is meaningful
    from the first frame.
    """
    validate_measurement_count(replicates)
    async with workspace_writer(
        workspace_root,
        writer=writer,
        instance_id=config.instance_id,
        cleanup=lambda: drain_worker_cleanup(workspace_root),
    ) as writer:
        from zicato.core import assert_distinct_callables  # noqa: PLC0415

        assert_distinct_callables(config.target_call_llm, config.evaluation_call_llm)
        from zicato.epoch.execution import bind_runtime_to_epoch  # noqa: PLC0415

        config = bind_runtime_to_epoch(config, workspace_root, epoch_id)

        from zicato.tournament.scoring import read_gen_score  # noqa: PLC0415
        from zicato.workspace.layout import WorkspaceLayout  # noqa: PLC0415

        stored_parent = read_gen_score(
            WorkspaceLayout(workspace_root), epoch_id, parent_generation_id
        )
        if stored_parent is None or stored_parent.to_dict() != parent_historical_agg:
            raise ValueError(
                "champion aggregate does not match the selected epoch's recorded score"
            )
        parent_historical_agg = stored_parent.to_dict()
        if (
            "base_seed" not in parent_historical_agg
            or parent_historical_agg["base_seed"] != config.seed
            or parent_historical_agg.get("generation_id") != parent_generation_id
        ):
            raise ValueError(
                "champion aggregate does not establish the requested generation and seed"
            )

        # The champion side stays ONE frozen cached aggregate no matter how high
        # ``replicates`` goes, so replicating here buys a replicated challenger
        # against an unreplicated champion. Warn explicitly rather than letting
        # an operator infer a symmetric noise reduction from the contract.
        if replicates > 1:
            log.warning(
                "fast-mode duel: replicating the CHALLENGER board %d× (replicates=%d), "
                "but the champion side is a single frozen cached aggregate — the noise "
                "reduction is one-sided. Use --mode full for independent draws on both sides.",
                replicates,
                replicates,
            )

        # Same board-level disable_drift / judge_only threading as the full
        # A/B path.
        board = _stamp_disable_drift(board, disable_drift)
        board = _stamp_judge_only(board, judge_only)

        # Best-effort tournament-state publication for the live dashboard.
        # Fast mode pre-fills both sides: the challenger rows are queued
        # (they progress to running/completed via _run_single's existing
        # update_tournament_entry calls), and the champion rows are stamped
        # "cached" with the per-entry scalar already known from the cached
        # aggregate. The dashboard hall renders the head-to-head delta the
        # instant each challenger run settles, rather than staying blank
        # until round end.
        rt = _runtime_state()
        parent_gen_id = str(parent_historical_agg.get("generation_id", ""))
        if rt is not None:
            state_mod, _ = rt
            try:
                from zicato.runtime.state import (  # noqa: PLC0415
                    ActiveTournament,
                    ActiveTournamentEntry,
                    RunStatus,
                    TournamentPhase,
                )

                now = _now_iso_utc()
                cached_per_entry = parent_historical_agg.get("per_entry") or {}
                child_entries = [
                    ActiveTournamentEntry(entry_id=e.id, side=Side.CHILD, status=RunStatus.QUEUED)
                    for e in board
                ]
                parent_entries: list[ActiveTournamentEntry] = []
                for e in board:
                    cached = (
                        cached_per_entry.get(e.id) if isinstance(cached_per_entry, dict) else None
                    )
                    loss_summary: dict[str, float] = {}
                    if isinstance(cached, dict):
                        drift = cached.get("drift_loss")
                        if isinstance(drift, int | float):
                            loss_summary["drift_loss"] = float(drift)
                        pf = cached.get("pass_fail")
                        if pf is not None:
                            loss_summary["pass_fail"] = 1.0 if pf else 0.0
                    parent_entries.append(
                        ActiveTournamentEntry(
                            entry_id=e.id,
                            side=Side.PARENT,
                            status=RunStatus.CACHED,
                            completed_at=now,
                            loss_summary=loss_summary,
                        )
                    )
                state_mod.write_active_tournament(
                    writer,
                    ActiveTournament(
                        tournament_id=f"tour-{parent_gen_id}-vs-{child_gen.id}-{now}",
                        parent_generation_id=parent_gen_id,
                        child_generation_id=child_gen.id,
                        epoch_id=epoch_id,
                        started_at=now,
                        entries=parent_entries + child_entries,
                        phase=TournamentPhase.RUNNING,
                        round_index=round_index,
                        total_rounds=total_rounds,
                        # Seed the champion-side partial aggregate with the
                        # cached aggregate so the running partial table is
                        # meaningful from the first frame; the challenger
                        # side fills in as boards settle (_IncrementalScorer).
                        partial_champion_agg=dict(parent_historical_agg),
                    ),
                )
            except Exception:  # noqa: BLE001
                pass

        try:
            # Board-unit scheduling: each board entry is one unit, and a
            # fast-mode unit runs ONLY the challenger (child) — the
            # champion's cached aggregate is reused. ``config.parallelism``
            # bounds the number of board units in flight — up to
            # ``parallelism`` run subprocesses at once (one challenger run
            # per unit).
            #
            # Each replicate slot keys a distinct per-unit cache slot, so an
            # already-evaluated replicate is reused and only missing slots run.
            # The index is stamped onto each entry's context as run provenance
            # for the harness under test (a seeded harness varies its noise draw
            # by it); slot 0 is left untouched, byte-identical to before.
            replicate_count = max(1, replicates)
            replicate_runs: list[dict[str, LossProfile]] = []
            if replicate_count > 1 and _overlap_replicate_slots(config, None):
                # No budget knob is engaged, so no decision sits on the boundary
                # between two slots and they run OVERLAPPED against ONE shared
                # semaphore — a permit freed by a finished unit is taken by the
                # next slot's unit instead of idling until the whole slot drains.
                # The scheduler mints that one semaphore (this round supplies
                # none), which is what keeps ``parallelism`` the ceiling.
                replicate_runs = await _run_replicate_slots_fast(
                    writer=writer,
                    adapter=adapter,
                    child_gen=child_gen,
                    board=board,
                    weights=weights,
                    config=config,
                    workspace_root=workspace_root,
                    epoch_id=epoch_id,
                    replicate_count=replicate_count,
                )
            else:
                for draw in range(replicate_count):
                    measurement = MeasurementDraw(MeasurementPurpose.TOURNAMENT, draw, config.seed)
                    # Budget expiry records an omission for every missing draw,
                    # so the fold cannot present partial execution as complete.
                    replicate_runs.append(
                        await _run_board_units_fast(
                            writer=writer,
                            adapter=adapter,
                            child_gen=child_gen,
                            board=_stamp_measurement(board, measurement),
                            weights=weights,
                            config=config,
                            workspace_root=workspace_root,
                            epoch_id=epoch_id,
                            measurement=measurement,
                        )
                    )
            if len(replicate_runs) == 1:
                child_losses = replicate_runs[0]
            else:
                child_losses = _average_losses(replicate_runs)
        finally:
            if rt is not None:
                state_mod, _ = rt
                try:
                    state_mod.clear_active_tournament(writer)
                except Exception:  # noqa: BLE001
                    pass

        # Fast mode compares the child against a cached whole-board historical
        # aggregate, so it does NOT thread a holdout into the gate: a train-only
        # child aggregate compared to a whole-board parent baseline would be an
        # apples-to-oranges scalar and could wrongly flip a decision. The
        # holdout-confirmation step lives on the full A/B path (the default
        # gauntlet promotion path); fast mode consults no holdout.
        child_agg = aggregate_generation_score(list(child_losses.values()), weights)
        outcome = await _gate_with_regression(
            parent_agg=parent_historical_agg,
            child_agg=child_agg,
            child_snapshot_root=child_gen.snapshot_root,
            weights=weights,
        )

        # Fast mode has no parent-side run, so per_entry_losses is empty.
        # Downstream code that wants to render per-entry deltas falls back
        # to the child losses inside ``child_agg["per_entry"]``.
        return TournamentResult(
            parent_generation_id=parent_gen_id,
            child_generation_id=child_gen.id,
            parent_agg=parent_historical_agg,
            child_agg=child_agg,
            outcome=outcome,
            per_entry_losses={},
            champion_eval_mode="fast",
        )


@with_workspace_imports
async def run_matchup(
    *,
    adapter: Any,
    left_gen: Generation,
    right_gen: Generation,
    board: list[BoardEntry],
    weights: ScoringWeights,
    config: RuntimeConfig,
    workspace_root: Path,
    epoch_id: str,
    board_subset: tuple[str, ...] | None = None,
    replicates: int = 1,
    first_measurement: MeasurementDraw = TOURNAMENT_DRAW,
    disable_drift: tuple[Any, ...] = (),
    judge_only: bool = False,
    round_index: int = 0,
    total_rounds: int = 0,
    match_id: str = "",
    fast: bool = False,
    matchup_budget_seconds: float | None = None,
    unit_semaphore: asyncio.Semaphore | None = None,
    left_diff_size: dict[str, int] | None = None,
    right_diff_size: dict[str, int] | None = None,
    writer: WorkspaceLock | None = None,
) -> TournamentResult:
    """Run one duel between two generations and apply the promotion gate.

    The competitors may be a champion and challenger or two field
    challengers; the gate treats ``left`` as the nominal parent. The runner
    honours a board subset and averaged paired replicates, aggregates both
    competitors, and applies ``_gate_with_regression`` and ``evaluate_gate``.

    Returns a :class:`TournamentResult` whose ``parent_*`` fields describe
    ``left`` and ``child_*`` describe ``right``, so a strategy can consume
    ``outcome.decision`` and ``outcome.delta_scalar`` without translation.

    ``match_id`` is the strategy's id for THIS matchup (e.g. ``"rung0_m2"``,
    ``"racing-final"``). It is threaded down to every board-entry run so
    each persisted :class:`LossProfile` (and the analytical-index ``runs`` /
    ``loss_profiles`` rows) is tagged with the matchup it ran within —
    enabling per-run rung attribution in the dashboard. Empty string leaves
    direct library calls untagged.

    ``first_measurement`` selects a purpose and starting local draw.
    Replicate ``i`` uses ``first_measurement.offset(i)``; the runner records
    ``config.seed`` with that identity and passes it into the harness context.
    Tournament matchups default to tournament draw zero. Evidence confirmation
    uses distinct draws under ``evidence_confirmation``, preserving the
    tournament measurements that selected the candidate.



    ``fast`` is the structure-independent cache-first evaluation knob (the
    runtime ``--mode fast`` setting, threaded identically to
    ``disable_drift`` and ``judge_only``). When set, both competitors resolve
    each ``(generation, entry, purpose, draw, base_seed)`` measurement through the unit cache and
    only missing slots run. The ``left`` competitor is normally the champion,
    whose cached-versus-fresh counts determine the resolved mode
    (``"fast"`` / ``"fast-degraded"`` / ``"full"``), which is
    recorded on the returned :attr:`TournamentResult.champion_eval_mode`
    for journal provenance; it never enters the gate or the contract.

    ``matchup_budget_seconds`` is an OPT-IN wall-clock cap on the duel's
    TOTAL board-unit execution. ``None`` (the default) ⇒ uncapped: every
    board unit × replicate × side runs to completion. When set, the runner
    checks the deadline before launching each batch. After the deadline,
    skipped units remain recorded as unstarted attempts and require fresh
    execution. These omissions do not populate the measurement cache or enter
    the aggregate as measured losses. An incomplete comparison cannot support
    promotion. The scheduler logs how many units were skipped. This bounds
    the AGGREGATE of an unbounded board × replicates × both-sides sweep
    (e.g. a racing final rung), a different axis from the per-board
    :attr:`BoardEntry.wall_clock_budget_seconds` (which bounds ONE unit).

    ``unit_semaphore`` is the optional cross-matchup concurrency gate. When
    the orchestrator runs several matchups of a round concurrently it
    passes ONE shared semaphore to every matchup so all of the round's
    board units draw from a single global cap (instead of each matchup
    minting its own ``Semaphore(parallelism)`` — which let N concurrent
    matchups run ``N × parallelism`` units at once). ``None`` gives the
    matchup its own semaphore.
    """
    validate_measurement_count(replicates)
    async with workspace_writer(
        workspace_root,
        writer=writer,
        instance_id=config.instance_id,
        cleanup=lambda: drain_worker_cleanup(workspace_root),
    ) as writer:
        from zicato.core import assert_distinct_callables  # noqa: PLC0415

        assert_distinct_callables(config.target_call_llm, config.evaluation_call_llm)
        from zicato.epoch.execution import bind_runtime_to_epoch  # noqa: PLC0415

        config = bind_runtime_to_epoch(config, workspace_root, epoch_id)

        board = _stamp_disable_drift(board, disable_drift)
        # Stamp the board-level judge_only flag onto each entry's
        # context so the adapter selects no-steering evaluation per entry. A
        # no-op when judge_only is False (the default), so the steering path
        # stays byte-identical. (Stamped before board_subset filtering so the
        # surviving slice carries it too.)
        board = _stamp_judge_only(board, judge_only)
        if board_subset is not None:
            subset = set(board_subset)
            board = [e for e in board if e.id in subset]

        left_losses, right_losses, champion_eval_mode, unit_provenance = await _run_replicated(
            writer=writer,
            adapter=adapter,
            left_gen=left_gen,
            right_gen=right_gen,
            board=board,
            weights=weights,
            config=config,
            workspace_root=workspace_root,
            epoch_id=epoch_id,
            replicates=replicates,
            first_measurement=first_measurement,
            match_id=match_id,
            fast=fast,
            matchup_budget_seconds=matchup_budget_seconds,
            unit_semaphore=unit_semaphore,
        )

        left_agg = aggregate_generation_score(
            list(left_losses.values()),
            weights,
            diff_size=left_diff_size,
        )
        right_agg = aggregate_generation_score(
            list(right_losses.values()),
            weights,
            diff_size=right_diff_size,
        )

        outcome = await _gate_with_regression(
            parent_agg=left_agg,
            child_agg=right_agg,
            child_snapshot_root=right_gen.snapshot_root,
            weights=weights,
        )

        per_entry_losses: dict[str, tuple[LossProfile, LossProfile]] = {}
        for entry_id, left_loss in left_losses.items():
            right_loss = right_losses.get(entry_id)
            if right_loss is not None:
                per_entry_losses[entry_id] = (left_loss, right_loss)

        _ = (round_index, total_rounds)  # reserved for live-state publication
        return TournamentResult(
            parent_generation_id=left_gen.id,
            child_generation_id=right_gen.id,
            parent_agg=left_agg,
            child_agg=right_agg,
            outcome=outcome,
            per_entry_losses=per_entry_losses,
            champion_eval_mode=champion_eval_mode,
            unit_provenance=unit_provenance,
        )


@with_workspace_imports
async def confirm_crowning_holdout(
    *,
    adapter: Any,
    champion_gen: Generation,
    challenger_gen: Generation,
    board: list[BoardEntry],
    train_outcome: GateOutcome,
    train_parent_agg: dict[str, Any],
    train_child_agg: dict[str, Any],
    weights: ScoringWeights,
    config: RuntimeConfig,
    workspace_root: Path,
    epoch_id: str,
    disable_drift: tuple[Any, ...] = (),
    judge_only: bool = False,
    fast: bool = False,
    writer: WorkspaceLock | None = None,
) -> tuple[GateOutcome, dict[str, Any] | None, float | None]:
    """Confirm the crowning training duel on the contract's holdout slice.

    No holdout slice records a disabled requirement and preserves the training
    decision. A training rejection skips holdout access. Otherwise the runner
    reserves a query before executing the holdout matchup and settles its
    release decision. Exhausted allowance, withholding, or incomplete execution
    defers promotion; a released negative result rejects it.

    Returns the final gate outcome, its holdout record, and the challenger's
    observed holdout scalar (or ``None`` when no complete measurement exists).
    Full and fast execution use the same confirmation policy.
    """
    async with workspace_writer(
        workspace_root,
        writer=writer,
        instance_id=config.instance_id,
        cleanup=lambda: drain_worker_cleanup(workspace_root),
    ) as writer:
        from zicato.board.split import rotation_seed, split_board  # noqa: PLC0415

        seed = rotation_seed(weights.overfitting, epoch_id)
        _train_ids, holdout_ids = split_board(board, weights.overfitting, seed=seed)
        if not holdout_ids:
            # No holdout slice: return the train decision unchanged.
            return train_outcome, dict(disabled_holdout_record()), None

        if train_outcome.decision != "promoted":
            # Holdout confirmation can only veto a train promotion.  A rejected
            # train duel therefore performs no holdout access and spends no query.
            return train_outcome, None, None

        reservation = None
        ladder_cfg = weights.overfitting.ladder
        if ladder_cfg.enabled:
            ladder_state, reservation = _reserve_ladder_query(workspace_root, epoch_id, ladder_cfg)
            if reservation is None:
                final, block = _ladder_exhausted_outcome(
                    train_outcome=train_outcome,
                    train_child_agg=train_child_agg,
                    state=ladder_state,
                    weights=weights,
                )
                return final, block, None

        holdout_result = await run_matchup(
            writer=writer,
            adapter=adapter,
            left_gen=champion_gen,
            right_gen=challenger_gen,
            board=board,
            weights=weights,
            config=config,
            workspace_root=workspace_root,
            epoch_id=epoch_id,
            board_subset=tuple(holdout_ids),
            disable_drift=disable_drift,
            judge_only=judge_only,
            fast=fast,
            match_id="holdout-confirm",
        )
        holdout_parent_agg = holdout_result.parent_agg
        holdout_child_agg = holdout_result.child_agg

        final_outcome, holdout_block = _ladder_mediated_outcome(
            train_outcome=train_outcome,
            parent_agg=train_parent_agg,
            child_agg=train_child_agg,
            holdout_parent_agg=holdout_parent_agg,
            holdout_child_agg=holdout_child_agg,
            weights=weights,
            workspace_root=workspace_root,
            epoch_id=epoch_id,
            reservation=reservation,
        )
        holdout_child_scalar = (
            None
            if holdout_child_agg.get("incomplete_entries")
            else float(holdout_child_agg["scalar"])
        )
        return final_outcome, holdout_block, holdout_child_scalar


# Public surface
__all__ = [
    "TournamentResult",
    "run_fast_mode",
    "run_tournament",
    "run_matchup",
    "confirm_crowning_holdout",
]


# ``asyncio`` is imported so type-checkers and human readers see the
# module is async-aware; the public coroutines above use ``await``
# directly and do not need to construct loops.
_ = asyncio
