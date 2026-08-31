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
from collections.abc import Callable
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
#: slots a tournament will actually read. This is the second row of the
#: reserved-base ledger (dev-guide ``04-evaluation-statistics.md §8.1``): real
#: duel replicates count up from 0, THIS calibration at 1000, the contract
#: pre-flight across 2000..2999
#: (:data:`zicato.epoch.preflight.PREFLIGHT_REPLICATE_BASE` +
#: :data:`~zicato.epoch.preflight.PREFLIGHT_REPLICATE_SPAN`), the candidate
#: screen at 3000 (:data:`zicato.epoch.screen.SCREEN_REPLICATE_BASE`), the
#: evidence gate at 4000
#: (:data:`zicato.selection.evidence_gate.EVIDENCE_REPLICATE_BASE`), board
#: reflection at 5000 (:data:`zicato.reflection.corpus.REFLECTION_REPLICATE_BASE`),
#: and eval-synthesis admission at 6000
#: (:data:`zicato.reflection.admission.SYNTHESIS_REPLICATE_BASE`).
CALIBRATION_REPLICATE_BASE: int = 1000

#: Width of the block :data:`CALIBRATION_REPLICATE_BASE` opens. Draw ``j``
#: caches at ``CALIBRATION_REPLICATE_BASE + j``, so a run count above this span
#: would walk into the contract pre-flight's block and its DEGRADED probes
#: would be indistinguishable from clean A/A draws in either direction.
#: :func:`measure_noise_floor` refuses rather than overlap — the mirror of the
#: pre-flight's own probe-sample guard. It is also the span every reader of the
#: calibration band tests against (:func:`zicato.tournament.unit_cache
#: .is_own_code_board_draw`) instead of a bare literal.
CALIBRATION_REPLICATE_SPAN: int = 1000

#: The heartbeat ``phase`` segment that names a calibration in flight. The
#: calibration is an epoch-open step running BEFORE the round it precedes has
#: proposed anything, so it must not inherit the round's phase: a workspace
#: stamped ``evolve_once:round_0`` with no active tournament has the same
#: shape as a WEDGED round, so a working calibration would otherwise read as
#: a hang (issue #175). Readers match this token as a phase segment
#: (:func:`zicato.query.loop_view._project_pipeline`) rather than the whole
#: string, because the loop appends live ``done/total`` draw progress to it.
CALIBRATION_PHASE_TOKEN: str = "calibrating_noise_floor"

#: The full phase the evolve loop stamps for the duration of the calibration.
#: The per-draw progress suffix (``:7/18``) is appended by the loop's draw
#: callback; no segment here is an idle token, so a calibrating workspace
#: reads ACTIVE (:func:`zicato.query.runtime_view.is_active_phase`).
CALIBRATION_PHASE: str = f"evolve_once:{CALIBRATION_PHASE_TOKEN}"

#: How many noise standard deviations a recommended ``promote_margin`` sits
#: above zero. The gate compares two aggregate scalars, so the quantity that
#: must be cleared is the standard deviation of their DIFFERENCE
#: (:attr:`NoiseFloor.delta_std`); at 2.5 sigma an A/A pair clears the margin
#: by chance ~1.2% of the time (two-sided). The same 2.5 the reflection tier
#: has always recommended — applied to a draw-count-stable statistic instead
#: of the range (see :func:`recommended_promote_margin`).
MARGIN_NOISE_MULTIPLE: float = 2.5


