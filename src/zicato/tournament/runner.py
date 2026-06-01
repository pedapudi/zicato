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
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass, replace
from datetime import UTC
from pathlib import Path
from typing import Any

from zicato.core import (
    BoardEntry,
    Generation,
    LossProfile,
    RuntimeConfig,
    ScoringWeights,
)
from zicato.tournament.gate import GateOutcome, evaluate_gate
from zicato.tournament.regression import RegressionResult, run_regression_suite
from zicato.tournament.scoring import aggregate_generation_score

log = logging.getLogger("zicato.tournament.runner")

#: Location of the SQLite analytical index, relative to the workspace
#: root (the ``.zicato/`` directory). Sibling module ``zicato.index``
#: owns the schema; the runner only knows the path so it can dual-write.
_INDEX_DB_RELPATH = "index.db"

#: Minimum interval (seconds) between successive ``last_progress`` bumps
#: for a single in-flight run. The per-run sink is wrapped so every
#: goldfive event would otherwise trigger a state-file write; throttling
#: keeps a chatty run from turning into a write storm on the runtime
#: directory.
_PROGRESS_BUMP_MIN_INTERVAL_S = 2.0


def _index_db_path(workspace_root: Path) -> Path:
    """Return the SQLite analytical index path for a workspace."""
    return workspace_root / _INDEX_DB_RELPATH


def _ingest_run_into_index(
    workspace_root: Path,
    epoch_id: str,
    generation_id: str,
    entry_id: str,
) -> None:
    """Best-effort dual-write of one run into the SQLite analytical index.

    Called after the run's ``loss.json`` has been written so the live
    index stays current as the tournament runs. The index module is a
    sibling that may not be present (it lands in parallel); the import is
    lazy and any failure — a missing module, a schema mismatch, an I/O
    error — is logged at ``debug`` level and swallowed. The on-disk
    ``loss.json`` / ``events.jsonl`` remain canonical and ``zicato
    reindex`` can always rebuild the index from scratch.
    """
    try:
        from zicato.index.ingest import ingest_run  # noqa: PLC0415

        ingest_run(
            workspace_root,
            _index_db_path(workspace_root),
            epoch_id,
            generation_id,
            entry_id,
        )
    except ImportError:
        # The index sibling is not installed in this environment — the
        # loop runs fine without the live index.
        log.debug("zicato.index.ingest unavailable; skipping live index dual-write")
    except Exception as exc:  # noqa: BLE001 — index write is best-effort
        log.debug(
            "live index ingest_run skipped for %s/%s/%s: %s",
            epoch_id,
            generation_id,
            entry_id,
            exc,
        )


@dataclass(frozen=True, slots=True)
class TournamentResult:
    """The full output of one tournament evaluation.

    Carries the parent and child generation ids, both per-generation
    aggregate dicts (see :func:`aggregate_generation_score`), the gate
    outcome, and a per-entry mapping of the two loss profiles for
    journaling. Fully JSON-serializable via
    :func:`dataclasses.asdict` + :func:`json.dumps` with
    ``default=str``.
    """

    parent_generation_id: str
    child_generation_id: str
    parent_agg: dict[str, Any]
    child_agg: dict[str, Any]
    outcome: GateOutcome
    per_entry_losses: dict[str, tuple[LossProfile, LossProfile]]


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


def _telemetry_helpers() -> tuple[Any, Any]:
    """Lazily import ``zicato.telemetry.sink`` and ``.reducer``.

    Imported per-call rather than at module load so the tournament
    package keeps loading even before telemetry is wired up; the cost
    of the per-call import is negligible compared to the actual run.
    """
    from zicato.telemetry import reducer as _reducer  # noqa: PLC0415
    from zicato.telemetry import sink as _sink  # noqa: PLC0415

    return _sink, _reducer


def _now_iso_utc() -> str:
    from datetime import datetime  # noqa: PLC0415

    return datetime.now(UTC).isoformat()


def _run_id_for(generation: Generation, entry: BoardEntry) -> str:
    return f"{generation.id}--{entry.id}"


#: ``BoardEntry.context`` key under which the board-level ``disable_drift``
#: suppression set is threaded to the adapter. ``context`` is the only
#: per-entry channel that already survives the full
#: runner -> args-file -> subprocess-worker -> ``validate_board_entry`` ->
#: adapter round-trip (it is a plain string-valued mapping serialised by
#: ``zicato.board.jsonl._entry_to_dict`` and re-parsed by
#: ``validate_board_entry``), and it is exactly what
#: ``zicato.adapters.adk._entry_disable_drift`` reads back. The value is a
#: space-separated list of ``goldfive.DriftKind`` wire strings.
_DISABLE_DRIFT_CONTEXT_KEY = "disable_drift"


def _drift_kind_wire(kind: Any) -> str:
    """Project one ``disable_drift`` element to its lowercase wire string.

    ``goldfive.DriftKind`` is a ``StrEnum`` whose ``.value`` is the wire
    token; a bare string is accepted unchanged. A stray
    ``"DriftKind.TOOL_ERROR"`` repr is normalised to its last dotted
    segment, lowercased — the same projection
    ``zicato.judge_runtime.disable`` applies on the read side, so the two
    ends agree on the token form.
    """
    text = str(getattr(kind, "value", kind)).strip()
    if "." in text:
        text = text.rsplit(".", 1)[-1]
    return text.lower()


