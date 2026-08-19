"""Per-replicate evidence for ONE ``(generation, board entry)`` cell.

A board entry can be executed several times against the same candidate: the
worker writes ``loss.json`` for the first draw and a sibling
``loss.r<N>.json`` for each further one. Every reader that needs
replicate-level precision on a cell enumerates the SAME files through this
module, so "what counts as a draw for this cell" has one definition:

* the eval matrix's replicate count / evidence tier
  (:func:`zicato.query.eval_view.build_eval_matrix`), and
* the matchup grid's per-entry score standard error
  (:func:`zicato.query.tournament_view.build_matchup_grid`).

The replicate namespace is partitioned by OWNER, and only two of its ranges
are evidence for a cell. The rest are real executions with other meanings —
a noise-floor trace, a degraded probe, a veto screen, a judge
meta-evaluation — and this module enumerates those too, as named
:class:`MeasurementBand` draws (:func:`measurement_band_draws_indexed`), so
a reader that wants "everything that ran" gets it from the same walk that
decides what counts as evidence rather than from a second, divergent one.

Best-effort throughout (DQ3): an unreadable file is skipped and a pruned or
absent run directory yields an empty list, never an error.
"""

from __future__ import annotations

import json
import math
import statistics
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from functools import cache
from typing import TYPE_CHECKING, Any

from zicato.query.paths import WorkspacePaths

if TYPE_CHECKING:  # pragma: no cover - typing only
    from zicato.core.loss import LossProfile

# Which replicate-index ranges count as EVIDENCE FOR A CELL (EVAL-VIEW.md §2.1 /
# §4.1). The board unit's replicate slots are reserved by purpose: real duel
# replicates count up from 0 (r0 = the canonical loss.json, plus the
# holdout-ladder confirmation re-runs, which reuse the low duel slots), the
# evidence-gate's paired draws sit at 4000+ (EVIDENCE_REPLICATE_BASE). Those two
# ranges are FRESH measurements of THIS cell, so they raise its evidence tier.
# EXCLUDED: A/A calibration at 1000+ (that is the champion NOISE-FLOOR trace — it
# feeds the flip badge, not the cell), the contract pre-flight at 2000+, the
# pre-tournament candidate screen at 3000+ (an ephemeral veto probe), and
# reflection draws at 5000+ (a meta-evaluation of the judges, not the candidate).
# Every one of those excluded ranges IS enumerated, as its own measurement band:
# see :data:`MEASUREMENT_BANDS` below.
CELL_EVIDENCE_REPLICATE_RANGES: tuple[tuple[int, int], ...] = ((0, 1000), (4000, 5000))


#: The width the reserved-base ledger gives each owner (calibration and the
#: contract pre-flight both declare spans of exactly this). Used for the last
#: band, whose owner declares no span constant and has no successor to bound
#: it — never as a substitute for a span an owner does declare.
RESERVED_BAND_WIDTH: int = 1000


@dataclass(frozen=True, slots=True)
class MeasurementBand:
    """One reserved replicate range, and what the draws inside it measure.

    ``key`` is the stable machine name a surface renders and a test asserts
    on; ``label`` and ``purpose`` are the reader-facing text, written so a
    band is self-describing wherever it is shown — nothing about a
    deliberately-degraded probe may depend on the reader having opened its
    parent. The range is half-open: ``start <= index < stop``.
    """

    key: str
    label: str
    purpose: str
    start: int
    stop: int

    def holds(self, index: int) -> bool:
        """Whether this band claims ``index``."""
        return self.start <= index < self.stop


#: The band an index lands in when NO owner claims it. The reserved-base
#: ledger is an ALLOW-LIST (:func:`zicato.tournament.unit_cache
#: .is_own_code_board_draw`), so an index outside every claimed range must
#: stay VISIBLE as unclaimed rather than be admitted as the generation's own
#: board evidence — the mistake that would let an unknown writer's draws be
#: read as champion behaviour.
UNCLAIMED_BAND: MeasurementBand = MeasurementBand(
    key="unclaimed",
    label="Unclaimed replicate band",
    purpose=(
        "Draws at replicate indices no owner in the reserved-base ledger claims. "
        "What produced them is unknown, so they are shown as unclaimed and are "
        "never counted as this generation's own board evidence."
    ),
    # Claims nothing by range; :func:`band_of` returns it as the fallback.
    start=0,
    stop=0,
)


