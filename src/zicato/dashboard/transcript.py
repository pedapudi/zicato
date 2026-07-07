"""Conversation reconstruction from a goldfive ``events.jsonl`` file.

The dashboard renders a champion run and a challenger run side by side as
two conversation transcripts. This module is the parser that turns one
goldfive ``JSONLPersistenceSink`` file into an ordered transcript:

* :class:`Turn` — one conversational step (a user prompt, an agent
  message, a tool call + its result).
* :class:`Annotation` — a margin note anchored near a turn (a drift
  detection, a steering decision, a judge verdict, a plan revision).
* :class:`Transcript` — the whole reconstruction plus run metadata.

Design constraints:

* Pure. The only I/O is reading the file the caller hands in. No network,
  no model calls.
* Tolerant. Each line is an independent JSON object; a malformed or
  truncated line is skipped, never fatal. A missing file yields an empty
  transcript. This mirrors the reducer's plain-JSON fallback and the
  supervisor's run-log tailer, both of which parse the same growing file.
* Two envelope shapes. goldfive's persistence sink writes camelCase keys
  (``MessageToJson`` default) with the payload kind as a top-level
  envelope key (``goldfiveLlmCallStart`` alongside ``eventId`` /
  ``runId`` / ``sequence`` / ``emittedAt``). The reducer's proto-reparse
  path instead writes a normalized ``{kind, payload, emitted_at, ...}``
  shape with proto ``{seconds, nanos}`` timestamps. Both occur, sometimes
  interleaved in one file; both are handled here.

Key normalization (camelCase -> snake_case) is reused from
:mod:`zicato.analyzer.aggregator` rather than re-implemented.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from zicato.query.paths import to_snake

__all__ = ["Annotation", "Transcript", "Turn", "reconstruct_transcript"]


# ---------------------------------------------------------------------------
# Envelope handling
# ---------------------------------------------------------------------------

# Top-level envelope keys that are NOT the payload kind in the camelCase
# shape. Anything else at the top level is the payload oneof key. Stored
# snake_cased so the lookup is shape-agnostic.
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
    """Recursively snake_case every dict key in ``value``.

    The aggregator's ``_snake_keys`` is shallow — sufficient there because
    its absorbers only read scalar payload fields. Transcript
    reconstruction reaches into nested payloads (plan tasks, drift detail,
    timestamp ``{seconds, nanos}`` sub-objects), so the conversion must
    descend. Scalars and lists pass through with their elements converted.
    """

    if isinstance(value, dict):
        return {to_snake(k): _snake_deep(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_snake_deep(v) for v in value]
    return value


def _iter_events(path: Path) -> tuple[list[dict[str, Any]], bool]:
    """Read ``path`` and return ``(events, last_line_ok)``.

    Each element of ``events`` is a fully snake_cased JSON object. Blank
    lines are skipped. A line that fails to parse is skipped; if it is the
    final line of the file it is reported via ``last_line_ok=False`` so
    the caller can flag a possibly-truncated in-progress run. A missing or
    unreadable file yields ``([], True)``.
    """

    events: list[dict[str, Any]] = []
    last_line_ok = True
    try:
        with open(path, encoding="utf-8") as handle:
            raw_lines = handle.read().splitlines()
    except OSError:
        return [], True

    # Trailing blank lines do not count as truncation.
    while raw_lines and not raw_lines[-1].strip():
        raw_lines.pop()

    for idx, line in enumerate(raw_lines):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            obj = json.loads(stripped)
        except json.JSONDecodeError:
            # A bad final line is the classic "writer mid-flush" case.
            if idx == len(raw_lines) - 1:
                last_line_ok = False
            continue
        if isinstance(obj, dict):
            events.append(_snake_deep(obj))
    return events, last_line_ok


def _kind_and_payload(event: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Resolve ``(kind, payload)`` for one snake_cased event envelope.

    Shape 2 (``{kind, payload, ...}``) wins when an explicit ``kind`` key
    is present. Otherwise the first non-envelope top-level key is the
    payload oneof key and its value the payload (shape 1).
    """

    explicit = event.get("kind")
    if isinstance(explicit, str) and explicit:
        payload = event.get("payload")
        return to_snake(explicit), payload if isinstance(payload, dict) else {}

    for key, value in event.items():
        if key in _ENVELOPE_KEYS:
            continue
        if isinstance(value, dict):
            return key, value
        # A scalar payload key (rare) still identifies the kind.
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
    """Normalize a timestamp to an RFC-3339 string.

    Accepts the two shapes goldfive emits: an RFC-3339 string (camelCase
    ``MessageToJson`` path) and a proto ``{seconds, nanos}`` object (the
    reducer's proto-reparse path). Anything unrecognized yields ``None``.
    """

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
        # Render with nanosecond precision, trimmed, RFC-3339 'Z' suffix.
        iso = dt.strftime("%Y-%m-%dT%H:%M:%S")
        if nsecs:
            iso += f".{nsecs:09d}".rstrip("0")
        return iso + "Z"
    return None


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class Turn:
    """One conversational step in the reconstructed transcript.

    Consecutive raw events attributed to the same agent collapse into a
    single turn; a tool call and its matching result land in the same
    turn's ``tool_calls`` / ``tool_results`` lists.

    ``run_index`` is the 1-based ordinal of the goldfive run this turn
    belongs to within a multi-run events file. ``multi_turn_emulated``
    board entries spawn N goldfive runs (one per emulated user turn) and
    write them into a single events stream; every run has a distinct
    ``runId``. The reconstructor groups events by ``runId`` first, then
    sorts the groups chronologically (by min ``emittedAt`` of each
    group). The 1-based ordinal lets the renderer emit a visible
    boundary between groups so the operator can see "turn 2 of a
    multi-turn entry" at a glance. Single-run files emit
    ``run_index == 1`` for every turn (no boundary fires).
    """

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
        }


