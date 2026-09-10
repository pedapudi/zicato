"""Read proposal episode events without changing tool argument names.

Each line carries sequence, time, type and data. Conversation turns come from
consumed inbox items, assistant messages and matching tool results. Requests
already record their complete message lists, including compaction summaries;
readers do not reconstruct the model backend's request preparation.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from zicato.telemetry.json_lines import read_json_lines

__all__ = [
    "EPISODE_START",
    "SUMMARY_REQUEST_PREFIX",
    "EpisodeEvent",
    "EpisodeLog",
    "inner_call_ids",
    "is_episode_log",
    "is_summary_request",
    "message_from",
    "read_episode_log",
]

#: The event type at ``seq`` 0 of every Foe log, and so the log's signature.
EPISODE_START = "episode/start"

#: Request ids with this prefix are summarization exchanges, which the
#: derived-message rule excludes.
SUMMARY_REQUEST_PREFIX = "cmp_"


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
    """Read accepted episode events, including a log that is still growing."""
    events, _malformed, last_line_ok = read_json_lines(path, _event_from)
    return EpisodeLog(events=events, last_line_ok=last_line_ok)


def is_summary_request(request_id: Any) -> bool:
    """Report whether a request id names a summarization exchange."""
    return _text(request_id).startswith(SUMMARY_REQUEST_PREFIX)


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


def message_from(
    event: EpisodeEvent,
    inbox: dict[int, EpisodeEvent],
    inner: frozenset[str],
) -> dict[str, Any] | None:
    """The one message ``event`` contributes, or ``None`` when it contributes none.

    Read the conversation contribution of a single event.
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
