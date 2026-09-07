"""Epoch-owned records describing unfinished epoch and baseline publication."""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from zicato.storage import atomic_write_json, read_json


def epoch_publication_path(workspace_root: Path) -> Path:
    return workspace_root / "epoch_publication.json"


def baseline_seed_path(workspace_root: Path, epoch_id: str) -> Path:
    return workspace_root / "epochs" / epoch_id / "baseline_seed.json"


def _record(path: Path, fields: set[str]) -> dict[str, Any] | None:
    raw = read_json(path)
    if raw is None and not path.exists():
        return None
    if not isinstance(raw, dict) or set(raw) != fields | {"format_version"}:
        raise ValueError(f"invalid publication record fields: {path}")
    if type(raw["format_version"]) is not int or raw["format_version"] != 1:
        raise ValueError(f"unsupported publication record format: {path}")
    return raw


def _text(raw: dict[str, Any], key: str) -> str:
    value = raw[key]
    if not isinstance(value, str) or not value:
        raise ValueError(f"publication field {key!r} must be a nonempty string")
    return value


def _optional_text(raw: dict[str, Any], key: str) -> str | None:
    return None if raw[key] is None else _text(raw, key)


def _coordinate(value: str) -> str:
    if value in {".", ".."} or re.fullmatch(r"[A-Za-z0-9_-]+", value) is None:
        raise ValueError(f"invalid publication coordinate: {value!r}")
    return value


def _identity(value: str) -> str:
    if re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError("publication identity must be a SHA-256 digest")
    return value


def _timestamp(value: str) -> str:
    if dt.datetime.fromisoformat(value).utcoffset() is None:
        raise ValueError("publication timestamp requires a timezone")
    return value


def prepared_directory(workspace_root: Path, relative: str) -> Path:
    """Resolve an operation-owned staging directory without allowing escape."""
    parts = Path(relative).parts
    if (
        len(parts) != 2
        or not parts[0].startswith((".epoch-publication-", ".baseline-seed-"))
        or parts[1] not in {"epoch", "source"}
        or ".." in parts
    ):
        raise ValueError(f"invalid prepared publication directory: {relative!r}")
    path = workspace_root / relative
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError("prepared publication directory must not be a symlink")
    if not path.resolve().is_relative_to(workspace_root.resolve()):
        raise ValueError(f"prepared publication directory escapes workspace: {relative!r}")
    return path


@dataclass(frozen=True)
class EpochPublication:
    epoch_id: str
    prepared_directory: str
    contract_hash: str
    content_identity: str
    predecessor_id: str | None
    predecessor_closed_at: str | None
    # Retained in format 1 so interrupted publications remain readable.
    recommendation_ids: tuple[str, ...]

    def write(self, workspace_root: Path) -> None:
        atomic_write_json(
            epoch_publication_path(workspace_root), {"format_version": 1, **asdict(self)}
        )

    @classmethod
    def read(cls, workspace_root: Path) -> EpochPublication | None:
        raw = _record(
            epoch_publication_path(workspace_root),
            {
                "epoch_id",
                "prepared_directory",
                "contract_hash",
                "content_identity",
                "predecessor_id",
                "predecessor_closed_at",
                "recommendation_ids",
            },
        )
        if raw is None:
            return None
        ids = raw["recommendation_ids"]
        if not isinstance(ids, list) or any(not isinstance(item, str) or not item for item in ids):
            raise ValueError("publication recommendation ids must be nonempty strings")
        epoch_id = _coordinate(_text(raw, "epoch_id"))
        directory = _text(raw, "prepared_directory")
        if not directory.startswith(".epoch-publication-") or not directory.endswith("/epoch"):
            raise ValueError("epoch publication requires its prepared epoch directory")
        prepared_directory(workspace_root, directory)
        predecessor = _optional_text(raw, "predecessor_id")
        closed_at = _optional_text(raw, "predecessor_closed_at")
        if predecessor is not None:
            _coordinate(predecessor)
            if predecessor == epoch_id:
                raise ValueError("an epoch cannot be its own predecessor")
        if closed_at is not None:
            _timestamp(closed_at)
            if predecessor is None:
                raise ValueError("epoch closure requires a predecessor")
        return cls(
            epoch_id,
            directory,
            _identity(_text(raw, "contract_hash")),
            _identity(_text(raw, "content_identity")),
            predecessor,
            closed_at,
            tuple(ids),
        )


@dataclass(frozen=True)
class BaselineSeed:
    epoch_id: str
    backend: str
    prepared_directory: str
    content_identity: str
    created_at: str
    source_epoch: str | None = None
    source_generation: str | None = None

    def body(self) -> dict[str, Any]:
        return {"format_version": 1, **asdict(self)}

    def write(self, workspace_root: Path) -> None:
        atomic_write_json(
            baseline_seed_path(workspace_root, self.epoch_id),
            self.body(),
        )

    @classmethod
    def read(
        cls, workspace_root: Path, epoch_id: str, *, path: Path | None = None
    ) -> BaselineSeed | None:
        raw = _record(
            path or baseline_seed_path(workspace_root, epoch_id),
            {
                "epoch_id",
                "backend",
                "prepared_directory",
                "content_identity",
                "created_at",
                "source_epoch",
                "source_generation",
            },
        )
        if raw is None:
            return None
        recorded_epoch = _coordinate(_text(raw, "epoch_id"))
        backend = _text(raw, "backend")
        if recorded_epoch != epoch_id or backend not in {"directory", "git"}:
            raise ValueError("baseline seed coordinates or backend are invalid")
        source_epoch = _optional_text(raw, "source_epoch")
        source_generation = _optional_text(raw, "source_generation")
        if (source_epoch is None) != (source_generation is None):
            raise ValueError("baseline seed predecessor needs epoch and generation coordinates")
        if source_epoch is not None and source_generation is not None:
            _coordinate(source_epoch)
            if re.fullmatch(r"v[0-9]+", source_generation) is None:
                raise ValueError("baseline seed source generation must be a generation id")
        directory = _text(raw, "prepared_directory")
        if not directory.startswith(".baseline-seed-") or not directory.endswith("/source"):
            raise ValueError("baseline seed requires its prepared source directory")
        prepared_directory(workspace_root, directory)
        return cls(
            epoch_id,
            backend,
            directory,
            _identity(_text(raw, "content_identity")),
            _timestamp(_text(raw, "created_at")),
            source_epoch,
            source_generation,
        )
