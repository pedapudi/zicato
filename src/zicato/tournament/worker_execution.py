"""Execute and clean up isolated evaluation workers."""

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
)
from zicato.core.measurement import (
    MeasurementDraw,
)
from zicato.driver_imports import with_workspace_imports
from zicato.epoch.genstore import EphemeralCheckout
from zicato.logging_stream import current_log_stream_path
from zicato.runtime.lock import WorkspaceLock
from zicato.runtime.spawn_permit import OPEN_PERMIT, WorkerPermit, acquire_worker_permit
from zicato.tournament.unit_cache import (
    _unit_loss_path,
)
from zicato.tournament.worker_transport import (
    _GENERATION_ID_CONTEXT_KEY,
    _PARENT_BUDGET_GRACE_S,
    _aborted_loss_profile,
    _checkout_run_snapshot,
    _configuration_spec,
    _discard_run_snapshot,
    _entry_measurement,
    _entry_to_dict,
    _ingest_run_into_index,
    _load_worker_result,
    _now_iso_utc,
    _resolve_harmonograf_grpc,
    _resolve_harmonograf_url,
    _run_id_for,
    _runtime_state,
    _stamp_measurement,
    _telemetry_helpers,
    _terminate_worker,
    _weights_spec,
    _worker_processes_gone,
    adapter_uses_integration,
    adapter_worker_spec,
    scrubbed_worker_env,
)
from zicato.util.async_tasks import finish_task as _finish_worker_task

log = logging.getLogger(__name__)

_PROGRESS_BUMP_MIN_INTERVAL_S = 2.0

_retained_worker_resources: dict[tuple[Path, int, float | None], _WorkerResources] = {}


@dataclass
class _WorkerResources:
    """Own one subprocess, its permit, and files until group exit is confirmed."""

    workspace_root: Path
    run_id: str
    args_path: Path
    result_path: Path
    permit: WorkerPermit = field(default_factory=lambda: OPEN_PERMIT)
    checkout: EphemeralCheckout | None = None
    proc: asyncio.subprocess.Process | None = None
    start_time: float | None = None
    pgid: int | None = None
    released: bool = False

    def processes_gone(self) -> bool:
        return self.proc is None or (
            self.pgid is not None
            and _worker_processes_gone(
                self.proc, expected_start_time=self.start_time, pgid=self.pgid
            )
        )

    def release(self) -> bool:
        if self.released:
            return False
        self.released = True
        self.permit.release()
        _discard_run_snapshot(self.checkout)
        for path in (self.args_path, self.result_path):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        rt = _runtime_state()
        if rt is not None:
            for cleanup in (rt[0].clear_worker_kill_request, rt[0].remove_active_run):
                try:
                    cleanup(self.workspace_root, self.run_id)
                except Exception as exc:  # noqa: BLE001 — independent best-effort cleanup
                    log.debug("run %s: resource cleanup failed: %s", self.run_id, exc)
        return True


async def _stop_worker(resources: _WorkerResources, supervisor_wait_s: float) -> bool:
    """Delegate termination, then use bounded fallback if the group remains alive."""
    if resources.processes_gone():
        return True
    rt = _runtime_state()
    if rt is not None:
        try:
            rt[0].request_worker_kill(resources.workspace_root, resources.run_id)
        except Exception as exc:  # noqa: BLE001 — fallback still owns termination
            log.debug("run %s: kill-request write failed: %s", resources.run_id, exc)
    deadline = time.monotonic() + supervisor_wait_s
    while time.monotonic() < deadline:
        if resources.processes_gone():
            return True
        await asyncio.sleep(min(0.05, max(0, deadline - time.monotonic())))
    assert resources.proc is not None
    if resources.start_time is None or resources.pgid is None:
        return False
    return await _terminate_worker(
        resources.proc, expected_start_time=resources.start_time, pgid=resources.pgid
    )


async def retry_worker_cleanup(workspace_root: Path) -> int:
    """Retry retained ownership once per worker; return the number fully released."""
    released = 0
    workspace_root = workspace_root.resolve()
    for key, resources in list(_retained_worker_resources.items()):
        if key[0] != workspace_root:
            continue
        terminated, cancelled = await _finish_worker_task(
            asyncio.create_task(_stop_worker(resources, supervisor_wait_s=0))
        )
        if terminated:
            released += resources.release()
            _retained_worker_resources.pop(key, None)
        if cancelled is not None:
            raise cancelled
    return released