def _stamp_disable_drift(
    board: list[BoardEntry],
    disable_drift: tuple[Any, ...],
) -> list[BoardEntry]:
    """Return ``board`` with the board-level ``disable_drift`` set on each entry.

    The board-level ``disable_drift`` (parsed by
    :func:`zicato.board.jsonl.load_board_with_meta` from the ``board_meta``
    header) is not a per-entry field, but the adapter is only ever handed
    a :class:`BoardEntry`. This stamps the suppression set onto every
    entry's :attr:`~zicato.core.BoardEntry.context` mapping under
    :data:`_DISABLE_DRIFT_CONTEXT_KEY` so it threads end-to-end —
    through the subprocess worker's entry (de)serialisation — to
    :func:`zicato.adapters.adk._entry_disable_drift`.

    When ``disable_drift`` is empty the board is returned unchanged, so a
    board with no ``board_meta`` header — and any per-entry
    ``context['disable_drift']`` an author set directly — is untouched.
    When non-empty the board-level setting is authoritative: it
    overwrites any per-entry value. :class:`BoardEntry` is frozen, so
    each affected entry is rebuilt via :func:`dataclasses.replace` rather
    than mutated.
    """
    if not disable_drift:
        return board
    wire = " ".join(_drift_kind_wire(k) for k in disable_drift)
    stamped: list[BoardEntry] = []
    for entry in board:
        context = dict(entry.context)
        context[_DISABLE_DRIFT_CONTEXT_KEY] = wire
        stamped.append(replace(entry, context=context))
    return stamped


def _runtime_state() -> tuple[Any, Any] | None:
    """Lazy-import runtime state helpers; return None if unavailable.

    Returns a ``(state_module, ActiveRun)`` pair, or ``None`` when the
    runtime-state subsystem is not importable. Typed as ``tuple[Any,
    Any]`` because the first element is a module object — there is no
    useful static type for it — and an explicit annotation keeps the
    call sites out of mypy's ``no-untyped-call`` net.
    """
    try:
        from zicato.runtime import state as state_mod  # noqa: PLC0415
        from zicato.runtime.state import ActiveRun  # noqa: PLC0415

        return state_mod, ActiveRun
    except ImportError:
        return None


# ---------------------------------------------------------------------------
# Subprocess worker spawn — the "L3" robustness layer.
# ---------------------------------------------------------------------------
#
# Every tournament run now executes in its OWN OS process: a
# ``python -m zicato._tournament_worker`` subprocess. The motivation is
# hard-enforcement of the per-run wall-clock budget. A run wedged inside
# the orchestrator process used to be un-killable without killing the
# whole ``evolve``; isolated in a subprocess it can be SIGTERM'd then
# SIGKILL'd by this parent — and, independently, by the supervisor
# watchdog keyed on the worker's own pid in ``active_runs/{run_id}.json``.
#
# A free side benefit: the Python-module-caching problem (two
# generations' source loaded into one interpreter, ``sys.modules``
# handing back the wrong one) disappears — each worker imports exactly
# one generation snapshot and then exits.

#: Margin (seconds) added to the entry's wall-clock budget before the
#: PARENT's ``asyncio.wait_for`` fires. The worker keeps its own
#: cooperative ``asyncio.wait_for`` at exactly the entry budget, so under
#: normal operation the worker aborts itself first and exits cleanly with
#: a budget-exceeded result. The parent's wait_for is the SECOND line of
#: defence — it only fires when the worker's cooperative budget did not
#: (a worker stuck in a C extension, a blocked syscall, a hung import).
#: 30s is comfortably longer than the worker's own teardown + loss-reduce
#: path so a healthy worker is never racing the parent.
_PARENT_BUDGET_GRACE_S: float = 30.0

#: Seconds the parent waits after SIGTERM before escalating to SIGKILL.
#: Matches the supervisor's two-stage escalation grace.
_SIGTERM_TO_SIGKILL_GRACE_S: float = 5.0

#: Filename prefix for a run's ephemeral snapshot working copy. The copy
#: lives in the system temp dir (``tempfile.mkdtemp``) so it never sits
#: inside the workspace tree — nothing under it can be mistaken for a
#: canonical generation snapshot, and it is removed when the run ends.
_EPHEMERAL_SNAPSHOT_PREFIX = "ztw-snap-"


