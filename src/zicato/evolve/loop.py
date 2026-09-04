"""The N-round evolve loop split out of :mod:`zicato.orchestrator`.

:func:`evolve_n_rounds` calls :func:`zicato.evolve.round_entry.evolve_once` up to
``rounds`` times, with the four loop circuit-breakers modelled as a small
:class:`StopPolicy` set:

* :class:`ConsecutiveRejectionPolicy` — stop after N rejected rounds in a row;
* :class:`DegenerateHealthPolicy` — stop after N consecutive CRITICAL
  loop-health findings;
* :class:`WallClockBudgetPolicy` — the total wall-clock ceiling, enforced
  both between rounds and (via :func:`asyncio.wait_for`) within a round.

The policies are pure bookkeeping over the existing counters/thresholds;
the loop drives them in the unchanged order and emits the unchanged log
lines and symbolic stop-reason strings. This is a behaviour-preserving
move — :func:`evolve_n_rounds` keeps its exact signature and is re-exported
from :mod:`zicato.orchestrator`.

Collaborators are imported from their owning phase modules. Tests patch those
owners directly; the dispatcher is not a private seam registry.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING

from zicato.epoch.preflight import PreflightRefusedError
from zicato.logging_stream import install_log_stream, set_log_context
from zicato.runtime.heartbeat import HeartbeatBeater
from zicato.runtime.lock import acquire_workspace_lock, release_workspace_lock
from zicato.runtime.resume import (
    ResumePlan,
    discard_interrupted_generation,
    prepare_resume,
)
from zicato.util import best_effort

if TYPE_CHECKING:
    from zicato.evolve.round_api import EvolveRoundOutcome

log = logging.getLogger("zicato.orchestrator")

CallLLM = Callable[[str, str, str], Awaitable[str]]


def emit_dialect_capability_warnings(workspace_root: Path) -> tuple[str, ...]:
    """Surface the telemetry-dialect capability warnings ONCE, at contract-load.

    ``dialect_capability_warnings(weights)`` (LOGGING.md §6) is a pure
    function of the contract's :class:`ScoringWeights` — e.g. a drift knob
    left inert under a drift-incapable telemetry dialect — so it belongs at
    the invocation-level preflight, NOT inside the per-board-unit reducer
    (where it fired N times, invisibly, inside the killable worker). Each
    warning is logged once at ``WARNING`` (captured by the structured
    stream AND shown on the operator's console). Returns the warnings it
    emitted so a test can assert the single-surface relocation. Best-effort
    at the call site; here we only need the loaded weights.
    """
    from zicato import workspace_loader  # noqa: PLC0415
    from zicato.telemetry.dialects import dialect_capability_warnings  # noqa: PLC0415

    weights = workspace_loader.load_current_scoring(workspace_root)
    warnings = dialect_capability_warnings(weights)
    for warning in warnings:
        log.warning(
            "contract pre-flight [telemetry dialect %s]: %s",
            weights.telemetry_dialect,
            warning,
        )
    return warnings


def log_effective_concurrency(workspace_root: Path) -> str:
    """Log the run's effective concurrency knobs ONCE, at invocation start.

    ``parallelism`` decides how many board units run at a time and defaults to
    4 regardless of the host. Unlogged, it leaves an operator on a 64-core box
    watching four units in flight with nothing to say a ceiling is in force,
    and nothing to separate "the default is holding you at 4" from "the work is
    simply slow" (issue #126). The same holds for its propose-side analogue and
    for the host-wide worker permit count, whose AUTO resolution is
    host-dependent and so unguessable from the config file alone.

    So report all three, plus the cores the process may actually run on and
    the tier ``parallelism`` was resolved from
    (:func:`zicato.runtime_factory.resolve_parallelism`). Emitted from
    :func:`evolve_n_rounds` rather than the per-round
    ``make_runtime_config`` call so the operator gets one line per run, not
    one per round. Returns the rendered line so a test can assert it.
    """
    from zicato.runtime.spawn_permit import (  # noqa: PLC0415
        _usable_cpus,
        effective_permit_count,
    )
    from zicato.runtime_factory import (  # noqa: PLC0415
        resolve_host_worker_permits,
        resolve_parallelism,
    )
    from zicato.workspace.config_io import read_workspace_config  # noqa: PLC0415

    runtime_dict = read_workspace_config(workspace_root).runtime
    parallelism, source = resolve_parallelism(runtime_dict)
    propose_raw = runtime_dict.get("propose_parallelism")
    propose_parallelism = int(propose_raw) if propose_raw is not None else 4
    permits_limit, _permits_source = resolve_host_worker_permits(runtime_dict)
    permits = effective_permit_count(permits_limit)
    if permits_limit is None:
        permits_text = f"AUTO -> {permits}"
    elif permits == 0:
        permits_text = "off"
    else:
        permits_text = str(permits)

    line = (
        f"evolve run configuration: parallelism={parallelism} (from {source}), "
        f"propose_parallelism={propose_parallelism}, "
        f"host_worker_permits={permits_text}, usable CPUs={_usable_cpus()}"
    )
    log.info("%s", line)
    return line


async def _sleep_for_backoff(seconds: float) -> None:
    """The endpoint-outage backoff sleep — a seam so tests can stub it."""
    await asyncio.sleep(seconds)


def _infra_backoff_knobs(workspace_root: Path) -> tuple[float, float]:
    """Read the endpoint-outage backoff ``(base_s, cap_s)`` for this workspace.

    The evolve loop needs only these two scalars from the runtime block, so
    it reads them directly off the workspace config (the same
    ``runtime.infra_backoff_*`` keys :func:`zicato.runtime_factory
    .make_runtime_config` threads onto :class:`~zicato.core.runtime
    .RuntimeConfig`) rather than resolving a full config with its LLM
    callables. Best-effort: any read failure falls back to the shared
    dataclass defaults, clamped non-negative.
    """
    from zicato.core.runtime import (  # noqa: PLC0415
        INFRA_BACKOFF_BASE_S_DEFAULT,
        INFRA_BACKOFF_CAP_S_DEFAULT,
    )

    base, cap = INFRA_BACKOFF_BASE_S_DEFAULT, INFRA_BACKOFF_CAP_S_DEFAULT
    try:
        from zicato.workspace.config_io import read_workspace_config  # noqa: PLC0415

        runtime = read_workspace_config(workspace_root).runtime
        raw_base = runtime.get("infra_backoff_base_s")
        raw_cap = runtime.get("infra_backoff_cap_s")
        if raw_base is not None:
            base = float(raw_base)
        if raw_cap is not None:
            cap = float(raw_cap)
    except Exception as exc:  # noqa: BLE001 — the backoff must never fail the loop
        log.debug("infra backoff knobs unavailable (%s); using defaults", exc)
    return max(0.0, base), max(0.0, cap)


def _epoch_round_base(workspace_root: Path, epoch_id: str | None) -> int:
    """The next ``round_index`` for ``epoch_id`` — one past its highest
    already-persisted round.

    Re-running ``evolve`` on an EXISTING (un-rolled) epoch must CONTINUE that
    epoch's round numbering rather than restart at 0. The loop counter is
    invocation-local (``range(rounds)``), but ``round_index`` is persisted on
    each generation and the dashboard groups generations by it — so a restart
    collides the new field with the prior invocation's rounds in one bucket
    (the "v9 lands in Round 0 next to v1–v4" bug). Returns
    ``max(persisted round_index) + 1``, or ``0`` for a fresh / unreadable epoch
    (the answer for a brand-new epoch, whose first round is 0).

    A PARENTLESS generation is skipped: the epoch's seed is CARRIED (copied from
    the registered trees, or from a rolled predecessor's promoted head), never
    minted by a round, so it does not represent a round already spent. It still
    persists ``round_index: 0`` — ``write_seed_experiment`` builds it with the
    ``Experiment.round_index`` default — so counting it started a seeded-but-unrun
    epoch's first real field at 1 and left a phantom round 0 in the timeline. This
    is the same rule the round-timeline reader uses to identify the seed (the
    parentless generation), so writer and reader agree on what a round is.
    """
    if not epoch_id:
        return 0
    from zicato.epoch.journal import read_epoch_experiments  # noqa: PLC0415

    best = -1
    try:
        records, _unreadable = read_epoch_experiments(workspace_root, epoch_id)
        for _generation_id, experiment in records:
            if not experiment.parent_generation_id:
                continue  # the seed is carried rather than minted, so it is no round
            best = max(best, experiment.round_index)
    except Exception:  # noqa: BLE001 — a missing / locked workspace ⇒ base 0
        return 0
    return best + 1


#: Default threshold for the loop-health circuit breaker: this many
#: consecutive rounds with a CRITICAL loop-health finding stops the
#: evolve loop early. Two is tight by design: one CRITICAL round could be a
#: transient (a single degenerate tournament, say), but two in a row means
#: the loop is producing no signal.
_DEGENERATE_HEALTH_STOP_THRESHOLD = 2


def _append_progress_seq(workspace_root: Path, transition: str) -> int | None:
    """Append a loop-level progress transition; return its ``seq`` or ``None``.

    The loop appends genuine loop transitions
    (:data:`progress_log.LOOP_START` / :data:`~progress_log.ROUND_START` /
    the terminal :data:`~progress_log.SETTLED` / :data:`~progress_log.STOPPED`)
    so the heartbeat's ``seq`` advances on real progress, never on the
    timer. Returns the new tail ``seq`` to stamp onto the heartbeat, or
    ``None`` on a write failure — passing ``None`` to
    :meth:`HeartbeatBeater.update` leaves the prior ``seq`` unchanged, so a
    log hiccup never regresses or fabricates the cursor. Best-effort: a
    progress-log failure must never abort the loop.
    """
    seq: int | None = None

    def _remember(value: int) -> None:
        nonlocal seq
        seq = value

    with best_effort(
        "progress-log append",
        on_error=lambda exc: log.debug("progress-log append skipped: %s", exc),
    ):
        from zicato.runtime import progress_log  # noqa: PLC0415

        _remember(progress_log.append_progress(workspace_root, transition))
    return seq


def _budget_aborted_outcome(parent_generation_id: str, budget_s: int) -> EvolveRoundOutcome:
    """Build the synthetic outcome for a round cut short by the total budget.

    Used when a single round's work is cancelled by
    :func:`asyncio.wait_for` because finishing it would push the whole
    ``evolve_n_rounds`` invocation past ``max_wall_clock_seconds``. The
    round never produced a real tournament decision, so we fabricate a
    rejection-style outcome whose ``rejection_reason`` is the symbolic
    ``"wall_clock_budget"`` string — the same token the per-entry
    budget uses for its aborts — so journal readers and the CLI can
    recognise it.
    """
    from zicato.evolve.round_api import EvolveRoundOutcome  # noqa: PLC0415

    return EvolveRoundOutcome(
        parent_generation_id=parent_generation_id,
        proposed_generation_id="",
        tournament_decision="rejected",
        rejection_reason=f"wall_clock_budget: evolve total budget of {budget_s}s exceeded",
        parent_scalar=0.0,
        child_scalar=0.0,
        delta_scalar=0.0,
    )


async def _apply_rubric_replacement(
    workspace_root: Path,
    payload: str,
    *,
    auto_epoch: bool,
    aux_call_llm: CallLLM,
    epoch_name: str | None,
) -> str:
    """Apply an operator ``rubric_replacement`` as a contract edit + epoch roll.

    The proposer brief is part of the evaluation contract (board + brief +
    scoring + harness identity). Replacing it mid-loop must NOT be a silent
    in-place patch, because generations on either side of the edit are not
    comparable. So this helper:

    1. Writes the operator's payload to the LIVE proposer brief (the same
       ``brief_path`` :func:`zicato.epoch.contract.resolve_contract_inputs`
       hashes into the contract).
    2. Re-runs :func:`ensure_epoch_for_contract`, which sees the drifted
       contract hash and rolls a fresh epoch (closing the current one,
       baselining from its promoted head) when ``auto_epoch`` is set.

    Returns the epoch id the loop should pin for every subsequent round (the
    rolled epoch when the brief drifted the contract; the current epoch if a
    no-op replacement somehow left the hash unchanged).
    """
    from zicato.epoch.contract import resolve_contract_inputs  # noqa: PLC0415
    from zicato.evolve.epoching import ensure_epoch_for_contract  # noqa: PLC0415

    brief_path = resolve_contract_inputs(workspace_root).brief_path
    brief_path.parent.mkdir(parents=True, exist_ok=True)
    brief_path.write_text(payload, encoding="utf-8")
    log.warning(
        "evolve: operator rubric_replacement — wrote %d bytes to the live "
        "proposer brief %s and rolling the epoch (contract edit)",
        len(payload),
        brief_path,
    )
    # Re-resolve the epoch: the drifted contract hash rolls a fresh epoch.
    return await ensure_epoch_for_contract(
        workspace_root,
        auto_epoch=auto_epoch,
        aux_call_llm=aux_call_llm,
        epoch_name=epoch_name,
    )


# ---------------------------------------------------------------------------
# Loop circuit-breakers — a small StopPolicy set
# ---------------------------------------------------------------------------


class ConsecutiveRejectionPolicy:
    """Stop after ``limit`` rejected rounds in a row.

    A promotion resets the run; ``limit <= 0`` is treated as "never stop
    early" by the caller (which normalises it to ``rounds + 1`` before
    constructing this policy), so this object always sees a positive limit.
    """

    reason = "consecutive_rejections"

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._streak = 0

    def observe(self, *, promoted: bool) -> bool:
        """Record a round's promotion verdict; return ``True`` to stop."""
        if promoted:
            self._streak = 0
            return False
        self._streak += 1
        return self._streak >= self._limit

    @property
    def streak(self) -> int:
        return self._streak