async def drain_worker_cleanup(workspace_root: Path) -> None:
    """Wait on the owning loop until every retained worker has stopped.

    A bounded termination attempt can fail to confirm exit. The invocation
    keeps its writer and services while retries remain necessary. Cancellation
    propagates only after the retained owners have all been released.
    """
    workspace_root = workspace_root.resolve()
    cancelled: asyncio.CancelledError | None = None
    while any(key[0] == workspace_root for key in _retained_worker_resources):
        try:
            await retry_worker_cleanup(workspace_root)
        except asyncio.CancelledError as exc:
            cancelled = cancelled or exc
        except Exception as exc:  # noqa: BLE001 — ownership remains retained for retry
            log.debug("worker cleanup remains unconfirmed for %s: %s", workspace_root, exc)
        if any(key[0] == workspace_root for key in _retained_worker_resources):
            try:
                await asyncio.sleep(0.1)
            except asyncio.CancelledError as exc:
                cancelled = cancelled or exc
    if cancelled is not None:
        raise cancelled


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

    The throttle is a simple monotonic-clock gate: the first emit always bumps
    (so a freshly-started run animates immediately), and subsequent emits bump
    only after the interval has elapsed. A run that emits nothing simply never
    bumps — the supervisor's deadline logic still covers a wedged run.

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


