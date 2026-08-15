"""Pure, tolerant conversation reconstruction from an ``events.jsonl`` file.

Both persisted camel-case envelopes and normalized ``{kind, payload}`` events
are accepted. Malformed lines are skipped so growing runs remain readable.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from zicato.query.paths import to_snake

__all__ = ["Annotation", "Transcript", "Turn", "reconstruct_transcript"]


# Snake-cased envelope fields that cannot be the payload kind.
_ENVELOPE_KEYS = {
    "event_id",
    "run_id",
    "sequence",
    "seq",
    "emitted_at",
    "session_id",
    "kind",
    "payload",
}


def _snake_deep(value: Any) -> Any:
    """Recursively snake-case keys because transcript payloads are nested."""

    if isinstance(value, dict):
        return {to_snake(k): _snake_deep(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_snake_deep(v) for v in value]
    return value


def _iter_events(path: Path) -> tuple[list[dict[str, Any]], bool]:
    """Return parsed events and whether the final nonblank line was valid."""

    events: list[dict[str, Any]] = []
    last_line_ok = True
    try:
        with open(path, encoding="utf-8") as handle:
            raw_lines = handle.read().splitlines()
    except OSError:
        return [], True

    while raw_lines and not raw_lines[-1].strip():
        raw_lines.pop()

    for idx, line in enumerate(raw_lines):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            obj = json.loads(stripped)
        except json.JSONDecodeError:
            if idx == len(raw_lines) - 1:
                last_line_ok = False
            continue
        if isinstance(obj, dict):
            events.append(_snake_deep(obj))
    return events, last_line_ok


def _kind_and_payload(event: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Resolve normalized and top-level-oneof envelope shapes."""

    explicit = event.get("kind")
    if isinstance(explicit, str) and explicit:
        payload = event.get("payload")
        return to_snake(explicit), payload if isinstance(payload, dict) else {}

    for key, value in event.items():
        if key in _ENVELOPE_KEYS:
            continue
        if isinstance(value, dict):
            return key, value
        return key, {}
    return "", {}


def _seq_of(event: dict[str, Any]) -> int | None:
    """Extract the per-run sequence number, tolerating int or string."""

    raw = event.get("sequence")
    if raw is None:
        raw = event.get("seq")
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _norm_ts(value: Any) -> str | None:
    """Normalize an RFC-3339 string or proto ``{seconds, nanos}`` timestamp."""

    if value is None:
        return None
    if isinstance(value, str):
        return value or None
    if isinstance(value, dict):
        seconds = value.get("seconds")
        nanos = value.get("nanos")
        if seconds is None and nanos is None:
            return None
        try:
            secs = int(seconds or 0)
            nsecs = int(nanos or 0)
        except (TypeError, ValueError):
            return None
        dt = datetime.fromtimestamp(secs, tz=UTC)
        iso = dt.strftime("%Y-%m-%dT%H:%M:%S")
        if nsecs:
            iso += f".{nsecs:09d}".rstrip("0")
        return iso + "Z"
    return None


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


def _execution_topology(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Build only invocation relationships stated by canonical events."""

    nodes: dict[str, dict[str, Any]] = {}
    for source_index, event in enumerate(events):
        kind, payload = _kind_and_payload(event)
        if kind not in {"agent_invocation_started", "agent_invocation_completed"}:
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
        else:
            outcome = str(payload.get("outcome") or "completed").lower()
            node["status"] = outcome if outcome in {"failed", "cancelled"} else "completed"
            node["summary"] = _clip(str(payload.get("summary") or ""))
            if node["name"] is None:
                name = payload.get("agent_name")
                node["name"] = name if isinstance(name, str) and name else None

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
    root_ids = [node_id for node_id, node in nodes.items() if node["parent_id"] is None]
    return {
        "fidelity": "partial" if unresolved_ids else "exact" if nodes else "unavailable",
        "nodes": sorted(nodes.values(), key=lambda node: node["start_source_index"]),
        "root_ids": root_ids,
        "unresolved_ids": unresolved_ids,
    }


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

    path = Path(events_path)
    events, last_line_ok = _iter_events(path)

    transcript = Transcript()
    transcript.event_count = len(events)
    transcript.execution = _execution_topology(events)
    if not events:
        # Missing / empty file → empty transcript. An empty file from a
        # run that has not emitted anything yet is "not complete".
        transcript.complete = False
        return transcript

    # Group before sorting because each run restarts its sequence at zero.
    groups: dict[str | None, list[tuple[int, dict[str, Any]]]] = {}
    insertion_order: list[str | None] = []
    for idx, event in enumerate(events):
        rid_raw = event.get("run_id")
        rid: str | None = rid_raw if isinstance(rid_raw, str) and rid_raw else None
        bucket = groups.get(rid)
        if bucket is None:
            bucket = []
            groups[rid] = bucket
            insertion_order.append(rid)
        bucket.append((idx, event))

    def _min_ts_of(bucket: list[tuple[int, dict[str, Any]]]) -> str:
        best: str | None = None
        for _idx, event in bucket:
            ts = _norm_ts(event.get("emitted_at"))
            if ts is None:
                continue
            if best is None or ts < best:
                best = ts
        return best or ""

    def _within_group_key(
        item: tuple[int, dict[str, Any]],
    ) -> tuple[int, int, str, int]:
        # Sequence-less lifecycle frames precede the sequenced stream.
        idx, event = item
        seq = _seq_of(event)
        ts = _norm_ts(event.get("emitted_at")) or ""
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
            kind, payload = _kind_and_payload(event)
            if not kind:
                continue
            seq = _seq_of(event)
            ts = _norm_ts(event.get("emitted_at"))

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
                transcript.execution["nodes"].append(
                    {
                        "node_id": tool_node_id,
                        "kind": "tool",
                        "parent_id": None,
                        "name": str(tool_call.get("name") or "tool"),
                        "status": "observed",
                        "start_source_index": src_idx,
                        "summary": "",
                        "fidelity": "turn",
                    }
                )
                transcript.execution["root_ids"].append(tool_node_id)
                transcript.execution["fidelity"] = "partial"
                sub = str(tool_call.get("name") or "")
                if sub:
                    pending_calls[sub] = current
            if tool_result is not None:
                current.tool_results.append(tool_result)

            if current.seq is not None:
                last_turn_seq = current.seq

        _flush()

    transcript.turns = [t for t in transcript.turns if t.text or t.tool_calls or t.tool_results]

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
