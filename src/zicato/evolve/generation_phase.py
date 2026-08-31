"""Generation coordinates and the finalized input to one evolve round."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from zicato.workspace import WorkspaceLayout, generation_ids
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


@dataclass(frozen=True, slots=True)
class FieldRound:
    """One round's coordinates, contract inputs, and runtime seams, expanded.

    :class:`PreparedRound` is what the evolve loop hands a round.  This value
    is that same state read out once, at the round's start, under the exact
    names the round's phases use.  Expanding it here rather than at the top of
    every phase keeps the round's board slices, model ids, and clock seams in
    one place, so two phases cannot disagree about which of them they run
    against.  ``prepared`` is retained because the candidate-batch phase
    consumes the whole value.
    """

    prepared: PreparedRound
    round_log: Any
    workspace_root: Path
    workspace_config: Any
    epoch_id: str
    round_index: int
    total_rounds: int
    parent_id: str
    adapter: Any
    config: Any
    weights: Any
    board: list[Any]
    train_board: list[Any]
    tournament_spec: Any
    strategy: Any
    mutations: list[Any]
    disable_drift: tuple[Any, ...]
    judge_only: bool
    fast_mode: bool
    beater: Any
    meta_loop_emitter: Any
    auxiliary_call_llm: Any
    auxiliary_model: str
    field_size: int


def current_marker(workspace_root: Path, epoch_id: str) -> Path:
    return WorkspaceLayout.from_root(workspace_root).current_generation_marker(epoch_id)


def current_generation(workspace_root: Path, epoch_id: str) -> str:
    marker = current_marker(workspace_root, epoch_id)
    if marker.exists() and (value := marker.read_text(encoding="utf-8").strip()):
        return value
    layout = WorkspaceLayout.from_root(workspace_root)
    candidates = generation_ids(layout, epoch_id)
    if not candidates:
        raise FileNotFoundError(
            f"no generations under {layout.generations_dir(epoch_id)}; "
            "the epoch has no baseline yet"
        )
    return candidates[-1]


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
    layout = WorkspaceLayout.from_root(workspace_root)
    return workspace_epochs.next_generation_id(generation_ids(layout, epoch_id))


def mutable_trees(adapter: Any, snapshot: Path) -> list[Path]:
    resolver = getattr(adapter, "mutable_subpaths", None)
    subpaths = resolver(snapshot) if callable(resolver) else None
    return list(subpaths) if subpaths else [snapshot]


__all__ = [
    "FieldRound",
    "PreparedRound",
    "current_generation",
    "mutable_trees",
    "next_generation_id",
    "safe_parent",
    "set_current_generation",
    "snapshot_root",
]
