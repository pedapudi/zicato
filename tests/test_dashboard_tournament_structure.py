"""Dashboard read-path tests for configurable tournament STRUCTURES.

Covers the additive API surface specified in TOURNAMENT-DATA-MODEL.md §3:

* ``build_epoch_view`` — the new ``tournament: {structure, params}`` block,
  read from the epoch's frozen ``scoring.json`` (absent ⇒ omitted).
* ``build_bracket`` (``/api/tournaments``) — the new top-level
  ``structure`` / ``structure_params`` + the ``tournaments[]`` array, with
  the legacy ``matchups`` / ``champion_lineage`` preserved byte-identically.
* ``build_tournament_structure`` (``/api/tournament-structure/...``) — the
  index → active → loss-files fallback chain.
* ``_normalize_tournament_statuses`` — an OPAQUE (generation-id) ``side``
  passes through untouched.
* The endpoint route resolves + a malformed coordinate degrades to an
  empty gauntlet structure (HTTP 200).
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
    _normalize_tournament_statuses,
    build_bracket,
    build_epoch_view,
    build_tournament_structure,
)


def _write_json(path: Path, obj: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj), encoding="utf-8")


EPOCH = "2026-06-01_e0"
SWISS_TOURN = f"{EPOCH}:v0->v1"


def _build_index(path: Path, *, structure: str) -> None:
    """A v3 index with one structure-aware tournaments row."""
    conn = sqlite3.connect(path)
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
    conn.executemany(
        "INSERT INTO generations VALUES(?,?,?,?,?)",
        [
            (EPOCH, "v0", None, 1, "2026-06-01T00:00:00Z"),
            (EPOCH, "v1", "v0", 1, "2026-06-01T00:10:00Z"),
        ],
    )
    conn.execute(
        "INSERT INTO experiments VALUES(?,?,?)",
        (EPOCH, "v1", "Tighten the planner."),
    )
    rounds = [
        {
            "round_index": 0,
            "label": "Round 1",
            "matches": [
                {
                    "match_id": "r0m0",
                    "competitors": ["v0", "v1"],
                    "winner": "v1",
                    "decision": "promoted",
                    "delta_scalar": -0.1,
                    "bracket_slot": "",
                    "bye": False,
                }
            ],
        }
    ]
    standings = [
        {
            "generation_id": "v1",
            "rank": 1,
            "scalar": 0.4,
            "wins": 1,
            "losses": 0,
            "status": "champion",
        },
        {
            "generation_id": "v0",
            "rank": 2,
            "scalar": 0.5,
            "wins": 0,
            "losses": 1,
            "status": "eliminated",
        },
    ]
    conn.execute(
        "INSERT INTO tournaments VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            SWISS_TOURN,
            EPOCH,
            "v0",
            "v1",
            "promoted",
            0.5,
            0.4,
            -0.1,
            None,
            "2026-06-01T00:30:00Z",
            structure,
            json.dumps({"rounds": 4}),
            json.dumps(["v0", "v1"]),
            json.dumps(rounds),
            json.dumps(standings),
        ),
    )
    conn.commit()
    conn.close()


@pytest.fixture
def swiss_workspace(tmp_path: Path) -> Path:
    ws = tmp_path / ".zicato"
    (ws / "runtime").mkdir(parents=True)
    (ws / "current_epoch").write_text(EPOCH, encoding="utf-8")
    edir = ws / "epochs" / EPOCH
    _write_json(edir / "config.json", {"contract_hash": "h", "closed": False, "goal": "g"})
    # the frozen scoring.json carries the tournament block (§1.2)
    _write_json(
        edir / "scoring.json",
        {
            "weights": {"drift_loss": 1.0},
            "tournament": {"structure": "swiss", "params": {"rounds": 4}},
        },
    )
    _build_index(ws / "index.db", structure="swiss")
    return ws


@pytest.fixture
def gauntlet_workspace(tmp_path: Path) -> Path:
    """A back-compat gauntlet workspace: scoring.json with NO tournament key."""
    ws = tmp_path / ".zicato"
    (ws / "runtime").mkdir(parents=True)
    (ws / "current_epoch").write_text(EPOCH, encoding="utf-8")
    edir = ws / "epochs" / EPOCH
    _write_json(edir / "config.json", {"contract_hash": "h", "closed": False, "goal": "g"})
    _write_json(edir / "scoring.json", {"weights": {"drift_loss": 1.0}})
    _build_index(ws / "index.db", structure="gauntlet")
    # blank the structure-json so the row is a degenerate gauntlet row
    conn = sqlite3.connect(ws / "index.db")
    conn.execute(
        "UPDATE tournaments SET competitors_json=NULL, rounds_json=NULL, standings_json=NULL"
    )
    conn.commit()
    conn.close()
    return ws


# ---------------------------------------------------------------------------
# build_epoch_view — the tournament block
# ---------------------------------------------------------------------------


def test_epoch_view_carries_tournament_block(swiss_workspace: Path) -> None:
    view = build_epoch_view(WorkspacePaths(swiss_workspace))
    assert view["tournament"] == {"structure": "swiss", "params": {"rounds": 4}}


def test_epoch_view_omits_block_for_gauntlet_scoring(gauntlet_workspace: Path) -> None:
    # scoring.json with no tournament key ⇒ block omitted (frontend defaults
    # to gauntlet); the rest of the view is byte-identical to before.
    view = build_epoch_view(WorkspacePaths(gauntlet_workspace))
    assert "tournament" not in view


def test_epoch_view_unknown_structure_degrades_to_gauntlet(tmp_path: Path) -> None:
    ws = tmp_path / ".zicato"
    (ws / "runtime").mkdir(parents=True)
    (ws / "current_epoch").write_text(EPOCH, encoding="utf-8")
    edir = ws / "epochs" / EPOCH
    _write_json(edir / "config.json", {"closed": False})
    _write_json(edir / "scoring.json", {"tournament": {"structure": "bogus", "params": 5}})
    view = build_epoch_view(WorkspacePaths(ws))
    # unknown token ⇒ gauntlet; non-object params ⇒ {}
    assert view["tournament"] == {"structure": "gauntlet", "params": {}}


# ---------------------------------------------------------------------------
# build_bracket — structure + tournaments[] additive, legacy preserved
# ---------------------------------------------------------------------------


def test_bracket_adds_structure_and_tournaments(swiss_workspace: Path) -> None:
    b = build_bracket(WorkspacePaths(swiss_workspace))
    assert b["structure"] == "swiss"
    assert b["structure_params"] == {"rounds": 4}
    # the legacy shape is preserved
    assert b["champion_lineage"] == ["v0", "v1"]
    assert len(b["matchups"]) == 1
    assert b["matchups"][0]["champion"] == "v0"
    assert b["matchups"][0]["challenger"] == "v1"
    # the new per-tournament array carries the structure internals
    assert len(b["tournaments"]) == 1
    t = b["tournaments"][0]
    assert t["tournament_id"] == SWISS_TOURN
    assert t["structure"] == "swiss"
    assert t["competitors"] == ["v0", "v1"]
    assert t["rounds"][0]["matches"][0]["winner"] == "v1"
    assert t["standings"][0]["status"] == "champion"


def test_bracket_gauntlet_legacy_byte_identical(gauntlet_workspace: Path) -> None:
    b = build_bracket(WorkspacePaths(gauntlet_workspace))
    # the gauntlet rows carry no structure internals ⇒ structure stays gauntlet
    assert b["structure"] == "gauntlet"
    assert b["structure_params"] == {}
    # legacy fields intact
    assert b["champion_lineage"] == ["v0", "v1"]
    assert len(b["matchups"]) == 1
    # the tournaments[] array is present-but-degenerate (one gauntlet row)
    assert len(b["tournaments"]) == 1
    assert b["tournaments"][0]["structure"] == "gauntlet"
    assert b["tournaments"][0]["rounds"] == []
    assert b["tournaments"][0]["standings"] == []


# ---------------------------------------------------------------------------
# build_tournament_structure — the fallback chain
# ---------------------------------------------------------------------------


def test_structure_reader_from_index(swiss_workspace: Path) -> None:
    st = build_tournament_structure(WorkspacePaths(swiss_workspace), EPOCH, SWISS_TOURN)
    assert st["source"] == "index"
    assert st["structure"] == "swiss"
    assert st["structure_params"] == {"rounds": 4}
    assert st["competitors"] == ["v0", "v1"]
    assert st["rounds"][0]["matches"][0]["decision"] == "promoted"
    assert st["standings"][0]["generation_id"] == "v1"


def test_structure_reader_loss_file_fallback(tmp_path: Path) -> None:
    # No index → reconstruct a degenerate single match from gen_score.json.
    ws = tmp_path / ".zicato"
    (ws / "runtime").mkdir(parents=True)
    (ws / "current_epoch").write_text(EPOCH, encoding="utf-8")
    gens = ws / "epochs" / EPOCH / "generations"
    _write_json(gens / "v0" / "gen_score.json", {"scalar": 0.5})
    _write_json(gens / "v1" / "gen_score.json", {"scalar": 0.4})
    st = build_tournament_structure(WorkspacePaths(ws), EPOCH, SWISS_TOURN)
    assert st["source"] == "loss_files"
    # the crowning pair was decoded from the tournament id convention
    ids = [c["generation_id"] for c in st["competitors"]]
    assert ids == ["v0", "v1"]
    match = st["rounds"][0]["matches"][0]
    assert match["competitors"] == ["v0", "v1"]
    # delta = challenger - champion = 0.4 - 0.5 = -0.1
    assert match["delta_scalar"] == pytest.approx(-0.1)


def test_structure_reader_unknown_tournament_empty(swiss_workspace: Path) -> None:
    st = build_tournament_structure(WorkspacePaths(swiss_workspace), EPOCH, "no_such_tourn")
    # not in index, not the active record, id does not decode ⇒ empty gauntlet
    assert st["structure"] == "gauntlet"
    assert st["rounds"] == []
    assert st["competitors"] == []
    # field_status is always present (additive) and empty here.
    assert st["field_status"] == []


def test_structure_reader_pre_v5_index_field_status_empty(swiss_workspace: Path) -> None:
    """The hand-built swiss index has no ``field_status_json`` column (a
    pre-v5 shape); the reader degrades it to an empty list rather than
    failing the resolution."""
    st = build_tournament_structure(WorkspacePaths(swiss_workspace), EPOCH, SWISS_TOURN)
    assert st["source"] == "index"
    assert st["field_status"] == []


def test_structure_reader_enriches_field_status_from_active(swiss_workspace: Path) -> None:
    """When the index row carries the settled bracket but no proposing
    outcomes, a matching live ``active_tournament.json`` (retained with
    phase=completed) lifts its ``field_status`` onto the resolved
    structure — so a just-completed epoch's proposing step survives."""
    from zicato.runtime.state import ActiveTournament, write_active_tournament

    write_active_tournament(
        swiss_workspace,
        ActiveTournament(
            tournament_id=SWISS_TOURN,
            parent_generation_id="",
            child_generation_id="v1",
            epoch_id=EPOCH,
            started_at="2026-06-01T00:30:00Z",
            phase="completed",
            structure="swiss",
            competitors=[
                {"generation_id": "v0", "seed": 1, "role": "champion"},
                {"generation_id": "v1", "seed": 2, "role": "challenger"},
            ],
            field_status=[
                {"generation_id": "v1", "status": "applied", "reason": "", "seed": 2},
            ],
        ),
    )
    st = build_tournament_structure(WorkspacePaths(swiss_workspace), EPOCH, SWISS_TOURN)
    # The settled bracket still comes from the index…
    assert st["source"] == "index"
    assert st["standings"][0]["generation_id"] == "v1"
    # …but field_status is lifted from the live envelope.
    assert [f["generation_id"] for f in st["field_status"]] == ["v1"]
    assert st["field_status"][0]["status"] == "applied"


