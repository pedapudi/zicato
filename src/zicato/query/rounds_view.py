"""The epoch's recorded rounds, active proposal status and loss-floor waterfall.

An experiment's integer birth-round stamp assigns its generation to a field.
The seed and each promoted champion carry forward into later fields. Lineage
supplies parentage and promotion decisions; tournament records identify the
champion and gate winner within each field.

Unapplied proposals have no experiment stamp. The active tournament supplies
those proposals and their statuses until publication assigns a birth round.
An epoch without applied challengers has no settled rounds.
"""

from __future__ import annotations

import logging
from typing import Any

from zicato.query.gate_view import build_score_trajectory
from zicato.query.lineage_view import build_lineage_view
from zicato.query.paths import (
    WorkspacePaths,
    _resolve_epoch_id,
)
from zicato.query.promoted_head import head_of_round, read_recorded_heads
from zicato.query.runtime_view import read_active_tournament_dict
from zicato.query.tournament_view import build_bracket

log = logging.getLogger("zicato.query")


def _is_num(v: Any) -> bool:
    return isinstance(v, int | float) and not isinstance(v, bool)


def _competitor_ids(record: dict[str, Any]) -> list[str]:
    comps = record.get("competitors")
    out: list[str] = []
    for c in comps if isinstance(comps, list) else []:
        gid = c.get("generation_id") if isinstance(c, dict) else c
        if gid is not None and str(gid):
            out.append(str(gid))
    return out


def _match_tournament_for_field(
    tournaments: list[dict[str, Any]], challenger_ids: list[str]
) -> dict[str, Any] | None:
    """The tournaments[] record whose competitors best cover a round's field."""
    if not tournaments or not challenger_ids:
        return None
    want = {str(c) for c in challenger_ids}
    best: dict[str, Any] | None = None
    best_hits = 0
    for t in tournaments:
        if not isinstance(t, dict):
            continue
        hits = sum(1 for c in _competitor_ids(t) if c in want)
        if hits > best_hits:
            best_hits = hits
            best = t
    return best if best_hits > 0 else None


