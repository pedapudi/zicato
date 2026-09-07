"""Measurement purpose, draw identity, and the integer storage encoding."""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from enum import Enum, StrEnum
from pathlib import Path
from typing import Any


class _UnknownSeed(Enum):
    VALUE = 0


UNKNOWN_SEED = _UnknownSeed.VALUE
type BaseSeed = int | None | _UnknownSeed


def seed_qualifier(base_seed: BaseSeed) -> str:
    """Encode recorded seed provenance; missing provenance has no qualifier."""
    if base_seed is UNKNOWN_SEED:
        return ""
    if base_seed is None:
        return "seed-none"
    if type(base_seed) is not int:
        raise ValueError("measurement base seed must be an integer or null")
    return f"seed-{base_seed}"


def _seed_from_qualifier(value: str) -> BaseSeed:
    if value == "seed-none":
        return None
    if value.startswith("seed-"):
        try:
            seed = int(value[5:])
        except ValueError:
            pass
        else:
            if seed_qualifier(seed) == value:
                return seed
    raise ValueError(f"invalid measurement seed directory {value!r}")


class MeasurementPurpose(StrEnum):
    """The evaluation that owns a measurement draw."""

    TOURNAMENT = "tournament"
    CALIBRATION = "calibration"
    PREFLIGHT = "contract_preflight"
    SCREEN = "candidate_screen"
    CONFIRMATION = "evidence_confirmation"
    REFLECTION = "board_reflection"
    ADMISSION = "eval_synthesis_admission"


@dataclass(frozen=True, slots=True)
class MeasurementRange:
    """One purpose's half-open interval in the historical integer encoding."""

    purpose: MeasurementPurpose
    start: int
    stop: int
    own_code: bool
    cell_evidence: bool = False

    @property
    def span(self) -> int:
        return self.stop - self.start

    def holds(self, index: int) -> bool:
        return self.start <= index < self.stop


MEASUREMENT_RANGES = (
    MeasurementRange(MeasurementPurpose.TOURNAMENT, 0, 1000, True, True),
    MeasurementRange(MeasurementPurpose.CALIBRATION, 1000, 2000, True),
    MeasurementRange(MeasurementPurpose.PREFLIGHT, 2000, 3000, False),
    MeasurementRange(MeasurementPurpose.SCREEN, 3000, 4000, False),
    MeasurementRange(MeasurementPurpose.CONFIRMATION, 4000, 5000, True, True),
    MeasurementRange(MeasurementPurpose.REFLECTION, 5000, 6000, True),
    MeasurementRange(MeasurementPurpose.ADMISSION, 6000, 7000, True),
)


def measurement_range(purpose: MeasurementPurpose | str) -> MeasurementRange:
    """Return the allocation owned by a named measurement purpose."""
    for allocation in MEASUREMENT_RANGES:
        if allocation.purpose == purpose:
            return allocation
    raise ValueError(f"unknown measurement purpose {purpose!r}")


# Compatible imports for callers that still use integer replicate indices.
CALIBRATION_REPLICATE_BASE = measurement_range(MeasurementPurpose.CALIBRATION).start
CALIBRATION_REPLICATE_SPAN = measurement_range(MeasurementPurpose.CALIBRATION).span
PREFLIGHT_REPLICATE_BASE = measurement_range(MeasurementPurpose.PREFLIGHT).start
PREFLIGHT_REPLICATE_SPAN = measurement_range(MeasurementPurpose.PREFLIGHT).span
SCREEN_REPLICATE_BASE = measurement_range(MeasurementPurpose.SCREEN).start
EVIDENCE_REPLICATE_BASE = measurement_range(MeasurementPurpose.CONFIRMATION).start
REFLECTION_REPLICATE_BASE = measurement_range(MeasurementPurpose.REFLECTION).start
SYNTHESIS_REPLICATE_BASE = measurement_range(MeasurementPurpose.ADMISSION).start


def range_at(index: int) -> MeasurementRange | None:
    """Return the owner of an integer slot, or None for an unclaimed slot."""
    return next((allocation for allocation in MEASUREMENT_RANGES if allocation.holds(index)), None)


