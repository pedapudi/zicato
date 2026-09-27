"""Per-turn audit records for the user emulator.

Every emulator turn produces an :class:`EmulatorTurnAudit`. The driver
keeps each record in memory and emits it, as a plain event on the
``zicato:emulator`` lane, to every sink the run was given; under the
tournament worker that is the run's ``events.jsonl``. Operators read these
records to see what the emulator saw and produced on each turn.

Audits do NOT carry the full transcript or the full emulator output;
just sizes, a short preview, and a persona-hash fingerprint. The reducer
joins these against the goldfive event stream by run id and turn index
if it needs more detail.
"""

from __future__ import annotations

import hashlib
import inspect
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from zicato.core.types import UserPersona

_log = logging.getLogger(__name__)

#: Length of the output preview held on the audit record.
_PREVIEW_CHARS = 200


@dataclass(frozen=True, slots=True)
class EmulatorTurnAudit:
    """Audit record for one emulator turn.

    Fields
    ------
    persona_hash:
        Short hex SHA-256 digest of the persona's serialized fields.
        Lets operators correlate audits across runs without revealing
        the persona body in the lane payload.
    transcript_chars_in:
        Total character count of the user-prompt block fed to the
        emulator on this turn — a cost proxy.
    output_chars_out:
        Character count of the emulator's response.
    output_preview:
        First :data:`_PREVIEW_CHARS` characters of the response. Truncated
        with no trailing ellipsis so byte counts stay predictable.
    """

    persona_hash: str
    transcript_chars_in: int
    output_chars_out: int
    output_preview: str


def _hash_persona(persona: UserPersona) -> str:
    """Compute a short stable hash of a persona's serialized fields.

    The hash is order-independent over field names and uses a NUL
    separator so distinct values cannot collide via concatenation.
    """
    blob = (
        f"goal\x00{persona.goal}\x00"
        f"constraints\x00{persona.constraints}\x00"
        f"stop_when\x00{persona.stop_when}"
    ).encode()
    return hashlib.sha256(blob).hexdigest()[:16]


def audit_turn(
    persona: UserPersona,
    transcript: tuple[str, ...],
    output: str,
) -> EmulatorTurnAudit:
    """Build an :class:`EmulatorTurnAudit` for one emulator turn.

    The transcript-char count is the sum of character lengths across the
    transcript tuple — not the size of the rendered prompt block — so
    the audit decouples from prompt-renderer changes.
    """
    transcript_chars = sum(len(t) for t in transcript)
    preview = output[:_PREVIEW_CHARS]
    return EmulatorTurnAudit(
        persona_hash=_hash_persona(persona),
        transcript_chars_in=transcript_chars,
        output_chars_out=len(output),
        output_preview=preview,
    )


async def emit_audit_span(
    sinks: Sequence[Any],
    audit: EmulatorTurnAudit,
    lane: str = "zicato:emulator",
) -> None:
    """Emit one audit record to every sink in ``sinks``, best-effort.

    The event is a plain dict; goldfive's ``JSONLPersistenceSink`` writes a
    dict as one JSON line. The module imports no goldfive symbols, so the
    emulator does not depend on goldfive internals.

    A sink's ``emit`` may be a plain method or a coroutine function (every
    goldfive sink declares ``async def emit``); an awaitable result is
    awaited before the next sink receives the record. A failing sink is
    logged and skipped, and the remaining sinks still receive the record:
    the audit is observability, so an audit failure never fails the run.

    Parameters
    ----------
    sinks:
        Sinks exposing an ``emit(event)`` method. An empty sequence emits
        nothing.
    audit:
        The audit record to emit.
    lane:
        Lane string stamped on the event. Defaults to ``"zicato:emulator"``.
    """
    event = {
        "lane": lane,
        "kind": "zicato.emulator.turn_audit",
        "persona_hash": audit.persona_hash,
        "transcript_chars_in": audit.transcript_chars_in,
        "output_chars_out": audit.output_chars_out,
        "output_preview": audit.output_preview,
    }
    for sink in sinks:
        try:
            result = sink.emit(event)
            if inspect.isawaitable(result):
                await result
        except Exception:  # noqa: BLE001 - best-effort observability path
            _log.exception("zicato:emulator audit emit failed; continuing")


__all__ = [
    "EmulatorTurnAudit",
    "audit_turn",
    "emit_audit_span",
]
