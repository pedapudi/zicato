"""One reader for a run's ``events.jsonl``: one envelope schema, one casing rule.

A run's event log is a JSON-lines file. Each line is one goldfive ``Event``
in wire form, and the same event reaches disk in either of two shapes:

* **Payload-key shape.** The payload sits at the top level under the name of
  its ``oneof`` case, alongside the envelope fields — ``{"eventId": …,
  "runId": …, "sequence": 3, "driftDetected": {…}}``. This is what
  ``MessageToJson`` writes, so field names are lowerCamelCase.
* **Normalized shape.** The case name and the payload are separate fields —
  ``{"event_id": …, "run_id": …, "kind": "pin_resolved", "payload": {…},
  "emitted_at": {"seconds": …, "nanos": …}}``. Field names are snake_case
  and the timestamp is a proto ``Timestamp`` message rather than a string.
  The meta-loop emitter writes this whenever the proto stubs are
  unavailable, and the supervisor's ``run_log.rs`` documents it as the
  form a proto reparse produces.

:func:`parse_event` resolves both to one :class:`EventRecord`, and
:func:`read_event_log` turns a file into records. Every consumer reads
through here — the loss reducer and its two non-goldfive dialects, the
analyzer's decision-event aggregator and process-exemplar extractor, the
proposer's redacted process query, the dashboard's transcript
reconstruction and its run-log tail, the synthetic drift matchers, the
terminal-frame check, and the foreign-trace sniffer — so a given line
yields the same payload case, the same payload field names and the same
timestamp everywhere.

The reader is pure, deterministic and filesystem-read-only. It imports no
proto stubs, so it stays importable where goldfive is not installed.

Reading is tolerant by contract. A line that is not JSON, or is JSON but not
an object, is counted as malformed and skipped; invalid UTF-8 bytes decode to
replacement characters, which then fail the JSON parse and are counted the
same way; a missing or unreadable file yields an empty log. A defect in one
line never costs the caller the rest of the file, because a reader that
aborts on a bad line biases every count taken from the lines after it.
"""

from __future__ import annotations

import datetime as _dt
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = [
    "ENVELOPE_KEYS",
    "EventLog",
    "EventRecord",
    "parse_event",
    "read_event_log",
    "snake_deep",
    "to_snake",
]


# ---------------------------------------------------------------------------
# The one casing rule
# ---------------------------------------------------------------------------


def to_snake(name: str) -> str:
    """Convert a ``camelCase`` / ``PascalCase`` identifier to ``snake_case``.

    An underscore goes before each uppercase ASCII letter that follows a
    lowercase letter or a digit; every other character is copied through with
    uppercase folded down. Input already in snake_case is unchanged, and the
    conversion is idempotent, so a file mixing both spellings normalizes to
    one vocabulary.

    A run of capitals is one word: ``goldfiveLLMCallStart`` becomes
    ``goldfive_llmcall_start`` rather than ``goldfive_l_l_m_call_start``. A
    field name generated from a proto never carries such a run — protobuf
    builds its JSON name from a snake_case field name, so each capital
    follows a lowercase letter — but a hand-written or foreign log may, and
    splitting inside an acronym produces a case name no dispatch table holds.

    The supervisor's Rust ``run_log::to_snake`` implements the same rule, so
    an event kind has one spelling whichever of the two services read it.
    """
    out: list[str] = []
    prev_lower_or_digit = False
    for ch in name:
        if ch.isascii() and ch.isupper():
            if prev_lower_or_digit:
                out.append("_")
            out.append(ch.lower())
            prev_lower_or_digit = False
        else:
            out.append(ch)
            prev_lower_or_digit = ch.isascii() and (ch.islower() or ch.isdigit())
    return "".join(out)


def snake_deep(value: Any) -> Any:
    """Snake-case every mapping key in ``value``, at every depth.

    Payloads nest — a revised plan carries its tasks, a completed invocation
    its usage counts — so a rule applied only to the top level leaves the
    inner names in whichever spelling the writer used. Lists are walked;
    scalars are returned unchanged.

    Two keys that differ only in spelling (``taskId`` beside ``task_id``)
    collapse to one, and the later key in iteration order wins. No goldfive
    message can carry both, since each proto field has a single JSON name.
    """
    if isinstance(value, dict):
        return {to_snake(k): snake_deep(v) for k, v in value.items()}
    if isinstance(value, list):
        return [snake_deep(v) for v in value]
    return value


# ---------------------------------------------------------------------------
# The one envelope schema
# ---------------------------------------------------------------------------

#: Top-level names that belong to the envelope and so can never name the
#: payload case. Spelled snake_case because :func:`parse_event` normalizes
#: the line before it reads it: ``emittedAt`` is already ``emitted_at`` by
#: then. ``seq`` is the sequence number's second spelling, and ``kind`` /
#: ``payload`` are the normalized shape's own two fields.
ENVELOPE_KEYS: frozenset[str] = frozenset(
    {
        "emitted_at",
        "event_id",
        "kind",
        "payload",
        "run_id",
        "seq",
        "sequence",
        "session_id",
    }
)


