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

The round-pipeline projection (``build_round_pipeline`` /
``GET /api/live/pipeline``) is covered here too: the server owns the
propose→apply→run→gate inference the stepper renders verbatim.
"""

from __future__ import annotations

import datetime as _dt
import json
import sqlite3
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from zicato.dashboard.readers.loop_view import _project_pipeline
from zicato.dashboard.server import create_app
from zicato.dashboard.state_reader import (
    WorkspacePaths,
    build_optimization_trajectory,
    build_round_pipeline,
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
# Pipeline projection: the pure inference
# ---------------------------------------------------------------------------


def _states(steps: list[dict]) -> dict[str, str]:
    return {s["id"]: s["state"] for s in steps}


def test_pipeline_proposing_no_field(_=None) -> None:
    steps, active, decision = _project_pipeline("proposing:round_0:v1")
    assert _states(steps) == {
        "propose": "active",
        "apply": "pending",
        "run": "pending",
        "gate": "pending",
    }
    assert active == "propose"
    assert decision is None


def test_pipeline_proposing_with_mixed_field() -> None:
    steps, active, _ = _project_pipeline(
        "proposing:round_1:v4",
        field_counts={"proposing": 1, "applied": 2, "rejected": 1, "total": 4},
    )
    assert _states(steps)["propose"] == "active"
    assert active == "propose"
    by_id = {s["id"]: s for s in steps}
    assert by_id["propose"]["detail"] == "3/4 slots settled"


def test_pipeline_field_settled_moves_to_apply() -> None:
    steps, active, _ = _project_pipeline(
        "proposing:round_1:v4",
        field_counts={"proposing": 0, "applied": 3, "rejected": 1, "total": 4},
    )
    assert _states(steps) == {
        "propose": "done",
        "apply": "active",
        "run": "pending",
        "gate": "pending",
    }
    assert active == "apply"
    assert {s["id"]: s for s in steps}["apply"]["detail"] == "3 applied · 1 rejected"


def test_pipeline_tournament_running() -> None:
    steps, active, _ = _project_pipeline("tournament:round_0:v1", run_count=3)
    assert _states(steps) == {
        "propose": "done",
        "apply": "done",
        "run": "active",
        "gate": "pending",
    }
    assert active == "run"
    assert {s["id"]: s for s in steps}["run"]["detail"] == "3 units in flight"


def test_pipeline_replicate_audit_is_the_gate() -> None:
    steps, active, _ = _project_pipeline("tournament:round_0:bt-replicate:v0:v1")
    assert _states(steps)["run"] == "done"
    assert _states(steps)["gate"] == "active"
    assert active == "gate"


def test_pipeline_settled_tournament_is_deciding() -> None:
    steps, active, _ = _project_pipeline(
        "tournament:round_0:v1", tournament_phase="completed", run_count=0
    )
    assert _states(steps)["run"] == "done"
    assert _states(steps)["gate"] == "active"
    assert {s["id"]: s for s in steps}["gate"]["detail"] == "deciding"
    assert active == "gate"


def test_pipeline_done_carries_the_decision() -> None:
    steps, active, decision = _project_pipeline("done:round_0:v1:promoted")
    assert all(s["state"] == "done" for s in steps)
    assert active is None
    assert decision == "promoted"


def test_pipeline_after_round_reads_done() -> None:
    steps, _, decision = _project_pipeline("after_round_2:rejected")
    assert all(s["state"] == "done" for s in steps)
    assert decision == "rejected"


def test_pipeline_idle_heads_read_all_pending() -> None:
    for phase in ("", "idle", "evolve_n_rounds:start", "evolve_n_rounds:done"):
        steps, active, decision = _project_pipeline(phase)
        assert all(s["state"] == "pending" for s in steps), phase
        assert active is None
        assert decision is None


# ---------------------------------------------------------------------------
# Pipeline projection: the workspace reader
# ---------------------------------------------------------------------------


def _pipeline_workspace(
    tmp_path: Path,
    *,
    phase: str,
    fresh: bool = True,
    tournament: dict | None = None,
    runs: int = 0,
) -> Path:
    ws = tmp_path / ".zicato"
    (ws / "runtime" / "active_runs").mkdir(parents=True)
    (ws / "epochs").mkdir(parents=True)
    now = _dt.datetime.now(_dt.UTC)
    beat = now if fresh else now - _dt.timedelta(minutes=10)
    (ws / "runtime" / "heartbeat.json").write_text(
        json.dumps(
            {
                "pid": 1,
                "instance_id": "default",
                "started_at": now.isoformat().replace("+00:00", "Z"),
                "last_heartbeat": beat.isoformat().replace("+00:00", "Z"),
                "epoch_id": EPOCH,
                "generation_id": "v1",
                "phase": phase,
                "round_index": 0,
            }
        ),
        encoding="utf-8",
    )
    if tournament is not None:
        (ws / "runtime" / "active_tournament.json").write_text(
            json.dumps(tournament), encoding="utf-8"
        )
    started = now.isoformat().replace("+00:00", "Z")
    deadline = (now + _dt.timedelta(minutes=3)).isoformat().replace("+00:00", "Z")
    for i in range(runs):
        (ws / "runtime" / "active_runs" / f"r{i}.json").write_text(
            json.dumps(
                {
                    "run_id": f"r{i}",
                    "pid": 100 + i,
                    "started_at": started,
                    "last_progress": started,
                    "wall_clock_budget_seconds": 180,
                    "deadline": deadline,
                    "events_jsonl_path": str(ws / "events.jsonl"),
                    "entry_id": f"entry_{i}",
                    "generation_id": "v1",
                    "epoch_id": EPOCH,
                }
            ),
            encoding="utf-8",
        )
    return ws


def test_build_round_pipeline_live_tournament(tmp_path: Path) -> None:
    ws = _pipeline_workspace(
        tmp_path,
        phase="tournament:round_0:v1",
        runs=2,
        tournament={
            "tournament_id": "t1",
            "epoch_id": EPOCH,
            "parent_generation_id": "v0",
            "child_generation_id": "v1",
            "phase": "running",
            "started_at": "2026-06-01T00:00:00Z",
            "entries": [],
        },
    )
    out = build_round_pipeline(WorkspacePaths(ws))
    assert out["running"] is True
    assert out["active_step"] == "run"
    assert out["round_index"] == 0
    assert _states(out["steps"])["propose"] == "done"
    assert _states(out["steps"])["run"] == "active"
    assert out["in_flight"] == 2


def test_build_round_pipeline_stale_heartbeat_not_running(tmp_path: Path) -> None:
    ws = _pipeline_workspace(tmp_path, phase="tournament:round_0:v1", fresh=False)
    out = build_round_pipeline(WorkspacePaths(ws))
    # The projection survives (post-mortem honesty) but running gates off.
    assert out["running"] is False
    assert out["stale"] is True
    assert _states(out["steps"])["run"] == "active"


def test_build_round_pipeline_foreign_epoch_tournament_ignored(tmp_path: Path) -> None:
    # A retained tournament from ANOTHER epoch must not feed field counts.
    ws = _pipeline_workspace(
        tmp_path,
        phase="proposing:round_0:v1",
        tournament={
            "tournament_id": "old",
            "epoch_id": "some_other_epoch",
            "parent_generation_id": "v0",
            "child_generation_id": "v9",
            "phase": "running",
            "started_at": "2026-06-01T00:00:00Z",
            "entries": [],
            "field_status": [{"generation_id": "v9", "status": "applied"}],
        },
    )
    out = build_round_pipeline(WorkspacePaths(ws))
    # With the foreign field ignored there is no slot data → propose active.
    assert out["active_step"] == "propose"
    by_id = {s["id"]: s for s in out["steps"]}
    assert by_id["propose"]["detail"] == ""


def test_build_round_pipeline_empty_workspace(tmp_path: Path) -> None:
    ws = tmp_path / ".zicato"
    ws.mkdir()
    out = build_round_pipeline(WorkspacePaths(ws))
    assert out["running"] is False
    assert out["active_step"] is None
    assert all(s["state"] == "pending" for s in out["steps"])


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


def test_live_pipeline_endpoint(client: TestClient) -> None:
    # The fixture workspace has no runtime tree: an honest idle projection.
    r = client.get("/api/live/pipeline")
    assert r.status_code == 200
    body = r.json()
    assert body["running"] is False
    assert [s["id"] for s in body["steps"]] == ["propose", "apply", "run", "gate"]
    assert all(s["state"] == "pending" for s in body["steps"])
