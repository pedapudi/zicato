"""Tournament runner: full A/B and fast inline keep/discard.

Two entry points:

* :func:`run_tournament` (full mode) — runs every board entry under
  BOTH parent and child generations.

Board-unit parallelism
----------------------
The unit of scheduling is a **board unit**: one per board entry. A
board unit owns the runs for a single board entry and is the thing the
:attr:`RuntimeConfig.parallelism` knob counts — ``parallelism`` is "how
many boards run in parallel", NOT how many subprocesses run in parallel.

* In **full mode** a board unit runs the **champion (parent)** run AND
  the **challenger (child)** run **simultaneously**: both
  :func:`_run_single` calls start together under one
  :func:`asyncio.gather`, and the unit does not finish until both have.
  The champion and challenger of the same entry therefore execute
  concurrently — each in its OWN subprocess worker, each pointed at its
  OWN per-run ephemeral snapshot copy, so there is no shared-state
  collision between the two sides of one entry.
* In **fast mode** a board unit runs **only the challenger (child)**.
  The champion's cached aggregate (``gen_score.json``) is reused, so the
  champion run is not executed at all. Fast mode degrades to a full
  board unit for the rare entry-set with no cached champion aggregate —
  but that fallback is decided by the caller (the orchestrator picks
  :func:`run_tournament` vs :func:`run_fast_mode`), not inside a unit.

The board units themselves play concurrently — the "tournament hall"
model, many boards in progress at once — bounded by a single
:class:`asyncio.Semaphore` sized from :attr:`RuntimeConfig.parallelism`.
Concurrency is safe because every run is fully isolated: each
board-entry run executes in its OWN subprocess worker (see below)
writing to a per-run ``active_runs/{run_id}.json`` + ``events.jsonl`` +
``loss.json``, keyed on a unique ``run_id`` of
``{generation_id}--{entry_id}``.

Cross-matchup parallelism (one global cap per round)
----------------------------------------------------
A non-gauntlet structure (swiss / elim / racing) schedules SEVERAL
matchups of a round concurrently (the selection driver fans the round's
batch out under one :func:`asyncio.gather`). Each :func:`run_matchup`
accepts an OPTIONAL ``unit_semaphore``: when the orchestrator passes one
shared semaphore to every matchup of a round, all of that round's board
units across all its concurrent matchups draw from ONE global
concurrency cap, instead of each matchup minting its own
``Semaphore(parallelism)`` (which let N concurrent matchups run
``N × parallelism`` units at once and re-pay worker-spawn + snapshot
overhead serially per matchup). When ``unit_semaphore`` is ``None`` —
every direct/gauntlet caller — each board-unit runner mints its own, so
the single-matchup path is byte-identical to before.

Set ``parallelism=1`` to run one board unit at a time. Note this is NOT
the same as "one subprocess at a time": with ``parallelism=1`` in full
mode a single board unit still spawns the champion and challenger
subprocesses CONCURRENTLY (2 workers). In general, P board units in
full mode means up to ``2 * P`` run subprocesses alive at once; in fast
mode up to ``P`` (challenger-only). The real-world ceiling on
``parallelism`` is almost always the LLM endpoint's own concurrency
limit — size it against ``2 * parallelism`` for full mode.

Per-run ephemeral working copies
--------------------------------
The canonical generation snapshot
(``epochs/{id}/generations/vN/snapshot/``) is treated as **immutable
code**: it is the tree ``derive_generation`` copies forward to seed the
next generation, so anything written into it accumulates across every
generation and would eventually exhaust the disk. A target agent,
however, may legitimately write near its own code — runtime ``output/``,
scratch files, caches — and a meta-harness must be robust to that. So
:func:`_run_single` never points a worker at the canonical snapshot
directly. Instead it makes a per-run **ephemeral working copy** of the
snapshot (a cheap, KB-sized ``copytree`` — code snapshots are small),
points the worker at THAT copy, and discards it once the run finishes —
on a clean exit, an abort, or a crash. Every runtime write the agent
makes therefore lands in the throwaway per-run directory; the canonical
snapshot stays code-only and small and ``derive_generation``'s
``copytree`` stays cheap. The run's telemetry (``events.jsonl`` /
``loss.json``) is unaffected — it is keyed on the workspace's
``runs/{entry_id}/`` layout, not on the working copy. This is the same
isolation a per-run ``git worktree`` would later give for free; a
code-only ``copytree`` per run is the correct interim mechanism.

* :func:`run_fast_mode` — autoresearch-style inline keep/discard.
  Only the child is run; comparison is against a previously-computed
  ``parent_historical_agg`` dict. Cheaper but skips the controlled-
  experiment guarantee (the world may have drifted since the parent
  was scored). Same gate logic applies.

The regression-suite gate (see :mod:`zicato.tournament.regression`)
runs BEFORE the scoring gate when
:attr:`ScoringWeights.regression_gate_enabled` is true. A failing
regression suite hard-rejects the candidate, shadowing any drift_loss /
pass_rate improvement: a patch that breaks the snapshot's own tests
cannot promote even when its scoring signal looks perfect.

Subprocess isolation ("L3")
---------------------------
Each board-entry run executes in its OWN OS process — a
``python -m zicato._tournament_worker`` subprocess (see
:mod:`zicato._tournament_worker`). :func:`_run_single` serialises one
run's inputs to a temp args file, spawns the worker, and waits on it
bounded by the entry's wall-clock budget plus a small grace margin
(:data:`_PARENT_BUDGET_GRACE_S`). The worker keeps its own cooperative
``asyncio.wait_for`` budget as the first line of defence; the parent's
SIGTERM-then-SIGKILL escalation is the second; an independent supervisor
watchdog — keyed on the worker's own pid stamped into
``active_runs/{run_id}.json`` — is the third. A wedged run can therefore
be killed without taking down the whole ``evolve``. A worker that
vanished without a result file (the supervisor SIGKILLed it) is recorded
as a normal aborted run, not a crash; the tournament continues.

The runner LAZY-imports :mod:`zicato.telemetry` per-call so the
package keeps loading cheaply even before the telemetry layer is
wired up. It uses two telemetry helpers:

* ``zicato.telemetry.sink.make_run_sink_path(workspace_root, epoch_id,
  generation_id, entry_id) -> Path`` — returns the events JSONL path
  the worker's sink writes to. Must be deterministic.
* ``zicato.telemetry.reducer.read_loss_profile(path) -> LossProfile`` —
  reads back the ``loss.json`` the worker produced.

The actual ``session.run`` driving (rich
:class:`~zicato.adapters.RunnableHarness` ``run(entry, sinks, config)``
shape and the legacy ``run(entry, sink_path)`` stub shape) now lives
inside the worker, not the runner — see
:func:`zicato._tournament_worker._drive_session`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import tempfile
import time
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
from zicato.tournament.gate import GateOutcome, evaluate_gate

# ``_load_ladder_state`` / ``_losses_for`` / ``_save_ladder_state`` are
# re-exported (F401) for back-compat — callers and tests reach them through
# ``zicato.tournament.runner`` even though the runner body no longer calls
# them directly (the governance helpers that do moved alongside).
from zicato.tournament.governance import (  # noqa: F401
    _holdout_aggs,
    _ladder_mediated_outcome,
    _load_ladder_state,
    _losses_for,
    _regression_rejection,
    _save_ladder_state,
    _train_aggs,
)
from zicato.tournament.regression import run_regression_suite

# The board-unit schedulers live in ``scheduling``. They evaluate each unit
# through the runner's ``_run_single`` (resolved via the runner namespace so
# the test suite's in-place patches still drive them). Re-exported (F401) for
# the stable import path (tests reach ``_IncrementalScorer`` through the
# runner module) and called by the public entry points that stay here.
from zicato.tournament.scheduling import (  # noqa: F401
    _effective_unit_semaphore,
    _IncrementalScorer,
    _run_board_units_fast,
    _run_board_units_full,
    _run_board_units_full_budgeted,
    _run_full_board_unit,
    _run_replicated,
    _run_unit_cache_first,
)
from zicato.tournament.scoring import aggregate_generation_score

# The per-unit loss cache + provenance lives in ``unit_cache``. Re-exported
# (F401) for the stable ``from zicato.tournament.runner import ...`` import
# path (tests reach ``_unit_loss_path`` / ``_average_losses`` through the
# runner module) and used by the schedulers that still live here.
from zicato.tournament.unit_cache import (  # noqa: F401
    _average_losses,
    _persist_unit_loss,
    _record_provenance,
    _resolve_cached_unit,
    _skipped_unit_loss,
    _unit_loss_path,
    _UnitProvenance,
)

# The subprocess/process-boundary transport (wire-spec builders, ephemeral
# snapshot copies, worker lifecycle + aborted-run synthesis, and the small
# shared primitives) lives in ``worker_transport``. It is imported by name
# so every helper is a RUNNER-module global: ``_run_single`` (which stays
# here) resolves them via bare-name lookup, and the test suite patches them
# (``_terminate_worker``, the timeout constants, ``_weights_spec``,
# ``_adapter_spec``, ``_entry_to_dict``, ``_stamp_*``) on this module's
# namespace, so the names must live here. Re-exported for the stable
# ``from zicato.tournament.runner import ...`` import surface (F401).
from zicato.tournament.worker_transport import (  # noqa: F401
    _ABORTED_TASK_FAILURE_MULTIPLIER,
    _DISABLE_DRIFT_CONTEXT_KEY,
    _EPHEMERAL_SNAPSHOT_PREFIX,
    _INDEX_DB_RELPATH,
    _JUDGE_ONLY_CONTEXT_KEY,
    _PARENT_BUDGET_GRACE_S,
    _SIGTERM_TO_SIGKILL_GRACE_S,
    _aborted_loss_profile,
    _adapter_spec,
    _callable_dotted_path,
    _discard_ephemeral_snapshot,
    _drift_kind_wire,
    _entry_to_dict,
    _index_db_path,
    _ingest_run_into_index,
    _load_worker_result,
    _make_ephemeral_snapshot,
    _now_iso_utc,
    _resolve_harmonograf_grpc,
    _resolve_harmonograf_url,
    _role_worker_spec,
    _run_id_for,
    _runtime_state,
    _scrubbed_worker_env,
    _stamp_disable_drift,
    _stamp_judge_only,
    _telemetry_helpers,
    _terminate_worker,
    _weights_spec,
)

log = logging.getLogger("zicato.tournament.runner")

#: Minimum interval (seconds) between successive ``last_progress`` bumps
#: for a single in-flight run. The per-run sink is wrapped so every
#: goldfive event would otherwise trigger a state-file write; throttling
#: keeps a chatty run from turning into a write storm on the runtime
#: directory.
_PROGRESS_BUMP_MIN_INTERVAL_S = 2.0


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
    #: of board units reused from the cache (``cached``) vs genuinely
    #: executed (``fresh``), keyed by generation id. Lets a structure-
    #: agnostic caller (the orchestrator) attribute reuse to the CHAMPION
    #: specifically — a generation appears on either side across a
    #: swiss/elim field, and only the champion's reuse drives the
    #: ``champion_eval_mode`` provenance. A gauntlet/ad-hoc caller can
    #: ignore it. Empty for legacy callers.
    unit_provenance: dict[str, _UnitProvenance] = field(default_factory=dict)
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


class _ProgressBumpingSink:
    """Sink decorator that bumps an :class:`ActiveRun`'s ``last_progress``.

    Wraps the canonical per-run goldfive sink (a
    :class:`~goldfive.sinks.persistence.JSONLPersistenceSink`, or any
    object exposing the async ``emit`` / ``close`` pair). Every
    :meth:`emit` is forwarded to the wrapped sink unchanged AND — at most
    once per :data:`_PROGRESS_BUMP_MIN_INTERVAL_S` seconds — also calls
    :func:`zicato.runtime.state.touch_active_run_progress` so the live
    dashboard sees the run's heartbeat advance.

    Why a wrapper rather than a hook inside the runner: goldfive owns the
    run loop once ``session.run`` is entered, so the only place the
    orchestrator can observe per-event progress is the sink boundary.

    The throttle is a simple monotonic-clock gate: the first emit always
    bumps (so a freshly-started run animates immediately), and subsequent
    emits bump only after the interval has elapsed. A run that emits
    nothing simply never bumps — the supervisor's deadline logic still
    covers a genuinely wedged run.

    The progress bump is strictly best-effort: a missing runtime-state
    module, or a write failure (e.g. the run already finished and the
    state file was removed), is swallowed. A telemetry-side error must
    never abort a run.
    """

    __slots__ = ("_inner", "_workspace_root", "_run_id", "_last_bump", "_bump")

    def __init__(self, inner: Any, workspace_root: Path, run_id: str) -> None:
        self._inner = inner
        self._workspace_root = workspace_root
        self._run_id = run_id
        # Negative-infinity sentinel so the very first emit always bumps.
        self._last_bump = float("-inf")
        # Resolve the bump callable once; ``None`` when runtime state is
        # unavailable, which turns every bump into a cheap no-op.
        self._bump: Any = None
        try:
            from zicato.runtime.state import (  # noqa: PLC0415
                touch_active_run_progress,
            )

            self._bump = touch_active_run_progress
        except ImportError:
            self._bump = None

    async def emit(self, event: Any) -> None:
        """Forward the event to the wrapped sink, then bump progress (throttled)."""
        await self._inner.emit(event)
        if self._bump is None:
            return
        now = time.monotonic()
        if now - self._last_bump < _PROGRESS_BUMP_MIN_INTERVAL_S:
            return
        self._last_bump = now
        try:
            self._bump(self._workspace_root, self._run_id)
        except Exception as exc:  # noqa: BLE001 — progress bump is best-effort
            log.debug("active-run progress bump skipped for %s: %s", self._run_id, exc)

    async def close(self) -> None:
        """Close the wrapped sink (no progress bump on close)."""
        await self._inner.close()


def _wrap_sinks_with_progress(
    sinks: list[Any],
    workspace_root: Path,
    run_id: str,
) -> list[Any]:
    """Wrap each per-run sink so emits bump the run's ``last_progress``.

    Returns a new list with every sink replaced by a
    :class:`_ProgressBumpingSink`. An empty input (no-goldfive
    environment) yields an empty list — there is nothing to wrap and the
    run simply does not animate.
    """
    return [_ProgressBumpingSink(s, workspace_root, run_id) for s in sinks]


async def _run_single(
    *,
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
    """Run one entry under one generation in an isolated subprocess worker.

    ``match_id`` is the tournament matchup this run executes within (e.g.
    ``"rung0_m2"``, ``"racing-final"``); empty string for a run that is
    not part of a tagged matchup (a gauntlet duel via
    :func:`run_tournament`, or an ad-hoc caller). The worker — which
    writes ``loss.json`` — does not know it, so the runner stamps it onto
    the :class:`LossProfile` after the run settles AND rewrites
    ``loss.json`` with the tag so a later full ``zicato reindex`` (which
    re-reads ``loss.json``) re-derives the same provenance. The aborted
    profiles synthesised on a killed/crashed run carry it too.

    ``side`` is the tournament side this run belongs to — ``"parent"``
    or ``"child"`` — supplied explicitly by the caller, which knows
    whether ``generation`` is the tournament's parent or child. It is
    used solely to target the correct row in the
    :class:`~zicato.runtime.state.ActiveTournament` grid: each board
    entry has TWO rows (one per side), so a per-entry state transition
    must be keyed on ``(entry_id, side)``, not ``entry_id`` alone, or a
    parent-side transition lands on the child row (and vice versa).
    Empty string when the run is not part of a tournament (ad-hoc
    callers); :func:`update_tournament_entry` then matches nothing and
    the call is a benign no-op.

    Sequencing:

    1. Make a per-run **ephemeral working copy** of the generation's
       code snapshot (a cheap ``copytree`` into a system-temp directory)
       and point the worker at THAT, never at the canonical
       ``generations/vN/snapshot/``. Any runtime write the agent makes
       near its own code lands in the throwaway copy, so the canonical
       snapshot stays code-only and ``derive_generation`` does not carry
       runtime output forward. See :func:`_make_ephemeral_snapshot`.
    2. Serialise the run's inputs (entry, adapter spec, call_llm dotted
       paths, scoring weights, sink/loss/result paths, and the ephemeral
       ``snapshot_root``) to a temp args file.
    3. Spawn ``python -m zicato._tournament_worker <args-file>`` via
       :func:`asyncio.create_subprocess_exec`. The worker stamps its OWN
       pid into ``active_runs/{run_id}.json`` so the supervisor can kill
       it individually.
    4. ``await asyncio.wait_for(proc.wait(), budget + GRACE)``. The
       worker's own cooperative budget normally fires first; the parent's
       wait_for is the second line of defence.
    5. On parent timeout: SIGTERM -> (grace) -> SIGKILL the worker, then
       synthesise an aborted :class:`LossProfile`.
    6. On clean exit: read the worker's result file -> the
       :class:`LossProfile` written to ``loss.json``. A worker that
       exited non-zero, OR a missing/corrupt result file (e.g. the
       SUPERVISOR SIGKILLed a wedged worker), is ALSO an aborted run —
       not a crash. The tournament continues to the next entry either
       way.
    7. Always clean up: the ephemeral snapshot working copy (even when
       the run aborted or crashed), the temp args/result files, and — if
       the worker was killed and could not remove its own ``active_runs``
       file — that too.
    """
    sink_module, reducer_module = _telemetry_helpers()
    sink_path = sink_module.make_run_sink_path(
        workspace_root=workspace_root,
        epoch_id=epoch_id,
        generation_id=generation.id,
        entry_id=entry.id,
    )
    from zicato.core.workspace import loss_profile_path  # noqa: PLC0415

    loss_path = loss_profile_path(workspace_root, epoch_id, generation.id, entry.id)
    run_id = _run_id_for(generation, entry)
    budget_s = float(entry.wall_clock_budget_seconds)

    rt = _runtime_state()

    # Best-effort tournament-entry transition for the live dashboard. The
    # worker writes the per-run ``active_runs`` file (with its own pid);
    # the orchestrator only owns the tournament-entry grid status.
    if rt is not None:
        state_mod, _ = rt
        try:
            state_mod.update_tournament_entry(
                workspace_root,
                entry.id,
                side,
                status=state_mod.RunStatus.RUNNING,
                started_at=_now_iso_utc(),
            )
        except Exception:  # noqa: BLE001 — state writes are best-effort
            pass

    # --- 1./2. Serialise the run's inputs to a temp args file. ---
    args_fd, args_name = tempfile.mkstemp(prefix=f"ztw-args-{run_id}-", suffix=".json")
    os.close(args_fd)
    args_path = Path(args_name)
    result_path = Path(args_name[: -len(".json")] + ".result.json")
    spawn_started = time.monotonic()
    # The per-run ephemeral snapshot working copy; assigned once the
    # copytree below succeeds, discarded in this function's ``finally``.
    ephemeral_snapshot: Path | None = None

    # The run's final LossProfile — assigned on every exit path (clean
    # finish OR abort) so the ``finally`` block can fold the loss summary
    # into the live active-tournament record (A3). Stays ``None`` only on
    # an unexpected hard crash, where the ``finally`` skips the fold.
    final_loss: LossProfile | None = None

    try:
        try:
            # --- 1. Per-run ephemeral working copy of the code
            # snapshot. The worker is pointed at this copy, never at the
            # canonical ``generations/vN/snapshot/``, so any runtime
            # write the agent makes near its own code lands here and is
            # discarded with the copy — the canonical snapshot stays
            # code-only and small.
            ephemeral_snapshot, scratch_dir = _make_ephemeral_snapshot(
                generation.snapshot_root, run_id
            )
            # The unified ``models`` block (runtime infra, NOT the contract)
            # is the source of truth for how each role reaches a provider in
            # the worker. For a configured role we pass its secret-free spec
            # and let the worker re-resolve (so a model-spec closure need not
            # cross the process boundary); for an unconfigured role we fall
            # back to the resolved callable's dotted path — today's behavior.
            from zicato import workspace_loader  # noqa: PLC0415
            from zicato.models_config import ModelsConfig, load_models_config  # noqa: PLC0415

            try:
                _models = load_models_config(workspace_loader.load_workspace_config(workspace_root))
            except (FileNotFoundError, ValueError):
                # No / malformed workspace config.json ⇒ no ``models`` block;
                # every role falls back to its resolved callable's dotted
                # path (today's behavior). Ad-hoc callers (tests) that run a
                # generation without a full workspace config still spawn.
                _models = ModelsConfig()
            args_payload = {
                "workspace_root": str(workspace_root),
                "epoch_id": epoch_id,
                "generation_id": generation.id,
                "snapshot_root": str(ephemeral_snapshot),
                "scratch_dir": str(scratch_dir),
                "entry": _entry_to_dict(entry),
                "adapter": _adapter_spec(adapter),
                "harness_role": _role_worker_spec(
                    "harness", models=_models, fallback_callable=config.harness_call_llm
                ),
                "auxiliary_role": _role_worker_spec(
                    "auxiliary", models=_models, fallback_callable=config.auxiliary_call_llm
                ),
                "judge_role": _role_worker_spec(
                    "judge",
                    models=_models,
                    fallback_callable=config.effective_judge_call_llm(),
                ),
                "sink_events_path": str(sink_path),
                "loss_path": str(loss_path),
                "result_path": str(result_path),
                "instance_id": config.instance_id,
                "seed": config.seed,
                "harmonograf_url": (_hg_url := _resolve_harmonograf_url(workspace_root)),
                "harmonograf_grpc": _resolve_harmonograf_grpc(workspace_root, _hg_url),
                "weights": _weights_spec(weights),
            }
            args_path.write_text(json.dumps(args_payload), encoding="utf-8")
        except (ValueError, OSError) as exc:
            # The run could not be prepared for a subprocess: either it
            # was not subprocess-serialisable (a closure-local callable,
            # a non-ADK adapter -> ValueError) or the ephemeral snapshot
            # copy failed (disk full, source snapshot missing -> OSError).
            # Treat as an aborted run so the tournament still aggregates,
            # rather than taking the whole evolve down.
            log.warning("run %s could not be prepared for a subprocess: %s", run_id, exc)
            final_loss = _aborted_loss_profile(
                run_id=run_id,
                entry=entry,
                generation_id=generation.id,
                epoch_id=epoch_id,
                weights=weights,
                runtime_ms=0,
                match_id=match_id,
                abort_cause="prepare_failed",
            )
            return final_loss

        # --- 3. Spawn the worker subprocess. ---
        # ``start_new_session=True`` runs the worker in its OWN session and
        # process-group (it calls ``setsid`` before ``exec``), so the worker
        # leads a group containing itself plus any grandchildren the inner
        # harness spawns (shells, helper tools). The worker records that
        # group's id (``pgid``) on its ActiveRun record, letting the
        # supervisor GROUP-kill the whole tree by negating the pgid rather
        # than leaking grandchildren when it kills the worker pid alone. It
        # also detaches the worker from the orchestrator's controlling
        # terminal so a Ctrl-C / SIGINT to the orchestrator's terminal group
        # is not broadcast straight into every in-flight worker.
        # Compose the worker's environment. By default ``env=None`` inherits
        # the orchestrator's full environment — today's behavior, byte-for-
        # byte unchanged. When the operator opts into ``scrub_worker_env`` the
        # worker instead gets a MINIMAL explicit env (process-essential keys +
        # the api_key_env names the configured roles need + any passthrough),
        # so a mutated worker cannot read every credential in the process env.
        worker_env: dict[str, str] | None = None
        if config.scrub_worker_env:
            worker_env = _scrubbed_worker_env(
                models=_models,
                extra_env_keys=tuple(config.worker_env_passthrough),
            )
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "zicato._tournament_worker",
            str(args_path),
            start_new_session=True,
            env=worker_env,
        )

        # --- 4. Wait, bounded by budget + GRACE. ---
        killed_by_parent = False
        try:
            await asyncio.wait_for(
                proc.wait(),
                timeout=budget_s + _PARENT_BUDGET_GRACE_S,
            )
        except TimeoutError:
            # --- 5. The worker's own cooperative budget did NOT fire.
            # The SINGLE SIGTERM→grace→SIGKILL escalator lives in the
            # supervisor; the parent REQUESTS the kill via a control marker
            # and waits for the supervisor to reap the worker, rather than
            # escalating itself — so there is no parent↔supervisor race over
            # the same worker pid.
            killed_by_parent = True
            log.warning(
                "run %s exceeded budget+grace (%.0fs); requesting supervisor kill",
                run_id,
                budget_s + _PARENT_BUDGET_GRACE_S,
            )
            if rt is not None:
                state_mod, _ = rt
                try:
                    state_mod.request_worker_kill(workspace_root, run_id)
                except Exception as exc:  # noqa: BLE001 — request is best-effort
                    log.debug("run %s: kill-request write failed: %s", run_id, exc)
            # Wait for the supervisor to escalate-kill the worker. The
            # supervisor's escalation (SIGTERM→grace→SIGKILL) is bounded, so
            # this wait is too. If the supervisor does NOT reap the worker
            # within the window — no supervisor attached, or it died — the
            # parent falls back to its own last-resort escalation so the
            # worker is never leaked. The fallback fires only AFTER the whole
            # supervisor window elapsed with the worker still alive, so it
            # never races a healthy supervisor over the same pid. The window
            # (config.supervisor_kill_wait_s) is the abort-latency floor when
            # no supervisor is attached.
            try:
                await asyncio.wait_for(proc.wait(), timeout=config.supervisor_kill_wait_s)
            except TimeoutError:
                log.warning(
                    "run %s: supervisor did not reap the worker within %.0fs; "
                    "parent escalating as a last resort",
                    run_id,
                    config.supervisor_kill_wait_s,
                )
                await _terminate_worker(proc)

        runtime_ms = int((time.monotonic() - spawn_started) * 1000)
        result = _load_worker_result(result_path)

        if killed_by_parent or result is None or proc.returncode != 0:
            # Aborted run. Three causes — now DISTINGUISHED via abort_cause so
            # loop-health can tell an honest agent infinite-loop (parent kill)
            # from a transient crash from our OWN watchdog over-firing, and so
            # the cache layer never persists an infra abort (only a genuine
            # wall-clock-budget exhaustion is cache-eligible). All three remain
            # NORMAL outcomes that must not abort the tournament:
            #   * the PARENT killed a wedged worker (killed_by_parent),
            #   * the SUPERVISOR SIGKILLed a worker past its deadline
            #     (process gone, result file missing),
            #   * the worker process itself crashed (non-zero exit, no
            #     usable result file).
            # killed_by_parent is checked FIRST: a parent kill can leave the
            # returncode non-zero too, but the parent kill is the more specific
            # (and the more actionable, for the over-firing-watchdog signal)
            # provenance.
            if killed_by_parent:
                abort_cause = "parent_kill"
            elif result is None:
                abort_cause = "gone_no_result"
                log.info(
                    "run %s: worker gone with no result file "
                    "(supervisor kill or crash); recording aborted run",
                    run_id,
                )
            else:
                abort_cause = f"nonzero_exit:{proc.returncode}"
                log.info(
                    "run %s: worker exited %s; recording aborted run",
                    run_id,
                    proc.returncode,
                )
            # Terminal-event invariant: the worker is dead and the
            # events.jsonl on disk most likely lacks a terminal
            # lifecycle frame (the worker was SIGKILLed before it could
            # emit one, or crashed mid-call). Append a ``run_aborted``
            # line directly so the downstream transcript reconstructor
            # can flip ``complete=True`` and the dashboard renders an
            # honest "timed out" panel rather than a misleading "in
            # progress" cue. No-op when a terminal frame is already
            # present (the worker's own cooperative path beat us to it).
            try:
                from zicato.telemetry.terminal_event import (  # noqa: PLC0415
                    ensure_run_aborted_event,
                )

                ensure_run_aborted_event(sink_path)
            except Exception as exc:  # noqa: BLE001 — best-effort
                log.debug("run %s: terminal-event append failed: %s", run_id, exc)
            final_loss = _aborted_loss_profile(
                run_id=run_id,
                entry=entry,
                generation_id=generation.id,
                epoch_id=epoch_id,
                weights=weights,
                runtime_ms=runtime_ms,
                match_id=match_id,
                abort_cause=abort_cause,
            )
            return final_loss

        # --- 6. Clean exit. Read the LossProfile the worker wrote. ---
        # The worker may itself have aborted via its OWN cooperative
        # budget — that is still a clean worker exit (exit code 0, result
        # file present) and the loss.json it wrote already carries
        # ``wall_clock_budget_exceeded=True``. We just read it back.
        loss_profile_path_str = str(result.get("loss_profile_path", loss_path))
        try:
            loss: LossProfile = reducer_module.read_loss_profile(Path(loss_profile_path_str))
        except (OSError, KeyError, ValueError, json.JSONDecodeError) as exc:
            # The worker said it finished cleanly but its loss.json is
            # unreadable — treat as aborted rather than crashing.
            log.warning("run %s: worker result loss.json unreadable: %s", run_id, exc)
            final_loss = _aborted_loss_profile(
                run_id=run_id,
                entry=entry,
                generation_id=generation.id,
                epoch_id=epoch_id,
                weights=weights,
                runtime_ms=runtime_ms,
                match_id=match_id,
                abort_cause="result_unreadable",
            )
            return final_loss

        # Tag the run with the matchup it ran within. The worker (which
        # wrote loss.json) does not know the match_id, so the runner
        # stamps it here and rewrites loss.json so a later full ``zicato
        # reindex`` — which re-reads loss.json — re-derives the same
        # provenance, not just the live dual-write below. ``match_id=""``
        # (a gauntlet / ad-hoc run) leaves the profile and file byte-
        # unchanged: there is nothing to stamp, so we skip the rewrite.
        if match_id:
            loss = replace(loss, match_id=match_id)
            try:
                reducer_module.write_loss_profile(loss, Path(loss_profile_path_str))
            except OSError as exc:  # noqa: BLE001 — provenance rewrite is best-effort
                log.debug("run %s: match_id loss.json rewrite skipped: %s", run_id, exc)

        # Live index dual-write: the run's loss.json is on disk (now
        # carrying match_id when tagged), so fold it into the SQLite
        # analytical index. Best-effort.
        _ingest_run_into_index(workspace_root, epoch_id, generation.id, entry.id)
        final_loss = loss
        return final_loss
    finally:
        # --- 7. Cleanup. Discard the per-run ephemeral snapshot working
        # copy (every runtime write the agent made is inside it — it
        # must not survive the run); remove the temp args/result files;
        # if the worker was killed before it could remove its own
        # active_runs file, the parent removes it here. This block runs
        # on every exit path — clean finish, abort, or crash.
        _discard_ephemeral_snapshot(ephemeral_snapshot)
        for tmp in (args_path, result_path):
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass
        if rt is not None:
            state_mod, _ = rt
            # Clear any kill-request marker this run wrote — the worker is
            # gone now, and a recycled run id must not inherit a stale
            # request (the supervisor would otherwise escalate a fresh,
            # innocent pid). Best-effort + idempotent; a no-op when no kill
            # was ever requested for this run.
            try:
                state_mod.clear_worker_kill_request(workspace_root, run_id)
            except Exception as exc:  # noqa: BLE001 — cleanup is best-effort
                log.debug("run %s: kill-request clear skipped: %s", run_id, exc)
            try:
                state_mod.remove_active_run(workspace_root, run_id)
                # A3: fold the run's per-entry loss summary into the live
                # active-tournament record so the dashboard renders a
                # per-entry score the instant the run finishes — rather
                # than leaving ``loss_summary`` empty until the journal
                # materialises. The shape is pinned by
                # ``state.loss_summary_from_profile`` /
                # ``drift_count_snapshot_from_profile`` (the Zone-B
                # contract). ``final_loss`` is set on every clean-finish
                # AND abort path; it is ``None`` only after an
                # unexpected hard crash, where we fall back to the bare
                # status transition.
                entry_updates: dict[str, Any] = {
                    "status": "completed",
                    "completed_at": _now_iso_utc(),
                }
                if final_loss is not None:
                    entry_updates["loss_summary"] = state_mod.loss_summary_from_profile(final_loss)
                    entry_updates["drift_count_snapshot"] = (
                        state_mod.drift_count_snapshot_from_profile(final_loss)
                    )
                    # Stamp the run's ADK/goldfive session id onto the
                    # live active-tournament entry so the dashboard can
                    # deep-link a finished board run into harmonograf
                    # (/#/session/<adk_session_id>) WITHOUT the SSE hot
                    # path ever opening events.jsonl. The LossProfile
                    # carries it; empty string when the run had none.
                    adk_sid = str(getattr(final_loss, "adk_session_id", "") or "")
                    if adk_sid:
                        entry_updates["adk_session_id"] = adk_sid
                state_mod.update_tournament_entry(
                    workspace_root,
                    entry.id,
                    side,
                    **entry_updates,
                )
            except Exception:  # noqa: BLE001
                pass


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
    if weights.regression_gate_enabled:
        regression = await run_regression_suite(
            child_snapshot_root,
            test_command=weights.regression_test_command,
            timeout_s=weights.regression_timeout_s,
        )
        if not regression.passed:
            return _regression_rejection(parent_agg, child_agg, regression)
    return evaluate_gate(
        parent_agg,
        child_agg,
        weights,
        holdout_parent_agg=holdout_parent_agg,
        holdout_child_agg=holdout_child_agg,
    )


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
) -> TournamentResult:
    """Run a full A/B tournament. See module docstring.

    ``child_diff_size`` is the OPT-IN parsimony / MDL input (OVERFITTING.md §5
    / §12 #4): the challenger generation's ``{added, removed, patches}`` diff
    size (see :func:`zicato.scoring.diff_complexity.diff_size`), threaded by the
    orchestrator from the child experiment's patch records. It folds a
    ``diff_complexity`` component into the CHALLENGER's scalar only when
    ``weights.diff_complexity_weight > 0``. ``None`` (every caller that does
    not opt in, and any ``diff_complexity_weight == 0.0`` contract) is
    byte-identical to today — the champion side never carries it, so the gate
    compares the challenger's diff against a parsimony-free baseline.

    ``force_fresh`` defaults to ``True`` — the historical behaviour, in
    which the rigorous full A/B path re-evaluates BOTH sides from scratch
    (no cache read) so a ``--mode full`` round always re-samples noise.
    The orchestrator's conservative crash-resume (RUNTIME.md §4) passes
    ``force_fresh=False`` for the one round it resumes in place, so the
    per-unit ``loss.json`` cache HITs every board unit the interrupted run
    already completed and only the unfinished entries re-run. Every other
    caller leaves the default, so behaviour is byte-identical to today.

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
    so a fresh epoch still scores the champion exactly once with no behaviour
    change. ``champion_force_fresh=True`` re-samples the champion too — the
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
    from zicato.core import assert_distinct_callables  # noqa: PLC0415

    assert_distinct_callables(config.harness_call_llm, config.auxiliary_call_llm)

    # Thread the board-level disable_drift onto each entry's context so
    # the adapter (running in a subprocess worker) can suppress the named
    # built-in judges. A no-op when the board has no board_meta header.
    board = _stamp_disable_drift(board, disable_drift)
    # Same threading for the board-level judge_only flag: the adapter
    # selects no-steering evaluation per entry off this context key. A
    # no-op when judge_only is False (the default), so the steering path
    # stays byte-identical.
    board = _stamp_judge_only(board, judge_only)

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
                workspace_root,
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

    try:
        # Board-unit scheduling: each board entry is one unit, and a
        # full-mode unit runs its champion (parent) and challenger
        # (child) runs CONCURRENTLY. ``config.parallelism`` bounds the
        # number of board units in flight — up to 2*parallelism run
        # subprocesses at once (champion + challenger per unit).
        #
        # CHILD (challenger) side — governed by ``force_fresh``. It defaults
        # to ``True`` (the historical full A/B semantics: a freshly proposed
        # generation has no prior evaluation under this contract, so it must
        # run). The orchestrator's conservative crash-resume passes
        # ``force_fresh=False`` for the one round it resumes in place: the
        # persisted per-unit ``loss.json`` of an interrupted round IS the
        # cache, so the units the interrupted run already completed cache-HIT
        # and only the unfinished entries re-run — resume is nearly free.
        #
        # CHAMPION (parent) side — governed by ``champion_force_fresh``. The
        # champion is immutable within the epoch, so it is cache-READ by
        # default (``champion_force_fresh=False``) — reused from a prior round
        # / its seed-scoring rather than needlessly re-run every round (§2
        # item 3). The first time it is seen it is a clean MISS and runs once,
        # then caches. ``champion_force_fresh=True`` re-samples the champion
        # too (the ``--mode full`` noise-resampling path).
        #
        # Both sides are persisted so a later fast round / structure can
        # reuse them.
        parent_losses, child_losses = await _run_board_units_full(
            adapter=adapter,
            parent_gen=parent_gen,
            child_gen=child_gen,
            board=board,
            weights=weights,
            config=config,
            workspace_root=workspace_root,
            epoch_id=epoch_id,
            force_fresh=force_fresh,
            parent_force_fresh=champion_force_fresh,
        )
    finally:
        if rt is not None:
            state_mod, _ = rt
            try:
                state_mod.clear_active_tournament(workspace_root)
            except Exception:  # noqa: BLE001
                pass

    # The scalar that gates promotion and steers selection / standings is
    # the TRAIN-slice scalar (OVERFITTING.md §12 #1). When the board is too
    # small to split — the common case and the default-safe degrade — the
    # train slice IS the full board, so these aggregates are byte-identical
    # to the pre-split full-board aggregates. The holdout slice (if any) is
    # confirmation-only and is threaded into the gate separately; it never
    # becomes the generation's reported score.
    parent_agg, child_agg = _train_aggs(
        board, parent_losses, child_losses, weights, epoch_id, child_diff_size=child_diff_size
    )
    holdout_parent_agg, holdout_child_agg = _holdout_aggs(
        board, parent_losses, child_losses, weights, epoch_id, child_diff_size=child_diff_size
    )

    # The regression check + the three train-slice rules decide on the TRAIN
    # aggregates only; the holdout is threaded separately through the Ladder
    # governor (OVERFITTING.md §4 / §12 #2) so its confirmation only *counts*
    # under the Ladder's release rule + per-epoch query budget. An absent
    # holdout (small board / split disabled) makes the Ladder a no-op, so the
    # decision stays byte-identical to Phase A.
    train_outcome = await _gate_with_regression(
        parent_agg=parent_agg,
        child_agg=child_agg,
        child_snapshot_root=child_gen.snapshot_root,
        weights=weights,
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
    )

    per_entry_losses: dict[str, tuple[LossProfile, LossProfile]] = {}
    for entry_id, parent_loss in parent_losses.items():
        child_loss = child_losses.get(entry_id)
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
            None if holdout_child_agg is None else float(holdout_child_agg["scalar"])
        ),
    )


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
    disable_drift: tuple[Any, ...] = (),
    judge_only: bool = False,
    round_index: int = 0,
    total_rounds: int = 0,
) -> TournamentResult:
    """Inline keep/discard against a historical aggregate.

    Runs only the child generation. Compares the result against the
    caller-supplied ``parent_historical_agg`` — typically the parent's
    last full-mode aggregate dict cached in the journal. Same gate
    logic, so the decision shape is identical to full mode. Per-entry
    losses contain only the child side; the parent tuple slot is left
    empty by storing the child's loss in both positions IS WRONG — we
    keep parent slot ``None``-equivalent by simply omitting parent
    losses from the per-entry map. (Fast mode has no parent
    per-entry loss profiles to report.)

    ``disable_drift`` is the board-level drift-suppression set, stamped
    onto each board entry's context exactly as in :func:`run_tournament`;
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
    from zicato.core import assert_distinct_callables  # noqa: PLC0415

    assert_distinct_callables(config.harness_call_llm, config.auxiliary_call_llm)

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
                cached = cached_per_entry.get(e.id) if isinstance(cached_per_entry, dict) else None
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
                workspace_root,
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
        child_losses = await _run_board_units_fast(
            adapter=adapter,
            child_gen=child_gen,
            board=board,
            weights=weights,
            config=config,
            workspace_root=workspace_root,
            epoch_id=epoch_id,
        )
    finally:
        if rt is not None:
            state_mod, _ = rt
            try:
                state_mod.clear_active_tournament(workspace_root)
            except Exception:  # noqa: BLE001
                pass

    # Fast mode compares the child against a cached whole-board historical
    # aggregate, so it does NOT thread a holdout into the gate: a train-only
    # child aggregate compared to a whole-board parent baseline would be an
    # apples-to-oranges scalar and could wrongly flip a decision. The
    # holdout-confirmation step lives on the full A/B path (the default
    # gauntlet promotion path); fast mode stays byte-identical to today.
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
    disable_drift: tuple[Any, ...] = (),
    judge_only: bool = False,
    round_index: int = 0,
    total_rounds: int = 0,
    match_id: str = "",
    fast: bool = False,
    matchup_budget_seconds: float | None = None,
    unit_semaphore: asyncio.Semaphore | None = None,
) -> TournamentResult:
    """Run ONE duel between two generations, ending in the unchanged gate.

    The selection-layer analogue of :func:`run_tournament`: it runs a
    single :class:`~zicato.selection.strategy.Matchup` between ``left_gen``
    and ``right_gen`` — champion-vs-challenger OR
    challenger-vs-challenger, since the gate only needs two aggregates and
    treats ``left`` as the nominal parent. It honours a ``board_subset``
    (racing rungs run on a board slice) and ``replicates`` (averaged
    paired runs), then aggregates and runs ``_gate_with_regression`` →
    ``evaluate_gate`` — the SAME gate, never re-decided.

    Returns a :class:`TournamentResult` whose ``parent_*`` fields describe
    ``left`` and ``child_*`` describe ``right``, so the strategy reads
    ``outcome.decision`` / ``outcome.delta_scalar`` exactly as the gauntlet
    does today.

    ``match_id`` is the strategy's id for THIS matchup (e.g. ``"rung0_m2"``,
    ``"racing-final"``). It is threaded down to every board-entry run so
    each persisted :class:`LossProfile` (and the analytical-index ``runs`` /
    ``loss_profiles`` rows) is tagged with the matchup it ran within —
    enabling per-run rung attribution in the dashboard. Empty string (the
    default) leaves runs untagged, which is exactly what the gauntlet path
    (via :func:`run_tournament`) does.

    ``fast`` is the structure-agnostic fast-mode champion-eval knob (the
    runtime ``--mode fast`` setting, threaded identically to
    ``disable_drift`` / ``judge_only``). When set, the ``left`` side is
    the CHAMPION and its per-board scalars are reused from the cached
    per-entry ``loss.json`` instead of being re-run — across EVERY
    structure that schedules matchups (racing / swiss / elim), exactly
    as the gauntlet's :func:`run_fast_mode` reuses the champion. The
    resolved mode (``"fast"`` / ``"fast-degraded"`` / ``"full"``) is
    recorded on the returned :attr:`TournamentResult.champion_eval_mode`
    for journal provenance; it never enters the gate or the contract.

    ``matchup_budget_seconds`` is an OPT-IN wall-clock cap on the duel's
    TOTAL board-unit execution. ``None`` (the default) ⇒ uncapped: every
    board unit × replicate × side runs to completion, byte-identical to
    today. When set, the runner tracks the running wall-clock total and,
    once it exceeds the cap, STOPS launching further board units; each
    un-run unit is recorded as a budget-exceeded
    :class:`~zicato.core.types.LossProfile` via the SAME aborted-run path a
    killed worker uses (so the partial aggregate scores consistently and the
    skipped unit is a cache hit next time). The cut-short event is LOGGED
    (how many units were skipped) — never silently truncated. This bounds
    the AGGREGATE of an unbounded board × replicates × both-sides sweep
    (e.g. a racing final rung), a different axis from the per-board
    :attr:`BoardEntry.wall_clock_budget_seconds` (which bounds ONE unit).

    ``unit_semaphore`` is the OPT-IN cross-matchup concurrency gate. When
    the orchestrator runs several matchups of a round concurrently it
    passes ONE shared semaphore to every matchup so all of the round's
    board units draw from a single global cap (instead of each matchup
    minting its own ``Semaphore(parallelism)`` — which let N concurrent
    matchups run ``N × parallelism`` units at once). ``None`` (every
    direct / gauntlet caller) ⇒ each board-unit runner mints its own,
    byte-identical to the single-matchup path.
    """
    from zicato.core import assert_distinct_callables  # noqa: PLC0415

    assert_distinct_callables(config.harness_call_llm, config.auxiliary_call_llm)

    board = _stamp_disable_drift(board, disable_drift)
    # Mirror the board-level judge_only threading the gauntlet path does in
    # run_tournament / run_fast_mode: stamp the flag onto each entry's
    # context so the adapter selects no-steering evaluation per entry. A
    # no-op when judge_only is False (the default), so the steering path
    # stays byte-identical. (Stamped before board_subset filtering so the
    # surviving slice carries it too.)
    board = _stamp_judge_only(board, judge_only)
    if board_subset is not None:
        subset = set(board_subset)
        board = [e for e in board if e.id in subset]

    left_losses, right_losses, champion_eval_mode, unit_provenance = await _run_replicated(
        adapter=adapter,
        left_gen=left_gen,
        right_gen=right_gen,
        board=board,
        weights=weights,
        config=config,
        workspace_root=workspace_root,
        epoch_id=epoch_id,
        replicates=replicates,
        match_id=match_id,
        fast=fast,
        matchup_budget_seconds=matchup_budget_seconds,
        unit_semaphore=unit_semaphore,
    )

    left_agg = aggregate_generation_score(list(left_losses.values()), weights)
    right_agg = aggregate_generation_score(list(right_losses.values()), weights)

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
) -> tuple[GateOutcome, dict[str, Any] | None, float | None]:
    """Ladder-mediate the holdout confirmation of a structure's crowning duel.

    The non-gauntlet structures (swiss / single_elim / double_elim / racing)
    resolve a leader/survivor through their bracket/swiss/racing logic — all
    scored on the TRAIN slice — and then run ONE final champion-gate duel of
    that survivor vs the reigning champion. ``train_outcome`` /
    ``train_parent_agg`` / ``train_child_agg`` are that crowning duel's
    TRAIN-slice gate verdict and aggregates (``train_outcome`` decided on the
    train slice, exactly like the gauntlet's ``run_tournament`` train gate).

    This function reuses the gauntlet's holdout machinery to add the SAME
    Ladder-mediated holdout confirmation on top of that crowning duel:

    1. Split the board into train / holdout via :func:`split_board` with the
       epoch-id :func:`rotation_seed` — identical to the gauntlet path. When
       the holdout is empty (small board / split disabled / no tagged entry)
       this returns ``(train_outcome, None, None)`` immediately, so the
       structure's decision is byte-identical to today's whole-board
       behaviour (the back-compat degrade).
    2. Otherwise run ONE additional duel — champion (``left``) vs survivor
       (``right``) — restricted to the HOLDOUT slice via ``board_subset``, to
       measure both sides' holdout-slice aggregates. The holdout is
       confirmation-only: it never picks the leader.
    3. Feed the train verdict + train/holdout aggregates through
       :func:`_ladder_mediated_outcome` — the same per-epoch
       :class:`~zicato.tournament.ladder.LadderState` at ``ladder_state_path``
       the gauntlet loads/saves, so the per-epoch query budget is SHARED
       across whichever path consults the holdout this epoch. A released
       non-confirmation flips the crowning promote to a ``rejected`` outcome
       (reason ``holdout_not_confirmed``); the champion stands.

    Returns ``(final_outcome, holdout_block, holdout_child_scalar)``:

    * ``final_outcome`` — the crowning verdict after holdout mediation (the
      orchestrator promotes iff it is ``"promoted"``).
    * ``holdout_block`` — the stable Ladder/holdout evidence dict (see
      :func:`zicato.tournament.ladder.holdout_record`) to journal verbatim
      under ``OutcomeRecord.holdout``; ``None`` when no holdout was consulted.
    * ``holdout_child_scalar`` — the challenger's holdout-slice scalar for the
      per-generation ``generalization_gap``; ``None`` when no holdout existed.

    Fast-mode note: ``fast`` is threaded to the holdout duel exactly as the
    internal matchups receive it, so the champion's holdout-slice board units
    are reused from the cache when already evaluated — the holdout
    confirmation is applied on the FULL path consistently, never silently
    skipped under ``--mode fast``.
    """
    from zicato.board.split import rotation_seed, split_board  # noqa: PLC0415

    seed = rotation_seed(weights.overfitting, epoch_id)
    _train_ids, holdout_ids = split_board(board, weights.overfitting, seed=seed)
    if not holdout_ids:
        # No holdout slice → byte-identical to today's whole-board decision.
        return train_outcome, None, None

    holdout_result = await run_matchup(
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
    )
    holdout_child_scalar = float(holdout_child_agg["scalar"])
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
