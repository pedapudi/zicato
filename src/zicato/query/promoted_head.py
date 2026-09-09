"""Read the champion named by each tournament, including multiple promotions.

Ancestry records parent relationships. Completed rounds record promotion status
and identify the primary promoted candidate. Historical views use those recorded
identities to distinguish the defending champion from other promoted candidates.
"""

from __future__ import annotations

from dataclasses import dataclass

from zicato.query.paths import WorkspacePaths


@dataclass(frozen=True, slots=True)
class RecordedHead:
    """One field round's recorded head and recorded defender.

    ``generation_id`` is empty for a round that HELD and for a round whose
    snapshot was written while it was still in flight (the envelope is opened
    before the bracket resolves) — an empty head means "this record names no
    head", never "no promotion happened".
    """

    tournament_id: str
    generation_id: str
    champion_generation_id: str


def read_recorded_heads(paths: WorkspacePaths, epoch_id: str) -> list[RecordedHead]:
    """Read the named champion and defender of each recorded tournament."""
    from zicato.tournament.records import field_tournament_records

    try:
        records = field_tournament_records(paths.root, epoch_id)
    except (OSError, ValueError, RuntimeError):
        return []
    return [
        RecordedHead(
            record.tournament_id, record.promoted_generation_id, record.champion_generation_id
        )
        for record in records
    ]


def head_of_round(heads: list[RecordedHead], tournament_id: str | None) -> str | None:
    """Read a tournament's primary promotion by its exact recorded identity."""
    if not tournament_id:
        return None
    match = next((h for h in heads if h.tournament_id == tournament_id), None)
    return match.generation_id or None if match is not None else None


def current_champion(paths: WorkspacePaths, epoch_id: str) -> str | None:
    """Read the committed champion; an unreadable record supplies no inferred winner."""
    from zicato.epoch.settlement_receipt import recorded_champion

    try:
        return recorded_champion(paths.root, epoch_id)
    except (OSError, ValueError, RuntimeError):
        return None


def champion_history(paths: WorkspacePaths, epoch_id: str) -> list[str]:
    """Read the baseline and committed primary promotions in round order."""
    from zicato.epoch.settlement_receipt import recorded_champions

    try:
        return recorded_champions(paths.root, epoch_id)
    except (OSError, ValueError, RuntimeError):
        return []
