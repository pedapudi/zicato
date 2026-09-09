"""Server-side racing-field join + round-timeline reader (WS4 Track B).

The frontend used to fabricate these shapes client-side
(``reconstructRacing`` + the ``rounds.js`` four-endpoint join). These tests
pin the server payloads that replaced them:

* ``build_racing_field`` / ``GET /api/epoch/{id}/racing-field`` — the
  per-challenger racing records joined into ONE rung/gate ladder.
* ``build_round_timeline`` / ``GET /api/epoch/{id}/round-timeline`` — the
  settled rounds along the champion spine + the loss-floor waterfall.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from tests._console_scenarios import (
    EpochSpec,
    FieldRecord,
    Gen,
    build_scenario,
    record_gate_comparisons,
)
from tests._workspace_support import write_generation, write_lineage, write_tournament
from zicato.dashboard.server import create_app
from zicato.index.ingest import rebuild_index
from zicato.query import (
    WorkspacePaths,
    build_racing_field,
    build_round_timeline,
)
from zicato.workspace import WorkspaceLayout

EPOCH = "2026-06-01_e0"


def _write_json(path: Path, obj: object) -> None:
    if path.name == "lineage.json":
        assert isinstance(obj, dict)
        write_lineage(WorkspaceLayout.from_root(path.parent), obj)
    elif path.name == "experiment.json":
        assert isinstance(obj, dict)
        write_generation(
            WorkspaceLayout.from_root(path.parents[4]),
            path.parents[2].name,
            path.parent.name,
            experiment=obj,
        )
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(obj), encoding="utf-8")


def _base_workspace(tmp_path: Path) -> Path:
    ws = tmp_path / ".zicato"
    (ws / "runtime").mkdir(parents=True)
    (ws / "current_epoch").write_text(EPOCH, encoding="utf-8")
    edir = ws / "epochs" / EPOCH
    _write_json(edir / "config.json", {"contract_hash": "h", "closed": False})
    return ws


def _field(champion, challengers, *, primary, rounds=(), state="settled", structure="racing"):
    return FieldRecord(
        challengers[0],
        structure,
        competitors=tuple(
            [{"generation_id": champion, "role": "champion"}]
            + [{"generation_id": gid, "role": "challenger"} for gid in challengers]
        ),
        rounds=rounds,
        champion=champion,
        promoted=primary or "",
        decision="promoted" if primary else "rejected",
        state=state,
    )


def _scenario(tmp_path, generations, fields=()):
    ws = build_scenario(
        tmp_path, EpochSpec(EPOCH, tuple(generations), "2026-06-01T00:00:00Z", fields=tuple(fields))
    )
    record_gate_comparisons(ws)
    rebuild_index(ws)
    return ws


def _racing_workspace(tmp_path: Path) -> Path:
    """A completed racing field with two rungs and its final comparison."""
    per_challenger = [
        ("v1", [{"match_id": "rung0_m0", "opponent": "v0", "won": False, "delta_scalar": 25.0}]),
        ("v2", [{"match_id": "rung0_m1", "opponent": "v0", "won": False, "delta_scalar": 3.3}]),
        (
            "v3",
            [
                {"match_id": "rung0_m2", "opponent": "v0", "won": True, "delta_scalar": -0.16},
                {"match_id": "rung1_m0", "opponent": "v0", "won": False, "delta_scalar": 1.0},
                {"match_id": "racing-final", "opponent": "v0", "won": True, "delta_scalar": -32.19},
            ],
        ),
        (
            "v4",
            [
                {"match_id": "rung0_m3", "opponent": "v0", "won": False, "delta_scalar": 0.002},
                {"match_id": "rung1_m1", "opponent": "v0", "won": False, "delta_scalar": 1.25},
            ],
        ),
    ]
    gens = [Gen("v0", scalar=0.5)]
    for gid, matches in per_challenger:
        gens.append(
            Gen(
                gid,
                "v0",
                "promoted" if gid == "v3" else "rejected",
                scalar=0.4,
                structure="racing",
                matches=tuple(matches),
            )
        )
    rounds = (
        {
            "round_index": 0,
            "matches": [
                {
                    "match_id": "rung0",
                    "competitors": ["v1", "v2", "v3", "v4"],
                    "cut": ["v1", "v2"],
                    "survivors": ["v3", "v4"],
                    "board_fraction": 0.25,
                    "deltas": {"v1": 25.0, "v2": 3.3, "v3": -0.16, "v4": 0.002},
                }
            ],
        },
        {
            "round_index": 1,
            "matches": [
                {
                    "match_id": "rung1",
                    "competitors": ["v3", "v4"],
                    "cut": ["v4"],
                    "survivors": ["v3"],
                    "board_fraction": 0.5,
                }
            ],
        },
        {
            "round_index": 2,
            "matches": [
                {
                    "match_id": "racing-final",
                    "competitors": ["v0", "v3"],
                    "winner": "v3",
                    "decision": "promoted",
                    "delta_scalar": -32.19,
                    "board_fraction": 1.0,
                }
            ],
        },
    )
    field = _field("v0", ["v1", "v2", "v3", "v4"], primary="v3", rounds=rounds)
    return _scenario(tmp_path, gens, [field])


def test_racing_field_joins_rungs_and_gate(tmp_path: Path) -> None:
    ws = _racing_workspace(tmp_path)
    field = build_racing_field(WorkspacePaths(ws), EPOCH)
    assert field["present"] is True
    assert field["structure"] == "racing"
    rounds = field["rounds"]
    rung_rounds = [r for r in rounds if r["matches"][0]["match_id"] != "racing-final"]
    assert len(rung_rounds) == 2

    r0 = rung_rounds[0]["matches"][0]
    assert sorted(r0["competitors"]) == ["v1", "v2", "v3", "v4"]
    assert sorted(r0["cut"]) == ["v1", "v2"]
    assert sorted(r0["survivors"]) == ["v3", "v4"]
    assert r0["board_fraction"] == pytest.approx(0.25)
    assert r0["deltas"]["v1"] == pytest.approx(25.0)
    assert r0["deltas"]["v3"] == pytest.approx(-0.16)

    r1 = rung_rounds[1]["matches"][0]
    assert sorted(r1["competitors"]) == ["v3", "v4"]
    assert r1["cut"] == ["v4"]
    assert r1["survivors"] == ["v3"]
    assert r1["board_fraction"] == pytest.approx(0.5)

    gate = next(r for r in rounds if r["matches"][0]["match_id"] == "racing-final")
    gm = gate["matches"][0]
    assert gm["winner"] == "v3"
    assert gm["decision"] == "promoted"
    assert sorted(gm["competitors"]) == ["v0", "v3"]
    assert gm["delta_scalar"] == pytest.approx(-32.19)
    assert gm["board_fraction"] == pytest.approx(1.0)


def test_racing_field_absent_without_records(tmp_path: Path) -> None:
    ws = _base_workspace(tmp_path)
    field = build_racing_field(WorkspacePaths(ws), EPOCH)
    assert field == {"epoch_id": EPOCH, "present": False}


def test_racing_field_endpoint(tmp_path: Path, static_dir: Path) -> None:
    ws = _racing_workspace(tmp_path)
    client = TestClient(create_app(ws, static_dir, read_only=True))
    payload = client.get(f"/api/epoch/{EPOCH}/racing-field").json()
    assert payload["present"] is True
    assert payload["champion_lineage"]  # lineage rides along for the gate read
    bad = client.get("/api/epoch/..%2F..%2Fetc/racing-field")
    assert bad.status_code in (200, 404)  # malformed → degraded, never 500


# ---------------------------------------------------------------------------
# The round timeline.
# ---------------------------------------------------------------------------


def _gauntlet_workspace(tmp_path: Path, *, round_indices=(0, 1)) -> Path:
    """Two declared gauntlet outcomes with explicit birth rounds."""
    return _scenario(
        tmp_path,
        [
            Gen("v0", scalar=0.5, entries=(("e1", 0.5, True),)),
            Gen(
                "v1",
                "v0",
                "rejected",
                scalar=0.9,
                entries=(("e1", 0.9, False),),
                round_index=round_indices[0],
            ),
            Gen(
                "v2",
                "v0",
                "promoted",
                scalar=0.3,
                entries=(("e1", 0.3, True),),
                round_index=round_indices[1],
            ),
        ],
    )


def test_round_timeline_from_gauntlet_matchups(tmp_path: Path) -> None:
    ws = _gauntlet_workspace(tmp_path)
    tl = build_round_timeline(WorkspacePaths(ws), EPOCH)
    assert tl["epoch_id"] == EPOCH
    assert tl["source"] == "round_index"
    rounds = tl["rounds"]
    assert [r["round_index"] for r in rounds] == [0, 1]
    # round 0: v0 defends against v1 (held); round 1: v2 promoted.
    assert rounds[0]["champion"]["id"] == "v0"
    assert [c["id"] for c in rounds[0]["challengers"]] == ["v1"]
    assert rounds[0]["gate"] == {"kind": "held", "gen": None}
    assert rounds[1]["champion"]["id"] == "v0"
    assert [c["id"] for c in rounds[1]["challengers"]] == ["v2"]
    assert rounds[1]["gate"] == {"kind": "promoted", "gen": "v2"}
    assert rounds[1]["challengers"][0]["promoted"] is True
    # the waterfall: round 0 holds the floor, round 1 drops it 0.5 -> 0.3.
    wf = tl["waterfall"]
    assert wf[0]["promoted"] is False
    assert wf[0]["from"] == wf[0]["to"]
    assert wf[1]["promoted"] is True
    assert wf[1]["gen"] == "v2"
    assert wf[1]["from"] == pytest.approx(0.5)
    assert wf[1]["to"] == pytest.approx(0.3)
    assert wf[1]["delta"] == pytest.approx(-0.2)


def test_round_timeline_prefers_round_index_stamp(tmp_path: Path) -> None:
    """A shared birth round groups both challengers under its recorded promotion."""
    ws = _gauntlet_workspace(tmp_path, round_indices=(0, 0))
    tl = build_round_timeline(WorkspacePaths(ws), EPOCH)
    assert tl["source"] == "round_index"
    rounds = tl["rounds"]
    # ONE round minting both challengers (they share round_index 0).
    assert len(rounds) == 1
    assert sorted(c["id"] for c in rounds[0]["challengers"]) == ["v1", "v2"]
    assert rounds[0]["gate"] == {"kind": "promoted", "gen": "v2"}


def _field_round_workspace(tmp_path: Path, *, third_round: str | None = None) -> Path:
    """One promotion followed by a held champion, with optional further evaluation."""
    values = [
        ("v0", 0.5),
        ("v1", 0.9),
        ("v2", 0.88),
        ("v3", 0.86),
        ("v4", 0.84),
        ("v5", 0.3),
        ("v6", 0.6),
        ("v7", 0.61),
    ]
    if third_round is not None:
        values.extend([("v8", 0.7), ("v9", 0.8)])
    gens = []
    for gid, scalar in values:
        index = int(gid[1:])
        parent = None if index == 0 else "v0" if index <= 5 else "v5"
        decision = (
            None
            if index == 0 or (index >= 8 and third_round == "running")
            else "promoted"
            if gid == "v5"
            else "rejected"
        )
        gens.append(
            Gen(
                gid,
                parent,
                decision,
                scalar=scalar,
                entries=(("e1", scalar, True),),
                round_index=0 if index <= 5 else 1 if index <= 7 else 2,
                structure="racing",
                champion_eval_mode="fast-degraded" if index >= 8 else "full",
            )
        )
    fields = [
        _field("v0", ["v1", "v2", "v3", "v4", "v5"], primary="v5"),
        _field("v5", ["v6", "v7"], primary=None),
    ]
    if third_round is not None:
        fields.append(
            _field(
                "v5",
                ["v8", "v9"],
                primary=None,
                state="in_progress" if third_round == "running" else "settled",
            )
        )
    return _scenario(tmp_path, gens, fields)


def test_field_round_names_the_new_champion_after_a_promotion(tmp_path: Path) -> None:
    """A round AFTER a promotion names the WINNER, never the champion it beat.

    A field row's parent column is empty by design, so the champion comes from
    the competitor list. Borrowing "the first competitor with a crowning row"
    reads the champion's OWN duel, whose parent is the champion it BEAT — which
    left the beaten champion defending every later round. The role tag on the
    competitor is the answer.
    """
    ws = _field_round_workspace(tmp_path)
    tl = build_round_timeline(WorkspacePaths(ws), EPOCH)
    rounds = tl["rounds"]
    assert [r["round_index"] for r in rounds] == [0, 1]
    # round 0: the carried champion v0 defends, and v5 takes the title.
    assert rounds[0]["champion"]["id"] == "v0"
    assert rounds[0]["gate"] == {"kind": "promoted", "gen": "v5"}
    # round 1: v5 DEFENDS. This is the regression — it read "v0" before.
    assert rounds[1]["champion"]["id"] == "v5", "the promoted challenger defends the next round"
    assert sorted(c["id"] for c in rounds[1]["challengers"]) == ["v6", "v7"]
    assert rounds[1]["gate"] == {"kind": "held", "gen": None}


def test_field_round_champion_metadata_comes_from_that_round(tmp_path: Path) -> None:
    """A held champion's scalar and evaluation mode come from the recorded round."""
    from dataclasses import replace

    from zicato.epoch.settlement_receipt import read_settlement_receipt, write_settlement_receipt

    ws = _field_round_workspace(tmp_path, third_round="settled")
    for round_index, scalar in [(1, 0.31), (2, 0.42)]:
        receipt = read_settlement_receipt(ws, EPOCH, round_index)
        write_settlement_receipt(
            ws,
            replace(
                receipt,
                candidates=tuple(
                    replace(candidate, parent_scalar=scalar) for candidate in receipt.candidates
                ),
            ),
        )
    champion = build_round_timeline(WorkspacePaths(ws), EPOCH)["rounds"][2]["champion"]
    assert champion == {
        "id": "v5",
        "scalar": pytest.approx(0.42),
        "eval_mode": "fast-degraded",
        "run_ref": f"epochs/{EPOCH}/generations/v5",
        "from_record": True,
    }


