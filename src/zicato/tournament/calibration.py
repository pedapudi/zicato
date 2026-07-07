"""A/A noise-floor calibration — measure what a "no change" delta looks like.

Evaluations are stochastic (agent outputs vary; judges are LLM-backed), yet
the promote gate compares two scalars against a fixed ``promote_margin``. A
margin below the evaluation's OWN noise floor promotes/rejects on noise. This
module measures that floor empirically with the oldest trick in A/B testing —
the **A/A test**: evaluate the SAME generation K times and look at the spread
of the resulting scalars. Any two draws form an A/A duel whose true effect is
exactly zero, so the observed ``delta_scalar`` spread IS the noise floor.

The measurement reuses the tournament's own board-unit machinery
(:func:`zicato.tournament.scheduling._run_board_units_fast` — one side, the
same subprocess workers, scoring, and per-unit persistence every duel uses)
so the floor is measured under EXACTLY the conditions duels run under. Each
draw is forced onto a DISTINCT replicate index: the per-unit cache is keyed
``(generation, entry, replicate_index)`` (see
:mod:`zicato.tournament.unit_cache`), so distinct indices are distinct cache
slots — a fresh noise draw per run rather than K reads of one cached result.
Re-running the audit under the same contract reuses the already-persisted
draws (cache hits), so calibration is idempotent and never wastes a run.

The floor is a RUNTIME measurement, never a contract input: it is persisted
onto the epoch record (``config.json``'s additive ``noise_floor`` field, see
:func:`zicato.epoch.lifecycle.set_epoch_noise_floor`) and read back by the
evolve-start margin check + the loop-health detector.
"""

from __future__ import annotations

import datetime as _dt
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from zicato.core import BoardEntry, Generation, RuntimeConfig, ScoringWeights

#: Default number of A/A draws. Five draws give ten pairwise deltas — enough
#: to see a floor without burning a round's worth of budget; operators
#: calibrating a very noisy harness can raise it.
DEFAULT_CALIBRATION_RUNS: int = 5

#: Replicate-index base for calibration draws. Deliberately far above any
#: index a real duel schedules (duel replicates count up from 0), so the
#: calibration's cache slots can never collide with — or pre-seed — the
#: slots a tournament will actually read.
CALIBRATION_REPLICATE_BASE: int = 1000


@dataclass(frozen=True, slots=True)
class NoiseFloor:
    """The measured A/A noise floor for one generation under one contract.

    Fields
    ------
    generation_id, epoch_id:
        The generation that duelled itself, and the epoch (contract) the
        draws ran under.
    runs:
        How many independent draws were taken (K).
    scalars:
        The K per-draw aggregate scalars, in draw order.
    max_abs_delta:
        ``max(scalars) - min(scalars)`` — the largest ``|delta_scalar|`` any
        A/A duel between two of the draws could have shown. THE measured
        floor: a ``promote_margin`` below this cannot distinguish a real
        improvement from a re-roll of the same generation.
    delta_std:
        The standard deviation of the A/A ``delta_scalar`` — i.e. of the
        difference between two independent draws: ``sqrt(2)`` times the
        population standard deviation of the draw scalars.
    measured_at:
        ISO-8601 UTC timestamp of the measurement.
    """

    generation_id: str
    epoch_id: str
    runs: int
    scalars: tuple[float, ...]
    max_abs_delta: float
    delta_std: float
    measured_at: str

    def to_json(self) -> dict[str, Any]:
        """The JSON shape persisted onto the epoch record."""
        return {
            "generation_id": self.generation_id,
            "epoch_id": self.epoch_id,
            "runs": self.runs,
            "scalars": list(self.scalars),
            "max_abs_delta": self.max_abs_delta,
            "delta_std": self.delta_std,
            "measured_at": self.measured_at,
        }


def delta_spread(scalars: list[float] | tuple[float, ...]) -> tuple[float, float]:
    """``(max_abs_delta, delta_std)`` of the A/A deltas implied by K draws.

    Pure — unit-testable with synthetic scalars. ``max_abs_delta`` is the
    range ``max - min`` (the largest ``|delta|`` any pairing of two draws
    shows); ``delta_std`` is the standard deviation of the difference of two
    independent draws, ``sqrt(2) * population_std(scalars)``. Fewer than two
    draws have no measurable spread — both values are ``0.0``.
    """
    if len(scalars) < 2:
        return 0.0, 0.0
    values = [float(s) for s in scalars]
    max_abs = max(values) - min(values)
    mean = sum(values) / len(values)
    variance = sum((v - mean) ** 2 for v in values) / len(values)
    return max_abs, math.sqrt(2.0 * variance)


