"""journal_view — the per-epoch journal document reads, served.

The journal endpoints used to read ``journal.md`` inline; these are those
file reads moved into the query layer. Both degrade best-effort (DQ3):
the JSON shape reads a missing journal as the empty string; the raw-text
read distinguishes absence (``None``) so its endpoint can answer an
unambiguous 404 for the "View raw journal" link.
"""

from __future__ import annotations

from typing import Any

from zicato.query.paths import WorkspacePaths


def read_epoch_journal(paths: WorkspacePaths, epoch_id: str) -> dict[str, Any]:
    """``GET /api/epoch/{id}/journal`` — ``{epoch_id, journal}``.

    A missing / unreadable ``journal.md`` degrades to the empty string —
    same shape, never raises (DQ3).
    """
    path = paths.epochs / epoch_id / "journal.md"
    try:
        text = path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        text = ""
    return {"epoch_id": epoch_id, "journal": text}


def read_epoch_journal_md(paths: WorkspacePaths, epoch_id: str) -> str | None:
    """The raw ``journal.md`` markdown for one epoch, or ``None`` when absent.

    ``None`` (not ``""``) on absence so the raw-markdown endpoint can
    answer an unambiguous 404 — the one caller that wants to distinguish
    "no journal yet" from "empty journal".
    """
    path = paths.epochs / epoch_id / "journal.md"
    try:
        return path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return None