def test_active_tournament_route_surfaces_field_status(
    swiss_workspace: Path, static_dir: Path
) -> None:
    """``/api/active-tournament`` carries the per-challenger field_status
    so the live hero can render the proposing-step tracker."""
    from zicato.runtime.state import ActiveTournament, write_active_tournament

    write_active_tournament(
        swiss_workspace,
        ActiveTournament(
            tournament_id=SWISS_TOURN,
            parent_generation_id="",
            child_generation_id="",
            epoch_id=EPOCH,
            started_at="2026-06-01T00:30:00Z",
            phase="proposing",
            structure="swiss",
            field_status=[
                {"generation_id": "v1", "status": "rejected", "reason": "invalid JSON", "seed": 2},
                {
                    "generation_id": "v2",
                    "status": "rejected",
                    "reason": "empty response",
                    "seed": 3,
                },
            ],
        ),
    )
    app = create_app(swiss_workspace, static_dir, read_only=True)
    with TestClient(app) as c:
        r = c.get("/api/active-tournament")
        assert r.status_code == 200
        fs = r.json()["field_status"]
        assert [f["generation_id"] for f in fs] == ["v1", "v2"]
        assert all(f["status"] == "rejected" for f in fs)
        assert fs[0]["reason"] == "invalid JSON"


