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

Persistence is routed through :class:`zicato.storage.StorageBackend` —
the canonical file backend by default (see :mod:`zicato.runtime._storage`).
The public helpers below keep their ``workspace_root: Path`` signatures
unchanged; internally each constructs the workspace's backend and
addresses records by logical key. The on-disk layout and the
atomic-write discipline are byte-identical to the pre-seam
implementation — a caller cannot tell the difference.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from zicato.runtime._storage import (
    active_run_key,
    active_runs_prefix,
    active_tournament_key,
    backend_for,
    heartbeat_key,
    kill_request_key,
)
from zicato.runtime.paths import ensure_runtime_dirs


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
    harmonograf_meta_session: str = ""

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
            "harmonograf_meta_session": self.harmonograf_meta_session,
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
            harmonograf_meta_session=str(d.get("harmonograf_meta_session", "")),
        )


def read_heartbeat(workspace_root: Path) -> Heartbeat | None:
    """Read ``heartbeat.json`` or return ``None`` if it does not exist."""
    raw = backend_for(workspace_root).read_json(heartbeat_key())
    if raw is None:
        return None
    return Heartbeat.from_dict(raw)


def write_heartbeat(workspace_root: Path, hb: Heartbeat) -> None:
    """Atomically write ``heartbeat.json``.

    Creates the runtime directory tree if it does not already exist so
    callers don't need to call :func:`ensure_runtime_dirs` first.
    """
    ensure_runtime_dirs(workspace_root)
    backend_for(workspace_root).write_json(heartbeat_key(), hb.to_dict())


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
        Unique id of the run (matches :class:`zicato.core.types.RunRecord.run_id`).
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

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "pid": self.pid,
            "pid_start_time": self.pid_start_time,
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
        raw_start = d.get("pid_start_time")
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
    backend = backend_for(workspace_root)
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
    backend_for(workspace_root).write_json(active_run_key(run.run_id), run.to_dict())


def remove_active_run(workspace_root: Path, run_id: str) -> None:
    """Delete one run's state file. Idempotent if already gone."""
    backend_for(workspace_root).delete(active_run_key(run_id))


def request_worker_kill(workspace_root: Path, run_id: str) -> None:
    """Ask the supervisor to escalate-kill a run's worker (parent→supervisor).

    Writes a ``control/kill_requests/{run_id}`` marker. The Rust
    supervisor — the single SIGTERM→grace→SIGKILL escalator — reads it,
    runs the escalation on the worker's pid, and clears the marker. The
    Python parent therefore never signals the worker itself, so there is
    no parent↔supervisor race over the same pid.

    Best-effort and idempotent: re-requesting an already-pending kill
    just rewrites the same marker. The payload carries the run id and a
    timestamp for the supervisor's audit log.
    """
    ensure_runtime_dirs(workspace_root)
    backend_for(workspace_root).write_json(
        kill_request_key(run_id),
        {"run_id": run_id, "requested_at": _utc_now_iso()},
    )


def clear_worker_kill_request(workspace_root: Path, run_id: str) -> None:
    """Remove a run's kill-request marker. Idempotent if already gone.

    The supervisor clears the marker once it has escalated; the parent
    also clears it on cleanup so a marker never outlives its run (a
    recycled run id must not inherit a stale request).
    """
    backend_for(workspace_root).delete(kill_request_key(run_id))


