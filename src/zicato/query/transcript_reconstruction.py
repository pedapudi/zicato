"""Pure, tolerant conversation reconstruction from one run's event file.

Two source formats reach this module, and :func:`reconstruct_transcript`
tells them apart by the first line of the file.

A **Goldfive/ADK ``events.jsonl``** is what a system under test emits. Both
wire shapes of such an event are accepted. The payload case, its field names
and the emission timestamp all come from :mod:`zicato.telemetry.event_log`, so
a turn is built from the same reading of a line that every other consumer of
the file gets. The conversation is inferred from surrounding observability
events, and its execution fidelity is whatever those events state.

A **Foe ``episode.jsonl``** is what a proposal episode writes, and it is the
only source a proposer transcript is served from. :mod:`zicato.query.foe_episode`
reads it and states the derived-message rule that turns its events into the
message list each request carried. Every tool call in such a log has exactly
one result matched by ``call_id``, and every request records the messages it
sent, so a Foe log always reconstructs at ``fidelity: exact``. The guarantee is
a property of the format rather than a judgement about one file.

Malformed lines are skipped in both formats so growing runs remain readable.
"""

from __future__ import annotations

import datetime as _dt
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from zicato.query.foe_episode import (
    EpisodeEvent,
    EpisodeLog,
    inner_call_ids,
    is_episode_log,
    message_from,
    read_episode_log,
)
from zicato.telemetry.event_log import EventRecord, read_event_log

__all__ = ["Annotation", "Transcript", "Turn", "reconstruct_transcript"]


@dataclass
class Turn:
    """One step; ``source_index`` is its live cursor and ``run_index`` its run group."""

    seq: int | None = None
    ts: str | None = None
    agent: str | None = None
    role: str = "agent"
    kind: str = ""
    text: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    tool_results: list[dict[str, Any]] = field(default_factory=list)
    run_id: str | None = None
    run_index: int = 1
    source_index: int = -1
    activity_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "ts": self.ts,
            "agent": self.agent,
            "role": self.role,
            "kind": self.kind,
            "text": self.text,
            "tool_calls": [dict(tc) for tc in self.tool_calls],
            "tool_results": [dict(tr) for tr in self.tool_results],
            "run_id": self.run_id,
            "run_index": self.run_index,
            "source_index": self.source_index,
            "activity_ids": list(self.activity_ids),
        }


@dataclass
class Annotation:
    """A margin note anchored near a turn in the transcript.

    Annotations are the steering / observability layer: drift detections,
    steering decisions, judge verdicts, plan revisions. They do not carry
    conversation content; they explain what the framework did alongside
    it. ``anchor_seq`` is the sequence of the nearest preceding turn so
    the dashboard can pin the note to the conversation flow.

    ``source_index`` is the parsed-event index this annotation was minted
    from — the same append cursor :class:`Turn` carries, so the
    live-follow delta filters both lists on one comparison.
    """

    kind: str = ""
    ts: str | None = None
    summary: str = ""
    anchor_seq: int | None = None
    detail: dict[str, Any] = field(default_factory=dict)
    source_index: int = -1

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "ts": self.ts,
            "summary": self.summary,
            "anchor_seq": self.anchor_seq,
            "detail": dict(self.detail),
            "source_index": self.source_index,
        }


@dataclass
class Transcript:
    """An ordered conversation reconstruction for one run."""

    turns: list[Turn] = field(default_factory=list)
    annotations: list[Annotation] = field(default_factory=list)
    run_id: str | None = None
    event_count: int = 0
    complete: bool = False
    execution: dict[str, Any] = field(
        default_factory=lambda: {
            "fidelity": "unavailable",
            "nodes": [],
            "root_ids": [],
            "unresolved_ids": [],
        }
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "event_count": self.event_count,
            "complete": self.complete,
            "turns": [t.to_dict() for t in self.turns],
            "annotations": [a.to_dict() for a in self.annotations],
            "execution": self.execution,
        }


# Kinds that carry an invocation_id and therefore state execution structure.
_EXECUTION_KINDS = {
    "agent_invocation_started",
    "agent_invocation_completed",
    "invocation_boundary_exited",
    "invocation_cancelled",
}


