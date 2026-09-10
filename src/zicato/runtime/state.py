"""Typed records and publication helpers for ``.zicato/runtime/``.

Heartbeat and active-run records use atomic file replacement. Tournament
updates append through the event log retained by the workspace writer lease;
readers fold those events into an :class:`ActiveTournament`. Missing runtime
records are valid before an invocation starts or after cleanup.

Readers accept a workspace path. Tournament publishers require the held
:class:`WorkspaceLock`, which owns sequencing for every concurrent matchup.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from typing import Any

from zicato.runtime._storage import (
    active_run_key,
    active_runs_prefix,
    heartbeat_key,
    kill_request_key,
)
from zicato.runtime.lock import WorkspaceLock
from zicato.runtime.paths import ensure_runtime_dirs
from zicato.storage import workspace_backend

# Keep the local clock binding replaceable for deterministic record tests.
from zicato.util.iso_time import now_iso as _utc_now_iso


@dataclass(frozen=True, slots=True)
class DashboardEndpoint:
    """The address a dashboard service has actually bound."""

    host: str
    port: int

    def __post_init__(self) -> None:
        if not isinstance(self.host, str) or not self.host:
            raise ValueError("dashboard endpoint host must be a nonempty string")
        if type(self.port) is not int or not 1 <= self.port <= 65535:
            raise ValueError("dashboard endpoint port must be an integer between 1 and 65535")

    def to_json(self) -> dict[str, Any]:
        return {"host": self.host, "port": self.port}

    @classmethod
    def from_json(cls, raw: Any) -> DashboardEndpoint:
        if not isinstance(raw, dict):
            raise ValueError("dashboard endpoint must be an object")
        return cls(raw.get("host") or "127.0.0.1", raw.get("port", 0))


def read_dashboard_endpoint(path: Path) -> DashboardEndpoint | None:
    """Treat absent or malformed convenience records as unavailable."""
    try:
        return DashboardEndpoint.from_json(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return None


def write_dashboard_endpoint(workspace_root: Path, endpoint: DashboardEndpoint) -> None:
    """Publish the address atomically without a partially written record."""
    from zicato.runtime.paths import dashboard_endpoint_path
    from zicato.storage import atomic_write_text

    atomic_write_text(
        dashboard_endpoint_path(workspace_root), json.dumps(endpoint.to_json()) + "\n"
    )


class RunStatus(StrEnum):
    """The lifecycle state of one :class:`ActiveTournamentEntry` row.

    * :attr:`QUEUED` (``"queued"``) — seeded before the run kicks off.
    * :attr:`RUNNING` (``"running"``) — the run is in flight.
    * :attr:`COMPLETED` (``"completed"``) — the run settled normally.
    * :attr:`ABORTED` (``"aborted"``) — the run was cut short.
    * :attr:`CACHED` (``"cached"``) — no run was executed; a cached
      per-entry scalar was reused.

    A :class:`~enum.StrEnum`, so a member equals its wire token and
    serialises identically to the bare string the field stored before —
    the on-disk JSON is byte-identical. A value loaded from disk as a
    bare ``str`` still compares equal.
    """

    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    ABORTED = "aborted"
    CACHED = "cached"


class TournamentPhase(StrEnum):
    """The lifecycle phase of an :class:`ActiveTournament` envelope.

    * :attr:`PROPOSING` (``"proposing"``) — the proposer is generating the
      challenger; no duel has begun.
    * :attr:`RUNNING` (``"running"``) — the tournament is executing.
    * :attr:`COMPLETED` (``"completed"``) — the tournament settled.
    * :attr:`STOPPED` (``"stopped"``) — a lingering envelope flipped to a
      terminal state on shutdown.

    A :class:`~enum.StrEnum`, so a member equals its wire token and
    serialises identically to the bare string. This names only the closed
    :class:`ActiveTournament` lifecycle tokens; the free-form
    ``evolve_n_rounds:*`` heartbeat-phase labels are a separate slot and
    stay bare strings.
    """

    PROPOSING = "proposing"
    RUNNING = "running"
    COMPLETED = "completed"
    STOPPED = "stopped"


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
    seq:
        The orchestrator's TRUE liveness cursor: the
        tail ``seq`` of the progress event log
        (:mod:`zicato.runtime.progress_log`) at the last genuine
        transition. Unlike ``last_heartbeat`` — which the beater thread
        bumps on a timer regardless of progress — this advances ONLY when
        the evolve loop appends a real transition (round start, propose,
        apply, tournament start/settle, gate, promote/reject). A watchdog
        keyed on ``seq`` advancing avoids the timestamp signal's
        false-positive (a slow LLM call ages the stamp) and false-negative
        (a wedged loop whose beater keeps stamping ``now()`` reads alive).
        Defaults to ``0``; a heartbeat written before this field existed
        reads back as ``0`` (the safe "no progress recorded" default),
        indistinguishable from a workspace whose orchestrator never wrote a
        progress log.
    harmonograf_url:
        Server address of the harmonograf console this run is streaming
        telemetry to, when configured (``zicato evolve --harmonograf-url``,
        the workspace ``config.json``, or the selected service handle). Empty string when the run is
        JSONL-only. The dashboard surfaces it as a "watch live" link.
        Optional — old readers ignore the field.
    harmonograf_meta_session:
        The harmonograf SESSION id for this evolve's meta-loop (the
        orchestrator's own proposer + process-judge timeline — the
        operator's "Gantt view of zicato itself"). Deterministic from
        the evolve start time via
        :func:`zicato.telemetry.harmonograf_supervisor.meta_loop_session_id`.
        The dashboard deep-links the top-bar "execution" entry at
        ``<harmonograf_url>/#/session/<harmonograf_meta_session>``. Empty
        when no meta-loop session is in scope (JSONL-only / degraded
        install). Optional — old readers ignore the field. See
        ``docs/design/HARMONOGRAF.md`` §2b.
    settings:
        Every setting the run is operating under, as ``{name: {"value":
        ..., "source": ...}}`` keyed by the knob's dotted configuration
        name, where ``source`` names the tier that set it — the dataclass
        default, the workspace ``config.json``, an invocation overlay, or the
        host's CPU count. Composed by
        :func:`zicato.runtime.effective_settings.effective_settings` and
        stamped when the run resolves its runtime configuration, so a
        ceiling nobody wrote down is distinguishable from one an operator
        chose. Empty for a heartbeat written before the map existed, and
        for any process that never resolves a runtime config. An OPEN map:
        it grows as knobs are added, and a reader takes the entries it
        recognises and ignores the rest.
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
    seq: int = 0
    harmonograf_url: str = ""
    harmonograf_meta_session: str = ""
    settings: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)

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
            "seq": self.seq,
            "harmonograf_url": self.harmonograf_url,
            "harmonograf_meta_session": self.harmonograf_meta_session,
            "settings": {name: dict(entry) for name, entry in self.settings.items()},
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
            # Absent seq reads back as 0 — the safe default for a heartbeat
            # written by a workspace that keeps no progress log.
            seq=int(d.get("seq", 0)),
            harmonograf_url=str(d.get("harmonograf_url", "")),
            harmonograf_meta_session=str(d.get("harmonograf_meta_session", "")),
            # Absent settings read back as the empty map — the shape a
            # heartbeat written before the field existed has.
            settings={
                str(name): dict(entry)
                for name, entry in (d.get("settings") or {}).items()
                if isinstance(entry, Mapping)
            },
        )


def read_heartbeat(workspace_root: Path) -> Heartbeat | None:
    """Read ``heartbeat.json`` or return ``None`` if it does not exist."""
    raw = workspace_backend(workspace_root, start=False).read_json(heartbeat_key())
    if raw is None:
        return None
    return Heartbeat.from_dict(raw)


def write_heartbeat(workspace_root: Path, hb: Heartbeat) -> None:
    """Atomically write ``heartbeat.json``.

    Creates the runtime directory tree if it does not already exist so
    callers don't need to call :func:`ensure_runtime_dirs` first.
    """
    ensure_runtime_dirs(workspace_root)
    workspace_backend(workspace_root, start=False).write_json(heartbeat_key(), hb.to_dict())


# ---------------------------------------------------------------------------
# Active runs
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ActiveRun:
    """Per-in-flight-run live-state record.

    One file per run lives under :func:`zicato.runtime.paths.active_runs_dir`.
    The supervisor reads them all to render the active-runs panel; the
    run's worker process writes one on run-start, bumps ``last_progress``
    as the run produces events, and removes the file on a clean run-end.

    Fields
    ------
    run_id:
        Unique id of the run (matches :attr:`zicato.core.LossProfile.run_id`).
    pid:
        OS process id of the **run's own worker process** — the
        ``python -m zicato._tournament_worker`` subprocess executing this
        single entry, NOT the orchestrator. Each tournament run is
        isolated in its own OS process; the worker stamps ``os.getpid()``
        here on start. This is what lets the supervisor watchdog
        SIGTERM/SIGKILL an individual wedged run (by this pid) without
        touching the orchestrator or any sibling run.
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
    pgid:
        OS process-group id of the run's own worker process. The worker
        is spawned in its OWN session/process-group (``start_new_session``),
        so the supervisor can GROUP-kill the worker AND any grandchildren
        the system under test spawned (shells, helper tools) by negating this
        id, rather than leaking them when it kills the worker pid alone.
        ``None`` for a record that omits the field, and on a platform
        without process groups; the supervisor then falls back to the
        single-pid kill.
    producer_pid, producer_start_time:
        The process that launched this worker and its process start token,
        captured before spawning. A stale global heartbeat does not make a
        worker orphaned while its recorded producer is alive. Missing identity
        remains unproven; readers must not infer it from the worker's current
        parent because an orphan may have been reparented.
    snapshot_path:
        Absolute path-as-string to the run's ephemeral snapshot checkout
        (the ``ztw-snap-*`` temp directory the generation store
        materialises the per-run code tree into — a ``copytree`` under
        the directory backend, a detached ``git worktree`` under the git
        backend). The runner discards it on a clean run-end, but if the
        ORCHESTRATOR dies mid-run the directory is orphaned; recording it
        here lets the supervisor GC the leftover ``ztw-snap-*`` tree
        after an orchestrator death. ``None`` for a record that omits the
        field, and for a run that mounted no ephemeral snapshot.
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
    pid_start_time: float | None = None
    pgid: int | None = None
    snapshot_path: str | None = None
    producer_pid: int | None = None
    producer_start_time: float | None = None

    def __post_init__(self) -> None:
        if self.producer_pid is not None and (
            isinstance(self.producer_pid, bool)
            or not isinstance(self.producer_pid, int)
            or self.producer_pid <= 0
        ):
            raise ValueError("active run producer_pid must be a positive integer or null")
        if self.producer_start_time is not None and (
            self.producer_pid is None
            or isinstance(self.producer_start_time, bool)
            or not isinstance(self.producer_start_time, int | float)
            or not math.isfinite(self.producer_start_time)
            or self.producer_start_time < 0
        ):
            raise ValueError(
                "active run producer_start_time requires a producer PID and a finite token"
            )

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "run_id": self.run_id,
            "pid": self.pid,
            "pid_start_time": self.pid_start_time,
            "pgid": self.pgid,
            "snapshot_path": self.snapshot_path,
            "started_at": self.started_at,
            "last_progress": self.last_progress,
            "wall_clock_budget_seconds": self.wall_clock_budget_seconds,
            "deadline": self.deadline,
            "events_jsonl_path": self.events_jsonl_path,
            "entry_id": self.entry_id,
            "generation_id": self.generation_id,
            "epoch_id": self.epoch_id,
        }
        if self.producer_pid is not None:
            payload.update(
                producer_pid=self.producer_pid, producer_start_time=self.producer_start_time
            )
        return payload

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ActiveRun:
        raw_start = d.get("pid_start_time")
        raw_pgid = d.get("pgid")
        raw_snapshot = d.get("snapshot_path")
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
            pid_start_time=float(raw_start) if raw_start is not None else None,
            pgid=int(raw_pgid) if raw_pgid is not None else None,
            snapshot_path=str(raw_snapshot) if raw_snapshot is not None else None,
            producer_pid=d.get("producer_pid"),
            producer_start_time=d.get("producer_start_time"),
        )


