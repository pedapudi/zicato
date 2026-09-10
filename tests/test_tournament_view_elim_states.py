"""Tournament publication records the analysis used by every diagram reader."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from zicato.tournament.structure import attach_elim_states

DATA = Path(__file__).parent / "data"
CASES = json.loads((DATA / "elim_states_cases.json").read_text())
SERVED = json.loads((DATA / "elim_states_served.json").read_text())


@pytest.mark.parametrize("name", CASES)
def test_recorded_diagrams(name: str) -> None:
    structure = (
        "double_elim"
        if name.startswith("double_elim") or name == "losers_bracket_drop"
        else "single_elim"
    )
    result = attach_elim_states({"structure": structure, "rounds": CASES[name]})
    assert {key: result[key] for key in ("rounds", "gen_states")} == SERVED[name]


def test_first_loss_retains_double_elimination_candidate() -> None:
    rounds = [
        {
            "stage_index": 0,
            "matches": [
                {
                    "match_id": "WB-R0-0",
                    "bracket_slot": "WB-R0-0",
                    "competitors": ["v1", "v2"],
                    "winner": "v1",
                    "pending": False,
                }
            ],
        }
    ]
    result = attach_elim_states({"structure": "double_elim", "rounds": rounds})
    candidate = result["gen_states"][1]
    assert candidate["lost_rounds"] == [0]
    assert candidate["eliminated_at_round"] is None
    assert "loser" not in rounds[0]["matches"][0]


def test_second_loss_eliminates_double_elimination_candidate() -> None:
    rounds = [
        {
            "stage_index": index,
            "matches": [
                {
                    "match_id": slot,
                    "bracket_slot": slot,
                    "competitors": ["v1", "v2"],
                    "winner": "v1",
                    "pending": False,
                }
            ],
        }
        for index, slot in enumerate(("WB-R0-0", "LB-R1-0"))
    ]
    result = attach_elim_states({"structure": "double_elim", "rounds": rounds})
    candidate = result["gen_states"][1]
    assert candidate["lost_rounds"] == [0, 1]
    assert candidate["lb_entry_round"] == 1
    assert candidate["eliminated_at_round"] == 1


def test_other_formats_retain_their_records() -> None:
    record = {"structure": "swiss", "rounds": []}
    assert attach_elim_states(record) is record
