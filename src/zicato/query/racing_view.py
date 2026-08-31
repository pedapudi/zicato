"""The settled racing ladder for one epoch, joined from the persisted records.

A racing tournament is persisted as ONE record PER CHALLENGER, so the rung
and gate ladder the dashboard renders has to be JOINED out of those
per-challenger records. This reader owns that join, so the client reads ONE
settled racing-field payload and never re-derives rungs, survivors, or the
crowned winner.

``GET /api/epoch/{epoch_id}/racing-field`` serves :func:`build_racing_field`.
The payload uses the SAME structure envelope shape as
``/api/tournament-structure`` (``structure`` / ``structure_params`` /
``competitors`` / ``rounds`` / ``standings`` + ``champion_lineage``), with
``present: false`` when the epoch has no racing records. The frontend then
renders its honest empty state and reconstructs no ladder of its own.

That payload shape is the contract the frontend renders against; how this
reader assembles it is internal.
"""

from __future__ import annotations

import re
from typing import Any

from zicato.query.paths import (
    WorkspacePaths,
    _resolve_epoch_id,
)
from zicato.query.tournament_view import build_bracket

_RUNG_RE = re.compile(r"^rung(\d+)")


def _is_num(v: Any) -> bool:
    return isinstance(v, int | float) and not isinstance(v, bool)


def _opt_float(v: Any) -> float | None:
    return float(v) if isinstance(v, int | float) and not isinstance(v, bool) else None


def _rung_index_of(match_id: Any) -> int | None:
    m = _RUNG_RE.match(str(match_id or ""))
    return int(m.group(1)) if m else None


def _is_final(match_id: Any) -> bool:
    return str(match_id or "") == "racing-final"


def _absent(epoch_id: str | None) -> dict[str, Any]:
    return {"epoch_id": epoch_id, "present": False}