def list_active_runs(workspace_root: Path) -> list[ActiveRun]:
    """Return every ``ActiveRun`` currently on disk, sorted by ``run_id``.

    Sorting gives the dashboard a stable rendering order even though the
    underlying filesystem makes no ordering guarantees. Returns an empty
    list when the directory does not exist or contains no run files.

    Half-written ``.tmp`` files (in the rare window of a racing write)
    are skipped — the storage backend's :meth:`~zicato.storage.StorageBackend.list_keys`
    excludes the ``.tmp`` artefacts an atomic write leaves behind.
    """
    backend = workspace_backend(workspace_root, start=False)
    out: list[ActiveRun] = []
    for key in backend.list_keys(active_runs_prefix()):
        raw = backend.read_json(key)
        if raw is None:
            continue
        out.append(ActiveRun.from_dict(raw))
    return out


def write_active_run(workspace_root: Path, run: ActiveRun) -> None:
    """Atomically write one run's state file."""
    ensure_runtime_dirs(workspace_root)
    backend = workspace_backend(workspace_root, start=False)
    backend.write_json(active_run_key(run.run_id), run.to_dict())


def remove_active_run(
    workspace_root: Path, run_id: str, *, expected_owner: ActiveRun | None = None
) -> None:
    """Remove a run record, optionally requiring its original process owner.

    Proposal attempts have unique keys. The comparison also refuses delayed
    cleanup if a different process record was written at the same key.
    """
    backend = workspace_backend(workspace_root, start=False)
    key = active_run_key(run_id)
    if expected_owner is not None:
        current = backend.read_json(key)
        if current is None or (current.get("pid"), current.get("pid_start_time")) != (
            expected_owner.pid,
            expected_owner.pid_start_time,
        ):
            return
    backend.delete(key)