def _make_ephemeral_snapshot(snapshot_root: Path, run_id: str) -> tuple[Path, Path]:
    """Copy a generation's code snapshot into a fresh per-run working dir.

    The worker is pointed at the returned snapshot path, NOT at
    ``snapshot_root`` itself. The canonical snapshot must stay code-only:
    it is the tree
    :meth:`zicato.epoch.genstore.DirectoryGenerationStore.derive_generation`
    copies forward to seed every subsequent generation, so any pollution
    there accumulates without bound (a real disk-exhaustion failure).

    Two layers protect the canonical snapshot:

    1. **Run output is routed to a per-run scratch directory** — this
       function also creates a sibling scratch directory and returns it.
       A target reads the scratch path from
       :data:`zicato.epoch.snapshot_scope.SCRATCH_DIR_ENV` and writes
       there, *outside* its own source tree. That is the primary fix.
    2. **The ephemeral working copy** — a stray write that ignores the
       scratch directory and lands next to the agent's own code still
       only pollutes this throwaway copy, not the canonical snapshot.

    The copy is a plain :func:`shutil.copytree` filtered by the shared
    snapshot-scope ignore — code snapshots are KB-sized, so a copy per
    run (even when many board units run concurrently, and even with the
    champion and challenger of one entry running at once) is cheap. Both
    the working copy and the scratch directory are created under a single
    :func:`tempfile.mkdtemp` parent in the OS temp dir, deliberately
    OUTSIDE the workspace tree.

    The caller owns cleanup — see :func:`_discard_ephemeral_snapshot`,
    which :func:`_run_single` invokes from its ``finally`` block so the
    whole mkdtemp parent (working copy *and* scratch directory) is
    removed even when the run aborts or crashes.

    Returns ``(working_copy, scratch_dir)``: the path the worker mounts
    as the inner harness's source root, and the per-run scratch directory
    the worker exports to the harness via the scratch-dir env var.
    """
    from zicato.epoch.snapshot_scope import copytree_ignore  # noqa: PLC0415

    parent = Path(tempfile.mkdtemp(prefix=f"{_EPHEMERAL_SNAPSHOT_PREFIX}{run_id}-"))
    # Copy *into* a child of the mkdtemp dir, keeping the snapshot's own
    # basename so any path the agent derives from ``__file__`` looks the
    # same as it would under the canonical snapshot. The ignore filter
    # drops run artifacts so a copy never carries forward stale output.
    working_copy = parent / Path(snapshot_root).name
    shutil.copytree(snapshot_root, working_copy, ignore=copytree_ignore())
    # The per-run scratch directory: a sibling of the working copy under
    # the same mkdtemp parent, so one cleanup removes both.
    scratch_dir = parent / "run-scratch"
    scratch_dir.mkdir()
    return working_copy, scratch_dir


def _discard_ephemeral_snapshot(working_copy: Path | None) -> None:
    """Remove a per-run ephemeral snapshot working copy and its temp parent.

    Best-effort: a cleanup failure must never turn a finished run into a
    crash. Removes the whole :func:`tempfile.mkdtemp` directory (the
    working copy's parent), not just the working copy, so no empty temp
    directory is left behind. ``None`` — the run never got as far as
    making a copy — is a no-op.
    """
    if working_copy is None:
        return
    # The mkdtemp dir is the working copy's parent; remove the whole thing.
    temp_root = working_copy.parent
    try:
        shutil.rmtree(temp_root, ignore_errors=True)
    except OSError as exc:  # noqa: BLE001 — ignore_errors already swallows most
        log.debug("ephemeral snapshot cleanup skipped for %s: %s", temp_root, exc)


def _callable_dotted_path(fn: Any) -> str:
    """Return a re-importable ``module:qualname`` dotted path for ``fn``.

    The worker subprocess re-imports the harness / auxiliary LLM
    callables from these paths. A callable must therefore be a
    module-level (or class-attribute) object; a closure-local callable
    has ``<locals>`` in its ``__qualname__`` and cannot be re-imported —
    we surface that as a clear :class:`ValueError` at spawn time rather
    than letting the worker fail opaquely.
    """
    module = getattr(fn, "__module__", None)
    qualname = getattr(fn, "__qualname__", None) or getattr(fn, "__name__", None)
    if not module or not qualname:
        raise ValueError(
            f"cannot derive an import path for callable {fn!r}: it has no __module__/__qualname__"
        )
    if "<locals>" in qualname:
        raise ValueError(
            f"callable {module}:{qualname} is defined inside a function "
            "(closure-local) and cannot be re-imported by a subprocess "
            "worker; pass a module-level callable instead"
        )
    return f"{module}:{qualname}"


def _adapter_spec(adapter: Any) -> dict[str, Any]:
    """Serialise a harness adapter into a JSON-friendly spec dict.

    The worker reconstructs the adapter from this dict (see
    :func:`zicato._tournament_worker._build_adapter`). Resolution order:

    1. If the adapter exposes a ``worker_spec()`` method, its return
       value is used verbatim — the adapter knows best how to make
       itself re-constructible in a subprocess. This is the
       extensibility hook for non-ADK adapters.
    2. Otherwise the :class:`~zicato.adapters.adk.ADKHarnessAdapter`
       shape is recognised by its ``name == "adk"`` plus the private
       ``_entrypoint`` attribute and the public ``mutable_trees`` list.

    Raises :class:`ValueError` when neither path applies; ``_run_single``
    turns that into an aborted run rather than crashing the tournament.
    """
    worker_spec = getattr(adapter, "worker_spec", None)
    if callable(worker_spec):
        spec = worker_spec()
        if isinstance(spec, dict):
            return spec
        raise ValueError(
            f"adapter {adapter!r}.worker_spec() returned {type(spec).__name__}, expected a dict"
        )

    name = getattr(adapter, "name", None)
    entrypoint = getattr(adapter, "_entrypoint", None)
    if name != "adk" or not entrypoint:
        raise ValueError(
            f"cannot serialise adapter {adapter!r} for subprocess execution: "
            "only the 'adk' adapter shape (or an adapter exposing a "
            "worker_spec() method) is supported"
        )
    trees = [str(Path(p)) for p in getattr(adapter, "mutable_trees", []) or []]
    return {"kind": "adk", "entrypoint": str(entrypoint), "mutable_trees": trees}


