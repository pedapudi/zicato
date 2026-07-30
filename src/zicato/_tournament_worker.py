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
    BUDGET_ABORT_CAUSE,
    BoardEntry,
    LossProfile,
    RunResult,
    RuntimeConfig,
    ScoringWeights,
    validate_board_entry,
)
from zicato.import_path import import_dotted_path
from zicato.util import best_effort

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

    The model-spec form resolves through
    :func:`zicato.models_config.lazy_text_call_llm`, so the ADK import graph
    (measured at 0.80 s / 88 MB per worker — RUNTIME.md §5.5.8) is paid on the
    role's FIRST CALL rather than at worker startup. A unit that never
    exercises a role — an entry with no LLM judge, a run that never reaches
    the auxiliary side — therefore never pays for it, and a unit that does
    exercise it pays exactly the same cost, just later. The spec *shape* is
    still validated eagerly, so a malformed ``models`` block fails fast here.
    """
    if not isinstance(spec, dict):
        raise ValueError(f"{role} role spec must be a JSON object, got {type(spec).__name__}")
    dotted = spec.get("dotted")
    if dotted:
        return _import_callable(str(dotted))
    raw_role = spec.get("models_role")
    if isinstance(raw_role, dict):
        from zicato.models_config import lazy_text_call_llm, role_spec_from_dict  # noqa: PLC0415

        return lazy_text_call_llm(role_spec_from_dict(raw_role), role=role)
    raise ValueError(f"{role} role spec has neither 'dotted' nor 'models_role': {spec!r}")


def _resolve_inner_model_from_role(spec: Any) -> Any:
    """Build the inner ADK agent model from the harness role worker spec.

    Mirrors :mod:`zicato.runtime_factory`'s inner-model construction inside the
    fresh worker interpreter: when the harness role is a ``models_role`` *model
    spec* (model + endpoint/api_key_env), build the ADK model object (a
    ``LiteLlm``) so the adapter rebinds the target's agents to it with native
    tool/function calling. Returns ``None`` for a dotted call_llm role or an
    endpoint-less spec that yields a bare string — the adapter then falls back
    to its guarded shim rebind, exactly as before. ``api_key_env`` is read from
    the worker's OWN ``os.environ`` (secrets never crossed the boundary).
    """
    if not isinstance(spec, dict):
        return None
    raw_role = spec.get("models_role")
    if not isinstance(raw_role, dict):
        return None
    from zicato.models_config import build_adk_model, role_spec_from_dict  # noqa: PLC0415

    role_spec = role_spec_from_dict(raw_role)
    if not role_spec.model:
        return None
    try:
        built = build_adk_model(role_spec, role="harness")
    except ValueError:
        return None
    return built if not isinstance(built, str) else None


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
          "harmonograf_url": "",
          "persist_run_results": true,   # optional; ABSENT => true
          "persist_judge_io": true       # optional; ABSENT => true
        }

    ``persist_run_results`` / ``persist_judge_io`` are the board-reflection
    capture knobs (runtime-only, never contract-hashed): result.json beside
    loss.json and the judge_io.jsonl sidecar. Absent keys (legacy args
    files) default to True — always-on with an opt-out.
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
        with best_effort(
            "worker harmonograf sink attach",
            on_error=lambda exc: log.warning("worker could not attach harmonograf sink: %s", exc),
        ):
            from zicato.telemetry.sink import _make_harmonograf_sink  # noqa: PLC0415

            # ``harmonograf_grpc`` carries the native gRPC dial target the
            # runner resolved (the auto-launched server's grpc_port, NOT
            # the browser-facing gRPC-Web port in ``harmonograf_url``). An
            # empty target falls back to deriving from the web URL — the
            # external-harmonograf single-port path.
            extra = _make_harmonograf_sink(harmonograf_url, grpc_target=harmonograf_grpc or None)
            if extra is not None:
                sinks.append(extra)
    return sinks, tracker


async def _close_sinks(sinks: list[Any]) -> None:
    """Best-effort close of every sink so the JSONL is flushed to disk."""
    for s in sinks:
        with best_effort(
            "worker sink close",
            on_error=lambda exc: log.debug("worker sink close failed: %s", exc),
        ):
            await s.close()


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
    with best_effort(
        "worker run_aborted emit",
        on_error=lambda exc: log.warning(
            "worker could not emit run_aborted on budget cancel: %s", exc
        ),
    ):
        evt = run_aborted_event(run_id=run_id, sequence=seq, reason=reason)
        await emit(sinks, evt)


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

    # Re-pin the orchestrator's process-pinned config overrides (CLI
    # flags such as --harness-call-timeout-ms / --aux-call-timeout) in
    # THIS fresh interpreter, before anything calls load_config(). The
    # pins travelled in the args file — the flag-to-config bridge across
    # the worker subprocess boundary; no environment variable involved.
    # An absent / empty key (a legacy args file, or no flags pinned)
    # leaves the worker on its own defaults.
    config_pins = args.get("config_pins")
    if config_pins:
        from zicato.config import pin_overrides  # noqa: PLC0415

        pin_overrides(config_pins)

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
    # Inner ADK agent model: when the harness role is a model spec, the agents
    # run on the configured endpoint (function-calling) instead of the shim.
    inner_model = _resolve_inner_model_from_role(args["harness_role"])
    auxiliary_call_llm = _resolve_role_call_llm(args["auxiliary_role"], role="auxiliary")
    # The judge role falls back to ``None`` when unconfigured, so
    # ``RuntimeConfig.effective_judge_call_llm`` resolves to the auxiliary.
    judge_call_llm = None
    judge_role = args.get("judge_role")
    if isinstance(judge_role, dict) and (judge_role.get("dotted") or judge_role.get("models_role")):
        judge_call_llm = _resolve_role_call_llm(judge_role, role="judge")

    # Capture knobs (board reflection's capture fix). Runtime-only,
    # additive, NEVER contract-hashed. An ABSENT key — a legacy args file
    # — defaults to True: the capture is always-on with an opt-out, so
    # historical callers gain the artifacts without a flag. Both writes
    # are best-effort: a capture failure never re-scores or aborts a run.
    # With both knobs OFF the worker's behavior (files written, loss
    # bytes, exit code) is byte-identical to before the knobs existed.
    persist_run_results = bool(args.get("persist_run_results", True))
    persist_judge_io = bool(args.get("persist_judge_io", True))
    judge_io_sink: Any = None
    if persist_judge_io:
        from zicato.judge_runtime.io_capture import (  # noqa: PLC0415
            JudgeIOFileSink,
            judge_io_path_for_loss,
        )

        # One sink per run, appending beside the run's loss slot
        # (judge_io.jsonl / judge_io.r{n}.jsonl) — the adapter reads it
        # off the config (the token_ledger live-object precedent) and
        # threads it into every custom inline judge it assembles.
        judge_io_sink = JudgeIOFileSink(judge_io_path_for_loss(loss_path))

    config = RuntimeConfig(
        instance_id=str(args.get("instance_id", "default")),
        workspace_root=workspace_root,
        harness_call_llm=harness_call_llm,
        auxiliary_call_llm=auxiliary_call_llm,
        seed=args.get("seed"),
        judge_call_llm=judge_call_llm,
        inner_model=inner_model,
        persist_run_results=persist_run_results,
        persist_judge_io=persist_judge_io,
        judge_io_sink=judge_io_sink,
    )

    weights = _weights_from_args(args)
    budget_s = float(entry.wall_clock_budget_seconds)

    # --- Write the active-runs state file with the WORKER's own pid. ---
    # This is the central change of the L3 layer: the run's worker pid
    # (not the orchestrator's) lands here, so the supervisor watchdog can
    # SIGKILL exactly this run by this pid.
    now = datetime.now(UTC)
    deadline = now + timedelta(seconds=int(budget_s))
    with best_effort(
        "worker active_run write",
        on_error=lambda exc: log.warning(
            "worker could not write active_run for %s: %s", run_id, exc
        ),
    ):
        from zicato.runtime.lock import pid_start_time as _pid_start_time

        # Record the worker's OWN process-group id so the supervisor can
        # group-kill the worker plus any grandchildren the inner harness
        # spawned (shells, helper tools), not just the worker pid. The
        # runner spawns us with ``start_new_session=True`` so we lead our
        # own group; ``os.getpgid`` is unavailable on a few platforms, so
        # this is best-effort and leaves ``pgid=None`` (single-pid kill) on
        # failure. Also record the ephemeral snapshot directory the runner
        # mounted us on, so the supervisor can GC the orphaned ``ztw-snap-*``
        # tree if the orchestrator dies mid-run.
        try:
            own_pgid: int | None = os.getpgid(os.getpid())
        except OSError:
            own_pgid = None
        state_mod.write_active_run(
            workspace_root,
            state_mod.ActiveRun(
                run_id=run_id,
                pid=os.getpid(),
                pid_start_time=_pid_start_time(os.getpid()),
                pgid=own_pgid,
                snapshot_path=str(snapshot_root),
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

    # --- Start the per-run heartbeat thread. ---
    # The thread bumps ``last_progress`` on the active-runs record every
    # few seconds.  Because blocking network I/O (LLM calls) releases the
    # GIL, the thread keeps beating even while the asyncio event loop is
    # parked waiting for a slow model response.  This prevents the
    # supervisor's staleness watchdog from issuing false-positive kill
    # escalations on valid long-running LLM calls.
    from zicato.runtime.heartbeat import RunHeartbeatBeater  # noqa: PLC0415

    run_hb = RunHeartbeatBeater(workspace_root, run_id)
    with best_effort(
        "worker run heartbeat start",
        on_error=lambda exc: log.warning(
            "worker could not start run heartbeat for %s: %s", run_id, exc
        ),
    ):
        run_hb.start()

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
        with best_effort(
            "worker run heartbeat stop",
            on_error=lambda exc: log.debug(
                "worker could not stop run heartbeat for %s: %s", run_id, exc
            ),
        ):
            run_hb.stop()
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
            with best_effort(
                "worker terminal-event fallback",
                on_error=lambda exc: log.debug("worker terminal-event fallback failed: %s", exc),
            ):
                from zicato.telemetry.terminal_event import (  # noqa: PLC0415
                    ensure_run_aborted_event,
                )

                ensure_run_aborted_event(
                    events_path,
                    reason=TERMINAL_REASON_WALL_CLOCK,
                )

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
    # Stamp the abort provenance so loop-health + the cache layer can tell a
    # genuine wall-clock-budget exhaustion (the worker's own cooperative
    # ``asyncio.wait_for`` fired) from an infra abort (a parent/supervisor
    # kill or crash, which the parent stamps). A cooperative budget abort IS
    # cache-eligible (re-running re-hits the same budget); the parent's
    # ``is_infra_abort_cause`` reads this field to decide. A genuinely
    # completed run leaves the field unset (``None``).
    if budget_exceeded:
        from dataclasses import replace as _replace  # noqa: PLC0415

        loss = _replace(loss, abort_cause=BUDGET_ABORT_CAUSE)
    reducer_mod.write_loss_profile(loss, loss_path)

    # Persist the run's user-facing RunResult as result.json beside
    # loss.json (board reflection's capture fix) — replicate-slotted like
    # the loss, atomic (tmp+fsync+rename), and STRICTLY best-effort: a
    # capture failure is logged and the run proceeds unchanged (loss.json
    # is already on disk; the result file below and the exit code are
    # untouched). The budget-abort path's synthesized RunResult flows
    # through here too, so an aborted run's (empty) capture is honest.
    # ``run_result is None`` is the legacy-stub path — no RunResult was
    # ever produced, so there is nothing to persist.
    if persist_run_results and run_result is not None:
        with best_effort(
            "worker run-result capture",
            on_error=lambda exc: log.warning(
                "worker could not persist result.json for %s: %s", run_id, exc
            ),
        ):
            from zicato.storage import atomic_write_json  # noqa: PLC0415
            from zicato.tournament.unit_cache import (  # noqa: PLC0415
                run_result_to_payload,
                unit_result_path,
            )

            atomic_write_json(unit_result_path(loss_path), run_result_to_payload(run_result))

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
    with best_effort(
        "worker active_run remove",
        on_error=lambda exc: log.debug(
            "worker could not remove active_run for %s: %s", run_id, exc
        ),
    ):
        state_mod.remove_active_run(workspace_root, run_id)


def _weights_from_args(args: dict[str, Any]) -> ScoringWeights:
    """Reconstruct :class:`ScoringWeights` from the serialised args.

    The args file carries the serialised :class:`ScoringWeights` under the
    ``weights`` key. A missing key falls back to the dataclass default — so a
    caller that does not care about scoring weights (a stub-adapter test) can
    omit the block entirely and still get a usable run.

    Thin delegator to :meth:`ScoringWeights.from_json` — the inverse of the
    SINGLE field-enumerating serde the runner serialises with
    (:func:`zicato.tournament.runner._weights_spec` → :meth:`ScoringWeights.to_json`).
    Because both sides now share ONE ``dataclasses.fields()``-driven serde, a
    field can no longer be carried by the writer but dropped here (or vice
    versa) — the silent worker-scores-under-defaults desync class
    (``per_judge_weights`` / ``pass_rate_monotonicity_scope`` /
    ``drift_kind_aggregation``) that two hand-aligned field lists kept
    re-introducing. ``from_json`` coerces every field to its declared type and
    re-runs ``ScoringWeights.__post_init__``, so a malformed transform / scope
    token in the args file fails fast or coerces, exactly as before.
    """
    return ScoringWeights.from_json(args.get("weights"))


def _install_worker_log_stream_from_args(args: dict[str, Any]) -> None:
    """Install the worker-side operator-log stream from the args file.

    Reads the invocation stream path the runner threaded through
    (``log_stream_path``) and the run coordinate (epoch / generation /
    entry) to bind context, then appends this worker's records to that one
    stream. Fully best-effort: any failure is swallowed so logging setup
    can never fail a run.
    """
    try:
        path = args.get("log_stream_path")
        if not path:
            return
        from zicato.logging_stream import install_worker_log_stream  # noqa: PLC0415

        epoch_id = str(args.get("epoch_id") or "") or None
        generation_id = str(args.get("generation_id") or "") or None
        entry = args.get("entry")
        entry_id = str(entry.get("id")) if isinstance(entry, dict) and entry.get("id") else None
        run_id = f"{generation_id}--{entry_id}" if generation_id and entry_id else None
        install_worker_log_stream(
            path,
            epoch_id=epoch_id,
            generation_id=generation_id,
            run_id=run_id,
        )
    except Exception:  # noqa: BLE001 — logging setup never fails a run
        pass


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

    # Structured operator-log stream (LOGGING.md §2): append this worker's
    # records to the SAME per-invocation file the orchestrator installed,
    # with the full (epoch, generation, run) context bound so every worker
    # record is attributed. Best-effort — a logging-setup failure must never
    # fail the run; ``None`` path (an ad-hoc drive) keeps stderr only.
    _install_worker_log_stream_from_args(args)

    try:
        asyncio.run(_run(args))
    except Exception as exc:  # noqa: BLE001 — non-zero exit; parent synthesises abort
        log.exception("zicato._tournament_worker: run failed")
        print(f"zicato._tournament_worker: run failed: {exc}", file=sys.stderr)
        return 1

    # A model-spec role whose resolution was DEFERRED (§5.5.8) can fail at its
    # first call, and if that first call was a judge's the exception was
    # swallowed — every judge boundary swallows by hard contract, so the run
    # would otherwise complete with the judge silently reporting "no signal"
    # and a scalar better than the truth. A role that cannot be resolved is a
    # deterministic CONFIG fault, so it exits non-zero exactly as the eager
    # path did: the parent records an infra abort instead of banking a score.
    from zicato.models_config import deferred_role_failures  # noqa: PLC0415

    failures = deferred_role_failures()
    if failures:
        detail = "; ".join(f"models.{role}: {msg}" for role, msg in sorted(failures.items()))
        log.error("zicato._tournament_worker: role resolution failed (%s)", detail)
        print(
            f"zicato._tournament_worker: role resolution failed: {detail}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover — exercised via subprocess in tests
    sys.exit(main())
