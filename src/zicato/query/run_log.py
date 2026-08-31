"""The run-log tail: a run's ``events.jsonl`` records plus an append cursor.

Locates the events file for the newest active run, parses its records, and
returns the last ``limit`` of them with a monotone cursor, so a follower
appends what arrived instead of re-reading the file.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from zicato.query.paths import (
    WorkspacePaths,
    _read_json_value,
)
from zicato.telemetry.event_log import EventRecord, read_event_log
from zicato.workspace import is_events_file

# ---------------------------------------------------------------------------
# Run-log tail
# ---------------------------------------------------------------------------


def clamp_run_log_limit(requested: int | None) -> int:
    if requested is None or requested <= 0:
        return RUN_LOG_DEFAULT_LIMIT
    return min(requested, RUN_LOG_MAX_LIMIT)


# Default / ceiling for the ``/api/run-log`` ``?limit=`` query — these
# match the Rust supervisor's ``run_log::DEFAULT_LIMIT`` / ``MAX_LIMIT``.
RUN_LOG_DEFAULT_LIMIT = 40


RUN_LOG_MAX_LIMIT = 500


#: What a record's kind reads as when its line carries no payload — an
#: envelope with nothing under it. The panel still shows the line.
_UNKNOWN_KIND = "unknown"


def _summarize(kind: str, payload: dict[str, Any]) -> str:
    if not payload:
        return kind

    def get(name: str) -> str | None:
        value = payload.get(name)
        return value if isinstance(value, str) else None

    detail: str | None = None
    if kind == "run_started":
        detail = get("goal_summary")
    elif kind == "conversation_started":
        detail = get("conversation_id")
    elif kind == "goal_derived":
        goals = payload.get("goals")
        if isinstance(goals, list) and goals and isinstance(goals[0], dict):
            s = goals[0].get("summary")
            detail = s if isinstance(s, str) else None
    elif kind == "drift_detected":
        agent = get("current_agent_id")
        what = get("detail")
        detail = f"{agent}: {what}" if agent and what else (agent or what)
    elif kind == "steering_decision_made":
        agent = get("agent_name")
        outcome = get("outcome")
        detail = f"{agent}: {outcome}" if agent and outcome else (agent or outcome)
    elif kind == "reasoning_judge_invoked":
        detail = get("classification") or get("reason")
    elif kind == "task_progress":
        task = get("task_id")
        frac = payload.get("fraction")
        if task and isinstance(frac, int | float):
            detail = f"{task} ({frac * 100:.0f}%)"
        elif task:
            detail = task
        elif isinstance(frac, int | float):
            detail = f"{frac * 100:.0f}%"
    elif kind in ("task_started", "task_completed"):
        detail = get("detail") or get("summary") or get("task_id")
    elif kind == "task_transitioned":
        task = get("task_id")
        to = get("to_status")
        detail = f"{task} -> {to}" if task and to else (task or to)
    elif kind == "delegation_observed":
        frm = get("from_agent")
        to = get("to_agent")
        detail = f"{frm} -> {to}" if frm and to else None
    elif kind in (
        "agent_invocation_started",
        "agent_invocation_completed",
        "invocation_boundary_entered",
        "invocation_boundary_exited",
    ):
        detail = get("agent_name")
    elif kind in ("goldfive_llm_call_start", "goldfive_llm_call_end"):
        detail = get("name")
    elif kind == "pin_resolved":
        detail = get("agent_name") or get("task_id")

    if not detail:
        detail = (
            get("agent_name") or get("detail") or get("summary") or get("reason") or get("task_id")
        )

    if detail and detail.strip():
        detail = detail.strip()
        if len(detail) > 100:
            return f"{kind}: {detail[:100]}..."
        return f"{kind}: {detail}"
    return kind


def _log_record(record: EventRecord) -> dict[str, Any]:
    kind = record.case or _UNKNOWN_KIND
    return {
        "seq": record.sequence,
        "kind": kind,
        "ts": record.emitted_at,
        "summary": _summarize(kind, record.payload),
    }


def _tail_events(path: Path, limit: int) -> list[dict[str, Any]]:
    records = [_log_record(r) for r in read_event_log(path).records]
    if len(records) > limit:
        records = records[len(records) - limit :]
    return records


def _newest_active_run_events(paths: WorkspacePaths) -> Path | None:
    runs_dir = paths.active_runs_dir
    if not runs_dir.is_dir():
        return None
    newest: tuple[float, Path] | None = None
    for entry in runs_dir.iterdir():
        if entry.suffix != ".json":
            continue
        try:
            mtime = entry.stat().st_mtime
        except OSError:
            mtime = 0.0
        if newest is None or mtime > newest[0]:
            newest = (mtime, entry)
    if newest is None:
        return None
    run = _read_json_value(newest[1])
    if not isinstance(run, dict):
        return None
    events_path = run.get("events_jsonl_path")
    return Path(events_path) if isinstance(events_path, str) and events_path else None


def _newest_epoch_events(paths: WorkspacePaths) -> Path | None:
    if not paths.epochs.is_dir():
        return None
    newest: tuple[float, Path] | None = None
    for events in paths.epochs.rglob("events*.jsonl"):
        if not is_events_file(events):
            continue
        try:
            mtime = events.stat().st_mtime
        except OSError:
            mtime = 0.0
        if newest is None or mtime > newest[0]:
            newest = (mtime, events)
    return newest[1] if newest is not None else None


def locate_events_file(paths: WorkspacePaths) -> Path | None:
    """The ``events.jsonl`` the run-log tails — newest active run first."""
    candidate = _newest_active_run_events(paths)
    if candidate is not None and candidate.exists():
        return candidate
    return _newest_epoch_events(paths)


def _event_cursor(event: dict[str, Any], fallback_index: int) -> int:
    """A monotone cursor for one run-log event.

    Prefers the event's own sequence number; an event with none falls back
    to its line index so the run-log tail can still advance append-only
    against a producer that omits sequence numbers.
    """
    seq = event.get("seq")
    return seq if isinstance(seq, int) else fallback_index


def build_run_log(paths: WorkspacePaths, limit: int, after: int | None = None) -> dict[str, Any]:
    """``GET /api/run-log`` body — run-log events plus an append cursor.

    Returns ``{"events": [...], "cursor": <int|None>, "events_path": str}``.

    * With ``after`` ``None`` the body is the last ``limit`` parseable
      events (the initial paint).
    * With ``after`` set, only events whose cursor is strictly greater
      than ``after`` are returned — the dashboard appends these to its
      log tail rather than re-rendering the whole list, which is what
      stops the visible flashing.

    ``cursor`` is the largest cursor in the file (``None`` when empty);
    the client passes it back as the next ``after``.
    """
    path = locate_events_file(paths)
    all_events = _tail_events(path, RUN_LOG_MAX_LIMIT) if path is not None else []
    cursor: int | None = None
    if all_events:
        cursor = max(_event_cursor(ev, i) for i, ev in enumerate(all_events))

    if after is None:
        events = all_events[-limit:] if len(all_events) > limit else all_events
    else:
        events = [ev for i, ev in enumerate(all_events) if _event_cursor(ev, i) > after]
        if len(events) > limit:
            events = events[-limit:]

    return {
        "events": events,
        "cursor": cursor,
        "events_path": str(path) if path is not None else None,
    }
