"""Prepare tournament diagrams when execution publishes its results."""

from __future__ import annotations

from typing import Any


def attach_elim_states(payload: dict[str, Any]) -> dict[str, Any]:
    """Record advancement and elimination beside the executed matches."""
    structure = payload.get("structure")
    if structure not in {"single_elim", "double_elim"}:
        return payload
    states: dict[str, dict[str, Any]] = {}
    rounds = []
    for column, stage in enumerate(payload["rounds"]):
        matches = []
        for match in stage["matches"]:
            competitors = match["competitors"]
            winner = match.get("winner") or None
            pending = bool(match.get("pending"))
            bye = bool(match.get("bye"))
            slot = match.get("bracket_slot", "")
            side = "LB" if slot.startswith("LB") else "WB"
            loser = (
                next((gid for gid in competitors if gid != winner), None)
                if winner and not bye
                else None
            )
            # A winners' bracket loss retains the second chance in double
            # elimination even before a losers' bracket match is scheduled.
            eliminates = structure == "single_elim" or side == "LB" or slot == "GF"
            for gid in competitors:
                state = states.setdefault(
                    gid,
                    {
                        "generation_id": gid,
                        "played_rounds": [],
                        "advanced_rounds": [],
                        "lost_rounds": [],
                        "eliminated_at_round": None,
                        "side_by_round": {},
                        "lb_entry_round": None,
                        "projected": None,
                    },
                )
                if column not in state["played_rounds"]:
                    state["played_rounds"].append(column)
                state["side_by_round"][str(column)] = side
                if side == "LB" and state["lb_entry_round"] is None:
                    state["lb_entry_round"] = column
                if pending:
                    projection = (match.get("projected") or {}).get(gid)
                    if projection is not None:
                        state["projected"] = projection
                elif bye or gid == winner:
                    if column not in state["advanced_rounds"]:
                        state["advanced_rounds"].append(column)
                elif winner:
                    if column not in state["lost_rounds"]:
                        state["lost_rounds"].append(column)
                    if eliminates:
                        state["eliminated_at_round"] = column
            matches.append({**match, "loser": loser})
        rounds.append(
            {
                **stage,
                "matches": matches,
                "bracket_side": "LB"
                if any(m.get("bracket_slot", "").startswith("LB") for m in matches)
                else "WB",
            }
        )
    return {**payload, "rounds": rounds, "gen_states": list(states.values())}
