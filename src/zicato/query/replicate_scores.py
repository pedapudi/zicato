"""Read the measurements of a candidate on one board entry.

Tournament and confirmation draws supply candidate evidence. Calibration,
preflight, screening, reflection, and admission remain separately visible.
Every reader uses the same path and record validation. Conflicting records
remain visible for diagnosis and cannot supply reusable evidence."""

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
    MeasurementDraw,
    artifact_measurement,
    iter_measurement_artifacts,
    recorded_artifact_measurement,
    recorded_measurement,
    seed_qualifier,
)
from zicato.query.paths import WorkspacePaths

if TYPE_CHECKING:  # pragma: no cover - typing only
    from zicato.core.loss import LossProfile


@dataclass(frozen=True, slots=True)
class MeasurementBand:
    """The display name and explanation of a measurement purpose."""

    key: str
    label: str
    purpose: str

    def holds(self, index: MeasurementDraw) -> bool:
        """Whether this band claims ``index``."""
        return self.key == index.purpose


@cache
def measurement_bands() -> tuple[MeasurementBand, ...]:
    """Descriptions of the purposes that do not supply tournament evidence."""

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
        ),
        MeasurementBand(
            key="board_reflection",
            label="Board reflection",
            purpose=(
                "Draws taken to evaluate the evaluation — the judges, the loss "
                "terms, the board entries themselves — rather than the generation "
                "they were drawn from."
            ),
        ),
        MeasurementBand(
            key="eval_synthesis_admission",
            label="Eval-synthesis admission probes",
            purpose=(
                "Draws that measure whether a DRAFTED board entry discriminates "
                "and how often it flips under noise, before it is admitted to the "
                "board."
            ),
        ),
    )


def band_of(index: MeasurementDraw) -> MeasurementBand | None:
    """Return the purpose description, or None for tournament evidence."""
    if index.cell_evidence:
        return None
    for band in measurement_bands():
        if band.holds(index):
            return band
    raise ValueError(f"measurement purpose has no description: {index.purpose}")


def measurement(name: str) -> MeasurementDraw | None:
    """Read purpose and draw from a supported loss filename."""
    return artifact_measurement(name)


def _indexed_draws(
    paths: WorkspacePaths,
    epoch_id: str,
    generation_id: str,
    entry_id: str,
    keep: Callable[[MeasurementDraw], bool],
) -> list[tuple[MeasurementDraw, LossProfile, bool]]:
    """Read selected draws and indicate whether each record agrees with its path."""
    from zicato.core.workspace import loss_profile_path  # noqa: PLC0415
    from zicato.telemetry.reducer import read_loss_profile  # noqa: PLC0415

    run_dir = loss_profile_path(paths.root, epoch_id, generation_id, entry_id).parent.parent
    if not run_dir.is_dir():
        return []
    draws: list[tuple[MeasurementDraw, LossProfile, bool]] = []
    seen = set()
    for child in iter_measurement_artifacts(run_dir):
        if not child.is_file():
            continue
        idx = artifact_measurement(child.name)
        if idx is None or not keep(idx):
            continue
        try:
            profile = read_loss_profile(child)
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            continue
        try:
            identity = recorded_artifact_measurement(run_dir, child, profile.measurement)
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
)


def _eligible_identity(index: MeasurementDraw, profile: LossProfile) -> bool:
    try:
        recorded_measurement(index, measurement=profile.measurement)
    except ValueError:
        return False
    return has_execution_evidence(profile)


def cell_replicate_draws_indexed(
    paths: WorkspacePaths, epoch_id: str, generation_id: str, entry_id: str
) -> list[tuple[MeasurementDraw, LossProfile]]:
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
            lambda measurement: measurement.cell_evidence,
        )
        if valid and _eligible_identity(index, profile)
    ]


def measurement_band_draws_indexed(
    paths: WorkspacePaths, epoch_id: str, generation_id: str, entry_id: str
) -> list[tuple[MeasurementDraw, MeasurementBand, LossProfile]]:
    """Every NON-evidence draw of ONE cell as ``(index, band, profile)``, ascending.

    The exact complement of :func:`cell_replicate_draws_indexed` over the same
    run directory: together the two account for every persisted replicate slot
    the cell holds, so a draw cannot be dropped by both. These draws are real
    executions with other meanings — they are never a cell's evidence, and
    each one carries the band that says what it measured.
    """
    out: list[tuple[MeasurementDraw, MeasurementBand, LossProfile]] = []
    for index, profile, valid in _indexed_draws(
        paths, epoch_id, generation_id, entry_id, lambda _: True
    ):
        band = band_of(index)
        if not (valid and _eligible_identity(index, profile)):
            band = AMBIGUOUS_BAND
        if band is not None:
            out.append((index, band, profile))
    return out


def cell_replicate_draws(
    paths: WorkspacePaths, epoch_id: str, generation_id: str, entry_id: str
) -> list[LossProfile]:
    """Read tournament evidence for the seed selected by the generation score."""
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


def selected_measurements(
    paths: WorkspacePaths, epoch_id: str, generation_id: str
) -> dict[str, dict[str, Any]]:
    """Read selected measurements once for candidate and task comparisons."""
    from zicato.query.paths import coerce_float, finite_float, layout_of
    from zicato.workspace.reads import read_generation_losses

    rows = {}
    for entry_id, loss in read_generation_losses(layout_of(paths), epoch_id, generation_id).items():
        drift = coerce_float(loss.get("drift_loss"))
        raw_metrics = loss.get("metrics")
        metrics = {
            str(name): value
            for name, raw in (raw_metrics.items() if isinstance(raw_metrics, dict) else ())
            if (value := finite_float(raw)) is not None
        }
        rows[entry_id] = {
            **loss,
            "drift_loss": drift,
            "score": finite_float(loss.get("score")),
            "metrics": metrics or None,
            "drift_observed": drift not in (None, 0.0)
            or any(metric["name"].startswith("drift:") for metric in loss.get("metric_counts", [])),
        }
    return rows