def test_field_round_with_no_crowning_row_reports_an_unknown_eval_mode(tmp_path: Path) -> None:
    """An opened tournament names its defender without inventing an evaluation mode."""
    ws = _field_round_workspace(tmp_path, third_round="running")
    champion = build_round_timeline(WorkspacePaths(ws), EPOCH)["rounds"][2]["champion"]
    assert champion["id"] == "v5"
    assert champion["eval_mode"] is None
    assert champion["run_ref"] is None


def test_round_timeline_drops_a_numerically_stamped_seed(tmp_path: Path) -> None:
    """A baseline's zero stamp does not create an empty tournament round."""
    ws = _gauntlet_workspace(tmp_path, round_indices=(1, 1))
    tl = build_round_timeline(WorkspacePaths(ws), EPOCH)
    assert tl["source"] == "round_index"
    rounds = tl["rounds"]
    # ONE round — the real one. The seed contributes no round of its own.
    assert [r["round_index"] for r in rounds] == [1]
    assert sorted(c["id"] for c in rounds[0]["challengers"]) == ["v1", "v2"]
    assert rounds[0]["champion"]["id"] == "v0", "the carried seed still DEFENDS its round"
    assert rounds[0]["gate"] == {"kind": "promoted", "gen": "v2"}
    # and no round anywhere is an empty field.
    assert all(r["challengers"] for r in rounds), "no round with an empty field"