@with_workspace_imports
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
    """Run one entry under one generation in an isolated subprocess worker.

    ``match_id`` is the tournament matchup this run executes within (e.g.
    ``"rung0_m2"``, ``"racing-final"``); empty string for a run that is
    not part of a tagged matchup (a gauntlet duel via
    :func:`run_tournament`, or an ad-hoc caller). After the worker settles,
    the runner stamps the matchup id onto the
    :class:`LossProfile` and rewrites the matching measurement loss file.
    ``zicato repair index`` reconstructs the matchup provenance from that file. The aborted
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

    1. Make a per-run **ephemeral checkout** of the generation's code
       snapshot (materialised by the workspace's generation store into a
       system-temp directory — a ``copytree`` under the directory
       backend, a per-run ``git worktree`` under the git backend) and
       point the worker at THAT, never at the canonical source tree. Any
       runtime write the agent makes near its own code lands in the
       throwaway checkout, so the canonical tree stays code-only and
       ``derive_generation`` does not carry runtime output forward. See
       :func:`_checkout_run_snapshot`.
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
       :class:`LossProfile` written to the measurement loss file. A worker that
       exited non-zero, OR a missing/corrupt result file (e.g. the
       SUPERVISOR SIGKILLed a wedged worker), is ALSO an aborted run —
       not a crash. The tournament continues to the next entry either
       way.
    7. Release the permit, checkout, and protocol files after worker-group exit.
       Cancellation waits through bounded teardown. Unconfirmed termination
       retains ownership for :func:`retry_worker_cleanup`.
    """
    entry = _stamp_measurement([entry], replace(_entry_measurement(entry), base_seed=config.seed))[
        0
    ]
    _, reducer_module = _telemetry_helpers()
    # The worker writes the loss identified by purpose, local draw, and seed.
    # Separate paths preserve each measurement's recorded provenance.

    loss_path = _unit_loss_path(
        workspace_root,
        epoch_id,
        generation.id,
        entry.id,
        _entry_measurement(entry),
        base_seed=config.seed,
    )
    from zicato.core.measurement import unit_artifact_name  # noqa: PLC0415
    from zicato.tournament.artifacts import archive_unit_artifacts  # noqa: PLC0415

    archive_unit_artifacts(loss_path)
    sink_path = loss_path.with_name(unit_artifact_name("events", _entry_measurement(entry)))
    run_id = _run_id_for(generation, entry, base_seed=config.seed)
    budget_s = float(entry.wall_clock_budget_seconds)

    rt = _runtime_state()

    # Best-effort tournament-entry transition for the live dashboard. The
    # worker writes the per-run ``active_runs`` file (with its own pid);
    # the orchestrator only owns the tournament-entry grid status.
    if rt is not None:
        state_mod, _ = rt
        try:
            state_mod.update_tournament_entry(
                writer,
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
    resources = _WorkerResources(workspace_root, run_id, args_path, result_path)
    teardown_task: asyncio.Task[bool] | None = None
    cancelled: asyncio.CancelledError | None = None

    # The run's final LossProfile — assigned on every exit path (clean
    # finish OR abort) so the ``finally`` block can fold the loss summary
    # into the live active-tournament record. Stays ``None`` only on
    # an unexpected hard crash, where the ``finally`` skips the fold.
    final_loss: LossProfile | None = None

    try:
        # --- 0. Host-wide worker permit. ``config.parallelism`` is a
        # per-PROCESS semaphore, so it cannot see a second orchestrator on
        # the same box; this permit is the cross-orchestrator bound. It is
        # taken BEFORE ``spawn_started`` is stamped so a queue wait never
        # inflates the run's reported ``runtime_ms``, and it covers the
        # snapshot checkout as well as the worker (the copytree is real
        # I/O worth bounding). AUTO by default and generous enough that a
        # single ordinary run never waits; degrades OPEN on any
        # infrastructure failure, so it can never block a run.
        resources.permit = await acquire_worker_permit(
            config.host_worker_permits,
            config.worker_permit_dir,
        )
        spawn_started = time.monotonic()
        try:
            # --- 1. Per-run ephemeral checkout of the code snapshot,
            # materialised by the workspace's generation store (a
            # copytree under the directory backend, a per-run git
            # worktree under the git backend). The worker is pointed at
            # this checkout, never at the canonical source tree, so any
            # runtime write the agent makes near its own code lands here
            # and is discarded with the checkout — the canonical tree
            # stays code-only and small.
            resources.checkout = _checkout_run_snapshot(
                workspace_root=workspace_root,
                epoch_id=epoch_id,
                generation=generation,
                run_id=run_id,
            )
            ephemeral_snapshot = resources.checkout.working_dir
            scratch_dir = resources.checkout.scratch_dir
            from zicato.models_config import (  # noqa: PLC0415
                ModelsConfig,
                execution_roles_for_runtime,
            )

            roles = json.loads(config.execution_roles or execution_roles_for_runtime(config))
            # Run provenance for the harness under test: the worker mounts
            # an EPHEMERAL snapshot copy with a throwaway name, so the
            # session cannot recover WHICH generation it is measuring from
            # its own root path. Stamp the generation id onto the
            # serialised entry's context (the one channel that survives
            # the worker round-trip — see _GENERATION_ID_CONTEXT_KEY);
            # a seeded/deterministic harness derives its per-run noise
            # from it. The in-process ``entry`` object is untouched.
            entry_dict = _entry_to_dict(entry)
            entry_dict["context"] = {
                **entry_dict.get("context", {}),
                _GENERATION_ID_CONTEXT_KEY: generation.id,
                "epoch_id": epoch_id,
            }
            harmonograf_metadata = {
                "zicato.epoch_id": epoch_id,
                "zicato.generation_id": generation.id,
                "zicato.entry_id": entry.id,
                "zicato.side": side,
                "zicato.measurement_purpose": str(_entry_measurement(entry).purpose),
                "zicato.measurement_draw": _entry_measurement(entry).draw,
                "zicato.trace_kind": "target",
            }
            runtime = _runtime_state()
            if runtime is not None:
                try:
                    active = runtime[0].read_active_tournament(workspace_root)
                    if active is not None:
                        harmonograf_metadata["zicato.tournament_id"] = active.tournament_id
                except Exception:  # noqa: BLE001 — telemetry labels are optional
                    pass
            if match_id:
                harmonograf_metadata["zicato.match_id"] = match_id
            adapter_spec = adapter_worker_spec(adapter)
            from zicato.core.run_context import RunContext  # noqa: PLC0415
            from zicato.core.runtime_context import (  # noqa: PLC0415
                TelemetryEndpoints,
                WorkerRuntimeContext,
            )
            from zicato.runtime.lock import pid_start_time  # noqa: PLC0415

            _hg_url = _resolve_harmonograf_url(workspace_root, config)
            _hg_grpc = _resolve_harmonograf_grpc(workspace_root, _hg_url, config)
            args_payload = {
                "measurement": replace(_entry_measurement(entry), base_seed=config.seed).to_json(),
                "entry": entry_dict,
                "adapter": adapter_spec,
                "driver_imports": config.driver_imports.document(),
                "target_role": roles["target"],
                "evaluation_role": roles["evaluation"],
                "judge_role": roles["judge"],
                "user_emulator_role": roles["user_emulator"],
                # The parent is the ONE producer of the run id: it stamps the
                # active_runs record the supervisor polices, so the worker must
                # not re-derive it from its own view of the entry (issue #250).
                "producer_pid": os.getpid(),
                "producer_start_time": pid_start_time(os.getpid()),
                "sink_events_path": str(sink_path),
                "loss_path": str(loss_path),
                "result_path": str(result_path),
                "harmonograf_metadata": harmonograf_metadata,
                "weights": _weights_spec(weights),
                "configuration": _configuration_spec(config),
                "runtime_context": WorkerRuntimeContext(
                    telemetry=TelemetryEndpoints(_hg_url, _hg_grpc),
                    run=RunContext(
                        workspace_root,
                        epoch_id,
                        generation.id,
                        run_id,
                        ephemeral_snapshot,
                        scratch_dir,
                    ),
                ).to_json(),
                # The invocation's operator-log stream path (LOGGING.md §2):
                # the worker APPENDS its structured records to the SAME file
                # the orchestrator installed, so worker logs reach the one
                # per-invocation stream. Absent (None) when no stream is
                # installed (an ad-hoc / test drive) — the worker then logs
                # to stderr only.
                "log_stream_path": (
                    str(_lsp) if (_lsp := current_log_stream_path()) is not None else None
                ),
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
                runtime_ms=0,
                match_id=match_id,
                abort_cause="prepare_failed",
            )
            return final_loss

        # --- 3. Spawn the worker subprocess. --- ``start_new_session=True``
        # runs the worker in its OWN session and process-group (it calls
        # ``setsid`` before ``exec``), so the worker leads a group containing
        # itself plus any grandchildren the system under test spawns (shells,
        # helper tools). The worker records that group's id (``pgid``) on its
        # ActiveRun record, letting the supervisor GROUP-kill the whole tree by
        # negating the pgid rather than leaking grandchildren when it kills the
        # worker pid alone. It also detaches the worker from the orchestrator's
        # controlling terminal so a Ctrl-C / SIGINT to the orchestrator's
        # terminal group is not broadcast straight into every in-flight worker.
        # Compose the worker's environment. When the operator opts into
        # ``scrub_worker_env`` the worker gets a MINIMAL explicit env
        # (process-essential keys + the api_key_env names the configured roles
        # need + any passthrough), so a mutated worker cannot read every
        # credential in the process env.
        try:
            worker_env: dict[str, str] | None = None
            if config.scrub_worker_env:
                goldfive_secret_names: tuple[str, ...] = ()
                if weights.goldfive is not None and adapter_uses_integration(
                    adapter_spec, "goldfive"
                ):
                    from zicato.integrations.goldfive import secret_env_names  # noqa: PLC0415

                    goldfive_secret_names = secret_env_names(weights.goldfive)
                worker_env = scrubbed_worker_env(
                    models=ModelsConfig(),
                    secret_env_keys=goldfive_secret_names
                    + tuple(
                        name
                        for document in roles.values()
                        for name in (
                            document.get("models_role", {}).get("api_key_env"),
                            document.get("transport", {}).get("api_key_env"),
                        )
                        if name
                    ),
                    extra_env_keys=tuple(config.worker_env_passthrough),
                )

            async def spawn() -> asyncio.subprocess.Process:
                proc = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-m",
                    "zicato._tournament_worker",
                    str(args_path),
                    start_new_session=True,
                    env=worker_env,
                )
                resources.proc = proc
                # start_new_session establishes this group even if the leader
                # exits before getpgid can observe it.
                resources.pgid = proc.pid
                resources.start_time = pid_start_time(proc.pid)
                try:
                    resources.pgid = os.getpgid(proc.pid)
                except (AttributeError, OSError):
                    pass
                return proc

            proc, cancelled = await _finish_worker_task(asyncio.create_task(spawn()))
            if cancelled is not None:
                raise cancelled
        except (AttributeError, ImportError, OSError, TypeError, ValueError) as exc:
            log.warning("run %s could not spawn its worker subprocess: %s", run_id, exc)
            final_loss = _aborted_loss_profile(
                run_id=run_id,
                entry=entry,
                generation_id=generation.id,
                epoch_id=epoch_id,
                runtime_ms=max(0, int((time.monotonic() - spawn_started) * 1000)),
                match_id=match_id,
                abort_cause="prepare_failed",
            )
            return final_loss

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
            teardown_task = asyncio.create_task(
                _stop_worker(resources, config.supervisor_kill_wait_s)
            )
            await asyncio.shield(teardown_task)

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
            # measurement event JSONL on disk most likely lacks a terminal
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
                runtime_ms=runtime_ms,
                match_id=match_id,
                abort_cause=abort_cause,
            )
            return final_loss

        # --- 6. Clean exit. Read the LossProfile the worker wrote. ---
        # The worker may itself have aborted via its OWN cooperative
        # budget — that is still a clean worker exit (exit code 0, result
        # file present) and the measurement loss file it wrote already carries
        # ``wall_clock_budget_exceeded=True``. We just read it back.
        loss_profile_path_str = str(result.get("loss_profile_path", loss_path))
        try:
            loss: LossProfile = reducer_module.read_loss_profile(Path(loss_profile_path_str))
            index = _entry_measurement(entry)
            expected = replace(index, base_seed=config.seed)
            if "measurement" in result:
                if MeasurementDraw.from_json(result["measurement"]) != expected:
                    raise ValueError("worker result measurement differs from the requested draw")
            if loss.measurement != expected:
                raise ValueError("worker loss measurement differs from the requested draw")
        except (OSError, KeyError, ValueError, json.JSONDecodeError) as exc:
            # The worker said it finished cleanly but its measurement loss file is
            # unreadable — treat as aborted rather than crashing.
            log.warning("run %s: worker loss record unreadable: %s", run_id, exc)
            final_loss = _aborted_loss_profile(
                run_id=run_id,
                entry=entry,
                generation_id=generation.id,
                epoch_id=epoch_id,
                runtime_ms=runtime_ms,
                match_id=match_id,
                abort_cause="result_unreadable",
            )
            return final_loss

        # Record matchup provenance in the measurement loss file so
        # ``zicato repair index`` reconstructs the same attribution.
        if match_id:
            loss = replace(loss, match_id=match_id)
            try:
                reducer_module.write_loss_profile(loss, Path(loss_profile_path_str))
            except OSError as exc:  # noqa: BLE001 — provenance rewrite is best-effort
                log.debug("run %s: match_id update in loss record skipped: %s", run_id, exc)

        # Live index dual-write: the run's measurement loss file is on disk (now
        # carrying match_id when tagged), so fold it into the SQLite
        # analytical index. Best-effort.
        _ingest_run_into_index(workspace_root, epoch_id, generation.id, entry.id)
        final_loss = loss
        return final_loss
    except asyncio.CancelledError as exc:
        cancelled = exc
        raise
    finally:
        terminated = resources.processes_gone()
        if not terminated:
            if teardown_task is None:
                teardown_task = asyncio.create_task(
                    _stop_worker(resources, config.supervisor_kill_wait_s)
                )
            try:
                terminated, repeated_cancel = await _finish_worker_task(teardown_task)
                cancelled = cancelled or repeated_cancel
            except asyncio.CancelledError as exc:
                cancelled = cancelled or exc
            except Exception as exc:  # noqa: BLE001 — retain ownership on failed cleanup
                log.error("run %s: worker termination failed: %s", run_id, exc)
        if terminated:
            resources.release()
        else:
            assert resources.proc is not None
            key = (workspace_root.resolve(), resources.proc.pid, resources.start_time)
            _retained_worker_resources[key] = resources
            log.error(
                "run %s: worker group termination is unconfirmed; retaining process %s, "
                "permit, checkout, and protocol files for retry_worker_cleanup",
                run_id,
                resources.proc.pid,
            )
        if rt is not None and terminated:
            state_mod, _ = rt
            try:
                # Fold the run's per-entry loss summary into the live
                # active-tournament record so the dashboard renders a per-entry
                # score the instant the run finishes — rather than leaving
                # ``loss_summary`` empty until the journal materialises. The
                # shape is pinned by ``state.loss_summary_from_profile`` /
                # ``drift_count_snapshot_from_profile`` (the Zone-B contract).
                # ``final_loss`` is set on every clean-finish AND abort path;
                # it is ``None`` only after an unexpected hard crash, where we
                # fall back to the bare status transition.
                entry_updates: dict[str, Any] = {
                    "status": "aborted" if cancelled is not None else "completed",
                    "completed_at": _now_iso_utc(),
                }
                if final_loss is not None and cancelled is None:
                    entry_updates["loss_summary"] = state_mod.loss_summary_from_profile(final_loss)
                    entry_updates["drift_count_snapshot"] = (
                        state_mod.drift_count_snapshot_from_profile(final_loss)
                    )
                    # Stamp the run's ADK/goldfive session id onto the
                    # live active-tournament entry so the dashboard can
                    # deep-link a finished board run into harmonograf
                    # (/#/session/<adk_session_id>) WITHOUT the SSE hot
                    # path ever opening measurement event JSONL. The LossProfile
                    # carries it; empty string when the run had none.
                    adk_sid = str(getattr(final_loss, "adk_session_id", "") or "")
                    if adk_sid:
                        entry_updates["adk_session_id"] = adk_sid
                state_mod.update_tournament_entry(
                    writer,
                    entry.id,
                    side,
                    **entry_updates,
                )
            except Exception:  # noqa: BLE001
                pass
        if cancelled is not None:
            raise cancelled