def _weights_spec(weights: ScoringWeights) -> dict[str, Any]:
    """Serialise :class:`ScoringWeights` into a JSON-friendly subset dict.

    Only the scalar / mapping / tuple fields are carried — enough for the
    worker's :func:`reduce_loss` call. The worker rebuilds a
    :class:`ScoringWeights` from this, defaulting any absent key.
    """
    return {
        "drift_weight": weights.drift_weight,
        "pass_weight": weights.pass_weight,
        "severity_weights": dict(weights.severity_weights),
        "per_kind_weights": dict(weights.per_kind_weights),
        "plan_revision_weight": weights.plan_revision_weight,
        "runtime_weight": weights.runtime_weight,
        "promote_margin": weights.promote_margin,
        "pass_rate_monotonicity": weights.pass_rate_monotonicity,
        "regression_gate_enabled": weights.regression_gate_enabled,
        "regression_test_command": list(weights.regression_test_command),
        "regression_timeout_s": weights.regression_timeout_s,
        "namespace_weights": dict(weights.namespace_weights),
        "namespace_monotonicity": dict(weights.namespace_monotonicity),
    }


def _entry_to_dict(entry: BoardEntry) -> dict[str, Any]:
    """Serialise a :class:`BoardEntry` into the JSON shape ``validate_board_entry`` reads.

    The worker re-parses the entry via
    :func:`zicato.core.validate_board_entry`, so the dict must match that
    parser's expected shape (nested ``expectation`` / ``user_persona`` /
    ``turns`` sub-dicts; lists rather than tuples).
    """
    out: dict[str, Any] = {
        "id": entry.id,
        "kind": entry.kind,
        "wall_clock_budget_seconds": entry.wall_clock_budget_seconds,
        "weight": entry.weight,
        "tags": list(entry.tags),
        "context": dict(entry.context),
    }
    if entry.expectation is not None:
        out["expectation"] = {
            "kind": entry.expectation.kind,
            "spec": entry.expectation.spec,
            "reads": entry.expectation.reads,
        }
    if entry.judges:
        out["judges"] = [
            {
                "name": j.name,
                "mode": j.mode.value if hasattr(j.mode, "value") else j.mode,
                "body": j.body,
                "severity": (j.severity.value if hasattr(j.severity, "value") else j.severity),
            }
            for j in entry.judges
        ]
    if entry.input is not None:
        out["input"] = entry.input
    if entry.turns is not None:
        out["turns"] = [{"user": t.user} for t in entry.turns]
    if entry.user_persona is not None:
        out["user_persona"] = {
            "goal": entry.user_persona.goal,
            "constraints": entry.user_persona.constraints,
            "stop_when": entry.user_persona.stop_when,
        }
    if entry.max_turns is not None:
        out["max_turns"] = entry.max_turns
    if entry.adversarial_agent_spec is not None:
        out["adversarial_agent_spec"] = entry.adversarial_agent_spec
    if entry.required_drift_kinds is not None:
        out["required_drift_kinds"] = list(entry.required_drift_kinds)
    return out


def _resolve_harmonograf_url(workspace_root: Path) -> str:
    """Best-effort harmonograf URL resolution for the worker args file.

    The worker runs in a fresh process and re-resolves the env var on
    its own, but the workspace ``config.json`` value is read here (in the
    orchestrator process) and threaded through the args file so the
    worker does not need the workspace-config loader.

    Resilient to a missing / unreadable workspace ``config.json``:
    ``resolve_harmonograf_url`` is called with whatever config dict we
    could load (or ``None`` on failure), so the
    ``ZICATO_HARMONOGRAF_URL`` env path — which the orchestrator's auto-
    launch wiring (#202) writes into — keeps working even when the
    workspace has no on-disk config yet (smoke tests, fresh init).
    """
    try:
        from zicato.telemetry.sink import resolve_harmonograf_url  # noqa: PLC0415
    except Exception:  # noqa: BLE001 — harmonograf wiring is optional
        return ""
    cfg: dict[str, Any] | None
    try:
        from zicato import workspace_loader  # noqa: PLC0415

        cfg = workspace_loader.load_workspace_config(workspace_root)
    except Exception:  # noqa: BLE001 — config is optional; env still wins
        cfg = None
    try:
        return resolve_harmonograf_url(cfg)
    except Exception:  # noqa: BLE001 — never fail a tournament on URL resolution
        return ""


#: Multiplier on ``task_failure_ratio`` in the synthesised aborted-run
#: loss — mirrors :data:`zicato.telemetry.reducer._TASK_FAILURE_RATIO_MULTIPLIER`
#: ("pure failures matter"). Kept as a local constant so this module does
#: not import the reducer just to compute an empty-counts loss.
_ABORTED_TASK_FAILURE_MULTIPLIER: float = 10.0