def _execution_topology(events: tuple[EventRecord, ...]) -> dict[str, Any]:
    """Build only invocation relationships and statuses stated by canonical events.

    Statuses are last-writer-wins in stream order: a completion event marks
    ``completed``; a boundary exit restates the terminal reason (``completed``,
    ``cancelled``, anything else ``failed`` with the reason surfaced); an
    explicit cancel marks ``cancelled``. Names come only from the invocation
    start / completion events — the boundary events attribute nested
    invocations to the host agent, so their ``agent_name`` is never consumed.
    """

    nodes: dict[str, dict[str, Any]] = {}
    for source_index, event in enumerate(events):
        kind, payload = event.case, event.payload
        if kind not in _EXECUTION_KINDS:
            continue
        node_id = payload.get("invocation_id")
        if not isinstance(node_id, str) or not node_id:
            continue
        node = nodes.setdefault(
            node_id,
            {
                "node_id": node_id,
                "kind": "agent",
                "parent_id": None,
                "name": None,
                "status": "running",
                "start_source_index": source_index,
                "summary": "",
                "fidelity": "exact",
            },
        )
        if kind == "agent_invocation_started":
            parent = payload.get("parent_invocation_id")
            node["parent_id"] = parent if isinstance(parent, str) and parent else None
            name = payload.get("agent_name")
            node["name"] = name if isinstance(name, str) and name else None
            node["start_source_index"] = source_index
        elif kind == "agent_invocation_completed":
            node["status"] = "completed"
            summary = _clip(str(payload.get("summary") or ""))
            if summary:
                node["summary"] = summary
            if node["name"] is None:
                name = payload.get("agent_name")
                node["name"] = name if isinstance(name, str) and name else None
        elif kind == "invocation_boundary_exited":
            reason = str(payload.get("reason") or "completed")
            if reason in ("completed", "cancelled"):
                node["status"] = reason
            else:
                node["status"] = "failed"
                if not node["summary"]:
                    node["summary"] = _clip(reason)
        else:  # invocation_cancelled
            node["status"] = "cancelled"
            if not node["summary"]:
                node["summary"] = _clip(str(payload.get("detail") or payload.get("reason") or ""))

    known = set(nodes)
    unresolved_ids: list[str] = []
    for node_id, node in nodes.items():
        parent = node["parent_id"]
        seen = {node_id}
        while parent in known and parent not in seen:
            seen.add(parent)
            parent = nodes[parent]["parent_id"]
        if parent is not None and (parent not in known or parent in seen):
            unresolved_ids.append(node_id)
    for node_id in unresolved_ids:
        nodes[node_id]["fidelity"] = "unresolved"
    execution = {
        "fidelity": "unavailable",
        "nodes": list(nodes.values()),
        "root_ids": [],
        "unresolved_ids": unresolved_ids,
    }
    _finalize_execution(execution)
    return execution


def _finalize_execution(execution: dict[str, Any]) -> None:
    """Order nodes chronologically and restate roots and overall fidelity."""

    nodes = execution["nodes"]
    nodes.sort(key=lambda node: node["start_source_index"])
    execution["root_ids"] = [n["node_id"] for n in nodes if n["parent_id"] is None]
    if not nodes:
        execution["fidelity"] = "unavailable"
    elif execution["unresolved_ids"] or any(n["fidelity"] == "turn" for n in nodes):
        execution["fidelity"] = "partial"
    else:
        execution["fidelity"] = "exact"


# Event kinds that become margin annotations rather than conversation turns.
_ANNOTATION_KINDS = {
    "drift_detected": "drift",
    "steering_decision_made": "steering",
    "reasoning_judge_invoked": "judge",
    "judgement_emitted": "judge",
    "plan_revised": "plan",
    "plan_submitted": "plan",
    "ladder_transition_decided": "steering",
    "detector_dispatch_ordered": "steering",
    "policy_applied": "steering",
    "retry_budget_spent": "steering",
    "refine_attempted": "steering",
}

_SYSTEM_KINDS = {
    "run_started",
    "run_completed",
    "run_aborted",
    "conversation_started",
    "conversation_ended",
}

_TERMINAL_KINDS = {"run_completed", "run_aborted", "conversation_ended"}

_TRUNC = " … [truncated]"