@dataclass(frozen=True, slots=True)
class EventRecord:
    """One event line, resolved to one shape.

    Attributes
    ----------
    case:
        The payload's ``oneof`` case in snake_case — ``"drift_detected"``,
        ``"task_progress"``. Empty when the line carries no payload: an
        envelope with nothing under it, or a line whose only non-envelope
        field holds something other than a message.
    payload:
        The payload's fields, snake-cased at every depth. Empty when
        :attr:`case` is empty, and also when the case is known but the
        payload is absent or is not a message.
    run_id, session_id, event_id:
        Envelope identity. Empty string when the field is absent or does not
        hold a string.
    sequence:
        The per-run sequence number, read from ``sequence`` or ``seq``.
        ``None`` when absent or unparseable. A numeric string is accepted:
        protobuf renders 64-bit integers as JSON strings.
    emitted_at:
        Emission time as an RFC-3339 string in UTC. A string on the line is
        passed through as written; a proto ``{"seconds": …, "nanos": …}``
        timestamp is rendered. ``None`` when absent or unreadable.
    raw:
        The whole line as parsed, with every key snake-cased at every depth.
        Consumers needing a field this record does not name read it here.
    """

    case: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    run_id: str = ""
    session_id: str = ""
    event_id: str = ""
    sequence: int | None = None
    emitted_at: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


def _string_field(event: dict[str, Any], name: str) -> str:
    value = event.get(name)
    return value if isinstance(value, str) else ""


def _sequence_of(event: dict[str, Any]) -> int | None:
    raw = event.get("sequence")
    if raw is None:
        raw = event.get("seq")
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str):
        try:
            return int(raw.strip())
        except ValueError:
            return None
    return None


def _emitted_at_of(event: dict[str, Any]) -> str | None:
    """Render ``emitted_at`` as RFC-3339 in UTC, or ``None``."""
    raw = event.get("emitted_at")
    if isinstance(raw, str):
        return raw or None
    if not isinstance(raw, dict):
        return None
    seconds = raw.get("seconds", 0)
    nanos = raw.get("nanos", 0)
    if not isinstance(seconds, int | float | str):
        return None
    if not isinstance(nanos, int | float | str):
        nanos = 0
    try:
        total = int(seconds) + int(nanos) / 1_000_000_000
    except (TypeError, ValueError):
        return None
    try:
        moment = _dt.datetime.fromtimestamp(total, _dt.UTC)
    except (OSError, OverflowError, ValueError):
        return None
    return moment.isoformat().replace("+00:00", "Z")


def _case_and_payload(event: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Resolve the payload case and its fields from a normalized event.

    The normalized shape is checked first: a ``kind`` holding a non-empty
    string names the case outright, and the payload is whatever ``payload``
    holds. Otherwise the payload key is the first non-envelope field holding
    a message, which is how the payload-key shape carries its ``oneof``. A
    non-envelope field holding a scalar is not a payload and is skipped: the
    ``oneof`` is a message field, so a scalar there belongs to some other
    record shape sharing the file's extension.
    """
    kind = event.get("kind")
    if isinstance(kind, str) and kind:
        payload = event.get("payload")
        return to_snake(kind), payload if isinstance(payload, dict) else {}
    for key, value in event.items():
        if key in ENVELOPE_KEYS:
            continue
        if isinstance(value, dict):
            return key, value
    return "", {}


def parse_event(obj: dict[str, Any]) -> EventRecord:
    """Resolve one parsed event line into an :class:`EventRecord`.

    ``obj`` is the line as :func:`json.loads` returned it, in either wire
    shape. Every key is snake-cased before anything is read from it, so the
    envelope schema and the payload dispatch both speak one vocabulary.
    """
    event = snake_deep(dict(obj))
    case, payload = _case_and_payload(event)
    return EventRecord(
        case=case,
        payload=payload,
        run_id=_string_field(event, "run_id"),
        session_id=_string_field(event, "session_id"),
        event_id=_string_field(event, "event_id"),
        sequence=_sequence_of(event),
        emitted_at=_emitted_at_of(event),
        raw=event,
    )


@dataclass(frozen=True, slots=True)
class EventLog:
    """The records of one event file, plus what reading it revealed.

    Attributes
    ----------
    records:
        The parseable lines in file order.
    malformed_line_count:
        Lines that were neither blank nor a JSON object. Advisory: callers
        that report reduction quality surface it, others ignore it.
    last_line_ok:
        ``False`` when the file's final non-blank line failed to parse —
        the signature of a log still being appended to, which a live reader
        distinguishes from a log with a defect in its middle. ``True`` for
        an empty or missing file.
    """

    records: tuple[EventRecord, ...] = ()
    malformed_line_count: int = 0
    last_line_ok: bool = True


def read_event_log(path: Path) -> EventLog:
    """Read one ``events.jsonl`` into an :class:`EventLog`.

    A missing or unreadable file yields an empty log rather than raising:
    every caller has a "no telemetry yet" path, and a run whose events file
    has not been opened is the ordinary case at the start of a run.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return EventLog()

    lines = text.splitlines()
    while lines and not lines[-1].strip():
        lines.pop()

    records: list[EventRecord] = []
    malformed = 0
    last_line_ok = True
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            obj = json.loads(stripped)
        except json.JSONDecodeError:
            malformed += 1
            if index == len(lines) - 1:
                last_line_ok = False
            continue
        if not isinstance(obj, dict):
            malformed += 1
            if index == len(lines) - 1:
                last_line_ok = False
            continue
        records.append(parse_event(obj))

    return EventLog(
        records=tuple(records),
        malformed_line_count=malformed,
        last_line_ok=last_line_ok,
    )
