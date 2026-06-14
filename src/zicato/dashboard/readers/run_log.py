"""run_log — extracted from zicato.dashboard.state_reader (pure move)."""

from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path
from typing import Any

from zicato.dashboard.readers.paths import (
    WorkspacePaths,
    _read_json_value,
    to_snake,
)

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


# Envelope keys that are *not* the payload kind in a goldfive event.
_ENVELOPE_KEYS = frozenset(
    {
        "emittedAt",
        "emitted_at",
        "eventId",
        "event_id",
        "runId",
        "run_id",
        "sessionId",
        "session_id",
        "sequence",
        "seq",
        "kind",
        "payload",
    }
)


def _str_either(obj: dict[str, Any], camel: str, snake: str) -> str | None:
    val = obj.get(camel)
    if val is None:
        val = obj.get(snake)
    return val if isinstance(val, str) else None


def _extract_seq(obj: dict[str, Any]) -> int | None:
    val = obj.get("sequence")
    if val is None:
        val = obj.get("seq")
    if isinstance(val, int):
        return val
    if isinstance(val, str):
        try:
            return int(val.strip())
        except ValueError:
            return None
    return None


def _extract_ts(obj: dict[str, Any]) -> str | None:
    raw = obj.get("emittedAt")
    if raw is None:
        raw = obj.get("emitted_at")
    if isinstance(raw, str):
        return raw
    if isinstance(raw, dict):
        secs = raw.get("seconds")
        nanos = raw.get("nanos", 0)
        if not isinstance(secs, int | float | str):
            return None
        if not isinstance(nanos, int | float | str):
            nanos = 0
        try:
            secs_i = int(secs)
            nanos_i = int(nanos)
        except (TypeError, ValueError):
            return None
        try:
            dt = _dt.datetime.fromtimestamp(secs_i + nanos_i / 1_000_000_000, _dt.UTC)
        except (OverflowError, OSError, ValueError):
            return None
        return dt.isoformat().replace("+00:00", "Z")
    return None


def _kind_and_payload(obj: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
    kind = obj.get("kind")
    if isinstance(kind, str):
        payload = obj.get("payload")
        return to_snake(kind), payload if isinstance(payload, dict) else None
    for key, val in obj.items():
        if key in _ENVELOPE_KEYS:
            continue
        return to_snake(key), val if isinstance(val, dict) else None
    return "unknown", None


def _summarize(kind: str, payload: dict[str, Any] | None) -> str:
    if not isinstance(payload, dict):
        return kind

    def get(camel: str, snake: str) -> str | None:
        return _str_either(payload, camel, snake)

    detail: str | None = None
    if kind == "run_started":
        detail = get("goalSummary", "goal_summary")
    elif kind == "conversation_started":
        detail = get("conversationId", "conversation_id")
    elif kind == "goal_derived":
        goals = payload.get("goals")
        if isinstance(goals, list) and goals and isinstance(goals[0], dict):
            s = goals[0].get("summary")
            detail = s if isinstance(s, str) else None
    elif kind == "drift_detected":
        agent = get("currentAgentId", "current_agent_id")
        what = get("detail", "detail")
        detail = f"{agent}: {what}" if agent and what else (agent or what)
    elif kind == "steering_decision_made":
        agent = get("agentName", "agent_name")
        outcome = get("outcome", "outcome")
        detail = f"{agent}: {outcome}" if agent and outcome else (agent or outcome)
    elif kind == "reasoning_judge_invoked":
        detail = get("classification", "classification") or get("reason", "reason")
    elif kind == "task_progress":
        task = get("taskId", "task_id")
        frac = payload.get("fraction")
        if task and isinstance(frac, int | float):
            detail = f"{task} ({frac * 100:.0f}%)"
        elif task:
            detail = task
        elif isinstance(frac, int | float):
            detail = f"{frac * 100:.0f}%"
    elif kind in ("task_started", "task_completed"):
        detail = get("detail", "detail") or get("summary", "summary") or get("taskId", "task_id")
    elif kind == "task_transitioned":
        task = get("taskId", "task_id")
        to = get("toStatus", "to_status")
        detail = f"{task} -> {to}" if task and to else (task or to)
    elif kind == "delegation_observed":
        frm = get("fromAgent", "from_agent")
        to = get("toAgent", "to_agent")
        detail = f"{frm} -> {to}" if frm and to else None
    elif kind in (
        "agent_invocation_started",
        "agent_invocation_completed",
        "invocation_boundary_entered",
        "invocation_boundary_exited",
    ):
        detail = get("agentName", "agent_name")
    elif kind in ("goldfive_llm_call_start", "goldfive_llm_call_end"):
        detail = get("name", "name")
    elif kind == "pin_resolved":
        detail = get("agentName", "agent_name") or get("taskId", "task_id")

    if not detail:
        detail = (
            get("agentName", "agent_name")
            or get("detail", "detail")
            or get("summary", "summary")
            or get("reason", "reason")
            or get("taskId", "task_id")
        )

    if detail and detail.strip():
        detail = detail.strip()
        if len(detail) > 100:
            return f"{kind}: {detail[:100]}..."
        return f"{kind}: {detail}"
    return kind


def _parse_log_line(line: str) -> dict[str, Any] | None:
    line = line.strip()
    if not line:
        return None
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None
    kind, payload = _kind_and_payload(obj)
    return {
        "seq": _extract_seq(obj),
        "kind": kind,
        "ts": _extract_ts(obj),
        "summary": _summarize(kind, payload),
    }


def _tail_events(path: Path, limit: int) -> list[dict[str, Any]]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except OSError:
        return []
    records = [r for r in (_parse_log_line(ln) for ln in text.splitlines()) if r]
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
    for events in paths.epochs.rglob("events.jsonl"):
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

    Prefers the event's own ``sequence`` / ``seq``; an event with no
    sequence falls back to its line index so the run-log tail can still
    advance append-only against a producer that omits sequence numbers.
    """
    seq = _extract_seq(event)
    return seq if seq is not None else fallback_index


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
