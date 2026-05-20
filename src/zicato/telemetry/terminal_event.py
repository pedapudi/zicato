"""Terminal-event invariant: every run's events.jsonl ends with a lifecycle frame.

Goldfive's inner runner emits ``run_completed`` / ``run_aborted`` on its
own clean exit and caught-exception paths, but it cannot emit them when
its task is *cancelled* from the outside — neither when zicato's worker
cancels the inner ``goldfive.run`` via :func:`asyncio.wait_for` (the
worker's cooperative wall-clock budget), nor when the orchestrator
SIGKILLs the whole worker subprocess (the parent / supervisor escalation).

Without a terminal frame on disk, downstream readers cannot tell a
"truly mid-flight" run from a "wall-clock killed and torn down" run.
The dashboard's transcript reconstruction in particular gates
``complete`` on a member of
:data:`zicato.dashboard.transcript._TERMINAL_KINDS` being present — see
the file header for the exact downstream consequence.

This module provides two co-operating pieces:

* :class:`SequenceTrackingSink` — a thin spy that decorates a
  goldfive-style sink, recording the last ``run_id`` and the maximum
  ``sequence`` it saw flow through. The worker wraps each of its sinks
  with this so it has those two scalars available the instant
  ``asyncio.wait_for`` fires.

* :func:`ensure_run_aborted_event` — appends one ``run_aborted`` JSON
  line directly to an ``events.jsonl`` file when one is not already
  present. The orchestrator calls this after a parent-side kill (where
  the worker is dead and cannot emit) and as a defensive fallback after
  a worker-side abort that closed sinks before emitting.

The contract is "the file ends with a terminal frame", not "exactly one
terminal frame per run" — both helpers no-op when a terminal frame is
already on disk.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

log = logging.getLogger("zicato.telemetry.terminal_event")

#: Reason string stamped onto the synthesised terminal frame when the
#: wall-clock budget fires. Mirrors the symbolic reason zicato's loss
#: profile already carries (``wall_clock_budget_exceeded=True``) so a
#: downstream consumer can correlate the two without string surgery.
WALL_CLOCK_REASON = "wall_clock_budget_exceeded"


#: Terminal payload keys (camelCase as goldfive's proto-to-JSON renders
#: them; the dashboard reconstructor accepts both styles, but we always
#: write the proto-canonical spelling so the JSONL stays uniform).
_TERMINAL_PAYLOAD_KEYS = ("runCompleted", "runAborted", "conversationEnded")
_TERMINAL_PAYLOAD_KEYS_SNAKE = ("run_completed", "run_aborted", "conversation_ended")


class SequenceTrackingSink:
    """Decorate a sink, recording the last ``run_id`` and max ``sequence``.

    The wrapped sink's ``emit`` and ``close`` are forwarded unchanged.
    The wrapper extracts ``run_id`` and ``sequence`` from each event
    (proto message OR dict envelope) before the event flows on, so a
    subsequent caller can ask :attr:`last_run_id` / :attr:`max_sequence`
    even after the inner task that emitted those events has been
    cancelled.

    The wrapper is intentionally tolerant: an event missing one or both
    fields is forwarded unchanged and contributes nothing to the
    tracker. The tracker is *additive* observability over the sink —
    it never alters the event stream.
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.last_run_id: str = ""
        self.max_sequence: int = -1

    @property
    def inner(self) -> Any:
        """Return the wrapped sink (for tests / introspection)."""
        return self._inner

    async def emit(self, event: Any) -> None:
        # Extract from a proto message first, then fall back to dict /
        # attribute access. Any extraction error is swallowed — the
        # tracker is best-effort observability and must never break the
        # emit path.
        try:
            run_id = ""
            seq: int = -1
            if hasattr(event, "DESCRIPTOR"):
                run_id = str(getattr(event, "run_id", "") or "")
                seq = int(getattr(event, "sequence", 0) or 0)
            elif isinstance(event, dict):
                run_id = str(event.get("run_id") or event.get("runId") or "")
                raw_seq = event.get("sequence")
                if raw_seq is not None:
                    seq = int(raw_seq)
            else:
                run_id = str(getattr(event, "run_id", "") or "")
                raw_seq = getattr(event, "sequence", None)
                if raw_seq is not None:
                    seq = int(raw_seq)
            if run_id:
                self.last_run_id = run_id
            if seq > self.max_sequence:
                self.max_sequence = seq
        except Exception as exc:  # noqa: BLE001 — observability must not fail emit
            log.debug("SequenceTrackingSink: could not track event: %s", exc)
        await self._inner.emit(event)

    async def close(self) -> None:
        await self._inner.close()


def _looks_terminal(line: str) -> bool:
    """Return True iff ``line`` is a JSON object with a terminal payload key."""
    line = line.strip()
    if not line:
        return False
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return False
    if not isinstance(obj, dict):
        return False
    for key in _TERMINAL_PAYLOAD_KEYS:
        if key in obj:
            return True
    for key in _TERMINAL_PAYLOAD_KEYS_SNAKE:
        if key in obj:
            return True
    return False


