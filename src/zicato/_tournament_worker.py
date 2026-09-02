"""Subprocess worker that executes ONE tournament run in its own OS process.

This module is the subprocess-worker robustness layer. A wedged board-entry
run holds the interpreter, so running it inside the orchestrator process
would make killing the whole ``evolve`` invocation the only way to stop it.
Isolating each run as its own subprocess is what lets a per-run wall-clock
budget be hard-enforced:

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
   OWN pid, which is what lets the supervisor kill this one run rather
   than the orchestrator;
2. loads the harness from the ``snapshot_root`` it was handed — a per-run
   ephemeral working copy of the generation's code snapshot, NOT the
   canonical ``generations/vN/snapshot/`` (the parent makes the copy so
   any runtime write the agent does near its own code cannot pollute the
   canonical snapshot) — and drives the one entry under goldfive;
3. captures every regular file produced under the run scratch directory,
   writes a deterministic artifact manifest, and exposes that inventory to
   expectation evaluators;
4. computes the :class:`~zicato.core.LossProfile` via
   :func:`zicato.telemetry.reducer.reduce_loss` and writes ``loss.json``;
5. writes a result file (the :class:`~zicato.core.RunResult` plus the
   loss-profile path, runtime, and aborted flag) as JSON;
6. on a clean exit removes its ``active_runs`` file.

Module-caching note
-------------------
Because every run is a fresh subprocess, the Python-module-caching problem
a single-process runner has — loading two generations' source into one
interpreter and getting the wrong one back from ``sys.modules`` — does not
arise here. Each worker imports the
one generation snapshot it was handed and then exits; there is never a
second generation's source in the same interpreter to collide with.

The worker is killable: if SIGTERM/SIGKILL'd mid-run it
just dies, leaving (at worst) a stale ``active_runs`` file and no result
file. The parent treats "process gone + no result file" as a normal
aborted-run outcome — that is the supervisor-kill path.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import sys
import time
from collections.abc import Mapping
from dataclasses import asdict, replace
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
from zicato.judge_runtime.error_register import judge_error_snapshot
from zicato.util import best_effort, now_iso

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

    * ``{"dotted": "module:qualname"}`` — re-import the callable (a role
      configured by dotted path, or one left unconfigured); or
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
    to its guarded shim rebind. ``api_key_env`` is read from
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


def _goldfive_config_for_adapter(
    weights: ScoringWeights, adapter_spec: dict[str, Any]
) -> Mapping[str, Any] | None:
    """Expose Goldfive settings only to an adapter that declares the capability."""
    from zicato.tournament.worker_transport import adapter_uses_integration  # noqa: PLC0415

    return weights.goldfive if adapter_uses_integration(adapter_spec, "goldfive") else None


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
            "mutable_trees": ["<abs path>", ...],
            "integrations": ["goldfive"]
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
    loss.json and the judge_io.jsonl sidecar. An args file that omits either
    key defaults it to True — always-on with an opt-out.
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


def build_adapter(spec: dict[str, Any]) -> Any:
    """Reconstruct a harness adapter from its serialised spec dict.

    Thin alias for :func:`zicato.adapter_factory.make_adapter_from_spec`,
    kept because the worker's own call site and every test that patches
    adapter reconstruction refer to it by this name on this module.
    """
    from zicato.adapter_factory import make_adapter_from_spec  # noqa: PLC0415

    return make_adapter_from_spec(spec)


#: Schema token of the per-generation harness-load provenance file.
HARNESS_LOAD_SCHEMA = "zicato.harness_load/1"


def _record_harness_load(
    workspace_root: Path,
    *,
    epoch_id: str,
    generation_id: str,
    session: Any,
    snapshot_root: Path,
    tree_status: dict[str, str] | None = None,
) -> None:
    """Record WHAT this generation actually loaded from its snapshot.

    The worker is the only process that ever imports the entrypoint or the
    mutable trees, so it is the only one that knows where they resolved. It
    writes that to the generation's ``harness_load.json``; the orchestrator
    reads it back after the duel and emits the round log's ``harness_loaded``
    event (the round log has a single writer, the orchestrator, so the worker
    must not append to it directly).

    Two facts land here, both keyed to the mutated-tree invariant. First,
    ``entrypoint_file`` — the resolved entrypoint module, and the recorded
    value is SNAPSHOT-RELATIVE (``agent/agent.py``) rather than the
    absolute ``__file__``. ``snapshot_root`` here is the per-run EPHEMERAL
    checkout (``ztw-snap-*`` under system temp, deleted in ``_run_single``'s
    ``finally``), so the absolute path names a directory that is already gone
    by the time anyone reads the round log, differs for every unit of the same
    generation, and folds the operator's machine layout into a durable record.
    The relative path is the part that carries the provenance — which module
    inside the snapshot ran — and it is comparable across generations, runs and
    checkouts. The absolute path still goes to the log line below for live
    debugging. Falls back to the raw string if it is somehow not under the
    snapshot, and is EMPTY for the dependency shape (the entrypoint lives
    outside every mutable tree by design, so no snapshot-relative file names
    it).

    Second, ``trees_verified`` / ``trees_never_imported`` — the post-run
    per-tree verdicts from :func:`zicato.adapters.adk.tree_import_status`,
    passed as ``tree_status`` by :func:`_verify_trees_after_run`. These
    ACCUMULATE across the generation's units rather than overwriting: a tree ANY
    unit imported from the snapshot is verified for the generation, and
    ``trees_never_imported`` is what is left over — the trees no unit ever
    touched. That is the only observable form of a shadowed snapshot — an
    installed entrypoint that never imports the mutated tree (issue #110). One
    unit's
    read-modify-write can lose a concurrent unit's verification, which can only
    ever ADD a warning-severity never-imported entry, never suppress a failure:
    a tree imported from outside the snapshot fails its own unit at load time
    and again in :func:`_verify_trees_after_run`.

    The entrypoint half stays idempotent — every worker for a generation
    resolves the same file. Best-effort in both directions: an adapter that
    reports neither an ``entrypoint_file`` nor a tree status (any non-ADK kind)
    writes nothing, and a write failure is logged at debug and never fails a
    run.
    """
    absolute_file = str(getattr(session, "entrypoint_file", "") or "")
    if not absolute_file and not tree_status:
        return
    entrypoint_file = ""
    if absolute_file:
        log.info(
            "worker: generation %s harness entrypoint resolved to %s",
            generation_id,
            absolute_file,
        )
        resolved = Path(absolute_file)
        root = Path(snapshot_root).resolve()
        entrypoint_file = (
            str(resolved.relative_to(root)) if resolved.is_relative_to(root) else str(resolved)
        )
    with best_effort(
        "worker harness-load record",
        on_error=lambda exc: log.debug(
            "worker could not record harness load for %s: %s", generation_id, exc
        ),
    ):
        from zicato.adapters.adk import (  # noqa: PLC0415
            TREE_IMPORT_NEVER_IMPORTED,
            TREE_IMPORT_VERIFIED,
        )
        from zicato.core.workspace import harness_load_path  # noqa: PLC0415
        from zicato.storage import atomic_write_json, read_json  # noqa: PLC0415

        path = harness_load_path(workspace_root, epoch_id, generation_id)
        previous = read_json(path) or {}
        verified = set(previous.get("trees_verified") or [])
        never = set(previous.get("trees_never_imported") or [])
        for basename, verdict in (tree_status or {}).items():
            if verdict == TREE_IMPORT_VERIFIED:
                verified.add(basename)
            elif verdict == TREE_IMPORT_NEVER_IMPORTED:
                never.add(basename)
        atomic_write_json(
            path,
            {
                "schema": HARNESS_LOAD_SCHEMA,
                "generation_id": generation_id,
                "entrypoint_file": entrypoint_file or str(previous.get("entrypoint_file") or ""),
                "trees_verified": sorted(verified),
                "trees_never_imported": sorted(never - verified),
            },
        )


def _verify_trees_after_run(
    workspace_root: Path,
    *,
    epoch_id: str,
    generation_id: str,
    session: Any,
    snapshot_root: Path,
) -> None:
    """Post-run: verify every mutable tree ran from THIS generation's snapshot.

    The truth layer of the mutated-tree invariant (see
    :mod:`zicato.adapters.adk`): once the unit's run has finished,
    ``sys.modules`` records what the run really imported, which is the only
    place the question "were this generation's mutations under test?" can be
    answered. Pure observation — it imports nothing and executes no target
    code — so it is safe on every exit path, including an aborted run (which
    simply reports whatever it had imported by then).

    Two outcomes, asymmetric:

    * a tree imported from OUTSIDE the snapshot FAILS the run (raise →
      non-zero worker exit → the parent synthesises an infra abort). Load time
      should already have refused it; reaching here means something defeated
      that check, and a scored unit is exactly what must not happen.
    * a tree that was NEVER imported is recorded rather than raised — a single board
      entry may legitimately not exercise every tree. It becomes a
      warning-severity loop-health finding only when NO unit of the generation
      ever imported that tree.

    The record is written BEFORE the raise so the evidence survives the failure.
    Silently inert for an adapter kind that reports no tree status.
    """
    reader = getattr(session, "tree_import_status", None)
    status: dict[str, str] = reader() or {} if callable(reader) else {}
    if not status:
        return
    _record_harness_load(
        workspace_root,
        epoch_id=epoch_id,
        generation_id=generation_id,
        session=session,
        snapshot_root=snapshot_root,
        tree_status=status,
    )
    from zicato.adapters.adk import TREE_IMPORT_OUTSIDE_ROOT  # noqa: PLC0415

    outside = sorted(b for b, verdict in status.items() if verdict == TREE_IMPORT_OUTSIDE_ROOT)
    if outside:
        raise RuntimeError(
            f"worker: mutable tree(s) {outside!r} were imported from OUTSIDE the "
            f"generation snapshot {str(snapshot_root)!r} — this generation's "
            f"mutations to them were NOT under test, so the unit must not score "
            f"(issue #110). The adapter's load-time assert should have refused "
            f"this; reaching it post-run means the import resolved elsewhere "
            f"after the load."
        )


# ---------------------------------------------------------------------------
# Sink wiring
# ---------------------------------------------------------------------------


def _build_sinks(
    events_path: Path,
    harmonograf_url: str,
    harmonograf_grpc: str = "",
    harmonograf_metadata: dict[str, str] | None = None,
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

    from zicato.telemetry.sink import archive_prior_events  # noqa: PLC0415
    from zicato.telemetry.terminal_event import SequenceTrackingSink  # noqa: PLC0415

    events_path.parent.mkdir(parents=True, exist_ok=True)
    # A re-measured unit (the champion, re-run every round under
    # ``--mode full``) would otherwise have its prior raw telemetry
    # truncated by this ``mode="write"`` sink; retain one predecessor as
    # ``events.prev.jsonl`` first (issue #122).
    archive_prior_events(events_path)
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
            extra = _make_harmonograf_sink(
                harmonograf_url,
                grpc_target=harmonograf_grpc or None,
                metadata=harmonograf_metadata,
            )
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
# Drive one entry
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
    a bare ``run(entry, sink_path)`` stub is detected by parameter
    name, and the rich ``run(entry, sinks, config)`` shape is the
    default. This is the one implementation of that dispatch, so a worker
    run and an in-process run cannot diverge in how they call a target.
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

    ``run_result`` is ``null`` on the bare-stub path, which produces no
    RunResult. ``aborted`` is the worker's view of whether the run hit
    its own cooperative budget; the parent additionally treats a missing
    or non-zero-exit result file as aborted (supervisor / parent kill).
    """
    serialized_result = asdict(run_result) if run_result is not None else None
    artifacts = run_result.artifacts if run_result is not None else None
    if serialized_result is not None and artifacts is not None:
        serialized_result["artifacts"]["root"] = str(artifacts.root)
        serialized_result["artifacts"]["manifest_path"] = str(artifacts.manifest_path)
    payload = {
        "schema": "zicato.tournament_worker.result/1",
        "run_result": serialized_result,
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
    # flags such as --aux-call-timeout) in
    # THIS fresh interpreter, before anything calls load_config(). The
    # pins travelled in the args file — the flag-to-config bridge across
    # the worker subprocess boundary; no environment variable involved.
    # An absent or empty key — an args file that pinned no flags — leaves
    # the worker on its own defaults.
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
    harmonograf_metadata = {
        str(key): str(value) for key, value in (args.get("harmonograf_metadata") or {}).items()
    }

    # Export the per-run scratch directory so the inner harness routes
    # its run output OUTSIDE the generation snapshot. Without this a
    # target writing next to its own code (e.g. the presentation agent's
    # ``output/``) would pollute the snapshot, and the pollution would
    # compound generation over generation. The runner supplies a fresh
    # scratch dir per run; an args file that omits the key leaves the
    # env var unset and the target falls back to its own default.
    from zicato.epoch.snapshot_scope import SCRATCH_DIR_ENV  # noqa: PLC0415

    scratch_dir: Path | None = None
    scratch_raw = args.get("scratch_dir")
    if scratch_raw:
        scratch_dir = Path(scratch_raw)
        scratch_dir.mkdir(parents=True, exist_ok=True)
        os.environ[SCRATCH_DIR_ENV] = str(scratch_dir)

    entry = validate_board_entry(args["entry"])
    # The run id arrives from the parent, which minted it via
    # zicato.core.workspace.run_id_for_unit and already stamped the
    # active_runs record the supervisor polices. Re-deriving it here from
    # this process's own view of the entry would give the id two producers,
    # and any skew between the two views silently reproduces issue #250.
    run_id = str(args["run_id"])
    weights = _weights_from_args(args)
    budget_s = float(entry.wall_clock_budget_seconds)

    # --- Write the active-runs state file with the WORKER's own pid. ---
    # This is what the subprocess-worker layer buys: the pid recorded here
    # is the run's own worker, rather than the orchestrator's, so the
    # supervisor watchdog can SIGKILL this one run by this pid.
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
        # spawned (shells, helper tools) rather than just the worker pid. The
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

    # Resolve each LLM role in THIS fresh interpreter — either a dotted
    # path re-import (a dotted or unconfigured role) or a model spec from the
    # workspace ``models`` block, re-resolved here (reading any api_key_env
    # from the worker's OWN os.environ — secrets never crossed the boundary).
    #
    # Deliberately placed AFTER the active-runs write + heartbeat start
    # above rather than before. An endpoint-shaped harness role (spec.model +
    # endpoint/api_key_env — the live-validation shape) forces
    # ``_resolve_inner_model_from_role`` to import the whole ``google.adk``
    # graph right here, a measured ~1 s / 80 MB / ~1500 modules (RUNTIME.md
    # §5.5.8), and ``ADKHarnessAdapter.load()`` a few lines below imports
    # the SAME graph unconditionally for every ADK-adapter run regardless of
    # ``inner_model`` — so the import cost is unavoidable for this shape and
    # deferring it past ``.load()`` saves nothing on top (measured: building
    # the ``LiteLlm`` object after ``google.adk`` is already resident costs
    # ~2 ms, vs. ~1 s to import ``google.adk`` itself; see §5.5.8's refuted
    # entry). What DOES move by resolving here instead of at the top of this
    # function: the worker's ``active_run`` state file and heartbeat thread
    # — the two things the supervisor watchdog and the orchestrator's
    # staleness check key on — are now written/started BEFORE this ~1 s
    # import instead of after, closing the window where a live worker looks
    # unregistered to the supervisor while it pays a tax neither the runner
    # nor the watchdog cares about it paying eagerly.
    harness_call_llm = _resolve_role_call_llm(args["harness_role"], role="harness")
    # Inner ADK agent model: when the harness role is a model spec, the agents
    # run on the configured endpoint (function-calling) instead of the shim.
    # Only the ADK adapter ever reads ``config.inner_model``
    # (``ADKHarnessAdapter.run``) — resolving it for a non-ADK adapter kind
    # would import ADK for a value nothing consumes, so it is skipped there.
    adapter_spec = args["adapter"]
    adapter_kind = adapter_spec.get("kind") if isinstance(adapter_spec, dict) else None
    inner_model = (
        _resolve_inner_model_from_role(args["harness_role"]) if adapter_kind == "adk" else None
    )
    auxiliary_call_llm = _resolve_role_call_llm(args["auxiliary_role"], role="auxiliary")
    # The judge role falls back to ``None`` when unconfigured, so
    # ``RuntimeConfig.effective_judge_call_llm`` resolves to the auxiliary.
    judge_call_llm = None
    judge_role = args.get("judge_role")
    if isinstance(judge_role, dict) and (judge_role.get("dotted") or judge_role.get("models_role")):
        judge_call_llm = _resolve_role_call_llm(judge_role, role="judge")
    user_emulator_call_llm = None
    emulator_role = args.get("user_emulator_role")
    if isinstance(emulator_role, dict) and (
        emulator_role.get("dotted") or emulator_role.get("models_role")
    ):
        user_emulator_call_llm = _resolve_role_call_llm(emulator_role, role="user_emulator")

    # Capture knobs (board reflection's capture fix). Runtime-only,
    # additive, NEVER contract-hashed. An ABSENT key defaults to True: the
    # capture is always-on with an opt-out, so a caller that passes no flag
    # still gets the artifacts. Both writes are best-effort: a capture
    # failure never re-scores or aborts a run.
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
        user_emulator_call_llm=user_emulator_call_llm,
        inner_model=inner_model,
        persist_run_results=persist_run_results,
        persist_judge_io=persist_judge_io,
        judge_io_sink=judge_io_sink,
        goldfive=_goldfive_config_for_adapter(weights, adapter_spec),
    )

    sinks, tracker = _build_sinks(
        events_path, harmonograf_url, harmonograf_grpc, harmonograf_metadata
    )
    adapter = build_adapter(adapter_spec)

    run_result: RunResult | None = None
    runtime_ms = 0
    budget_exceeded = False
    # Wall-clock position of this board unit (LossProfile.started_at /
    # ended_at). Wall clock rather than the monotonic clock ``runtime_ms`` measures:
    # a duration is comparable within one process, a position has to be
    # comparable across the round's workers. The span opens before the
    # adapter loads — loading the harness is part of the unit's occupancy of
    # a worker slot — and closes in the ``finally`` below, so it is stamped
    # on every exit including the budget timeout.
    started_at = now_iso()
    ended_at = started_at
    try:
        session = adapter.load(snapshot_root)
        _record_harness_load(
            workspace_root,
            epoch_id=epoch_id,
            generation_id=generation_id,
            session=session,
            snapshot_root=snapshot_root,
        )

        async def _drive() -> tuple[RunResult | None, int, bool]:
            return await _drive_session(
                session=session,
                entry=entry,
                events_path=events_path,
                sinks=sinks,
                config=config,
            )

        # Cooperative first line of defence: the worker enforces the
        # per-entry budget in-process. The parent's wait_for (budget plus
        # GRACE) is the second line;
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
        # The mutated-tree invariant's truth layer: now that the run has
        # finished, sys.modules says which mutable trees this unit actually
        # imported and from where. Raises (failing the unit rather than
        # scoring it) when a tree came from outside the snapshot; records a
        # never-imported tree for the generation's health finding otherwise.
        _verify_trees_after_run(
            workspace_root,
            epoch_id=epoch_id,
            generation_id=generation_id,
            session=session,
            snapshot_root=snapshot_root,
        )
    finally:
        # Closes the wall-clock span opened before the adapter load. First
        # statement of the block: everything below is bookkeeping (sink
        # close, terminal-event fallback) rather than the unit running.
        ended_at = now_iso()
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

    if scratch_dir is not None:
        try:
            from zicato.tournament.artifacts import capture_run_artifacts  # noqa: PLC0415

            artifacts = capture_run_artifacts(scratch_dir, loss_path)
            if run_result is not None:
                run_result = replace(run_result, artifacts=artifacts)
        except OSError as exc:
            log.warning("run %s artifact capture failed: %s", run_id, exc)

    expectation_result = await _evaluate_expectation(entry, run_result, config)

    # A run that did not complete successfully must be scored worst-case,
    # never zero. The worker reaches this point only on a clean worker
    # exit; the run ITSELF may still have failed. ``run_result.aborted``
    # is the adapter's verdict — set for a harness exception (a crash),
    # an emulator answer-leak abort, an unavailable scripted / emulated
    # driver, or an unsupported entry kind. ``budget_exceeded`` already
    # covers the wall-clock case and is passed separately. A ``None``
    # ``run_result`` is the bare-stub path — a genuinely completed run
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
    # Stamp the unit's wall-clock position (issue #242) — every profile the
    # worker writes carries one, aborted or not.
    loss = replace(loss, started_at=started_at, ended_at=ended_at)
    # Attribute the worst-case penalty ``run_not_completed`` just bought
    # (issue #245): the reducer adds a heavy fixed term and floors
    # ``task_failure_ratio``, which lands as a large ``drift_loss`` next to an
    # empty ``drift_counts``. The adapter's own reason is the only record of
    # WHY, and it goes here rather than on ``abort_cause`` — that field
    # decides cache eligibility, and any non-budget value there would stop
    # this scored failure from being persisted at all.
    if run_not_completed and run_result is not None:
        loss = replace(loss, not_completed_reason=run_result.abort_reason or None)
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
    # Stamp per-judge CALL-FAILURE provenance (issue #121). Both judge kinds
    # swallow their callable's exceptions by hard contract, and goldfive emits
    # no event for the empty verdict that produces — so without this stamp a
    # judge whose endpoint 404s on every invocation is byte-identical, in
    # loss.json AND events.jsonl, to a judge that ran and found nothing, and
    # loop health reports the broken one as dead weight. This worker process
    # evaluated exactly this one board unit, so the process-wide register is
    # this run's count. Empty tuple (the healthy case, every judge returned)
    # leaves the written bytes unchanged.
    judge_errors = judge_error_snapshot()
    if judge_errors:
        from dataclasses import replace as _replace  # noqa: PLC0415

        loss = _replace(loss, judge_errors=judge_errors)
    # Retain the measurement this write is about to truncate (issue #122),
    # the loss-side twin of the events archive in ``_build_sinks``. THIS is
    # the seam: the champion under ``--mode full`` is re-run every round and
    # each round's worker overwrites the slot, so by the time the parent's
    # ``_persist_unit_loss`` re-persists the same profile the predecessor is
    # already gone. Best-effort — a failed archive never costs the run its
    # loss.json.
    from zicato.tournament.unit_cache import archive_outgoing_unit_loss  # noqa: PLC0415

    with best_effort("unit_loss_archive"):
        archive_outgoing_unit_loss(loss_path)
    reducer_mod.write_loss_profile(loss, loss_path)

    # Persist the run's user-facing RunResult as result.json beside
    # loss.json (board reflection's capture fix) — replicate-slotted like
    # the loss, atomic (tmp+fsync+rename), and STRICTLY best-effort: a
    # capture failure is logged and the run proceeds unchanged (loss.json
    # is already on disk; the result file below and the exit code are
    # untouched). The budget-abort path's synthesized RunResult flows
    # through here too, so an aborted run's (empty) capture is honest.
    # ``run_result is None`` is the bare-stub path — no RunResult was
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
    Because both sides share ONE ``dataclasses.fields()``-driven serde, a
    field cannot be carried by the writer and dropped here (or the reverse).
    Two hand-aligned field lists would let it, and the worker would then
    score under defaults for the dropped field and say nothing — which is how
    ``per_judge_weights``, ``pass_rate_monotonicity_scope`` and
    ``drift_kind_aggregation`` each desynced.
    ``from_json`` coerces every field to its declared type and
    re-runs ``ScoringWeights.__post_init__``, so a malformed transform / scope
    token in the args file fails fast or coerces, as it does at contract load.
    """
    return ScoringWeights.from_json(args.get("weights"))


def _install_worker_log_stream_from_args(args: dict[str, Any]) -> None:
    """Install the worker-side operator-log stream from the args file.

    Reads the invocation stream path the runner threaded through
    (``log_stream_path``), the run coordinate (epoch / generation) and the
    parent-minted ``run_id`` to bind context, then appends this worker's
    records to that one stream. Fully best-effort: any failure is swallowed
    so logging setup can never fail a run.
    """
    try:
        path = args.get("log_stream_path")
        if not path:
            return
        from zicato.logging_stream import install_worker_log_stream  # noqa: PLC0415

        epoch_id = str(args.get("epoch_id") or "") or None
        generation_id = str(args.get("generation_id") or "") or None
        run_id = str(args.get("run_id") or "") or None
        install_worker_log_stream(
            path,
            epoch_id=epoch_id,
            generation_id=generation_id,
            run_id=run_id,
            level=str(args.get("log_level") or "INFO"),
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