# ---------------------------------------------------------------------------
# _normalize_tournament_statuses — opaque side passthrough
# ---------------------------------------------------------------------------


def test_normalize_opaque_side_passthrough() -> None:
    tourn = {
        "structure": "swiss",
        "parent_generation_id": "",
        "child_generation_id": "",
        "entries": [
            {"entry_id": "e1", "side": "v5", "status": "completed"},
            {"entry_id": "e1", "side": "v4", "status": "running"},
        ],
    }
    out = _normalize_tournament_statuses(tourn)
    by_side = {e["side"]: e for e in out["entries"]}
    # the opaque competitor side becomes the per-entry generation_id
    assert by_side["v5"]["generation_id"] == "v5"
    assert by_side["v4"]["generation_id"] == "v4"
    # statuses still normalize
    assert by_side["v5"]["status"] == "done"
    assert by_side["v4"]["status"] == "running"


def test_normalize_gauntlet_side_unchanged() -> None:
    tourn = {
        "parent_generation_id": "v0",
        "child_generation_id": "v1",
        "entries": [
            {"entry_id": "e1", "side": "parent", "status": "completed"},
            {"entry_id": "e1", "side": "child", "status": "queued"},
        ],
    }
    out = _normalize_tournament_statuses(tourn)
    by_side = {e["side"]: e for e in out["entries"]}
    assert by_side["parent"]["generation_id"] == "v0"
    assert by_side["child"]["generation_id"] == "v1"


