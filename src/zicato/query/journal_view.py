"""Serve the journal generated from recorded proposals and round outcomes."""

from __future__ import annotations

from typing import Any

from zicato.query.paths import WorkspacePaths


def read_epoch_journal(paths: WorkspacePaths, epoch_id: str) -> dict[str, Any]:
    """Return the journal generated from this epoch's recorded experiments."""
    return {"epoch_id": epoch_id, "journal": read_epoch_journal_md(paths, epoch_id) or ""}


def read_epoch_journal_md(paths: WorkspacePaths, epoch_id: str) -> str | None:
    """Return journal Markdown, or no document when records are unavailable."""
    from zicato.epoch.journal import read_journal

    try:
        return read_journal(paths.root, epoch_id) or None
    except (OSError, ValueError, RuntimeError):
        return None
