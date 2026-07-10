"""The served ELIM MODEL fold — ``derive_elim_states`` (U3, DQ1).

The client's elimFlow used to derive this model per render (the column
re-sort, the duplicate-match collapse, the elimination-vs-drop pass, the
phantom-✕ guards). These tests pin the SERVER fold that replaced it:

* the shared fixture ``tests/data/elim_states_fixture.json`` — asserted
  byte-for-byte here AND by the Rust twin
  (``crates/supervisor/src/elim_states.rs``), the ch08 parity recipe;
* one named property per behavior: dedupe / drop-vs-elimination / bye /
  pending / the single-round degenerate double-booking / a winner
  without a loser / the malformed-blob degrade;
* the ``attach_elim_states`` wiring: elim payloads gain the model,
  non-elim payloads pass through KEY-ABSENT.
"""

from __future__ import annotations

import json
from pathlib import Path

from zicato.query.tournament_view import attach_elim_states, derive_elim_states

_FIXTURE = Path(__file__).parent / "data" / "elim_states_fixture.json"


def _match(slot: str, comps: list[str], winner: str | None = None, **kw: object) -> dict:
    m: dict = {"match_id": slot, "bracket_slot": slot, "competitors": comps, "winner": winner}
    m.update(kw)
    return m


def _gs(out: dict, gid: str) -> dict:
    return next(g for g in out["gen_states"] if g["generation_id"] == gid)


# ---------------------------------------------------------------------------
# The shared Python↔Rust fixture (the parity pin)
# ---------------------------------------------------------------------------


def test_shared_fixture_pins_the_fold() -> None:
    """input → expected, byte-for-byte — the Rust twin asserts the SAME file."""
    fixture = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    got = derive_elim_states(fixture["input_rounds"])
    assert got == fixture["expected"]


def test_shared_fixture_malformed_competitors_case() -> None:
    """F1: the DQ1 scalar contract — non-scalar competitors/winner (bool,
    null, object, array) drop identically across all three folds. The Rust
    and node twins assert the SAME fixture case."""
    fixture = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    case = fixture["malformed_competitors_case"]
    got = derive_elim_states(case["input_rounds"])
    assert got == case["expected"]


# ---------------------------------------------------------------------------
# Named properties
# ---------------------------------------------------------------------------


def test_rounds_are_sorted_temporally_and_sided() -> None:
    """Mis-ordered input (GF before LB) serves WB → LB → GF, sides stamped."""
    out = derive_elim_states(
        [
            {"round_index": 2, "matches": [_match("GF", ["v0", "v1"], "v1")]},
            {"round_index": 1, "matches": [_match("LB-R1-0", ["v2", "v3"], "v2")]},
            {"round_index": 0, "matches": [_match("WB-R0-0", ["v1", "v2"], "v1")]},
        ]
    )
    assert [r["round_index"] for r in out["rounds"]] == [0, 1, 2]
    assert [r["bracket_side"] for r in out["rounds"]] == ["WB", "LB", "WB"]


def test_stage_index_is_accepted_for_the_sort() -> None:
    out = derive_elim_states(
        [
            {"stage_index": 1, "matches": [_match("F", ["v1", "v3"], "v1")]},
            {"stage_index": 0, "matches": [_match("WB-R0-0", ["v1", "v2"], "v1")]},
        ]
    )
    assert [r["stage_index"] for r in out["rounds"]] == [0, 1]


def test_dedupe_keeps_the_most_decided_duplicate() -> None:
    """A pending duplicate never shadows the settled record, in either order."""
    settled = _match("LB-R1-0", ["v2", "v4"], "v2")
    pending = _match("LB-R1-0", ["v2", "v4"], None, pending=True)
    for matches in ([pending, settled], [settled, pending]):
        out = derive_elim_states([{"round_index": 0, "matches": matches}])
        (only,) = out["rounds"][0]["matches"]
        assert only["winner"] == "v2"
        assert only["loser"] == "v4"


def test_distinct_matches_sharing_a_column_are_not_collapsed() -> None:
    out = derive_elim_states(
        [
            {
                "round_index": 0,
                "matches": [
                    _match("WB-R0-0", ["v1", "v2"], "v1"),
                    _match("WB-R0-1", ["v3", "v4"], "v3"),
                ],
            }
        ]
    )
    assert len(out["rounds"][0]["matches"]) == 2


def test_drop_vs_elimination() -> None:
    """A loss with a later appearance is a WB→LB drop; without one, terminal."""
    out = derive_elim_states(
        [
            {"round_index": 0, "matches": [_match("WB-R0-0", ["v1", "v2"], "v1")]},
            {"round_index": 1, "matches": [_match("LB-R1-0", ["v2", "v3"], "v3")]},
        ]
    )
    v2 = _gs(out, "v2")
    assert v2["lost_rounds"] == [0, 1]
    assert v2["eliminated_at_round"] == 1  # the col-0 loss was a drop (played later)
    assert v2["lb_entry_round"] == 1
    assert v2["side_by_round"] == {"0": "WB", "1": "LB"}
    v3 = _gs(out, "v3")
    assert v3["eliminated_at_round"] is None  # won its only match