def build_round_timeline(paths: WorkspacePaths, epoch_id: str | None = None) -> dict[str, Any]:
    """``GET /api/epoch/{id}/round-timeline`` — the settled rounds + waterfall.

    Returns::

        {
          "epoch_id", "structure", "source",
          "rounds": [{round_index,
                      champion: {id, scalar, eval_mode, run_ref, from_record},
                      challengers: [{id, scalar, promoted}],
                      structure, gate: {kind, gen},
                      tournament_id | null, source}],
          "waterfall": [{round_index, from, to, delta, promoted, gen}],
        }

    ``gate.kind`` is ``"promoted"`` (with ``gen`` the winning challenger) or
    ``"held"``. ``tournament_id`` names the round's field record in the
    ``/api/tournaments`` payload (an id-keyed lookup — the frontend never
    heuristically matches records to rounds). Degrades to an empty rounds
    list when there is no epoch.
    """
    try:
        epoch_id = _resolve_epoch_id(paths, epoch_id)
    except ValueError:
        # an unknown / malformed epoch degrades to the empty timeline.
        epoch_id = None
    if epoch_id is None:
        return {
            "epoch_id": None,
            "structure": "gauntlet",
            "source": "none",
            "rounds": [],
            "waterfall": [],
        }

    lineage = build_lineage_view(paths, epoch_id, include_ratings=False)
    gens: list[dict[str, Any]] = [g for g in lineage.get("generations", []) if isinstance(g, dict)]
    # Hand over the feed just walked instead of making the trajectory walk
    # the same epoch a second time — both are scoped to ``epoch_id``, and
    # the trajectory's own epoch filter is a no-op on an already-scoped feed.
    traj = build_score_trajectory(paths, epoch_id, lineage=lineage)
    scalar_by: dict[str, float] = {}
    for p in traj.get("points", []) or []:
        if isinstance(p, dict) and _is_num(p.get("scalar")):
            scalar_by[str(p.get("generation_id"))] = float(p["scalar"])
    bracket = build_bracket(paths, epoch_id)
    structure = str(bracket.get("structure") or "gauntlet")
    tournaments = bracket.get("tournaments")
    tournaments = (
        [t for t in tournaments if isinstance(t, dict)] if isinstance(tournaments, list) else []
    )
    lineage_ids_raw = bracket.get("champion_lineage")
    lineage_ids = [str(g) for g in lineage_ids_raw] if isinstance(lineage_ids_raw, list) else []

    by_id = {str(g["generation_id"]): g for g in gens if g.get("generation_id") is not None}

    def _scalar_of(gid: str | None) -> float | None:
        return scalar_by.get(str(gid)) if gid is not None else None

    def _promoted_of(gid: str) -> bool:
        g = by_id.get(str(gid))
        return bool(g and g.get("promoted"))

    # The SEED = round 0's INCOMING champion — the lineage ROOT, else the
    # parentless generation, never the current/reigning champion.
    parentless = next((g for g in gens if not g.get("parent_generation_id")), None)
    seed_id: str | None
    if lineage_ids:
        seed_id = lineage_ids[0]
    elif parentless is not None:
        seed_id = str(parentless["generation_id"])
    else:
        seed_id = None

    # The runner's own per-round statement of WHICH member of a promoted set
    # took the title. Read once for the epoch and matched per round below.
    recorded_heads = read_recorded_heads(paths, epoch_id)

    def _build_rounds(per_round: list[dict[str, Any]], source: str) -> list[dict[str, Any]]:
        carried = seed_id
        out: list[dict[str, Any]] = []
        for r in per_round:
            challengers = []
            for gid in r["challenger_ids"]:
                gid = str(gid)
                if carried is not None and gid == str(carried):
                    continue  # the carried champion is never a "minted" challenger
                challengers.append(
                    {
                        "id": gid,
                        "scalar": _scalar_of(gid),
                        "promoted": _promoted_of(gid),
                    }
                )
            round_index = r["round_index"]
            ref = r.get("tournament_ref")
            tournament_id = (
                str(ref.get("tournament_id"))
                if isinstance(ref, dict) and ref.get("tournament_id") is not None
                else None
            )
            head = head_of_round(recorded_heads, tournament_id)
            gate = (
                {"kind": "promoted", "gen": head}
                if head is not None
                else {"kind": "held", "gen": None}
            )
            # Prefer the CANONICAL per-round champion off the tournament
            # record ({id, scalar, eval_mode, run_ref}) over the
            # reconstructed spine + trajectory scalar.
            ref_champ = None
            if isinstance(ref, dict):
                rc = ref.get("champion")
                if isinstance(rc, dict) and rc.get("id") is not None:
                    ref_champ = rc
            champ_id = str(ref_champ["id"]) if ref_champ else carried
            # The record stays the champion source, but the spine is now a real
            # cross-check: ``carried`` advances by the recorded head above, so a
            # divergence here is two records disagreeing rather than the known
            # first-match weakness. Surfaced as a log line, never a payload
            # field — the served answer is the record either way.
            if ref_champ is not None and carried is not None and champ_id != str(carried):
                log.info(
                    "round-timeline: epoch %s round %s — record champion %s disagrees with the "
                    "carried spine %s; serving the record",
                    epoch_id,
                    round_index,
                    champ_id,
                    carried,
                )
            champion = {
                "id": champ_id,
                "scalar": (
                    float(ref_champ["scalar"])
                    if ref_champ and _is_num(ref_champ.get("scalar"))
                    else _scalar_of(champ_id)
                ),
                "eval_mode": (ref_champ.get("eval_mode") if ref_champ else None),
                "run_ref": (ref_champ.get("run_ref") if ref_champ else None),
                "from_record": ref_champ is not None,
            }
            out.append(
                {
                    "round_index": round_index,
                    "champion": champion,
                    "challengers": challengers,
                    "structure": structure,
                    "gate": gate,
                    "tournament_id": tournament_id,
                    "tournament": ref if isinstance(ref, dict) else None,
                    "source": source,
                }
            )
            if head is not None:
                carried = head
        return out

    def _payload(rounds: list[dict[str, Any]], source: str) -> dict[str, Any]:
        active = read_active_tournament_dict(paths)
        if isinstance(active, dict) and active.get("epoch_id") in (None, epoch_id):
            phase = str(active.get("phase") or "").split(":", 1)[0].lower()
            field = active.get("field_status")
            if phase in {"proposing", "applying"} and isinstance(field, list):
                ids = [
                    str(row["generation_id"])
                    for row in field
                    if isinstance(row, dict) and row.get("generation_id")
                ]
                seen = {str(c["id"]) for r in rounds for c in r.get("challengers", [])}
                if ids and not seen.intersection(ids):
                    last = rounds[-1] if rounds else None
                    # The in-flight round's incoming champion is the previous
                    # round's HEAD — read off its settled gate, which already
                    # resolved the recorded one, rather than re-deriving it from
                    # the lineage flags (which cannot name a head).
                    last_gate = (last or {}).get("gate") or {}
                    champion_id = (
                        last_gate.get("gen")
                        or ((last or {}).get("champion") or {}).get("id")
                        or seed_id
                    )
                    projected = active.get("projected")
                    projected = projected if isinstance(projected, dict) else {}
                    status = {
                        str(row.get("generation_id")): row.get("status")
                        for row in field
                        if isinstance(row, dict)
                    }
                    live = {
                        "round_index": active.get("round_index", len(rounds)),
                        "champion": {
                            "id": champion_id,
                            "scalar": _scalar_of(champion_id),
                            "eval_mode": None,
                            "run_ref": None,
                            "from_record": False,
                        },
                        "challengers": [
                            {
                                "id": gid,
                                "scalar": (projected.get(gid) or {}).get("scalar")
                                if isinstance(projected.get(gid), dict)
                                else _scalar_of(gid),
                                "promoted": _promoted_of(gid),
                                "status": status.get(gid) or "proposing",
                                "projected": gid in projected,
                                "boards_done": (projected.get(gid) or {}).get("boards_done")
                                if isinstance(projected.get(gid), dict)
                                else None,
                                "boards_total": (projected.get(gid) or {}).get("boards_total")
                                if isinstance(projected.get(gid), dict)
                                else None,
                            }
                            for gid in ids
                        ],
                        "structure": structure,
                        "gate": {"kind": "pending", "gen": None},
                        "tournament_id": None,
                        "tournament": None,
                        "source": "inflight",
                        "inflight": True,
                        "phase": active.get("phase"),
                    }
                    same = last is not None and last.get("round_index") == live["round_index"]
                    replace = same and last is not None and not last.get("challengers")
                    rounds = [*rounds[:-1], live] if replace else [*rounds, live]
        return {
            "epoch_id": epoch_id,
            "structure": structure,
            "source": source,
            "rounds": rounds,
            "waterfall": _waterfall(rounds),
        }

    buckets: dict[int, list[str]] = {}
    for generation in gens:
        generation_id = generation.get("generation_id")
        round_index = generation.get("round_index")
        # Unapplied proposals have no birth round; their status belongs to
        # the active tournament projection below.
        if generation_id is None or type(round_index) is not int:
            continue
        # A parentless seed is carried into the field, never minted by it.
        if str(generation_id) == str(seed_id) and not generation.get("parent_generation_id"):
            continue
        buckets.setdefault(round_index, []).append(str(generation_id))
    per_round = [
        {
            "round_index": round_index,
            "challenger_ids": buckets[round_index],
            "tournament_ref": _match_tournament_for_field(tournaments, buckets[round_index]),
        }
        for round_index in sorted(buckets)
    ]
    return _payload(_build_rounds(per_round, "round_index"), "round_index")