def _clip(text: str, limit: int = 8000) -> str:
    """Bound a message body so a serialized transcript stays sane."""

    if len(text) <= limit:
        return text
    return text[:limit] + _TRUNC


def _drift_summary(payload: dict[str, Any]) -> str:
    kind = str(payload.get("kind") or "drift").replace("DRIFT_KIND_", "").lower()
    severity = str(payload.get("severity") or "").replace("DRIFT_SEVERITY_", "").lower()
    detail = str(payload.get("detail") or "")
    head = f"{kind}/{severity}".strip("/")
    return f"{head}: {detail}".strip(": ") if detail else head


def _steering_summary(payload: dict[str, Any]) -> str:
    detector = str(payload.get("detector_name") or "detector")
    outcome = str(payload.get("outcome") or "")
    reason = str(payload.get("reason") or "")
    head = f"{detector} → {outcome}".strip(" →")
    return f"{head}: {reason}" if reason else head


def _judge_summary(kind: str, payload: dict[str, Any]) -> str:
    if kind == "judgement_emitted":
        name = str(payload.get("judge_name") or "judge")
        verdict = str(payload.get("verdict_kind") or "")
        detail = str(payload.get("detail") or "")
        head = f"{name} ({verdict})".strip(" ()")
        return f"{head}: {detail}" if detail else head
    # reasoning_judge_invoked
    classification = str(payload.get("classification") or payload.get("severity") or "")
    on_task = payload.get("on_task")
    verdict = classification or ("on_task" if on_task else "off_task")
    reason = str(payload.get("reason") or "")
    head = f"reasoning_judge → {verdict}"
    return f"{head}: {reason}" if reason else head


def _plan_summary(kind: str, payload: dict[str, Any]) -> str:
    raw_plan = payload.get("plan")
    plan: dict[str, Any] = raw_plan if isinstance(raw_plan, dict) else {}
    raw_tasks = plan.get("tasks")
    tasks: list[Any] = raw_tasks if isinstance(raw_tasks, list) else []
    titles = [str(t.get("title") or t.get("id") or "") for t in tasks if isinstance(t, dict)]
    rev = payload.get("revision_index")
    if rev is None and isinstance(plan, dict):
        rev = plan.get("revision_index")
    reason = str(payload.get("reason") or "")
    head = f"plan rev{rev} ({len(titles)} task(s))" if rev is not None else "plan"
    if titles:
        head += ": " + ", ".join(t for t in titles if t)
    return f"{head} — {reason}" if reason else head


def _annotation_summary(kind: str, payload: dict[str, Any]) -> str:
    if kind == "drift_detected":
        return _drift_summary(payload)
    if kind in (
        "steering_decision_made",
        "ladder_transition_decided",
        "detector_dispatch_ordered",
        "policy_applied",
        "retry_budget_spent",
        "refine_attempted",
    ):
        return _steering_summary(payload)
    if kind in ("reasoning_judge_invoked", "judgement_emitted"):
        return _judge_summary(kind, payload)
    if kind in ("plan_revised", "plan_submitted"):
        return _plan_summary(kind, payload)
    # Fallback: any short string field on the payload.
    for key in ("reason", "detail", "summary", "outcome"):
        val = payload.get(key)
        if isinstance(val, str) and val:
            return _clip(val, 512)
    return kind


def _agent_of(payload: dict[str, Any]) -> str | None:
    """Pull the agent name a payload is attributed to, if any."""

    for key in (
        "agent_name",
        "subject_agent_id",
        "target_agent_id",
        "current_agent_id",
        "from_agent",
    ):
        val = payload.get(key)
        if isinstance(val, str) and val:
            return val
    return None