def _aborted_loss_profile(
    *,
    run_id: str,
    entry: BoardEntry,
    generation_id: str,
    epoch_id: str,
    weights: ScoringWeights,
    runtime_ms: int,
) -> LossProfile:
    """Synthesise a worst-case aborted :class:`LossProfile` for one run.

    Used when the parent has to kill a wedged worker, or when a worker
    vanished (supervisor SIGKILL) without leaving a result file. The
    profile carries ``wall_clock_budget_exceeded=True``.

    The ``drift_loss`` scalar is computed inline (empty drift counts, a
    full ``task_failure_ratio`` of 1.0, the heavy fixed budget-exceeded
    term) rather than by calling into the reducer — the runner must be
    able to synthesise a definite-loss profile even when the reducer is
    unavailable, so the tournament can always aggregate a killed run as a
    loss for the entry.
    """
    sev_vals = list(weights.severity_weights.values()) or [1.0]
    drift_loss = (
        weights.runtime_weight * (runtime_ms / 1000.0)
        + _ABORTED_TASK_FAILURE_MULTIPLIER * 1.0
        + 5.0 * max(sev_vals)
    )
    drift_loss = max(0.0, drift_loss)
    return LossProfile(
        run_id=run_id,
        entry_id=entry.id,
        generation_id=generation_id,
        epoch_id=epoch_id,
        drift_counts=(),
        plan_revisions=0,
        task_failure_ratio=1.0,
        runtime_ms=runtime_ms,
        wall_clock_budget_exceeded=True,
        expectation_result=None,
        drift_loss=drift_loss,
        pass_fail=(False if entry.expectation is not None else None),
    )


async def _terminate_worker(proc: Any) -> None:
    """Escalate SIGTERM -> (grace) -> SIGKILL on a wedged worker process.

    Mirrors the supervisor's two-stage escalation. After SIGTERM we wait
    :data:`_SIGTERM_TO_SIGKILL_GRACE_S` for a clean exit; if the worker
    is still alive we SIGKILL it. Either way we ``await proc.wait()`` so
    no zombie is left and the parent observes the final exit code.
    """
    if proc.returncode is not None:
        return
    try:
        proc.terminate()
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(proc.wait(), timeout=_SIGTERM_TO_SIGKILL_GRACE_S)
        return
    except TimeoutError:
        pass
    try:
        proc.kill()
    except ProcessLookupError:
        pass
    try:
        await proc.wait()
    except ProcessLookupError:
        pass


def _load_worker_result(result_path: Path) -> dict[str, Any] | None:
    """Read the worker's result JSON; return ``None`` if missing or corrupt.

    A missing or unparseable result file is the canonical signal that the
    worker did NOT finish cleanly — it was SIGKILLed (by the parent's own
    escalation or by the supervisor) before it could write the file.
    ``_run_single`` treats that as a normal aborted-run outcome, never a
    crash.
    """
    if not result_path.exists():
        return None
    try:
        with open(result_path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return data


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
) -> LossProfile:
    """Run one entry under one generation in an isolated subprocess worker.

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
                status="running",
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
            args_payload = {
                "workspace_root": str(workspace_root),
                "epoch_id": epoch_id,
                "generation_id": generation.id,
                "snapshot_root": str(ephemeral_snapshot),
                "scratch_dir": str(scratch_dir),
                "entry": _entry_to_dict(entry),
                "adapter": _adapter_spec(adapter),
                "harness_call_llm": _callable_dotted_path(config.harness_call_llm),
                "auxiliary_call_llm": _callable_dotted_path(config.auxiliary_call_llm),
                "sink_events_path": str(sink_path),
                "loss_path": str(loss_path),
                "result_path": str(result_path),
                "instance_id": config.instance_id,
                "seed": config.seed,
                "harmonograf_url": _resolve_harmonograf_url(workspace_root),
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
            )
            return final_loss

        # --- 3. Spawn the worker subprocess. ---
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "zicato._tournament_worker",
            str(args_path),
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
            # Escalate SIGTERM -> SIGKILL ourselves.
            killed_by_parent = True
            log.warning(
                "run %s exceeded budget+grace (%.0fs); terminating worker",
                run_id,
                budget_s + _PARENT_BUDGET_GRACE_S,
            )
            await _terminate_worker(proc)

        runtime_ms = int((time.monotonic() - spawn_started) * 1000)
        result = _load_worker_result(result_path)

        if killed_by_parent or result is None or proc.returncode != 0:
            # Aborted run. Three indistinguishable-and-equivalent causes,
            # all NORMAL outcomes that must not abort the tournament:
            #   * the PARENT killed a wedged worker (killed_by_parent),
            #   * the SUPERVISOR SIGKILLed a worker past its deadline
            #     (process gone, result file missing),
            #   * the worker process itself crashed (non-zero exit, no
            #     usable result file).
            if not killed_by_parent and result is None:
                log.info(
                    "run %s: worker gone with no result file "
                    "(supervisor kill or crash); recording aborted run",
                    run_id,
                )
            elif proc.returncode not in (0, None):
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
            )
            return final_loss

        # Live index dual-write: the run's loss.json is on disk, so fold
        # it into the SQLite analytical index. Best-effort.
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

    The accumulators are plain lists guarded by an :class:`asyncio.Lock`.
    The lock is not strictly required while the runner stays
    single-threaded — a coroutine body runs uninterrupted between
    ``await`` points — but it makes the read-modify-recompute-persist
    sequence an explicit critical section, so a future move of any part
    of it onto a thread (or an interleaving ``await`` added inside
    ``record``) cannot corrupt the running aggregate. The state write is
    strictly best-effort: a missing runtime-state module or an I/O error
    is swallowed, exactly as every other dashboard-facing write in this
    module — incremental scoring must never abort a run.
    """

    __slots__ = (
        "_weights",
        "_workspace_root",
        "_champion",
        "_challenger",
        "_lock",
        "_state",
    )

    def __init__(self, weights: ScoringWeights, workspace_root: Path) -> None:
        self._weights = weights
        self._workspace_root = workspace_root
        self._champion: list[LossProfile] = []
        self._challenger: list[LossProfile] = []
        self._lock = asyncio.Lock()
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
            if self._state is None:
                return
            try:
                self._state.update_tournament_partial_aggregate(
                    self._workspace_root,
                    champion_agg=champion_agg,
                    challenger_agg=challenger_agg,
                )
            except Exception as exc:  # noqa: BLE001 — partial scoring is best-effort
                log.debug("partial-aggregate persist skipped: %s", exc)