def test_round_timeline_owns_live_field_overlay(tmp_path: Path) -> None:
    ws = _gauntlet_workspace(tmp_path)
    write_tournament(
        ws,
        {
            "epoch_id": EPOCH,
            "round_index": 2,
            "phase": "proposing:round_2:v3",
            "field_status": [
                {"generation_id": "v3", "status": "applied"},
                {"generation_id": "v4", "status": "proposing"},
            ],
            "projected": {"v3": {"scalar": 0.25, "boards_done": 2, "boards_total": 4}},
        },
    )

    live = build_round_timeline(WorkspacePaths(ws), EPOCH)["rounds"][-1]
    assert live["inflight"] is True
    assert live["gate"] == {"kind": "pending", "gen": None}
    assert [c["status"] for c in live["challengers"]] == ["applied", "proposing"]
    assert live["challengers"][0]["boards_done"] == 2


def test_round_timeline_endpoint_and_empty_degrade(tmp_path: Path, static_dir: Path) -> None:
    ws = _gauntlet_workspace(tmp_path)
    client = TestClient(create_app(ws, static_dir, read_only=True))
    payload = client.get(f"/api/epoch/{EPOCH}/round-timeline").json()
    assert payload["rounds"] and payload["waterfall"]
    # an unknown epoch degrades to the single-round shape over zero gens.
    empty = client.get("/api/epoch/never_ran/round-timeline").json()
    assert empty["rounds"][0]["challengers"] == [] if empty["rounds"] else True