def _conversation_event(
    kind: str, payload: dict[str, Any]
) -> tuple[str, str, str, dict[str, Any] | None, dict[str, Any] | None] | None:
    """Map a conversation-bearing event to turn material.

    Returns ``(role, agent, text, tool_call, tool_result)`` or ``None``
    when the event does not contribute conversation content. ``tool_call``
    / ``tool_result`` are dicts (or ``None``) to append to the turn.

    goldfive does not emit explicit "assistant message" events; the
    conversation content lives inside the surrounding observability
    events. We reconstruct it from:

    * ``goal_derived`` — the user-facing goal list (a user turn).
    * ``run_started`` — the raw user request (a user/system turn).
    * ``task_completed`` — the substantive agent output for a task.
    * ``goldfive_llm_call_start`` / ``goldfive_llm_call_end`` — the
      reasoning / decision text the framework saw and produced.
    * ``delegation_observed`` — a coordinator invoking a sub-agent: a
      tool call whose result is the sub-agent's invocation completion.
    * ``agent_invocation_completed`` — the sub-agent's final summary.
    """

    if kind == "run_started":
        text = str(payload.get("goal_summary") or "")
        return ("user", "", text, None, None)

    if kind == "goal_derived":
        goals = payload.get("goals")
        lines: list[str] = []
        if isinstance(goals, list):
            for goal in goals:
                if isinstance(goal, dict):
                    lines.append(str(goal.get("summary") or goal.get("id") or ""))
        return ("user", "", "\n".join(line for line in lines if line), None, None)

    if kind in ("run_completed", "run_aborted", "conversation_ended"):
        text = str(payload.get("outcome_summary") or payload.get("reason") or "")
        return ("system", "", text, None, None)

    if kind == "conversation_started":
        return ("system", "", "conversation started", None, None)

    if kind == "task_completed":
        text = str(payload.get("summary") or "")
        return ("agent", "", text, None, None)

    if kind == "delegation_observed":
        from_agent = str(payload.get("from_agent") or "")
        to_agent = str(payload.get("to_agent") or "")
        args_raw = payload.get("tool_args_json")
        args: Any = args_raw
        if isinstance(args_raw, str) and args_raw:
            try:
                args = json.loads(args_raw)
            except json.JSONDecodeError:
                args = args_raw
        call = {
            "name": to_agent or "delegate",
            "args": args if args is not None else {},
            "task_id": payload.get("task_id"),
        }
        return ("agent", from_agent, "", call, None)

    if kind == "agent_invocation_completed":
        agent = str(payload.get("agent_name") or "")
        text = str(payload.get("summary") or "")
        return ("agent", agent, text, None, None)

    if kind == "goldfive_llm_call_start":
        # Carries the reasoning / input the framework fed to an LLM. For a
        # judge_* span this is the agent's chain-of-thought; surfacing it
        # gives the transcript its "thinking" content.
        text = str(payload.get("input_preview") or "")
        return ("agent", "", text, None, None)

    if kind == "goldfive_llm_call_end":
        text = str(payload.get("decision_summary") or payload.get("output_preview") or "")
        return ("agent", "", text, None, None)

    return None


def reconstruct_transcript(events_path: Path, *, partial_ok: bool = True) -> Transcript:
    """Reconstruct an ordered conversation transcript from ``events_path``.

    The file's own first line selects the reader. A Foe ``episode.jsonl``
    opens with ``episode/start`` at ``seq`` 0 and is reconstructed by
    :func:`_reconstruct_episode`; anything else is read as a Goldfive/ADK
    event stream by :func:`_reconstruct_event_stream`. The two produce the
    same :class:`Transcript` shape, so every caller and every rendering
    surface handles a proposal episode and a system-under-test run alike.

    A missing file yields an empty :class:`Transcript` from the ADK path
    (never raises), which is the same-shaped answer both readers degrade to.
    ``partial_ok`` reaches only the ADK path: an episode already reports
    ``complete`` false for a torn final line whatever the caller asks for.
    """
    path = Path(events_path)
    if is_episode_log(path):
        return _reconstruct_episode(read_episode_log(path))
    return _reconstruct_event_stream(path, partial_ok=partial_ok)


def _episode_agent(start: EpisodeEvent | None) -> str | None:
    """The contract name the episode ran under, which names its speaker."""
    if start is None:
        return None
    contract = start.data.get("contract")
    name = contract.get("name") if isinstance(contract, dict) else None
    return name if isinstance(name, str) and name else None


def _episode_ts(event: EpisodeEvent) -> str | None:
    """One event's ``time`` as RFC-3339 in UTC, the spelling every turn uses."""
    if event.time is None:
        return None
    try:
        moment = _dt.datetime.fromtimestamp(event.time / 1000, _dt.UTC)
    except (OSError, OverflowError, ValueError):
        return None
    return moment.isoformat().replace("+00:00", "Z")