async def _run_full_board_unit(
    *,
    adapter: Any,
    parent_gen: Generation,
    child_gen: Generation,
    entry: BoardEntry,
    weights: ScoringWeights,
    config: RuntimeConfig,
    workspace_root: Path,
    epoch_id: str,
    scorer: _IncrementalScorer | None = None,
) -> tuple[LossProfile, LossProfile]:
    """Run ONE board entry's champion + challenger concurrently.

    A board unit in full mode owns both sides of a single board entry.
    This launches the champion (``parent_gen``) run and the challenger
    (``child_gen``) run **simultaneously** — two :func:`_run_single`
    coroutines started together under one :func:`asyncio.gather` — and
    does not return until BOTH have settled.

    The two runs are safely concurrent: :func:`_run_single` spawns each
    in its OWN subprocess worker, each pointed at its OWN per-run
    ephemeral snapshot working copy (a distinct ``tempfile.mkdtemp``
    tree, see :func:`_make_ephemeral_snapshot`) and writing to a
    distinct ``run_id`` (``{generation_id}--{entry_id}``, and the two
    generations differ). So nothing — snapshot copy, ``active_runs``
    state file, ``loss.json`` — is shared between the champion and
    challenger of the same entry.

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
    parent_result, child_result = await asyncio.gather(
        _run_single(
            adapter=adapter,
            generation=parent_gen,
            entry=entry,
            weights=weights,
            config=config,
            workspace_root=workspace_root,
            epoch_id=epoch_id,
            side="parent",
        ),
        _run_single(
            adapter=adapter,
            generation=child_gen,
            entry=entry,
            weights=weights,
            config=config,
            workspace_root=workspace_root,
            epoch_id=epoch_id,
            side="child",
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


async def _run_board_units_full(
    *,
    adapter: Any,
    parent_gen: Generation,
    child_gen: Generation,
    board: list[BoardEntry],
    weights: ScoringWeights,
    config: RuntimeConfig,
    workspace_root: Path,
    epoch_id: str,
) -> tuple[dict[str, LossProfile], dict[str, LossProfile]]:
    """Run every board entry as a full-mode board unit, bounded concurrency.

    The board entries are the "boards" of the tournament hall: up to
    :attr:`RuntimeConfig.parallelism` BOARD UNITS play at once. The
    semaphore counts board units, not subprocesses — in full mode each
    admitted unit runs champion + challenger concurrently (see
    :func:`_run_full_board_unit`), so ``parallelism`` board units mean up
    to ``2 * parallelism`` run subprocesses alive at once.

    ``parallelism == 1`` admits exactly one board unit at a time, in
    board order; the next entry's champion/challenger pair does not start
    until the current entry's pair has fully settled (subprocess spawn,
    wait, loss read-back, AND ``finally`` cleanup, on both sides). It is
    NOT byte-identical to the historical generation-at-a-time runner
    (which scored the whole parent board before the child board) — but
    the gate still compares two fully-aggregated generations, so the
    decision is unchanged.

    Result ordering is independent of completion order: the two
    ``entry.id -> LossProfile`` maps are rebuilt by zipping the board
    (input order) with the gather results (:func:`asyncio.gather`
    preserves submission order). Failure handling matches the historical
    contract: a raising board unit does not cancel in-flight siblings,
    and the first failure (board order) is re-raised after every sibling
    has settled.

    Each board unit is scored the instant its champion + challenger
    runs settle — see :class:`_IncrementalScorer`. The running partial
    aggregate is rewritten onto the live
    :class:`~zicato.runtime.state.ActiveTournament` as every unit
    finishes, so a reader (the dashboard) watches the server-side
    ``scalar`` accumulate concurrently with the boards still in flight,
    rather than seeing 0.00 until the whole round ends.

    Returns ``(parent_losses, child_losses)`` — the per-entry champion
    and challenger loss maps.
    """
    semaphore = asyncio.Semaphore(config.parallelism)
    scorer = _IncrementalScorer(weights, workspace_root)

    async def _bounded(entry: BoardEntry) -> tuple[LossProfile, LossProfile]:
        async with semaphore:
            return await _run_full_board_unit(
                adapter=adapter,
                parent_gen=parent_gen,
                child_gen=child_gen,
                entry=entry,
                weights=weights,
                config=config,
                workspace_root=workspace_root,
                epoch_id=epoch_id,
                scorer=scorer,
            )

    results = await asyncio.gather(
        *(_bounded(entry) for entry in board),
        return_exceptions=True,
    )
    for result in results:
        if isinstance(result, BaseException):
            raise result

    parent_losses: dict[str, LossProfile] = {}
    child_losses: dict[str, LossProfile] = {}
    for entry, result in zip(board, results, strict=True):
        # Every result is a (parent, child) tuple here: the loop above
        # already re-raised on the first BaseException.
        parent_loss, child_loss = result  # type: ignore[misc]
        parent_losses[entry.id] = parent_loss
        child_losses[entry.id] = child_loss
    return parent_losses, child_losses


async def _run_board_units_fast(
    *,
    adapter: Any,
    child_gen: Generation,
    board: list[BoardEntry],
    weights: ScoringWeights,
    config: RuntimeConfig,
    workspace_root: Path,
    epoch_id: str,
) -> dict[str, LossProfile]:
    """Run every board entry as a fast-mode board unit, bounded concurrency.

    A fast-mode board unit runs ONLY the challenger (child) — the
    champion's cached ``gen_score.json`` aggregate is reused, so no
    champion run is executed. Up to :attr:`RuntimeConfig.parallelism`
    board units play at once; with one challenger run per unit, that is
    up to ``parallelism`` run subprocesses alive at once (half the
    full-mode ceiling).

    ``parallelism == 1`` admits exactly one challenger run at a time, in
    board order. Result ordering, failure surfacing (first failure in
    board order, no sibling cancellation) match
    :func:`_run_board_units_full`. Returns the per-entry challenger loss
    map.

    As in full mode, each board unit is scored the instant its
    challenger run settles — see :class:`_IncrementalScorer` — so the
    running partial aggregate (challenger side only; fast mode has no
    champion run) is rewritten onto any live
    :class:`~zicato.runtime.state.ActiveTournament` as every unit
    finishes, concurrently with the boards still in flight.
    """
    semaphore = asyncio.Semaphore(config.parallelism)
    scorer = _IncrementalScorer(weights, workspace_root)

    async def _bounded(entry: BoardEntry) -> LossProfile:
        async with semaphore:
            child_loss = await _run_single(
                adapter=adapter,
                generation=child_gen,
                entry=entry,
                weights=weights,
                config=config,
                workspace_root=workspace_root,
                epoch_id=epoch_id,
                # Fast mode runs only the challenger; the side label is
                # "child" for the rare case an ActiveTournament file does
                # exist, and a benign no-op otherwise.
                side="child",
            )
            # Score this board unit the instant it settles — concurrently
            # with the sibling board units still running.
            await scorer.record(challenger_loss=child_loss)
            return child_loss

    results = await asyncio.gather(
        *(_bounded(entry) for entry in board),
        return_exceptions=True,
    )
    for result in results:
        if isinstance(result, BaseException):
            raise result

    losses: dict[str, LossProfile] = {}
    for entry, result in zip(board, results, strict=True):
        losses[entry.id] = result  # type: ignore[assignment]
    return losses


async def _gate_with_regression(
    *,
    parent_agg: dict[str, Any],
    child_agg: dict[str, Any],
    child_snapshot_root: Path,
    weights: ScoringWeights,
) -> GateOutcome:
    """Apply the promote gate, prefixed by a regression-suite check.

    The regression check is a HARD GATE: when
    :attr:`ScoringWeights.regression_gate_enabled` is true, the child
    snapshot's own test suite runs as a subprocess BEFORE we evaluate
    the scoring gate. Any failure (or timeout) forces the
    :class:`GateOutcome` to ``"rejected"`` with a reason like
    ``"regression suite failed: N tests"`` — regardless of how strongly
    the child improved on drift_loss / pass_rate.

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
    return evaluate_gate(parent_agg, child_agg, weights)


