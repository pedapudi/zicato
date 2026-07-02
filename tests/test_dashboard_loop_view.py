"""Tests for the loop-communication readers + endpoints.

``zicato.dashboard.readers.loop_view`` wraps the tournament-detail
queries (:func:`optimization_trajectory` / :func:`tournament_cost`) with
the dashboard's best-effort degrade discipline AND the uncertainty-honest
verdict: a "plateaued" flag whose trailing-window movement sits below the
epoch's measured A/A ``noise_floor`` must read ``no_signal``, never a
confident "plateaued". These tests seed the analytical index directly
(the ``test_tournament_detail`` fixture pattern) and exercise the readers
plus the ``/api/epoch/{id}/trajectory`` + ``/api/epoch/{id}/cost``
endpoints end-to-end.
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
    build_optimization_trajectory,
    build_tournament_cost,
)

EPOCH = "2026-06_e0"


# ---------------------------------------------------------------------------
# Fixture workspace + seeded index
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE generations (
    epoch_id TEXT, generation_id TEXT, parent_generation_id TEXT, promoted INTEGER
);
CREATE TABLE experiments (
    epoch_id TEXT, generation_id TEXT,
    hypothesis_core_idea TEXT, hypothesis_why TEXT, hypothesis_json TEXT,
    tournament_decision TEXT, rejection_reason TEXT,
    scalar_score_delta REAL, drift_loss_delta REAL, pass_rate_delta REAL,
    outcome_json TEXT
);
CREATE TABLE patches (
    patch_id TEXT, epoch_id TEXT, generation_id TEXT,
    mutation_id TEXT, op TEXT, rationale TEXT
);
CREATE TABLE runs (
    run_id TEXT, epoch_id TEXT, generation_id TEXT, entry_id TEXT,
    runtime_ms INTEGER, aborted INTEGER
);
CREATE TABLE loss_profiles (
    run_id TEXT, epoch_id TEXT, generation_id TEXT, entry_id TEXT,
    drift_loss REAL, pass_fail INTEGER, loss_json TEXT
);
CREATE TABLE metric_counts (
    run_id TEXT, namespace TEXT, name TEXT, severity TEXT, count REAL
);
CREATE TABLE tournaments (
    tournament_id TEXT, epoch_id TEXT,
    parent_generation_id TEXT, child_generation_id TEXT,
    decision TEXT, parent_scalar REAL, child_scalar REAL, delta_scalar REAL,
    rejection_reason TEXT, ran_at TEXT
);
"""


def _seed_index(db_path: Path, *, scalars: list[float], rejected: int = 1) -> None:
    """Seed a promoted spine ``v0..vN`` with the given per-champion scalars.

    ``scalars[0]`` is the seed's scalar; every subsequent value is a
    promoted challenger's ``child_scalar``. ``rejected`` extra challengers
    are hung off the final champion so the promotion rate has a
    denominator larger than the promoted count.
    """
    conn = sqlite3.connect(db_path)
    conn.executescript(_SCHEMA)
    gens = [f"v{i}" for i in range(len(scalars))]
    for i, gid in enumerate(gens):
        parent = gens[i - 1] if i > 0 else None
        conn.execute(
            "INSERT INTO generations VALUES(?,?,?,?)",
            (EPOCH, gid, parent, 1 if i > 0 else 0),
        )
        if i > 0:
            conn.execute(
                "INSERT INTO tournaments VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    f"t{i}",
                    EPOCH,
                    parent,
                    gid,
                    "promoted",
                    scalars[i - 1],
                    scalars[i],
                    scalars[i] - scalars[i - 1],
                    "",
                    "2026-06-01T00:00:00Z",
                ),
            )
            conn.execute(
                "INSERT INTO experiments VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (EPOCH, gid, "idea", "why", "{}", "promoted", "", 0.0, 0.0, 0.0, "{}"),
            )
            conn.execute(
                "INSERT INTO runs VALUES(?,?,?,?,?,?)",
                (f"r{gid}", EPOCH, gid, "entry_a", 1000 * i, 0),
            )
    champion = gens[-1]
    for j in range(rejected):
        gid = f"v{len(gens) + j}"
        conn.execute(
            "INSERT INTO generations VALUES(?,?,?,?)",
            (EPOCH, gid, champion, 0),
        )
        conn.execute(
            "INSERT INTO experiments VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (EPOCH, gid, "idea", "why", "{}", "rejected", "worse", 0.0, 0.0, 0.0, "{}"),
        )
        conn.execute(
            "INSERT INTO runs VALUES(?,?,?,?,?,?)",
            (f"r{gid}", EPOCH, gid, "entry_a", 500, 1),
        )
    conn.commit()
    conn.close()


