"""Dataclasses + load/save helpers for ``.zicato/runtime/`` state files.

This module is the typed surface every state-file reader (the supervisor
binary, the dashboard, tests) goes through. The orchestrator writes via
the same helpers so the round-trip is symmetric.

The dataclasses here are **runtime-only state** — distinct from the
persisted-journal types in :mod:`zicato.core.types`. They are frozen,
slotted, JSON-friendly, and carry the minimum information the supervisor
and the dashboard need to render a live view of an in-progress epoch.

Every writer is atomic (``.tmp`` + ``fsync`` + ``os.replace``); see
:mod:`zicato.runtime._atomic`. Readers tolerate missing files and return
``None`` so the supervisor can run against a workspace that has never
booted an orchestrator.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from zicato.runtime._atomic import atomic_write_json, read_json
from zicato.runtime.paths import (
    active_run_path,
    active_runs_dir,
    active_tournament_path,
    ensure_runtime_dirs,
    heartbeat_path,
)


def _utc_now_iso() -> str:
    """Return current UTC time as an ISO-8601 string with seconds precision.

    Uses ``timespec='seconds'`` so the strings diff cleanly in journals
    and don't carry microsecond noise the dashboard would have to trim.
    The trailing ``Z`` is the explicit UTC marker convention every other
    zicato writer uses.
    """
    return _dt.datetime.now(_dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Heartbeat
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Heartbeat:
    """The orchestrator's liveness pulse.

    Bumped every few seconds by :class:`zicato.runtime.heartbeat.HeartbeatBeater`.
    The supervisor reads this file to detect a stalled orchestrator and
    escalate SIGTERM/SIGKILL. Operators can also tail it for a one-shot
    "is anything happening?" check.

    Fields
    ------
    pid:
        OS process id of the orchestrator. Used by the supervisor to
        verify the process is still alive (``os.kill(pid, 0)``).
    instance_id:
        Logical instance identifier (matches
        :class:`zicato.core.types.RuntimeConfig.instance_id`). Allows
        nested zicato deployments to share a workspace without colliding.
    started_at, last_heartbeat:
        ISO-8601 UTC timestamps. ``started_at`` is the orchestrator's
        boot time; ``last_heartbeat`` is the most recent bump. Watchdog
        thresholds key off the latter.
    epoch_id, generation_id:
        Currently-active lineage coordinates. Empty string when the
        orchestrator is between epochs or has not yet selected one.
    phase:
        Short symbolic state string (e.g. ``"tournament:entry=foo"``,
        ``"proposer"``, ``"applier"``). Free-form for now — the
        dashboard renders it verbatim.
    round_index:
        0-based index of the current evolve round. Useful for the
        supervisor's progress bar.
    round_started_at:
        ISO-8601 UTC timestamp of when the current round began. Lets the
        supervisor compute elapsed-in-round without re-reading any other
        state file.
    harmonograf_url:
        Server address of the harmonograf console this run is streaming
        telemetry to, when configured (via ``ZICATO_HARMONOGRAF_URL`` or
        the workspace ``config.json``). Empty string when the run is
        JSONL-only. The dashboard surfaces it as a "watch live" link.
        Optional — old readers ignore the field.
    """

    pid: int
    instance_id: str
    started_at: str
    last_heartbeat: str
    epoch_id: str = ""
    generation_id: str = ""
    phase: str = ""
    round_index: int = 0
    round_started_at: str = ""
    harmonograf_url: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dict for JSON encoding."""
        return {
            "pid": self.pid,
            "instance_id": self.instance_id,
            "started_at": self.started_at,
            "last_heartbeat": self.last_heartbeat,
            "epoch_id": self.epoch_id,
            "generation_id": self.generation_id,
            "phase": self.phase,
            "round_index": self.round_index,
            "round_started_at": self.round_started_at,
            "harmonograf_url": self.harmonograf_url,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Heartbeat:
        """Construct from a JSON-decoded dict."""
        return cls(
            pid=int(d["pid"]),
            instance_id=str(d["instance_id"]),
            started_at=str(d["started_at"]),
            last_heartbeat=str(d["last_heartbeat"]),
            epoch_id=str(d.get("epoch_id", "")),
            generation_id=str(d.get("generation_id", "")),
            phase=str(d.get("phase", "")),
            round_index=int(d.get("round_index", 0)),
            round_started_at=str(d.get("round_started_at", "")),
            harmonograf_url=str(d.get("harmonograf_url", "")),
        )


def read_heartbeat(workspace_root: Path) -> Heartbeat | None:
    """Read ``heartbeat.json`` or return ``None`` if it does not exist."""
    raw = read_json(heartbeat_path(workspace_root))
    if raw is None:
        return None
    return Heartbeat.from_dict(raw)


def write_heartbeat(workspace_root: Path, hb: Heartbeat) -> None:
    """Atomically write ``heartbeat.json``.

    Creates the runtime directory tree if it does not already exist so
    callers don't need to call :func:`ensure_runtime_dirs` first.
    """
    ensure_runtime_dirs(workspace_root)
    atomic_write_json(heartbeat_path(workspace_root), hb.to_dict())


# ---------------------------------------------------------------------------
# Active runs
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ActiveRun:
    """Per-in-flight-run live-state record.

    One file per run lives under :func:`zicato.runtime.paths.active_runs_dir`.
    The supervisor reads them all to render the active-runs panel; the
    orchestrator writes one on run-start, bumps ``last_progress`` as the
    run produces events, and removes the file on run-end.

    Fields
    ------
    run_id:
        Unique id of the run (matches :class:`zicato.core.types.RunRecord.run_id`).
    pid:
        OS process id of the orchestrator process executing this run.
    started_at, last_progress:
        ISO-8601 UTC timestamps. ``last_progress`` is bumped whenever
        the run emits a goldfive event; the supervisor compares against
        it to detect stuck runs.
    wall_clock_budget_seconds, deadline:
        The budget the orchestrator promised this run, and the absolute
        ISO-8601 UTC deadline (``started_at + budget``). The supervisor
        kills the run when wall-clock passes the deadline regardless of
        whether the orchestrator notices.
    events_jsonl_path:
        Absolute path-as-string to the goldfive events JSONL the run is
        currently writing to. The dashboard's drill-down link points
        harmonograf at this path.
    entry_id, generation_id, epoch_id:
        Lineage coordinates of the run.
    """

    run_id: str
    pid: int
    started_at: str
    last_progress: str
    wall_clock_budget_seconds: int
    deadline: str
    events_jsonl_path: str
    entry_id: str
    generation_id: str
    epoch_id: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "pid": self.pid,
            "started_at": self.started_at,
            "last_progress": self.last_progress,
            "wall_clock_budget_seconds": self.wall_clock_budget_seconds,
            "deadline": self.deadline,
            "events_jsonl_path": self.events_jsonl_path,
            "entry_id": self.entry_id,
            "generation_id": self.generation_id,
            "epoch_id": self.epoch_id,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ActiveRun:
        return cls(
            run_id=str(d["run_id"]),
            pid=int(d["pid"]),
            started_at=str(d["started_at"]),
            last_progress=str(d["last_progress"]),
            wall_clock_budget_seconds=int(d["wall_clock_budget_seconds"]),
            deadline=str(d["deadline"]),
            events_jsonl_path=str(d["events_jsonl_path"]),
            entry_id=str(d["entry_id"]),
            generation_id=str(d["generation_id"]),
            epoch_id=str(d["epoch_id"]),
        )


def list_active_runs(workspace_root: Path) -> list[ActiveRun]:
    """Return every ``ActiveRun`` currently on disk, sorted by ``run_id``.

    Sorting gives the dashboard a stable rendering order even though the
    underlying filesystem makes no ordering guarantees. Returns an empty
    list when the directory does not exist or contains no run files.

    Half-written ``.tmp`` files (in the rare window of a racing write)
    are skipped — :mod:`zicato.runtime._atomic` writes them with the
    ``.tmp`` suffix and we glob for ``*.json`` only.
    """
    runs_dir = active_runs_dir(workspace_root)
    if not runs_dir.exists():
        return []
    out: list[ActiveRun] = []
    for entry in sorted(runs_dir.iterdir()):
        if entry.suffix != ".json":
            continue
        raw = read_json(entry)
        if raw is None:
            continue
        out.append(ActiveRun.from_dict(raw))
    return out


def write_active_run(workspace_root: Path, run: ActiveRun) -> None:
    """Atomically write one run's state file."""
    ensure_runtime_dirs(workspace_root)
    atomic_write_json(active_run_path(workspace_root, run.run_id), run.to_dict())


def remove_active_run(workspace_root: Path, run_id: str) -> None:
    """Delete one run's state file. Idempotent if already gone."""
    path = active_run_path(workspace_root, run_id)
    if path.exists():
        path.unlink()


def touch_active_run_progress(workspace_root: Path, run_id: str) -> None:
    """Bump ``last_progress`` on one run's state file.

    Cheap helper for the orchestrator's per-event hook. Reads the
    existing file, replaces the timestamp field, atomically writes it
    back. If the file does not exist (e.g. the run already finished
    and the cleanup beat the event hook), the call is a no-op rather
    than an error — that race is benign.
    """
    path = active_run_path(workspace_root, run_id)
    raw = read_json(path)
    if raw is None:
        return
    current = ActiveRun.from_dict(raw)
    bumped = replace(current, last_progress=_utc_now_iso())
    atomic_write_json(path, bumped.to_dict())


# ---------------------------------------------------------------------------
# Active tournament
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ActiveTournamentEntry:
    """One entry's status inside the active tournament.

    Fields
    ------
    entry_id:
        The :class:`zicato.core.types.BoardEntry.id` this row represents.
    side:
        Which generation the run was scheduled against — ``"parent"`` or
        ``"child"``. The tournament executes both sides for each entry
        and the gate compares them; the dashboard groups by ``side`` to
        render the head-to-head view.
    status:
        Symbolic lifecycle state — ``"queued"``, ``"running"``,
        ``"completed"``, or ``"aborted"``.
    started_at, completed_at:
        ISO-8601 UTC timestamps; empty strings until set.
    loss_summary:
        Per-metric loss snapshot (e.g. ``{"drift_loss": 0.12,
        "pass_fail": 1.0}``). Empty until the reducer finishes for this
        entry. Stored on the runtime file so the dashboard can render a
        predicted-verdict band before the journal materializes.
    drift_count_snapshot:
        Per-drift-kind total count for this entry (sum across
        severities). Same role as ``loss_summary`` for the drift-heatmap
        panel.
    """

    entry_id: str
    side: str
    status: str
    started_at: str = ""
    completed_at: str = ""
    loss_summary: dict[str, float] = field(default_factory=dict)
    drift_count_snapshot: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "entry_id": self.entry_id,
            "side": self.side,
            "status": self.status,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "loss_summary": dict(self.loss_summary),
            "drift_count_snapshot": dict(self.drift_count_snapshot),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ActiveTournamentEntry:
        return cls(
            entry_id=str(d["entry_id"]),
            side=str(d["side"]),
            status=str(d["status"]),
            started_at=str(d.get("started_at", "")),
            completed_at=str(d.get("completed_at", "")),
            loss_summary={str(k): float(v) for k, v in d.get("loss_summary", {}).items()},
            drift_count_snapshot={
                str(k): int(v) for k, v in d.get("drift_count_snapshot", {}).items()
            },
        )


@dataclass(frozen=True, slots=True)
class ActiveTournament:
    """Snapshot of an in-progress tournament.

    One file at :func:`zicato.runtime.paths.active_tournament_path`. The
    orchestrator writes the initial shape (every entry × every side at
    ``status="queued"``) before kicking off the first run; per-entry
    transitions go through :func:`update_tournament_entry`.

    Fields
    ------
    tournament_id:
        Stable id for this tournament (convention:
        ``"tourn_{epoch}_{child_generation}"``).
    parent_generation_id, child_generation_id:
        The two generations the tournament compares.
    epoch_id:
        The owning epoch.
    started_at:
        ISO-8601 UTC of tournament start.
    entries:
        Per-(entry × side) status rows. Order is preserved across writes
        so the dashboard can render a stable grid.
    phase:
        Symbolic state of the tournament as a whole — ``"running"``,
        ``"completed"``, ``"aborted"``. Distinct from any individual
        entry's ``status``.
    round_index:
        0-based index of the evolve round this tournament belongs to.
        Lets the dashboard render "Tournament — round N of M". Defaults
        to 0; old readers ignore the field.
    total_rounds:
        Total number of evolve rounds requested for the current
        invocation. The "M" in "round N of M". Defaults to 0 (unknown);
        old readers ignore the field.
    """

    tournament_id: str
    parent_generation_id: str
    child_generation_id: str
    epoch_id: str
    started_at: str
    entries: list[ActiveTournamentEntry] = field(default_factory=list)
    phase: str = "running"
    round_index: int = 0
    total_rounds: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "tournament_id": self.tournament_id,
            "parent_generation_id": self.parent_generation_id,
            "child_generation_id": self.child_generation_id,
            "epoch_id": self.epoch_id,
            "started_at": self.started_at,
            "phase": self.phase,
            "round_index": self.round_index,
            "total_rounds": self.total_rounds,
            "entries": [e.to_dict() for e in self.entries],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ActiveTournament:
        return cls(
            tournament_id=str(d["tournament_id"]),
            parent_generation_id=str(d["parent_generation_id"]),
            child_generation_id=str(d["child_generation_id"]),
            epoch_id=str(d["epoch_id"]),
            started_at=str(d["started_at"]),
            phase=str(d.get("phase", "running")),
            round_index=int(d.get("round_index", 0)),
            total_rounds=int(d.get("total_rounds", 0)),
            entries=[ActiveTournamentEntry.from_dict(e) for e in d.get("entries", [])],
        )


def read_active_tournament(workspace_root: Path) -> ActiveTournament | None:
    """Read the active-tournament JSON or return ``None`` if absent."""
    raw = read_json(active_tournament_path(workspace_root))
    if raw is None:
        return None
    return ActiveTournament.from_dict(raw)


def write_active_tournament(workspace_root: Path, t: ActiveTournament) -> None:
    """Atomically write the active-tournament JSON."""
    ensure_runtime_dirs(workspace_root)
    atomic_write_json(active_tournament_path(workspace_root), t.to_dict())


def update_tournament_entry(workspace_root: Path, entry_id: str, **updates: Any) -> None:
    """Update one entry inside the active tournament.

    Reads the current tournament JSON, replaces every entry whose
    :attr:`ActiveTournamentEntry.entry_id` matches with the per-field
    overrides supplied as keyword arguments, and atomically writes the
    result. If no tournament file exists, the call is a no-op (rather
    than an error) — the orchestrator may not have initialized one yet.

    Multiple rows can share an ``entry_id`` (one per side) and the
    updater touches every matching row. Callers that need per-side
    targeting should call twice with different ``side=...`` payloads
    in :func:`write_active_tournament` before transition.

    Special-cased fields are passed through :class:`dataclasses.replace`
    so unknown keyword names raise :class:`TypeError` — the call site
    catches typos immediately.
    """
    current = read_active_tournament(workspace_root)
    if current is None:
        return
    new_entries = [replace(e, **updates) if e.entry_id == entry_id else e for e in current.entries]
    new = replace(current, entries=new_entries)
    write_active_tournament(workspace_root, new)


def clear_active_tournament(workspace_root: Path) -> None:
    """Remove the active-tournament JSON. Idempotent."""
    path = active_tournament_path(workspace_root)
    if path.exists():
        path.unlink()


__all__ = [
    "Heartbeat",
    "ActiveRun",
    "ActiveTournamentEntry",
    "ActiveTournament",
    "read_heartbeat",
    "write_heartbeat",
    "list_active_runs",
    "write_active_run",
    "remove_active_run",
    "touch_active_run_progress",
    "read_active_tournament",
    "write_active_tournament",
    "update_tournament_entry",
    "clear_active_tournament",
]
