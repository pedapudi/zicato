"""The Foe episode log: its envelope, and the derived-message rule.

A Foe episode writes one append-only ``episode.jsonl`` whose format is
specified by ``docs/log-format.md`` in the Foe repository. One JSON object
per line carries ``seq`` (contiguous from 0), ``time``, ``type`` and
``data``; the first event is always ``episode/start`` at ``seq`` 0, which is
how :func:`is_episode_log` tells such a file from a Goldfive/ADK
``events.jsonl``.

The log is read verbatim. :mod:`zicato.telemetry.event_log` normalizes an ADK
event's field names to one casing; nothing here rewrites a key. A ``data``
payload holds tool arguments the model wrote, so renaming a key inside one
would change what the transcript says the episode did.

The message list a request carried is derived from the events before it by
one rule, which the log format specifies under "Derived messages" and which
Foe's runtime, its viewer and its Python package all apply identically:

1. Begin with an empty list.
2. Walk events in ``seq`` order.
3. A ``model/request`` contributes one ``user`` message built from the
   ``inbox/item`` events its ``consumed`` list names, in the order listed,
   with their content blocks concatenated. The message sits at the request's
   position, so an item that arrived while an earlier request was in flight
   appears after that request's assistant message.
4. An ``assistant/message`` becomes an ``assistant`` message carrying its
   text and tool calls, whether or not the response was interrupted.
5. Each ``tool/result`` becomes a ``tool`` message carrying ``rendered``,
   except one whose call was opened by a ``tool/inner-call``: an inner
   dispatch a composing tool made never reaches the model, because the outer
   call's own result is the whole account of it.
6. Every other event type contributes nothing.

A ``model/request`` whose ``request_id`` starts with ``cmp_``, and the
``assistant/message`` answering it, contribute nothing: that pair is a
summarization exchange, and its request records the prompt it sent rather
than a derived list. After the latest ``compaction/summary`` before a
request, the list instead opens with the task verbatim and the continuation
message :func:`render_continuation` builds, and the walk starts at the
summary's ``first_kept_seq``.

The rule is checkable against the log itself: every ordinary ``model/request``
records the list it actually sent, so a reader that recomputes the list with
:func:`derive_messages` and finds a difference has found a defect in one of
the two.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = [
    "EPISODE_START",
    "SUMMARY_REQUEST_PREFIX",
    "EpisodeEvent",
    "EpisodeLog",
    "derive_messages",
    "inner_call_ids",
    "is_episode_log",
    "is_summary_request",
    "latest_summary",
    "message_from",
    "read_episode_log",
    "render_continuation",
]

#: The event type at ``seq`` 0 of every Foe log, and so the log's signature.
EPISODE_START = "episode/start"

#: Request ids with this prefix are summarization exchanges, which the
#: derived-message rule excludes.
SUMMARY_REQUEST_PREFIX = "cmp_"

#: What a continuation message prints for a list with no items.
_STATE_NONE = "(none)"


@dataclass(frozen=True, slots=True)
class EpisodeEvent:
    """One log line: the envelope fields, and the payload exactly as written."""

    seq: int
    type: str
    data: dict[str, Any]
    time: int | None = None


@dataclass(frozen=True, slots=True)
class EpisodeLog:
    """The events of one episode log, plus what reading it revealed.

    ``last_line_ok`` is ``False`` when the file's final non-blank line failed
    to parse, which is the signature of a log still being appended to. It is
    the same tolerance :mod:`zicato.telemetry.event_log` applies, so a live
    proposal episode is readable while it runs.
    """

    events: tuple[EpisodeEvent, ...] = ()
    last_line_ok: bool = True


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _items(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _number(value: Any, fallback: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return fallback
    return int(value)


def _event_from(obj: Any) -> EpisodeEvent | None:
    """Resolve one parsed line, or ``None`` when it is not a log event."""
    if not isinstance(obj, dict):
        return None
    seq = obj.get("seq")
    kind = obj.get("type")
    if isinstance(seq, bool) or not isinstance(seq, int) or not isinstance(kind, str):
        return None
    time = obj.get("time")
    return EpisodeEvent(
        seq=seq,
        type=kind,
        data=_mapping(obj.get("data")),
        time=time if isinstance(time, int) and not isinstance(time, bool) else None,
    )


def is_episode_log(path: Path) -> bool:
    """Report whether ``path`` is a Foe episode log.

    The test is the log's own signature: the first non-blank line parses to
    an object whose ``seq`` is 0 and whose ``type`` is ``episode/start``.
    Every Foe log opens with that event and no other format writes it, so the
    check needs no more than one line and never mistakes a Goldfive/ADK
    ``events.jsonl`` for an episode. A missing or unreadable file is not one.
    """
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            for raw in handle:
                stripped = raw.strip()
                if not stripped:
                    continue
                try:
                    first = json.loads(stripped)
                except json.JSONDecodeError:
                    return False
                event = _event_from(first)
                return event is not None and event.seq == 0 and event.type == EPISODE_START
    except OSError:
        return False
    return False


def read_episode_log(path: Path) -> EpisodeLog:
    """Read one ``episode.jsonl`` into an :class:`EpisodeLog`.

    A missing or unreadable file yields an empty log rather than raising, and
    a line that is not a log event is skipped, so a defect in one line never
    costs the caller the rest of the episode.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return EpisodeLog()

    lines = text.splitlines()
    while lines and not lines[-1].strip():
        lines.pop()

    events: list[EpisodeEvent] = []
    last_line_ok = True
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            event = _event_from(json.loads(stripped))
        except json.JSONDecodeError:
            event = None
        if event is None:
            if index == len(lines) - 1:
                last_line_ok = False
            continue
        events.append(event)

    return EpisodeLog(events=tuple(events), last_line_ok=last_line_ok)


