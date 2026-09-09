"""Measurement purpose, independent draw number, seed, and artifact paths."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, replace
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


def _seed_from_qualifier(value: str) -> int | None:
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
class MeasurementDraw:
    """The purpose, draw number, and seed of one measurement."""

    purpose: MeasurementPurpose
    draw: int
    base_seed: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.purpose, MeasurementPurpose):
            raise ValueError("measurement purpose must be a supported purpose")
        if type(self.draw) is not int or self.draw < 0:
            raise ValueError("measurement draw must be a nonnegative integer")
        if self.base_seed is not None and type(self.base_seed) is not int:
            raise ValueError("measurement base seed must be an integer or null")

    def __lt__(self, other: MeasurementDraw) -> bool:
        return (self.purpose, self.draw, seed_qualifier(self.base_seed)) < (
            other.purpose,
            other.draw,
            seed_qualifier(other.base_seed),
        )

    @property
    def own_code(self) -> bool:
        """Whether this purpose evaluates the recorded generation's own source."""
        return self.purpose not in {MeasurementPurpose.PREFLIGHT, MeasurementPurpose.SCREEN}

    @property
    def cell_evidence(self) -> bool:
        return self.purpose in {MeasurementPurpose.TOURNAMENT, MeasurementPurpose.CONFIRMATION}

    def offset(self, count: int) -> MeasurementDraw:
        return replace(self, draw=self.draw + count)

    @classmethod
    def from_context(cls, context: Mapping[str, str]) -> MeasurementDraw:
        raw = context.get("measurement")
        return cls.from_json(json.loads(raw)) if raw is not None else TOURNAMENT_DRAW

    def to_json(self) -> dict[str, Any]:
        return {"purpose": str(self.purpose), "draw": self.draw, "base_seed": self.base_seed}

    @classmethod
    def from_json(cls, value: Any) -> MeasurementDraw:
        if not isinstance(value, Mapping) or set(value) != {"purpose", "draw", "base_seed"}:
            raise ValueError("measurement requires purpose, draw, and base_seed")
        return cls(MeasurementPurpose(value["purpose"]), value["draw"], value["base_seed"])


TOURNAMENT_DRAW = MeasurementDraw(MeasurementPurpose.TOURNAMENT, 0)


def validate_measurement_count(count: int, *, allow_empty: bool = False) -> None:
    minimum = 0 if allow_empty else 1
    if type(count) is not int or count < minimum:
        raise ValueError(f"measurement draw count must be an integer >= {minimum}, got {count!r}")


def recorded_measurement(
    expected: MeasurementDraw, *, measurement: MeasurementDraw | None
) -> MeasurementDraw:
    """Require the record to name the purpose and draw selected by its path."""
    if measurement is None or (measurement.purpose, measurement.draw) != (
        expected.purpose,
        expected.draw,
    ):
        raise ValueError("recorded measurement conflicts with the selected purpose and draw")
    return measurement


def measurement_artifact_path(
    run_dir: Path,
    artifact: str,
    measurement: MeasurementDraw,
    *,
    base_seed: BaseSeed = UNKNOWN_SEED,
) -> Path:
    seed = measurement.base_seed if base_seed is UNKNOWN_SEED else base_seed
    return run_dir / seed_qualifier(seed) / unit_artifact_name(artifact, measurement)


def iter_measurement_artifacts(run_dir: Path, artifact: str = "loss") -> Iterator[Path]:
    """Enumerate supported measurement files for every recorded seed."""
    if not run_dir.is_dir():
        return
    for directory in sorted(run_dir.iterdir()):
        if not directory.is_dir() or directory.is_symlink():
            continue
        try:
            _seed_from_qualifier(directory.name)
        except ValueError:
            continue
        for path in sorted(directory.iterdir()):
            if path.is_file() and artifact_measurement(path.name, artifact) is not None:
                yield path


def iter_measurement_attempts(loss_path: Path) -> Iterator[Path]:
    """Enumerate a slot's retained losses, excluding unpublished archive copies.

    Historical sibling attempts come first in sequence order. Complete archive
    directories follow in publication order, with their digest breaking ties.
    Missing loss files remain absent: a partial execution is never a measurement.
    """
    index = artifact_measurement(loss_path.name)
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
    *,
    artifact: str = "loss",
) -> MeasurementDraw:
    """Require recorded purpose, draw, and seed to agree with the physical path."""
    expected = artifact_measurement(path.name, artifact)
    if expected is None or path.parent.parent != run_dir:
        raise ValueError("artifact path does not identify a measurement in this entry")
    expected = replace(expected, base_seed=_seed_from_qualifier(path.parent.name))
    if measurement != expected:
        raise ValueError("recorded measurement conflicts with its artifact path")
    return expected


def unit_artifact_name(artifact: str, measurement: MeasurementDraw) -> str:
    extension = "jsonl" if artifact in {"events", "judge_io"} else "json"
    return f"{artifact}.{measurement.purpose}.r{measurement.draw}.{extension}"


def artifact_measurement(name: str, artifact: str = "loss") -> MeasurementDraw | None:
    extension = "jsonl" if artifact in {"events", "judge_io"} else "json"
    match = re.fullmatch(re.escape(artifact) + r"\.([a-z_]+)\.r(0|[1-9][0-9]*)\." + extension, name)
    if match is None:
        return None
    try:
        return MeasurementDraw(MeasurementPurpose(match[1]), int(match[2]))
    except ValueError:
        return None