def _outcome_text(outcome: Any) -> str:
    """One line stating how the episode ended, from its ``episode/end``."""
    if not isinstance(outcome, dict):
        return ""
    kind = str(outcome.get("kind") or "")
    for key in ("message", "error", "code", "limit"):
        detail = outcome.get(key)
        if isinstance(detail, str) and detail:
            return f"{kind}: {detail}" if kind else detail
    value = outcome.get("value")
    if isinstance(value, str) and value:
        return f"{kind}: {value}" if kind else value
    return kind


def _outcome_status(outcome: Any) -> str:
    """An episode outcome as a node status: only ``completed`` completes."""
    kind = outcome.get("kind") if isinstance(outcome, dict) else None
    return "completed" if kind == "completed" else "failed"


def _episode_topology(events: tuple[EpisodeEvent, ...], episode_id: str) -> dict[str, Any]:
    """The episode's activity tree, every edge stated by an event.

    The episode itself is the root; a model-issued tool call hangs off it,
    and an inner dispatch off the call that composed it (``outer_call_id``).
    Nothing here infers a parent, and every obligation a Foe log opens is
    closed in the same log, so every node is ``exact`` and nothing is
    unresolved.
    """
    nodes: dict[str, dict[str, Any]] = {}
    end = next((event for event in events if event.type == "episode/end"), None)
    start = next((event for event in events if event.type == "episode/start"), None)
    nodes[episode_id] = {
        "node_id": episode_id,
        "kind": "agent",
        "parent_id": None,
        "name": _episode_agent(start),
        "status": "running" if end is None else _outcome_status(end.data.get("outcome")),
        "start_source_index": 0,
        "summary": _clip(_outcome_text(end.data.get("outcome")) if end else "", 512),
        "fidelity": "exact",
    }
    for source_index, event in enumerate(events):
        data = event.data
        if event.type in ("assistant/message", "tool/inner-call"):
            calls = data.get("tool_calls") if event.type == "assistant/message" else [data]
            parent = (
                episode_id
                if event.type == "assistant/message"
                else _tool_node_id(episode_id, str(data.get("outer_call_id") or ""))
            )
            for call in calls if isinstance(calls, list) else []:
                if not isinstance(call, dict):
                    continue
                call_id = str(call.get("id") or call.get("call_id") or "")
                if not call_id:
                    continue
                nodes[_tool_node_id(episode_id, call_id)] = {
                    "node_id": _tool_node_id(episode_id, call_id),
                    "kind": "tool",
                    "parent_id": parent,
                    "name": str(call.get("name") or "tool"),
                    "status": "running",
                    "start_source_index": source_index,
                    "summary": "",
                    "fidelity": "exact",
                }
        elif event.type == "tool/result":
            node = nodes.get(_tool_node_id(episode_id, str(data.get("call_id") or "")))
            if node is not None:
                node["status"] = "failed" if data.get("is_error") is True else "completed"
                node["summary"] = _clip(str(data.get("subject") or ""), 512)

    execution = {
        "fidelity": "unavailable",
        "nodes": list(nodes.values()),
        "root_ids": [],
        "unresolved_ids": [],
    }
    _finalize_execution(execution)
    return execution


def _tool_node_id(episode_id: str, call_id: str) -> str:
    return f"tool:{episode_id}:{call_id}"


def _seed_summary(origin: Any, source_id: str) -> str:
    """What the copied prefix above this boundary was copied from.

    ``fork_origin`` names the source episode and the ``seq`` in that source
    log the copy stopped at. A log seeded by a writer that recorded no origin
    still gets the boundary; it just cannot say where the prefix came from.
    """
    source = source_id or "another episode"
    at = origin.get("seq") if isinstance(origin, dict) else None
    where = f" up to its seq {at}" if isinstance(at, int) and not isinstance(at, bool) else ""
    return f"end of the prefix copied from {source}{where}; the episode's own events follow"


