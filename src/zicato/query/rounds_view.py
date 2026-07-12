"""rounds_view — the epoch ROUND TIMELINE, served (not joined client-side).

Within ONE epoch the outer evolve loop runs N ROUNDS. Each round =
(an incoming CHAMPION, carried from the prior round) + (a freshly-minted
FIELD of challengers) -> a TOURNAMENT -> a GATE (one challenger may be
promoted). The epoch is therefore a CHAMPION SPINE threaded through the
rounds.

The frontend used to derive this model by JOINING four endpoints
(``/api/epoch`` + ``/api/lineage`` + ``/api/score-trajectory`` +
``/api/tournaments``) in ``rounds.js``. This reader is that join moved
server-side: ``GET /api/epoch/{id}/round-timeline`` serves the SETTLED
rounds + the loss-floor waterfall; the client only overlays the LIVE
in-flight round from its SSE state (a live overlay, not a re-derivation).

SOURCE PRIORITY (degrades gracefully when ``round_index`` is absent):
  (1) per-generation ``round_index`` (the authoritative birth-round stamp);
  (2) the per-round FIELD-TOURNAMENT records, one per round;
  (3) the gauntlet matchups, round-ordered by ``ran_at``;
  (4) nothing -> the whole epoch is a single round 0.
Racing skips (2)/(3): it is persisted per CHALLENGER, so without a
round-index stamp a racing epoch is a single round (its figure is the
racing-field ladder, served by :mod:`racing_view`).
"""

from __future__ import annotations

from typing import Any

from zicato.query.gate_view import build_score_trajectory
from zicato.query.lineage_view import build_lineage_view
from zicato.query.paths import (
    WorkspacePaths,
    _resolve_epoch_id,
)
from zicato.query.tournament_view import build_bracket


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
    traj = build_score_trajectory(paths, epoch_id)
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

    def _build_rounds(per_round: list[dict[str, Any]], source: str) -> list[dict[str, Any]]:
        carried = seed_id
        out: list[dict[str, Any]] = []
        for i, r in enumerate(per_round):
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
            promoted_challenger = next((c for c in challengers if c["promoted"]), None)
            gate = (
                {"kind": "promoted", "gen": promoted_challenger["id"]}
                if promoted_challenger
                else {"kind": "held", "gen": None}
            )
            # Prefer the CANONICAL per-round champion off the tournament
            # record ({id, scalar, eval_mode, run_ref}) over the
            # reconstructed spine + trajectory scalar.
            ref = r.get("tournament_ref")
            ref_champ = None
            if isinstance(ref, dict):
                rc = ref.get("champion")
                if isinstance(rc, dict) and rc.get("id") is not None:
                    ref_champ = rc
            champ_id = str(ref_champ["id"]) if ref_champ else carried
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
                    "round_index": (int(r["round_index"]) if _is_num(r.get("round_index")) else i),
                    "champion": champion,
                    "challengers": challengers,
                    "structure": structure,
                    "gate": gate,
                    "tournament_id": (
                        str(ref.get("tournament_id"))
                        if isinstance(ref, dict) and ref.get("tournament_id") is not None
                        else None
                    ),
                    "source": source,
                }
            )
            if promoted_challenger:
                carried = str(promoted_challenger["id"])
        return out

    def _payload(rounds: list[dict[str, Any]], source: str) -> dict[str, Any]:
        return {
            "epoch_id": epoch_id,
            "structure": structure,
            "source": source,
            "rounds": rounds,
            "waterfall": _waterfall(rounds),
        }

    # ── (1) per-gen round_index — the authoritative birth round ─────────
    if any(_is_num(g.get("round_index")) for g in gens):
        buckets: dict[int, list[str]] = {}
        for g in gens:
            gid = g.get("generation_id")
            if gid is None:
                continue
            ri_raw = g.get("round_index")
            # the seed champion is carried, not minted — no birth round.
            if str(gid) == str(seed_id) and not _is_num(ri_raw):
                continue
            ri = int(ri_raw) if isinstance(ri_raw, int | float) else 0
            buckets.setdefault(ri, []).append(str(gid))
        per_round = [
            {
                "round_index": ri,
                "challenger_ids": buckets[ri],
                "tournament_ref": _match_tournament_for_field(tournaments, buckets[ri]),
            }
            for ri in sorted(buckets)
        ]
        return _payload(_build_rounds(per_round, "round_index"), "round_index")

    # RACING is persisted per CHALLENGER — its records/matchups are not
    # distinct rounds. Without a round_index stamp it is a SINGLE round.
    if structure != "racing":
        # ── (2) per-round FIELD-TOURNAMENT records ────────────────────────
        field_records = [t for t in tournaments if _competitor_ids(t)]
        if field_records:
            seen: set[str] = set()
            per_round = []
            for i, t in enumerate(field_records):
                comps = _competitor_ids(t)
                fresh = [c for c in comps if c not in seen]
                seen.update(comps)
                per_round.append(
                    {
                        "round_index": i,
                        "challenger_ids": fresh if fresh else comps,
                        "tournament_ref": t,
                    }
                )
            return _payload(_build_rounds(per_round, "field"), "field")

        # ── (3) gauntlet matchups — each its own single-challenger round ──
        matchups = bracket.get("matchups")
        matchups = (
            [m for m in matchups if isinstance(m, dict)] if isinstance(matchups, list) else []
        )
        matchups.sort(key=lambda m: str(m.get("ran_at") or ""))
        if matchups:
            per_round = [
                {
                    "round_index": i,
                    "challenger_ids": [str(m.get("challenger"))],
                    "tournament_ref": None,
                }
                for i, m in enumerate(matchups)
                if m.get("challenger") is not None
            ]
            return _payload(_build_rounds(per_round, "matchups"), "matchups")

    # ── (4) single round 0 — every scored/promoted challenger, one round ──
    challenger_ids = [
        str(g["generation_id"])
        for g in gens
        if g.get("generation_id") is not None
        and str(g["generation_id"]) != str(seed_id)
        and (_scalar_of(str(g["generation_id"])) is not None or g.get("promoted"))
    ]
    single_ref = (
        next((t for t in tournaments if _competitor_ids(t)), None)
        if structure != "racing"
        else None
    )
    per_round = [{"round_index": 0, "challenger_ids": challenger_ids, "tournament_ref": single_ref}]
    return _payload(_build_rounds(per_round, "single"), "single")


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