class NoiseFloorInconclusive(RuntimeError):
    """An A/A noise-floor draw hit an INFRA abort — the measurement is void.

    An infra abort (worker crash, endpoint outage) is NOT a measurement of the
    generation, so folding its worst-case not-completed scalar into a floor
    would let a transient outage poison the epoch — and, under a hard
    pre-flight gate, falsely disqualify a contract that an outage merely made
    un-measurable. Callers that opt into strict measurement
    (``raise_on_infra_abort=True``) treat this as a best-effort skip: persist
    nothing and re-measure on a later round once the endpoint is healthy. See
    :func:`zicato.core.loss.is_infra_abort_cause`.
    """


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
        floor for the "is my margin inside the noise?" comparison: a
        ``promote_margin`` below this cannot distinguish a real improvement
        from a re-roll of the same generation. A **range** statistic, so it
        grows with K — never base a margin RECOMMENDATION on it
        (:func:`recommended_promote_margin`).
    delta_std:
        The standard deviation of the A/A ``delta_scalar`` — i.e. of the
        difference between two independent draws: ``sqrt(2)`` times the
        population standard deviation of the draw scalars. Unlike
        :attr:`max_abs_delta` this does NOT drift with K, which is why it —
        not the range — is what a margin recommendation scales.
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
    # ``math.fsum``: the noise floor this returns is compared against a
    # contract margin, so it must not shift with the interpreter version.
    mean = math.fsum(values) / len(values)
    variance = math.fsum((v - mean) ** 2 for v in values) / len(values)
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
    raise_on_infra_abort: bool = False,
    on_draw: Callable[[int, int], None] | None = None,
) -> NoiseFloor:
    """Duel ``generation`` against itself ``runs`` times; measure the spread.

    Each draw evaluates the full board once through the SAME board-unit
    runner every duel uses (one side per draw — an A/A duel is two draws of
    the same generation, so K draws yield every pairwise duel at once), on a
    distinct replicate index so the per-unit cache serves a fresh sample per
    draw instead of replaying one cached result. Each draw's per-entry losses
    aggregate through the SAME :func:`aggregate_generation_score` the gate
    scores with, so the measured scalars are the quantity ``promote_margin``
    thresholds.

    A deterministic harness (e.g. the target_0 planted-defect adapter)
    measures a floor of exactly ``0.0``; a stochastic one measures the spread
    an operator's margin must clear. Returns the :class:`NoiseFloor`; the
    caller decides whether to persist it
    (:func:`zicato.epoch.lifecycle.set_epoch_noise_floor`).

    ``raise_on_infra_abort`` (default ``False``): when set, a draw that hits an
    infra abort (:func:`~zicato.core.loss.is_infra_abort_cause` — a worker
    crash / endpoint outage, never a genuine budget exhaustion) VOIDS the whole
    measurement with :class:`NoiseFloorInconclusive` instead of folding the
    outage's worst-case not-completed scalar into the floor. The default-on
    contract pre-flight opts in (an outage must never disqualify a contract);
    the tolerant default preserves the ``zicato board audit`` surface.

    ``on_draw`` (default ``None`` — no behaviour change for callers that do not
    pass one) is called ``(draws_completed, runs)`` after each draw settles, so
    a long serial measurement can report progress live rather than looking like
    a hang. It is strictly an observability hook: it runs inside the draw loop,
    so it must be cheap and must not raise.
    """
    from zicato.core.loss import is_infra_abort_cause  # noqa: PLC0415
    from zicato.tournament.scheduling import _run_board_units_fast  # noqa: PLC0415
    from zicato.tournament.scoring import aggregate_generation_score  # noqa: PLC0415
    from zicato.tournament.worker_transport import (  # noqa: PLC0415
        _stamp_disable_drift,
        _stamp_judge_only,
        _stamp_replicate_index,
    )

    if runs < 2:
        raise ValueError(f"noise-floor calibration needs at least 2 runs, got {runs!r}")
    if runs > CALIBRATION_REPLICATE_SPAN:
        # Draw j caches at CALIBRATION_REPLICATE_BASE + j, so a wider run count
        # would squat the contract pre-flight's block: a later `board preflight`
        # would read these clean A/A draws as its own cached degraded probes,
        # and every reader of the calibration band would read the pre-flight's
        # degraded probes as champion behaviour. Refuse rather than overlap.
        block_end = CALIBRATION_REPLICATE_BASE + CALIBRATION_REPLICATE_SPAN - 1
        raise ValueError(
            f"noise-floor calibration: {runs} draws exceed the reserved replicate "
            f"block of {CALIBRATION_REPLICATE_SPAN} "
            f"({CALIBRATION_REPLICATE_BASE}..{block_end}); lower --runs "
            '(or the "contract_preflight" run count in config.json)'
        )

    board = _stamp_disable_drift(board, disable_drift)
    board = _stamp_judge_only(board, judge_only)

    scalars: list[float] = []
    for draw in range(runs):
        replicate_index = CALIBRATION_REPLICATE_BASE + draw
        losses = await _run_board_units_fast(
            adapter=adapter,
            child_gen=generation,
            # Stamp the replicate index onto each entry's context, as the
            # replicated-duel path does before it calls the same
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
        # An infra abort (endpoint outage, worker crash) is not a measurement
        # of the generation — folding its worst-case not-completed scalar into
        # the floor would let a transient outage poison the epoch. A strict
        # consumer (the default-on pre-flight) opts to void the whole
        # measurement instead of persisting an outage-derived floor.
        if raise_on_infra_abort and any(
            is_infra_abort_cause(getattr(lp, "abort_cause", None)) for lp in losses.values()
        ):
            raise NoiseFloorInconclusive(
                f"A/A noise-floor draw {draw} hit an infra abort (endpoint outage / "
                "worker crash); the measurement is inconclusive and must not be "
                "persisted — an outage must never disqualify a contract."
            )
        agg = aggregate_generation_score(list(losses.values()), weights)
        scalars.append(float(agg.get("scalar", 0.0)))
        if on_draw is not None:
            # Reported AFTER the draw settles, so the count is draws COMPLETED
            # — never draws started. The caller stamps the initial 0/K itself.
            on_draw(draw + 1, runs)

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


def recommended_promote_margin(
    *,
    scalars: list[float] | tuple[float, ...] | None = None,
    delta_std: float | None = None,
    max_abs_delta: float | None = None,
    multiple: float = MARGIN_NOISE_MULTIPLE,
) -> float:
    """A ``promote_margin`` recommendation that does not drift with K. Pure.

    Recommending ``multiple * max_abs_delta`` would drift with K, because
    ``max_abs_delta`` is a **range** statistic: the expected range of K draws
    from a fixed distribution grows without bound in K. On an UNCHANGED board,
    raising the calibration draw count therefore raises the recommended margin
    — and pushes it toward (issue #112: past) the largest improvement the loop
    can actually produce, which is the one place a margin must never go. The
    recommendation degrading as the measurement improves is backwards.

    ``delta_std`` — already computed and persisted alongside the range by
    :func:`delta_spread` — is the standard deviation of the A/A
    ``delta_scalar``, i.e. of exactly the difference the promote gate
    thresholds. It is a consistent estimator: more draws sharpen it rather
    than inflate it. The recommendation is ``multiple * delta_std``
    (:data:`MARGIN_NOISE_MULTIPLE` sigma).

    Supply the dispersion in whichever form the caller has:

    * ``scalars`` — K raw draw scalars; ``delta_std`` is derived via
      :func:`delta_spread`.
    * ``delta_std`` — the persisted statistic, straight from a
      :meth:`NoiseFloor.to_json` record.
    * ``max_abs_delta`` — the RANGE, used only as a degraded fallback when no
      ``delta_std`` is available (a hand-written floor record that predates
      the field). A real measurement with a positive range always has a
      positive ``delta_std``, so this branch never fires on measured data.

    Returns ``0.0`` when no dispersion is available at all: an unmeasured
    floor recommends nothing.
    """
    if scalars is not None:
        _, derived_std = delta_spread(scalars)
        if derived_std > 0.0:
            return multiple * derived_std
        if max_abs_delta is None:
            # A zero-spread measurement (a deterministic harness) recommends
            # nothing — there is no noise for a margin to clear.
            return 0.0
    if delta_std is not None and delta_std > 0.0:
        return multiple * float(delta_std)
    if max_abs_delta is not None and max_abs_delta > 0.0:
        return multiple * float(max_abs_delta)
    return 0.0


def recommended_promote_margin_from_floor(floor: dict[str, Any] | None) -> float | None:
    """:func:`recommended_promote_margin` from a persisted floor record.

    Reads a :meth:`NoiseFloor.to_json` dict, preferring the draw-count-stable
    ``delta_std`` and falling back to ``max_abs_delta`` only when the record
    carries no usable std. Tolerant like :func:`margin_below_floor`: ``None`` /
    malformed / all-zero dispersion yields ``None`` (nothing to recommend).
    """
    if not isinstance(floor, dict):
        return None

    def _num(key: str) -> float | None:
        try:
            return float(floor[key])
        except (KeyError, TypeError, ValueError):
            return None

    recommended = recommended_promote_margin(
        delta_std=_num("delta_std"),
        max_abs_delta=_num("max_abs_delta"),
    )
    return recommended if recommended > 0.0 else None


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
    "CALIBRATION_PHASE",
    "CALIBRATION_PHASE_TOKEN",
    "CALIBRATION_REPLICATE_BASE",
    "DEFAULT_CALIBRATION_RUNS",
    "MARGIN_NOISE_MULTIPLE",
    "NoiseFloor",
    "delta_spread",
    "margin_below_floor",
    "measure_noise_floor",
    "recommended_promote_margin",
    "recommended_promote_margin_from_floor",
]