def _reconstruct_episode(log: EpisodeLog) -> Transcript:
    """Project one Foe episode log onto the shared :class:`Transcript` shape.

    Turns follow the derived-message rule
    (:func:`zicato.query.foe_episode.message_from`). A request contributes the
    user turn built from the inbox items it consumed. An assistant response
    contributes an agent turn carrying its text and its tool calls. Each tool
    result lands on the turn that issued the call it answers, and the episode
    outcome closes the transcript as a system turn.

    A log seeded from another episode — a fork or a replay — carries the
    copied prefix before its ``seed/end`` event. Those turns are attributed to
    the episode they were copied from, in their own run group, and the
    boundary itself becomes a margin annotation naming the source and the
    ``seq`` the copy stopped at. What follows is the live episode's own work.

    Every node is ``exact``: the format gives each tool call exactly one
    ``call_id``-matched result and makes each request record the messages it
    sent, so nothing here is inferred and nothing is left unresolved.
    """
    events = log.events
    start = next((event for event in events if event.type == "episode/start"), None)
    episode_id = str(start.data.get("id") or "") if start is not None else ""
    agent = _episode_agent(start)
    # The copied prefix, if any, is everything below the seed boundary.
    seed_seq = next((e.seq for e in events if e.type == "seed/end"), None)
    origin = start.data.get("fork_origin") if start is not None else None
    source_id = str(origin.get("episode_id") or "") if isinstance(origin, dict) else ""

    transcript = Transcript(run_id=episode_id or None, event_count=len(events))
    transcript.execution = _episode_topology(events, episode_id or "episode")

    inbox: dict[int, EpisodeEvent] = {}
    inner = inner_call_ids(events)
    issuers: dict[str, Turn] = {}
    last_seq: int | None = None

    for source_index, event in enumerate(events):
        if event.type == "inbox/item":
            inbox[event.seq] = event
        seeded = seed_seq is not None and event.seq < seed_seq
        message = message_from(event, inbox, inner)
        if message is None:
            if event.type == "seed/end":
                transcript.annotations.append(
                    Annotation(
                        kind="seed",
                        ts=_episode_ts(event),
                        summary=_seed_summary(origin, source_id),
                        anchor_seq=last_seq,
                        detail={"event_kind": event.type, "fork_origin": origin},
                        source_index=source_index,
                    )
                )
            continue

        if message["role"] == "tool":
            # A result whose call opened in this log lands on the turn that
            # issued it; the runtime's own settlement results name no call,
            # so they land on the turn they follow.
            target = issuers.get(str(message["call_id"])) or (
                transcript.turns[-1] if transcript.turns else None
            )
            if target is not None:
                target.tool_results.append(
                    {
                        "call_id": message["call_id"],
                        "name": message["name"],
                        "result": _clip(str(message["rendered"])),
                        "is_error": message["is_error"],
                    }
                )
                target.source_index = max(target.source_index, source_index)
            continue

        turn = Turn(
            seq=event.seq,
            ts=_episode_ts(event),
            agent=agent if message["role"] == "assistant" else None,
            role="agent" if message["role"] == "assistant" else "user",
            kind=event.type,
            run_id=(source_id or None) if seeded else (episode_id or None),
            run_index=1 if seeded else 2 if seed_seq is not None else 1,
            source_index=source_index,
        )
        if message["role"] == "user":
            turn.text = _clip(_content_text(message["content"]))
        else:
            turn.text = _clip(str(message["text"]))
            for call in message["tool_calls"]:
                call_id = str(call.get("id") or "")
                turn.tool_calls.append(
                    {
                        "id": call_id,
                        "name": str(call.get("name") or "tool"),
                        "args": call.get("args"),
                    }
                )
                turn.activity_ids.append(_tool_node_id(episode_id or "episode", call_id))
                if call_id:
                    issuers[call_id] = turn
        transcript.turns.append(turn)
        last_seq = event.seq

    end = next((event for event in events if event.type == "episode/end"), None)
    if end is not None:
        transcript.turns.append(
            Turn(
                seq=end.seq,
                ts=_episode_ts(end),
                role="system",
                kind=end.type,
                text=_clip(_outcome_text(end.data.get("outcome"))),
                run_id=episode_id or None,
                run_index=2 if seed_seq is not None else 1,
                source_index=len(events) - 1,
            )
        )

    transcript.turns = [t for t in transcript.turns if t.text or t.tool_calls or t.tool_results]
    transcript.complete = end is not None and log.last_line_ok
    return transcript


def _content_text(blocks: Any) -> str:
    """The text of one message's content blocks, blank-line separated."""
    parts: list[str] = []
    for block in blocks if isinstance(blocks, list) else []:
        text = block.get("text") if isinstance(block, dict) else None
        if isinstance(text, str) and text:
            parts.append(text)
    return "\n\n".join(parts)