class DegenerateHealthPolicy:
    """Stop after ``threshold`` consecutive CRITICAL loop-health rounds.

    A round whose health is not CRITICAL resets the streak. When
    ``enabled`` is false the policy never fires (the opt-out path) and never
    advances its streak.
    """

    reason = "degenerate_health"

    def __init__(self, *, enabled: bool, threshold: int) -> None:
        self._enabled = enabled
        self._threshold = threshold
        self._streak = 0

    def observe(self, *, health_critical: bool) -> bool:
        """Record a round's health verdict; return ``True`` to stop."""
        if self._enabled and health_critical:
            self._streak += 1
            return self._streak >= self._threshold
        self._streak = 0
        return False

    @property
    def streak(self) -> int:
        return self._streak


class WallClockBudgetPolicy:
    """The total wall-clock ceiling for the whole ``evolve_n_rounds`` call.

    Records a monotonic start so a wall-clock adjustment mid-run can't move
    the deadline. ``ceiling_s is None`` leaves the loop unbounded (the
    historical behaviour) — :meth:`enabled` is then false and every check is
    a no-op.
    """

    def __init__(self, ceiling_s: int | None) -> None:
        self._ceiling = ceiling_s
        self._start = time.monotonic()

    @property
    def enabled(self) -> bool:
        return self._ceiling is not None

    @property
    def ceiling_s(self) -> int | None:
        return self._ceiling

    def between_rounds_exhausted(self) -> bool:
        """``True`` when the budget is already spent before the next round."""
        if self._ceiling is None:
            return False
        return (time.monotonic() - self._start) >= self._ceiling

    def remaining_s(self) -> float:
        """Remaining budget, clamped to a tiny positive slice.

        Only meaningful when :meth:`enabled`; the between-rounds check has
        already returned for an exhausted budget, so the clamp guards only
        against a vanishing / negative slice.
        """
        assert self._ceiling is not None
        remaining = self._ceiling - (time.monotonic() - self._start)
        return max(remaining, 0.001)


