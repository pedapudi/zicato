"""Canonical inputs retained for one composed response, with explicit epoch identity."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

from zicato.epoch._storage import RecordError
from zicato.epoch.journal import patch_body, read_experiment_contents
from zicato.query.paths import WorkspacePaths, _read_json_value, layout_of
from zicato.workspace import generation_ids


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _copy(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _copy(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_copy(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class CapturedJson:
    """One parsed observation; consumers receive independent mutable copies."""

    _value: Any

    def __post_init__(self) -> None:
        object.__setattr__(self, "_value", _freeze(self._value))

    def copy(self) -> Any:
        return _copy(self._value)


@dataclass(frozen=True, slots=True)
class GenerationInputs:
    """One epoch's generation body, referenced patches, or recorded read failure."""

    body: CapturedJson
    patches: CapturedJson
    unreadable: str | None = None


def capture_generations(paths: WorkspacePaths, epoch_id: str) -> Mapping[str, GenerationInputs]:
    layout = layout_of(paths)
    records = {}
    for generation_id in generation_ids(layout, epoch_id):
        try:
            contents = read_experiment_contents(layout.root, epoch_id, generation_id)
        except RecordError as exc:
            records[generation_id] = GenerationInputs(
                CapturedJson(None), CapturedJson({}), str(exc)
            )
            continue
        records[generation_id] = GenerationInputs(
            CapturedJson(contents.body if contents is not None else None),
            CapturedJson(
                {
                    patch.mutation_id: patch_body(patch)
                    for patch in contents.patches
                    if patch.mutation_id
                }
                if contents is not None
                else {}
            ),
        )
    return MappingProxyType(records)


@dataclass(frozen=True, slots=True)
class EpochInputs:
    """Shared canonical inputs for one selected epoch in one response.

    Each file contributes one observation, including absence. This is not a
    transaction across files. A later response captures its own observations.
    """

    root: Path
    epoch_id: str
    config: CapturedJson
    scoring: CapturedJson
    generations: Mapping[str, GenerationInputs]

    @classmethod
    def capture(cls, paths: WorkspacePaths, epoch_id: str) -> EpochInputs:
        layout = layout_of(paths)
        return cls(
            root=paths.root.resolve(),
            epoch_id=epoch_id,
            config=CapturedJson(_read_json_value(layout.epoch_config(epoch_id))),
            scoring=CapturedJson(_read_json_value(layout.scoring(epoch_id))),
            generations=capture_generations(paths, epoch_id),
        )

    def check(self, paths: WorkspacePaths, epoch_id: str | None) -> None:
        if self.root != paths.root.resolve() or self.epoch_id != epoch_id:
            raise ValueError("query inputs belong to a different workspace or epoch")

    def experiment(self, generation_id: str) -> dict[str, Any] | None:
        record = self.generations.get(generation_id)
        return record.body.copy() if record is not None else None