@dataclass(frozen=True, slots=True)
class MeasurementDraw:
    """A purpose-local draw and its selected execution seed.

    Missing historical seed provenance is distinct from an explicitly
    unseeded execution, whose base seed is None.
    """

    purpose: MeasurementPurpose
    draw: int
    base_seed: BaseSeed = UNKNOWN_SEED

    def __post_init__(self) -> None:
        seed_qualifier(self.base_seed)
        allocation = measurement_range(self.purpose)
        if type(self.draw) is not int or not 0 <= self.draw < allocation.span:
            raise ValueError(
                f"measurement draw for {self.purpose!s} must be an integer in "
                f"0..{allocation.span - 1}, got {self.draw!r}"
            )

    @property
    def replicate_index(self) -> int:
        """The compatible filename and harness-seed encoding."""
        return measurement_range(self.purpose).start + self.draw

    def to_json(self) -> dict[str, Any]:
        value: dict[str, Any] = {"purpose": str(self.purpose), "draw": self.draw}
        if self.base_seed is not UNKNOWN_SEED:
            value["base_seed"] = self.base_seed
        return value

    @classmethod
    def from_json(cls, value: Any) -> MeasurementDraw:
        if not isinstance(value, Mapping) or set(value) not in (
            {"purpose", "draw"},
            {"purpose", "draw", "base_seed"},
        ):
            raise ValueError(
                "measurement identity must contain purpose, draw, and optional base_seed"
            )
        return cls(
            MeasurementPurpose(value["purpose"]),
            value["draw"],
            value.get("base_seed", UNKNOWN_SEED),
        )

    @classmethod
    def from_index(cls, index: int, *, base_seed: BaseSeed = UNKNOWN_SEED) -> MeasurementDraw:
        if type(index) is not int or (allocation := range_at(index)) is None:
            raise ValueError(f"unclaimed measurement replicate index {index!r}")
        return cls(allocation.purpose, index - allocation.start, base_seed)


def validate_measurement_interval(
    base: int, count: int, *, purpose: MeasurementPurpose | None = None, allow_empty: bool = False
) -> MeasurementDraw:
    """Validate the complete requested interval before any draw is scheduled."""
    first = MeasurementDraw.from_index(base)
    allocation = measurement_range(first.purpose)
    minimum = 0 if allow_empty else 1
    if type(count) is not int or count < minimum:
        raise ValueError(f"measurement draw count must be an integer >= {minimum}, got {count!r}")
    if purpose is not None and first.purpose != purpose:
        raise ValueError(f"measurement index {base} belongs to {first.purpose}, not {purpose}")
    if base + count > allocation.stop:
        raise ValueError(
            f"measurement interval [{base}, {base + count}) crosses the {first.purpose} "
            f"range [{allocation.start}, {allocation.stop}); maximum count here is "
            f"{allocation.stop - base}"
        )
    return first


def recorded_measurement(
    index: int, *, measurement: MeasurementDraw | None, match_id: str = ""
) -> MeasurementDraw:
    """Decode explicit identity or historical producer provenance without guessing conflicts."""
    expected = MeasurementDraw.from_index(index)
    if measurement is not None:
        if (measurement.purpose, measurement.draw) != (expected.purpose, expected.draw):
            raise ValueError(
                f"ambiguous measurement: recorded {measurement.purpose} draw {measurement.draw} "
                f"conflicts with slot {index}"
            )
        return measurement
    prefixes = (
        ("aa-calibration:", MeasurementPurpose.CALIBRATION),
        ("contract-preflight:", MeasurementPurpose.PREFLIGHT),
        ("candidate-screen", MeasurementPurpose.SCREEN),
        ("bt-replicate:", MeasurementPurpose.CONFIRMATION),
        ("reflection:", MeasurementPurpose.REFLECTION),
        ("admission-", MeasurementPurpose.ADMISSION),
    )
    producer = next((purpose for prefix, purpose in prefixes if match_id.startswith(prefix)), None)
    if producer is None and expected.purpose == MeasurementPurpose.TOURNAMENT:
        return expected
    if producer == expected.purpose:
        return expected
    raise ValueError(
        f"ambiguous historical measurement at slot {index}: "
        f"producer {match_id!r} does not establish purpose {expected.purpose}"
    )


def measurement_artifact_path(
    run_dir: Path, artifact: str, index: int, *, base_seed: BaseSeed = UNKNOWN_SEED
) -> Path:
    """Locate one draw without overwriting another seed or historical record."""
    return run_dir / seed_qualifier(base_seed) / unit_artifact_name(artifact, index)


