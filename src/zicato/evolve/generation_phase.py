"""Generation coordinates owned by the prepare and persist phases."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from zicato.workspace import WorkspaceLayout


@dataclass(frozen=True, slots=True)
class RoundSession:
    """Stable identity and dependencies shared by one round's phases."""

    workspace_root: Path
    epoch_id: str
    round_index: int
    total_rounds: int
    instance_id: str
    adapter: Any
    config: Any
    weights: Any


def round_number(generation_id: str) -> int | None:
    suffix = generation_id[1:]
    return int(suffix) if generation_id.startswith("v") and suffix.isdigit() else None


def current_marker(workspace_root: Path, epoch_id: str) -> Path:
    return WorkspaceLayout.from_root(workspace_root).current_generation_marker(epoch_id)


def current_generation(workspace_root: Path, epoch_id: str) -> str:
    marker = current_marker(workspace_root, epoch_id)
    if marker.exists() and (value := marker.read_text(encoding="utf-8").strip()):
        return value
    root = WorkspaceLayout.from_root(workspace_root).generations_dir(epoch_id)
    candidates = [path.name for path in root.iterdir() if path.is_dir()] if root.exists() else []
    if not candidates:
        raise FileNotFoundError(f"no generations under {root}; the epoch has no baseline yet")

    def key(name: str) -> tuple[int, int, str]:
        number = round_number(name)
        return (0, number, name) if number is not None else (1, 0, name)

    return max(candidates, key=key)


def safe_parent(workspace_root: Path, epoch_id: str | None) -> str:
    if not epoch_id:
        return ""
    try:
        return current_generation(workspace_root, epoch_id)
    except (FileNotFoundError, OSError):
        return ""


def set_current_generation(workspace_root: Path, epoch_id: str, generation_id: str) -> None:
    marker = current_marker(workspace_root, epoch_id)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(f"{generation_id}\n", encoding="utf-8")


def snapshot_root(workspace_root: Path, epoch_id: str, generation_id: str) -> Path:
    from zicato.epoch.genstore import default_generation_store

    return default_generation_store(workspace_root).snapshot_root(epoch_id, generation_id)


def next_generation_id(workspace_root: Path, epoch_id: str) -> str:
    from zicato.epoch.genstore import default_generation_store

    numbers = [
        number
        for generation_id in default_generation_store(workspace_root).list_generations(epoch_id)
        if (number := round_number(generation_id)) is not None
    ]
    return f"v{max(numbers, default=-1) + 1}"


def mutable_trees(adapter: Any, snapshot: Path) -> list[Path]:
    resolver = getattr(adapter, "mutable_subpaths", None)
    subpaths = resolver(snapshot) if callable(resolver) else None
    return list(subpaths) if subpaths else [snapshot]


__all__ = [
    "RoundSession",
    "current_generation",
    "mutable_trees",
    "next_generation_id",
    "round_number",
    "safe_parent",
    "set_current_generation",
    "snapshot_root",
]