async def measure_noise_floor(
    *,
    adapter: Any,
    generation: Generation,
    board: list[BoardEntry],
    weights: ScoringWeights,
    config: RuntimeConfig,
    workspace_root: Path,
    epoch_id: str,
    runs: int = DEFAULT_CALIBRATION_RUNS,
    disable_drift: tuple[Any, ...] = (),
    judge_only: bool = False,
) -> NoiseFloor:
    """Duel ``generation`` against itself ``runs`` times; measure the spread.

    Each draw evaluates the full board once through the SAME board-unit
    runner every duel uses (one side per draw — an A/A duel is two draws of
    the same generation, so K draws yield every pairwise duel at once), on a
    distinct replicate index so the per-unit cache serves a fresh sample per
    draw instead of replaying one cached result. Each draw's per-entry losses
    aggregate through the SAME :func:`aggregate_generation_score` the gate
    scores with, so the measured scalars are exactly the quantity
    ``promote_margin`` thresholds.

    A deterministic harness (e.g. the target_0 planted-defect adapter)
    measures a floor of exactly ``0.0``; a stochastic one measures the spread
    an operator's margin must clear. Returns the :class:`NoiseFloor`; the
    caller decides whether to persist it
    (:func:`zicato.epoch.lifecycle.set_epoch_noise_floor`).
    """
    from zicato.tournament.scheduling import _run_board_units_fast  # noqa: PLC0415
    from zicato.tournament.scoring import aggregate_generation_score  # noqa: PLC0415
    from zicato.tournament.worker_transport import (  # noqa: PLC0415
        _stamp_disable_drift,
        _stamp_judge_only,
        _stamp_replicate_index,
    )

    if runs < 2:
        raise ValueError(f"noise-floor calibration needs at least 2 runs, got {runs!r}")

    board = _stamp_disable_drift(board, disable_drift)
    board = _stamp_judge_only(board, judge_only)

    scalars: list[float] = []
    for draw in range(runs):
        replicate_index = CALIBRATION_REPLICATE_BASE + draw
        losses = await _run_board_units_fast(
            adapter=adapter,
            child_gen=generation,
            # Stamp the replicate index onto each entry's context, exactly
            # as the replicated-duel path does before it calls the same
            # runner: the cache key alone does not reach the harness, and a
            # seeded harness derives its noise draw from the STAMPED index
            # — without the stamp every "fresh" draw re-rolls the identical
            # seed and a stochastic harness measures a floor of 0.0.
            board=_stamp_replicate_index(board, replicate_index),
            weights=weights,
            config=config,
            workspace_root=workspace_root,
            epoch_id=epoch_id,
            match_id=f"aa-calibration:{draw}",
            # Distinct replicate index per draw ⇒ distinct cache slot ⇒ a
            # fresh sample (and an idempotent re-read on a repeated audit).
            replicate_index=replicate_index,
        )
        agg = aggregate_generation_score(list(losses.values()), weights)
        scalars.append(float(agg.get("scalar", 0.0)))

    max_abs, std = delta_spread(scalars)
    return NoiseFloor(
        generation_id=generation.id,
        epoch_id=epoch_id,
        runs=runs,
        scalars=tuple(scalars),
        max_abs_delta=max_abs,
        delta_std=std,
        measured_at=_dt.datetime.now(_dt.UTC).replace(microsecond=0).isoformat(),
    )


def margin_below_floor(promote_margin: float, floor: dict[str, Any] | None) -> bool:
    """Whether the contract's ``promote_margin`` is inside the measured noise.

    Reads a persisted :meth:`NoiseFloor.to_json` dict (tolerant: ``None`` /
    malformed ⇒ ``False``, no floor to compare against). ``True`` when the
    margin is strictly below ``max_abs_delta`` — a duel decided by the margin
    alone cannot then distinguish a real improvement from an A/A re-roll.
    """
    if not isinstance(floor, dict):
        return False
    try:
        max_abs = float(floor.get("max_abs_delta", 0.0))
    except (TypeError, ValueError):
        return False
    return promote_margin < max_abs


__all__ = [
    "CALIBRATION_REPLICATE_BASE",
    "DEFAULT_CALIBRATION_RUNS",
    "NoiseFloor",
    "delta_spread",
    "margin_below_floor",
    "measure_noise_floor",
]