def build_racing_field(paths: WorkspacePaths, epoch_id: str | None = None) -> dict[str, Any]:
    """The settled racing ladder for one epoch, joined from the records.

    Returns ``{epoch_id, present, structure: "racing", structure_params,
    competitors, rounds, standings, champion_lineage, source:
    "reconstructed"}`` — one rung round per rung index (each match carrying
    ``competitors`` / ``survivors`` / ``cut`` / ``deltas`` /
    ``board_fraction``) plus the synthesized ``racing-final`` champion-gate
    round. ``present: false`` when the epoch has no racing records at all.
    """
    try:
        epoch_id = _resolve_epoch_id(paths, epoch_id)
    except ValueError:
        # an unknown / malformed epoch degrades to the absent shape.
        return _absent(str(epoch_id))
    if epoch_id is None:
        return _absent(None)
    bracket = build_bracket(paths, epoch_id)
    all_records = bracket.get("tournaments")
    all_records = all_records if isinstance(all_records, list) else []
    racing = [
        t
        for t in all_records
        if isinstance(t, dict)
        and str(t.get("structure")) == "racing"
        and isinstance(t.get("rounds"), list)
        and t["rounds"]
    ]
    if not racing:
        return _absent(epoch_id)
    lineage_raw = bracket.get("champion_lineage")
    lineage = [str(g) for g in lineage_raw] if isinstance(lineage_raw, list) else []

    def _envelope(
        rounds: list[dict[str, Any]],
        *,
        params: dict[str, Any],
        competitors: list[Any],
        standings: list[Any],
    ) -> dict[str, Any]:
        return {
            "epoch_id": epoch_id,
            "present": True,
            "structure": "racing",
            "structure_params": params,
            "competitors": competitors,
            "rounds": rounds,
            "standings": standings,
            "champion_lineage": lineage,
            "source": "reconstructed",
        }

    # ---- FAST PATH — an ASSEMBLED record whose rounds already hold the rung
    # field ({competitors, survivors, cut}); synthesize the gate from lineage
    # when the record itself committed no ``racing-final`` match.
    def _first_match(r: Any) -> dict[str, Any]:
        if isinstance(r, dict) and isinstance(r.get("matches"), list) and r["matches"]:
            m = r["matches"][0]
            return m if isinstance(m, dict) else {}
        return {}

    assembled = next(
        (
            t
            for t in racing
            if any(
                isinstance(_first_match(r).get("survivors"), list)
                or isinstance(_first_match(r).get("cut"), list)
                or isinstance(_first_match(r).get("competitors"), list)
                for r in (t.get("rounds") or [])
            )
        ),
        None,
    )
    if assembled is not None:
        rounds = list(assembled.get("rounds") or [])
        has_final = any(_is_final(_first_match(r).get("match_id")) for r in rounds)
        if not has_final:
            last_survivors: list[str] = []
            for r in reversed(rounds):
                m = _first_match(r)
                surv = m.get("survivors")
                if isinstance(surv, list) and surv:
                    last_survivors = [str(s) for s in surv]
                    break
            if len(last_survivors) == 1:
                survivor = last_survivors[0]
                promoted = bool(lineage) and lineage[-1] == survivor
                a_comps_raw = assembled.get("competitors")
                comp_ids = [
                    str(c.get("generation_id") if isinstance(c, dict) else c)
                    for c in (a_comps_raw if isinstance(a_comps_raw, list) else [])
                ]
                champ = next((c for c in comp_ids if c != survivor), None)
                rounds.append(
                    {
                        "stage_index": len(rounds),
                        "label": "Champion gate",
                        "matches": [
                            {
                                "match_id": "racing-final",
                                "competitors": [c for c in (champ, survivor) if c],
                                "winner": survivor if promoted else (champ or ""),
                                "decision": "promoted" if promoted else "rejected",
                                "board_fraction": 1.0,
                            }
                        ],
                    }
                )
        a_params = assembled.get("structure_params")
        a_standings = assembled.get("standings")
        a_comps = assembled.get("competitors")
        return _envelope(
            rounds,
            params=a_params if isinstance(a_params, dict) else {},
            competitors=a_comps if isinstance(a_comps, list) else [],
            standings=a_standings if isinstance(a_standings, list) else [],
        )

    # ---- PER-CHALLENGER JOIN — each record is one challenger's flattened
    # racing path; the challenger id is the ``->`` suffix of the tournament
    # id (the ingester convention), the champion is competitors[0].
    def _champion_of(t: dict[str, Any]) -> str | None:
        comps_raw = t.get("competitors")
        comps = [str(c) for c in comps_raw] if isinstance(comps_raw, list) else []
        return comps[0] if comps else None

    def _challenger_of(t: dict[str, Any]) -> str | None:
        tid = str(t.get("tournament_id") or "")
        arrow = tid.rfind("->")
        if arrow >= 0:
            return tid[arrow + 2 :]
        comps_raw = t.get("competitors")
        comps = [str(c) for c in comps_raw] if isinstance(comps_raw, list) else []
        if len(comps) > 1:
            return comps[1]
        return comps[0] if comps else None

    by_rung: dict[int, dict[str, dict[str, Any]]] = {}
    finalists: list[str] = []
    final_match: dict[str, dict[str, Any]] = {}
    champion_id: str | None = None
    for t in racing:
        chall = _challenger_of(t)
        if not chall:
            continue
        champ = _champion_of(t)
        if champ and champion_id is None:
            champion_id = champ
        for r in t.get("rounds") or []:
            if not isinstance(r, dict):
                continue
            mid = r.get("match_id")
            if _is_final(mid):
                if chall not in finalists:
                    finalists.append(chall)
                delta = _opt_float(r.get("delta_scalar"))
                final_match[chall] = {
                    "won": bool(r.get("won")),
                    "delta": delta,
                    "opponent": r.get("opponent") or champ or None,
                }
                continue
            ri = _rung_index_of(mid)
            if ri is None:
                continue
            by_rung.setdefault(ri, {})[chall] = {
                "delta": _opt_float(r.get("delta_scalar")),
                "won": bool(r.get("won")),
            }
    if not by_rung and not finalists:
        return _absent(epoch_id)

    # per-rung board fraction: rung N covers min(1, base * eta^N) of the board.
    params_raw = bracket.get("structure_params")
    params: dict[str, Any] = params_raw if isinstance(params_raw, dict) else {}
    eta_opt = _opt_float(params.get("eta"))
    eta = eta_opt if eta_opt is not None and eta_opt >= 2 else 2.0
    base_opt = _opt_float(params.get("board_fraction"))
    base_frac = base_opt if base_opt is not None and base_opt > 0 else None

    def _frac_for(ri: int) -> float | None:
        return None if base_frac is None else min(1.0, base_frac * (eta**ri))

    rung_idxs = sorted(by_rung)
    rounds = []
    for k, ri in enumerate(rung_idxs):
        field_map = by_rung[ri]
        field = list(field_map)
        next_field = by_rung.get(rung_idxs[k + 1]) if k + 1 < len(rung_idxs) else None
        survivors: list[str] = []
        cut: list[str] = []
        for c in field:
            carried = (next_field is not None and c in next_field) or c in finalists
            (survivors if carried else cut).append(c)
        rounds.append(
            {
                "stage_index": ri,
                "label": f"Rung {ri}",
                "matches": [
                    {
                        "match_id": f"rung{ri}",
                        "competitors": field,
                        "survivors": survivors,
                        "cut": cut,
                        "board_fraction": _frac_for(ri),
                        "deltas": {c: field_map[c]["delta"] for c in field},
                    }
                ],
            }
        )

    # The champion gate (the ``racing-final`` match): the lone finalist faces
    # the champion on the full board; the lineage-crowned finalist wins ties.
    if finalists:
        crowned = lineage[-1] if lineage else None
        survivor = crowned if (crowned in finalists) else finalists[0]
        fm = final_match.get(survivor, {})
        promoted = bool(fm.get("won"))
        champ = champion_id or fm.get("opponent") or None
        rounds.append(
            {
                "stage_index": (rung_idxs[-1] if rung_idxs else 0) + 1,
                "label": "Champion gate",
                "matches": [
                    {
                        "match_id": "racing-final",
                        "competitors": [c for c in (champ, survivor) if c],
                        "winner": survivor if promoted else (champ or ""),
                        "decision": "promoted" if promoted else "rejected",
                        "delta_scalar": fm.get("delta"),
                        "board_fraction": 1.0,
                    }
                ],
            }
        )

    standings_raw = bracket.get("standings")
    return _envelope(
        rounds,
        params=params,
        competitors=[],
        standings=standings_raw if isinstance(standings_raw, list) else [],
    )