# ---------------------------------------------------------------------------
# The served ELIM MODEL (U3) — the third served join the node mock mirrors.
# ---------------------------------------------------------------------------
#
# The client's elimFlow/elimRadial used to derive the whole elim model per
# render; ``derive_elim_states`` is that fold moved server-side, attached to
# every payload the figures read: the /api/tournament-structure record, the
# /api/tournaments entries (the per-round minis' tournamentRef), and the live
# /api/active-tournament envelope. test/mock_server.mjs mirrors the fold
# (``deriveElimStates``) exactly as it mirrors the racing-field/round-timeline
# joins; the shared fixture tests/data/elim_states_fixture.json pins the
# Python + Rust + mock folds byte-for-byte.


def _elim_workspace(tmp_path: Path) -> Path:
    """An elimination record with its final listed before the opening matches."""
    rounds = [
        # the final FIRST — the server must serve it sorted (WB rounds first).
        {
            "round_index": 1,
            "label": "Final",
            "matches": [
                {
                    "match_id": "F",
                    "bracket_slot": "F",
                    "competitors": ["v1", "v3"],
                    "winner": "v1",
                    "decision": "promoted",
                }
            ],
        },
        {
            "round_index": 0,
            "label": "Round 1",
            "matches": [
                {
                    "match_id": "WB-R0-0",
                    "bracket_slot": "WB-R0-0",
                    "competitors": ["v1", "v2"],
                    "winner": "v1",
                }
            ],
        },
    ]
    field = _field(
        "v0", ["v1", "v2", "v3"], primary="v1", rounds=tuple(rounds), structure="single_elim"
    )
    return _scenario(
        tmp_path,
        [
            Gen("v0", scalar=1.0),
            *[
                Gen(
                    gid,
                    "v0",
                    "promoted" if gid == "v1" else "rejected",
                    scalar=0.5,
                    structure="single_elim",
                )
                for gid in ["v1", "v2", "v3"]
            ],
        ],
        [field],
    )