def _reconstruct_event_stream(events_path: Path, *, partial_ok: bool = True) -> Transcript:
    """Reconstruct an ordered conversation transcript from an ADK event stream.

    ``events_path`` is a goldfive ``JSONLPersistenceSink`` file (one
    ``goldfive.v1.Event`` per line). The result groups raw events into
    conversational :class:`Turn` objects and surfaces drift / steering /
    judge events as margin :class:`Annotation` objects.

    Parameters
    ----------
    events_path:
        Path to the ``events.jsonl`` file. A missing file yields an empty
        :class:`Transcript` (never raises).
    partial_ok:
        When ``True`` (default) a growing / in-progress file is fine: a
        truncated final line is skipped rather than treated as an error,
        and :attr:`Transcript.complete` reports whether a terminal event
        was seen. When ``False`` the same parsing happens but a truncated
        final line additionally forces ``complete = False``.

    Notes
    -----
    The function never raises on malformed input — a bad line is skipped.

    Multi-run files (``multi_turn_emulated`` board entries spawn N
    goldfive runs into one events stream) are grouped by ``runId``
    BEFORE the within-run sort. The groups are ordered by the minimum
    ``emittedAt`` across each group — earliest run first, chronologically
    — and then within each group events without ``sequence`` (the
    sink-emitted ``conversation_started`` lifecycle frame) sort FIRST in
    timestamp order, with the sequenced events following in ``sequence``
    order. ``emitted_at`` breaks ties among same-sequence events;
    insertion order is the final fallback for events missing both
    fields. Every emitted :class:`Turn` carries the 1-based ``run_index``
    of its group, which lets the renderer paint a visible boundary
    between runs in a multi-run transcript.

    Single-run files (the common case) collapse to a single group with
    ``run_index == 1`` on every turn; the per-run-id grouping is a no-op
    relative to the prior single-stream behaviour.
    """

    log = read_event_log(Path(events_path))
    events, last_line_ok = log.records, log.last_line_ok

    transcript = Transcript()
    transcript.event_count = len(events)
    transcript.execution = _execution_topology(events)
    agent_ids = {node["node_id"] for node in transcript.execution["nodes"]}
    if not events:
        # Missing / empty file → empty transcript. An empty file from a
        # run that has not emitted anything yet is "not complete".
        transcript.complete = False
        return transcript

    # Group before sorting because each run restarts its sequence at zero.
    groups: dict[str | None, list[tuple[int, EventRecord]]] = {}
    insertion_order: list[str | None] = []
    for idx, event in enumerate(events):
        rid: str | None = event.run_id or None
        bucket = groups.get(rid)
        if bucket is None:
            bucket = []
            groups[rid] = bucket
            insertion_order.append(rid)
        bucket.append((idx, event))

    def _min_ts_of(bucket: list[tuple[int, EventRecord]]) -> str:
        best: str | None = None
        for _idx, event in bucket:
            ts = event.emitted_at
            if ts is None:
                continue
            if best is None or ts < best:
                best = ts
        return best or ""

    def _within_group_key(
        item: tuple[int, EventRecord],
    ) -> tuple[int, int, str, int]:
        # Sequence-less lifecycle frames precede the sequenced stream.
        idx, event = item
        seq = event.sequence
        ts = event.emitted_at or ""
        return (
            0 if seq is None else 1,
            seq if seq is not None else 0,
            ts,
            idx,
        )

    insertion_index: dict[str | None, int] = {rid: i for i, rid in enumerate(insertion_order)}
    ordered_run_ids: list[str | None] = sorted(
        groups.keys(),
        key=lambda rid: (_min_ts_of(groups[rid]), insertion_index[rid]),
    )

    transcript.run_id = next(
        (rid for rid in ordered_run_ids if rid is not None),
        None,
    )

    saw_terminal = False
    last_turn_seq: int | None = None
    # Reset this map at each run boundary to prevent cross-run pairing.
    pending_calls: dict[str, Turn] = {}

    for run_index, rid in enumerate(ordered_run_ids, start=1):
        bucket = sorted(groups[rid], key=_within_group_key)

        current: Turn | None = None
        pending_calls.clear()

        def _flush() -> None:
            nonlocal current
            if current is not None:
                transcript.turns.append(current)
                current = None

        for src_idx, event in bucket:
            kind, payload = event.case, event.payload
            if not kind:
                continue
            seq = event.sequence
            ts = event.emitted_at

            if kind in _TERMINAL_KINDS:
                saw_terminal = True

            if kind in _ANNOTATION_KINDS:
                transcript.annotations.append(
                    Annotation(
                        kind=_ANNOTATION_KINDS[kind],
                        ts=ts,
                        summary=_clip(_annotation_summary(kind, payload), 1200),
                        anchor_seq=last_turn_seq,
                        detail={"event_kind": kind, **payload},
                        source_index=src_idx,
                    )
                )
                continue

            mapped = _conversation_event(kind, payload)
            if mapped is None:
                continue

            role, agent_hint, text, tool_call, tool_result = mapped
            agent = agent_hint or _agent_of(payload)

            if kind == "agent_invocation_completed" and agent:
                caller = pending_calls.pop(agent, None)
                if caller is not None:
                    caller.tool_results.append(
                        {
                            "name": agent,
                            "result": text,
                            "task_id": payload.get("task_id"),
                        }
                    )
                    if src_idx > caller.source_index:
                        caller.source_index = src_idx
                    if not text:
                        continue

            same_turn = (
                current is not None
                and current.role == role
                and (current.agent or None) == (agent or None)
                and role not in _SYSTEM_KINDS  # system turns never merge
                and kind not in _SYSTEM_KINDS
            )

            if not same_turn:
                _flush()
                current = Turn(
                    seq=seq,
                    ts=ts,
                    agent=agent,
                    role=role,
                    kind=kind,
                    run_id=rid,
                    run_index=run_index,
                    source_index=src_idx,
                )
            else:
                assert current is not None
                if current.seq is None:
                    current.seq = seq
                if current.ts is None:
                    current.ts = ts
                if not current.agent and agent:
                    current.agent = agent
                if src_idx > current.source_index:
                    current.source_index = src_idx

            assert current is not None
            if kind == "agent_invocation_completed":
                invocation_id = payload.get("invocation_id")
                if isinstance(invocation_id, str) and invocation_id:
                    current.activity_ids.append(invocation_id)
            if text:
                # Run start and trivial goal derivation can repeat the prompt.
                segments = current.text.split("\n\n") if current.text else []
                if text not in segments:
                    current.text = _clip((current.text + "\n\n" + text) if current.text else text)
                current.kind = kind
            if tool_call is not None:
                current.tool_calls.append(tool_call)
                current.kind = kind
                tool_node_id = f"tool:{rid or 'run'}:{src_idx}"
                current.activity_ids.append(tool_node_id)
                # ``delegation_observed`` states the DELEGATING invocation's
                # id; an id matching a known invocation is an explicit parent
                # edge, never an inference. Without one the observation stays
                # turn-scoped.
                host = payload.get("invocation_id")
                parent_id = host if isinstance(host, str) and host in agent_ids else None
                transcript.execution["nodes"].append(
                    {
                        "node_id": tool_node_id,
                        "kind": "tool",
                        "parent_id": parent_id,
                        "name": str(tool_call.get("name") or "tool"),
                        "status": "observed",
                        "start_source_index": src_idx,
                        "summary": "",
                        "fidelity": "exact" if parent_id else "turn",
                    }
                )
                sub = str(tool_call.get("name") or "")
                if sub:
                    pending_calls[sub] = current
            if tool_result is not None:
                current.tool_results.append(tool_result)

            if current.seq is not None:
                last_turn_seq = current.seq

        _flush()

    transcript.turns = [t for t in transcript.turns if t.text or t.tool_calls or t.tool_results]
    _finalize_execution(transcript.execution)

    # Anchor early annotations to the first visible turn.
    if transcript.turns:
        first_seq = transcript.turns[0].seq
        for ann in transcript.annotations:
            if ann.anchor_seq is None and first_seq is not None:
                ann.anchor_seq = first_seq

    if saw_terminal:
        transcript.complete = True
    else:
        transcript.complete = False
    if not last_line_ok:
        transcript.complete = False

    return transcript
