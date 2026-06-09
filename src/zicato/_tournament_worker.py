"""Subprocess worker that executes ONE tournament run in its own OS process.

This module is the "L3" robustness layer. Historically every board-entry
run executed *inside* the orchestrator process, which meant a wedged run
could only be stopped by killing the whole ``evolve``. Isolating each run
as its own subprocess lets a per-run wall-clock budget be hard-enforced:

* the parent (:func:`zicato.tournament.runner._run_single`) wraps the
  worker in :func:`asyncio.wait_for` and escalates SIGTERM -> SIGKILL,
* an independent supervisor watchdog can SIGKILL a worker whose
  :attr:`zicato.runtime.state.ActiveRun.deadline` has passed — keyed on
  the worker's own pid — without ever touching the orchestrator.

Run it as::

    python -m zicato._tournament_worker <args-file.json>

The args file describes exactly one run (see :func:`_load_args` for the
shape). The worker:

1. writes ``active_runs/{run_id}.json`` with ``pid = os.getpid()`` — its
   OWN pid, the key difference from the old in-process model where the
   orchestrator stamped its own pid;
2. loads the harness from the ``snapshot_root`` it was handed — a per-run
   ephemeral working copy of the generation's code snapshot, NOT the
   canonical ``generations/vN/snapshot/`` (the parent makes the copy so
   any runtime write the agent does near its own code cannot pollute the
   canonical snapshot) — and drives the one entry under goldfive
   (mirroring the runner's old ``_drive_session``);
3. computes the :class:`~zicato.core.LossProfile` via
   :func:`zicato.telemetry.reducer.reduce_loss` and writes ``loss.json``;
4. writes a result file (the :class:`~zicato.core.RunResult` plus the
   loss-profile path, runtime, and aborted flag) as JSON;
5. on a clean exit removes its ``active_runs`` file.

Module-caching note
-------------------
Because every run is a fresh subprocess, the Python-module-caching
problem the old single-process runner had — loading two generations'
source into one interpreter and getting the wrong one back from
``sys.modules`` — simply does not arise here. Each worker imports the
one generation snapshot it was handed and then exits; there is never a
second generation's source in the same interpreter to collide with.

The worker is deliberately killable: if SIGTERM/SIGKILL'd mid-run it
just dies, leaving (at worst) a stale ``active_runs`` file and no result
file. The parent treats "process gone + no result file" as a normal
aborted-run outcome — that is precisely the supervisor-kill path.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import sys
import time
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from zicato.core import (
    BoardEntry,
    LossProfile,
    RunResult,
    RuntimeConfig,
    ScoringWeights,
    validate_board_entry,
)
from zicato.import_path import import_dotted_path

log = logging.getLogger("zicato._tournament_worker")

#: Symbolic ``abort_reason`` the worker stamps onto its synthesised
#: result when its own cooperative ``asyncio.wait_for`` budget fires.
#: The parent maps a clean exit carrying this reason to "worker hit its
#: own budget" (as opposed to a parent- or supervisor-driven kill).
WORKER_BUDGET_ABORT_REASON = "wall_clock_budget"


#: Reason string the worker stamps onto the synthesised ``run_aborted``
#: telemetry frame it emits when its cooperative budget fires. Aligned
#: with :attr:`zicato.core.LossProfile.wall_clock_budget_exceeded` so a
#: downstream consumer can correlate the loss profile and the event
#: stream without string surgery.
TERMINAL_REASON_WALL_CLOCK = "wall_clock_budget_exceeded"


# ---------------------------------------------------------------------------
# Args / result file shapes
# ---------------------------------------------------------------------------


def _import_callable(dotted: str) -> Any:
    """Resolve a ``pkg.mod:attr`` or ``pkg.mod.attr`` dotted path to a callable.

    Delegates to :func:`zicato.import_path.import_dotted_path` so the
    colon-separated (entry-point style) and dot-separated forms are handled
    by the single shared implementation.
    """
    obj: Any = import_dotted_path(dotted, label="call_llm dotted path")
    if not callable(obj):
        raise ValueError(f"call_llm dotted path {dotted!r} did not resolve to a callable")
    return obj


def _resolve_role_call_llm(spec: Any, *, role: str) -> Any:
    """Resolve one role's worker spec to a text call_llm in this interpreter.

    The spec is the dict the runner emitted (see
    :func:`zicato.tournament.runner._role_worker_spec`):

    * ``{"dotted": "module:qualname"}`` — re-import the callable (legacy /
      unconfigured role); or
    * ``{"models_role": {...}}`` — a workspace ``models.<role>`` spec, which
      this worker re-resolves with the same machinery the runtime factory
      uses (reading any ``api_key_env`` from the worker's OWN os.environ).
    """
    if not isinstance(spec, dict):
        raise ValueError(f"{role} role spec must be a JSON object, got {type(spec).__name__}")
    dotted = spec.get("dotted")
    if dotted:
        return _import_callable(str(dotted))
    raw_role = spec.get("models_role")
    if isinstance(raw_role, dict):
        from zicato.models_config import resolve_text_call_llm, role_spec_from_dict  # noqa: PLC0415

        return resolve_text_call_llm(role_spec_from_dict(raw_role), role=role)
    raise ValueError(f"{role} role spec has neither 'dotted' nor 'models_role': {spec!r}")


def _load_args(args_path: Path) -> dict[str, Any]:
    """Read and minimally validate the worker's JSON args file.

    The args file shape (one run)::

        {
          "workspace_root": "<abs path to .zicato dir>",
          "epoch_id": "<epoch id>",
          "generation_id": "<generation id>",
          "snapshot_root": "<abs path to a per-run code-snapshot working copy>",
          "scratch_dir": "<abs path to a per-run scratch dir OUTSIDE the snapshot>",
          "entry": { ...BoardEntry as a dict (validate_board_entry shape)... },
          "adapter": {
            "kind": "adk",
            "entrypoint": "module.path:agent_symbol",
            "mutable_trees": ["<abs path>", ...]
          },
          "harness_role":   {"dotted": "pkg.module:callable"} | {"models_role": {...}},
          "auxiliary_role": {"dotted": "pkg.module:callable"} | {"models_role": {...}},
          "judge_role":     {"dotted": "pkg.module:callable"} | {"models_role": {...}},
          "sink_events_path": "<abs path to events.jsonl>",
          "loss_path": "<abs path to loss.json>",
          "result_path": "<abs path the worker writes its result JSON to>",
          "instance_id": "default",
          "seed": null,
          "harmonograf_url": ""
        }
    """
    with open(args_path, encoding="utf-8") as f:
        args: dict[str, Any] = json.load(f)
    required = (
        "workspace_root",
        "epoch_id",
        "generation_id",
        "snapshot_root",
        "entry",
        "adapter",
        "harness_role",
        "auxiliary_role",
        "sink_events_path",
        "loss_path",
        "result_path",
    )
    missing = [k for k in required if k not in args]
    if missing:
        raise ValueError(f"worker args file missing keys: {missing}")
    return args


def _build_adapter(spec: dict[str, Any]) -> Any:
    """Reconstruct a harness adapter from its serialised spec dict.

    Two spec shapes are understood:

    * ``{"kind": "adk", "entrypoint": ..., "mutable_trees": [...]}`` — the
      production shape; reconstructs an
      :class:`~zicato.adapters.adk.ADKHarnessAdapter` directly (without
      going through :func:`zicato.adapter_factory.make_adapter_from_config`,
      so the worker needs no full workspace-config dict).
    * ``{"kind": "import", "factory": "module:callable", "args": [...]}``
      — a generic shape for any non-ADK adapter. The dotted path is
      imported and called with the optional positional ``args`` to
      produce the adapter object. The runner does not emit this shape
      today, but it keeps the worker decoupled from a single concrete
      adapter implementation.
    """
    kind = spec.get("kind")
    if kind == "adk":
        from zicato.adapters.adk import ADKHarnessAdapter  # noqa: PLC0415

        entrypoint = str(spec["entrypoint"])
        raw_trees = spec.get("mutable_trees") or []
        trees = [Path(t) for t in raw_trees] if raw_trees else None
        return ADKHarnessAdapter(entrypoint=entrypoint, mutable_trees=trees)
    if kind == "import":
        factory = _import_callable(str(spec["factory"]))
        return factory(*spec.get("args", []))
    raise ValueError(f"worker cannot reconstruct adapter kind {kind!r}")


# ---------------------------------------------------------------------------
# Sink wiring
# ---------------------------------------------------------------------------


def _build_sinks(
    events_path: Path, harmonograf_url: str, harmonograf_grpc: str = ""
) -> tuple[list[Any], Any]:
    """Build the per-run sink list: canonical JSONL plus optional harmonograf.

    Returns ``(sinks, tracker)`` where ``tracker`` is the
    :class:`~zicato.telemetry.terminal_event.SequenceTrackingSink`
    decorating the canonical JSONL sink (or ``None`` when goldfive is
    not installed and no sinks were attached). The tracker holds the
    last ``run_id`` and the max ``sequence`` that flowed through, so
    the worker can synthesise a ``run_aborted`` lifecycle frame on a
    wall-clock cancellation — the goldfive runner's own
    ``_emit_run_aborted`` is unreachable when ``asyncio.wait_for``
    cancels the inner task, leaving the events file without a
    terminal frame and the downstream transcript reconstructor unable
    to flip ``complete=True``.

    Returns ``([], None)`` when goldfive is not installed — matching
    the runner's pre-existing tolerance for a no-goldfive environment.
    """
    try:
        from goldfive.sinks.persistence import JSONLPersistenceSink  # noqa: PLC0415
    except ModuleNotFoundError:
        return [], None

    from zicato.telemetry.terminal_event import SequenceTrackingSink  # noqa: PLC0415

    events_path.parent.mkdir(parents=True, exist_ok=True)
    tracker = SequenceTrackingSink(JSONLPersistenceSink(path=events_path, mode="write"))
    sinks: list[Any] = [tracker]

    if harmonograf_url:
        try:
            from zicato.telemetry.sink import _make_harmonograf_sink  # noqa: PLC0415

            # ``harmonograf_grpc`` carries the native gRPC dial target the
            # runner resolved (the auto-launched server's grpc_port, NOT
            # the browser-facing gRPC-Web port in ``harmonograf_url``). An
            # empty target falls back to deriving from the web URL — the
            # external-harmonograf single-port path.
            extra = _make_harmonograf_sink(harmonograf_url, grpc_target=harmonograf_grpc or None)
            if extra is not None:
                sinks.append(extra)
        except Exception as exc:  # noqa: BLE001 — harmonograf is additive only
            log.warning("worker could not attach harmonograf sink: %s", exc)
    return sinks, tracker


async def _close_sinks(sinks: list[Any]) -> None:
    """Best-effort close of every sink so the JSONL is flushed to disk."""
    for s in sinks:
        try:
            await s.close()
        except Exception as exc:  # noqa: BLE001 — never fail a run on sink close
            log.debug("worker sink close failed: %s", exc)


async def _emit_worker_abort(
    *,
    sinks: list[Any],
    tracker: Any,
    reason: str,
) -> None:
    """Emit a ``run_aborted`` lifecycle frame through the open sinks.

    Called from the worker's ``finally`` block on the cooperative
    wall-clock cancellation path, *before* sinks are closed. Goldfive's
    own ``_emit_run_aborted`` is unreachable when ``asyncio.wait_for``
    cancels the inner task — :class:`CancelledError` propagates through
    every ``try`` / ``except Exception`` inside ``goldfive.run``. The
    worker therefore takes responsibility for the terminal frame.

    ``tracker`` is the :class:`SequenceTrackingSink` decorating the
    canonical sink; ``last_run_id`` / ``max_sequence`` were captured
    from the in-flight event stream and are the only correct values to
    stamp onto the synthesised envelope — the worker's ``run_id`` does
    not match the goldfive ``run_id`` (the adapter mints its own
    uuid4 per ``goldfive.run`` call).

    Best-effort: any failure is logged and swallowed; the worker still
    falls back to :func:`ensure_run_aborted_event` to write the line
    directly to disk after sinks close.
    """
    if not sinks or tracker is None:
        return
    try:
        from goldfive.events import emit, run_aborted_event  # noqa: PLC0415
    except ModuleNotFoundError:
        return
    run_id = str(getattr(tracker, "last_run_id", "") or "")
    if not run_id:
        # We never observed a runId on any event — there is no event
        # stream to terminate. The on-disk fallback handles this case.
        return
    seq = int(getattr(tracker, "max_sequence", -1)) + 1
    if seq <= 0:
        seq = 1
    try:
        evt = run_aborted_event(run_id=run_id, sequence=seq, reason=reason)
        await emit(sinks, evt)
    except Exception as exc:  # noqa: BLE001 — emit must never fail the worker
        log.warning("worker could not emit run_aborted on budget cancel: %s", exc)


# ---------------------------------------------------------------------------
# Drive one entry — mirrors the old runner._drive_session
# ---------------------------------------------------------------------------


async def _drive_session(
    *,
    session: Any,
    entry: BoardEntry,
    events_path: Path,
    sinks: list[Any],
    config: RuntimeConfig,
) -> tuple[RunResult | None, int, bool]:
    """Drive one ``session.run`` and return (result, runtime_ms, budget_exceeded).

    Identical dispatch logic to the in-process runner's old
    ``_drive_session``: synthetic kinds bypass the adapter session,
    legacy ``run(entry, sink_path)`` stubs are detected by parameter
    name, and the rich ``run(entry, sinks, config)`` shape is the
    default. Kept here verbatim so a worker run is byte-equivalent to
    what the runner used to do inline.
    """
    started = time.monotonic()

    if entry.kind in ("synthetic_adversarial", "synthetic_clean"):
        from zicato.synthetic import (  # noqa: PLC0415
            run_adversarial_entry,
            run_clean_entry,
        )

        synth_runner = (
            run_adversarial_entry if entry.kind == "synthetic_adversarial" else run_clean_entry
        )
        result = await synth_runner(entry, sinks, config)
        runtime_ms = (
            result.runtime_ms
            if isinstance(result, RunResult) and result.runtime_ms > 0
            else int((time.monotonic() - started) * 1000)
        )
        budget_exceeded = bool(
            isinstance(result, RunResult)
            and result.aborted
            and result.abort_reason == "wall_clock_budget_exceeded"
        )
        return result, runtime_ms, budget_exceeded

    sig = inspect.signature(session.run)
    param_names = list(sig.parameters)
    legacy = len(param_names) >= 2 and param_names[1] in ("sink_path", "events_path")

    if legacy:
        await session.run(entry, events_path)
        runtime_ms = int((time.monotonic() - started) * 1000)
        return None, runtime_ms, False

    result = await session.run(entry, sinks, config)
    runtime_ms = (
        result.runtime_ms
        if isinstance(result, RunResult) and result.runtime_ms > 0
        else int((time.monotonic() - started) * 1000)
    )
    budget_exceeded = bool(
        isinstance(result, RunResult)
        and result.aborted
        and result.abort_reason == "wall_clock_budget"
    )
    return (result if isinstance(result, RunResult) else None), runtime_ms, budget_exceeded


async def _evaluate_expectation(
    entry: BoardEntry,
    run_result: RunResult | None,
    config: RuntimeConfig,
) -> Any:
    """Evaluate ``entry.expectation`` against ``run_result`` if both present."""
    if entry.expectation is None or run_result is None:
        return None
    from zicato.board.matchers import evaluate_expectation  # noqa: PLC0415

    return await evaluate_expectation(
        entry.expectation,
        run_result,
        aux_call_llm=config.effective_judge_call_llm(),
    )


# ---------------------------------------------------------------------------
# Result file
# ---------------------------------------------------------------------------


def _write_result(
    result_path: Path,
    *,
    run_result: RunResult | None,
    loss_path: Path,
    runtime_ms: int,
    aborted: bool,
    abort_reason: str,
) -> None:
    """Write the worker's result JSON the parent reads back.

    Result file shape::

        {
          "schema": "zicato.tournament_worker.result/1",
          "run_result": { ...RunResult dict... } | null,
          "loss_profile_path": "<abs path to loss.json>",
          "runtime_ms": <int>,
          "aborted": <bool>,
          "abort_reason": "<symbolic reason or empty string>"
        }

    ``run_result`` is ``null`` on the legacy stub path (no RunResult is
    produced). ``aborted`` is the worker's view of whether the run hit
    its own cooperative budget; the parent additionally treats a missing
    or non-zero-exit result file as aborted (supervisor / parent kill).
    """
    payload = {
        "schema": "zicato.tournament_worker.result/1",
        "run_result": asdict(run_result) if run_result is not None else None,
        "loss_profile_path": str(loss_path),
        "runtime_ms": int(runtime_ms),
        "aborted": bool(aborted),
        "abort_reason": str(abort_reason),
    }
    result_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = result_path.with_suffix(result_path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, sort_keys=True, indent=2)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, result_path)


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------


async def _run(args: dict[str, Any]) -> None:
    """Execute the single run described by ``args``.

    Writes the ``active_runs`` state file with the worker's own pid,
    drives the entry, reduces the loss, and writes ``loss.json`` plus the
    result file. Removes the ``active_runs`` file on a clean exit.
    """
    from zicato.runtime import state as state_mod  # noqa: PLC0415
    from zicato.telemetry import reducer as reducer_mod  # noqa: PLC0415

    workspace_root = Path(args["workspace_root"])
    epoch_id = str(args["epoch_id"])
    generation_id = str(args["generation_id"])
    snapshot_root = Path(args["snapshot_root"])
    events_path = Path(args["sink_events_path"])
    loss_path = Path(args["loss_path"])
    result_path = Path(args["result_path"])
    harmonograf_url = str(args.get("harmonograf_url", "") or "")
    harmonograf_grpc = str(args.get("harmonograf_grpc", "") or "")

    # Export the per-run scratch directory so the inner harness routes
    # its run output OUTSIDE the generation snapshot. Without this a
    # target writing next to its own code (e.g. the presentation agent's
    # ``output/``) would pollute the snapshot, and the pollution would
    # compound generation over generation. The runner supplies a fresh
    # scratch dir per run; an absent key (a legacy args file) leaves the
    # env var unset and the target falls back to its own default.
    from zicato.epoch.snapshot_scope import SCRATCH_DIR_ENV  # noqa: PLC0415

    scratch_raw = args.get("scratch_dir")
    if scratch_raw:
        scratch_dir = Path(scratch_raw)
        scratch_dir.mkdir(parents=True, exist_ok=True)
        os.environ[SCRATCH_DIR_ENV] = str(scratch_dir)

    entry = validate_board_entry(args["entry"])
    run_id = f"{generation_id}--{entry.id}"

    # Resolve each LLM role in THIS fresh interpreter — either a dotted
    # path re-import (legacy / unconfigured role) or a model spec from the
    # workspace ``models`` block, re-resolved here (reading any api_key_env
    # from the worker's OWN os.environ — secrets never crossed the boundary).
    harness_call_llm = _resolve_role_call_llm(args["harness_role"], role="harness")
    auxiliary_call_llm = _resolve_role_call_llm(args["auxiliary_role"], role="auxiliary")
    # The judge role falls back to ``None`` when unconfigured, so
    # ``RuntimeConfig.effective_judge_call_llm`` resolves to the auxiliary.
    judge_call_llm = None
    judge_role = args.get("judge_role")
    if isinstance(judge_role, dict) and (judge_role.get("dotted") or judge_role.get("models_role")):
        judge_call_llm = _resolve_role_call_llm(judge_role, role="judge")
    config = RuntimeConfig(
        instance_id=str(args.get("instance_id", "default")),
        workspace_root=workspace_root,
        harness_call_llm=harness_call_llm,
        auxiliary_call_llm=auxiliary_call_llm,
        seed=args.get("seed"),
        judge_call_llm=judge_call_llm,
    )

    weights = _weights_from_args(args)
    budget_s = float(entry.wall_clock_budget_seconds)

    # --- Write the active-runs state file with the WORKER's own pid. ---
    # This is the central change of the L3 layer: the run's worker pid
    # (not the orchestrator's) lands here, so the supervisor watchdog can
    # SIGKILL exactly this run by this pid.
    now = datetime.now(UTC)
    deadline = now + timedelta(seconds=int(budget_s))
    try:
        state_mod.write_active_run(
            workspace_root,
            state_mod.ActiveRun(
                run_id=run_id,
                pid=os.getpid(),
                started_at=now.isoformat(),
                last_progress=now.isoformat(),
                wall_clock_budget_seconds=int(budget_s),
                deadline=deadline.isoformat(),
                events_jsonl_path=str(events_path),
                entry_id=entry.id,
                generation_id=generation_id,
                epoch_id=epoch_id,
            ),
        )
    except Exception as exc:  # noqa: BLE001 — state write is best-effort
        log.warning("worker could not write active_run for %s: %s", run_id, exc)

    # --- Start the per-run heartbeat thread. ---
    # The thread bumps ``last_progress`` on the active-runs record every
    # few seconds.  Because blocking network I/O (LLM calls) releases the
    # GIL, the thread keeps beating even while the asyncio event loop is
    # parked waiting for a slow model response.  This prevents the
    # supervisor's staleness watchdog from issuing false-positive kill
    # escalations on valid long-running LLM calls.
    from zicato.runtime.heartbeat import RunHeartbeatBeater  # noqa: PLC0415

    run_hb = RunHeartbeatBeater(workspace_root, run_id)
    try:
        run_hb.start()
    except Exception as exc:  # noqa: BLE001 — heartbeat is best-effort
        log.warning("worker could not start run heartbeat for %s: %s", run_id, exc)

    sinks, tracker = _build_sinks(events_path, harmonograf_url, harmonograf_grpc)
    adapter = _build_adapter(args["adapter"])

    run_result: RunResult | None = None
    runtime_ms = 0
    budget_exceeded = False
    try:
        session = adapter.load(snapshot_root)

        async def _drive() -> tuple[RunResult | None, int, bool]:
            return await _drive_session(
                session=session,
                entry=entry,
                events_path=events_path,
                sinks=sinks,
                config=config,
            )

        # Cooperative first line of defence: the worker keeps the same
        # in-process per-entry budget the old in-process runner relied
        # on. The parent's wait_for (budget + GRACE) is the second line;
        # the supervisor's deadline kill is the third.
        try:
            run_result, runtime_ms, budget_exceeded = await asyncio.wait_for(
                _drive(), timeout=budget_s
            )
        except TimeoutError:
            budget_exceeded = True
            runtime_ms = int(budget_s * 1000)
            run_result = RunResult(
                run_id=run_id,
                entry_id=entry.id,
                final_output="",
                transcript=(),
                runtime_ms=runtime_ms,
                aborted=True,
                abort_reason=WORKER_BUDGET_ABORT_REASON,
            )
    finally:
        # Stop the heartbeat thread before closing sinks so the thread
        # does not try to bump a run record that is about to be removed.
        try:
            run_hb.stop()
        except Exception as exc:  # noqa: BLE001 — cleanup is best-effort
            log.debug("worker could not stop run heartbeat for %s: %s", run_id, exc)
        # Terminal-event invariant: when the cooperative wait_for
        # cancelled the inner goldfive task, goldfive could not reach
        # its own ``_emit_run_aborted`` path (CancelledError propagated
        # through). Emit one here so the events file always ends with a
        # lifecycle frame — without it the dashboard transcript stays
        # ``complete: False`` and the column renders a misleading "in
        # progress" cue. See zicato.telemetry.terminal_event.
        if budget_exceeded and tracker is not None and sinks:
            await _emit_worker_abort(
                sinks=sinks,
                tracker=tracker,
                reason=TERMINAL_REASON_WALL_CLOCK,
            )
        await _close_sinks(sinks)
        # Defensive fallback: if the in-process emit could not land
        # (e.g. the sink raised) the events file still lacks a terminal
        # frame on disk. Append one directly so the invariant holds.
        if budget_exceeded:
            try:
                from zicato.telemetry.terminal_event import (  # noqa: PLC0415
                    ensure_run_aborted_event,
                )

                ensure_run_aborted_event(
                    events_path,
                    reason=TERMINAL_REASON_WALL_CLOCK,
                )
            except Exception as exc:  # noqa: BLE001 — never fail the run on this
                log.debug("worker terminal-event fallback failed: %s", exc)

    expectation_result = await _evaluate_expectation(entry, run_result, config)

    # A run that did not complete successfully must be scored worst-case,
    # never zero. The worker reaches this point only on a clean worker
    # exit; the run ITSELF may still have failed. ``run_result.aborted``
    # is the adapter's verdict — set for a harness exception (a crash),
    # an emulator answer-leak abort, an unavailable scripted / emulated
    # driver, or an unsupported entry kind. ``budget_exceeded`` already
    # covers the wall-clock case and is passed separately. A ``None``
    # ``run_result`` is the legacy stub path — a genuinely completed run
    # with no RunResult — so it is NOT treated as not-completed.
    run_not_completed = bool(run_result is not None and run_result.aborted)

    loss: LossProfile = reducer_mod.reduce_loss(
        events_path,
        entry,
        generation_id,
        epoch_id,
        expectation_result,
        runtime_ms,
        budget_exceeded,
        weights,
        run_not_completed=run_not_completed,
    )
    reducer_mod.write_loss_profile(loss, loss_path)

    _write_result(
        result_path,
        run_result=run_result,
        loss_path=loss_path,
        runtime_ms=runtime_ms,
        aborted=budget_exceeded,
        abort_reason=(WORKER_BUDGET_ABORT_REASON if budget_exceeded else ""),
    )

    # Clean exit — remove our own active-runs file. If the worker had
    # instead been SIGKILLed, this never runs and the parent removes it.
    try:
        state_mod.remove_active_run(workspace_root, run_id)
    except Exception as exc:  # noqa: BLE001 — cleanup is best-effort
        log.debug("worker could not remove active_run for %s: %s", run_id, exc)


def _weights_from_args(args: dict[str, Any]) -> ScoringWeights:
    """Reconstruct :class:`ScoringWeights` from the serialised args.

    The args file carries the scalar-and-mapping subset of
    :class:`ScoringWeights` under the ``weights`` key. A missing key
    falls back to the dataclass default — so a caller that does not care
    about scoring weights (a stub-adapter test) can omit the block
    entirely and still get a usable run.

    The symmetric writer is :func:`zicato.tournament.runner._weights_spec`;
    the two must stay field-for-field in lock-step. A field carried by the
    writer but not read here (or vice versa) is silently reset to its default
    in the subprocess — the defect class this reader's ``pass_rate_monotonicity_scope``
    handling guards against (issue #17).
    """
    raw = args.get("weights") or {}
    if not isinstance(raw, dict):
        return ScoringWeights()
    defaults = ScoringWeights()
    # The monotonicity scope is a closed Literal — coerce an unknown token
    # back to the default so a malformed args file can never desync the
    # worker's gate-view from the parent's. Valid tokens pass through.
    raw_scope = raw.get("pass_rate_monotonicity_scope", defaults.pass_rate_monotonicity_scope)
    scope = (
        raw_scope
        if raw_scope in ("per_entry", "aggregate")
        else defaults.pass_rate_monotonicity_scope
    )
    return ScoringWeights(
        drift_weight=float(raw.get("drift_weight", defaults.drift_weight)),
        pass_weight=float(raw.get("pass_weight", defaults.pass_weight)),
        severity_weights=dict(raw.get("severity_weights", defaults.severity_weights)),
        per_kind_weights=dict(raw.get("per_kind_weights", defaults.per_kind_weights)),
        per_judge_weights=dict(raw.get("per_judge_weights", defaults.per_judge_weights)),
        default_judge_weight=float(raw.get("default_judge_weight", defaults.default_judge_weight)),
        plan_revision_weight=float(raw.get("plan_revision_weight", defaults.plan_revision_weight)),
        runtime_weight=float(raw.get("runtime_weight", defaults.runtime_weight)),
        promote_margin=float(raw.get("promote_margin", defaults.promote_margin)),
        pass_rate_monotonicity=bool(
            raw.get("pass_rate_monotonicity", defaults.pass_rate_monotonicity)
        ),
        pass_rate_monotonicity_scope=scope,
        regression_gate_enabled=bool(
            raw.get("regression_gate_enabled", defaults.regression_gate_enabled)
        ),
        regression_test_command=tuple(
            raw.get("regression_test_command", defaults.regression_test_command)
        ),
        regression_timeout_s=int(raw.get("regression_timeout_s", defaults.regression_timeout_s)),
        namespace_weights=dict(raw.get("namespace_weights", defaults.namespace_weights)),
        namespace_monotonicity=dict(
            raw.get("namespace_monotonicity", defaults.namespace_monotonicity)
        ),
        # Declarative scoring transforms (issue #19 phase 2). Symmetric with
        # the writer in ``runner._weights_spec``: ``drift_kind_aggregation``
        # drives Seam 1 which runs HERE in the worker, so it must survive the
        # boundary or the worker would score drift with neutral defaults while
        # the orchestrator shows transformed (the per_judge_weights desync
        # class). An absent key falls back to the neutral default; ``None``
        # for ``pass_transform`` is preserved (it means "no transform").
        # ``ScoringWeights.__post_init__`` re-validates the reconstructed
        # specs, so a corrupt args file fails fast here too.
        pass_transform=(
            raw["pass_transform"]
            if isinstance(raw.get("pass_transform"), dict)
            else defaults.pass_transform
        ),
        drift_kind_aggregation=dict(
            raw.get("drift_kind_aggregation", defaults.drift_kind_aggregation)
        ),
        # Dotted-spec scoring plugins (issue #19 phase 3). Symmetric with the
        # writer in ``runner._weights_spec``. ``drift_reducer`` drives Seam 1
        # which runs HERE in the worker, so it MUST survive the boundary or the
        # worker would score drift with no plugin while the orchestrator believed
        # otherwise (the per_judge_weights desync class). A non-string / absent
        # value reads back as the neutral (no-plugin) default.
        drift_reducer=str(raw.get("drift_reducer", defaults.drift_reducer) or ""),
        scalar_fn=str(raw.get("scalar_fn", defaults.scalar_fn) or ""),
    )


def main(argv: list[str] | None = None) -> int:
    """Module entry point: ``python -m zicato._tournament_worker <args-file>``.

    Returns a process exit code: ``0`` on a completed run (including a
    run the worker aborted via its own cooperative budget — that is a
    *successful* worker outcome, the loss profile and result file were
    still written), non-zero on a setup or execution failure the worker
    could not turn into a loss profile.
    """
    logging.basicConfig(level=logging.WARNING)
    args_argv = sys.argv[1:] if argv is None else argv
    if len(args_argv) != 1:
        print(
            "usage: python -m zicato._tournament_worker <args-file.json>",
            file=sys.stderr,
        )
        return 2

    args_path = Path(args_argv[0])
    try:
        args = _load_args(args_path)
    except Exception as exc:  # noqa: BLE001 — surface as a clean non-zero exit
        print(f"zicato._tournament_worker: bad args file: {exc}", file=sys.stderr)
        return 2

    try:
        asyncio.run(_run(args))
    except Exception as exc:  # noqa: BLE001 — non-zero exit; parent synthesises abort
        log.exception("zicato._tournament_worker: run failed")
        print(f"zicato._tournament_worker: run failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover — exercised via subprocess in tests
    sys.exit(main())
