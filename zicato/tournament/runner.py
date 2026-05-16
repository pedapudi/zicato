"""Tournament runner: full A/B and fast inline keep/discard.

Two entry points:

* :func:`run_tournament` (full mode) — runs every board entry under
  BOTH parent and child generations, sequentially. Sequentiality is
  deliberate: running the same entry concurrently against two harness
  instances exposes shared mutable state (file handles, telemetry
  sinks, model-client globals) we cannot reason about per adapter.
  Within a single generation the runner still does one entry at a
  time, again because adapters routinely keep per-process state we'd
  otherwise need a contract about.

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

The runner LAZY-imports :mod:`zicato.telemetry` per-call so the
package keeps loading cheaply even before the telemetry layer is
wired up. The two helpers we expect from telemetry are:

* ``zicato.telemetry.sink.make_run_sink_path(workspace_root, epoch_id,
  generation_id, entry_id) -> Path`` — returns the events JSONL path
  the sink should write to. Must be deterministic.
* ``zicato.telemetry.reducer.reduce_loss(events_jsonl_path, entry,
  generation_id, epoch_id, expectation_result, runtime_ms,
  wall_clock_budget_exceeded, weights) -> LossProfile`` — reads
  the JSONL and produces a :class:`LossProfile`.

The session protocol used here intentionally accepts both the rich
:class:`~zicato.adapters.RunnableHarness` shape (``run(entry, sinks,
config) -> RunResult``) and the legacy stub shape (``run(entry,
sink_path) -> None``) so adapter implementations and lightweight
tests can both drive the runner. The driver inspects the session's
:meth:`run` signature at runtime and dispatches accordingly.
"""

from __future__ import annotations

import asyncio
import inspect
import time
from dataclasses import dataclass
from datetime import UTC
from pathlib import Path
from typing import Any

from zicato.core import (
    BoardEntry,
    ExpectationResult,
    Generation,
    LossProfile,
    RunResult,
    RuntimeConfig,
    ScoringWeights,
)
from zicato.tournament.gate import GateOutcome, evaluate_gate
from zicato.tournament.regression import RegressionResult, run_regression_suite
from zicato.tournament.scoring import aggregate_generation_score


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


def _telemetry_helpers() -> tuple[Any, Any]:
    """Lazily import ``zicato.telemetry.sink`` and ``.reducer``.

    Imported per-call rather than at module load so the tournament
    package keeps loading even before telemetry is wired up; the cost
    of the per-call import is negligible compared to the actual run.
    """
    from zicato.telemetry import reducer as _reducer  # noqa: PLC0415
    from zicato.telemetry import sink as _sink  # noqa: PLC0415

    return _sink, _reducer


async def _drive_session(
    *,
    session: Any,
    entry: BoardEntry,
    sink_path: Path,
    sinks: list[Any],
    config: RuntimeConfig,
) -> tuple[RunResult | None, int, bool]:
    """Drive one ``session.run`` call and return (result, runtime_ms, budget_exceeded).

    Three execution paths are accepted:

    * ``synthetic_adversarial`` / ``synthetic_clean`` entries — the
      adapter does not own the adversarial-agent zoo (see
      :mod:`zicato.synthetic`). The runner routes synthetic kinds
      directly to :func:`zicato.synthetic.run_adversarial_entry` /
      :func:`zicato.synthetic.run_clean_entry`, passing the same sink
      list the full-protocol path would build. This is what makes the
      target-2 dogfood board's adversarial recall / clean precision
      entries actually produce events.jsonl.
    * ``run(entry, sinks, config) -> RunResult`` — the full
      :class:`~zicato.adapters.RunnableHarness` shape. The caller passes
      in the pre-built ``sinks`` list (a :class:`JSONLPersistenceSink`
      aimed at ``sink_path`` plus, when configured, a live harmonograf
      sink). When the result is ``aborted`` with ``abort_reason ==
      "wall_clock_budget"`` we set the budget-exceeded flag.
    * ``run(entry, sink_path) -> None`` — legacy / stub shape used by
      tests that hand-write the events JSONL. The runner measures
      wall-clock duration itself; budget-exceeded is always ``False``
      on this path because the stub has no way to communicate an
      abort.

    The dispatch between the second and third paths is by parameter-
    name inspection rather than by ``hasattr(session, "run")``
    introspection so a test stub that deliberately mimics the legacy
    shape can do so without having to register a runtime-protocol
    marker.
    """
    started = time.monotonic()

    # Synthetic kinds bypass the adapter's session entirely.
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

    # Decide by the second positional parameter (after ``entry``).
    legacy = len(param_names) >= 2 and param_names[1] in ("sink_path", "events_path")

    if legacy:
        await session.run(entry, sink_path)
        runtime_ms = int((time.monotonic() - started) * 1000)
        return None, runtime_ms, False

    # Full-protocol path — the caller pre-built the sink list (JSONL
    # plus, when configured, a harmonograf live-stream sink).
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