def _file_already_has_terminal(events_path: Path) -> bool:
    """Cheap check: walk the file tail for an existing terminal frame.

    The file is small (one run, JSONL), so a full-file scan is fine. We
    do not assume the terminal is on the *last* line — a wedged emitter
    might have written extra after a goldfive abort.
    """
    if not events_path.exists():
        return False
    try:
        with open(events_path, encoding="utf-8") as f:
            for line in f:
                if _looks_terminal(line):
                    return True
    except OSError as exc:
        log.debug("ensure_run_aborted_event: could not read %s: %s", events_path, exc)
    return False


def _highest_seen(events_path: Path) -> tuple[str, int]:
    """Walk ``events.jsonl`` for the last ``run_id`` and the max ``sequence``.

    Returns ``("", -1)`` when the file is missing or unreadable. The
    caller folds these into the synthesised terminal frame.
    """
    last_run_id = ""
    max_seq = -1
    if not events_path.exists():
        return last_run_id, max_seq
    try:
        with open(events_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(obj, dict):
                    continue
                rid = obj.get("runId") or obj.get("run_id")
                if isinstance(rid, str) and rid:
                    last_run_id = rid
                raw_seq = obj.get("sequence")
                if raw_seq is not None:
                    try:
                        seq = int(raw_seq)
                    except (TypeError, ValueError):
                        continue
                    if seq > max_seq:
                        max_seq = seq
    except OSError as exc:
        log.debug("ensure_run_aborted_event: could not read %s: %s", events_path, exc)
    return last_run_id, max_seq


def _proto_run_aborted_line(run_id: str, sequence: int, reason: str) -> str:
    """Build a proto-canonical JSON line for a ``run_aborted`` envelope.

    Falls back to a dict-shape envelope if the goldfive proto stubs are
    not importable (a no-goldfive test environment). Both shapes are
    accepted by the dashboard's transcript reconstructor — the proto
    path is preferred because every other line in the file is proto-
    canonical and a stable byte shape matters for snapshot tests.
    """
    try:
        from goldfive.events import run_aborted_event  # noqa: PLC0415
        from google.protobuf.json_format import MessageToJson  # noqa: PLC0415
    except ModuleNotFoundError:
        envelope: dict[str, Any] = {
            "run_id": run_id,
            "sequence": sequence,
            "run_aborted": {"reason": reason},
        }
        return json.dumps(envelope, sort_keys=True)
    evt = run_aborted_event(run_id=run_id, sequence=sequence, reason=reason)
    # MessageToJson with sort_keys for byte-stable output (matches the
    # goldfive sink's own serialisation style).
    return MessageToJson(evt, sort_keys=True, indent=None)


def ensure_run_aborted_event(
    events_path: Path,
    *,
    reason: str = WALL_CLOCK_REASON,
    run_id: str = "",
    sequence: int | None = None,
) -> bool:
    """Append a ``run_aborted`` line to ``events_path`` iff none is present.

    Returns ``True`` iff a terminal frame was appended; ``False`` when
    the file already ends with a terminal (the no-op case) or could not
    be touched (the file does not exist and no sink ever opened it).

    ``run_id`` / ``sequence`` are optional overrides — useful when the
    caller already has them in hand (a worker holding a
    :class:`SequenceTrackingSink`). When omitted, both are recovered by
    scanning the existing JSONL.

    Best-effort: any I/O failure is swallowed with a debug log. The
    invariant is "do everything reasonable to leave the file with a
    terminal frame"; the orchestrator must still continue if this
    cannot land for some reason (e.g. the run dir was torn down).
    """
    if _file_already_has_terminal(events_path):
        return False
    if not events_path.exists():
        # No prior emits — there is no event stream to terminate. The
        # downstream reconstructor handles a missing file by returning
        # an empty transcript, which is the right semantics.
        return False

    if not run_id or sequence is None:
        scanned_run_id, scanned_seq = _highest_seen(events_path)
        if not run_id:
            run_id = scanned_run_id
        if sequence is None:
            sequence = scanned_seq + 1 if scanned_seq >= 0 else 0

    if not run_id:
        # We saw events on disk but none carried a run_id. Synthesise
        # an empty run_id — the dashboard's reconstructor handles a
        # blank run_id gracefully and `complete: True` still flips.
        run_id = ""

    line = _proto_run_aborted_line(run_id=run_id, sequence=int(sequence), reason=reason)
    try:
        with open(events_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()
    except OSError as exc:
        log.debug("ensure_run_aborted_event: append failed for %s: %s", events_path, exc)
        return False
    return True


__all__ = [
    "SequenceTrackingSink",
    "WALL_CLOCK_REASON",
    "ensure_run_aborted_event",
]
