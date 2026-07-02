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
import sqlite3
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from zicato.dashboard.server import create_app
from zicato.dashboard.state_reader import (
    WorkspacePaths,
    build_racing_field,
    build_round_timeline,
)

EPOCH = "2026-06-01_e0"


def _write_json(path: Path, obj: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj), encoding="utf-8")


def _base_workspace(tmp_path: Path) -> Path:
    ws = tmp_path / ".zicato"
    (ws / "runtime").mkdir(parents=True)
    (ws / "current_epoch").write_text(EPOCH, encoding="utf-8")
    edir = ws / "epochs" / EPOCH
    _write_json(edir / "config.json", {"contract_hash": "h", "closed": False})
    return ws


def _index_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE generations(epoch_id TEXT, generation_id TEXT,
            parent_generation_id TEXT, promoted INTEGER, created_at TEXT,
            PRIMARY KEY(epoch_id, generation_id));
        CREATE TABLE experiments(epoch_id TEXT, generation_id TEXT,
            hypothesis_core_idea TEXT, PRIMARY KEY(epoch_id, generation_id));
        CREATE TABLE tournaments(tournament_id TEXT PRIMARY KEY, epoch_id TEXT,
            parent_generation_id TEXT, child_generation_id TEXT, decision TEXT,
            parent_scalar REAL, child_scalar REAL, delta_scalar REAL,
            rejection_reason TEXT, ran_at TEXT,
            structure TEXT, structure_params_json TEXT, competitors_json TEXT,
            rounds_json TEXT, standings_json TEXT);
        """
    )


def _racing_workspace(tmp_path: Path) -> Path:
    """Per-challenger racing records mirroring the frontend's old join input.

    v1/v2 are cut at rung 0; v3/v4 reach rung 1; v3 reaches the
    ``racing-final`` gate and is promoted (lineage v0 -> v3).
    """
    ws = _base_workspace(tmp_path)
    _write_json(
        ws / "epochs" / EPOCH / "scoring.json",
        {"tournament": {"structure": "racing", "params": {"eta": 2, "board_fraction": 0.25}}},
    )
    conn = sqlite3.connect(ws / "index.db")
    _index_schema(conn)
    conn.executemany(
        "INSERT INTO generations VALUES(?,?,?,?,?)",
        [
            (EPOCH, "v0", None, 1, "2026-06-01T00:00:00Z"),
            (EPOCH, "v1", "v0", 0, "2026-06-01T00:10:00Z"),
            (EPOCH, "v2", "v0", 0, "2026-06-01T00:11:00Z"),
            (EPOCH, "v3", "v0", 1, "2026-06-01T00:12:00Z"),
            (EPOCH, "v4", "v0", 0, "2026-06-01T00:13:00Z"),
        ],
    )
    params = json.dumps({"eta": 2, "board_fraction": 0.25})
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
    for i, (chall, rounds) in enumerate(per_challenger):
        conn.execute(
            "INSERT INTO tournaments VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                f"{EPOCH}:v0->{chall}",
                EPOCH,
                "v0",
                chall,
                "promoted" if chall == "v3" else "rejected",
                0.5,
                0.4,
                -0.1,
                None,
                f"2026-06-01T00:3{i}:00Z",
                "racing",
                params,
                json.dumps(["v0", chall]),
                json.dumps(rounds),
                json.dumps([]),
            ),
        )
    conn.commit()
    conn.close()
    return ws


@pytest.fixture
def static_dir(tmp_path: Path) -> Path:
    d = tmp_path / "static"
    d.mkdir()
    (d / "index.html").write_text("<!doctype html><title>z</title>", encoding="utf-8")
    return d


# ---------------------------------------------------------------------------
# The racing-field join.
# ---------------------------------------------------------------------------


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


def _gauntlet_workspace(tmp_path: Path) -> Path:
    """Two settled gauntlet matchup rounds: v1 rejected, then v2 promoted."""
    ws = _base_workspace(tmp_path)
    gens_dir = ws / "epochs" / EPOCH / "generations"
    _write_json(gens_dir / "v0" / "experiment.json", {"parent_generation_id": None})
    _write_json(
        gens_dir / "v1" / "experiment.json",
        {"parent_generation_id": "v0", "outcome": {"tournament_decision": "rejected"}},
    )
    _write_json(
        gens_dir / "v2" / "experiment.json",
        {"parent_generation_id": "v0", "outcome": {"tournament_decision": "promoted"}},
    )
    conn = sqlite3.connect(ws / "index.db")
    _index_schema(conn)
    conn.executescript(
        """
        CREATE TABLE loss_profiles(run_id TEXT, epoch_id TEXT, generation_id TEXT,
            entry_id TEXT, drift_loss REAL, pass_fail INTEGER, loss_json TEXT);
        """
    )
    conn.executemany(
        "INSERT INTO generations VALUES(?,?,?,?,?)",
        [
            (EPOCH, "v0", None, 1, "2026-06-01T00:00:00Z"),
            (EPOCH, "v1", "v0", 0, "2026-06-01T00:10:00Z"),
            (EPOCH, "v2", "v0", 1, "2026-06-01T00:20:00Z"),
        ],
    )
    conn.executemany(
        "INSERT INTO loss_profiles VALUES(?,?,?,?,?,?,?)",
        [
            ("r0", EPOCH, "v0", "e1", 0.5, 1, None),
            ("r1", EPOCH, "v1", "e1", 0.9, 0, None),
            ("r2", EPOCH, "v2", "e1", 0.3, 1, None),
        ],
    )
    conn.executemany(
        "INSERT INTO tournaments(tournament_id, epoch_id, parent_generation_id, "
        "child_generation_id, decision, parent_scalar, child_scalar, delta_scalar, "
        "rejection_reason, ran_at, structure, structure_params_json, competitors_json, "
        "rounds_json, standings_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (
                f"{EPOCH}:v0->v1",
                EPOCH,
                "v0",
                "v1",
                "rejected",
                0.5,
                0.9,
                0.4,
                None,
                "2026-06-01T00:15:00Z",
                "gauntlet",
                None,
                None,
                None,
                None,
            ),
            (
                f"{EPOCH}:v0->v2",
                EPOCH,
                "v0",
                "v2",
                "promoted",
                0.5,
                0.3,
                -0.2,
                None,
                "2026-06-01T00:25:00Z",
                "gauntlet",
                None,
                None,
                None,
                None,
            ),
        ],
    )
    conn.commit()
    conn.close()
    return ws


def test_round_timeline_from_gauntlet_matchups(tmp_path: Path) -> None:
    ws = _gauntlet_workspace(tmp_path)
    tl = build_round_timeline(WorkspacePaths(ws), EPOCH)
    assert tl["epoch_id"] == EPOCH
    assert tl["source"] == "matchups"
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
    """A per-gen ``round_index`` stamp is the authoritative birth round."""
    ws = _gauntlet_workspace(tmp_path)
    gens_dir = ws / "epochs" / EPOCH / "generations"
    _write_json(
        gens_dir / "v1" / "experiment.json",
        {
            "parent_generation_id": "v0",
            "round_index": 0,
            "outcome": {"tournament_decision": "rejected"},
        },
    )
    _write_json(
        gens_dir / "v2" / "experiment.json",
        {
            "parent_generation_id": "v0",
            "round_index": 0,
            "outcome": {"tournament_decision": "promoted"},
        },
    )
    tl = build_round_timeline(WorkspacePaths(ws), EPOCH)
    assert tl["source"] == "round_index"
    rounds = tl["rounds"]
    # ONE round minting both challengers (they share round_index 0).
    assert len(rounds) == 1
    assert sorted(c["id"] for c in rounds[0]["challengers"]) == ["v1", "v2"]
    assert rounds[0]["gate"] == {"kind": "promoted", "gen": "v2"}


def test_round_timeline_endpoint_and_empty_degrade(tmp_path: Path, static_dir: Path) -> None:
    ws = _gauntlet_workspace(tmp_path)
    client = TestClient(create_app(ws, static_dir, read_only=True))
    payload = client.get(f"/api/epoch/{EPOCH}/round-timeline").json()
    assert payload["rounds"] and payload["waterfall"]
    # an unknown epoch degrades to the single-round shape over zero gens.
    empty = client.get("/api/epoch/never_ran/round-timeline").json()
    assert empty["rounds"][0]["challengers"] == [] if empty["rounds"] else True