def _build_sinks(
    workspace_root: Path,
    epoch_id: str,
    generation_id: str,
    entry_id: str,
) -> list[Any]:
    """Build the per-run sink list via the telemetry multi-sink builder.

    Delegates to :func:`zicato.telemetry.sink.make_run_sinks`, which
    always attaches the canonical :class:`JSONLPersistenceSink` and,
    when a harmonograf URL is configured (``ZICATO_HARMONOGRAF_URL`` env
    or the workspace ``config.json``), additionally attaches a live
    harmonograf sink. If goldfive is not installed the builder returns
    an empty list rather than raising — the adapter is free to wire its
    own telemetry capture in that case, and the reducer's JSONL fallback
    handles a missing file by producing an empty event walk.

    The workspace ``config.json`` is read best-effort so the
    ``harmonograf_url`` config key is honoured; a failure to load it
    falls back to the environment-variable-only resolution path.

    Falls back to a direct :class:`JSONLPersistenceSink` build when the
    multi-sink builder is unavailable — e.g. a lightweight test that
    swaps a minimal stub module in for ``zicato.telemetry.sink``.
    """
    workspace_config: dict[str, Any] | None = None
    try:
        from zicato import workspace_loader  # noqa: PLC0415

        workspace_config = workspace_loader.load_workspace_config(workspace_root)
    except Exception:  # noqa: BLE001 — config is optional for sink wiring
        workspace_config = None

    try:
        from zicato.telemetry.sink import make_run_sinks  # noqa: PLC0415
    except ImportError:
        # The telemetry sink module is present but does not expose the
        # multi-sink builder (a stubbed module in a unit test). Fall
        # back to the direct JSONL-only build.
        return _build_jsonl_sink_only(workspace_root, epoch_id, generation_id, entry_id)

    return make_run_sinks(
        workspace_root,
        epoch_id,
        generation_id,
        entry_id,
        workspace_config=workspace_config,
    )


def _build_jsonl_sink_only(
    workspace_root: Path,
    epoch_id: str,
    generation_id: str,
    entry_id: str,
) -> list[Any]:
    """Direct JSONL-only sink build — the no-harmonograf fallback path.

    Lazily imports :mod:`goldfive.sinks.persistence`; returns an empty
    list when goldfive is not installed rather than raising.
    """
    try:
        from goldfive.sinks.persistence import JSONLPersistenceSink  # noqa: PLC0415
    except ModuleNotFoundError:
        return []
    from zicato.core.workspace import events_jsonl_path  # noqa: PLC0415

    sink_path = events_jsonl_path(workspace_root, epoch_id, generation_id, entry_id)
    sink_path.parent.mkdir(parents=True, exist_ok=True)
    return [JSONLPersistenceSink(path=sink_path, mode="write")]


async def _evaluate_entry_expectation(
    entry: BoardEntry,
    run_result: RunResult | None,
    config: RuntimeConfig,
) -> ExpectationResult | None:
    """Evaluate ``entry.expectation`` against ``run_result`` if both present.

    Returns ``None`` when the entry has no expectation OR when the
    session shape was legacy and we therefore have no
    :class:`RunResult` to feed the matcher. The reducer handles a
    ``None`` expectation result by leaving the corresponding
    :class:`LossProfile` fields unset.
    """
    if entry.expectation is None or run_result is None:
        return None
    from zicato.board.matchers import evaluate_expectation  # noqa: PLC0415

    return await evaluate_expectation(
        entry.expectation,
        run_result,
        aux_call_llm=config.auxiliary_call_llm,
    )


def _now_iso_utc() -> str:
    from datetime import datetime  # noqa: PLC0415

    return datetime.now(UTC).isoformat()


def _run_id_for(generation: Generation, entry: BoardEntry) -> str:
    return f"{generation.id}--{entry.id}"


def _runtime_state():  # type: ignore[no-untyped-def]
    """Lazy-import runtime state helpers; return None if unavailable."""
    try:
        from zicato.runtime import state as state_mod  # noqa: PLC0415
        from zicato.runtime.state import ActiveRun  # noqa: PLC0415

        return state_mod, ActiveRun
    except ImportError:
        return None


