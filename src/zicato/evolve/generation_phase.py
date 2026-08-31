"""Generation coordinates and the finalized input to one evolve round."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from zicato.workspace import WorkspaceLayout, natural_key
from zicato.workspace import epochs as workspace_epochs


@dataclass(frozen=True, slots=True)
class PreparedRound:
    """Finalized contract, runtime, and proposer inputs for one round.

    The value is constructed after runtime-only rebinding, including the
    per-round token ledger, and after the train/holdout split and proposer
    context have been derived.  Tournament structures consume this same
    immutable value instead of reconstructing round state or accepting a
    long list of independently drifting arguments.
    """

    workspace_root: Path
    workspace_config: Any
    epoch_id: str
    round_index: int
    total_rounds: int
    instance_id: str
    parent_generation: Any
    adapter: Any
    config: Any
    weights: Any
    board: tuple[Any, ...]
    train_board: tuple[Any, ...]
    tournament_spec: Any
    strategy: Any
    brief: Any
    mutations: tuple[Any, ...]
    patterns: tuple[Any, ...]
    loss_summary: str
    failure_profile: str
    metric_priorities: str
    process_exemplars: str
    genealogy: tuple[Any, ...]
    calibration: Any
    disable_drift: tuple[Any, ...]
    judge_only: bool
    fast_mode: bool
    max_proposer_retries: int
    beater: Any
    meta_loop_emitter: Any
    proposer_agent: Any
    round_log: Any
    screen_candidates: Any
    recombine_pair: Any
    custom_judge_names: frozenset[str]


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

    return max(candidates, key=natural_key)


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

    return default_generation_store(workspace_root).materialize_snapshot(epoch_id, generation_id)


def next_generation_id(workspace_root: Path, epoch_id: str) -> str:
    """The id to mint for this epoch's next generation, read from disk."""
    root = WorkspaceLayout.from_root(workspace_root).generations_dir(epoch_id)
    names = [path.name for path in root.iterdir() if path.is_dir()] if root.is_dir() else []
    return workspace_epochs.next_generation_id(names)


def mutable_trees(adapter: Any, snapshot: Path) -> list[Path]:
    resolver = getattr(adapter, "mutable_subpaths", None)
    subpaths = resolver(snapshot) if callable(resolver) else None
    return list(subpaths) if subpaths else [snapshot]


__all__ = [
    "PreparedRound",
    "current_generation",
    "mutable_trees",
    "next_generation_id",
    "safe_parent",
    "set_current_generation",
    "snapshot_root",
]
