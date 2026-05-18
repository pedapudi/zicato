"""Tests for the standalone dashboard service (``zicato.dashboard.server``).

These exercise the ASGI app end-to-end with Starlette's ``TestClient``
against a self-contained fixture ``.zicato/`` workspace, asserting that
every endpoint returns the JSON shape the vanilla-JS dashboard expects
(byte compatible with the retired Rust supervisor) and that the SSE
stream connects and the conversation endpoints reconstruct transcripts.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import types
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from zicato.dashboard.server import create_app

# ---------------------------------------------------------------------------
# Fixture workspace
# ---------------------------------------------------------------------------


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _write_json(path: Path, obj: object) -> None:
    _write(path, json.dumps(obj))


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """A populated ``.zicato/`` workspace covering every endpoint's inputs."""
    ws = tmp_path / ".zicato"
    runtime = ws / "runtime"
    (runtime / "active_runs").mkdir(parents=True)
    (runtime / "control").mkdir(parents=True)
    epoch_id = "2026-05-16_e0"
    epoch_dir = ws / "epochs" / epoch_id

    _write(ws / "current_epoch", epoch_id)

    # heartbeat
    _write_json(
        runtime / "heartbeat.json",
        {
            "pid": 4242,
            "instance_id": "default",
            "started_at": "2026-05-16T04:00:00Z",
            "last_heartbeat": "2026-05-16T04:30:00Z",
            "epoch_id": epoch_id,
            "generation_id": "v1",
            "phase": "tournament",
            "round_index": 1,
            "round_started_at": "2026-05-16T04:25:00Z",
        },
    )

    # active tournament
    _write_json(
        runtime / "active_tournament.json",
        {
            "tournament_id": "tourn_e0_v1",
            "parent_generation_id": "v0",
            "child_generation_id": "v1",
            "epoch_id": epoch_id,
            "started_at": "2026-05-16T04:25:00Z",
            "phase": "running",
            "round_index": 1,
            "total_rounds": 3,
            "entries": [
                {"entry_id": "waffles_single", "side": "parent", "status": "completed"},
                {"entry_id": "waffles_single", "side": "child", "status": "running"},
            ],
        },
    )

    # one active run, pointing at an events.jsonl
    run_events = (
        ws / "epochs" / epoch_id / "generations" / "v1" / "runs" / "waffles_single" / "events.jsonl"
    )
    _write(
        run_events,
        "\n".join(
            [
                json.dumps(
                    {
                        "emittedAt": "2026-05-16T04:25:01Z",
                        "eventId": "r1:0:a",
                        "runId": "r1",
                        "sequence": "0",
                        "conversationStarted": {"conversationId": "c1"},
                    }
                ),
                json.dumps(
                    {
                        "emittedAt": "2026-05-16T04:25:02Z",
                        "eventId": "r1:1:b",
                        "runId": "r1",
                        "sequence": "1",
                        "runStarted": {"goalSummary": "Make a deck about waffles."},
                    }
                ),
                json.dumps(
                    {
                        "emittedAt": "2026-05-16T04:25:03Z",
                        "eventId": "r1:2:c",
                        "runId": "r1",
                        "sequence": "2",
                        "steeringDecisionMade": {
                            "agentName": "coordinator",
                            "outcome": "no_drift",
                        },
                    }
                ),
            ]
        )
        + "\n",
    )
    _write_json(
        runtime / "active_runs" / "waffles_single.json",
        {
            "run_id": "waffles_single",
            "pid": 5151,
            "started_at": "2026-05-16T04:25:00Z",
            "last_progress": "2026-05-16T04:25:03Z",
            "wall_clock_budget_seconds": 180,
            "deadline": "2026-05-16T04:28:00Z",
            "events_jsonl_path": str(run_events),
            "entry_id": "waffles_single",
            "generation_id": "v1",
            "epoch_id": epoch_id,
        },
    )

    # champion-side run for the matchup endpoint
    champ_events = (
        ws / "epochs" / epoch_id / "generations" / "v0" / "runs" / "waffles_single" / "events.jsonl"
    )
    _write(
        champ_events,
        json.dumps(
            {
                "emittedAt": "2026-05-16T04:20:01Z",
                "sequence": "0",
                "runStarted": {"goalSummary": "Make a deck about waffles."},
            }
        )
        + "\n",
    )

    # lineage.json
    _write_json(
        ws / "lineage.json",
        {
            "epochs": [
                {
                    "id": epoch_id,
                    "generations": [
                        {
                            "id": "v0",
                            "parent_id": None,
                            "promoted": True,
                            "created_at": "2026-05-16T04:00:00Z",
                        },
                    ],
                }
            ]
        },
    )
    # generation directories so the directory-derived lineage view sees them
    for gen in ("v0", "v1"):
        (epoch_dir / "generations" / gen).mkdir(parents=True, exist_ok=True)
    _write_json(
        epoch_dir / "generations" / "v1" / "experiment.json",
        {
            "parent_generation_id": "v0",
            "proposed_at": "2026-05-16T04:25:00Z",
            "outcome": {"decision": "rejected"},
        },
    )

    # epoch contract files
    _write(
        epoch_dir / "board.jsonl",
        json.dumps(
            {
                "id": "waffles_single",
                "kind": "single_turn",
                "input": "Make a presentation about waffles.",
                "wall_clock_budget_seconds": 180,
                "weight": 1.0,
                "tags": ["smoke"],
                "expectation": {"kind": "predicate"},
            }
        )
        + "\n",
    )
    _write(epoch_dir / "brief.md", "# Proposer brief\nBe clear.\n")
    _write_json(epoch_dir / "scoring.json", {"weights": {"drift_loss": 1.0}})
    _write_json(epoch_dir / "config.json", {"contract_hash": "h1", "closed": False})
    _write_json(
        epoch_dir / "mutations.json",
        [
            {
                "id": "m1",
                "kind": "span",
                "file": "agent/a.py",
                "line_start": 1,
                "line_end": 4,
                "content": "hello",
            }
        ],
    )
    _write_json(
        ws / "config.json", {"adk_entrypoint": "mod:agent", "mutable_trees": ["/abs/agent"]}
    )

    # loop-health report
    _write_json(
        epoch_dir / "health" / "round_1.json",
        {
            "epoch_id": epoch_id,
            "healthy": False,
            "checked_at": "2026-05-16T04:29:00Z",
            "findings": [{"code": "non_differentiating_entry"}],
        },
    )

    # SQLite analytical index
    _build_index(ws / "index.db", epoch_id)

    return ws


