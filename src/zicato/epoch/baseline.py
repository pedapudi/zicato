"""Prepare immutable baseline source and complete interrupted seed bookkeeping."""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING

from zicato.core.types import Generation
from zicato.epoch.genstore import default_generation_store
from zicato.epoch.journal import read_experiment_if_present, write_seed_experiment
from zicato.epoch.lineage import append_to_lineage
from zicato.epoch.publication import BaselineSeed, baseline_seed_path, prepared_directory
from zicato.epoch.seed_sources import (
    GIT_ADMIN_BASENAMES,
    prepare_seed_sources,
    seed_content_identity,
    validated_seed_sources,
)
from zicato.storage import atomic_write_text, durable_unlink, sync_directory_tree
from zicato.workspace.projection import mark_epoch_changed

if TYPE_CHECKING:
    from zicato.runtime.lock import WorkspaceLock


def prepare_baseline_seed(
    workspace_root: Path,
    epoch_id: str,
    sources: Iterable[Path],
    *,
    backend: str,
    created_at: str,
    source_coordinates: tuple[str, str] | None = None,
) -> BaselineSeed:
    """Retain a synchronized source tree before recording publication intent."""
    excluded = GIT_ADMIN_BASENAMES if backend == "git" else frozenset()
    resolved = validated_seed_sources(sources, excluded_names=excluded)
    workspace_root.mkdir(parents=True, exist_ok=True)
    parent = Path(tempfile.mkdtemp(prefix=".baseline-seed-", dir=workspace_root))
    prepared = parent / "source"
    try:
        prepare_seed_sources(resolved, prepared)
        sync_directory_tree(parent)
        identity = seed_content_identity(prepared)
    except BaseException:
        shutil.rmtree(parent)
        raise
    return BaselineSeed(
        epoch_id=epoch_id,
        backend=backend,
        prepared_directory=prepared.relative_to(workspace_root).as_posix(),
        content_identity=identity,
        created_at=created_at,
        source_epoch=source_coordinates[0] if source_coordinates else None,
        source_generation=source_coordinates[1] if source_coordinates else None,
    )


def validate_baseline_seed(workspace_root: Path, seed: BaselineSeed) -> Path:
    """Refuse missing or changed prepared content instead of reading live sources."""
    prepared = prepared_directory(workspace_root, seed.prepared_directory)
    if seed_content_identity(prepared) != seed.content_identity:
        raise ValueError(f"prepared baseline source changed for epoch {seed.epoch_id}")
    return prepared


def finish_baseline_seed(
    workspace_root: Path, seed: BaselineSeed, *, writer: WorkspaceLock
) -> None:
    """Finish the source, lineage, marker, and experiment writes idempotently."""
    from zicato.runtime.lock import validate_workspace_lock  # noqa: PLC0415

    validate_workspace_lock(writer, workspace_root)
    prepared = validate_baseline_seed(workspace_root, seed)
    store = default_generation_store(workspace_root)
    if store.backend_name != seed.backend:
        raise ValueError("baseline publication backend differs from workspace configuration")
    read_experiment_if_present(workspace_root, seed.epoch_id, "v0")
    if not store.has_generation(seed.epoch_id, "v0"):
        mark_epoch_changed(workspace_root, seed.epoch_id)
        store.seed_generation(seed.epoch_id, "v0", sorted(prepared.iterdir()))
    snapshot = store.materialize_snapshot(seed.epoch_id, "v0")
    excluded = GIT_ADMIN_BASENAMES if seed.backend == "git" else frozenset()
    if seed_content_identity(snapshot, excluded_names=excluded) != seed.content_identity:
        raise ValueError(f"published baseline source differs from prepared epoch {seed.epoch_id}")

    parent = (
        f"{seed.source_epoch}:{seed.source_generation}" if seed.source_epoch is not None else None
    )
    append_to_lineage(
        workspace_root,
        seed.epoch_id,
        Generation(
            id="v0",
            epoch_id=seed.epoch_id,
            parent_id=None,
            snapshot_root=snapshot,
            created_at=seed.created_at,
            promoted=True,
        ),
        parent_id=parent,
    )
    marker = workspace_root / "epochs" / seed.epoch_id / "current_generation"
    if not marker.exists():
        mark_epoch_changed(workspace_root, seed.epoch_id)
        atomic_write_text(marker, "v0\n")
    write_seed_experiment(workspace_root, seed.epoch_id, proposed_at=seed.created_at)
    durable_unlink(baseline_seed_path(workspace_root, seed.epoch_id))
    shutil.rmtree(prepared.parent, ignore_errors=True)