def is_summary_request(request_id: Any) -> bool:
    """Report whether a request id names a summarization exchange."""
    return _text(request_id).startswith(SUMMARY_REQUEST_PREFIX)


def latest_summary(events: tuple[EpisodeEvent, ...], seq: int) -> EpisodeEvent | None:
    """The last ``compaction/summary`` strictly before ``seq``, if any."""
    for event in reversed(events):
        if event.seq < seq and event.type == "compaction/summary":
            return event
    return None


def inner_call_ids(events: tuple[EpisodeEvent, ...]) -> frozenset[str]:
    """Call ids opened by a ``tool/inner-call`` rather than by the model.

    A composing tool dispatches these through the registry while its own
    model-issued call runs. Their results never reach the model, so they
    contribute no message and no conversation turn.
    """
    return frozenset(
        _text(event.data.get("call_id"))
        for event in events
        if event.type == "tool/inner-call" and _text(event.data.get("call_id"))
    )


def _user_text(text: str) -> dict[str, Any]:
    return {"role": "user", "content": [{"type": "text", "text": text}]}


def _amount(value: Any) -> str:
    """One budget dimension: its value, or ``unlimited`` when it has none."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return "unlimited"
    return str(int(value)) if float(value).is_integer() else str(value)


def _lines_of(items: list[Any]) -> str:
    """A list value: one item per line below the label, or ``(none)`` after it."""
    if not items:
        return f" {_STATE_NONE}"
    return "".join(f"\n- {item}" for item in items)


def render_continuation(data: dict[str, Any]) -> str:
    """The continuation message one ``compaction/summary`` contributes.

    The typed state as labelled lines, then the model's own summary. Byte for
    byte what Foe's runtime renders, which is what lets a recorded request be
    checked against a recomputed one.
    """
    state = _mapping(data.get("state"))
    files = _mapping(state.get("files"))
    covered = _mapping(state.get("covered"))
    budget = _mapping(state.get("budget_remaining"))
    children: list[str] = []
    for child in _items(state.get("children")):
        entry = _mapping(child)
        outcome = _mapping(entry.get("outcome"))
        detail = _text(outcome.get("code")) or _text(outcome.get("limit"))
        suffix = f" {detail}" if detail else ""
        children.append(
            f"{_text(entry.get('id'))} ({_text(entry.get('contract'))}): "
            f"{_text(outcome.get('kind'))}{suffix}"
        )
    lines = [
        f"covered: seq {_number(covered.get('first_seq'))} to {_number(covered.get('last_seq'))}",
        f"done_when: {_text(state.get('done_when'))}",
        f"outstanding_findings:{_lines_of(_items(state.get('outstanding_findings')))}",
        f"files_read:{_lines_of(_items(files.get('read')))}",
        f"files_written:{_lines_of(_items(files.get('written')))}",
        f"files_edited:{_lines_of(_items(files.get('edited')))}",
        f"children:{_lines_of(children)}",
        "budget_remaining: "
        f"model_calls {_amount(budget.get('model_calls'))}, "
        f"input_tokens {_amount(budget.get('input_tokens'))}, "
        f"output_tokens {_amount(budget.get('output_tokens'))}, "
        f"seconds {_amount(budget.get('seconds'))}",
    ]
    body = "\n".join(lines)
    return f"## Continuation state\n\n{body}\n\n## Summary\n\n{_text(data.get('summary'))}"


def message_from(
    event: EpisodeEvent,
    inbox: dict[int, EpisodeEvent],
    inner: frozenset[str],
) -> dict[str, Any] | None:
    """The one message ``event`` contributes, or ``None`` when it contributes none.

    Rules 3 to 6 of the derived-message rule, for a single event.
    ``inbox`` holds the ``inbox/item`` events seen so far, keyed by ``seq``,
    which is how a request resolves the items its ``consumed`` list names;
    ``inner`` is :func:`inner_call_ids`. Both the per-request derivation and
    the transcript projection walk events through this one function, so there
    is a single reading of what each event contributes.
    """
    if event.type == "model/request":
        if is_summary_request(event.data.get("request_id")):
            return None
        blocks: list[Any] = []
        for consumed in _items(event.data.get("consumed")):
            item = inbox.get(_number(consumed, -1))
            if item is not None:
                blocks.extend(_items(item.data.get("content")))
        return {"role": "user", "content": blocks} if blocks else None
    if event.type == "assistant/message":
        if is_summary_request(event.data.get("request_id")):
            return None
        return {
            "role": "assistant",
            "text": _text(event.data.get("text")),
            "tool_calls": [
                call for call in _items(event.data.get("tool_calls")) if isinstance(call, dict)
            ],
        }
    if event.type == "tool/result":
        call_id = _text(event.data.get("call_id"))
        if call_id in inner:
            return None
        return {
            "role": "tool",
            "call_id": call_id,
            "name": _text(event.data.get("name")),
            "rendered": _text(event.data.get("rendered")),
            "is_error": event.data.get("is_error") is True,
        }
    return None


def derive_messages(events: tuple[EpisodeEvent, ...], request_seq: int) -> list[dict[str, Any]]:
    """The message list for the ``model/request`` at ``request_seq``.

    Implements the derived-message rule this module's docstring states. An
    ``inbox/item`` enters the list at the position of the request that
    consumed it, wherever the item itself lies, which is what keeps a
    steering message that arrived while a tool was running after that tool's
    result.
    """
    inbox: dict[int, EpisodeEvent] = {}
    messages: list[dict[str, Any]] = []
    summary = latest_summary(events, request_seq)
    first_kept = 0
    if summary is not None:
        messages.append(_user_text(_text(_mapping(summary.data.get("state")).get("task"))))
        messages.append(_user_text(render_continuation(summary.data)))
        first_kept = _number(summary.data.get("first_kept_seq"))
    inner = inner_call_ids(events)

    for event in events:
        if event.seq > request_seq:
            break
        if event.type == "inbox/item":
            inbox[event.seq] = event
        if event.seq < first_kept:
            continue
        message = message_from(event, inbox, inner)
        if message is not None:
            messages.append(message)
    return messages