def _waterfall(rounds: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The loss-floor waterfall steps for the settled rounds.

    Each round is a step: ``from`` = the incoming champion's loss floor,
    ``to`` = the outgoing floor (the promoted challenger's loss when the
    gate promoted; a held round keeps the floor flat); ``delta`` = to -
    from (negative = the floor dropped); ``promoted`` flags a real
    promotion step; ``gen`` names the winning mutation.
    """
    out: list[dict[str, Any]] = []
    for r in rounds:
        champion = r.get("champion") or {}
        frm: float | None = float(champion["scalar"]) if _is_num(champion.get("scalar")) else None
        gate = r.get("gate") or {}
        promoted = bool(gate.get("kind") == "promoted" and gate.get("gen") is not None)
        gen = gate.get("gen") if promoted else None
        to: float | None = frm if _is_num(frm) else None
        if promoted and gen is not None:
            winner = next(
                (c for c in (r.get("challengers") or []) if str(c.get("id")) == str(gen)),
                None,
            )
            if winner is not None and _is_num(winner.get("scalar")):
                to = float(winner["scalar"])
        delta = (to - frm) if (frm is not None and to is not None) else None
        out.append(
            {
                "round_index": r.get("round_index"),
                "from": frm,
                "to": to,
                "delta": delta,
                "promoted": bool(promoted and delta is not None and delta != 0),
                "gen": gen,
            }
        )
    return out
