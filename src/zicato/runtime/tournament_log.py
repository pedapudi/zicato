"""Read and fold the active tournament event log.

The workspace writer lease retains one event writer for all matchups.
Publication helpers in :mod:`zicato.runtime.state` append these event types:

``Snapshot``
    A complete :meth:`ActiveTournament.to_dict` record that resets the fold.
``EntryUpdate``
    Per-field overrides for one ``(entry_id, side)`` row.
``PartialAggregate``
    Running aggregates for either or both tournament sides.
``ProjectedUpdate``
    Per-generation standings, also folded into live round progress.

The fold starts at the last snapshot and applies later updates in order.
An absent or empty log has no active tournament. The state module owns the
row and round merge functions used during replay.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

from zicato.runtime._storage import active_tournament_log_key
from zicato.runtime.channel import EventLog
from zicato.storage import workspace_backend

# Event type tokens — the single producer + the fold agree on these.
SNAPSHOT = "Snapshot"
ENTRY_UPDATE = "EntryUpdate"
PARTIAL_AGGREGATE = "PartialAggregate"
PROJECTED_UPDATE = "ProjectedUpdate"


def _log(workspace_root: Path) -> EventLog:
    """Bind the active-tournament :class:`EventLog` for a workspace."""
    return EventLog(workspace_backend(workspace_root, start=False), active_tournament_log_key())


def has_log(workspace_root: Path) -> bool:
    """Return ``True`` iff a non-empty event log exists for this workspace."""
    return _log(workspace_root).tail() is not None


# ---------------------------------------------------------------------------
# Fold — the consumer surface. Replays the log into an ActiveTournament.
# ---------------------------------------------------------------------------


def fold_active_tournament(workspace_root: Path) -> Any | None:
    """Fold the event log into an :class:`ActiveTournament`, or ``None``.

    Replays from the LAST ``Snapshot`` event (the authoritative reset) and
    applies every later delta in append order. Returns ``None`` when the
    tournament has been cleared or never started.

    An absent or empty event log carries no live tournament state.
    """
    # Lazy import to avoid an import cycle (state imports this module).
    from zicato.runtime.state import (  # noqa: PLC0415
        ActiveTournament,
        _apply_entry_update,
        _champion_ids,
        _fold_projected_into_live_progress,
    )

    events = _log(workspace_root).read()
    if not events:
        return None

    # Start from the last Snapshot (a Snapshot resets the fold) + the
    # deltas that follow it.
    base_idx = 0
    for i, ev in enumerate(events):
        if ev.type == SNAPSHOT:
            base_idx = i
    base_event = events[base_idx]
    if base_event.type != SNAPSHOT:
        # A malformed log with no base Snapshot — nothing to fold.
        return None
    current = ActiveTournament.from_dict(dict(base_event.payload))

    for ev in events[base_idx + 1 :]:
        if ev.type == SNAPSHOT:
            current = ActiveTournament.from_dict(dict(ev.payload))
        elif ev.type == ENTRY_UPDATE:
            p = ev.payload or {}
            current = _apply_entry_update(
                current, str(p.get("entry_id", "")), str(p.get("side", "")), p.get("updates") or {}
            )
        elif ev.type == PARTIAL_AGGREGATE:
            p = ev.payload or {}
            updates: dict[str, Any] = {}
            if isinstance(p.get("champion_agg"), dict):
                updates["partial_champion_agg"] = dict(p["champion_agg"])
            if isinstance(p.get("challenger_agg"), dict):
                updates["partial_challenger_agg"] = dict(p["challenger_agg"])
            if updates:
                current = replace(current, **updates)
        elif ev.type == PROJECTED_UPDATE:
            p = ev.payload or {}
            projected = p.get("projected") or {}
            if not projected:
                continue
            merged = {str(k): dict(v) for k, v in current.projected.items()}
            for gid, row in projected.items():
                if isinstance(row, dict):
                    merged[str(gid)] = dict(row)
            rounds, _changed = _fold_projected_into_live_progress(
                current.rounds, projected, champion_ids=_champion_ids(current.competitors)
            )
            current = replace(current, projected=merged, rounds=rounds)
    return current