@dataclass
class Annotation:
    """A margin note anchored near a turn in the transcript.

    Annotations are the steering / observability layer: drift detections,
    steering decisions, judge verdicts, plan revisions. They do not carry
    conversation content; they explain what the framework did alongside
    it. ``anchor_seq`` is the sequence of the nearest preceding turn so
    the dashboard can pin the note to the conversation flow.
    """

    kind: str = ""
    ts: str | None = None
    summary: str = ""
    anchor_seq: int | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "ts": self.ts,
            "summary": self.summary,
            "anchor_seq": self.anchor_seq,
            "detail": dict(self.detail),
        }


@dataclass
class Transcript:
    """An ordered conversation reconstruction for one run."""

    turns: list[Turn] = field(default_factory=list)
    annotations: list[Annotation] = field(default_factory=list)
    run_id: str | None = None
    event_count: int = 0
    complete: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "event_count": self.event_count,
            "complete": self.complete,
            "turns": [t.to_dict() for t in self.turns],
            "annotations": [a.to_dict() for a in self.annotations],
        }


# ---------------------------------------------------------------------------
# Event classification
# ---------------------------------------------------------------------------

# Event kinds whose payload becomes a margin annotation rather than a
# conversation turn. These are the drift / steering / judge observability
# events from the goldfive Event taxonomy.
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

# Kinds that mark run / conversation lifecycle. They become low-noise
# "system" turns so the transcript has visible start / end anchors.
_SYSTEM_KINDS = {
    "run_started",
    "run_completed",
    "run_aborted",
    "conversation_started",
    "conversation_ended",
}

# Terminal kinds — their presence means the file is not truncated.
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