def touch_active_run_progress(workspace_root: Path, run_id: str) -> None:
    """Bump ``last_progress`` on one run's state file.

    Cheap helper for the orchestrator's per-event hook. Reads the
    existing record, replaces the timestamp field, atomically writes it
    back. If the record does not exist (e.g. the run already finished
    and the cleanup beat the event hook), the call is a no-op rather
    than an error — that race is benign.
    """
    backend = backend_for(workspace_root)
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
    # ``rounds[].matches[]`` entry. Default ``""`` so an old
    # active_tournament.json loads unchanged.
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
    buckets**, keyed by the verbatim :class:`~zicato.core.types.DriftCount`
    ``kind`` wire string (including ``custom:<judge_name>`` namespaced
    custom-judge kinds). Drift kinds with no events are absent from the
    mapping.
    """
    snapshot: dict[str, int] = {}
    for dc in getattr(profile, "drift_counts", ()) or ():
        kind = str(getattr(dc, "kind", ""))
        if not kind:
            continue
        snapshot[kind] = snapshot.get(kind, 0) + int(getattr(dc, "count", 0) or 0)
    return snapshot


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
        the fields. ``from_dict`` still accepts the legacy
        ``partial_parent_agg`` / ``partial_child_agg`` key names so an
        ``active_tournament.json`` written before this rename loads.
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
        completes; old readers ignore the field, and an old
        ``active_tournament.json`` with no ``projected`` key loads
        byte-identical.
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
    # ── NEW: the structure envelope (data-model §2.2) ──
    # All default to the gauntlet reading so an old active_tournament.json
    # loads unchanged. For ``structure == "gauntlet"`` the runner keeps
    # writing ``parent_generation_id`` / ``child_generation_id`` exactly
    # as today and MAY leave these empty; a non-gauntlet structure
    # populates them as the authoritative field set.
    structure: str = "gauntlet"
    structure_params: dict[str, Any] = field(default_factory=dict)
    competitors: list[dict[str, Any]] = field(default_factory=list)
    rounds: list[dict[str, Any]] = field(default_factory=list)
    standings: list[dict[str, Any]] = field(default_factory=list)
    # ── NEW: per-challenger proposing-step outcomes ──
    # The minting outcome for every challenger the proposer attempted this
    # round: ``{generation_id, status: "applied"|"rejected", reason, seed?}``.
    # Lets the dashboard render the candidate-generation step (the field
    # forming) live and post-hoc — a field where every challenger failed
    # reads as "N proposed · 0 applied". Defaults empty so an old
    # active_tournament.json (and the gauntlet path) loads byte-identical.
    field_status: list[dict[str, Any]] = field(default_factory=list)
    # ── NEW: live projected standing per in-flight competitor ──
    # ``{generation_id: {scalar, boards_done, boards_total, pass_rate}}`` —
    # the running aggregate over the boards settled so far for an in-flight
    # competitor, rewritten by the runner as each board unit settles. The
    # dashboard marks these "projected" (distinct from a settled scalar).
    # Defaults empty so an old active_tournament.json loads byte-identical.
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
            "standings": [dict(s) for s in self.standings],
            "field_status": [dict(f) for f in self.field_status],
            "projected": {str(k): dict(v) for k, v in self.projected.items()},
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ActiveTournament:
        # Accept the legacy `partial_parent_agg` / `partial_child_agg`
        # key names so an active_tournament.json written before the
        # champion/challenger rename still loads.
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
            standings=[dict(s) for s in d.get("standings", []) if isinstance(s, dict)],
            field_status=[dict(f) for f in d.get("field_status", []) if isinstance(f, dict)],
            projected={
                str(k): dict(v)
                for k, v in (d.get("projected") or {}).items()
                if isinstance(v, dict)
            },
        )


def read_active_tournament(workspace_root: Path) -> ActiveTournament | None:
    """Read the active-tournament JSON or return ``None`` if absent."""
    raw = backend_for(workspace_root).read_json(active_tournament_key())
    if raw is None:
        return None
    return ActiveTournament.from_dict(raw)


def write_active_tournament(workspace_root: Path, t: ActiveTournament) -> None:
    """Atomically write the active-tournament JSON."""
    ensure_runtime_dirs(workspace_root)
    backend_for(workspace_root).write_json(active_tournament_key(), t.to_dict())


def update_tournament_entry(workspace_root: Path, entry_id: str, side: str, **updates: Any) -> None:
    """Update one entry inside the active tournament.

    Reads the current tournament JSON, replaces the entry matching the
    ``(entry_id, side)`` pair with the per-field overrides supplied as
    keyword arguments, and atomically writes the result. If no
    tournament file exists, the call is a no-op (rather than an error) —
    the orchestrator may not have initialized one yet.

    Each board entry appears TWICE in :attr:`ActiveTournament.entries` —
    once with ``side="parent"`` and once with ``side="child"``. Matching
    on ``entry_id`` alone would land a parent-side transition on the
    child row (or both rows), so the ``side`` is part of the key. Only
    the FIRST row matching the pair is updated; if two rows somehow
    still share the same ``(entry_id, side)`` the later duplicates are
    left untouched rather than crashing — the call stays a benign no-op
    on a malformed tournament file.

    Special-cased fields are passed through :class:`dataclasses.replace`
    so unknown keyword names raise :class:`TypeError` — the call site
    catches typos immediately.
    """
    current = read_active_tournament(workspace_root)
    if current is None:
        return
    new_entries: list[ActiveTournamentEntry] = []
    updated = False
    for e in current.entries:
        if not updated and e.entry_id == entry_id and e.side == side:
            new_entries.append(replace(e, **updates))
            updated = True
        else:
            new_entries.append(e)
    new = replace(current, entries=new_entries)
    write_active_tournament(workspace_root, new)


def update_tournament_partial_aggregate(
    workspace_root: Path,
    *,
    champion_agg: dict[str, Any] | None = None,
    challenger_agg: dict[str, Any] | None = None,
) -> None:
    """Rewrite the active tournament's running partial-aggregate dicts.

    Called by the runner the instant a board unit settles, so a reader
    (the dashboard) sees a real server-side ``scalar`` accumulate as the
    tournament runs — rather than 0.00 until the whole round ends.

    Reads the current tournament JSON, replaces only the
    :attr:`ActiveTournament.partial_champion_agg` /
    :attr:`ActiveTournament.partial_challenger_agg` fields with whichever
    side(s) were supplied, and atomically writes the result. The
    per-entry status rows are untouched — this writer and
    :func:`update_tournament_entry` only ever read-modify-write the same
    file from the single orchestrator process, so the two never race.
    If no tournament file exists, the call is a no-op.
    """
    current = read_active_tournament(workspace_root)
    if current is None:
        return
    updates: dict[str, Any] = {}
    if champion_agg is not None:
        updates["partial_champion_agg"] = dict(champion_agg)
    if challenger_agg is not None:
        updates["partial_challenger_agg"] = dict(challenger_agg)
    if not updates:
        return
    write_active_tournament(workspace_root, replace(current, **updates))


def update_tournament_projected(
    workspace_root: Path,
    projected: dict[str, dict[str, Any]],
) -> None:
    """Merge live projected-standing rows into the active tournament.

    Mirrors :func:`update_tournament_partial_aggregate`: called by the
    runner the instant a board unit settles, so a reader (the dashboard)
    watches an in-flight competitor's *projected* standing
    (``scalar`` / ``boards_done`` / ``boards_total`` / ``pass_rate``)
    climb as its boards land — distinct from a settled scalar.

    Reads the current tournament JSON, MERGES the supplied per-generation
    rows onto :attr:`ActiveTournament.projected` (so concurrently-running
    sides each keep their own row), AND folds the just-written progress
    onto the live rung's per-lane ``live_progress`` (data-model §2.4), then
    atomically writes the result. A competitor settles out of the map
    naturally once the strategy folds a real settled scalar onto its
    standing; until then the projected row is the live read. If no
    tournament file exists, the call is a no-op.

    The ``live_progress`` fold is the keystone of the live-racing render:
    the selection DRIVER republishes the whole envelope only ONCE per
    scheduled batch (before the rung's matchups run), so the orchestrator's
    :func:`_overlay_projected_live_progress` runs at rung START when nothing
    has landed and never again that rung. Folding here — at the single point
    each board lands — refreshes the live rung continuously, independent of
    the driver's once-per-batch cadence, so the dashboard's rung no longer
    freezes at ``{boards_total, inflight}`` mid-rung. The fold mirrors the
    overlay's merge exactly so the live-arrived render is byte-identical to
    a fresh republish + overlay.

    ANTI-FLASH: a lane is mutated ONLY when a rounded value actually changes
    (the dashboard digest-gates renders on the rounded scalar + integer
    board counts), so a board that does not move any rounded lane value
    leaves the record byte-identical and the republish stays a no-op.

    CHAMPION BENCHMARK: a racing rung runs N concurrent
    champion-vs-challenger duels, each with its own scorer writing
    ``projected[champion_id]`` — last-writer-wins would thrash the champion
    lane (each duel aggregates the champion over only ITS boards). So the
    champion lane keeps the STRATEGY-seeded ``projected_scalar`` (the
    benchmark the strategy wrote from its own ``_scalars[champion]``) rather
    than the per-duel scalar, and its ``boards_done`` only ever GROWS to the
    most-progressed duel — never regresses to a less-progressed last writer.
    Per-challenger lanes are keyed by challenger id (one writer each) so
    they take the projected scalar directly.
    """
    if not projected:
        return
    current = read_active_tournament(workspace_root)
    if current is None:
        return
    merged = {str(k): dict(v) for k, v in current.projected.items()}
    for gid, row in projected.items():
        if isinstance(row, dict):
            merged[str(gid)] = dict(row)
    rounds, rounds_changed = _fold_projected_into_live_progress(
        current.rounds,
        projected,
        champion_ids=_champion_ids(current.competitors),
    )
    if not rounds_changed:
        # The merge of ``projected`` may still differ (it always takes the
        # latest row); but if neither the projected map nor any rounded lane
        # value moved, the write is a no-op the dashboard would digest-gate
        # away anyway. We still persist the merged projected map (cheap,
        # additive) so the standings overlay reads the latest scalar.
        write_active_tournament(workspace_root, replace(current, projected=merged))
        return
    write_active_tournament(workspace_root, replace(current, projected=merged, rounds=rounds))


def _champion_ids(competitors: list[dict[str, Any]]) -> set[str]:
    """Generation ids whose ``role`` marks them the champion (defender)."""
    return {
        str(c.get("generation_id", ""))
        for c in competitors
        if str(c.get("role", "")) == "champion" and c.get("generation_id")
    }


def _fold_projected_into_live_progress(
    rounds: list[dict[str, Any]],
    projected: dict[str, dict[str, Any]],
    *,
    champion_ids: set[str],
) -> tuple[list[dict[str, Any]], bool]:
    """Fold per-board ``projected`` rows onto the live rung ``live_progress``.

    Returns ``(rounds, changed)`` — ``rounds`` is a deep-ish copy with the
    fold applied in place, ``changed`` is ``True`` iff any lane's rounded
    value actually moved (the anti-flash gate). Mirrors the orchestrator's
    :func:`zicato.orchestrator._overlay_projected_live_progress` merge so
    the live-arrived render converges to a fresh republish + overlay:

    * ``boards_done`` ← the projected row's ``boards_done`` (champion lane:
      max across duels, never regress);
    * ``boards_total`` ← only when the strategy left the lane's total unset;
    * ``projected_scalar`` / ``projected`` ← the projected row's ``scalar``
      (challenger lanes only — the champion lane keeps its strategy-seeded
      benchmark to avoid per-duel thrash).

    A lane with no matching projected row is untouched (graceful fallback);
    a round whose matches carry no ``live_progress`` (every non-racing
    structure, and a racing rung before it is scheduled) is a no-op.
    """
    changed = False
    new_rounds: list[dict[str, Any]] = []
    for r in rounds:
        new_r = dict(r)
        matches = r.get("matches") or []
        new_matches: list[dict[str, Any]] = []
        for m in matches:
            lanes = m.get("live_progress")
            if not lanes:
                new_matches.append(m)
                continue
            new_m = dict(m)
            new_lanes: dict[str, Any] = {}
            for gid, lane in lanes.items():
                proj = projected.get(str(gid))
                if not isinstance(proj, dict):
                    new_lanes[gid] = lane
                    continue
                folded = dict(lane)
                lane_changed = _fold_one_lane(folded, proj, is_champion=str(gid) in champion_ids)
                changed = changed or lane_changed
                new_lanes[gid] = folded
            new_m["live_progress"] = new_lanes
            new_matches.append(new_m)
        new_r["matches"] = new_matches
        new_rounds.append(new_r)
    return new_rounds, changed


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


def clear_active_tournament(workspace_root: Path) -> None:
    """Remove the active-tournament JSON. Idempotent."""
    backend_for(workspace_root).delete(active_tournament_key())


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