async def _run_single(
    *,
    adapter: Any,
    generation: Generation,
    entry: BoardEntry,
    weights: ScoringWeights,
    config: RuntimeConfig,
    workspace_root: Path,
    epoch_id: str,
) -> LossProfile:
    """Run one entry under one generation; produce its :class:`LossProfile`.

    Sequencing:

    1. Build the per-run sink path via the telemetry sink helper.
    2. Load the harness via ``adapter.load(generation.snapshot_root)``.
    3. Drive ``session.run`` via :func:`_drive_session`, which handles
       the two session shapes (legacy / rich) and returns the optional
       :class:`RunResult`, runtime, and budget-exceeded flag.
    4. Evaluate the entry's expectation (if any) against the
       :class:`RunResult` using the workspace's auxiliary callable.
    5. Call :func:`reduce_loss` with the full positional contract,
       which produces and persists the :class:`LossProfile`.
    """
    sink_module, reducer_module = _telemetry_helpers()
    sink_path = sink_module.make_run_sink_path(
        workspace_root=workspace_root,
        epoch_id=epoch_id,
        generation_id=generation.id,
        entry_id=entry.id,
    )
    # Build the per-run sink list once: the canonical JSONL sink plus,
    # when configured, a live harmonograf stream sink.
    sinks = _build_sinks(workspace_root, epoch_id, generation.id, entry.id)

    # Best-effort runtime-state write so the live dashboard can render
    # the in-flight entry. Failures here MUST NOT abort the tournament.
    rt = _runtime_state()
    run_id = _run_id_for(generation, entry)
    if rt is not None:
        state_mod, ActiveRun = rt
        try:
            import os  # noqa: PLC0415
            from datetime import datetime, timedelta  # noqa: PLC0415

            now = datetime.now(UTC)
            deadline = now + timedelta(seconds=int(entry.wall_clock_budget_seconds))
            state_mod.write_active_run(
                workspace_root,
                ActiveRun(
                    run_id=run_id,
                    pid=os.getpid(),
                    started_at=now.isoformat(),
                    last_progress=now.isoformat(),
                    wall_clock_budget_seconds=int(entry.wall_clock_budget_seconds),
                    deadline=deadline.isoformat(),
                    events_jsonl_path=str(sink_path),
                    entry_id=entry.id,
                    generation_id=generation.id,
                    epoch_id=epoch_id,
                ),
            )
            state_mod.update_tournament_entry(
                workspace_root,
                entry.id,
                side=_tournament_side_for(generation, workspace_root),
                status="running",
                started_at=now.isoformat(),
            )
        except Exception:  # noqa: BLE001 - state writes are best-effort
            pass

    try:
        session = adapter.load(generation.snapshot_root)
        run_result, runtime_ms, budget_exceeded = await _drive_session(
            session=session,
            entry=entry,
            sink_path=sink_path,
            sinks=sinks,
            config=config,
        )

        expectation_result = await _evaluate_entry_expectation(entry, run_result, config)

        loss: LossProfile = reducer_module.reduce_loss(
            sink_path,
            entry,
            generation.id,
            epoch_id,
            expectation_result,
            runtime_ms,
            budget_exceeded,
            weights,
        )
        return loss
    finally:
        if rt is not None:
            state_mod, _ = rt
            try:
                state_mod.remove_active_run(workspace_root, run_id)
                state_mod.update_tournament_entry(
                    workspace_root,
                    entry.id,
                    side=_tournament_side_for(generation, workspace_root),
                    status="completed",
                    completed_at=_now_iso_utc(),
                )
            except Exception:  # noqa: BLE001
                pass


def _tournament_side_for(generation: Generation, workspace_root: Path) -> str:
    """Map a generation id to 'parent' or 'child' by consulting the
    currently-active tournament state file. Returns '' if no tournament
    is active (the dashboard falls back to neutral rendering)."""
    rt = _runtime_state()
    if rt is None:
        return ""
    state_mod, _ = rt
    try:
        tournament = state_mod.read_active_tournament(workspace_root)
    except Exception:  # noqa: BLE001
        return ""
    if tournament is None:
        return ""
    if generation.id == tournament.parent_generation_id:
        return "parent"
    if generation.id == tournament.child_generation_id:
        return "child"
    return ""


async def _run_generation(
    *,
    adapter: Any,
    generation: Generation,
    board: list[BoardEntry],
    weights: ScoringWeights,
    config: RuntimeConfig,
    workspace_root: Path,
    epoch_id: str,
) -> dict[str, LossProfile]:
    """Run all board entries under one generation, sequentially."""
    losses: dict[str, LossProfile] = {}
    for entry in board:
        loss = await _run_single(
            adapter=adapter,
            generation=generation,
            entry=entry,
            weights=weights,
            config=config,
            workspace_root=workspace_root,
            epoch_id=epoch_id,
        )
        losses[entry.id] = loss
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
    round_index: int = 0,
    total_rounds: int = 0,
) -> TournamentResult:
    """Run a full A/B tournament. See module docstring.

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
        parent_losses = await _run_generation(
            adapter=adapter,
            generation=parent_gen,
            board=board,
            weights=weights,
            config=config,
            workspace_root=workspace_root,
            epoch_id=epoch_id,
        )
        child_losses = await _run_generation(
            adapter=adapter,
            generation=child_gen,
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
    """
    from zicato.core import assert_distinct_callables  # noqa: PLC0415

    assert_distinct_callables(config.harness_call_llm, config.auxiliary_call_llm)

    child_losses = await _run_generation(
        adapter=adapter,
        generation=child_gen,
        board=board,
        weights=weights,
        config=config,
        workspace_root=workspace_root,
        epoch_id=epoch_id,
    )

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
        parent_generation_id=str(parent_historical_agg.get("generation_id", "")),
        child_generation_id=child_gen.id,
        parent_agg=parent_historical_agg,
        child_agg=child_agg,
        outcome=outcome,
        per_entry_losses={},
    )


# Public surface
__all__ = [
    "TournamentResult",
    "run_fast_mode",
    "run_tournament",
]


# ``asyncio`` is imported so type-checkers and human readers see the
# module is async-aware; the public coroutines above use ``await``
# directly and do not need to construct loops.
_ = asyncio
