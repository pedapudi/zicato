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
    """The recorded head of ONE round, or ``None`` when no record names one.

    Matched on the field-tournament id EXACTLY: the durable snapshot and the
    served bracket record carry the same ``{epoch}:field:{first challenger}``
    id, so no heuristic is needed and none is used. Matching on a competitor
    overlap would let round N+1's record claim round N, because its champion
    is one of round N's challengers.

    The returned id is the record's verbatim claim. Whether it belongs to the
    round's lineage-promoted set is the CALLER's check, so a record that names
    a generation outside that set stays visible as a disagreement instead of
    being silently dropped here.
    """
    if not tournament_id:
        return None
    match = next((h for h in heads if h.tournament_id == tournament_id), None)
    return match.generation_id or None if match is not None else None


def recorded_head_ids(heads: list[RecordedHead]) -> frozenset[str]:
    """Every generation the epoch's records name as a promoted head.

    Both recorded forms count: a round's own crowned head, and the defender
    every later round names — a generation defends a round only by having
    headed the one before it. For a reader that needs to know WHETHER an id
    was ever a head (resolving a branched spine) rather than which round it
    headed. The epoch's seed rides in as round 0's defender, which is
    harmless: it is the spine's root by construction.
    """
    return frozenset(
        gid for head in heads for gid in (head.generation_id, head.champion_generation_id) if gid
    )