def request_worker_kill(workspace_root: Path, run_id: str) -> None:
    """Ask the supervisor to escalate-kill a run's worker (parent→supervisor).

    Writes a ``control/kill_requests/{run_id}`` marker. The supervisor
    verifies the recorded process identity, terminates its owned group,
    and clears the marker after confirmation. The parent may use a bounded
    identity-checked fallback if delegated termination remains unconfirmed.

    Best-effort and idempotent: re-requesting an already-pending kill
    just rewrites the same marker. The payload carries the run id and a
    timestamp for the supervisor's audit log.
    """
    ensure_runtime_dirs(workspace_root)
    workspace_backend(workspace_root, start=False).write_json(
        kill_request_key(run_id),
        {"run_id": run_id, "requested_at": _utc_now_iso()},
    )


def clear_worker_kill_request(workspace_root: Path, run_id: str) -> None:
    """Remove a run's kill-request marker. Idempotent if already gone.

    The supervisor clears the marker after confirmed termination; the parent
    also clears it on cleanup so a marker never outlives its run (a
    recycled run id must not inherit a stale request).
    """
    workspace_backend(workspace_root, start=False).delete(kill_request_key(run_id))


def touch_active_run_progress(workspace_root: Path, run_id: str) -> None:
    """Bump ``last_progress`` on one run's state file.

    Cheap helper for the orchestrator's per-event hook. Reads the
    existing record, replaces the timestamp field, atomically writes it
    back. If the record does not exist (e.g. the run already finished
    and the cleanup beat the event hook), the call is a no-op rather
    than an error — that race is benign.
    """
    backend = workspace_backend(workspace_root, start=False)
    key = active_run_key(run_id)
    raw = backend.read_json(key)
    if raw is None:
        return
    current = ActiveRun.from_dict(raw)
    bumped = replace(current, last_progress=_utc_now_iso())
    backend.write_json(key, bumped.to_dict())


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
    adk_session_id:
        The ADK/goldfive session id for this entry's run — the
        ``sessionId`` envelope field carried on every event in the run's
        ``events.jsonl``. The runner stamps it here from the run's
        :class:`~zicato.core.types.LossProfile` the instant the run
        finishes, so the dashboard can deep-link a finished board run
        into harmonograf (``/#/session/<adk_session_id>``) without the
        SSE hot path ever having to open ``events.jsonl``. Empty string
        until the run completes (or when the run carried no session id).
    """

    entry_id: str
    side: str
    status: str
    started_at: str = ""
    completed_at: str = ""
    loss_summary: dict[str, float] = field(default_factory=dict)
    drift_count_snapshot: dict[str, int] = field(default_factory=dict)
    adk_session_id: str = ""
    # ADDITIVE (data-model §2.3): which round/match this run is part of.
    # ``side`` stays ``"parent"``/``"child"`` for a gauntlet; for every
    # other structure the runner passes the competitor's generation id as
    # ``side`` (an opaque key), and ``match_id`` links the row to a
    # ``rounds[].matches[]`` entry. Default ``""`` so a
    # Snapshot payload without this field loads unchanged.
    match_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "entry_id": self.entry_id,
            "side": self.side,
            "status": self.status,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "loss_summary": dict(self.loss_summary),
            "drift_count_snapshot": dict(self.drift_count_snapshot),
            "adk_session_id": self.adk_session_id,
            "match_id": self.match_id,
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
            adk_session_id=str(d.get("adk_session_id", "") or ""),
            match_id=str(d.get("match_id", "") or ""),
        )


def loss_summary_from_profile(profile: Any) -> dict[str, float]:
    """Project a :class:`~zicato.core.types.LossProfile` to ``loss_summary``.

    This is the pinned contract for the
    :attr:`ActiveTournamentEntry.loss_summary` field — the per-entry
    scalar snapshot the dashboard renders for a completed run. The
    dashboard consumes exactly these keys; the runner produces them via
    this single function so producer and consumer never diverge.

    Returned keys (all values ``float``):

    * ``drift_loss`` — the weighted drift-loss scalar (lower is better).
    * ``task_failure_ratio`` — fatally-failed-task ratio, ``[0.0, 1.0]``.
    * ``plan_revisions`` — plan-revision event count.
    * ``runtime_ms`` — wall-clock duration, milliseconds.
    * ``wall_clock_budget_exceeded`` — ``1.0`` iff the run was
      force-aborted on its budget, else ``0.0``.
    * ``tokens_spent``, ``output_chars``, ``schema_failures`` — the
      first-class cost / output / schema scalars.
    * ``pass_fail`` — ``1.0`` / ``0.0``; **omitted** when the profile's
      ``pass_fail`` is ``None`` (entry had no expectation).
    * ``turns_completed``, ``memory_failure_count``,
      ``context_loss_count`` — multi-turn extras; each **omitted** when
      ``None`` (single-turn entries leave these unset).

    Accepts any object exposing the :class:`LossProfile` field surface
    (typed as ``Any`` so this module stays free of a ``zicato.core``
    import).
    """
    summary: dict[str, float] = {
        "drift_loss": float(getattr(profile, "drift_loss", 0.0) or 0.0),
        "task_failure_ratio": float(getattr(profile, "task_failure_ratio", 0.0) or 0.0),
        "plan_revisions": float(getattr(profile, "plan_revisions", 0) or 0),
        "runtime_ms": float(getattr(profile, "runtime_ms", 0) or 0),
        "wall_clock_budget_exceeded": (
            1.0 if getattr(profile, "wall_clock_budget_exceeded", False) else 0.0
        ),
        "tokens_spent": float(getattr(profile, "tokens_spent", 0) or 0),
        "output_chars": float(getattr(profile, "output_chars", 0) or 0),
        "schema_failures": float(getattr(profile, "schema_failures", 0) or 0),
    }
    pass_fail = getattr(profile, "pass_fail", None)
    if pass_fail is not None:
        summary["pass_fail"] = 1.0 if pass_fail else 0.0
    for opt_name in ("turns_completed", "memory_failure_count", "context_loss_count"):
        opt_val = getattr(profile, opt_name, None)
        if opt_val is not None:
            summary[opt_name] = float(opt_val)
    return summary


def drift_count_snapshot_from_profile(profile: Any) -> dict[str, int]:
    """Project a :class:`~zicato.core.types.LossProfile` to ``drift_count_snapshot``.

    Pinned contract for :attr:`ActiveTournamentEntry.drift_count_snapshot`
    — the per-drift-kind total event count, **summed across severity
    buckets**, keyed by the verbatim :class:`~zicato.core.types.MetricCount`
    ``kind`` wire string (including ``custom:<judge_name>`` namespaced
    custom-judge kinds). Drift kinds with no events are absent from the
    mapping.
    """
    snapshot: dict[str, int] = {}
    for metric in profile.metric_counts:
        if not metric.name.startswith("drift:"):
            continue
        kind = metric.name.removeprefix("drift:")
        snapshot[kind] = snapshot.get(kind, 0) + int(metric.count)
    return snapshot


@dataclass(frozen=True, slots=True)
class ActiveTournament:
    """Snapshot of an in-progress tournament.

    Reconstructed from :func:`zicato.runtime.paths.active_tournament_log_path`.
    The orchestrator appends the initial shape (every entry × every side at
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
    partial_champion_agg, partial_challenger_agg:
        The **running partial aggregate** for each side — the same dict
        shape :func:`zicato.tournament.scoring.aggregate_generation_score`
        produces (``scalar`` / ``drift_loss_mean`` / ``pass_rate`` /
        ``entry_count`` / ``per_entry`` / ...), but computed only over
        the board units that have finished SO FAR. The runner rewrites
        these the instant each board unit settles, so a reader (the
        dashboard) sees a real server-side scalar climb as the
        tournament runs rather than 0.00 until the round ends. Empty
        dict before the first board unit completes; old readers ignore
        the fields. ``from_dict`` also accepts the
        ``partial_parent_agg`` / ``partial_child_agg`` spellings, so a
        ``Snapshot`` payload using either name loads.
    projected:
        The **live projected standing** per in-flight competitor, keyed by
        ``generation_id``. Each value is ``{scalar, boards_done,
        boards_total, pass_rate}`` — the running aggregate (the same
        :func:`zicato.tournament.scoring.aggregate_generation_score` the
        partial aggregate uses) over the board units that have settled SO
        FAR for that competitor, rewritten by the runner the instant each
        board unit settles. The dashboard folds these onto the matching
        standings rows + pending matches and marks them "projected"
        (visually distinct from a settled scalar) so an in-flight candidate
        shows a live, climbing standing. Empty before the first board unit
        completes. Missing ``projected`` fields decode to an empty dictionary.
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
    partial_champion_agg: dict[str, Any] = field(default_factory=dict)
    partial_challenger_agg: dict[str, Any] = field(default_factory=dict)
    # Missing structure fields decode to gauntlet defaults.
    # For ``structure == "gauntlet"`` the runner keeps
    # writing ``parent_generation_id`` / ``child_generation_id`` and MAY
    # leave these empty; a non-gauntlet structure
    # populates them as the authoritative field set.
    structure: str = "gauntlet"
    structure_params: dict[str, Any] = field(default_factory=dict)
    competitors: list[dict[str, Any]] = field(default_factory=list)
    rounds: list[dict[str, Any]] = field(default_factory=list)
    gen_states: list[dict[str, Any]] | None = None
    standings: list[dict[str, Any]] = field(default_factory=list)
    # The minting outcome for every challenger the proposer attempted this
    # round: ``{generation_id, status: "applied"|"rejected", reason, seed?}``.
    # Lets the dashboard render the candidate-generation step (the field
    # forming) live and post-hoc — a field where every challenger failed
    # reads as "N proposed · 0 applied". Missing fields decode to an empty list.
    field_status: list[dict[str, Any]] = field(default_factory=list)
    # ``{generation_id: {scalar, boards_done, boards_total, pass_rate}}`` —
    # the running aggregate over the boards settled so far for an in-flight
    # competitor, rewritten by the runner as each board unit settles. The
    # dashboard marks these "projected" (distinct from a settled scalar).
    # Missing fields decode to an empty dictionary.
    projected: dict[str, dict[str, Any]] = field(default_factory=dict)

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
            "partial_champion_agg": dict(self.partial_champion_agg),
            "partial_challenger_agg": dict(self.partial_challenger_agg),
            "structure": self.structure,
            "structure_params": dict(self.structure_params),
            "competitors": [dict(c) for c in self.competitors],
            "rounds": [dict(r) for r in self.rounds],
            **({"gen_states": self.gen_states} if self.gen_states is not None else {}),
            "standings": [dict(s) for s in self.standings],
            "field_status": [dict(f) for f in self.field_status],
            "projected": {str(k): dict(v) for k, v in self.projected.items()},
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ActiveTournament:
        # Accept the `partial_parent_agg` / `partial_child_agg` spellings
        # alongside the champion/challenger names, so a
        # Snapshot payload using either pair loads.
        raw_champion = d.get("partial_champion_agg", d.get("partial_parent_agg"))
        raw_challenger = d.get("partial_challenger_agg", d.get("partial_child_agg"))
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
            partial_champion_agg=dict(raw_champion) if isinstance(raw_champion, dict) else {},
            partial_challenger_agg=dict(raw_challenger) if isinstance(raw_challenger, dict) else {},
            structure=str(d.get("structure", "gauntlet") or "gauntlet"),
            structure_params=(
                dict(d["structure_params"]) if isinstance(d.get("structure_params"), dict) else {}
            ),
            competitors=[dict(c) for c in d.get("competitors", []) if isinstance(c, dict)],
            rounds=[dict(r) for r in d.get("rounds", []) if isinstance(r, dict)],
            gen_states=d.get("gen_states"),
            standings=[dict(s) for s in d.get("standings", []) if isinstance(s, dict)],
            field_status=[dict(f) for f in d.get("field_status", []) if isinstance(f, dict)],
            projected={
                str(k): dict(v)
                for k, v in (d.get("projected") or {}).items()
                if isinstance(v, dict)
            },
        )


def read_active_tournament(workspace_root: Path) -> ActiveTournament | None:
    """Read the live active tournament by FOLDING the event log, or ``None``.

    The live state is an append-only single-writer event log
    (:mod:`zicato.runtime.tournament_log`); this folds it into an
    :class:`ActiveTournament`. A missing log returns ``None``.
    """
    from zicato.runtime import tournament_log  # noqa: PLC0415

    return tournament_log.fold_active_tournament(workspace_root)


def write_active_tournament(writer: WorkspaceLock, t: ActiveTournament) -> None:
    """Publish the strategy's complete display state through its owned writer."""
    t = _complete_tournament_progress(t)
    writer._owned_lease().tournament_state = None
    writer.tournament_log.append("Snapshot", t.to_dict())
    writer._owned_lease().tournament_state = t


def _update_active_tournament(writer: WorkspaceLock, **updates: Any) -> None:
    """Publish replaced fields synchronously, retaining state on the writer lease.

    Matchups share this writer on one event loop. No await separates reading,
    appending, and retaining state, so concurrent matchups cannot lose updates.
    Readers apply field replacements without calculating tournament progress.
    """
    lease = writer._owned_lease()
    current = lease.tournament_state or read_active_tournament(writer.workspace_root)
    if current is None:
        return
    updated = _complete_tournament_progress(replace(current, **updates))
    before, after = current.to_dict(), updated.to_dict()
    entries = {
        str(index): entry
        for index, entry in enumerate(after.pop("entries"))
        if entry != before["entries"][index]
    }
    fields = {key: value for key, value in after.items() if value != before[key]}
    if fields or entries:
        lease.tournament_state = None
        writer.tournament_log.append("Update", {"fields": fields, "entries": entries})
    lease.tournament_state = updated


def _apply_entry_update(
    current: ActiveTournament, entry_id: str, side: str, updates: dict[str, Any]
) -> ActiveTournament:
    """Return ``current`` with the first ``(entry_id, side)`` row overridden.

    The pure fold step behind :func:`update_tournament_entry`: only the
    FIRST row matching the ``(entry_id, side)`` pair is replaced (each
    board entry appears once per side, so the ``side`` is part of the
    key); later duplicates are left untouched. Unknown override names
    raise :class:`TypeError` via :func:`dataclasses.replace`, catching a
    producer typo at the call site.
    """
    new_entries: list[ActiveTournamentEntry] = []
    updated = False
    for e in current.entries:
        if not updated and e.entry_id == entry_id and e.side == side:
            new_entries.append(replace(e, **updates))
            updated = True
        else:
            new_entries.append(e)
    return replace(current, entries=new_entries)


def update_tournament_entry(
    writer: WorkspaceLock, entry_id: str, side: str, **updates: Any
) -> None:
    """Publish one board entry's status through the retained tournament writer."""
    unknown = set(updates) - set(ActiveTournamentEntry.__dataclass_fields__)
    if unknown:
        raise TypeError(f"update_tournament_entry got unexpected field(s): {sorted(unknown)}")
    current = writer._owned_lease().tournament_state or read_active_tournament(
        writer.workspace_root
    )
    if current is not None:
        entries = _apply_entry_update(current, entry_id, side, updates).entries
        _update_active_tournament(writer, entries=entries)


def update_tournament_partial_aggregate(
    writer: WorkspaceLock,
    *,
    champion_agg: dict[str, Any] | None = None,
    challenger_agg: dict[str, Any] | None = None,
) -> None:
    """Publish running aggregates as board entries complete."""
    updates = {}
    if champion_agg is not None:
        updates["partial_champion_agg"] = dict(champion_agg)
    if challenger_agg is not None:
        updates["partial_challenger_agg"] = dict(challenger_agg)
    if updates:
        _update_active_tournament(writer, **updates)


def update_tournament_projected(
    writer: WorkspaceLock, projected: dict[str, dict[str, Any]]
) -> None:
    """Publish standings and round progress from the completed board results."""
    current = writer._owned_lease().tournament_state or read_active_tournament(
        writer.workspace_root
    )
    if current is not None and projected:
        _update_active_tournament(writer, projected={**current.projected, **projected})


def _champion_ids(competitors: list[dict[str, Any]]) -> set[str]:
    """Generation ids whose ``role`` marks them the champion (defender)."""
    return {
        str(c.get("generation_id", ""))
        for c in competitors
        if str(c.get("role", "")) == "champion" and c.get("generation_id")
    }


def _fold_one_lane(lane: dict[str, Any], proj: dict[str, Any], *, is_champion: bool) -> bool:
    """Fold one projected row onto one ``live_progress`` lane in place.

    Returns ``True`` iff a rounded lane value actually changed (anti-flash).
    The champion lane never overwrites its strategy-seeded
    ``projected_scalar`` benchmark, and its ``boards_done`` only grows.
    """
    changed = False
    if "boards_done" in proj:
        new_done = int(proj["boards_done"])
        cur_done = lane.get("boards_done")
        # The champion lane is written by every concurrent duel — take the
        # most-progressed (max), never let a less-progressed last writer
        # regress the count.
        if is_champion and isinstance(cur_done, int):
            new_done = max(cur_done, new_done)
        if cur_done != new_done:
            lane["boards_done"] = new_done
            changed = True
    if "boards_total" in proj and "boards_total" not in lane:
        lane["boards_total"] = int(proj["boards_total"])
        changed = True
    if not is_champion and "scalar" in proj:
        # Round to the dashboard's display precision so a sub-threshold
        # wobble in the running aggregate does not flash the lane.
        new_scalar = round(float(proj["scalar"]), 4)
        cur_scalar = lane.get("projected_scalar")
        if not (isinstance(cur_scalar, int | float) and round(float(cur_scalar), 4) == new_scalar):
            lane["projected_scalar"] = float(proj["scalar"])
            lane["projected"] = True
            changed = True
    return changed


def _complete_tournament_progress(current: ActiveTournament) -> ActiveTournament:
    """Calculate display progress once at publication, without deciding outcomes.

    Strategies supply every competitor and scheduled match. Completed matches
    retain their results. Running standings use measured scalars; Swiss points
    change only when its strategy records a completed match.
    """
    from copy import deepcopy

    current = deepcopy(current)
    champions = _champion_ids(current.competitors)
    rounds = current.rounds
    board_size = current.structure_params.get("board_size")
    active_seen = False
    in_flight = set()
    champion_agg = dict(current.partial_champion_agg)
    for round_ in rounds:
        matches = round_.get("matches", [])
        pending = any(match.get("pending") for match in matches)
        queued = pending and active_seen
        round_["queued"] = queued
        if pending:
            active_seen = True
        for match in matches:
            if not match.get("pending"):
                continue
            match["queued"] = queued
            total = board_size
            if isinstance(total, int) and current.structure == "racing":
                total = max(1, round(total * match.get("board_fraction", 1.0)))
            match["total"] = total
            lanes = match.setdefault("live_progress", {})
            for gid in match.get("competitors", []):
                lanes.setdefault(gid, {})
            projections = {}
            for gid, lane in lanes.items():
                if not queued:
                    in_flight.add(gid)
                projection = current.projected.get(gid, {}) if not queued else {}
                if projection:
                    _fold_one_lane(lane, projection, is_champion=gid in champions)
                if total is not None:
                    lane.setdefault("boards_total", total)
                lane["total"] = lane.get("boards_total")
                lane["done"] = lane.get("boards_done", 0)
                if gid in champions and "scalar" not in champion_agg:
                    scalar = lane.get("projected_scalar")
                    if isinstance(scalar, int | float):
                        champion_agg["scalar"] = scalar
                scalar = lane.get("projected_scalar")
                if isinstance(scalar, int | float) and not queued:
                    benchmark = champion_agg.get("scalar")
                    if isinstance(benchmark, int | float):
                        lane["partialDelta"] = scalar - benchmark
                    projections[gid] = {
                        "scalar": scalar,
                        "boards_done": lane.get("boards_done", 0),
                        "boards_total": lane.get("boards_total"),
                    }
            match["projected"] = projections or None
            match["done"] = sum(lane.get("done", 0) for lane in lanes.values())
    standings = []
    for standing in current.standings:
        row = dict(standing)
        gid = row["generation_id"]
        projection = current.projected.get(gid, {})
        if gid in in_flight and projection and "scalar" in projection:
            row.update(
                in_flight=True,
                projected_scalar=projection["scalar"],
                boards_done=projection.get("boards_done"),
                boards_total=projection.get("boards_total"),
            )
        else:
            for key in ("in_flight", "projected_scalar", "boards_done", "boards_total"):
                row.pop(key, None)
        standings.append(row)

    def scalar_key(row: dict[str, Any]) -> float:
        value = row.get("projected_scalar") if row.get("in_flight") else row.get("scalar")
        return float(value) if isinstance(value, int | float) else float("inf")

    if in_flight and current.projected:
        if current.structure == "swiss":
            standings.sort(key=lambda row: (-row.get("wins", 0), scalar_key(row)))
        elif current.structure in {"racing", "single_elim", "double_elim"}:
            standings.sort(key=scalar_key)
        for rank, row in enumerate(standings, 1):
            row["rank"] = rank
    from zicato.tournament.structure import attach_elim_states

    diagram = attach_elim_states({"structure": current.structure, "rounds": rounds})
    return replace(
        current,
        rounds=diagram["rounds"],
        gen_states=diagram.get("gen_states"),
        standings=standings,
        partial_champion_agg=champion_agg,
    )


def clear_active_tournament(writer: WorkspaceLock) -> None:
    """Clear the active tournament event log. Idempotent."""
    lease = writer._owned_lease()
    try:
        writer.tournament_log.clear()
    finally:
        lease.tournament_state = None


__all__ = [
    "RunStatus",
    "TournamentPhase",
    "Heartbeat",
    "ActiveRun",
    "ActiveTournamentEntry",
    "ActiveTournament",
    "read_heartbeat",
    "write_heartbeat",
    "list_active_runs",
    "write_active_run",
    "remove_active_run",
    "request_worker_kill",
    "clear_worker_kill_request",
    "touch_active_run_progress",
    "read_active_tournament",
    "write_active_tournament",
    "update_tournament_entry",
    "update_tournament_partial_aggregate",
    "update_tournament_projected",
    "clear_active_tournament",
    "loss_summary_from_profile",
    "drift_count_snapshot_from_profile",
]