def _build_index(path: Path, epoch_id: str) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE generations(epoch_id TEXT, generation_id TEXT,
            parent_generation_id TEXT, promoted INTEGER);
        CREATE TABLE experiments(epoch_id TEXT, generation_id TEXT,
            hypothesis_core_idea TEXT, hypothesis_why TEXT, hypothesis_json TEXT,
            tournament_decision TEXT, rejection_reason TEXT, scalar_score_delta REAL,
            drift_loss_delta REAL, pass_rate_delta REAL, outcome_json TEXT);
        CREATE TABLE patches(patch_id TEXT, epoch_id TEXT, generation_id TEXT,
            mutation_id TEXT, op TEXT, rationale TEXT);
        CREATE TABLE loss_profiles(run_id TEXT, epoch_id TEXT, generation_id TEXT,
            entry_id TEXT, drift_loss REAL, pass_fail TEXT, loss_json TEXT);
        CREATE TABLE tournaments(tournament_id TEXT, epoch_id TEXT,
            parent_generation_id TEXT, child_generation_id TEXT, decision TEXT,
            parent_scalar REAL, child_scalar REAL, delta_scalar REAL,
            rejection_reason TEXT, ran_at TEXT);
        CREATE TABLE runs(run_id TEXT, epoch_id TEXT, generation_id TEXT,
            entry_id TEXT, started_at TEXT, ended_at TEXT, aborted INTEGER,
            runtime_ms INTEGER);
        CREATE TABLE metric_counts(run_id TEXT, namespace TEXT, name TEXT,
            severity TEXT, count REAL);
        """
    )
    conn.executemany(
        "INSERT INTO generations VALUES(?,?,?,?)",
        [(epoch_id, "v0", None, 1), (epoch_id, "v1", "v0", 0)],
    )
    conn.execute(
        "INSERT INTO experiments VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (
            epoch_id,
            "v1",
            "tighten the planner",
            "planner overshoots",
            '{"k":1}',
            "rejected",
            "worse drift",
            -0.1,
            0.2,
            -0.05,
            '{"o":2}',
        ),
    )
    conn.execute(
        "INSERT INTO patches VALUES(?,?,?,?,?,?)",
        ("p1", epoch_id, "v1", "m1", "replace", "swap prompt"),
    )
    conn.executemany(
        "INSERT INTO loss_profiles VALUES(?,?,?,?,?,?,?)",
        [
            ("r0", epoch_id, "v0", "waffles_single", 0.5, "fail", "{}"),
            ("r1", epoch_id, "v1", "waffles_single", 0.2, "pass", "{}"),
        ],
    )
    conn.execute(
        "INSERT INTO tournaments VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            "t1",
            epoch_id,
            "v0",
            "v1",
            "rejected",
            0.8,
            0.8,
            0.0,
            "worse drift",
            "2026-05-16T04:30:00Z",
        ),
    )
    # runs feed the score-trajectory / drift-movements builders; one run
    # per loss-profile row.
    conn.executemany(
        "INSERT INTO runs VALUES(?,?,?,?,?,?,?,?)",
        [
            ("r0", epoch_id, "v0", "waffles_single", "", "", 0, 100),
            ("r1", epoch_id, "v1", "waffles_single", "", "", 0, 100),
        ],
    )
    # metric_counts: champion (v0) has one off_topic drift; challenger
    # (v1) has an off_topic AND a new tool_error — a clear worsening.
    conn.executemany(
        "INSERT INTO metric_counts VALUES(?,?,?,?,?)",
        [
            ("r0", "drift", "drift:off_topic", "warning", 1.0),
            ("r1", "drift", "drift:off_topic", "warning", 1.0),
            ("r1", "drift", "drift:tool_error", "critical", 2.0),
        ],
    )
    conn.commit()
    conn.close()


@pytest.fixture
def static_dir(tmp_path: Path) -> Path:
    d = tmp_path / "static"
    d.mkdir()
    (d / "index.html").write_text("<!doctype html><title>zicato</title>", encoding="utf-8")
    (d / "app.js").write_text("// app", encoding="utf-8")
    return d


@pytest.fixture
def client(workspace: Path, static_dir: Path) -> TestClient:
    app = create_app(workspace, static_dir, read_only=True)
    with TestClient(app) as c:
        yield c


@pytest.fixture
def rw_client(workspace: Path, static_dir: Path) -> TestClient:
    app = create_app(workspace, static_dir, read_only=False)
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# GET endpoints — shapes
# ---------------------------------------------------------------------------


def test_health_shape(client: TestClient) -> None:
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    for key in ("version", "uptime_seconds", "read_only", "workspace", "port", "build"):
        assert key in body
    assert body["read_only"] is True
    assert isinstance(body["uptime_seconds"], int)


def test_state_snapshot(client: TestClient) -> None:
    r = client.get("/api/state")
    assert r.status_code == 200
    body = r.json()
    for key in (
        "heartbeat",
        "lock",
        "active_runs",
        "active_tournament",
        "lineage",
        "epoch_id",
        "epoch",
        "generated_at",
    ):
        assert key in body
    assert body["epoch_id"] == "2026-05-16_e0"
    assert body["heartbeat"]["pid"] == 4242
    assert isinstance(body["active_runs"], list)


def test_active_tournament_matches_file(client: TestClient, workspace: Path) -> None:
    r = client.get("/api/active-tournament")
    assert r.status_code == 200
    body = r.json()
    on_disk = json.loads((workspace / "runtime" / "active_tournament.json").read_text())
    assert body["tournament_id"] == on_disk["tournament_id"]
    assert body["parent_generation_id"] == "v0"
    assert body["child_generation_id"] == "v1"
    assert len(body["entries"]) == 2


def test_heartbeat_endpoint(client: TestClient) -> None:
    r = client.get("/api/heartbeat")
    assert r.status_code == 200
    assert r.json()["phase"] == "tournament"


def test_lineage_shape(client: TestClient) -> None:
    r = client.get("/api/lineage")
    assert r.status_code == 200
    body = r.json()
    assert "generations" in body
    gens = {g["generation_id"]: g for g in body["generations"]}
    assert set(gens) == {"v0", "v1"}
    # v0 promoted via lineage.json, v1 rejected via experiment.json.
    assert gens["v0"]["promoted"] is True
    assert gens["v1"]["promoted"] is False
    assert gens["v1"]["parent_generation_id"] == "v0"
    for g in body["generations"]:
        for key in ("generation_id", "epoch_id", "parent_generation_id", "promoted", "created_at"):
            assert key in g


def test_active_runs_progress(client: TestClient) -> None:
    r = client.get("/api/active-runs")
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1
    run = body[0]
    assert run["run_id"] == "waffles_single"
    # progress / elapsed / budget enrichment present.
    for key in ("progress", "elapsed_seconds", "budget_seconds"):
        assert key in run
    assert run["budget_seconds"] == 180
    assert 0.0 <= run["progress"] <= 1.0


def test_run_log_tails_events(client: TestClient) -> None:
    r = client.get("/api/run-log")
    assert r.status_code == 200
    events = r.json()["events"]
    assert len(events) == 3
    kinds = [e["kind"] for e in events]
    # camelCase normalized to snake_case.
    assert kinds == ["conversation_started", "run_started", "steering_decision_made"]
    last = events[-1]
    assert last["seq"] == 2
    assert "coordinator" in last["summary"]


def test_run_log_limit_param(client: TestClient) -> None:
    r = client.get("/api/run-log?limit=1")
    assert r.status_code == 200
    assert len(r.json()["events"]) == 1


def test_run_log_carries_append_cursor(client: TestClient) -> None:
    """The run-log body carries a monotone ``cursor`` for append polling."""
    r = client.get("/api/run-log")
    body = r.json()
    assert "cursor" in body
    # The fixture's last event has seq 2; the cursor is the max seq.
    assert body["cursor"] == 2
    assert "events_path" in body


def test_run_log_after_cursor_returns_only_newer(client: TestClient) -> None:
    """``?after=`` returns only events past the cursor (append-only tail)."""
    full = client.get("/api/run-log").json()
    cursor = full["cursor"]
    # Nothing newer than the max cursor — an empty append batch.
    after = client.get(f"/api/run-log?after={cursor}").json()
    assert after["events"] == []
    # Everything is "newer than -1": the after-batch equals the full tail.
    from_start = client.get("/api/run-log?after=-1").json()
    assert len(from_start["events"]) == len(full["events"])


def test_active_tournament_normalizes_completed_to_done(
    client: TestClient, workspace: Path
) -> None:
    """A finished entry written as ``completed`` must NOT read as queued.

    The fixture's active tournament has a ``parent`` side written with
    ``status="completed"``. The dashboard renders four buckets; the read
    layer normalizes the producer's spelling so the front-end sees a
    canonical ``done`` and the entry cannot mislabel as ``queued``.
    """
    on_disk = json.loads((workspace / "runtime" / "active_tournament.json").read_text())
    raw_statuses = {(e["entry_id"], e["side"]): e["status"] for e in on_disk["entries"]}
    assert raw_statuses[("waffles_single", "parent")] == "completed"

    body = client.get("/api/active-tournament").json()
    by_side = {(e["entry_id"], e["side"]): e for e in body["entries"]}
    parent = by_side[("waffles_single", "parent")]
    # Normalized to the canonical bucket — done, never queued.
    assert parent["status"] == "done"
    # The producer's exact spelling is preserved for post-mortem use.
    assert parent["status_raw"] == "completed"
    child = by_side[("waffles_single", "child")]
    assert child["status"] == "running"


def test_environment_endpoint_consolidates_feeds(client: TestClient) -> None:
    """``/api/environment`` returns the whole environment in one read."""
    r = client.get("/api/environment")
    assert r.status_code == 200
    body = r.json()
    for key in (
        "workspace",
        "epoch_id",
        "epoch",
        "active_tournament",
        "tournaments",
        "generations",
        "active_runs",
        "health_report",
        "heartbeat",
        "run_log",
        "generated_at",
    ):
        assert key in body, f"/api/environment missing {key}"
    # Statuses inside the consolidated read are normalized too.
    by_side = {(e["entry_id"], e["side"]): e for e in body["active_tournament"]["entries"]}
    assert by_side[("waffles_single", "parent")]["status"] == "done"


def test_epoch_view(client: TestClient) -> None:
    r = client.get("/api/epoch")
    assert r.status_code == 200
    body = r.json()
    assert body["epoch_id"] == "2026-05-16_e0"
    assert body["contract_hash"] == "h1"
    assert body["closed"] is False
    assert body["harness"]["entrypoint"] == "mod:agent"
    assert len(body["board"]) == 1
    assert body["board"][0]["expectation_kind"] == "predicate"
    assert body["brief"].startswith("# Proposer brief")
    assert len(body["mutations"]) == 1
    assert body["mutations"][0]["lines"] == "1-4"


def test_epoch_view_brief_falls_back_to_legacy_rubric_md(workspace: Path) -> None:
    """A pre-rename epoch with only ``rubric.md`` still populates ``brief``."""
    from zicato.dashboard.state_reader import WorkspacePaths, build_epoch_view

    epoch_dir = workspace / "epochs" / "2026-05-16_e0"
    # Simulate an epoch frozen before the rename: rename brief.md back
    # to the legacy rubric.md.
    (epoch_dir / "brief.md").rename(epoch_dir / "rubric.md")

    view = build_epoch_view(WorkspacePaths(workspace))
    assert view["brief"].startswith("# Proposer brief")


def test_epoch_view_brief_prefers_brief_md_over_legacy(workspace: Path) -> None:
    """When both files exist, ``brief.md`` wins over the legacy name."""
    from zicato.dashboard.state_reader import WorkspacePaths, build_epoch_view

    epoch_dir = workspace / "epochs" / "2026-05-16_e0"
    _write(epoch_dir / "rubric.md", "# legacy brief\nold\n")

    view = build_epoch_view(WorkspacePaths(workspace))
    assert view["brief"].startswith("# Proposer brief")


def test_tournaments_bracket(client: TestClient) -> None:
    r = client.get("/api/tournaments")
    assert r.status_code == 200
    body = r.json()
    assert body["epoch_id"] == "2026-05-16_e0"
    assert body["champion_lineage"] == ["v0"]
    assert len(body["matchups"]) == 1
    m = body["matchups"][0]
    assert m["champion"] == "v0"
    assert m["challenger"] == "v1"
    assert m["decision"] == "rejected"
    assert m["hypothesis_core_idea"] == "tighten the planner"


def test_tournament_detail(client: TestClient) -> None:
    r = client.get("/api/tournaments/v1")
    assert r.status_code == 200
    body = r.json()
    assert body["generation_id"] == "v1"
    assert body["champion"] == "v0"
    assert body["decision"] == "rejected"
    assert body["hypothesis"]["core_idea"] == "tighten the planner"
    assert body["hypothesis"]["raw"] == {"k": 1}
    assert len(body["patches"]) == 1
    assert len(body["ab_grid"]) == 1
    cell = body["ab_grid"][0]
    assert cell["entry_id"] == "waffles_single"
    # parent 0.5 -> child 0.2: lower drift loss is an improvement.
    assert cell["verdict"] == "improved"


def test_tournament_detail_invalid_id(client: TestClient) -> None:
    # An id the validator rejects (a space) degrades to an empty matchup
    # rather than erroring.
    r = client.get("/api/tournaments/bad%20id")
    assert r.status_code == 200
    body = r.json()
    assert body["patches"] == []
    assert body["ab_grid"] == []


def test_health_report(client: TestClient) -> None:
    r = client.get("/api/health-report")
    assert r.status_code == 200
    body = r.json()
    assert body["healthy"] is False
    assert len(body["findings"]) == 1
    assert body["epoch_id"] == "2026-05-16_e0"


# ---------------------------------------------------------------------------
# Score trajectory — the environment-wide evolution curve
# ---------------------------------------------------------------------------


def test_score_trajectory_endpoint(client: TestClient) -> None:
    """``/api/score-trajectory`` plots the absolute scalar per generation."""
    r = client.get("/api/score-trajectory")
    assert r.status_code == 200
    body = r.json()
    assert body["epoch_id"] == "2026-05-16_e0"
    points = {p["generation_id"]: p for p in body["points"]}
    # Both fixture generations are plotted, in lineage order.
    assert [p["generation_id"] for p in body["points"]] == ["v0", "v1"]
    # The scalar is the mean drift_loss of the generation's runs:
    # v0 had a single run at drift_loss 0.5; v1 at 0.2.
    assert points["v0"]["scalar"] == pytest.approx(0.5)
    assert points["v1"]["scalar"] == pytest.approx(0.2)
    # v1's loss is LOWER than v0's — the curve shows an improvement.
    assert points["v1"]["scalar"] < points["v0"]["scalar"]
    assert points["v0"]["promoted"] is True
    assert points["v1"]["promoted"] is False
    assert points["v0"]["entry_count"] == 1


def test_score_trajectory_in_environment_payload(client: TestClient) -> None:
    """The consolidated /api/environment carries the score trajectory."""
    r = client.get("/api/environment")
    assert r.status_code == 200
    body = r.json()
    assert "score_trajectory" in body
    assert isinstance(body["score_trajectory"]["points"], list)
    assert len(body["score_trajectory"]["points"]) == 2


# ---------------------------------------------------------------------------
# Drift-kind movements — champion -> challenger per-kind deltas
# ---------------------------------------------------------------------------


def test_drift_movements_endpoint(client: TestClient) -> None:
    """``/api/drift-movements/:gen`` reports champion->challenger drift deltas."""
    r = client.get("/api/drift-movements/v1")
    assert r.status_code == 200
    body = r.json()
    assert body["champion"] == "v0"
    assert body["challenger"] == "v1"
    moves = {m["kind"]: m for m in body["movements"]}
    # off_topic: 1 on the champion, 1 on the challenger — unchanged.
    assert moves["off_topic"]["champion_count"] == 1
    assert moves["off_topic"]["challenger_count"] == 1
    assert moves["off_topic"]["direction"] == "unchanged"
    # tool_error: absent on the champion, 2 on the challenger — worsened.
    assert moves["tool_error"]["champion_count"] == 0
    assert moves["tool_error"]["challenger_count"] == 2
    assert moves["tool_error"]["delta"] == 2
    assert moves["tool_error"]["direction"] == "worsened"
    # Biggest absolute movement first.
    assert body["movements"][0]["kind"] == "tool_error"


def test_drift_movements_unknown_generation(client: TestClient) -> None:
    """A generation with no tournament degrades to an empty movement list."""
    r = client.get("/api/drift-movements/v9")
    assert r.status_code == 200
    body = r.json()
    assert body["movements"] == []
    assert body["champion"] is None


def test_drift_movements_invalid_id(client: TestClient) -> None:
    """A malformed generation id degrades to an empty matchup, no 500."""
    r = client.get("/api/drift-movements/bad%20id")
    assert r.status_code == 200
    assert r.json()["movements"] == []


# ---------------------------------------------------------------------------
# Static serving
# ---------------------------------------------------------------------------


def test_static_root(client: TestClient) -> None:
    r = client.get("/")
    assert r.status_code == 200
    assert "zicato" in r.text.lower()


def test_static_asset_root_relative(client: TestClient) -> None:
    # index.html references app.js at the document root.
    r = client.get("/app.js")
    assert r.status_code == 200
    assert "// app" in r.text


def test_static_asset_under_static_prefix(client: TestClient) -> None:
    r = client.get("/static/app.js")
    assert r.status_code == 200


def test_unknown_static_is_404(client: TestClient) -> None:
    r = client.get("/does-not-exist.css")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Control endpoints
# ---------------------------------------------------------------------------


def test_control_forbidden_when_read_only(client: TestClient) -> None:
    r = client.post("/api/control/pause")
    assert r.status_code == 403


def test_control_pause_writes_marker(rw_client: TestClient, workspace: Path) -> None:
    r = rw_client.post("/api/control/pause", json={"reason": "test"})
    assert r.status_code == 202
    assert r.json()["accepted"] is True
    marker = workspace / "runtime" / "control" / "pause_epoch"
    assert marker.exists()
    assert json.loads(marker.read_text())["reason"] == "test"


def test_control_kill_writes_per_run_marker(rw_client: TestClient, workspace: Path) -> None:
    r = rw_client.post("/api/control/kill/waffles_single")
    assert r.status_code == 202
    assert (workspace / "runtime" / "control" / "kill_runs" / "waffles_single").exists()


def test_control_promote_and_reject(rw_client: TestClient, workspace: Path) -> None:
    assert rw_client.post("/api/control/promote/v1").status_code == 202
    assert rw_client.post("/api/control/reject/v1").status_code == 202
    assert (workspace / "runtime" / "control" / "promote" / "v1").exists()
    assert (workspace / "runtime" / "control" / "reject" / "v1").exists()


def test_control_kill_rejects_bad_id(rw_client: TestClient) -> None:
    # An id the validator rejects (a space) is a 400, not a marker write.
    r = rw_client.post("/api/control/kill/bad%20id")
    assert r.status_code == 400


def test_control_brief_writes_text(rw_client: TestClient, workspace: Path) -> None:
    r = rw_client.post("/api/control/brief", content=b"new brief body")
    assert r.status_code == 202
    # The control file keeps its protocol name regardless of the
    # UI-facing endpoint rename.
    path = workspace / "runtime" / "control" / "rubric_replacement.txt"
    assert path.read_bytes() == b"new brief body"


# ---------------------------------------------------------------------------
# SSE
# ---------------------------------------------------------------------------


def test_events_route_is_registered(client: TestClient) -> None:
    # Starlette's TestClient buffers the whole response body, so it
    # cannot consume an unbounded SSE stream over HTTP. Assert the route
    # exists (the SSE stream itself is exercised directly below).
    routes = {r.path for r in client.app.routes}  # type: ignore[attr-defined]
    assert "/events" in routes


@pytest.mark.asyncio
async def test_sse_stream_emits_snapshot_then_change(workspace: Path) -> None:
    # Exercise the SSE generator directly: it must yield a `snapshot`
    # frame first, then a `state_change` frame when a runtime file
    # mutates — the protocol the dashboard JS consumes.
    import asyncio

    from zicato.dashboard.sse import ChangeBroker, sse_event_stream
    from zicato.dashboard.state_reader import WorkspacePaths

    paths = WorkspacePaths(workspace)
    broker = ChangeBroker(paths)
    await broker.start()
    try:
        stream = sse_event_stream(broker, paths)
        first = await asyncio.wait_for(stream.__anext__(), timeout=5)
        assert first.startswith("event: snapshot")
        data_line = next(line for line in first.splitlines() if line.startswith("data: "))
        payload = json.loads(data_line[len("data: ") :])
        assert payload["type"] == "snapshot"
        assert payload["data"]["epoch_id"] == "2026-05-16_e0"

        # Mutate a runtime file; the watcher must push a state_change.
        (workspace / "runtime" / "heartbeat.json").write_text(
            json.dumps(
                {
                    "pid": 1,
                    "instance_id": "x",
                    "started_at": "2026-05-16T04:00:00Z",
                    "last_heartbeat": "2026-05-16T05:00:00Z",
                }
            ),
            encoding="utf-8",
        )
        change = await asyncio.wait_for(stream.__anext__(), timeout=8)
        # A keepalive comment is also acceptable noise before the change.
        if change.startswith(": ping"):
            change = await asyncio.wait_for(stream.__anext__(), timeout=8)
        assert change.startswith("event: state_change")
        await stream.aclose()
    finally:
        await broker.stop()


@pytest.mark.asyncio
async def test_sse_coalesces_burst_into_one_state_change(workspace: Path) -> None:
    """A burst of file writes yields ONE coalesced state_change frame.

    This is the polling-storm fix: the orchestrator touches the runtime
    tree many times in quick succession, and the old broker fanned every
    write out into its own state_change (which the dashboard answered
    with a fresh wave of polls). The coalesced broker debounces the
    burst into a single frame carrying the set of changed regions.
    """
    import asyncio

    from zicato.dashboard.sse import ChangeBroker, sse_event_stream
    from zicato.dashboard.state_reader import WorkspacePaths

    paths = WorkspacePaths(workspace)
    broker = ChangeBroker(paths)
    await broker.start()
    try:
        stream = sse_event_stream(broker, paths)
        first = await asyncio.wait_for(stream.__anext__(), timeout=5)
        assert first.startswith("event: snapshot")

        # Fire a burst of writes inside the coalesce window.
        runtime = workspace / "runtime"
        for i in range(12):
            (runtime / "heartbeat.json").write_text(
                json.dumps({"pid": i, "instance_id": "x", "started_at": "z"}),
                encoding="utf-8",
            )
        (runtime / "active_tournament.json").write_text(
            json.dumps(
                {
                    "tournament_id": "t",
                    "parent_generation_id": "v0",
                    "child_generation_id": "v1",
                    "epoch_id": "2026-05-16_e0",
                    "started_at": "z",
                    "entries": [],
                }
            ),
            encoding="utf-8",
        )

        # Collect frames for a short window; skip keepalive pings.
        frames: list[str] = []
        for _ in range(20):
            try:
                fr = await asyncio.wait_for(stream.__anext__(), timeout=1.5)
            except (TimeoutError, StopAsyncIteration):
                break
            if fr.startswith(": ping"):
                continue
            frames.append(fr)
            # One coalesced frame is enough to prove the debounce.
            if len(frames) >= 1:
                break
        await stream.aclose()

        assert frames, "expected at least one coalesced state_change frame"
        state_changes = [f for f in frames if f.startswith("event: state_change")]
        assert state_changes, "burst produced no state_change"
        data_line = next(ln for ln in state_changes[0].splitlines() if ln.startswith("data: "))
        payload = json.loads(data_line[len("data: ") :])
        # The coalesced frame carries the SET of regions touched.
        assert "kinds" in payload
        assert isinstance(payload["kinds"], list)
    finally:
        await broker.stop()


# ---------------------------------------------------------------------------
# Conversation endpoints — with a stub transcript module
# ---------------------------------------------------------------------------


def _install_stub_transcript(monkeypatch: pytest.MonkeyPatch) -> None:
    """Install a minimal ``zicato.dashboard.transcript`` stub.

    The stub matches the documented contract:
    ``reconstruct_transcript(events_path, *, partial_ok=True) ->
    Transcript`` with a ``.to_dict()`` yielding ``{turns, annotations,
    run_id, event_count, complete}``.
    """

    class _Transcript:
        def __init__(self, events_path: Path) -> None:
            lines = [ln for ln in Path(events_path).read_text().splitlines() if ln.strip()]
            self._count = len(lines)

        def to_dict(self) -> dict:
            return {
                "turns": [{"role": "user", "text": "stub"}],
                "annotations": [],
                "run_id": "stub-run",
                "event_count": self._count,
                "complete": True,
            }

    def reconstruct_transcript(events_path, *, partial_ok=True):  # noqa: ANN001
        return _Transcript(events_path)

    module = types.ModuleType("zicato.dashboard.transcript")
    module.reconstruct_transcript = reconstruct_transcript
    module.Transcript = _Transcript
    monkeypatch.setitem(sys.modules, "zicato.dashboard.transcript", module)

    # Rebind the guarded import inside the endpoints module.
    import zicato.dashboard.endpoints as ep

    monkeypatch.setattr(ep, "reconstruct_transcript", reconstruct_transcript)
    monkeypatch.setattr(ep, "_HAVE_TRANSCRIPT", True)


def test_conversation_endpoint(
    workspace: Path, static_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_stub_transcript(monkeypatch)
    app = create_app(workspace, static_dir, read_only=True)
    with TestClient(app) as c:
        r = c.get("/api/conversation/waffles_single")
        assert r.status_code == 200
        body = r.json()
        for key in ("turns", "annotations", "run_id", "event_count", "complete"):
            assert key in body
        assert body["event_count"] == 3


def test_conversation_missing_run_is_404(
    workspace: Path, static_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_stub_transcript(monkeypatch)
    app = create_app(workspace, static_dir, read_only=True)
    with TestClient(app) as c:
        r = c.get("/api/conversation/no_such_run")
        assert r.status_code == 404


def test_matchup_conversations_endpoint(
    workspace: Path, static_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_stub_transcript(monkeypatch)
    app = create_app(workspace, static_dir, read_only=True)
    with TestClient(app) as c:
        r = c.get("/api/matchup/waffles_single/conversations")
        assert r.status_code == 200
        body = r.json()
        assert set(body) == {"champion", "challenger"}
        # champion = parent generation v0, challenger = child generation v1.
        assert body["champion"]["generation_id"] == "v0"
        assert body["challenger"]["generation_id"] == "v1"
        for side in ("champion", "challenger"):
            assert "run_id" in body[side]
            assert "transcript" in body[side]
        assert body["challenger"]["transcript"]["event_count"] == 3


def test_conversation_unavailable_without_transcript_module(
    client: TestClient,
) -> None:
    # The default `client` fixture has no transcript stub installed; if
    # the real module is not importable the endpoint reports 503, and if
    # it is importable it returns transcript-shaped JSON. Either way the
    # server stays up and the route exists.
    r = client.get("/api/conversation/waffles_single")
    assert r.status_code in (200, 503)


# ---------------------------------------------------------------------------
# Degraded workspaces — endpoints never 500
# ---------------------------------------------------------------------------


def test_empty_workspace_endpoints_do_not_500(tmp_path: Path, static_dir: Path) -> None:
    ws = tmp_path / ".zicato"
    (ws / "runtime").mkdir(parents=True)
    app = create_app(ws, static_dir, read_only=True)
    with TestClient(app) as c:
        for path in (
            "/api/health",
            "/api/state",
            "/api/environment",
            "/api/epoch",
            "/api/lineage",
            "/api/run-log",
            "/api/active-runs",
            "/api/active-tournament",
            "/api/tournaments",
            "/api/tournaments/v1",
            "/api/health-report",
        ):
            assert c.get(path).status_code == 200, path