def _workspace(tmp_path: Path, *, noise_floor: dict | None = None) -> Path:
    ws = tmp_path / ".zicato"
    epoch_dir = ws / "epochs" / EPOCH
    epoch_dir.mkdir(parents=True)
    (ws / "current_epoch").write_text(EPOCH, encoding="utf-8")
    cfg: dict = {"contract_hash": "h1", "closed": False}
    if noise_floor is not None:
        cfg["noise_floor"] = noise_floor
    (epoch_dir / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
    return ws


def _floor(max_abs_delta: float) -> dict:
    return {
        "generation_id": "v0",
        "epoch_id": EPOCH,
        "runs": 3,
        "scalars": [1.0, 1.0 + max_abs_delta / 2, 1.0 + max_abs_delta],
        "max_abs_delta": max_abs_delta,
        "delta_std": max_abs_delta / 2,
        "measured_at": "2026-06-01T00:00:00Z",
    }


# ---------------------------------------------------------------------------
# Reader: build_optimization_trajectory
# ---------------------------------------------------------------------------


def test_trajectory_improving(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    _seed_index(ws / "index.db", scalars=[3.6, 2.4, 1.2])
    out = build_optimization_trajectory(WorkspacePaths(ws), EPOCH)
    assert out["epoch_id"] == EPOCH
    assert [p["generation_id"] for p in out["points"]] == ["v0", "v1", "v2"]
    assert out["plateaued"] is False
    assert out["verdict"] == "improving"
    # 2 promoted of 3 challengers (one rejected hang-on).
    assert out["promoted_count"] == 2
    assert out["challenger_count"] == 3
    assert out["promotion_rate"] == pytest.approx(2 / 3)
    assert out["noise_floor"] is None
    assert "note" not in out


def test_trajectory_plateaued_above_floor(tmp_path: Path) -> None:
    # A non-improving window whose movement (0.1) EXCEEDS the floor (0.05):
    # the plateau is a real, resolvable verdict.
    ws = _workspace(tmp_path, noise_floor=_floor(0.05))
    _seed_index(ws / "index.db", scalars=[3.0, 2.0, 2.0, 2.1])
    out = build_optimization_trajectory(WorkspacePaths(ws), EPOCH)
    assert out["plateaued"] is True
    assert out["recent_movement"] == pytest.approx(0.1)
    assert out["verdict"] == "plateaued"
    assert out["noise_floor"]["max_abs_delta"] == pytest.approx(0.05)


def test_trajectory_no_signal_below_floor(tmp_path: Path) -> None:
    # The same non-improving window, but the measured A/A floor (0.2) is
    # LARGER than the window's whole movement (0.1): claiming "plateaued"
    # would overstate the measurement — the honest verdict is no_signal.
    ws = _workspace(tmp_path, noise_floor=_floor(0.2))
    _seed_index(ws / "index.db", scalars=[3.0, 2.0, 2.0, 2.1])
    out = build_optimization_trajectory(WorkspacePaths(ws), EPOCH)
    assert out["plateaued"] is True
    assert out["verdict"] == "no_signal"


def test_trajectory_plateaued_without_floor_stays_plateaued(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)  # never measured → no floor on the epoch
    _seed_index(ws / "index.db", scalars=[3.0, 2.0, 2.0, 2.0])
    out = build_optimization_trajectory(WorkspacePaths(ws), EPOCH)
    assert out["plateaued"] is True
    assert out["verdict"] == "plateaued"
    assert out["noise_floor"] is None


def test_trajectory_degrades_without_index(tmp_path: Path) -> None:
    # No index.db at all: the empty shape with a note — and the floor STILL
    # attached (it lives on the epoch config, not the index).
    ws = _workspace(tmp_path, noise_floor=_floor(0.3))
    out = build_optimization_trajectory(WorkspacePaths(ws), EPOCH)
    assert out["points"] == []
    assert out["verdict"] is None
    assert out["promotion_rate"] is None
    assert "note" in out
    assert out["noise_floor"]["max_abs_delta"] == pytest.approx(0.3)


def test_trajectory_degrades_on_corrupt_index(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    (ws / "index.db").write_bytes(b"this is not a sqlite database at all")
    out = build_optimization_trajectory(WorkspacePaths(ws), EPOCH)
    assert out["points"] == []
    assert "note" in out


def test_trajectory_ignores_malformed_floor(tmp_path: Path) -> None:
    # A floor with a non-numeric max_abs_delta is treated as unmeasured.
    ws = _workspace(tmp_path, noise_floor={"max_abs_delta": "not-a-number"})
    _seed_index(ws / "index.db", scalars=[3.0, 2.0, 2.0, 2.0])
    out = build_optimization_trajectory(WorkspacePaths(ws), EPOCH)
    assert out["noise_floor"] is None
    assert out["verdict"] == "plateaued"


# ---------------------------------------------------------------------------
# Reader: build_tournament_cost
# ---------------------------------------------------------------------------


def test_cost_passthrough(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    _seed_index(ws / "index.db", scalars=[3.6, 2.4, 1.2])
    out = build_tournament_cost(WorkspacePaths(ws), EPOCH)
    assert out["epoch_id"] == EPOCH
    # 3 challengers (v1, v2 promoted; v3 rejected), one run each.
    assert out["total_run_count"] == 3
    assert out["promoted_count"] == 2
    # v1: 1000ms, v2: 2000ms, v3 (rejected): 500ms → 3500 total, /2 promotions.
    assert out["total_runtime_ms"] == 3500
    assert out["cost_per_promotion_ms"] == pytest.approx(1750.0)
    assert out["total_aborted_count"] == 1
    assert len(out["per_matchup"]) == 3


def test_cost_degrades_without_index(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    out = build_tournament_cost(WorkspacePaths(ws), EPOCH)
    assert out["per_matchup"] == []
    assert out["cost_per_promotion_ms"] is None
    assert "note" in out


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    ws = _workspace(tmp_path, noise_floor=_floor(0.2))
    _seed_index(ws / "index.db", scalars=[3.0, 2.0, 2.0, 2.1])
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("<!doctype html><title>zicato</title>", encoding="utf-8")
    app = create_app(ws, static, read_only=True)
    with TestClient(app) as c:
        yield c


def test_trajectory_endpoint(client: TestClient) -> None:
    r = client.get(f"/api/epoch/{EPOCH}/trajectory")
    assert r.status_code == 200
    body = r.json()
    assert body["epoch_id"] == EPOCH
    assert body["verdict"] == "no_signal"
    assert body["noise_floor"]["max_abs_delta"] == pytest.approx(0.2)
    assert [p["generation_id"] for p in body["points"]] == ["v0", "v1", "v2", "v3"]


def test_trajectory_endpoint_malformed_id_degrades(client: TestClient) -> None:
    r = client.get("/api/epoch/bad%20id/trajectory")
    assert r.status_code == 200
    body = r.json()
    assert body["points"] == []
    assert body["verdict"] is None


def test_cost_endpoint(client: TestClient) -> None:
    r = client.get(f"/api/epoch/{EPOCH}/cost")
    assert r.status_code == 200
    body = r.json()
    assert body["epoch_id"] == EPOCH
    assert body["total_run_count"] > 0
    assert "cost_per_promotion_ms" in body


def test_cost_endpoint_malformed_id_degrades(client: TestClient) -> None:
    r = client.get("/api/epoch/bad%20id/cost")
    assert r.status_code == 200
    body = r.json()
    assert body["per_matchup"] == []
    assert body["cost_per_promotion_ms"] is None