@cache
def measurement_bands() -> tuple[MeasurementBand, ...]:
    """The reserved replicate bands that are NOT cell evidence, ascending.

    The bounds are LITERAL MIRRORS of the owning modules' constants
    (``CALIBRATION_REPLICATE_BASE``/``_SPAN``, ``PREFLIGHT_REPLICATE_BASE``/
    ``_SPAN``, ``SCREEN_REPLICATE_BASE``, ``EVIDENCE_REPLICATE_BASE``,
    ``REFLECTION_REPLICATE_BASE``, ``SYNTHESIS_REPLICATE_BASE``), not
    imports: the owners pull in the pre-flight, screening, and reflection
    machinery, and reflection reaches the dashboard — chains the query
    layer's import contracts forbid. The correspondence test in
    ``tests/test_query_execution_plan.py`` imports both sides and pins the
    mirror to the owners, so a moved base fails a test instead of drifting.
    The ledger itself is described at
    :func:`zicato.tournament.unit_cache.is_own_code_board_draw`.
    """

    return (
        MeasurementBand(
            key="calibration",
            label="A/A noise-floor calibration",
            purpose=(
                "Repeated draws of this generation's own code over the frozen "
                "board. Every pair is a duel whose true effect is zero, so their "
                "spread is the evaluation's noise floor — not evidence about any "
                "candidate."
            ),
            start=1000,
            stop=2000,
        ),
        MeasurementBand(
            key="contract_preflight",
            label="Contract pre-flight — deliberately degraded probes",
            purpose=(
                "Draws of DELIBERATELY DEGRADED copies of this generation's code, "
                "cached under the generation's own id. Each probe blanks or "
                "scrambles one mutation point to test whether the board can "
                "out-signal its own noise. A failure here is the probe working as "
                "designed and says NOTHING about what this generation does."
            ),
            start=2000,
            stop=3000,
        ),
        MeasurementBand(
            key="candidate_screen",
            label="Candidate screen",
            purpose=(
                "A proposed candidate's pre-tournament veto probe, run over a "
                "rotating TRAIN panel subset rather than the frozen board, from an "
                "ephemeral snapshot that never entered the lineage. It "
                "disqualifies; it never ranks, and its scalar is never evidence."
            ),
            start=3000,
            stop=4000,
        ),
        MeasurementBand(
            key="board_reflection",
            label="Board reflection",
            purpose=(
                "Draws taken to evaluate the evaluation — the judges, the loss "
                "terms, the board entries themselves — rather than the generation "
                "they were drawn from."
            ),
            start=5000,
            stop=6000,
        ),
        MeasurementBand(
            key="eval_synthesis_admission",
            label="Eval-synthesis admission probes",
            purpose=(
                "Draws that measure whether a DRAFTED board entry discriminates "
                "and how often it flips under noise, before it is admitted to the "
                "board."
            ),
            start=6000,
            stop=7000,
        ),
    )


def band_of(index: int) -> MeasurementBand | None:
    """The measurement band ``index`` belongs to.

    ``None`` for an index inside :data:`CELL_EVIDENCE_REPLICATE_RANGES` — that
    is a work unit, enumerated by :func:`cell_replicate_draws_indexed` and
    never a band draw. Anything else that no band claims is
    :data:`UNCLAIMED_BAND`.
    """
    if any(lo <= index < hi for lo, hi in CELL_EVIDENCE_REPLICATE_RANGES):
        return None
    for band in measurement_bands():
        if band.holds(index):
            return band
    return UNCLAIMED_BAND


def replicate_index(name: str) -> int | None:
    """The replicate index of a ``loss.json`` / ``loss.r<N>.json`` file, else ``None``.

    ``loss.json`` is replicate 0 (the canonical worker output); ``loss.r<N>.json``
    is replicate ``N`` (the sibling slot the worker writes). Any other filename is
    not a replicate loss file.
    """
    if name == "loss.json":
        return 0
    if name.startswith("loss.r") and name.endswith(".json"):
        mid = name[len("loss.r") : -len(".json")]
        if mid.isdigit():
            return int(mid)
    return None


