"""The tournament live-state EVENT LOG.

Several writers publish the in-progress tournament's live state: the
orchestrator's full-envelope republish (once per scheduled batch) and the
runner's per-board-unit updates (entry transitions, partial aggregates,
projected standings). Were that state one mutable ``active_tournament.json``
file, each writer would read-modify-write it and the slower of two racing
writers would silently drop the other's update.

So the state is instead a **single-writer, append-only EVENT LOG** built on
:class:`zicato.runtime.channel.EventLog`. Every
state transition is **one atomic append** — never a read-modify-write of
a shared mutable file — so concurrent writers cannot lose each other's
updates. A reader **folds the log into the live view**: an
:class:`~zicato.runtime.state.ActiveTournament` reconstructed by replaying
the events. "Settled" is just the terminal ``Snapshot`` event with
``phase == "completed"``.

Event vocabulary
----------------
Each event's ``payload`` carries only the delta its writer produces:

``Snapshot``
    A FULL :meth:`ActiveTournament.to_dict` envelope — the base/reset
    state. Written by the orchestrator's republish, the gauntlet runner's
    open, and the settle. A ``Snapshot`` RESETS the fold (it is the
    authoritative whole-envelope state at that point), so the fold starts
    from the LAST ``Snapshot`` and applies the delta events after it.
``EntryUpdate`` (``{entry_id, side, updates}``)
    One ``(entry_id, side)`` row's per-field overrides — the runner's
    per-board entry transition (queued → running → completed + the loss
    summary / drift snapshot / adk session id).
``PartialAggregate`` (``{champion_agg?, challenger_agg?}``)
    The running partial aggregate for one or both sides — the runner's
    per-board aggregate fold.
``ProjectedUpdate`` (``{projected}``)
    The live projected-standing rows per in-flight competitor — the
    runner's per-board projection. The fold merges them onto
    ``projected`` AND folds them into the live rung ``live_progress``,
    using the merge code in :mod:`zicato.runtime.state`.

Writer and fold share those merge helpers rather than each implementing
the semantics, so the two cannot drift apart.

When no event log exists
------------------------
:func:`fold_active_tournament` falls back to a plain
``active_tournament.json`` snapshot, so a hand-written or hand-edited
snapshot still surfaces. :func:`clear_log` removes the log and that
snapshot together, so a cleared tournament reads ``None`` either way.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

from zicato.runtime._storage import active_tournament_key, active_tournament_log_key
from zicato.runtime.channel import EventLog
from zicato.runtime.paths import ensure_runtime_dirs
from zicato.storage import workspace_backend

# Event type tokens — the single producer + the fold agree on these.
SNAPSHOT = "Snapshot"
ENTRY_UPDATE = "EntryUpdate"
PARTIAL_AGGREGATE = "PartialAggregate"
PROJECTED_UPDATE = "ProjectedUpdate"


def _log(workspace_root: Path) -> EventLog:
    """Bind the active-tournament :class:`EventLog` for a workspace."""
    return EventLog(workspace_backend(workspace_root, start=False), active_tournament_log_key())


# ---------------------------------------------------------------------------
# Appenders — the single-writer producer surface. Each is ONE atomic append.
# ---------------------------------------------------------------------------


def append_snapshot(workspace_root: Path, envelope: dict[str, Any]) -> None:
    """Append a full-envelope ``Snapshot`` event (the base/reset state).

    ``envelope`` is an :meth:`ActiveTournament.to_dict` dict. A
    ``Snapshot`` is the authoritative whole-envelope state at this point,
    so the fold restarts from it — a republish that would have overwritten
    the snapshot file appends a fresh ``Snapshot`` instead.
    """
    ensure_runtime_dirs(workspace_root)
    _log(workspace_root).append(SNAPSHOT, envelope)


def append_entry_update(
    workspace_root: Path, entry_id: str, side: str, updates: dict[str, Any]
) -> None:
    """Append one ``EntryUpdate`` delta for the ``(entry_id, side)`` row."""
    ensure_runtime_dirs(workspace_root)
    _log(workspace_root).append(
        ENTRY_UPDATE, {"entry_id": entry_id, "side": side, "updates": dict(updates)}
    )


def append_partial_aggregate(
    workspace_root: Path,
    *,
    champion_agg: dict[str, Any] | None = None,
    challenger_agg: dict[str, Any] | None = None,
) -> None:
    """Append a ``PartialAggregate`` delta for one or both sides."""
    payload: dict[str, Any] = {}
    if champion_agg is not None:
        payload["champion_agg"] = dict(champion_agg)
    if challenger_agg is not None:
        payload["challenger_agg"] = dict(challenger_agg)
    if not payload:
        return
    ensure_runtime_dirs(workspace_root)
    _log(workspace_root).append(PARTIAL_AGGREGATE, payload)


def append_projected_update(workspace_root: Path, projected: dict[str, dict[str, Any]]) -> None:
    """Append a ``ProjectedUpdate`` delta (the live projected-standing rows)."""
    if not projected:
        return
    ensure_runtime_dirs(workspace_root)
    _log(workspace_root).append(
        PROJECTED_UPDATE,
        {"projected": {str(k): dict(v) for k, v in projected.items() if isinstance(v, dict)}},
    )


def has_log(workspace_root: Path) -> bool:
    """Return ``True`` iff a non-empty event log exists for this workspace."""
    return _log(workspace_root).tail() is not None


def clear_log(workspace_root: Path) -> None:
    """Remove the event log AND any snapshot beside it. Idempotent.

    A cleared tournament must read ``None`` from BOTH the log and the
    fallback snapshot, so clearing removes both keys.
    """
    backend = workspace_backend(workspace_root, start=False)
    backend.delete(active_tournament_log_key())
    backend.delete(active_tournament_key())


# ---------------------------------------------------------------------------
# Fold — the consumer surface. Replays the log into an ActiveTournament.
# ---------------------------------------------------------------------------


def fold_active_tournament(workspace_root: Path) -> Any | None:
    """Fold the event log into an :class:`ActiveTournament`, or ``None``.

    Replays from the LAST ``Snapshot`` event (the authoritative reset) and
    applies every later delta in append order. Returns ``None`` when the
    tournament has been cleared or never started.

    When no event log exists, falls back to reading a plain
    ``active_tournament.json`` snapshot.
    """
    # Lazy import to avoid an import cycle (state imports this module).
    from zicato.runtime.state import (  # noqa: PLC0415
        ActiveTournament,
        _apply_entry_update,
        _champion_ids,
        _fold_projected_into_live_progress,
        read_active_tournament_snapshot,
    )

    events = _log(workspace_root).read()
    if not events:
        # No log — fall back to the plain snapshot.
        return read_active_tournament_snapshot(workspace_root)

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