def test_tournament_structure_serves_the_elim_model(tmp_path: Path, static_dir: Path) -> None:
    """/api/tournament-structure carries sorted rounds + gen_states (DQ1)."""
    ws = _elim_workspace(tmp_path)
    client = TestClient(create_app(ws, static_dir, read_only=True))
    payload = client.get(f"/api/tournament-structure/{EPOCH}/{EPOCH}:field:v1").json()
    assert payload["structure"] == "single_elim"
    # PRE-SORTED: the mis-ordered record serves Round 1 before the Final.
    assert [r["round_index"] for r in payload["rounds"]] == [0, 1]
    assert [r["bracket_side"] for r in payload["rounds"]] == ["WB", "WB"]
    # the served per-match loser + the top-level gen_states fold.
    assert payload["rounds"][0]["matches"][0]["loser"] == "v2"
    states = {g["generation_id"]: g for g in payload["gen_states"]}
    assert states["v1"]["advanced_rounds"] == [0, 1]
    assert states["v2"]["eliminated_at_round"] == 0
    assert states["v3"]["eliminated_at_round"] == 1


def test_bracket_tournaments_carry_the_elim_model(tmp_path: Path, static_dir: Path) -> None:
    """/api/tournaments entries (the tournamentRef source) carry gen_states."""
    ws = _elim_workspace(tmp_path)
    client = TestClient(create_app(ws, static_dir, read_only=True))
    payload = client.get(f"/api/tournaments?epoch={EPOCH}").json()
    (record,) = payload["tournaments"]
    assert [r["round_index"] for r in record["rounds"]] == [0, 1]
    assert {g["generation_id"] for g in record["gen_states"]} == {"v1", "v2", "v3"}


def test_active_tournament_serves_the_elim_model(tmp_path: Path, static_dir: Path) -> None:
    """The LIVE path: /api/active-tournament carries the same fold.

    The Rust supervisor applies the identical enrichment
    (crates/supervisor/src/elim_states.rs) so the two dashboards agree.
    """
    ws = _base_workspace(tmp_path)
    write_tournament(
        ws,
        {
            "structure": "single_elim",
            "phase": "running",
            "rounds": [
                {
                    "round_index": 0,
                    "label": "Final",
                    "matches": [
                        {
                            "match_id": "F",
                            "bracket_slot": "F",
                            "competitors": ["v0", "v1"],
                            "winner": None,
                            "pending": True,
                        }
                    ],
                }
            ],
            "entries": [],
        },
    )
    client = TestClient(create_app(ws, static_dir, read_only=True))
    payload = client.get("/api/active-tournament").json()
    assert payload["rounds"][0]["bracket_side"] == "WB"
    assert payload["rounds"][0]["matches"][0]["loser"] is None
    states = {g["generation_id"]: g for g in payload["gen_states"]}
    assert states["v0"]["eliminated_at_round"] is None  # pending final: nobody out
    assert states["v1"]["played_rounds"] == [0]
