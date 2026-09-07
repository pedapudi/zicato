"""The dead-letter queue for inconclusive crowning duels.

When the Bradley--Terry promotion pre-gate (:mod:`zicato.selection.evidence_gate`)
exhausts its replicate budget and the rating CIs *still* overlap, the duel is
terminally ``"inconclusive"`` — the champion stands, but the unresolved verdict
must not be silently dropped. This module persists one record per such duel to
``<workspace>/runtime/inconclusive/<gen>.json`` (the dead-letter queue), so an
operator (and the dashboard) can see exactly which challenger could neither be
crowned nor cleanly rejected, and on what evidence.

The record is an additive runtime artifact: it exists ONLY on a run that opted
into the pre-gate AND reached the inconclusive terminal state, so every other
run's runtime tree is byte-identical to before this module existed.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from zicato.epoch._storage import RecordError
from zicato.runtime.paths import inconclusive_dir, inconclusive_record_path
from zicato.storage import atomic_write_json


@dataclass(frozen=True, slots=True)
class InconclusiveRecord:
    """One unresolved crowning duel captured in the dead-letter queue.

    Fields
    ------
    generation_id:
        The challenger generation whose promotion was held inconclusive.
    champion_id:
        The champion (incumbent) it duelled.
    epoch_id:
        The epoch the duel ran in (best-effort context for the reader).
    rating:
        The final ``gate.rating`` block (champion/challenger CIs, ``p_stronger``,
        ``threshold``, ``ci_overlap``, ``replicates_spent``, ``n_duels``) — the
        full evidence the verdict was terminal on.
    ci_history:
        The per-refit ``p_stronger`` / ``ci_overlap`` trace from the
        defer→replicate loop, so the reader can show the duel failing to
        converge.
    reason:
        The human-readable inconclusive reason.
    """

    generation_id: str
    champion_id: str
    epoch_id: str
    rating: Mapping[str, Any]
    ci_history: Sequence[Mapping[str, Any]]
    reason: str
    _json: str | None = field(default=None, repr=False, compare=False)

    def to_json(self) -> dict[str, Any]:
        """The persisted JSON shape."""
        fields = {
            "generation_id": self.generation_id,
            "champion_id": self.champion_id,
            "epoch_id": self.epoch_id,
            "rating": dict(self.rating),
            "ci_history": [dict(h) for h in self.ci_history],
            "reason": self.reason,
        }
        stored: dict[str, Any] = json.loads(self._json) if self._json is not None else {}
        stored.update(fields)
        return stored

    @classmethod
    def from_json(cls, body: Any) -> InconclusiveRecord:
        """Accept recorded duel identity and retain the evidence owner's payload."""
        if not isinstance(body, dict):
            raise RecordError("inconclusive duel: expected a JSON object")
        for key in ("generation_id", "champion_id", "epoch_id"):
            if not isinstance(body.get(key), str) or not body[key]:
                raise RecordError(f"inconclusive duel: {key} must be a nonempty string")
        if not isinstance(body.get("reason"), str):
            raise RecordError("inconclusive duel: reason must be a string")
        if not isinstance(body.get("rating"), dict):
            raise RecordError("inconclusive duel: rating must be an object")
        history = body.get("ci_history")
        if not isinstance(history, list) or any(not isinstance(row, dict) for row in history):
            raise RecordError("inconclusive duel: ci_history must be an array of objects")
        try:
            encoded = json.dumps(body, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise RecordError(f"inconclusive duel: invalid JSON value: {exc}") from exc
        accepted = json.loads(encoded)
        return cls(
            generation_id=accepted["generation_id"],
            champion_id=accepted["champion_id"],
            epoch_id=accepted["epoch_id"],
            rating=accepted["rating"],
            ci_history=tuple(accepted["ci_history"]),
            reason=accepted["reason"],
            _json=encoded,
        )


def record_inconclusive(workspace_root: Path, record: InconclusiveRecord) -> Path:
    """Persist one inconclusive duel to the dead-letter queue (atomic).

    Creates ``runtime/inconclusive/`` lazily and writes
    ``<generation_id>.json`` atomically. Returns the written path. Re-recording
    the same generation overwrites its prior record (a duel only goes
    inconclusive once per resolution).
    """
    body = InconclusiveRecord.from_json(record.to_json()).to_json()
    path = inconclusive_record_path(workspace_root, record.generation_id)
    atomic_write_json(path, body)
    return path


def read_inconclusive(workspace_root: Path, generation_id: str) -> InconclusiveRecord | None:
    """Read accepted duel facts; only an absent file returns ``None``."""
    path = inconclusive_record_path(workspace_root, generation_id)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise RecordError(f"inconclusive duel {path}: {exc}") from exc
    record = InconclusiveRecord.from_json(data)
    if record.generation_id != generation_id:
        raise RecordError(f"inconclusive duel {path}: generation_id does not match its location")
    return record


def list_inconclusive(workspace_root: Path) -> list[InconclusiveRecord]:
    """Every dead-letter record in the queue (sorted by generation id).

    Returns ``[]`` when the queue directory does not exist. Half-written
    ``.tmp`` artifacts an atomic write may transiently leave are skipped.
    """
    queue = inconclusive_dir(workspace_root)
    if not queue.is_dir():
        return []
    out: list[InconclusiveRecord] = []
    for path in sorted(queue.glob("*.json")):
        record = read_inconclusive(workspace_root, path.stem)
        if record is not None:
            out.append(record)
    return out


__all__ = [
    "InconclusiveRecord",
    "record_inconclusive",
    "read_inconclusive",
    "list_inconclusive",
]