def _regression_rejection(
    parent_agg: dict[str, Any],
    child_agg: dict[str, Any],
    regression: RegressionResult,
) -> GateOutcome:
    """Build the ``rejected`` :class:`GateOutcome` for a regression failure.

    The reason string is short enough to fit on one journal line:
    ``"regression suite failed: <N> tests"`` for ordinary failures or
    ``"regression suite failed: <summary>"`` for timeouts / exit-code-
    only failures. Deltas are computed from the aggregate dicts so the
    rejection record still carries the scoring evidence.
    """
    parent_scalar = float(parent_agg.get("scalar", 0.0))
    child_scalar = float(child_agg.get("scalar", 0.0))
    parent_pass = float(parent_agg.get("pass_rate", 1.0))
    child_pass = float(child_agg.get("pass_rate", 1.0))
    if regression.failed_tests:
        reason = f"regression suite failed: {len(regression.failed_tests)} tests"
    else:
        reason = f"regression suite failed: {regression.summary}"
    return GateOutcome(
        decision="rejected",
        reason=reason,
        delta_scalar=child_scalar - parent_scalar,
        delta_pass_rate=child_pass - parent_pass,
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
    round_index: int = 0,
    total_rounds: int = 0,
) -> TournamentResult:
    """Run a full A/B tournament. See module docstring.

    ``disable_drift`` is the board-level drift-suppression set parsed
    from the board's ``board_meta`` header (see
    :func:`zicato.board.jsonl.load_board_with_meta`). It is stamped onto
    every board entry's :attr:`~zicato.core.BoardEntry.context` so it
    threads through to the adapter's judge assembly; an empty tuple (the
    default) leaves the board entries untouched.

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

    # Best-effort tournament-state publication for the live dashboard.
    rt = _runtime_state()
    if rt is not None:
        state_mod, _ = rt
        try:
            from zicato.runtime.state import (  # noqa: PLC0415
                ActiveTournament,
                ActiveTournamentEntry,
            )

            now = _now_iso_utc()
            entries = [
                ActiveTournamentEntry(entry_id=e.id, side="parent", status="queued") for e in board
            ] + [ActiveTournamentEntry(entry_id=e.id, side="child", status="queued") for e in board]
            state_mod.write_active_tournament(
                workspace_root,
                ActiveTournament(
                    tournament_id=f"tour-{parent_gen.id}-vs-{child_gen.id}-{now}",
                    parent_generation_id=parent_gen.id,
                    child_generation_id=child_gen.id,
                    epoch_id=epoch_id,
                    started_at=now,
                    entries=entries,
                    phase="running",
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
        parent_losses, child_losses = await _run_board_units_full(
            adapter=adapter,
            parent_gen=parent_gen,
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

    parent_agg = aggregate_generation_score(list(parent_losses.values()), weights)
    child_agg = aggregate_generation_score(list(child_losses.values()), weights)

    outcome = await _gate_with_regression(
        parent_agg=parent_agg,
        child_agg=child_agg,
        child_snapshot_root=child_gen.snapshot_root,
        weights=weights,
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

    # Same board-level disable_drift threading as the full A/B path.
    board = _stamp_disable_drift(board, disable_drift)

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
            )

            now = _now_iso_utc()
            cached_per_entry = parent_historical_agg.get("per_entry") or {}
            child_entries = [
                ActiveTournamentEntry(entry_id=e.id, side="child", status="queued") for e in board
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
                        side="parent",
                        status="cached",
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
                    phase="running",
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
    )


async def _run_replicated(
    *,
    adapter: Any,
    left_gen: Generation,
    right_gen: Generation,
    board: list[BoardEntry],
    weights: ScoringWeights,
    config: RuntimeConfig,
    workspace_root: Path,
    epoch_id: str,
    replicates: int,
) -> tuple[dict[str, LossProfile], dict[str, LossProfile]]:
    """Run a paired board ``replicates`` times, averaging per-entry losses.

    The §9-lever-1 replication knob. ``replicates == 1`` is the current
    single-run path (it simply returns ``_run_board_units_full``'s maps
    unchanged). For ``replicates > 1`` the paired board is run N times and
    the per-entry drift losses are averaged BEFORE aggregation, so a noisy
    single run no longer decides a duel. Only the scalar-bearing
    ``drift_loss`` is averaged; ``pass_fail`` is taken as the majority
    (true only when a strict majority of replicates passed), which keeps
    the pass-rate monotonicity rule meaningful under replication.

    The board-unit runner is reused verbatim — replication is a thin loop
    over it — so the per-run subprocess isolation, scoring, and failure
    surfacing are unchanged.
    """
    runs: list[tuple[dict[str, LossProfile], dict[str, LossProfile]]] = []
    for _ in range(max(1, replicates)):
        left_losses, right_losses = await _run_board_units_full(
            adapter=adapter,
            parent_gen=left_gen,
            child_gen=right_gen,
            board=board,
            weights=weights,
            config=config,
            workspace_root=workspace_root,
            epoch_id=epoch_id,
        )
        runs.append((left_losses, right_losses))
    if len(runs) == 1:
        return runs[0]
    left_avg = _average_losses([r[0] for r in runs])
    right_avg = _average_losses([r[1] for r in runs])
    return left_avg, right_avg


def _average_losses(
    runs: list[dict[str, LossProfile]],
) -> dict[str, LossProfile]:
    """Average per-entry ``drift_loss`` across replicate runs.

    Returns one ``entry_id -> LossProfile`` map whose ``drift_loss`` is
    the mean across runs and whose ``pass_fail`` is the strict-majority
    vote (``None`` is preserved when the entry has no expectation). All
    other fields are taken from the first run's profile (they are not
    scalar-bearing in the gate). ``dataclasses.replace`` keeps the
    profile shape intact.
    """
    from dataclasses import replace as _replace  # noqa: PLC0415

    if not runs:
        return {}
    entry_ids = list(runs[0].keys())
    out: dict[str, LossProfile] = {}
    for entry_id in entry_ids:
        profiles = [r[entry_id] for r in runs if entry_id in r]
        if not profiles:
            continue
        mean_drift = sum(float(p.drift_loss) for p in profiles) / len(profiles)
        pass_votes = [p.pass_fail for p in profiles if p.pass_fail is not None]
        if pass_votes:
            true_count = sum(1 for v in pass_votes if v)
            majority_pass: bool | None = true_count * 2 > len(pass_votes)
        else:
            majority_pass = None
        out[entry_id] = _replace(profiles[0], drift_loss=mean_drift, pass_fail=majority_pass)
    return out


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
    round_index: int = 0,
    total_rounds: int = 0,
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
    """
    from zicato.core import assert_distinct_callables  # noqa: PLC0415

    assert_distinct_callables(config.harness_call_llm, config.auxiliary_call_llm)

    board = _stamp_disable_drift(board, disable_drift)
    if board_subset is not None:
        subset = set(board_subset)
        board = [e for e in board if e.id in subset]

    left_losses, right_losses = await _run_replicated(
        adapter=adapter,
        left_gen=left_gen,
        right_gen=right_gen,
        board=board,
        weights=weights,
        config=config,
        workspace_root=workspace_root,
        epoch_id=epoch_id,
        replicates=replicates,
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
    )


# Public surface
__all__ = [
    "TournamentResult",
    "run_fast_mode",
    "run_tournament",
    "run_matchup",
]


# ``asyncio`` is imported so type-checkers and human readers see the
# module is async-aware; the public coroutines above use ``await``
# directly and do not need to construct loops.
_ = asyncio
