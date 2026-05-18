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
import importlib
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

log = logging.getLogger("zicato._tournament_worker")

#: Symbolic ``abort_reason`` the worker stamps onto its synthesised
#: result when its own cooperative ``asyncio.wait_for`` budget fires.
#: The parent maps a clean exit carrying this reason to "worker hit its
#: own budget" (as opposed to a parent- or supervisor-driven kill).
WORKER_BUDGET_ABORT_REASON = "wall_clock_budget"


# ---------------------------------------------------------------------------
# Args / result file shapes
# ---------------------------------------------------------------------------


def _import_callable(dotted: str) -> Any:
    """Resolve a ``pkg.mod:attr`` or ``pkg.mod.attr`` dotted path to a callable.

    Mirrors :func:`zicato.runtime_factory._import_callable` but kept
    local so the worker has no import-time dependency surface beyond the
    core types. Both the colon form (entry-point style) and the plain
    dot form are accepted because the runner serialises whichever the
    callable's ``__module__`` / ``__qualname__`` produced.
    """
    if ":" in dotted:
        module_path, _, attr = dotted.partition(":")
    else:
        module_path, _, attr = dotted.rpartition(".")
    if not module_path or not attr:
        raise ValueError(f"call_llm dotted path {dotted!r} must be 'pkg.module.attr'")
    module = importlib.import_module(module_path)
    obj: Any = module
    # Support nested attribute access (``Class.method`` style qualnames),
    # skipping any ``<locals>`` segments which are not importable.
    for part in attr.split("."):
        if part == "<locals>":
            raise ValueError(
                f"call_llm dotted path {dotted!r} refers to a closure-local "
                "callable that cannot be re-imported in a worker subprocess"
            )
        obj = getattr(obj, part)
    if not callable(obj):
        raise ValueError(f"call_llm dotted path {dotted!r} did not resolve to a callable")
    return obj


def _load_args(args_path: Path) -> dict[str, Any]:
    """Read and minimally validate the worker's JSON args file.

    The args file shape (one run)::

        {
          "workspace_root": "<abs path to .zicato dir>",
          "epoch_id": "<epoch id>",
          "generation_id": "<generation id>",
          "snapshot_root": "<abs path to a per-run code-snapshot working copy>",
          "entry": { ...BoardEntry as a dict (validate_board_entry shape)... },
          "adapter": {
            "kind": "adk",
            "entrypoint": "module.path:agent_symbol",
            "mutable_trees": ["<abs path>", ...]
          },
          "harness_call_llm": "pkg.module:callable",
          "auxiliary_call_llm": "pkg.module:callable",
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
        "harness_call_llm",
        "auxiliary_call_llm",
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


def _build_sinks(events_path: Path, harmonograf_url: str) -> list[Any]:
    """Build the per-run sink list: canonical JSONL plus optional harmonograf.

    Returns an empty list when goldfive is not installed — matching the
    runner's pre-existing tolerance for a no-goldfive environment.
    """
    try:
        from goldfive.sinks.persistence import JSONLPersistenceSink  # noqa: PLC0415
    except ModuleNotFoundError:
        return []

    events_path.parent.mkdir(parents=True, exist_ok=True)
    sinks: list[Any] = [JSONLPersistenceSink(path=events_path, mode="write")]

    if harmonograf_url:
        try:
            from zicato.telemetry.sink import _make_harmonograf_sink  # noqa: PLC0415

            extra = _make_harmonograf_sink(harmonograf_url)
            if extra is not None:
                sinks.append(extra)
        except Exception as exc:  # noqa: BLE001 — harmonograf is additive only
            log.warning("worker could not attach harmonograf sink: %s", exc)
    return sinks


async def _close_sinks(sinks: list[Any]) -> None:
    """Best-effort close of every sink so the JSONL is flushed to disk."""
    for s in sinks:
        try:
            await s.close()
        except Exception as exc:  # noqa: BLE001 — never fail a run on sink close
            log.debug("worker sink close failed: %s", exc)


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
        aux_call_llm=config.auxiliary_call_llm,
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

    entry = validate_board_entry(args["entry"])
    run_id = f"{generation_id}--{entry.id}"

    # The two LLM callables are re-imported from their dotted paths in
    # THIS fresh interpreter — they are necessarily distinct callables.
    harness_call_llm = _import_callable(str(args["harness_call_llm"]))
    auxiliary_call_llm = _import_callable(str(args["auxiliary_call_llm"]))
    config = RuntimeConfig(
        instance_id=str(args.get("instance_id", "default")),
        workspace_root=workspace_root,
        harness_call_llm=harness_call_llm,
        auxiliary_call_llm=auxiliary_call_llm,
        seed=args.get("seed"),
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

    sinks = _build_sinks(events_path, harmonograf_url)
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
        await _close_sinks(sinks)

    expectation_result = await _evaluate_expectation(entry, run_result, config)

    loss: LossProfile = reducer_mod.reduce_loss(
        events_path,
        entry,
        generation_id,
        epoch_id,
        expectation_result,
        runtime_ms,
        budget_exceeded,
        weights,
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
    """
    raw = args.get("weights") or {}
    if not isinstance(raw, dict):
        return ScoringWeights()
    defaults = ScoringWeights()
    return ScoringWeights(
        drift_weight=float(raw.get("drift_weight", defaults.drift_weight)),
        pass_weight=float(raw.get("pass_weight", defaults.pass_weight)),
        severity_weights=dict(raw.get("severity_weights", defaults.severity_weights)),
        per_kind_weights=dict(raw.get("per_kind_weights", defaults.per_kind_weights)),
        plan_revision_weight=float(raw.get("plan_revision_weight", defaults.plan_revision_weight)),
        runtime_weight=float(raw.get("runtime_weight", defaults.runtime_weight)),
        promote_margin=float(raw.get("promote_margin", defaults.promote_margin)),
        pass_rate_monotonicity=bool(
            raw.get("pass_rate_monotonicity", defaults.pass_rate_monotonicity)
        ),
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