# ---------------------------------------------------------------------------
# endpoint route
# ---------------------------------------------------------------------------


@pytest.fixture
def static_dir(tmp_path: Path) -> Path:
    d = tmp_path / "static"
    d.mkdir()
    (d / "index.html").write_text("<!doctype html><title>z</title>", encoding="utf-8")
    return d


def test_tournament_structure_route(swiss_workspace: Path, static_dir: Path) -> None:
    app = create_app(swiss_workspace, static_dir, read_only=True)
    with TestClient(app) as c:
        r = c.get(f"/api/tournament-structure/{EPOCH}/{SWISS_TOURN}")
        assert r.status_code == 200
        body = r.json()
        assert body["structure"] == "swiss"
        assert body["source"] == "index"
        assert body["standings"][0]["generation_id"] == "v1"


def test_tournament_structure_route_malformed_id(swiss_workspace: Path, static_dir: Path) -> None:
    app = create_app(swiss_workspace, static_dir, read_only=True)
    with TestClient(app) as c:
        # a dotdot-bearing id (single path segment) reaches the handler and is
        # rejected by _is_safe_tournament_id → empty gauntlet at HTTP 200.
        r = c.get(f"/api/tournament-structure/{EPOCH}/a..b")
        assert r.status_code == 200
        assert r.json()["structure"] == "gauntlet"
        assert r.json()["rounds"] == []


def test_tournaments_route_carries_structure(swiss_workspace: Path, static_dir: Path) -> None:
    app = create_app(swiss_workspace, static_dir, read_only=True)
    with TestClient(app) as c:
        body = c.get("/api/tournaments").json()
        assert body["structure"] == "swiss"
        assert body["tournaments"][0]["tournament_id"] == SWISS_TOURN


def test_epoch_route_carries_tournament_block(swiss_workspace: Path, static_dir: Path) -> None:
    app = create_app(swiss_workspace, static_dir, read_only=True)
    with TestClient(app) as c:
        body = c.get("/api/epoch").json()
        assert body["tournament"] == {"structure": "swiss", "params": {"rounds": 4}}
