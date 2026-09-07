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
are evidence for a cell. The rest are real executions with other meanings: a
noise-floor trace, a degraded probe, a veto screen, a judge meta-evaluation.
This module enumerates those too, as named :class:`MeasurementBand` draws
(:func:`measurement_band_draws_indexed`). A reader that wants "everything
that ran" therefore gets it from the same walk that decides what counts as
evidence, rather than from a second, divergent one.

Best-effort throughout: an unreadable file is skipped and a pruned or
absent run directory yields an empty list, never an error.
"""

from __future__ import annotations

import json
import math
import statistics
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from functools import cache
from typing import TYPE_CHECKING, Any

from zicato.core.loss import has_execution_evidence, validate_loss_identity
from zicato.core.measurement import (
    MEASUREMENT_RANGES,
    MeasurementPurpose,
    artifact_replicate_index,
    iter_measurement_artifacts,
    measurement_range,
    recorded_artifact_measurement,
    recorded_measurement,
    seed_qualifier,
)
from zicato.query.paths import WorkspacePaths

if TYPE_CHECKING:  # pragma: no cover - typing only
    from zicato.core.loss import LossProfile

# Which replicate-index ranges count as EVIDENCE FOR A CELL (EVAL-VIEW.md §2.1
# / §4.1). The board unit's replicate slots are reserved by purpose: real duel
# replicates count up from 0 (r0 = the canonical loss.json, plus the
# holdout-ladder confirmation re-runs, which reuse the low duel slots), the
# evidence-gate's paired draws sit at 4000+ (EVIDENCE_REPLICATE_BASE). Those
# two ranges are FRESH measurements of THIS cell, so they raise its evidence
# tier. EXCLUDED: A/A calibration at 1000+ (the champion NOISE-FLOOR trace,
# which feeds the flip badge rather than the cell), the contract pre-flight at
# 2000+, the pre-tournament candidate screen at 3000+ (an ephemeral veto
# probe), and reflection draws at 5000+ (a meta-evaluation of the judges rather
# than of the candidate). Every one of those excluded ranges IS enumerated, as
# its own measurement band: see :data:`MEASUREMENT_BANDS` below.
CELL_EVIDENCE_REPLICATE_RANGES = tuple(
    (allocation.start, allocation.stop)
    for allocation in MEASUREMENT_RANGES
    if allocation.cell_evidence
)


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

    Bounds come from the dependency-safe measurement purpose registry.
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
            start=measurement_range(MeasurementPurpose.CALIBRATION).start,
            stop=measurement_range(MeasurementPurpose.CALIBRATION).stop,
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
            start=measurement_range(MeasurementPurpose.PREFLIGHT).start,
            stop=measurement_range(MeasurementPurpose.PREFLIGHT).stop,
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
            start=measurement_range(MeasurementPurpose.SCREEN).start,
            stop=measurement_range(MeasurementPurpose.SCREEN).stop,
        ),
        MeasurementBand(
            key="board_reflection",
            label="Board reflection",
            purpose=(
                "Draws taken to evaluate the evaluation — the judges, the loss "
                "terms, the board entries themselves — rather than the generation "
                "they were drawn from."
            ),
            start=measurement_range(MeasurementPurpose.REFLECTION).start,
            stop=measurement_range(MeasurementPurpose.REFLECTION).stop,
        ),
        MeasurementBand(
            key="eval_synthesis_admission",
            label="Eval-synthesis admission probes",
            purpose=(
                "Draws that measure whether a DRAFTED board entry discriminates "
                "and how often it flips under noise, before it is admitted to the "
                "board."
            ),
            start=measurement_range(MeasurementPurpose.ADMISSION).start,
            stop=measurement_range(MeasurementPurpose.ADMISSION).stop,
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
    return artifact_replicate_index(name)


def _indexed_draws(
    paths: WorkspacePaths,
    epoch_id: str,
    generation_id: str,
    entry_id: str,
    keep: Callable[[int], bool],
) -> list[tuple[int, LossProfile, bool]]:
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
    draws: list[tuple[int, LossProfile, bool]] = []
    seen = set()
    for child in iter_measurement_artifacts(run_dir, include_aliases=True):
        if not child.is_file():
            continue
        idx = artifact_replicate_index(child.name, canonical=False)
        if idx is None or not keep(idx):
            continue
        try:
            profile = read_loss_profile(child)
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            continue
        try:
            identity = recorded_artifact_measurement(
                run_dir, child, profile.measurement, profile.match_id
            )
            validate_loss_identity(
                profile,
                epoch_id=epoch_id,
                generation_id=generation_id,
                entry_id=entry_id,
                measurement=identity,
            )
        except ValueError:
            draws.append((idx, profile, False))
            continue
        if identity in seen:
            continue
        seen.add(identity)
        draws.append((idx, replace(profile, measurement=identity), True))
    draws.sort(
        key=lambda pair: (
            pair[0],
            seed_qualifier(pair[1].measurement.base_seed)
            if pair[1].measurement is not None
            else "",
            pair[1].run_id,
        )
    )
    return draws


AMBIGUOUS_BAND = MeasurementBand(
    key="ambiguous",
    label="Ambiguous measurement provenance",
    purpose="The record does not establish its measurement purpose or actual execution. "
    "These records remain visible but supply no reusable evidence.",
    start=0,
    stop=0,
)


def _eligible_identity(index: int, profile: LossProfile) -> bool:
    try:
        recorded_measurement(index, measurement=profile.measurement, match_id=profile.match_id)
    except ValueError:
        return False
    return has_execution_evidence(profile)


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
    return [
        (index, profile)
        for index, profile, valid in _indexed_draws(
            paths,
            epoch_id,
            generation_id,
            entry_id,
            lambda idx: any(lo <= idx < hi for lo, hi in CELL_EVIDENCE_REPLICATE_RANGES),
        )
        if valid and _eligible_identity(index, profile)
    ]


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
    out: list[tuple[int, MeasurementBand, LossProfile]] = []
    for index, profile, valid in _indexed_draws(
        paths, epoch_id, generation_id, entry_id, lambda _: True
    ):
        band = band_of(index)
        if band != UNCLAIMED_BAND and not (valid and _eligible_identity(index, profile)):
            band = AMBIGUOUS_BAND
        if band is not None:
            out.append((index, band, profile))
    return out


def cell_replicate_draws(
    paths: WorkspacePaths, epoch_id: str, generation_id: str, entry_id: str
) -> list[LossProfile]:
    """Describe the cell's draws for the seed recorded by its generation score.

    A score without seed provenance selects historical unknown-seed records.
    All-seed audit enumeration remains available through
    :func:`cell_replicate_draws_indexed`; descriptive visibility does not admit
    a draw to a selected-seed decision.
    """
    from zicato.workspace import WorkspaceLayout  # noqa: PLC0415
    from zicato.workspace.reads import generation_base_seed  # noqa: PLC0415

    try:
        selected_seed = generation_base_seed(
            WorkspaceLayout.from_root(paths.root), epoch_id, generation_id
        )
    except ValueError:
        return []
    indexed = cell_replicate_draws_indexed(paths, epoch_id, generation_id, entry_id)
    return [
        profile
        for _, profile in indexed
        if profile.measurement is not None and profile.measurement.base_seed == selected_seed
    ]


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
    so this is the spread of the *mean* of the draws rather than of one draw. ``None``
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
