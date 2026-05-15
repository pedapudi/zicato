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

The runner LAZY-imports :mod:`zicato.telemetry` per-call so the
package keeps loading cheaply even before the telemetry layer is
wired up. The two helpers we expect from telemetry are:

* ``zicato.telemetry.sink.make_run_sink_path(workspace_root, epoch_id,
  generation_id, entry_id) -> Path`` — returns the events JSONL path
  the sink should write to. Must be deterministic.
* ``zicato.telemetry.reducer.reduce_loss(events_jsonl_path, *,
  entry_id, generation_id, epoch_id, weights) -> LossProfile`` — reads
  the JSONL and produces a :class:`LossProfile`.

The adapter contract used here is intentionally narrow: a callable-
shaped object exposing ``load(snapshot_root)`` returning a "harness
session" with an async ``run(entry, sink_path)`` method that writes
the events JSONL at ``sink_path`` and returns. Concrete
:class:`HarnessAdapter` implementations conform; tests stub it.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
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


async def _run_single(
    *,
    adapter: Any,
    generation: Generation,
    entry: BoardEntry,
    weights: ScoringWeights,
    workspace_root: Path,
    epoch_id: str,
) -> LossProfile:
    """Run one entry under one generation; produce its :class:`LossProfile`.

    Sequencing (matches the spec):

    1. Build the per-run sink path via the telemetry sink helper.
    2. Load the harness via ``adapter.load(generation.snapshot_root)``.
    3. ``await session.run(entry, sink_path)`` — the session writes the
       events JSONL.
    4. ``reducer.reduce_loss(...)`` reads the JSONL and produces the
       :class:`LossProfile`. The reducer is also responsible for
       persisting ``loss.json`` next to ``events.jsonl``.
    """
    sink_module, reducer_module = _telemetry_helpers()
    sink_path = sink_module.make_run_sink_path(
        workspace_root=workspace_root,
        epoch_id=epoch_id,
        generation_id=generation.id,
        entry_id=entry.id,
    )

    session = adapter.load(generation.snapshot_root)
    await session.run(entry, sink_path)

    loss = reducer_module.reduce_loss(
        events_jsonl_path=sink_path,
        entry_id=entry.id,
        generation_id=generation.id,
        epoch_id=epoch_id,
        weights=weights,
    )
    return loss


async def _run_generation(
    *,
    adapter: Any,
    generation: Generation,
    board: list[BoardEntry],
    weights: ScoringWeights,
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
            workspace_root=workspace_root,
            epoch_id=epoch_id,
        )
        losses[entry.id] = loss
    return losses


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
) -> TournamentResult:
    """Run a full A/B tournament. See module docstring."""
    # Defense-in-depth: the runner re-checks the two-callable invariant.
    # The check happens here (and not just at config construction) so a
    # caller who hand-built a RuntimeConfig can't slip a colluding pair
    # through to the runner.
    from zicato.core import assert_distinct_callables  # noqa: PLC0415

    assert_distinct_callables(config.harness_call_llm, config.auxiliary_call_llm)

    parent_losses = await _run_generation(
        adapter=adapter,
        generation=parent_gen,
        board=board,
        weights=weights,
        workspace_root=workspace_root,
        epoch_id=epoch_id,
    )
    child_losses = await _run_generation(
        adapter=adapter,
        generation=child_gen,
        board=board,
        weights=weights,
        workspace_root=workspace_root,
        epoch_id=epoch_id,
    )

    parent_agg = aggregate_generation_score(
        list(parent_losses.values()), weights
    )
    child_agg = aggregate_generation_score(
        list(child_losses.values()), weights
    )
    outcome = evaluate_gate(parent_agg, child_agg, weights)

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
        workspace_root=workspace_root,
        epoch_id=epoch_id,
    )

    child_agg = aggregate_generation_score(
        list(child_losses.values()), weights
    )
    outcome = evaluate_gate(parent_historical_agg, child_agg, weights)

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
