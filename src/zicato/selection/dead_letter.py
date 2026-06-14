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

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from zicato.runtime.paths import inconclusive_dir, inconclusive_record_path
from zicato.storage._atomic import atomic_write_json, read_json


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

    def to_json(self) -> dict[str, Any]:
        """The persisted JSON shape."""
        return {
            "generation_id": self.generation_id,
            "champion_id": self.champion_id,
            "epoch_id": self.epoch_id,
            "rating": dict(self.rating),
            "ci_history": [dict(h) for h in self.ci_history],
            "reason": self.reason,
        }


def record_inconclusive(workspace_root: Path, record: InconclusiveRecord) -> Path:
    """Persist one inconclusive duel to the dead-letter queue (atomic).

    Creates ``runtime/inconclusive/`` lazily and writes
    ``<generation_id>.json`` atomically. Returns the written path. Re-recording
    the same generation overwrites its prior record (a duel only goes
    inconclusive once per resolution).
    """
    inconclusive_dir(workspace_root).mkdir(parents=True, exist_ok=True)
    path = inconclusive_record_path(workspace_root, record.generation_id)
    atomic_write_json(path, record.to_json())
    return path


def read_inconclusive(workspace_root: Path, generation_id: str) -> dict[str, Any] | None:
    """Read one dead-letter record, or ``None`` when absent / unreadable.

    Tolerant by contract: an absent file (the default for every run that never
    hit the inconclusive state) returns ``None`` rather than raising, so the
    dashboard reader degrades cleanly to "no dead-letter record".
    """
    path = inconclusive_record_path(workspace_root, generation_id)
    data = read_json(path)
    if not isinstance(data, dict):
        return None
    return data


def list_inconclusive(workspace_root: Path) -> list[dict[str, Any]]:
    """Every dead-letter record in the queue (sorted by generation id).

    Returns ``[]`` when the queue directory does not exist. Half-written
    ``.tmp`` artifacts an atomic write may transiently leave are skipped.
    """
    queue = inconclusive_dir(workspace_root)
    if not queue.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for path in sorted(queue.glob("*.json")):
        if path.name.endswith(".tmp"):
            continue
        data = read_json(path)
        if isinstance(data, dict):
            out.append(data)
    return out


__all__ = [
    "InconclusiveRecord",
    "record_inconclusive",
    "read_inconclusive",
    "list_inconclusive",
]
