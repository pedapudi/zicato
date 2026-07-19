"""WS-INGEST — import foreign agent trajectories into ``ImportedTrace`` records.

The trajectory-bootstrap on-ramp (TRAJECTORY-BOOTSTRAP.md §2/§3): point zicato
at a directory of foreign trace files — production logs from ANY agent,
captured with zero zicato involvement — and reduce each one, through the
EXISTING telemetry dialect reducer (TELEMETRY-DIALECTS.md), into an
:class:`ImportedTrace` carrying the reduced :class:`DialectSignals`, the
reconstructed conversation, and source provenance. The miner
(:func:`zicato.reflection.mining.imported_trace_episodes`) turns these into
signal-episodes; nothing here authors a suggestion or touches a contract.

**Goldfive-optional (the standing principle).** ADK-style event logs and bare
transcripts are FIRST-CLASS equals of goldfive traces — the importer needs no
goldfive artifact, no workspace, and no evolve loop. It sniffs each file's
format deterministically (§2.2), routes it to that dialect's producer, and
satisfies the producer's ``entry`` parameter with a SYNTHETIC PLACEHOLDER —
verified in TRAJECTORY-BOOTSTRAP.md §2.1: no dialect producer reads ``entry``.

Every read is tolerant (the dialect discipline): a malformed line is counted,
never raised; an unreadable file is skipped; an empty directory yields ``[]``.
Ids are content hashes (no wall-clock), so re-importing the same directory is
idempotent.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from zicato.core import (
    DIALECT_ADK_EVENTS,
    DIALECT_GOLDFIVE,
    DIALECT_TRANSCRIPT,
    BoardEntry,
    DriftCount,
)
from zicato.telemetry.dialects import DialectSignals

_LOG = logging.getLogger(__name__)

#: The synthetic placeholder handed to a dialect producer for a foreign trace.
#: No producer reads ``entry`` (TRAJECTORY-BOOTSTRAP.md §2.1), so this is never
#: inspected — it exists only to satisfy the ``DialectReducer`` Protocol.
_PLACEHOLDER_ENTRY: BoardEntry = BoardEntry(
    id="__imported__",
    kind="single_turn",
    wall_clock_budget_seconds=1,
    input="",
)

# --- sniffing vocabulary (TRAJECTORY-BOOTSTRAP.md §2.2) ---------------------

#: goldfive ``Event`` envelope markers (snake_case + camelCase twins). A
#: goldfive line carries one of these AND exactly one nested-dict payload.
_GOLDFIVE_ENVELOPE_KEYS: frozenset[str] = frozenset(
    {"event_id", "eventId", "sequence", "emitted_at", "emittedAt"}
)
#: Every envelope key (payload discovery skips these — mirrors reducer._payload).
_GOLDFIVE_ALL_ENVELOPE: frozenset[str] = _GOLDFIVE_ENVELOPE_KEYS | frozenset(
    {"run_id", "runId", "session_id", "sessionId"}
)
#: The BEHAVIORAL ADK event types (message types are NOT here — a message-only
#: log is a transcript wearing type tags, §2.2).
_ADK_BEHAVIORAL_TYPES: frozenset[str] = frozenset(
    {
        "tool_call",
        "tool_response",
        "agent_transfer",
        "transfer",
        "error",
        "exception",
        "model_usage",
        "run_start",
    }
)
_ADK_MESSAGE_TYPES: frozenset[str] = frozenset({"agent_message", "user_message"})
_TRANSCRIPT_ROLES: frozenset[str] = frozenset(
    {"user", "assistant", "agent", "human", "model", "system"}
)


# ---------------------------------------------------------------------------
# The imported-trace record (TRAJECTORY-BOOTSTRAP.md §3.1)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ImportedTrace:
    """One reduced foreign trace file (TRAJECTORY-BOOTSTRAP.md §3.1).

    Fields
    ------
    trace_id:
        Content-stable ``trace-{8hex}`` over ``(source_file, dialect,
        signal_digest)`` — deterministic, NO wall-clock, so a re-import
        resolves the same id.
    source_file:
        The basename of the source trace file (provenance).
    dialect:
        The sniffed format — ``goldfive`` / ``adk_events`` / ``transcript``.
    signals:
        The EXISTING dialect reducer's :class:`DialectSignals` output — the
        reconstructed transcript, the drift counts, the task/cost counts.
    line_count, malformed_line_count:
        Non-empty source lines seen / lines the reducer could not parse.
    """

    trace_id: str
    source_file: str
    dialect: str
    signals: DialectSignals
    line_count: int
    malformed_line_count: int

    @property
    def user_turns(self) -> tuple[str, ...]:
        return self.signals.user_turns

    @property
    def agent_turns(self) -> tuple[str, ...]:
        return self.signals.agent_turns

    def to_json(self) -> dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "source_file": self.source_file,
            "dialect": self.dialect,
            "signals": _signals_to_json(self.signals),
            "line_count": self.line_count,
            "malformed_line_count": self.malformed_line_count,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> ImportedTrace:
        return cls(
            trace_id=str(data.get("trace_id", "")),
            source_file=str(data.get("source_file", "")),
            dialect=str(data.get("dialect", DIALECT_TRANSCRIPT)),
            signals=_signals_from_json(data.get("signals", {})),
            line_count=int(data.get("line_count", 0)),
            malformed_line_count=int(data.get("malformed_line_count", 0)),
        )


def _signals_to_json(signals: DialectSignals) -> dict[str, Any]:
    """Canonical JSON of a :class:`DialectSignals` (drift counts flattened)."""
    return {
        "drift_counts": [
            {"kind": dc.kind, "severity": dc.severity, "count": dc.count}
            for dc in signals.drift_counts
        ],
        "plan_revisions": signals.plan_revisions,
        "task_started": signals.task_started,
        "task_failed": signals.task_failed,
        "llm_call_count": signals.llm_call_count,
        "token_count": signals.token_count,
        "agent_text_chars": signals.agent_text_chars,
        "run_id": signals.run_id,
        "adk_session_id": signals.adk_session_id,
        "agent_turns": list(signals.agent_turns),
        "user_turns": list(signals.user_turns),
        "malformed_line_count": signals.malformed_line_count,
        "warnings": list(signals.warnings),
    }


def _signals_from_json(raw: Any) -> DialectSignals:
    """Rebuild a :class:`DialectSignals` from its JSON shape (tolerant)."""
    if not isinstance(raw, dict):
        return DialectSignals()
    drift_counts = tuple(
        DriftCount(
            kind=str(dc.get("kind", "")),
            severity=dc.get("severity", "info"),
            count=int(dc.get("count", 0)),
        )
        for dc in raw.get("drift_counts", [])
        if isinstance(dc, dict)
    )
    return DialectSignals(
        drift_counts=drift_counts,
        plan_revisions=int(raw.get("plan_revisions", 0)),
        task_started=int(raw.get("task_started", 0)),
        task_failed=int(raw.get("task_failed", 0)),
        llm_call_count=int(raw.get("llm_call_count", 0)),
        token_count=int(raw.get("token_count", 0)),
        agent_text_chars=int(raw.get("agent_text_chars", 0)),
        run_id=str(raw.get("run_id", "")),
        adk_session_id=str(raw.get("adk_session_id", "")),
        agent_turns=tuple(str(t) for t in raw.get("agent_turns", [])),
        user_turns=tuple(str(t) for t in raw.get("user_turns", [])),
        malformed_line_count=int(raw.get("malformed_line_count", 0)),
        warnings=tuple(str(w) for w in raw.get("warnings", [])),
    )


# ---------------------------------------------------------------------------
# Format sniffing (deterministic, TRAJECTORY-BOOTSTRAP.md §2.2)
# ---------------------------------------------------------------------------


def _read_objects(path: Path) -> tuple[list[dict[str, Any]], int, int]:
    """``(objects, non_empty_lines, malformed)`` — tolerant JSONL read.

    A non-JSON line, or a JSON value that is not an object, is COUNTED as
    malformed and skipped (mirrors :func:`dialects._iter_json_objects`). A
    missing / unreadable file yields ``([], 0, 0)``.
    """
    objs: list[dict[str, Any]] = []
    non_empty = 0
    malformed = 0
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return [], 0, 0
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        non_empty += 1
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            malformed += 1
            continue
        if isinstance(parsed, dict):
            objs.append(parsed)
        else:
            malformed += 1
    return objs, non_empty, malformed


def _looks_goldfive(obj: dict[str, Any]) -> bool:
    """A goldfive ``Event``: an envelope marker + exactly one nested-dict payload."""
    if not (_GOLDFIVE_ENVELOPE_KEYS & obj.keys()):
        return False
    nested = [k for k, v in obj.items() if k not in _GOLDFIVE_ALL_ENVELOPE and isinstance(v, dict)]
    return len(nested) >= 1


def _obj_type(obj: dict[str, Any]) -> str:
    """The event kind, tolerant of ``type`` / ``event_type`` / ``kind``."""
    for k in ("type", "event_type", "kind"):
        v = obj.get(k)
        if isinstance(v, str) and v:
            return v
    return ""


def _looks_transcript(obj: dict[str, Any]) -> bool:
    """A bare transcript line: a known role + a text field, no recognised type."""
    if _obj_type(obj):
        return False
    role = obj.get("role")
    if not (isinstance(role, str) and role.lower() in _TRANSCRIPT_ROLES):
        return False
    return any(isinstance(obj.get(k), str) for k in ("content", "text", "message"))


def sniff_dialect(path: Path) -> str:
    """Deterministically sniff a trace file's dialect (TRAJECTORY-BOOTSTRAP.md §2.2).

    Fixed precedence (strongest producer first, so every ambiguity resolves
    deterministically): any goldfive-signal line ⇒ ``goldfive``; else any
    BEHAVIORAL adk event ⇒ ``adk_events``; else any transcript / message line ⇒
    ``transcript``; else (empty / all-malformed) ⇒ ``transcript`` (the floor).
    """
    objs, _non_empty, _malformed = _read_objects(path)
    saw_adk_behavioral = False
    saw_transcript = False
    for obj in objs:
        if _looks_goldfive(obj):
            return DIALECT_GOLDFIVE
        etype = _obj_type(obj)
        if etype in _ADK_BEHAVIORAL_TYPES:
            saw_adk_behavioral = True
        elif etype in _ADK_MESSAGE_TYPES or _looks_transcript(obj):
            saw_transcript = True
    if saw_adk_behavioral:
        return DIALECT_ADK_EVENTS
    if saw_transcript:
        return DIALECT_TRANSCRIPT
    return DIALECT_TRANSCRIPT


# ---------------------------------------------------------------------------
# Import (TRAJECTORY-BOOTSTRAP.md §3)
# ---------------------------------------------------------------------------


def _trace_id(source_file: str, dialect: str, signals: DialectSignals) -> str:
    """Content-stable ``trace-{8hex}`` — no wall-clock (idempotent re-import)."""
    digest = json.dumps(_signals_to_json(signals), sort_keys=True)
    payload = "|".join([source_file, dialect, digest])
    return "trace-" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:8]


def import_trace_file(path: Path) -> ImportedTrace:
    """Sniff + reduce + reconstruct one foreign trace file. Zero workspace.

    Reduces the file through the EXISTING dialect reducer for its sniffed
    format (via :func:`zicato.telemetry.reducer.dialect_producer`), using the
    synthetic placeholder entry (§2.1). Tolerant: a malformed line rides
    ``malformed_line_count``, never a crash.
    """
    from zicato.telemetry.reducer import dialect_producer  # noqa: PLC0415

    dialect = sniff_dialect(path)
    _objs, non_empty, malformed = _read_objects(path)
    producer = dialect_producer(dialect)
    try:
        signals = producer(path, _PLACEHOLDER_ENTRY)
    except Exception as exc:  # noqa: BLE001 — a foreign file is untrusted; degrade
        _LOG.info("trace import: %r reduced with no signals (%s)", path.name, exc)
        signals = DialectSignals(malformed_line_count=malformed)
    return ImportedTrace(
        trace_id=_trace_id(path.name, dialect, signals),
        source_file=path.name,
        dialect=dialect,
        signals=signals,
        line_count=non_empty,
        malformed_line_count=max(malformed, signals.malformed_line_count),
    )


def import_trajectories(trace_dir: Path) -> list[ImportedTrace]:
    """Import every ``*.jsonl`` trace file in a directory, sorted by filename.

    Pure over the filesystem — ZERO workspace, ZERO goldfive artifact required
    (the goldfive-optional principle). An unreadable file is skipped with a
    logged reason; an absent / empty directory yields ``[]``.
    """
    trace_dir = Path(trace_dir)
    if not trace_dir.is_dir():
        return []
    out: list[ImportedTrace] = []
    for path in sorted(trace_dir.iterdir()):
        if path.suffix != ".jsonl" or not path.is_file():
            continue
        try:
            out.append(import_trace_file(path))
        except OSError as exc:
            _LOG.info("trace import: skipped unreadable %r (%s)", path.name, exc)
    return out


# ---------------------------------------------------------------------------
# Persistence — imported/{trace_id}.json under the mint-mode reflection dir
# ---------------------------------------------------------------------------


def _imported_dir(workspace_root: Path, epoch_id: str, reflection_id: str) -> Path:
    from zicato.core.workspace import reflection_dir  # noqa: PLC0415

    return reflection_dir(workspace_root, epoch_id, reflection_id) / "imported"


def write_imported_traces(
    workspace_root: Path,
    epoch_id: str,
    reflection_id: str,
    traces: list[ImportedTrace],
) -> Path:
    """Persist each trace as ``imported/{trace_id}.json`` (atomic); return the dir."""
    directory = _imported_dir(workspace_root, epoch_id, reflection_id)
    directory.mkdir(parents=True, exist_ok=True)
    for trace in traces:
        path = directory / f"{trace.trace_id}.json"
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(trace.to_json(), indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)
    return directory


def read_imported_traces(
    workspace_root: Path, epoch_id: str, reflection_id: str
) -> list[ImportedTrace]:
    """Read persisted imported traces (tolerant: absence/defect ⇒ ``[]``)."""
    directory = _imported_dir(workspace_root, epoch_id, reflection_id)
    if not directory.is_dir():
        return []
    out: list[ImportedTrace] = []
    for path in sorted(directory.iterdir()):
        if path.suffix != ".json":
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            continue
        if isinstance(raw, dict):
            out.append(ImportedTrace.from_json(raw))
    return out


__all__ = [
    "ImportedTrace",
    "import_trace_file",
    "import_trajectories",
    "read_imported_traces",
    "sniff_dialect",
    "write_imported_traces",
]
