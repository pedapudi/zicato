"""promoted_head — WHICH member of a promoted set actually took the title.

``lineage.json`` is the single authority for topology and for the tri-state
promotion flag, and it deliberately says nothing beyond that. On a round that
promotes a SET rather than a single challenger — an operator multi-promote, a
tie — ``_apply_field_overrides`` marks EVERY member promoted in lineage while
only ONE, the primary head, moves the champion pointer and defends the next
round (``zicato.evolve.field``). "Which member headed the round" is therefore
not derivable from the lineage flags at all: a reader that takes the first
flagged member names a generation that never defended.

The runner records the head itself, on the round's durable field-tournament
snapshot (``zicato.evolve.dashboard_projection``), in two forms:

* ``promoted_generation_id`` — the head this round crowned, written at settle;
* ``champion_generation_id`` — the generation that DEFENDED this round, which
  is the head the previous round crowned (or the epoch's seed).

The SQLite index is not a source: its field-tournament row leaves the
per-matchup ``parent_generation_id`` / ``child_generation_id`` columns empty by
design (a field is a round, not a duel), so the head does not survive ingest.

The runtime ``current_generation`` marker stays UNREAD here, by doctrine and by
shape. Doctrine: the query layer serves recorded history, and the marker is the
loop's live pointer. Shape: it holds ONE id for the whole epoch, so it can
never answer which generation headed round 3 — only the per-round records can.
"""

from __future__ import annotations

from dataclasses import dataclass

from zicato.query.paths import WorkspacePaths, _read_json_value


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
    """One entry per readable field-tournament snapshot, in file-name order.

    Best-effort throughout: a missing directory, an unreadable file or a
    malformed record yields fewer entries, never an error.
    """
    from zicato.core.workspace import field_tournaments_dir  # noqa: PLC0415

    heads: list[RecordedHead] = []
    tdir = field_tournaments_dir(paths.root, epoch_id)
    if not tdir.is_dir():
        return heads
    for record_path in sorted(tdir.glob("field-*.json")):
        record = _read_json_value(record_path)
        if not isinstance(record, dict):
            continue
        tournament_id = record.get("tournament_id")
        if not isinstance(tournament_id, str) or not tournament_id:
            continue
        head = record.get("promoted_generation_id")
        champion = record.get("champion_generation_id")
        heads.append(
            RecordedHead(
                tournament_id=tournament_id,
                generation_id=head if isinstance(head, str) else "",
                champion_generation_id=champion if isinstance(champion, str) else "",
            )
        )
    return heads


def head_of_round(heads: list[RecordedHead], tournament_id: str | None) -> str | None:
    """The recorded head of ONE round, or ``None`` when no record names one.

    Matched on the field-tournament id EXACTLY — the durable snapshot and the
    served bracket record carry the same ``{epoch}:field:{first challenger}``
    id, so no heuristic is needed and none is used: matching on a competitor
    overlap would let round N+1's record (whose champion is one of round N's
    challengers) claim round N.

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
