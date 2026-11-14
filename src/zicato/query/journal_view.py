"""Serve the journal generated from recorded proposals and round outcomes."""

from __future__ import annotations

from zicato.query.paths import WorkspacePaths


def read_epoch_journal_md(paths: WorkspacePaths, epoch_id: str) -> str | None:
    """Return journal Markdown, or no document when records are unavailable."""
    from zicato.epoch.journal import read_journal

    try:
        return read_journal(paths.root, epoch_id) or None
    except (OSError, ValueError, RuntimeError):
        return None
