"""Durable change signals for the derived analytical index.

Canonical writers issue an epoch revision before replacing indexed records.
UUIDs let delegated workers issue revisions without a shared read/increment
operation. A repair captures revisions only after workers have finished and
acknowledges that snapshot after committing a complete epoch projection.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from zicato.storage import atomic_write_text
from zicato.workspace.layout import WorkspaceLayout


def mark_epoch_changed(workspace_root: Path, epoch_id: str) -> None:
    """Record a pending projection before a canonical mutation starts.

    The caller owns the workspace writer lease or executes delegated work
    within that invocation. This function does not acquire another lease.
    Failure to persist the signal must prevent the canonical replacement.
    """
    if not epoch_id or epoch_id in {".", ".."} or Path(epoch_id).name != epoch_id:
        raise ValueError("index revision requires one epoch directory name")
    path = WorkspaceLayout.from_root(workspace_root).index_revision(epoch_id)
    atomic_write_text(path, uuid4().hex + "\n")


def epoch_revisions(workspace_root: Path) -> dict[str, str]:
    """Read the epoch revisions without creating workspace files."""
    directory = WorkspaceLayout.from_root(workspace_root).index_revisions_dir
    return {path.stem: path.read_text(encoding="utf-8") for path in directory.glob("*.revision")}