def test_bye_advances_without_a_loser() -> None:
    out = derive_elim_states(
        [{"round_index": 0, "matches": [_match("WB-R0-1", ["v3"], "v3", bye=True)]}]
    )
    (only,) = out["rounds"][0]["matches"]
    assert only["loser"] is None
    v3 = _gs(out, "v3")
    assert v3["advanced_rounds"] == [0]
    assert v3["eliminated_at_round"] is None


def test_pending_match_eliminates_nobody() -> None:
    out = derive_elim_states(
        [{"round_index": 0, "matches": [_match("final", ["v0", "v1"], None, pending=True)]}]
    )
    for gid in ("v0", "v1"):
        gs = _gs(out, gid)
        assert gs["played_rounds"] == [0]
        assert gs["advanced_rounds"] == []
        assert gs["lost_rounds"] == []
        assert gs["eliminated_at_round"] is None
    (only,) = out["rounds"][0]["matches"]
    assert only["loser"] is None


def test_no_winner_no_decision_no_pending_flag_reads_pending() -> None:
    """The implicit-pending rule: no winner + no bye + no decision."""
    out = derive_elim_states([{"round_index": 0, "matches": [_match("m0", ["v1", "v2"])]}])
    assert _gs(out, "v1")["eliminated_at_round"] is None
    assert _gs(out, "v2")["lost_rounds"] == []


def test_single_round_degenerate_double_booking_elimination_wins() -> None:
    """One gen in TWO col-0 matches (a win AND a loss): elimination wins.

    The champion-vs-field seeding can put one generation in two matches
    of the same column; the render rule (and the old client pass) is that
    the loss terminates the lane there when nothing is played later.
    """
    out = derive_elim_states(
        [
            {
                "round_index": 0,
                "matches": [
                    _match("WB-R0-0", ["v1", "v2"], "v1"),
                    _match("WB-R0-1", ["v1", "v3"], "v3"),
                ],
            }
        ]
    )
    v1 = _gs(out, "v1")
    assert v1["advanced_rounds"] == [0]
    assert v1["lost_rounds"] == [0]
    assert v1["eliminated_at_round"] == 0  # no later column → the loss terminates


def test_projected_rides_only_a_pending_match() -> None:
    proj = {"v1": {"scalar": 0.5, "boards_done": 1, "boards_total": 4}}
    settled = derive_elim_states(
        [{"round_index": 0, "matches": [_match("m", ["v0", "v1"], "v1", projected=proj)]}]
    )
    assert _gs(settled, "v1")["projected"] is None
    pending = derive_elim_states(
        [
            {
                "round_index": 0,
                "matches": [_match("m", ["v0", "v1"], None, pending=True, projected=proj)],
            }
        ]
    )
    assert _gs(pending, "v1")["projected"] == proj["v1"]


def test_tbd_and_empty_competitors_are_ignored() -> None:
    out = derive_elim_states(
        [{"round_index": 0, "matches": [_match("m", ["v1", "tbd", ""], None, pending=True)]}]
    )
    assert [g["generation_id"] for g in out["gen_states"]] == ["v1"]


def test_malformed_blob_degrades_to_empty(tmp_path: Path) -> None:
    assert derive_elim_states(None) == {"rounds": [], "gen_states": []}
    assert derive_elim_states("junk") == {"rounds": [], "gen_states": []}
    assert derive_elim_states([1, "x", None]) == {"rounds": [], "gen_states": []}
    out = derive_elim_states([{"round_index": 0}])  # no matches list
    assert out["rounds"][0]["matches"] == []
    assert out["gen_states"] == []


# ---------------------------------------------------------------------------
# attach_elim_states — the wiring seam
# ---------------------------------------------------------------------------


def test_attach_enriches_elim_and_skips_others() -> None:
    rounds = [
        {"round_index": 1, "matches": [_match("F", ["v1", "v3"], "v1")]},
        {"round_index": 0, "matches": [_match("WB-R0-0", ["v1", "v2"], "v1")]},
    ]
    elim = attach_elim_states({"structure": "single_elim", "rounds": [*rounds]})
    assert [r["round_index"] for r in elim["rounds"]] == [0, 1]
    assert {g["generation_id"] for g in elim["gen_states"]} == {"v1", "v2", "v3"}

    swiss = attach_elim_states({"structure": "swiss", "rounds": [*rounds]})
    assert "gen_states" not in swiss  # KEY-ABSENT for non-elim (additive)
    assert swiss["rounds"][0]["round_index"] == 1  # untouched, original order
