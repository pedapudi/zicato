"""Read published tournament snapshots and complete field replacements.

The owned runtime writer calculates display progress before appending. Readers
replay the last snapshot and later replacements without tournament rules.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from zicato.runtime._storage import active_tournament_log_key
from zicato.runtime.channel import EventLog
from zicato.storage import workspace_backend

SNAPSHOT = "Snapshot"
UPDATE = "Update"


def _log(workspace_root: Path) -> EventLog:
    return EventLog(workspace_backend(workspace_root, start=False), active_tournament_log_key())


def has_log(workspace_root: Path) -> bool:
    return _log(workspace_root).tail() is not None


def fold_active_tournament(workspace_root: Path) -> Any | None:
    """Read the last complete publication, or None when no tournament is active."""
    from zicato.runtime.state import ActiveTournament

    current = None
    for event in _log(workspace_root).read():
        if event.type == SNAPSHOT:
            current = dict(event.payload)
        elif event.type == UPDATE and current is not None:
            current.update(event.payload)
    return ActiveTournament.from_dict(current) if current is not None else None