# ---------------------------------------------------------------------------
# Turn extraction
# ---------------------------------------------------------------------------


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
    if not events:
        # Missing / empty file → empty transcript. An empty file from a
        # run that has not emitted anything yet is "not complete".
        transcript.complete = False
        return transcript

    # --- group events by run_id -----------------------------------------
    # ``multi_turn_emulated`` board entries spawn one goldfive run per
    # emulated user turn and write them into one events file. Each run
    # owns its own ``runId`` and its own ``conversation_started`` /
    # ``run_completed`` lifecycle frames; the per-run sequence numbering
    # restarts at 0 inside each group. A flat sort over the merged stream
    # would interleave the runs (and lift every ``conversation_started``
    # frame to the top — see bug #172). Grouping first preserves
    # per-run-id contiguity in the rendered transcript.
    #
    # Events without a ``run_id`` (a malformed line, or a future stream
    # variant) fall into a single anonymous group keyed on ``None`` so
    # they still render in chronological order.
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
        # The group's chronological anchor is the earliest emitted_at
        # across all of its events. Missing timestamps sort last within a
        # group but never relocate the group itself.
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
        # Same shape as the prior flat sort key, applied per-group:
        # events without ``sequence`` (the lifecycle ``conversation_started``
        # frame) sort FIRST in timestamp order; sequenced events follow
        # in sequence order with emitted_at breaking ties.
        idx, event = item
        seq = _seq_of(event)
        ts = _norm_ts(event.get("emitted_at")) or ""
        return (
            0 if seq is None else 1,
            seq if seq is not None else 0,
            ts,
            idx,
        )

    # Order the groups by their min-emittedAt anchor. Ties (a tied
    # min-ts is vanishingly rare in practice — distinct goldfive runs
    # write distinct lifecycle timestamps) fall back to first-seen
    # insertion order for stability.
    insertion_index: dict[str | None, int] = {rid: i for i, rid in enumerate(insertion_order)}
    ordered_run_ids: list[str | None] = sorted(
        groups.keys(),
        key=lambda rid: (_min_ts_of(groups[rid]), insertion_index[rid]),
    )

    # The transcript-level run_id is the FIRST group's run id (matches
    # the single-run case verbatim; for a multi-run stream it surfaces
    # the chronologically earliest run, which is the natural "primary"
    # label for the column header).
    transcript.run_id = next(
        (rid for rid in ordered_run_ids if rid is not None),
        None,
    )

    saw_terminal = False
    last_turn_seq: int | None = None
    # Pending tool calls keyed by sub-agent name so a completion can be
    # matched back to the delegating turn that called it.
    pending_calls: dict[str, Turn] = {}

    # Walk each group in chronological order. The grouping state (current
    # open turn, pending tool-call map) is RESET at each group boundary —
    # a delegation in run 1 is never matched against an
    # ``agent_invocation_completed`` from run 2, and run 2's first event
    # always opens a fresh turn rather than extending run 1's last.
    for run_index, rid in enumerate(ordered_run_ids, start=1):
        bucket = sorted(groups[rid], key=_within_group_key)
        ordered = [event for _, event in bucket]

        current: Turn | None = None
        pending_calls.clear()

        def _flush() -> None:
            nonlocal current
            if current is not None:
                transcript.turns.append(current)
                current = None

        for event in ordered:
            kind, payload = _kind_and_payload(event)
            if not kind:
                continue
            seq = _seq_of(event)
            ts = _norm_ts(event.get("emitted_at"))

            if kind in _TERMINAL_KINDS:
                saw_terminal = True

            # --- annotations -------------------------------------------
            if kind in _ANNOTATION_KINDS:
                transcript.annotations.append(
                    Annotation(
                        kind=_ANNOTATION_KINDS[kind],
                        ts=ts,
                        summary=_clip(_annotation_summary(kind, payload), 1200),
                        anchor_seq=last_turn_seq,
                        detail={"event_kind": kind, **payload},
                    )
                )
                continue

            # --- conversation turns ------------------------------------
            mapped = _conversation_event(kind, payload)
            if mapped is None:
                # Boundary / lifecycle bookkeeping events with no content
                # (agent_invocation_started, invocation_boundary_*,
                # task_started, task_transitioned, ...) are not turns.
                # They still update the agent context for following
                # content.
                continue

            role, agent_hint, text, tool_call, tool_result = mapped
            agent = agent_hint or _agent_of(payload)

            # A delegation result: try to attach to the calling turn.
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
                    # The sub-agent's own summary still deserves a turn
                    # so the side-by-side view shows what it produced.
                    if not text:
                        continue

            # Decide whether this extends the current turn or opens a
            # new one.
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
                )
            else:
                assert current is not None
                # Keep the earliest seq/ts as the turn's anchor.
                if current.seq is None:
                    current.seq = seq
                if current.ts is None:
                    current.ts = ts
                if not current.agent and agent:
                    current.agent = agent

            assert current is not None
            if text:
                # Dedup exact-duplicate segments before appending. goldfive emits
                # the user's prompt TWICE for a trivially-derived goal — once as
                # ``run_started.goal_summary`` (the raw request) and again as
                # ``goal_derived.goals[*].summary`` (the framework's derived
                # goal, identical text for a single-task board). Both map to a
                # user turn and merge into one, so the prompt rendered twice.
                # Skipping an incoming segment already present keeps the prompt
                # (and any other accidental identical adjacency) shown once.
                segments = current.text.split("\n\n") if current.text else []
                if text not in segments:
                    current.text = _clip((current.text + "\n\n" + text) if current.text else text)
                current.kind = kind
            if tool_call is not None:
                current.tool_calls.append(tool_call)
                current.kind = kind
                sub = str(tool_call.get("name") or "")
                if sub:
                    pending_calls[sub] = current
            if tool_result is not None:
                current.tool_results.append(tool_result)

            if current.seq is not None:
                last_turn_seq = current.seq

        _flush()

    # Drop content-free turns (a delegation that produced no text and
    # was fully absorbed into a caller's tool_results leaves an empty
    # shell).
    transcript.turns = [t for t in transcript.turns if t.text or t.tool_calls or t.tool_results]

    # Re-anchor annotations whose anchor was minted before any turn:
    # pin them to the first turn instead of leaving a dangling None.
    if transcript.turns:
        first_seq = transcript.turns[0].seq
        for ann in transcript.annotations:
            if ann.anchor_seq is None and first_seq is not None:
                ann.anchor_seq = first_seq

    if saw_terminal:
        transcript.complete = True
    else:
        # No terminal event: the run looks in progress. A truncated
        # final line reinforces that. ``partial_ok`` does not change the
        # verdict (the transcript is still returned either way) — it
        # documents that the caller expects and tolerates this state.
        transcript.complete = False
    if not last_line_ok:
        transcript.complete = False

    return transcript