def iter_measurement_artifacts(
    run_dir: Path, artifact: str = "loss", *, include_aliases: bool = False
) -> Iterator[Path]:
    """Enumerate physical slots, including historical files and all recorded seeds.

    Attempts remain separate from reusable slots. Unclaimed indices are
    enumerated so audit readers can explain their exclusion. Audit readers
    may include filename aliases, which never establish a valid physical slot.
    """
    if not run_dir.is_dir():
        return
    directories = [run_dir]
    for child in sorted(run_dir.iterdir()):
        if child.is_dir() and not child.is_symlink():
            try:
                _seed_from_qualifier(child.name)
            except ValueError:
                continue
            directories.append(child)
    for directory in directories:
        for path in sorted(directory.iterdir()):
            if (
                path.is_file()
                and artifact_replicate_index(path.name, artifact, canonical=not include_aliases)
                is not None
            ):
                yield path


def iter_measurement_attempts(loss_path: Path) -> Iterator[Path]:
    """Enumerate a slot's retained losses, excluding unpublished archive copies.

    Historical sibling attempts come first in sequence order. Complete archive
    directories follow in publication order, with their digest breaking ties.
    Missing loss files remain absent: a partial execution is never a measurement.
    """
    index = artifact_replicate_index(loss_path.name)
    if index is None or not loss_path.parent.is_dir():
        return
    prefix = unit_artifact_name("loss", index)[:-5]
    sibling_pattern = re.compile(re.escape(prefix) + r"\.a([1-9][0-9]*)\.json")
    siblings = [
        (int(match[1]), path)
        for path in loss_path.parent.iterdir()
        if (match := sibling_pattern.fullmatch(path.name)) and path.is_file()
    ]
    yield from (path for _, path in sorted(siblings))
    archive_root = loss_path.parent / "attempts"
    if not archive_root.is_dir():
        return
    archive_pattern = re.compile(re.escape(prefix) + r"-[0-9a-f]{64}")
    archives = []
    for directory in archive_root.iterdir():
        if directory.is_symlink() or not archive_pattern.fullmatch(directory.name):
            continue
        path = directory / loss_path.name
        try:
            if path.is_file():
                archives.append((directory.stat().st_mtime_ns, path))
        except OSError:
            continue
    yield from (path for _, path in sorted(archives))


def recorded_artifact_measurement(
    run_dir: Path,
    path: Path,
    measurement: MeasurementDraw | None,
    match_id: str = "",
    *,
    artifact: str = "loss",
) -> MeasurementDraw:
    """Require persisted purpose, draw, and seed to agree with the physical path."""
    index = artifact_replicate_index(path.name, artifact)
    if index is None:
        raise ValueError("artifact path does not identify a measurement draw")
    if path.parent == run_dir:
        base_seed: BaseSeed = UNKNOWN_SEED
    elif path.parent.parent == run_dir:
        base_seed = _seed_from_qualifier(path.parent.name)
    else:
        raise ValueError("measurement artifact is outside its entry directory")
    recorded = recorded_measurement(index, measurement=measurement, match_id=match_id)
    if recorded.base_seed != base_seed:
        raise ValueError("recorded measurement seed conflicts with its artifact path")
    return recorded


def unit_artifact_name(artifact: str, index: int) -> str:
    """The compatible filename shared by a draw's loss and capture companions."""
    extension = "jsonl" if artifact in {"events", "judge_io"} else "json"
    suffix = f".r{index}" if index > 0 else ""
    return f"{artifact}{suffix}.{extension}"


def artifact_replicate_index(
    name: str, artifact: str = "loss", *, canonical: bool = True
) -> int | None:
    """Decode a slot name; noncanonical aliases are available only for audit."""
    if name == unit_artifact_name(artifact, 0):
        return 0
    prefix = f"{artifact}.r"
    suffix = ".jsonl" if artifact in {"events", "judge_io"} else ".json"
    if name.startswith(prefix) and name.endswith(suffix):
        value = name[len(prefix) : -len(suffix)]
        if value.isdecimal():
            index = int(value)
            if not canonical or unit_artifact_name(artifact, index) == name:
                return index
    return None