async def evolve_n_rounds(
    *,
    rounds: int,
    workspace_root: Path,
    epoch_id: str | None = None,
    harness_call_llm: CallLLM,
    auxiliary_call_llm: CallLLM,
    instance_id: str = "default",
    fast_mode: bool = False,
    max_consecutive_rejections: int = 3,
    max_proposer_retries: int = 2,
    auto_epoch: bool = True,
    epoch_name: str | None = None,
    stop_on_degenerate_health: bool = True,
    max_wall_clock_seconds: int | None = None,
    stop_reason_out: list[str] | None = None,
) -> list[EvolveRoundOutcome]:
    """Loop :func:`evolve_once` up to ``rounds`` times.

    Stops early on ``max_consecutive_rejections`` rejected rounds in a
    row — that's a strong signal the proposer is stuck and the
    operator probably wants to inspect the proposer brief / patterns
    before spending more LLM calls. A successful promotion resets the
    consecutive-rejection counter.

    A second circuit breaker watches loop *health*: when
    ``stop_on_degenerate_health`` is true (the default), the loop stops early
    once :data:`_DEGENERATE_HEALTH_STOP_THRESHOLD` consecutive rounds report a
    CRITICAL loop-health finding (e.g. degenerate scoring, where the tournament
    cannot tell a real improvement from noise). Same shape as the
    consecutive-rejection breaker: there is no point spending more LLM calls on
    a loop that is producing no usable signal. A round whose health is not
    CRITICAL resets the counter. Pass ``stop_on_degenerate_health=False`` to
    opt out and run every requested round regardless of health.

    A third early-exit is the **total wall-clock budget**: when
    ``max_wall_clock_seconds`` is set (``None``, the default, leaves the
    loop unbounded), the orchestrator records
    a monotonic start time and enforces the ceiling two ways:

    * **Between rounds** — before starting round N+1, if the elapsed
      time has already reached the budget, the loop stops cleanly with
      a logged message and returns the outcomes gathered so far, in the
      same shape as the consecutive-reject breaker.
    * **Within a round** — each round's work is wrapped in
      :func:`asyncio.wait_for` with a timeout equal to the *remaining*
      budget, so a single long round cannot blow the total. A round
      that would exceed the ceiling is cancelled; it is recorded as an
      aborted round (a synthetic :class:`EvolveRoundOutcome` carrying a
      ``"wall_clock_budget"`` rejection reason) and the loop stops.

    The total budget is enforced *in addition to* — not instead of —
    each board entry's own ``wall_clock_budget_seconds``; both apply.
    Note the within-round cancellation is a Layer-1 ``asyncio.wait_for``
    guard (see ``docs/design/ROBUSTNESS.md``): it only pre-empts
    *cooperative* async work. A round wedged in a blocking call or a
    CPU-bound loop is not hard-killed here — that requires the
    subprocess-worker boundary. This is the same contract the
    per-entry budget relies on.

    The mandatory workspace gate runs first, before auto-epoching or any
    model call. Contract-hash auto-epoching then runs ONCE, before the round
    loop: when ``epoch_id`` is ``None`` and ``auto_epoch`` is true, the
    orchestrator resolves (and, if the contract drifted, auto-rolls) the epoch
    via :func:`ensure_epoch_for_contract`. The resolved id is then pinned for
    every round of this invocation so the loop never re-rolls mid-flight.
    When ``epoch_id`` is passed explicitly, auto-rolling is skipped. The
    workspace gate still verifies that the selected epoch matches its frozen
    contract before any model call.

    The list of :class:`EvolveRoundOutcome` returned has one entry per
    round attempted (which may be fewer than ``rounds`` if any
    early-stop fired).

    ``stop_reason_out`` is an optional caller-supplied list the function
    appends a single symbolic terminal-reason string to before
    returning, so a caller (the CLI) can render a summary that
    distinguishes the terminal states without re-deriving them from the
    outcomes. One of: ``"completed"`` (all rounds ran),
    ``"consecutive_rejections"``, ``"degenerate_health"``,
    ``"wall_clock_budget_between_rounds"`` (the total budget was already
    spent before the next round started), or
    ``"wall_clock_budget_mid_round"`` (a round was cancelled because
    finishing it would overrun the total budget). Callers that do not
    pass the list see no behavioural change.
    """
    from zicato import workspace_loader  # noqa: PLC0415
    from zicato.evolve.dashboard_projection import _mark_run_terminal  # noqa: PLC0415
    from zicato.evolve.epoching import ensure_epoch_for_contract  # noqa: PLC0415
    from zicato.evolve.ingest import index_preflight  # noqa: PLC0415
    from zicato.evolve.lifecycle_services import (  # noqa: PLC0415
        _build_meta_loop_emitter_safe,
        _now_iso,
        _resolve_or_launch_harmonograf,
    )
    from zicato.evolve.round_api import DEFERRED_INFRA_DECISION  # noqa: PLC0415
    from zicato.evolve.round_entry import evolve_once  # noqa: PLC0415
    from zicato.runtime.control_consumer import (  # noqa: PLC0415
        block_while_paused,
        claim_rubric_replacement,
        claim_skip_round,
    )

    def _set_stop_reason(reason: str) -> None:
        if stop_reason_out is not None:
            stop_reason_out.append(reason)

    if rounds <= 0:
        _set_stop_reason("completed")
        return []

    # One of the two public spend boundaries (``evolve_once`` is the other,
    # and gates itself the same way). Keeping the gate here rather than in a
    # command-layer helper is what makes it unbypassable: a library caller
    # reaches it without going through the CLI. It must precede
    # auto-epoching, because resolving contract drift may call the auxiliary
    # model. Every round below is then handed ``workspace_checked=True``, so
    # a multi-round invocation pays for the gate once.
    # Imported per call rather than at module scope. Suites that drive this
    # loop against a minimal fixture workspace patch
    # ``zicato.check.require_workspace_valid``; hoisting this import would
    # bind the function once and silently defeat every one of those patches.
    from zicato.check import require_workspace_valid  # noqa: PLC0415

    require_workspace_valid(
        workspace_root,
        epoch_id=epoch_id,
        live_contract=epoch_id is None,
    )

    if max_consecutive_rejections <= 0:
        # 0 / negative effectively disables early-stop — protect against
        # nonsense values by treating them as "never stop early".
        max_consecutive_rejections = rounds + 1

    # The structured operator-log stream (LOGGING.md) for THIS invocation.
    # Installed here — the shared orchestrator entrypoint — so every
    # ``zicato.*`` ``log.*`` call for the whole loop is captured into
    # ``.zicato/logs/<stamp>-<pid>.jsonl`` with zero call-site changes.
    # Acquired outside the try (like the lock below) and closed in the same
    # ``finally``. Best-effort: a logging-setup failure never fails the run.
    log_level = (
        workspace_loader.load_workspace_config(workspace_root)
        .get("runtime", {})
        .get("log_level", "INFO")
    )
    log_stream = install_log_stream(workspace_root, level=str(log_level))
    # Tag every orchestrator record with the pinned epoch for this
    # invocation (optional enrichment; the workers bind the full
    # epoch/generation/run context on their side).
    set_log_context(epoch_id=epoch_id)
    # Install the contract's declared mutation-site syntax table for this
    # process, before anything enumerates. Deliberately NOT best-effort: a
    # workspace that declares a file type and then enumerates without it
    # would present a narrower surface than its own contract, silently.
    declared_suffixes = workspace_loader.activate_mutation_surface(workspace_root)
    if declared_suffixes:
        log.info("mutation surface: declared file types %s", ", ".join(declared_suffixes))
    # Contract-load preflight: surface the telemetry-dialect capability
    # warnings ONCE per invocation (relocated out of the per-entry reducer),
    # beside the epoch-open preflight machinery below. Best-effort.
    with best_effort(
        "dialect-capability preflight warnings",
        on_error=lambda exc: log.debug("dialect-capability preflight skipped: %s", exc),
    ):
        emit_dialect_capability_warnings(workspace_root)
    # Run-start configuration report (issue #126): the concurrency ceilings
    # in force for this invocation, named once so an operator can tell a
    # deliberate cap from an unremarked default. Best-effort, like every
    # other preflight here — a config read must never fail a run.
    with best_effort(
        "concurrency configuration report",
        on_error=lambda exc: log.debug("concurrency configuration report skipped: %s", exc),
    ):
        log_effective_concurrency(workspace_root)

    # Workspace lock + heartbeat lifecycle. The lock keeps two concurrent
    # orchestrators from corrupting the same workspace; the beater writes
    # ``heartbeat.json`` so the supervisor binary can detect a wedge.
    lock = acquire_workspace_lock(workspace_root, instance_id)
    _prepared_plan = ResumePlan()
    try:
        # Reconcile the open epoch under the workspace lock before contract
        # drift can close it or choose its promoted head as a new baseline.
        # A complete receipt is replayed; a receiptless field is discarded;
        # and a resumable single challenger is retained unless a contract roll
        # makes its cached evaluation incomparable with the new contract.
        if epoch_id is None:
            from zicato.epoch.lifecycle import current_epoch_id  # noqa: PLC0415

            current_epoch = current_epoch_id(workspace_root)
            if current_epoch is not None:
                _prepared_plan = prepare_resume(workspace_root, current_epoch)

            def discard_resume_before_roll(rolling_epoch: str) -> None:
                nonlocal _prepared_plan
                if not _prepared_plan.resumes_in_place:
                    return
                generation_id = _prepared_plan.resume_generation_id
                experiment = _prepared_plan.resume_experiment
                assert generation_id is not None and experiment is not None
                discard_interrupted_generation(
                    workspace_root,
                    rolling_epoch,
                    generation_id,
                    round_index=experiment.round_index,
                )
                _prepared_plan = ResumePlan(
                    discarded_generation_id=generation_id,
                    classification="discard_unrecorded_field",
                )

            epoch_id = await ensure_epoch_for_contract(
                workspace_root,
                auto_epoch=auto_epoch,
                aux_call_llm=auxiliary_call_llm,
                epoch_name=epoch_name,
                before_contract_roll=discard_resume_before_roll,
            )
            set_log_context(epoch_id=epoch_id)
            if epoch_id != current_epoch:
                _prepared_plan = prepare_resume(workspace_root, epoch_id)
        else:
            _prepared_plan = prepare_resume(workspace_root, epoch_id)
    except BaseException:
        release_workspace_lock(lock)
        with best_effort(
            "operator-log stream close after epoch resolution failure",
            on_error=lambda exc: log.debug("operator-log stream close raised: %s", exc),
        ):
            log_stream.close()
        raise
    # Conservative crash-resume reconciliation (RUNTIME.md §4, ROBUSTNESS.md
    # §2.6) — runs ONCE, right after the lock is held and before any new
    # work. It clears the stale runtime/ state of a prior dead evolve and,
    # if the prior run was interrupted mid-tournament with completed board
    # units on disk, returns a plan to resume that generation in place
    # (reuse the persisted experiment so the unit cache HITs the done
    # units). On ANY ambiguity it discards the partial generation so the
    # round re-runs fresh. A clean workspace yields the default no-op plan,
    # so a cold start resumes nothing. The plan is consumed by
    # the FIRST round only; later rounds pass ``None``.
    resume_plan: ResumePlan | None = (
        None if _prepared_plan.classification == "clean" else _prepared_plan
    )
    # Rebuild or heal the derived index only after recovery has finished all
    # canonical writes and discarded any receiptless field. Cleanup can safely
    # invalidate a stale index without owning a second rebuild path; this one
    # preflight reconstructs the final canonical state under the workspace
    # lock before proposer memory reads it. Failure remains non-fatal because
    # the filesystem is authoritative.
    with best_effort(
        "index self-heal preflight",
        on_error=lambda exc: log.debug("index self-heal preflight skipped: %s", exc),
    ):
        log.info("%s", index_preflight(workspace_root))
    # The orchestrator progress event log is the TRUE
    # liveness signal (its monotonic ``seq`` advances only on a genuine
    # transition, never on the heartbeat timer). Clear any prior invocation's
    # log so this one's ``seq`` starts from 1 — a stale tail must never read
    # as live progress. Best-effort: a clear failure must not block the run.
    from zicato.runtime import progress_log  # noqa: PLC0415

    with best_effort(
        "progress-log clear",
        on_error=lambda exc: log.debug("progress-log clear skipped: %s", exc),
    ):
        progress_log.clear_log(workspace_root)
    beater = HeartbeatBeater(workspace_root, instance_id, interval_s=2.0)
    # Resolve the harmonograf console URL once up front so the supervisor
    # / dashboard can surface a "watch live" link from the heartbeat for
    # the whole invocation. When no URL is configured (the default after
    # #202) the supervisor auto-launches an in-process harmonograf bound
    # to a free localhost port; the handle is shut down in the finally
    # block. The auto-launched URL is also pushed into ZICATO_HARMONOGRAF_URL
    # so per-board-run workers attach their per-run sinks to the same
    # server without any further plumbing.
    harmonograf_url, harmonograf_handle = _resolve_or_launch_harmonograf(workspace_root)
    # Meta-loop goldfive emitter. One per evolve invocation, stable
    # session id derived from the start ISO — the proposer + analyzer
    # call sites take it through ``evolve_once`` so their LLM calls
    # land as paired envelopes on the same harmonograf timeline workers
    # already feed. Constructed best-effort; a degraded install (no
    # goldfive proto stubs) returns an emitter with an empty sink list
    # and every emit is a no-op. The emitter is closed in the same
    # ``finally`` block that tears the harmonograf supervisor down.
    evolve_started_at_iso = _now_iso()
    meta_loop_emitter = _build_meta_loop_emitter_safe(
        workspace_root, harmonograf_url, evolve_started_at_iso
    )
    # Bind the emitter as the ambient meta-loop emitter so the structural
    # spans (round / phase / matchup / worker / slate slot) can be opened by
    # deep call sites — the runner, the board-unit scheduler, the best-of-N
    # slate — without threading it through every signature. Every descendant
    # asyncio task inherits the binding; it is reset in the teardown finally.
    from zicato.telemetry.meta_loop import (  # noqa: PLC0415
        reset_current_emitter,
        set_current_emitter,
    )

    _emitter_token = set_current_emitter(meta_loop_emitter)
    outcomes: list[EvolveRoundOutcome] = []
    try:
        await beater.start()
        # The meta-loop session id is surfaced on the heartbeat so the
        # dashboard can deep-link the top-bar "execution" entry into the
        # zicato-level harmonograf session (the proposer + judge timeline).
        # Read it off the emitter — empty when no meta-loop session is in
        # scope. See docs/design/HARMONOGRAF.md §2b/§4.
        meta_session = getattr(meta_loop_emitter, "session_id", "") or ""
        # First genuine transition: the loop booted (epoch resolved, lock
        # held). Stamp its seq so a reader sees a live, advancing cursor
        # from the very first beat. Best-effort.
        loop_start_seq = _append_progress_seq(workspace_root, progress_log.LOOP_START)
        beater.update(
            epoch_id=epoch_id or "",
            phase="evolve_n_rounds:start",
            seq=loop_start_seq,
            harmonograf_url=harmonograf_url,
            harmonograf_meta_session=meta_session,
        )
        beater.bump_now()
        reject_policy = ConsecutiveRejectionPolicy(max_consecutive_rejections)
        health_policy = DegenerateHealthPolicy(
            enabled=stop_on_degenerate_health,
            threshold=_DEGENERATE_HEALTH_STOP_THRESHOLD,
        )
        # Total wall-clock budget — a monotonic clock so a wall-clock
        # adjustment mid-run can't move the deadline. ``None`` leaves
        # the loop unbounded.
        budget = WallClockBudgetPolicy(max_wall_clock_seconds)
        budget_stopped = False
        stop_reason = "completed"
        # Endpoint-outage backoff state: consecutive deferred rounds
        # double the delay from base up to the cap; any settled round
        # resets the streak. Knobs read once — workspace config is static
        # for the life of one evolve invocation.
        infra_backoff_base_s, infra_backoff_cap_s = _infra_backoff_knobs(workspace_root)
        infra_deferral_streak = 0
        # The CUMULATIVE round index for the pinned epoch. The loop counter
        # ``round_idx`` is invocation-local (0..rounds-1, used only for the
        # "round X of N" budget messages); the PERSISTED ``round_index`` CONTINUES
        # the epoch's existing numbering so a re-run of evolve on the same epoch
        # does not collide its new field with a prior invocation's rounds.
        # Recomputed below if a rubric replacement rolls the epoch mid-loop.
        epoch_round_index = _epoch_round_base(workspace_root, epoch_id)
        for round_idx in range(rounds):
            # Between-rounds budget check — before spending the next
            # round's LLM calls, bail if the total budget is spent.
            # Same shape as the consecutive-reject breaker above.
            if budget.between_rounds_exhausted():
                log.warning(
                    "evolve_n_rounds: evolve total wall-clock budget of %ds "
                    "reached after %d rounds (round %d/%d)",
                    max_wall_clock_seconds,
                    round_idx,
                    round_idx,
                    rounds,
                )
                budget_stopped = True
                stop_reason = "wall_clock_budget_between_rounds"
                break

            # --- Operator control protocol, between-rounds safe point ---
            # BEFORE scheduling the next round:
            #
            #  * pause_epoch — block scheduling while the flag is present;
            #    return only once the operator clears it (resume gesture).
            #  * rubric_replacement — a CONTRACT edit: write the operator's
            #    new proposer brief to the live brief and let contract-hash
            #    auto-epoching roll the epoch (never a silent in-place patch).
            #    The rolled epoch id is re-pinned for every subsequent round.
            #
            # A stale skip_round flag is drained here too: between rounds
            # there is no in-flight round to abort, so it is archived as a
            # no-op rather than firing on the next round. (The live skip is
            # claimed at the top of evolve_once, the round it targets.)
            block_while_paused(workspace_root)
            claim_skip_round(workspace_root)
            _rubric = claim_rubric_replacement(workspace_root)
            if _rubric is not None:
                epoch_id = await _apply_rubric_replacement(
                    workspace_root,
                    _rubric.payload,
                    auto_epoch=auto_epoch,
                    aux_call_llm=auxiliary_call_llm,
                    epoch_name=epoch_name,
                )
                # The rubric replacement rolled the epoch (a contract edit) — the
                # new epoch is fresh, so restart its round numbering from its own
                # base (0 for a brand-new epoch) rather than continuing the prior
                # epoch's count.
                epoch_round_index = _epoch_round_base(workspace_root, epoch_id)
                # Re-tag the operator log with the rolled epoch id.
                set_log_context(epoch_id=epoch_id)

            round_start_seq = _append_progress_seq(workspace_root, progress_log.ROUND_START)
            beater.update(
                epoch_id=epoch_id or "",
                round_index=epoch_round_index,
                round_started_at=_now_iso(),
                seq=round_start_seq,
                phase=f"evolve_once:round_{epoch_round_index}",
            )
            beater.bump_now()

            # The resume plan is consumed by the first round only; clear it
            # afterwards so a later round always proposes fresh.
            round_resume_plan = resume_plan
            resume_plan = None

            async def _run_round(
                _round_idx: int = epoch_round_index,
                _resume_plan: ResumePlan | None = round_resume_plan,
                # Bind the epoch as a default arg so a rubric_replacement that
                # rolled ``epoch_id`` earlier this iteration is captured by
                # value (not late-bound). The reassignment above means the
                # closure must snapshot the current epoch rather than the loop
                # variable.
                _epoch_id: str | None = epoch_id,
            ) -> EvolveRoundOutcome:
                from zicato.telemetry.meta_loop import SPAN_ROUND, meta_span  # noqa: PLC0415

                # The round span frames every phase / matchup / worker / slot
                # of this round on the meta-loop timeline (HARMONOGRAF.md §7).
                async with meta_span(
                    f"round {_round_idx}",
                    kind=SPAN_ROUND,
                    meta={"round_index": _round_idx, "epoch_id": _epoch_id or ""},
                ):
                    return await evolve_once(
                        workspace_root=workspace_root,
                        epoch_id=_epoch_id,
                        harness_call_llm=harness_call_llm,
                        auxiliary_call_llm=auxiliary_call_llm,
                        instance_id=instance_id,
                        fast_mode=fast_mode,
                        max_proposer_retries=max_proposer_retries,
                        beater=beater,
                        round_index=_round_idx,
                        total_rounds=rounds,
                        meta_loop_emitter=meta_loop_emitter,
                        resume_plan=_resume_plan,
                        workspace_checked=True,
                    )

            try:
                if not budget.enabled:
                    # Unbounded — run the round with no within-round ceiling.
                    outcome = await _run_round()
                else:
                    remaining = budget.remaining_s()
                    try:
                        # Layer-1 asyncio.wait_for guard: a round that would
                        # push past the total budget is cancelled. This only
                        # pre-empts cooperative async work — a round wedged
                        # in a blocking call or CPU-bound loop is not
                        # hard-killed here; that needs the subprocess worker
                        # boundary. Same caveat as the per-entry budget. See
                        # docs/design/ROBUSTNESS.md.
                        outcome = await asyncio.wait_for(_run_round(), timeout=remaining)
                    except TimeoutError:
                        # asyncio.wait_for raises the builtin TimeoutError
                        # (asyncio.TimeoutError is an alias of it on 3.11+).
                        assert max_wall_clock_seconds is not None
                        from zicato.evolve.generation_phase import safe_parent  # noqa: PLC0415

                        parent_id = safe_parent(workspace_root, epoch_id)
                        outcome = _budget_aborted_outcome(parent_id, max_wall_clock_seconds)
                        outcomes.append(outcome)
                        log.warning(
                            "evolve_n_rounds: round %d aborted — evolve total wall-clock "
                            "budget of %ds exceeded mid-round; stopping (round %d/%d)",
                            round_idx,
                            max_wall_clock_seconds,
                            round_idx + 1,
                            rounds,
                        )
                        budget_stopped = True
                        stop_reason = "wall_clock_budget_mid_round"
                        break
            except PreflightRefusedError as exc:
                # Achievable-signal HARD gate (issue #84,
                # runtime.preflight_gate == "refuse"): the contract cannot
                # out-signal its own noise. No round ran — stop cleanly BEFORE
                # spending budget, with a clear reason (never a traceback).
                log.warning("evolve_n_rounds: %s", exc)
                stop_reason = "preflight_refused"
                break
            outcomes.append(outcome)
            beater.update(
                epoch_id=epoch_id or "",
                generation_id=outcome.proposed_generation_id,
                round_index=epoch_round_index,
                phase=f"after_round_{epoch_round_index}:{outcome.tournament_decision}",
            )
            beater.bump_now()
            # Best-effort epoch-report refresh so file:// readers (and the
            # dashboard's static fallback) see the latest lineage
            # immediately after each round, including the rounds that end
            # before the round epilogue's own refresh. Deterministic and
            # digest-gated: a round that moved no data rewrites nothing.
            with best_effort(
                "in-progress epoch report refresh",
                on_error=lambda exc: log.debug("in-progress epoch report refresh skipped: %s", exc),
            ):
                from zicato.epoch.analysis import (  # noqa: PLC0415
                    regenerate_in_progress_html,
                )
                from zicato.epoch.lifecycle import current_epoch_id  # noqa: PLC0415

                eid = epoch_id or current_epoch_id(workspace_root)
                if eid:
                    regenerate_in_progress_html(workspace_root, eid)
            if outcome.tournament_decision == DEFERRED_INFRA_DECISION:
                # Endpoint-outage deferral: the round burned NO experiment — it
                # persists un-outcomed on disk. Back off (exponential, capped)
                # instead of re-proposing straight into a dead endpoint, then
                # hand the next round the SAME conservative reconciliation a
                # crash-resume performs: resume the deferred generation in
                # place when any board unit completed (the unit cache HITs the
                # done work), discard it cleanly when none did. Deliberately
                # skips BOTH stop-policies below — a deferral is evidence about
                # the endpoint rather than about the experiment stream, so it
                # must neither count toward consecutive rejections nor
                # reset/advance the degenerate-health streak.
                infra_deferral_streak += 1
                delay = min(
                    infra_backoff_cap_s,
                    infra_backoff_base_s * (2 ** (infra_deferral_streak - 1)),
                )
                log.warning(
                    "evolve_n_rounds: round %d/%d deferred on the endpoint-outage "
                    "circuit (deferral %d in a row); backing off %.1fs before the "
                    "next round",
                    round_idx + 1,
                    rounds,
                    infra_deferral_streak,
                    delay,
                )
                if delay > 0 and round_idx + 1 < rounds:
                    # No sleep after the FINAL round — there is nothing left
                    # to back off for; the reconciliation below still runs
                    # so the workspace is left clean for the next evolve.
                    beater.update(phase=f"infra_backoff:round_{epoch_round_index}:{delay:g}s")
                    beater.bump_now()
                    await _sleep_for_backoff(delay)
                _reconciled = prepare_resume(workspace_root, epoch_id or "")
                resume_plan = None if _reconciled.classification == "clean" else _reconciled
                epoch_round_index += 1
                continue
            infra_deferral_streak = 0
            if reject_policy.observe(promoted=outcome.tournament_decision == "promoted"):
                # The streak count alone says the loop stopped without saying
                # why it could not promote. The last round's gate reason carries the
                # deciding numbers, so it rides along (issue #129).
                log.warning(
                    "evolve_n_rounds: stopping after %d consecutive rejections "
                    "(round %d/%d); last rejection: %s",
                    reject_policy.streak,
                    round_idx + 1,
                    rounds,
                    outcome.rejection_reason or "(no reason recorded)",
                )
                stop_reason = reject_policy.reason
                break
            # Loop-health circuit breaker — stop early when the loop has
            # produced no usable signal for too many rounds running.
            if health_policy.observe(health_critical=outcome.health_critical):
                # Name the finding that tripped the breaker. The streak count
                # says a CRITICAL fired N rounds running; the round's health
                # summary says WHICH detector fired, with its measured
                # quantities and remediation (issue #129).
                log.warning(
                    "evolve_n_rounds: stopping after %d consecutive rounds with a "
                    "CRITICAL loop-health finding (round %d/%d) — %s. The loop is "
                    "producing no usable signal; inspect the scoring weights / "
                    "proposer brief before resuming. (Pass "
                    "stop_on_degenerate_health=False to opt out.)",
                    health_policy.streak,
                    round_idx + 1,
                    rounds,
                    outcome.health_summary or "no finding recorded",
                )
                stop_reason = health_policy.reason
                break
            # Advance the cumulative epoch round index for the next iteration
            # (the break paths above skip this — the loop is ending anyway).
            epoch_round_index += 1
        # Terminal progress marker — a clean, orchestrator-produced end.
        # A reader distinguishes this SETTLED/STOPPED tail from a STALLED
        # one (seq frozen mid-flight, no terminal event). ``budget_stopped``
        # (a budget / circuit-breaker cut) is STILL a clean end, marked
        # STOPPED to distinguish it from a fully-completed SETTLED run.
        terminal_seq = _append_progress_seq(
            workspace_root,
            progress_log.STOPPED if budget_stopped else progress_log.SETTLED,
        )
        beater.update(
            phase="evolve_n_rounds:budget_exhausted" if budget_stopped else "evolve_n_rounds:done",
            seq=terminal_seq,
        )
        beater.bump_now()
    finally:
        # Defensive terminal-state write (issue: a dead/closed run reading
        # LIVE). A cleanly-ended loop stamps a terminal heartbeat phase
        # above; here we ALSO flip any lingering active-tournament envelope
        # out of ``phase="running"`` so a normally-ended run never reads as a
        # live tournament — even inside the heartbeat freshness window. Runs
        # on BOTH the clean and the error/interrupt path (a SIGKILL still
        # can't self-clean, which the frontend freshness gate covers).
        _mark_run_terminal(workspace_root)
        await beater.stop()
        release_workspace_lock(lock)
        # Remove the operator-log handler + restore the logger level. Best-
        # effort: teardown failures never propagate out of the ``finally``.
        with best_effort(
            "operator-log stream close",
            on_error=lambda exc: log.debug("operator-log stream close raised: %s", exc),
        ):
            log_stream.close()
        # Flush + close the meta-loop emitter BEFORE the harmonograf
        # supervisor is stopped — a sink that needs to push a final
        # buffer to the gRPC console wants the server still up.
        reset_current_emitter(_emitter_token)
        if meta_loop_emitter is not None:
            with best_effort(
                "meta-loop emitter close",
                on_error=lambda exc: log.debug("meta-loop emitter close raised: %s", exc),
            ):
                await meta_loop_emitter.close()
        # Shut down the auto-launched harmonograf server (no-op on the
        # opt-out / failure-isolation paths). MUST run unconditionally
        # so a crashed evolve still tears the embedded server down.
        with best_effort(
            "harmonograf shutdown",
            on_error=lambda exc: log.debug("harmonograf shutdown raised: %s", exc),
        ):
            harmonograf_handle.shutdown()
    _set_stop_reason(stop_reason)
    return outcomes