def _indexed_draws(
    paths: WorkspacePaths,
    epoch_id: str,
    generation_id: str,
    entry_id: str,
    keep: Callable[[int], bool],
) -> list[tuple[int, LossProfile]]:
    """The persisted replicate slots of ONE cell that ``keep`` admits, ascending.

    THE walk of a run directory. Both the cell-evidence enumeration and the
    measurement-band enumeration run through it with different admission
    rules, so the two can partition the same files instead of drifting apart
    over which ones exist.

    An ATTEMPT sibling (``loss.a3.json`` / ``loss.r2.a3.json``) is excluded by
    construction: :func:`replicate_index` returns ``None`` for a name whose
    replicate part is not a bare number, so a superseded execution can never
    reach either caller as a draw.
    """
    from zicato.core.workspace import loss_profile_path  # noqa: PLC0415
    from zicato.telemetry.reducer import read_loss_profile  # noqa: PLC0415

    run_dir = loss_profile_path(paths.root, epoch_id, generation_id, entry_id).parent
    if not run_dir.is_dir():
        return []
    draws: list[tuple[int, LossProfile]] = []
    for child in sorted(run_dir.iterdir()):
        if not child.is_file():
            continue
        idx = replicate_index(child.name)
        if idx is None or not keep(idx):
            continue
        try:
            profile = read_loss_profile(child)
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            continue
        draws.append((idx, profile))
    draws.sort(key=lambda pair: pair[0])
    return draws


def cell_replicate_draws_indexed(
    paths: WorkspacePaths, epoch_id: str, generation_id: str, entry_id: str
) -> list[tuple[int, LossProfile]]:
    """The qualifying draws for ONE cell as ``(replicate index, profile)``, ascending.

    THE enumeration; :func:`cell_replicate_draws` is this without the
    indices. A reader that must NAME a draw (the execution plan renders one
    node per replicate) needs the index the filename carries, and deriving
    it a second time is how two surfaces start disagreeing about which
    files count.
    """
    return _indexed_draws(
        paths,
        epoch_id,
        generation_id,
        entry_id,
        lambda idx: any(lo <= idx < hi for lo, hi in CELL_EVIDENCE_REPLICATE_RANGES),
    )


def measurement_band_draws_indexed(
    paths: WorkspacePaths, epoch_id: str, generation_id: str, entry_id: str
) -> list[tuple[int, MeasurementBand, LossProfile]]:
    """Every NON-evidence draw of ONE cell as ``(index, band, profile)``, ascending.

    The exact complement of :func:`cell_replicate_draws_indexed` over the same
    run directory: together the two account for every persisted replicate slot
    the cell holds, so a draw cannot be dropped by both. These draws are real
    executions with other meanings — they are never a cell's evidence, and
    each one carries the band that says what it measured.
    """
    indexed = _indexed_draws(
        paths,
        epoch_id,
        generation_id,
        entry_id,
        lambda idx: not any(lo <= idx < hi for lo, hi in CELL_EVIDENCE_REPLICATE_RANGES),
    )
    out: list[tuple[int, MeasurementBand, LossProfile]] = []
    for idx, profile in indexed:
        band = band_of(idx)
        if band is not None:
            out.append((idx, band, profile))
    return out


def cell_replicate_draws(
    paths: WorkspacePaths, epoch_id: str, generation_id: str, entry_id: str
) -> list[LossProfile]:
    """The qualifying per-replicate loss profiles for ONE cell, in index order.

    The DURABLE evidence source for a cell's replicate count: the loss files that
    actually exist under the run directory, filtered to
    :data:`CELL_EVIDENCE_REPLICATE_RANGES`. This is not the ``loss_profiles``
    index row count, which is always 1 (that table's key is ``run_id`` = one row
    per ``(generation, entry)``).
    """
    indexed = cell_replicate_draws_indexed(paths, epoch_id, generation_id, entry_id)
    return [profile for _, profile in indexed]


def _finite_score(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    f = float(value)
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return f


def replicate_scores(
    paths: WorkspacePaths, epoch_id: str, generation_id: str, entry_id: str
) -> list[float]:
    """The continuous ``score`` of each qualifying replicate draw for one cell.

    A draw whose profile predates the continuous outcome (``score is None``) or
    carries a non-finite value contributes nothing, so a bool-only board yields
    ``[]`` rather than a fabricated 0.0.
    """
    scores: list[float] = []
    for profile in cell_replicate_draws(paths, epoch_id, generation_id, entry_id):
        value = _finite_score(getattr(profile, "score", None))
        if value is not None:
            scores.append(value)
    return scores


def standard_error(values: Sequence[float]) -> float | None:
    """Standard error of the mean over ``values``: sample sd / sqrt(n).

    Uses the SAMPLE standard deviation (Bessel-corrected, ``n - 1`` denominator),
    so this is the spread of the *mean* of the draws, not of one draw. ``None``
    for fewer than two values: a single draw measures no spread and must render
    as "unavailable", never as ``±0.000``.
    """
    if len(values) < 2:
        return None
    try:
        sd = statistics.stdev(values)
    except statistics.StatisticsError:
        return None
    if sd != sd or sd in (float("inf"), float("-inf")):
        return None
    return sd / math.sqrt(len(values))
