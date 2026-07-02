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
    (d / "app_T.js").write_text("// app", encoding="utf-8")
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


def test_read_heartbeat_dict_falls_back_to_mtime_for_unageable_stamp(tmp_path: Path) -> None:
    """A heartbeat whose ``last_heartbeat`` is not a usable ISO timestamp must
    still surface an AGEABLE timestamp (the file mtime) so the dashboard's
    staleness gate can age out a dead/torn-down run instead of reading it LIVE.
    """
    import datetime as _dt
    import os

    from zicato.dashboard.state_reader import WorkspacePaths, read_heartbeat_dict

    ws = tmp_path / "ws"
    runtime = ws / ".zicato" / "runtime"
    runtime.mkdir(parents=True)
    hb_path = runtime / "heartbeat.json"
    # A record whose mandatory `last_heartbeat` is present (so `from_dict`
    # accepts it) but is NOT a parseable ISO timestamp — exactly the kind of
    # frozen/garbage stamp a torn-down run can leave behind.
    hb_path.write_text(
        json.dumps(
            {
                "pid": 4242,
                "instance_id": "default",
                "started_at": "2026-06-03T01:56:49Z",
                "last_heartbeat": "not-a-timestamp",
                "phase": "tournament:round_0:final",
                "epoch_id": "2026-06-03_e3",
                "generation_id": "v3",
            }
        ),
        encoding="utf-8",
    )
    # Stamp a known mtime ~13 minutes in the past so we can assert the fallback.
    mtime = _dt.datetime(2026, 6, 3, 3, 6, 4, tzinfo=_dt.UTC).timestamp()
    os.utime(hb_path, (mtime, mtime))

    out = read_heartbeat_dict(WorkspacePaths(ws / ".zicato"))
    assert out is not None
    # The unageable stamp is replaced by the file mtime as an ISO-8601 string.
    assert out["last_heartbeat"] == "2026-06-03T03:06:04Z"
    # Other fields pass through unchanged.
    assert out["phase"] == "tournament:round_0:final"
    assert out["pid"] == 4242


def test_read_heartbeat_dict_preserves_a_usable_stamp(tmp_path: Path) -> None:
    """A heartbeat that already carries a parseable ISO ``last_heartbeat`` is
    returned verbatim — the mtime fallback only fires for an unageable stamp.
    """
    from zicato.dashboard.state_reader import WorkspacePaths, read_heartbeat_dict

    ws = tmp_path / "ws"
    runtime = ws / ".zicato" / "runtime"
    runtime.mkdir(parents=True)
    (runtime / "heartbeat.json").write_text(
        json.dumps(
            {
                "pid": 7,
                "instance_id": "default",
                "started_at": "2026-06-03T01:56:49Z",
                "last_heartbeat": "2026-06-03T03:30:00Z",
                "phase": "proposing:field",
            }
        ),
        encoding="utf-8",
    )
    out = read_heartbeat_dict(WorkspacePaths(ws / ".zicato"))
    assert out is not None
    assert out["last_heartbeat"] == "2026-06-03T03:30:00Z"


def test_standalone_harmonograf_url_injected_into_heartbeat(tmp_path: Path) -> None:
    """A persistent per-workspace url is injected so the deep-links light up.

    The standalone dashboard resolves a persistent harmonograf and stamps the
    url onto ``WorkspacePaths``; ``read_heartbeat_dict`` must inject it (and the
    ``harmonograf_persistent`` flag) so the frontend's liveness gate reads true
    even with no active run.
    """
    from zicato.dashboard.state_reader import WorkspacePaths, read_heartbeat_dict

    ws = tmp_path / "ws"
    runtime = ws / ".zicato" / "runtime"
    runtime.mkdir(parents=True)
    (runtime / "heartbeat.json").write_text(
        json.dumps(
            {
                "pid": 7,
                "instance_id": "default",
                "started_at": "2026-06-03T01:56:49Z",
                "last_heartbeat": "2026-06-03T03:30:00Z",
                "phase": "idle",
            }
        ),
        encoding="utf-8",
    )
    url = "http://127.0.0.1:42017"
    out = read_heartbeat_dict(WorkspacePaths(ws / ".zicato", harmonograf_url=url))
    assert out is not None
    assert out["harmonograf_url"] == url
    assert out["harmonograf_persistent"] is True


def test_live_evolve_heartbeat_url_wins_over_injected(tmp_path: Path) -> None:
    """Precedence: a live evolve's heartbeat ``harmonograf_url`` is not clobbered.

    When the on-disk heartbeat already carries a url (a live evolve writing its
    own server), the dashboard-injected persistent url must NOT overwrite it —
    the live server wins. The ``harmonograf_persistent`` flag is still set.
    """
    from zicato.dashboard.state_reader import WorkspacePaths, read_heartbeat_dict

    ws = tmp_path / "ws"
    runtime = ws / ".zicato" / "runtime"
    runtime.mkdir(parents=True)
    live_url = "http://127.0.0.1:55555"
    (runtime / "heartbeat.json").write_text(
        json.dumps(
            {
                "pid": 7,
                "instance_id": "default",
                "started_at": "2026-06-03T01:56:49Z",
                "last_heartbeat": "2026-06-03T03:30:00Z",
                "phase": "tournament",
                "harmonograf_url": live_url,
            }
        ),
        encoding="utf-8",
    )
    out = read_heartbeat_dict(
        WorkspacePaths(ws / ".zicato", harmonograf_url="http://127.0.0.1:42017")
    )
    assert out is not None
    # The live evolve's url wins.
    assert out["harmonograf_url"] == live_url
    assert out["harmonograf_persistent"] is True


def test_standalone_harmonograf_synthesizes_heartbeat_for_postmortem(tmp_path: Path) -> None:
    """No on-disk heartbeat + a persistent url ⇒ a synthetic heartbeat renders links."""
    from zicato.dashboard.state_reader import WorkspacePaths, read_heartbeat_dict

    ws = tmp_path / "ws"
    (ws / ".zicato" / "runtime").mkdir(parents=True)
    url = "http://127.0.0.1:42017"
    out = read_heartbeat_dict(WorkspacePaths(ws / ".zicato", harmonograf_url=url))
    assert out is not None
    assert out["harmonograf_url"] == url
    assert out["harmonograf_persistent"] is True
    # An ageable timestamp is always present.
    assert out["last_heartbeat"]


def test_no_injection_without_persistent_url(tmp_path: Path) -> None:
    """Without a persistent url, no heartbeat at all stays ``None`` (no synthesis)."""
    from zicato.dashboard.state_reader import WorkspacePaths, read_heartbeat_dict

    ws = tmp_path / "ws"
    (ws / ".zicato" / "runtime").mkdir(parents=True)
    out = read_heartbeat_dict(WorkspacePaths(ws / ".zicato"))
    assert out is None


def test_run_serves_when_harmonograf_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Failure isolation: ``run`` builds a working app even if harmonograf fails.

    The ensure-helper is stubbed to raise; ``_ensure_workspace_harmonograf`` must
    swallow it and the dashboard app still answers ``/api/health``.
    """
    import zicato.dashboard.server as server_mod

    def _boom(_root: Path) -> object:
        raise RuntimeError("harmonograf exploded")

    # Patch the public ensure-helper the server imports lazily.
    import zicato.telemetry.harmonograf_supervisor as sup

    monkeypatch.setattr(sup, "ensure_workspace_harmonograf", _boom)

    handle = server_mod._ensure_workspace_harmonograf(tmp_path)
    # No-op handle — empty url, not launched, shutdown is safe.
    assert getattr(handle, "web_url", None) == ""
    assert getattr(handle, "launched", None) is False
    handle.shutdown()

    # And a created app (with the empty url) still serves health.
    static_dir = tmp_path / "static"
    static_dir.mkdir()
    app = create_app(tmp_path / ".zicato", static_dir, read_only=True, harmonograf_url="")
    (tmp_path / ".zicato" / "runtime").mkdir(parents=True)
    with TestClient(app) as c:
        assert c.get("/api/health").status_code == 200


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


def test_lineage_round_index_read(tmp_path: Path) -> None:
    """build_lineage_view surfaces a generation's round_index when stamped,
    and OMITS the key (not null) when the stamp is absent — so a pre-feature
    experiment.json reads byte-identically and the dashboard's lineage fallback
    kicks in."""
    from zicato.dashboard.state_reader import WorkspacePaths, build_lineage_view

    epoch_dir = tmp_path / "epochs" / "2026-06-02_e0"
    for gen in ("v0", "v1", "v2"):
        (epoch_dir / "generations" / gen).mkdir(parents=True, exist_ok=True)
    # v1 is minted in round 1 (stamped); v2 has no stamp (pre-feature).
    _write_json(
        epoch_dir / "generations" / "v1" / "experiment.json",
        {"parent_generation_id": "v0", "round_index": 1, "outcome": {"decision": "rejected"}},
    )
    _write_json(
        epoch_dir / "generations" / "v2" / "experiment.json",
        {"parent_generation_id": "v0", "outcome": {"decision": "rejected"}},
    )
    paths = WorkspacePaths(tmp_path)
    gens = {g["generation_id"]: g for g in build_lineage_view(paths)["generations"]}
    assert gens["v1"]["round_index"] == 1, "the stamped birth round is surfaced"
    assert "round_index" not in gens["v2"], "an unstamped gen OMITS the key (not null)"
    assert "round_index" not in gens["v0"], "a directory-only seed omits round_index"


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
        "epochs",
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


def test_epoch_view_board_skips_board_meta_header(workspace: Path) -> None:
    """The board's ``board_meta`` header line is not a board entry.

    A board.jsonl whose first line is a ``board_meta`` object must not
    surface that line as a spurious all-``—`` board row.
    """
    from zicato.dashboard.state_reader import WorkspacePaths, build_epoch_view

    epoch_dir = workspace / "epochs" / "2026-05-16_e0"
    _write(
        epoch_dir / "board.jsonl",
        "\n".join(
            [
                json.dumps({"board_meta": True, "disable_drift": ["user_steer"]}),
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
                ),
            ]
        )
        + "\n",
    )
    view = build_epoch_view(WorkspacePaths(workspace))
    # Only the real entry — the board_meta header is dropped.
    assert len(view["board"]) == 1
    assert view["board"][0]["id"] == "waffles_single"
    assert all(e.get("board_meta") is not True for e in view["board"])


# ---------------------------------------------------------------------------
# Per-epoch goal summary — the Overview epochs-table annotation
# ---------------------------------------------------------------------------


def test_build_epochs_summary_distils_goal_from_brief(workspace: Path) -> None:
    """build_epochs_summary distils a one-line goal from each epoch brief."""
    from zicato.dashboard.state_reader import WorkspacePaths, build_epochs_summary

    epoch_dir = workspace / "epochs" / "2026-05-16_e0"
    _write(
        epoch_dir / "brief.md",
        "# Proposer brief\n\n## Goal\n\n"
        "Produce coherent, structured presentation outputs from the tree.\n\n"
        "- a bullet that must not be picked as the summary\n\n"
        "## Preferred edits\n\nSomething else.\n",
    )
    summary = build_epochs_summary(WorkspacePaths(workspace))
    assert len(summary) == 1
    row = summary[0]
    assert row["epoch_id"] == "2026-05-16_e0"
    assert row["goal"] == "Produce coherent, structured presentation outputs from the tree."


def test_build_epochs_summary_goal_none_when_no_goal_section(workspace: Path) -> None:
    """A brief with no ``## Goal`` section yields a null goal, not an error."""
    from zicato.dashboard.state_reader import WorkspacePaths, build_epochs_summary

    epoch_dir = workspace / "epochs" / "2026-05-16_e0"
    _write(epoch_dir / "brief.md", "# Proposer brief\n\n## Preferred edits\n\nNo goal here.\n")
    summary = build_epochs_summary(WorkspacePaths(workspace))
    assert summary[0]["goal"] is None


def test_build_epochs_summary_reads_legacy_rubric_md(workspace: Path) -> None:
    """The goal distillation falls back to the legacy ``rubric.md`` name."""
    from zicato.dashboard.state_reader import WorkspacePaths, build_epochs_summary

    epoch_dir = workspace / "epochs" / "2026-05-16_e0"
    (epoch_dir / "brief.md").unlink()
    _write(epoch_dir / "rubric.md", "# Epoch\n\n## Goal\n\nStabilise the schema.\n")
    summary = build_epochs_summary(WorkspacePaths(workspace))
    assert summary[0]["goal"] == "Stabilise the schema."


def test_environment_includes_epochs_summary(client: TestClient) -> None:
    """``/api/environment`` carries the per-epoch goal summary list."""
    r = client.get("/api/environment")
    assert r.status_code == 200
    body = r.json()
    assert "epochs" in body, "/api/environment must carry the epochs summary"
    assert isinstance(body["epochs"], list)
    ids = {e["epoch_id"] for e in body["epochs"]}
    assert "2026-05-16_e0" in ids
    for e in body["epochs"]:
        assert "goal" in e, "each epochs-summary row carries a goal field"


# ---------------------------------------------------------------------------
# Epoch experiment log / journal / analysis — new in feat/epoch-experiment-log
# ---------------------------------------------------------------------------


def test_epoch_view_includes_experiments_journal_analysis(workspace: Path) -> None:
    """build_epoch_view now carries experiments, journal, and analysis fields."""
    from zicato.dashboard.state_reader import WorkspacePaths, build_epoch_view

    epoch_id = "2026-05-16_e0"
    epoch_dir = workspace / "epochs" / epoch_id
    gen_dir = epoch_dir / "generations" / "v1"
    patches_dir = gen_dir / "patches"
    patches_dir.mkdir(parents=True, exist_ok=True)

    # Write a patch file for v1.
    _write_json(
        patches_dir / "p_abc.json",
        {
            "id": "p_abc",
            "mutation_id": "m1",
            "op": "replace",
            "rationale": "tighten planner prompt",
            "new_content": "new-content-here",
        },
    )
    # Write journal and analysis markdown.
    _write(epoch_dir / "journal.md", "# Journal\n\n## v1\nRejected.\n")
    _write(epoch_dir / "analysis.md", "# Analysis\n\nTwo experiments.\n")

    view = build_epoch_view(WorkspacePaths(workspace))

    # Experiments: v1 has an experiment.json and one patch.
    assert "experiments" in view
    assert isinstance(view["experiments"], list)
    exp_by_gen = {e["generation_id"]: e for e in view["experiments"]}
    assert "v1" in exp_by_gen
    v1 = exp_by_gen["v1"]
    assert v1["patches"]["m1"]["op"] == "replace"
    assert v1["patches"]["m1"]["new_content"] == "new-content-here"

    # Journal.
    assert "journal" in view
    assert "v1" in view["journal"]

    # Analysis.
    assert "analysis_md" in view
    assert "Two experiments" in view["analysis_md"]
    assert "analysis_html_available" in view
    assert view["analysis_html_available"] is False
    # The inline paper-styled HTML fragment ships alongside the markdown
    # so the Epoch view can render the report as a paper card inline.
    assert "analysis_html_inline" in view
    assert "paper paper-card" in view["analysis_html_inline"]


def test_epoch_view_experiments_empty_without_gens(workspace: Path) -> None:
    """build_epoch_view yields an empty experiments list when no generation dirs exist."""
    from zicato.dashboard.state_reader import WorkspacePaths, build_epoch_view

    # Remove all generation directories so the walker finds nothing.
    epoch_dir = workspace / "epochs" / "2026-05-16_e0"
    import shutil

    gens_dir = epoch_dir / "generations"
    if gens_dir.is_dir():
        shutil.rmtree(gens_dir)

    view = build_epoch_view(WorkspacePaths(workspace))
    assert view["experiments"] == []
    assert view["journal"] == ""
    assert view["analysis_md"] == ""
    assert view["analysis_html_available"] is False
    # No analysis -> no inline fragment (empty string, not missing key).
    assert view["analysis_html_inline"] == ""


def test_epoch_view_analysis_html_available_flag(workspace: Path) -> None:
    """analysis_html_available is True when the HTML file exists."""
    from zicato.dashboard.state_reader import WorkspacePaths, build_epoch_view

    epoch_dir = workspace / "epochs" / "2026-05-16_e0"
    _write(epoch_dir / "analysis.html", "<html><body>report</body></html>")

    view = build_epoch_view(WorkspacePaths(workspace))
    assert view["analysis_html_available"] is True


# ---------------------------------------------------------------------------
# Δscalar aggregates — champion spine vs gross (bug #169)
# ---------------------------------------------------------------------------
#
# The Epoch header's "NET Δscalar" tile used to sum every experiment's
# delta — including the rejected challengers that never enter the
# lineage. That number misframes meta-loop progress: only the promoted
# spine moves the loss forward. The backend now hands the frontend both
# numbers as ``delta_scalar_summary = {champion_spine, gross}``.


def _exp(
    *,
    gen: str,
    parent: str | None,
    decision: str | None,
    delta: float | None,
) -> dict[str, object]:
    """Compact factory for fixture experiment dicts."""
    outcome: dict[str, object] | None = None
    if decision is not None or delta is not None:
        outcome = {}
        if decision is not None:
            outcome["tournament_decision"] = decision
        if delta is not None:
            outcome["scalar_score_delta"] = delta
    rec: dict[str, object] = {
        "generation_id": gen,
        "parent_generation_id": parent,
    }
    if outcome is not None:
        rec["outcome"] = outcome
    return rec


def test_compute_epoch_delta_summary_champion_spine_vs_gross() -> None:
    """The canonical t6-shape fixture pins both numbers (bug #169).

    Chain: ``v0 → v1(promoted,-10) → v2(rejected,+5) → v3(promoted,-15)
    → v4(rejected,+20)``. v0 has no delta (it is the baseline; the
    proposer never ran against it). The champion-spine net is the sum
    across the promoted hops — ``-10 + -15 = -25``. The gross net sums
    every recorded delta, promoted or not — ``-10 + 5 + -15 + 20 = 0``.
    """
    from zicato.dashboard.state_reader import compute_epoch_delta_summary

    experiments = [
        # v0 is the baseline — no outcome, no delta.
        {"generation_id": "v0", "parent_generation_id": None},
        _exp(gen="v1", parent="v0", decision="promoted", delta=-10.0),
        _exp(gen="v2", parent="v1", decision="rejected", delta=5.0),
        _exp(gen="v3", parent="v1", decision="promoted", delta=-15.0),
        _exp(gen="v4", parent="v3", decision="rejected", delta=20.0),
    ]
    summary = compute_epoch_delta_summary(experiments)
    assert summary["champion_spine"] == pytest.approx(-25.0)
    assert summary["gross"] == pytest.approx(0.0)


def test_compute_epoch_delta_summary_t6_run8_shape() -> None:
    """Pin the exact numbers from the bug report (t6 run #8).

    Five experiments, two promoted (v1, v3) and three rejected
    (v2, v4, v5). Champion-spine net = ``-14.429 + -24.331 = -38.760``;
    gross = sum of all five = ``+19.482`` — exactly the discrepancy the
    operator caught on the Epoch header.
    """
    from zicato.dashboard.state_reader import compute_epoch_delta_summary

    experiments = [
        _exp(gen="v1", parent="v0", decision="promoted", delta=-14.429),
        _exp(gen="v2", parent="v1", decision="rejected", delta=10.123),
        _exp(gen="v3", parent="v1", decision="promoted", delta=-24.331),
        _exp(gen="v4", parent="v3", decision="rejected", delta=42.405),
        _exp(gen="v5", parent="v3", decision="rejected", delta=5.714),
    ]
    summary = compute_epoch_delta_summary(experiments)
    assert summary["champion_spine"] == pytest.approx(-38.760)
    assert summary["gross"] == pytest.approx(19.482)


def test_compute_epoch_delta_summary_empty_and_lone_promotion() -> None:
    """No promoted generations -> spine is None; gross still sums.

    A single promotion is the default first-tournament outcome (parent
    → first promoted child) and is *not* yet meta-loop progress: the
    spine reads "—" until a second promotion lands. The gross figure
    still sums every recorded delta — it is the all-experiments view.
    """
    from zicato.dashboard.state_reader import compute_epoch_delta_summary

    # An epoch with no promoted experiments at all.
    only_rejected = [
        _exp(gen="v1", parent="v0", decision="rejected", delta=1.0),
        _exp(gen="v2", parent="v0", decision="rejected", delta=2.0),
    ]
    summary = compute_epoch_delta_summary(only_rejected)
    assert summary["champion_spine"] is None
    assert summary["gross"] == pytest.approx(3.0)

    # A single promoted generation: spine reads "—" (None) per the
    # bug-#169 spec — the meta-loop has not yet chained two promotions.
    one_promoted = [
        _exp(gen="v1", parent="v0", decision="promoted", delta=-7.5),
    ]
    summary = compute_epoch_delta_summary(one_promoted)
    assert summary["champion_spine"] is None
    assert summary["gross"] == pytest.approx(-7.5)

    # Two promoted generations chain into a real spine net.
    two_promoted = [
        _exp(gen="v1", parent="v0", decision="promoted", delta=-7.5),
        _exp(gen="v2", parent="v1", decision="promoted", delta=-2.5),
    ]
    summary = compute_epoch_delta_summary(two_promoted)
    assert summary["champion_spine"] == pytest.approx(-10.0)
    assert summary["gross"] == pytest.approx(-10.0)


def test_compute_epoch_delta_summary_no_deltas_returns_none() -> None:
    """An epoch with no finite deltas yields None for both fields.

    A common in-flight shape: experiments exist but no tournament has
    written an outcome yet. Neither tile should read "0.000" — both
    must read "—" (no comparison possible yet).
    """
    from zicato.dashboard.state_reader import compute_epoch_delta_summary

    experiments = [
        {"generation_id": "v0", "parent_generation_id": None},
        {"generation_id": "v1", "parent_generation_id": "v0"},
    ]
    summary = compute_epoch_delta_summary(experiments)
    assert summary["champion_spine"] is None
    assert summary["gross"] is None


def test_compute_epoch_delta_summary_skips_malformed_entries() -> None:
    """Best-effort: non-dicts, missing ids, non-finite deltas are skipped."""
    from zicato.dashboard.state_reader import compute_epoch_delta_summary

    experiments: list[dict[str, object]] = [
        # Wrong types — silently skipped.
        "not a dict",  # type: ignore[list-item]
        {"generation_id": None, "outcome": {"scalar_score_delta": -1.0}},
        {"generation_id": "vNaN", "outcome": {"scalar_score_delta": float("nan")}},
        {"generation_id": "vInf", "outcome": {"scalar_score_delta": float("inf")}},
        _exp(gen="v1", parent="v0", decision="promoted", delta=-2.0),
        _exp(gen="v2", parent="v1", decision="promoted", delta=-1.0),
    ]
    summary = compute_epoch_delta_summary(experiments)
    assert summary["champion_spine"] == pytest.approx(-3.0)
    assert summary["gross"] == pytest.approx(-3.0)


def test_build_epoch_view_carries_delta_scalar_summary(workspace: Path) -> None:
    """build_epoch_view surfaces the spine/gross aggregates so the SPA
    can render the headline without re-walking experiments client-side.
    """
    from zicato.dashboard.state_reader import WorkspacePaths, build_epoch_view

    epoch_dir = workspace / "epochs" / "2026-05-16_e0"
    # Three generations: one promoted, one rejected, one promoted —
    # spine = -3 + -2 = -5; gross = -3 + 4 + -2 = -1.
    for gid, parent, decision, delta in [
        ("v1", "v0", "promoted", -3.0),
        ("v2", "v1", "rejected", 4.0),
        ("v3", "v1", "promoted", -2.0),
    ]:
        _write_json(
            epoch_dir / "generations" / gid / "experiment.json",
            {
                "generation_id": gid,
                "parent_generation_id": parent,
                "outcome": {
                    "tournament_decision": decision,
                    "scalar_score_delta": delta,
                },
            },
        )

    view = build_epoch_view(WorkspacePaths(workspace))
    summary = view["delta_scalar_summary"]
    assert summary["champion_spine"] == pytest.approx(-5.0)
    assert summary["gross"] == pytest.approx(-1.0)


def test_epoch_journal_endpoint(client: TestClient, workspace: Path) -> None:
    """GET /api/epoch/{id}/journal returns { epoch_id, journal }."""
    epoch_id = "2026-05-16_e0"
    epoch_dir = workspace / "epochs" / epoch_id
    _write(epoch_dir / "journal.md", "# Journal\n\n## v1\nRejected.\n")

    r = client.get(f"/api/epoch/{epoch_id}/journal")
    assert r.status_code == 200
    body = r.json()
    assert body["epoch_id"] == epoch_id
    assert "v1" in body["journal"]


def test_epoch_journal_endpoint_absent(client: TestClient) -> None:
    """Journal endpoint degrades gracefully when journal.md is absent."""
    r = client.get("/api/epoch/2026-05-16_e0/journal")
    assert r.status_code == 200
    body = r.json()
    assert body["epoch_id"] == "2026-05-16_e0"
    assert body["journal"] == ""


def test_epoch_journal_endpoint_invalid_id(client: TestClient) -> None:
    """Journal endpoint rejects unsafe epoch ids."""
    r = client.get("/api/epoch/../secrets/journal")
    assert r.status_code in (400, 404)


def test_epoch_journal_md_endpoint(client: TestClient, workspace: Path) -> None:
    """GET /api/epoch/{id}/journal.md serves journal.md raw as text/markdown.

    The "View raw journal" link on the merged Experiments section points
    at this endpoint so a fresh tab renders the human-readable markdown
    directly — not the JSON envelope ``/journal`` wraps it in.
    """
    epoch_id = "2026-05-16_e0"
    epoch_dir = workspace / "epochs" / epoch_id
    body_text = "# Journal\n\n## v1\nRejected: loss rose.\n"
    _write(epoch_dir / "journal.md", body_text)

    r = client.get(f"/api/epoch/{epoch_id}/journal.md")
    assert r.status_code == 200
    # Body is the raw markdown bytes — no JSON envelope.
    assert r.text == body_text
    # Content-type is text/markdown (not application/json).
    ct = r.headers.get("content-type", "")
    assert ct.startswith("text/markdown"), f"unexpected content-type: {ct!r}"
    # And specifically not the JSON envelope shape — try to parse and
    # fail. (Just to prove we are not serving the wrong shape here.)
    import json as _json

    try:
        _json.loads(r.text)
        raise AssertionError("journal.md endpoint must not return JSON")
    except _json.JSONDecodeError:
        pass


def test_epoch_journal_md_endpoint_absent(client: TestClient) -> None:
    """journal.md endpoint returns 404 when the file is absent.

    Unlike the JSON ``/journal`` endpoint (which degrades to an empty
    string so the SPA can render a "no journal yet" empty state), the
    ``.md`` endpoint is opened in a fresh browser tab by the user, so a
    404 is the right signal — there is nothing to read.
    """
    r = client.get("/api/epoch/2026-05-16_e0/journal.md")
    assert r.status_code == 404


def test_epoch_journal_md_endpoint_invalid_id(client: TestClient) -> None:
    """journal.md endpoint rejects unsafe epoch ids."""
    r = client.get("/api/epoch/../secrets/journal.md")
    assert r.status_code in (400, 404)


def test_epoch_analysis_endpoint(client: TestClient, workspace: Path) -> None:
    """GET /api/epoch/{id}/analysis returns { epoch_id, analysis_md, analysis_html_available }."""
    epoch_id = "2026-05-16_e0"
    epoch_dir = workspace / "epochs" / epoch_id
    _write(epoch_dir / "analysis.md", "# Analysis\n\nTwo experiments.\n")

    r = client.get(f"/api/epoch/{epoch_id}/analysis")
    assert r.status_code == 200
    body = r.json()
    assert body["epoch_id"] == epoch_id
    assert "Two experiments" in body["analysis_md"]
    assert "analysis_html_available" in body
    assert body["analysis_html_available"] is False
    # The endpoint also returns a paper-styled inline HTML fragment so
    # the Epoch view's Analysis section can render the report as a
    # paper card inline (same renderer as the standalone analysis.html).
    assert "analysis_html_inline" in body
    assert "paper paper-card" in body["analysis_html_inline"]
    # Inline fragment must be a fragment (no DOCTYPE), self-contained
    # (carries its own scoped CSS) and free of external resources.
    inline = body["analysis_html_inline"]
    assert not inline.startswith("<!DOCTYPE")
    assert "<style>" in inline
    assert 'href="http' not in inline
    assert 'src="http' not in inline


def test_epoch_analysis_html_endpoint_present(client: TestClient, workspace: Path) -> None:
    """GET /api/epoch/{id}/analysis.html serves the HTML when present."""
    epoch_id = "2026-05-16_e0"
    epoch_dir = workspace / "epochs" / epoch_id
    _write(epoch_dir / "analysis.html", "<html><body>report</body></html>")

    r = client.get(f"/api/epoch/{epoch_id}/analysis.html")
    assert r.status_code == 200
    assert "report" in r.text
    assert "text/html" in r.headers["content-type"]


def test_epoch_analysis_html_endpoint_absent(client: TestClient) -> None:
    """GET /api/epoch/{id}/analysis.html returns 404 when absent."""
    r = client.get("/api/epoch/2026-05-16_e0/analysis.html")
    assert r.status_code == 404


def test_epoch_analysis_html_endpoint_invalid_id(client: TestClient) -> None:
    """Analysis HTML endpoint rejects unsafe epoch ids."""
    r = client.get("/api/epoch/../secrets/analysis.html")
    assert r.status_code in (400, 404)


def test_environment_epoch_includes_new_fields(client: TestClient, workspace: Path) -> None:
    """The consolidated /api/environment carries the new epoch fields."""
    epoch_id = "2026-05-16_e0"
    epoch_dir = workspace / "epochs" / epoch_id
    _write(epoch_dir / "journal.md", "# Journal\n\n## round 1\n")
    _write(epoch_dir / "analysis.md", "# Analysis\n\n## summary\n")

    r = client.get("/api/environment")
    assert r.status_code == 200
    body = r.json()
    epoch = body.get("epoch", {})
    assert "experiments" in epoch, "epoch must include experiments list"
    assert isinstance(epoch["experiments"], list)
    assert "journal" in epoch, "epoch must include journal text"
    assert "Analysis" in epoch.get("analysis_md", "")
    assert "analysis_html_available" in epoch


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


def test_tournament_detail_surfaces_adk_session_ids(workspace: Path) -> None:
    """``ab_grid`` cells carry ``parent_adk_session_id`` / ``child_adk_session_id``
    when the ``loss_json`` column stores a matching ``adk_session_id``."""
    import json as _json

    from zicato.dashboard.state_reader import WorkspacePaths, build_matchup_detail

    # Patch the index to include real adk_session_id in loss_json.
    index_path = workspace / "index.db"
    conn = sqlite3.connect(index_path)
    parent_lj = _json.dumps({"adk_session_id": "session-parent-aaa"})
    child_lj = _json.dumps({"adk_session_id": "session-child-bbb"})
    conn.execute(
        "UPDATE loss_profiles SET loss_json = ?"
        " WHERE generation_id = 'v0' AND entry_id = 'waffles_single'",
        (parent_lj,),
    )
    conn.execute(
        "UPDATE loss_profiles SET loss_json = ?"
        " WHERE generation_id = 'v1' AND entry_id = 'waffles_single'",
        (child_lj,),
    )
    conn.commit()
    conn.close()

    paths = WorkspacePaths(workspace)
    detail = build_matchup_detail(paths, "v1")
    assert len(detail["ab_grid"]) == 1
    cell = detail["ab_grid"][0]
    assert cell.get("parent_adk_session_id") == "session-parent-aaa"
    assert cell.get("child_adk_session_id") == "session-child-bbb"


def test_tournament_detail_ab_grid_omits_absent_adk_session_id(workspace: Path) -> None:
    """An ``ab_grid`` cell without a valid ``adk_session_id`` in ``loss_json``
    simply omits the key — the cell is not malformed."""
    from zicato.dashboard.state_reader import WorkspacePaths, build_matchup_detail

    # The fixture index has loss_json = '{}' — no adk_session_id.
    paths = WorkspacePaths(workspace)
    detail = build_matchup_detail(paths, "v1")
    assert len(detail["ab_grid"]) == 1
    cell = detail["ab_grid"][0]
    assert "parent_adk_session_id" not in cell
    assert "child_adk_session_id" not in cell


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
# /api/matchup-grid — completed-tournament per-entry outcomes read from the
# persisted per-run loss.json files (NOT the SQLite index).
# ---------------------------------------------------------------------------


def _seed_loss_files(workspace: Path) -> None:
    """Write per-run ``loss.json`` + ``gen_score.json`` for v0 and v1.

    Mirrors the on-disk layout a real run produces under
    ``epochs/{id}/generations/{gen}/runs/{entry}/loss.json``. The
    champion (v0) and challenger (v1) each ran two board entries; on
    ``extract_invoice`` the challenger improved, on ``schema_response``
    it regressed — a clear mixed outcome.
    """
    epoch_id = "2026-05-16_e0"
    gens = epoch_id  # readability alias
    layout: dict[str, dict[str, dict[str, object]]] = {
        "v0": {
            "extract_invoice": {"drift_loss": 0.30, "pass_fail": True},
            "schema_response": {"drift_loss": 0.12, "pass_fail": True},
        },
        "v1": {
            "extract_invoice": {"drift_loss": 0.21, "pass_fail": True},
            "schema_response": {"drift_loss": 0.34, "pass_fail": False},
        },
    }
    for gen, entries in layout.items():
        for entry, fields in entries.items():
            loss = {
                "run_id": f"{gen}--{entry}",
                "entry_id": entry,
                "generation_id": gen,
                "epoch_id": gens,
                "drift_counts": [],
                "plan_revisions": 0,
                "task_failure_ratio": 0.0,
                "runtime_ms": 1000,
                "wall_clock_budget_exceeded": False,
                "expectation_result": None,
                "adk_session_id": f"session-{gen}-{entry}",
                **fields,
            }
            _write_json(
                workspace
                / "epochs"
                / epoch_id
                / "generations"
                / gen
                / "runs"
                / entry
                / "loss.json",
                loss,
            )
    # gen_score.json aggregates — the orchestrator's cached per-generation
    # score (drift mean + scalar + the per-component composition).
    _write_json(
        workspace / "epochs" / epoch_id / "generations" / "v0" / "gen_score.json",
        {
            "generation_id": "v0",
            "drift_loss_mean": 0.21,
            "pass_rate": 1.0,
            "scalar": 0.21,
            "scalar_components": {"drift": 0.21, "pass": 0.0},
        },
    )
    _write_json(
        workspace / "epochs" / epoch_id / "generations" / "v1" / "gen_score.json",
        {
            "generation_id": "v1",
            "drift_loss_mean": 0.275,
            "pass_rate": 0.5,
            "scalar": 0.375,
            "scalar_components": {"drift": 0.275, "pass": 0.10},
        },
    )


def test_matchup_grid_reads_persisted_loss_files(workspace: Path) -> None:
    """``/api/matchup-grid`` reconstructs the per-entry A/B grid from
    the on-disk ``loss.json`` files — no SQLite index involved."""
    from zicato.dashboard.state_reader import WorkspacePaths, build_matchup_grid

    _seed_loss_files(workspace)
    paths = WorkspacePaths(workspace)
    grid = build_matchup_grid(paths, "2026-05-16_e0", "v0", "v1")

    assert grid["champion"] == "v0"
    assert grid["challenger"] == "v1"
    assert grid["source"] == "loss_files"
    rows = {r["entry_id"]: r for r in grid["entry_grid"]}
    assert set(rows) == {"extract_invoice", "schema_response"}

    # extract_invoice: champion 0.30 -> challenger 0.21 — an improvement.
    win = rows["extract_invoice"]
    assert win["parent_drift_loss"] == 0.30
    assert win["child_drift_loss"] == 0.21
    assert win["parent_pass"] is True and win["child_pass"] is True
    assert win["delta"] == pytest.approx(-0.09)
    assert win["verdict"] == "improved"
    assert win["won_by"] == "v1"

    # schema_response: champion 0.12 -> challenger 0.34 — a regression.
    lose = rows["schema_response"]
    assert lose["delta"] == pytest.approx(0.22)
    assert lose["verdict"] == "regressed"
    assert lose["won_by"] == "v0"
    assert lose["child_pass"] is False

    # The per-run adk_session_id surfaces for the harmonograf jump-offs.
    assert win["parent_session_id"] == "session-v0-extract_invoice"
    assert win["child_session_id"] == "session-v1-extract_invoice"


def test_matchup_grid_scalar_breakdown_from_gen_scores(workspace: Path) -> None:
    """The scalar block composes from the two ``gen_score.json`` aggregates;
    ``components`` is the challenger-minus-champion delta of each term."""
    from zicato.dashboard.state_reader import WorkspacePaths, build_matchup_grid

    _seed_loss_files(workspace)
    paths = WorkspacePaths(workspace)
    grid = build_matchup_grid(paths, "2026-05-16_e0", "v0", "v1")

    scalar = grid["scalar"]
    assert scalar is not None
    assert scalar["parent"] == pytest.approx(0.21)
    assert scalar["child"] == pytest.approx(0.375)
    assert scalar["delta"] == pytest.approx(0.165)
    # drift: 0.275 - 0.21 = +0.065 ; pass: 0.10 - 0.0 = +0.10
    assert scalar["components"]["drift"] == pytest.approx(0.065)
    assert scalar["components"]["pass"] == pytest.approx(0.10)


def _seed_scored_loss_files(workspace: Path) -> None:
    """Write loss.json carrying the continuous ``score`` + ``metrics`` (#18).

    The champion (v0) and challenger (v1) each ran one SCORED entry
    (``extract_invoice`` with a continuous score + precision/recall
    decomposition) and one BOOL-ONLY entry (``schema_response`` with no
    ``score`` / ``metrics`` — the back-compat path). gen_score.json
    carries a per-generation ``mean_score`` for v0 and v1.
    """
    epoch_id = "2026-05-16_e0"
    layout: dict[str, dict[str, dict[str, object]]] = {
        "v0": {
            "extract_invoice": {
                "drift_loss": 0.30,
                "pass_fail": True,
                "score": 0.62,
                "metrics": {"precision": 0.70, "recall": 0.55},
            },
            # bool-only entry: no score / metrics — renders by pass bit.
            "schema_response": {"drift_loss": 0.12, "pass_fail": True},
        },
        "v1": {
            "extract_invoice": {
                "drift_loss": 0.21,
                "pass_fail": True,
                "score": 0.81,
                "metrics": {"precision": 0.88, "recall": 0.74},
            },
            "schema_response": {"drift_loss": 0.34, "pass_fail": False},
        },
    }
    for gen, entries in layout.items():
        for entry, fields in entries.items():
            loss = {
                "run_id": f"{gen}--{entry}",
                "entry_id": entry,
                "generation_id": gen,
                "epoch_id": epoch_id,
                "drift_counts": [],
                "runtime_ms": 1000,
                "wall_clock_budget_exceeded": False,
                "adk_session_id": f"session-{gen}-{entry}",
                **fields,
            }
            _write_json(
                workspace
                / "epochs"
                / epoch_id
                / "generations"
                / gen
                / "runs"
                / entry
                / "loss.json",
                loss,
            )
    _write_json(
        workspace / "epochs" / epoch_id / "generations" / "v0" / "gen_score.json",
        {"generation_id": "v0", "scalar": 0.21, "mean_score": 0.62},
    )
    _write_json(
        workspace / "epochs" / epoch_id / "generations" / "v1" / "gen_score.json",
        {"generation_id": "v1", "scalar": 0.375, "mean_score": 0.81},
    )


def test_matchup_grid_surfaces_continuous_score_and_metrics(workspace: Path) -> None:
    """A scored entry carries ``parent_score`` / ``child_score`` plus the
    ``parent_metrics`` / ``child_metrics`` precision/recall decomposition;
    a bool-only entry carries ``None`` for all four (back-compat)."""
    from zicato.dashboard.state_reader import WorkspacePaths, build_matchup_grid

    _seed_scored_loss_files(workspace)
    paths = WorkspacePaths(workspace)
    grid = build_matchup_grid(paths, "2026-05-16_e0", "v0", "v1")
    rows = {r["entry_id"]: r for r in grid["entry_grid"]}

    scored = rows["extract_invoice"]
    assert scored["parent_score"] == pytest.approx(0.62)
    assert scored["child_score"] == pytest.approx(0.81)
    assert scored["parent_metrics"] == {"precision": 0.70, "recall": 0.55}
    assert scored["child_metrics"] == {"precision": 0.88, "recall": 0.74}

    # Bool-only entry: no score / metrics — every score field is None so
    # the view degrades to the existing pass/fail glyph.
    boolean = rows["schema_response"]
    assert boolean["parent_score"] is None
    assert boolean["child_score"] is None
    assert boolean["parent_metrics"] is None
    assert boolean["child_metrics"] is None
    assert boolean["parent_pass"] is True
    assert boolean["child_pass"] is False


def test_matchup_grid_scalar_carries_mean_score(workspace: Path) -> None:
    """The scalar block carries a per-generation ``mean_score`` summary
    (parent / child / delta) read from gen_score.json — never recomputed."""
    from zicato.dashboard.state_reader import WorkspacePaths, build_matchup_grid

    _seed_scored_loss_files(workspace)
    paths = WorkspacePaths(workspace)
    grid = build_matchup_grid(paths, "2026-05-16_e0", "v0", "v1")

    ms = grid["scalar"]["mean_score"]
    assert ms["parent"] == pytest.approx(0.62)
    assert ms["child"] == pytest.approx(0.81)
    assert ms["delta"] == pytest.approx(0.19)


def test_matchup_grid_no_mean_score_when_absent(workspace: Path) -> None:
    """Back-compat: a gen_score.json without ``mean_score`` yields a
    scalar block with no ``mean_score`` key (degrades to today's view)."""
    from zicato.dashboard.state_reader import WorkspacePaths, build_matchup_grid

    _seed_loss_files(workspace)  # the pre-score seeder — no mean_score
    paths = WorkspacePaths(workspace)
    grid = build_matchup_grid(paths, "2026-05-16_e0", "v0", "v1")
    assert grid["scalar"] is not None
    assert "mean_score" not in grid["scalar"]
    # And the entry rows carry score/metrics == None (bool-only path).
    for row in grid["entry_grid"]:
        assert row["parent_score"] is None
        assert row["child_score"] is None
        assert row["parent_metrics"] is None
        assert row["child_metrics"] is None


def _build_per_entry_index(db: Path, loss_json_by_run: dict[str, str]) -> None:
    """Build a loss_profiles index with the full column set the per-entry
    reader queries, one v1 row per ``loss_json_by_run`` entry."""
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE loss_profiles(run_id TEXT, epoch_id TEXT, generation_id TEXT, "
        "entry_id TEXT, drift_loss REAL, pass_fail TEXT, runtime_ms INTEGER, "
        "wall_clock_budget_exceeded INTEGER, loss_json TEXT, tournament_id TEXT)"
    )
    conn.executemany(
        "INSERT INTO loss_profiles VALUES(?,?,?,?,?,?,?,?,?,?)",
        [
            (
                run,
                "2026-05-16_e0",
                "v1",
                "waffles_single",
                0.2,
                "pass",
                100,
                0,
                lj,
                None,
            )
            for run, lj in loss_json_by_run.items()
        ],
    )
    conn.commit()
    conn.close()


def test_per_entry_for_generation_carries_score_from_loss_json(tmp_path: Path) -> None:
    """``build_per_entry_for_generation`` parses the continuous ``score`` +
    ``metrics`` out of the index's ``loss_json`` blob (no schema change),
    and surfaces a per-generation ``mean_score`` from gen_score.json."""
    import json as _json

    from zicato.dashboard.state_reader import (
        WorkspacePaths,
        build_per_entry_for_generation,
    )

    ws = tmp_path / ".zicato"
    ws.mkdir()
    _build_per_entry_index(
        ws / "index.db",
        {"r1": _json.dumps({"score": 0.77, "metrics": {"precision": 0.9, "recall": 0.6}})},
    )
    _write_json(
        ws / "epochs" / "2026-05-16_e0" / "generations" / "v1" / "gen_score.json",
        {"generation_id": "v1", "mean_score": 0.77},
    )

    paths = WorkspacePaths(ws)
    pe = build_per_entry_for_generation(paths, "2026-05-16_e0", "v1")
    assert pe["mean_score"] == pytest.approx(0.77)
    entry = next(e for e in pe["entries"] if e["entry_id"] == "waffles_single")
    assert entry["score"] == pytest.approx(0.77)
    assert entry["metrics"] == {"precision": 0.9, "recall": 0.6}


def test_per_entry_for_generation_back_compat_no_score(tmp_path: Path) -> None:
    """An index whose ``loss_json`` is ``{}`` (the pre-score default)
    yields ``score`` / ``metrics`` == None and ``mean_score`` == None."""
    from zicato.dashboard.state_reader import (
        WorkspacePaths,
        build_per_entry_for_generation,
    )

    ws = tmp_path / ".zicato"
    ws.mkdir()
    _build_per_entry_index(ws / "index.db", {"r1": "{}"})

    paths = WorkspacePaths(ws)
    pe = build_per_entry_for_generation(paths, "2026-05-16_e0", "v1")
    assert pe["mean_score"] is None
    entry = next(e for e in pe["entries"] if e["entry_id"] == "waffles_single")
    assert entry["score"] is None
    assert entry["metrics"] is None


def test_matchup_grid_endpoint(client: TestClient, workspace: Path) -> None:
    """The ``/api/matchup-grid/{epoch}/{champion}/{challenger}`` route
    serves the persisted-loss-file grid as JSON."""
    _seed_loss_files(workspace)
    r = client.get("/api/matchup-grid/2026-05-16_e0/v0/v1")
    assert r.status_code == 200
    body = r.json()
    assert body["source"] == "loss_files"
    assert len(body["entry_grid"]) == 2
    assert body["scalar"]["delta"] == pytest.approx(0.165)


def test_matchup_grid_endpoint_no_files(client: TestClient) -> None:
    """A matchup with no persisted loss files degrades to an empty grid
    (HTTP 200) — not a 500."""
    r = client.get("/api/matchup-grid/2026-05-16_e0/v0/v1")
    assert r.status_code == 200
    body = r.json()
    assert body["entry_grid"] == []
    assert body["scalar"] is None


def test_matchup_grid_endpoint_invalid_id(client: TestClient) -> None:
    """A malformed coordinate degrades to an empty grid rather than 500."""
    r = client.get("/api/matchup-grid/2026-05-16_e0/bad%20id/v1")
    assert r.status_code == 200
    body = r.json()
    assert body["entry_grid"] == []
    assert body["scalar"] is None


def test_matchup_grid_one_sided(workspace: Path) -> None:
    """An entry that ran on only one side still appears, with the missing
    side's loss reported as ``null``."""
    from zicato.dashboard.state_reader import WorkspacePaths, build_matchup_grid

    _seed_loss_files(workspace)
    # The challenger ran an extra entry the champion never did.
    _write_json(
        workspace
        / "epochs"
        / "2026-05-16_e0"
        / "generations"
        / "v1"
        / "runs"
        / "extra_entry"
        / "loss.json",
        {
            "run_id": "v1--extra_entry",
            "entry_id": "extra_entry",
            "generation_id": "v1",
            "epoch_id": "2026-05-16_e0",
            "drift_counts": [],
            "plan_revisions": 0,
            "task_failure_ratio": 0.0,
            "runtime_ms": 1000,
            "wall_clock_budget_exceeded": False,
            "expectation_result": None,
            "drift_loss": 0.4,
            "pass_fail": False,
        },
    )
    paths = WorkspacePaths(workspace)
    grid = build_matchup_grid(paths, "2026-05-16_e0", "v0", "v1")
    rows = {r["entry_id"]: r for r in grid["entry_grid"]}
    assert "extra_entry" in rows
    assert rows["extra_entry"]["parent_drift_loss"] is None
    assert rows["extra_entry"]["child_drift_loss"] == 0.4
    assert rows["extra_entry"]["verdict"] == "flat"
    assert rows["extra_entry"]["won_by"] is None


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
    # index.html references app_T.js at the document root.
    r = client.get("/app_T.js")
    assert r.status_code == 200
    assert "// app" in r.text


def test_static_asset_under_static_prefix(client: TestClient) -> None:
    r = client.get("/static/app_T.js")
    assert r.status_code == 200


def test_unknown_static_is_404(client: TestClient) -> None:
    r = client.get("/does-not-exist.css")
    assert r.status_code == 404


def test_static_asset_carries_etag_validator(client: TestClient) -> None:
    # A served asset keeps `no-cache` but now carries an ETag/Last-Modified
    # validator so the browser can revalidate cheaply instead of re-downloading.
    r = client.get("/static/app_T.js")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-cache"
    assert r.headers.get("etag")
    assert r.headers.get("last-modified")


def test_static_revalidation_returns_304(client: TestClient) -> None:
    first = client.get("/static/app_T.js")
    etag = first.headers["etag"]
    second = client.get("/static/app_T.js", headers={"If-None-Match": etag})
    assert second.status_code == 304
    assert second.content == b""  # a 304 carries no body — no re-download
    assert second.headers["etag"] == etag


def test_static_mismatched_etag_returns_fresh_200(client: TestClient) -> None:
    r = client.get("/static/app_T.js", headers={"If-None-Match": '"deadbeef-1"'})
    assert r.status_code == 200
    assert "// app" in r.text


def test_static_etag_changes_when_file_edited(client: TestClient, static_dir: Path) -> None:
    before = client.get("/static/app_T.js").headers["etag"]
    # An edit (changed size) must change the ETag so the browser refetches.
    (static_dir / "app_T.js").write_text("// app — edited longer", encoding="utf-8")
    after = client.get("/static/app_T.js")
    assert after.status_code == 200
    assert after.headers["etag"] != before
    # The stale ETag no longer matches → a fresh 200, not a 304.
    assert client.get("/static/app_T.js", headers={"If-None-Match": before}).status_code == 200


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


def test_control_resume_deletes_pause_flag(rw_client: TestClient, workspace: Path) -> None:
    """Resume atomically unlinks the pause flag; a second resume is a no-op."""
    assert rw_client.post("/api/control/pause", json={"reason": "hold"}).status_code == 202
    marker = workspace / "runtime" / "control" / "pause_epoch"
    assert marker.exists()

    r = rw_client.post("/api/control/resume")
    assert r.status_code == 202
    body = r.json()
    assert body["accepted"] is True
    assert body["removed"] is True
    assert not marker.exists()

    # Idempotent: resuming an unpaused workspace is an accepted no-op.
    r = rw_client.post("/api/control/resume")
    assert r.status_code == 202
    assert r.json()["removed"] is False


def test_control_resume_forbidden_when_read_only(client: TestClient, workspace: Path) -> None:
    """A read-only dashboard must answer 403 and leave the flag intact."""
    marker = workspace / "runtime" / "control" / "pause_epoch"
    marker.write_text("{}", encoding="utf-8")
    r = client.post("/api/control/resume")
    assert r.status_code == 403
    assert marker.exists()


def test_paused_flag_surfaces_in_runtime_payloads(rw_client: TestClient, workspace: Path) -> None:
    """Pause-flag presence rides /api/state (top-level) + the heartbeat dict."""
    # Unpaused baseline.
    snap = rw_client.get("/api/state").json()
    assert snap["paused"] is False
    assert snap["heartbeat"]["paused"] is False
    hb = rw_client.get("/api/heartbeat").json()
    assert hb["paused"] is False

    # Paused: the flag flips both reads; resume flips them back.
    assert rw_client.post("/api/control/pause").status_code == 202
    snap = rw_client.get("/api/state").json()
    assert snap["paused"] is True
    assert snap["heartbeat"]["paused"] is True
    assert rw_client.post("/api/control/resume").status_code == 202
    assert rw_client.get("/api/state").json()["paused"] is False


def test_control_kill_writes_per_run_marker(rw_client: TestClient, workspace: Path) -> None:
    r = rw_client.post("/api/control/kill/waffles_single")
    assert r.status_code == 202
    assert (workspace / "runtime" / "control" / "kill_runs" / "waffles_single").exists()


def test_control_promote_and_reject(rw_client: TestClient, workspace: Path) -> None:
    assert rw_client.post("/api/control/promote/v1").status_code == 202
    assert rw_client.post("/api/control/reject/v1").status_code == 202
    assert (workspace / "runtime" / "control" / "promote" / "v1").exists()
    assert (workspace / "runtime" / "control" / "reject" / "v1").exists()


def test_control_promote_records_field_provenance(rw_client: TestClient, workspace: Path) -> None:
    """A field promote's body carries epoch/tournament_id/structure/reason."""
    import json as _json

    body = {
        "reason": "operator advances the diverse candidate",
        "epoch": "e2",
        "tournament_id": "tourn_e2_v3",
        "structure": "racing",
    }
    r = rw_client.post("/api/control/promote/v3", json=body)
    assert r.status_code == 202
    written = _json.loads((workspace / "runtime" / "control" / "promote" / "v3").read_text())
    assert written["generation_id"] == "v3"
    assert written["reason"] == "operator advances the diverse candidate"
    assert written["epoch"] == "e2"
    assert written["tournament_id"] == "tourn_e2_v3"
    assert written["structure"] == "racing"


def test_control_reject_empty_body_is_back_compat(rw_client: TestClient, workspace: Path) -> None:
    """A reason-less reject still writes a valid control file (no extra keys)."""
    import json as _json

    assert rw_client.post("/api/control/reject/v1").status_code == 202
    written = _json.loads((workspace / "runtime" / "control" / "reject" / "v1").read_text())
    assert written["generation_id"] == "v1"
    assert "epoch" not in written
    assert "structure" not in written


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
async def test_sse_frames_carry_progress_seq_and_terminal(workspace: Path) -> None:
    """RUNTIME-V2 Phase 4: snapshot + state_change frames carry ``seq`` /
    ``terminal`` — the orchestrator's true liveness cursor.
    """
    import asyncio

    from zicato.dashboard.sse import ChangeBroker, sse_event_stream
    from zicato.dashboard.state_reader import WorkspacePaths
    from zicato.runtime import progress_log

    # Seed the progress log: two genuine transitions then a terminal marker.
    progress_log.append_progress(workspace, progress_log.LOOP_START)
    progress_log.append_progress(workspace, progress_log.ROUND_START)
    progress_log.append_progress(workspace, progress_log.SETTLED)

    paths = WorkspacePaths(workspace)
    broker = ChangeBroker(paths)
    await broker.start()
    try:
        stream = sse_event_stream(broker, paths)
        first = await asyncio.wait_for(stream.__anext__(), timeout=5)
        assert first.startswith("event: snapshot")
        snap_data = next(ln for ln in first.splitlines() if ln.startswith("data: "))
        snap = json.loads(snap_data[len("data: ") :])
        # The opening snapshot frame carries the live cursor at the top level.
        assert snap["seq"] == 3
        assert snap["terminal"] is True
        # The heartbeat inside the snapshot also carries seq (round-tripped
        # from the on-disk heartbeat; the fixture's legacy file reads 0).
        assert snap["data"]["heartbeat"]["seq"] == 0

        # Mutate a runtime file to drive a state_change frame.
        (workspace / "runtime" / "lock.json").write_text(
            json.dumps({"pid": 1, "instance_id": "x"}), encoding="utf-8"
        )
        change = await asyncio.wait_for(stream.__anext__(), timeout=8)
        if change.startswith(": ping"):
            change = await asyncio.wait_for(stream.__anext__(), timeout=8)
        assert change.startswith("event: state_change")
        ch_data = next(ln for ln in change.splitlines() if ln.startswith("data: "))
        ch = json.loads(ch_data[len("data: ") :])
        assert ch["seq"] == 3
        assert ch["terminal"] is True
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


def test_run_transcript_resolves_inflight_run_without_loss_json(
    workspace: Path, static_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An IN-FLIGHT run's transcript resolves from its still-growing events file.

    The workspace fixture has v1/waffles_single mid-run: an active_runs
    record points at a growing ``events.jsonl`` and NO ``loss.json`` has
    been written yet (the reducer writes it only after scoring). The
    by-(epoch, gen, entry) transcript route must resolve straight to that
    ``generations/v1/runs/waffles_single/events.jsonl`` — the deterministic
    triple, no loss.json round-trip — so the dashboard can read the partial
    transcript of a candidate that is still running. This is the backend
    half of the live-transcript feature.
    """
    _install_stub_transcript(monkeypatch)
    epoch_id = "2026-05-16_e0"
    loss = (
        workspace
        / "epochs"
        / epoch_id
        / "generations"
        / "v1"
        / "runs"
        / "waffles_single"
        / "loss.json"
    )
    assert not loss.exists(), "precondition: the in-flight run has NO loss.json yet"

    app = create_app(workspace, static_dir, read_only=True)
    with TestClient(app) as c:
        # Resolve by the deterministic (epoch, gen, entry) triple — the path
        # the board view's resolveTranscript() takes for a running candidate.
        r = c.get(f"/api/run/{epoch_id}/v1/waffles_single/transcript")
        assert r.status_code == 200, r.text
        body = r.json()
        # The growing events.jsonl resolved (3 lines in the fixture) — the
        # in-flight transcript is served WITHOUT a loss.json.
        assert body["event_count"] == 3, body
        assert body["epoch_id"] == epoch_id
        assert body["generation_id"] == "v1"
        assert body["entry_id"] == "waffles_single"


def test_conversation_resolves_by_run_id_not_dir_name(
    workspace: Path, static_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``/api/conversation/{run_id}`` resolves the canonical board layout.

    Regression for "transcripts were stored but unreadable": board runs
    live at ``generations/<gen>/runs/<entry_id>/events.jsonl`` — the run
    DIRECTORY is named by ENTRY id, while the run id only appears in the
    ``runId`` field inside the events. The resolver previously looked for
    a directory named ``<run_id>`` and (with no ``active_runs`` entry for
    a completed run) returned 404 even though the events were on disk.

    Here the entry dir is ``every_expectation_kind_demo`` and the run id
    is the opaque hex ``033aa0f652ab4c9090cbe3a7a09a01c9`` carried inside
    every event line; no ``active_runs/<run_id>.json`` exists.
    """
    _install_stub_transcript(monkeypatch)
    run_id = "033aa0f652ab4c9090cbe3a7a09a01c9"
    entry_id = "every_expectation_kind_demo"
    events = (
        workspace
        / "epochs"
        / "2026-05-16_e0"
        / "generations"
        / "v3"
        / "runs"
        / entry_id
        / "events.jsonl"
    )
    _write(
        events,
        "\n".join(
            [
                json.dumps(
                    {
                        "emittedAt": "2026-05-16T05:00:01Z",
                        "eventId": f"{run_id}:0:a",
                        "runId": run_id,
                        "sessionId": run_id,
                        "sequence": "0",
                        "conversationStarted": {"conversationId": "c-x"},
                    }
                ),
                json.dumps(
                    {
                        "emittedAt": "2026-05-16T05:00:02Z",
                        "eventId": f"{run_id}:1:b",
                        "runId": run_id,
                        "sequence": "1",
                        "runStarted": {"goalSummary": "Demo every expectation kind."},
                    }
                ),
            ]
        )
        + "\n",
    )

    app = create_app(workspace, static_dir, read_only=True)
    with TestClient(app) as c:
        # Resolving by the opaque run id returns the stored transcript.
        r = c.get(f"/api/conversation/{run_id}")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["event_count"] == 2
        # The by-(epoch, gen, entry) transcript route resolves the same
        # events via the entry-id directory name.
        r2 = c.get(f"/api/run/2026-05-16_e0/v3/{entry_id}/transcript")
        assert r2.status_code == 200, r2.text
        assert r2.json()["event_count"] == 2


def test_conversation_reuse_run_id_falls_back_to_gen_entry_events(
    workspace: Path, static_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A successive-halving REUSE run_id resolves to its gen×entry transcript.

    In racing the fixed champion (v0) is re-raced across rungs, so the
    same gen×entry yields multiple per-rung run records — only one rung
    actually executed and emitted ``events.jsonl``; the rest are
    score-reuse records carrying a distinct ``run_id`` written into a
    ``runs/<entry>/loss.json`` but with NO transcript of their own. The
    reuse ``run_id`` must fall back to the gen×entry events file.

    The fixture's ``v0/runs/waffles_single/`` already carries a real
    ``events.jsonl``. We add a ``loss.json`` there stamping a distinct
    reuse run id; resolving that reuse run id must land on the real
    events file (whose own runId is absent / different).
    """
    _install_stub_transcript(monkeypatch)
    reuse_run_id = "reuse_rung2_v0_waffles_0000"
    _write_json(
        workspace
        / "epochs"
        / "2026-05-16_e0"
        / "generations"
        / "v0"
        / "runs"
        / "waffles_single"
        / "loss.json",
        {"run_id": reuse_run_id, "entry_id": "waffles_single", "drift_loss": 60.5, "pass_fail": 0},
    )
    app = create_app(workspace, static_dir, read_only=True)
    with TestClient(app) as c:
        # The reuse run id has no events.jsonl of its own, but the loss.json
        # in v0/runs/waffles_single points at the real gen×entry transcript.
        r = c.get(f"/api/conversation/{reuse_run_id}")
        assert r.status_code == 200, r.text
        body = r.json()
        # The champion v0 events file is the single-line fixture transcript.
        assert body["event_count"] == 1


def test_conversation_query_gen_entry_fallback_resolves_transcript(
    workspace: Path, static_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``/api/conversation/{run_id}?gen=&entry=`` falls back to gen×entry.

    The explicit, index-independent recovery path the dashboard's
    champion side uses: a run_id with no resolvable events, plus the
    candidate's known gen + entry, resolves directly to the gen×entry
    ``events.jsonl``.
    """
    _install_stub_transcript(monkeypatch)
    app = create_app(workspace, static_dir, read_only=True)
    with TestClient(app) as c:
        r = c.get(
            "/api/conversation/no_such_reuse_run",
            params={"gen": "v0", "entry": "waffles_single"},
        )
        assert r.status_code == 200, r.text
        assert r.json()["event_count"] == 1


def test_conversation_genuinely_absent_gen_entry_still_404(
    workspace: Path, static_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run_id with no events AND no real gen×entry stays a 404.

    The honest "unavailable" case must survive the fallback — a
    gen×entry that does not exist on disk does not fabricate a transcript.
    """
    _install_stub_transcript(monkeypatch)
    app = create_app(workspace, static_dir, read_only=True)
    with TestClient(app) as c:
        r = c.get(
            "/api/conversation/no_such_run",
            params={"gen": "v0", "entry": "no_such_entry"},
        )
        assert r.status_code == 404


def test_run_transcript_gen_entry_primary_resolves_reuse_run_id_pair(
    workspace: Path, static_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The (epoch, gen, entry) route is PRIMARY: it resolves the gen×entry's
    own events.jsonl directly, independent of how any run_id was minted.

    The champion v0×waffles_single pair's per-entry run record is a
    successive-halving REUSE record (a ``loss.json`` carrying a distinct
    ``run_id`` with no events of its own). Resolving the transcript by the
    deterministic triple must still land on the one real
    ``v0/runs/waffles_single/events.jsonl`` (the 1-line fixture transcript),
    never 404.
    """
    _install_stub_transcript(monkeypatch)
    _write_json(
        workspace
        / "epochs"
        / "2026-05-16_e0"
        / "generations"
        / "v0"
        / "runs"
        / "waffles_single"
        / "loss.json",
        {"run_id": "reuse_rung2_v0_0000", "entry_id": "waffles_single", "drift_loss": 60.5},
    )
    app = create_app(workspace, static_dir, read_only=True)
    with TestClient(app) as c:
        r = c.get("/api/run/2026-05-16_e0/v0/waffles_single/transcript")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["event_count"] == 1
        assert body["generation_id"] == "v0"
        assert body["entry_id"] == "waffles_single"


def test_run_transcript_run_disambiguator_selects_specific_rung(
    workspace: Path, static_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``?run=`` disambiguates when a gen×entry has MULTIPLE runs (re-races).

    Racing re-races a gen×entry across rungs; each rung lands in its own
    nested sub-directory under ``runs/<entry>/``. The default resolves to
    the entry's own ``runs/<entry>/events.jsonl``; ``?run=<rung-run-id>``
    selects the nested rung whose loss.json carries that run id.
    """
    _install_stub_transcript(monkeypatch)
    base = workspace / "epochs" / "2026-05-16_e0" / "generations" / "v1" / "runs" / "waffles_single"
    # A nested rung directory with its OWN events.jsonl (2 lines) + a
    # loss.json stamping the rung's run_id. The entry's own events.jsonl
    # already exists in the fixture (3 lines).
    rung = base / "rung3"
    _write(
        rung / "events.jsonl",
        "\n".join(
            [
                json.dumps({"runId": "rung3_run", "sequence": "0", "runStarted": {}}),
                json.dumps({"runId": "rung3_run", "sequence": "1", "steeringDecisionMade": {}}),
            ]
        )
        + "\n",
    )
    _write_json(rung / "loss.json", {"run_id": "rung3_run", "entry_id": "waffles_single"})
    app = create_app(workspace, static_dir, read_only=True)
    with TestClient(app) as c:
        # Default → the entry's own 3-line events.jsonl.
        r = c.get("/api/run/2026-05-16_e0/v1/waffles_single/transcript")
        assert r.status_code == 200, r.text
        assert r.json()["event_count"] == 3
        # Disambiguated → the nested rung's 2-line events.jsonl.
        r2 = c.get("/api/run/2026-05-16_e0/v1/waffles_single/transcript?run=rung3_run")
        assert r2.status_code == 200, r2.text
        assert r2.json()["event_count"] == 2


def test_run_transcript_genuinely_absent_pair_is_honest_empty(
    workspace: Path, static_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A genuinely-absent gen×entry returns the honest empty (no fabrication).

    The triple resolves to nothing when no events.jsonl exists for the pair —
    the route answers 200 with zero turns (the frontend renders the honest
    "could not be reconstructed" message) and never borrows a sibling's
    transcript.
    """
    _install_stub_transcript(monkeypatch)
    app = create_app(workspace, static_dir, read_only=True)
    with TestClient(app) as c:
        r = c.get("/api/run/2026-05-16_e0/v1/no_such_entry/transcript")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["event_count"] == 0
        assert body["turns"] == []
        # The conversation route 404s for a run with no events AND no real
        # gen×entry — the honest hard-absence on the back-compat path.
        r2 = c.get(
            "/api/conversation/no_such_run",
            params={"gen": "v1", "entry": "no_such_entry"},
        )
        assert r2.status_code == 404


def test_conversation_gen_entry_primary_prefers_triple_over_run_id(
    workspace: Path, static_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``/api/conversation`` is gen×entry-FIRST when coordinates are known.

    Even when the opaque run_id is unresolvable, supplying ``?gen=&entry=``
    resolves straight to the gen×entry events.jsonl — the deterministic
    triple is the primary key, the run_id only a disambiguator.
    """
    _install_stub_transcript(monkeypatch)
    app = create_app(workspace, static_dir, read_only=True)
    with TestClient(app) as c:
        r = c.get(
            "/api/conversation/totally_unknown_run",
            params={"gen": "v0", "entry": "waffles_single"},
        )
        assert r.status_code == 200, r.text
        # v0/runs/waffles_single/events.jsonl is the 1-line fixture.
        assert r.json()["event_count"] == 1


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
            # The result block is part of the contract: present (None
            # when no sibling loss.json) on every side.
            assert "result" in body[side]
        assert body["challenger"]["transcript"]["event_count"] == 3


def test_matchup_conversations_carries_loss_json_result_block(
    workspace: Path, static_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A sibling ``loss.json`` projects into the side's ``result`` block.

    Regression for the "v1 waffles_single timed out at 180s but the
    dashboard showed an empty in-progress column" bug: the API now
    carries the loss.json verdict so the frontend can render an honest
    "timed out" panel for a zero-turn complete run.
    """
    _install_stub_transcript(monkeypatch)

    # Drop a loss.json next to the challenger's events.jsonl carrying the
    # exact projection the dashboard needs.
    challenger_run_dir = (
        workspace / "epochs" / "2026-05-16_e0" / "generations" / "v1" / "runs" / "waffles_single"
    )
    _write_json(
        challenger_run_dir / "loss.json",
        {
            "run_id": "r1",
            "entry_id": "waffles_single",
            "generation_id": "v1",
            "epoch_id": "2026-05-16_e0",
            "drift_counts": [],
            "plan_revisions": 1,
            "task_failure_ratio": 1.0,
            "runtime_ms": 180000,
            "wall_clock_budget_exceeded": True,
            "drift_loss": 60.5,
            "pass_fail": False,
            "expectation_result": {
                "kind": "predicate",
                "passed": False,
                "detail": "predicate returned False",
            },
            "metric_counts": [
                {"name": "cost:llm_calls", "severity": "", "count": 8.0},
                {"name": "output:chars", "severity": "", "count": 7349.0},
            ],
        },
    )

    app = create_app(workspace, static_dir, read_only=True)
    with TestClient(app) as c:
        r = c.get("/api/matchup/waffles_single/conversations")
        assert r.status_code == 200
        body = r.json()

    result = body["challenger"]["result"]
    assert result is not None, "the challenger side must carry a result block"
    assert result["wall_clock_budget_exceeded"] is True
    assert result["runtime_ms"] == 180000
    assert result["pass_fail"] is False
    assert result["expectation_result"]["kind"] == "predicate"
    assert result["expectation_result"]["passed"] is False
    assert result["expectation_result"]["detail"] == "predicate returned False"

    metric_names = {m["name"]: m["count"] for m in result["metric_counts"]}
    assert metric_names == {"cost:llm_calls": 8.0, "output:chars": 7349.0}

    # The champion side has no loss.json — its result must be None
    # (the frontend then falls back to the existing zero-turn message).
    assert body["champion"]["result"] is None


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
# Per-(epoch, gen, entry) transcript endpoint — L4 conversation diff
# ---------------------------------------------------------------------------


def test_run_transcript_endpoint_returns_turn_shape(
    workspace: Path, static_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``GET /api/run/{epoch}/{gen}/{entry}/transcript`` returns the
    reducer's :class:`Transcript` ``.to_dict()`` plus the resolved
    coordinates. This is the endpoint the L4 conversation-diff view
    fetches the focused-side and compare-side transcripts from.
    """
    _install_stub_transcript(monkeypatch)
    app = create_app(workspace, static_dir, read_only=True)
    with TestClient(app) as c:
        r = c.get("/api/run/2026-05-16_e0/v1/waffles_single/transcript")
        assert r.status_code == 200
        body = r.json()
        # Reducer payload keys.
        for key in ("turns", "annotations", "run_id", "event_count", "complete"):
            assert key in body
        # Resolved coordinate keys (added by the endpoint so the
        # frontend can label the column without a second lookup).
        assert body["epoch_id"] == "2026-05-16_e0"
        assert body["generation_id"] == "v1"
        assert body["entry_id"] == "waffles_single"
        # Stub transcript counts the file's lines.
        assert body["event_count"] == 3
        # The stub returns a single user turn — verify the shape passes
        # through.
        assert isinstance(body["turns"], list)
        assert body["turns"][0]["role"] == "user"


def test_run_transcript_endpoint_missing_run_returns_empty_transcript(
    workspace: Path, static_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A nonexistent run yields a graceful empty transcript (HTTP 200).

    The L4 view must render an honest empty column when the compare
    target has no run on disk yet — a hard 404 would break the picker.
    """
    _install_stub_transcript(monkeypatch)
    app = create_app(workspace, static_dir, read_only=True)
    with TestClient(app) as c:
        r = c.get("/api/run/2026-05-16_e0/v99/waffles_single/transcript")
        assert r.status_code == 200
        body = r.json()
        assert body["turns"] == []
        assert body["annotations"] == []
        assert body["run_id"] is None
        assert body["event_count"] == 0
        assert body["complete"] is False
        assert body["epoch_id"] == "2026-05-16_e0"
        assert body["generation_id"] == "v99"


def test_run_transcript_endpoint_unsafe_ids_degrade(
    workspace: Path, static_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A malformed coordinate returns an empty transcript with an error,
    NOT a 500. Mirrors the per-judge / expectations endpoints' contract.
    """
    _install_stub_transcript(monkeypatch)
    app = create_app(workspace, static_dir, read_only=True)
    with TestClient(app) as c:
        # An id containing a path separator after URL decode is rejected
        # by ``_is_safe_id`` — the same predicate every per-(epoch, gen,
        # entry) endpoint uses, so the handler reports an empty
        # transcript with an ``error`` field rather than 500.
        r = c.get("/api/run/has%20space/v1/waffles_single/transcript")
        assert r.status_code == 200
        body = r.json()
        assert body["turns"] == []
        assert body["run_id"] is None
        assert "error" in body


# ---------------------------------------------------------------------------
# Fast-mode cached-champion transcript fetch
# ---------------------------------------------------------------------------
# When a fast-mode round runs, the champion side is NOT executed live —
# its ``status_raw`` is ``"cached"`` and the events on disk live under
# the cached generation's own runs directory (the round in which it was
# the live challenger). The matchup-conversations fetcher must route
# cached sides through THAT directory, not the in-progress round's, or
# the conversation view paints "Waiting for the first turn…" for an
# entry that actually completed several rounds ago.


def _populate_fast_mode_cached_workspace(
    tmp_path: Path,
    *,
    epoch_id: str = "2026-05-18_fastmode",
    entry_id: str = "waffles_single",
    cached_gen: str = "v1",
    challenger_gen: str = "v2",
    cached_events: list[str] | None = None,
    challenger_events: list[str] | None = None,
    champion_status: str = "cached",
    challenger_status: str = "running",
) -> Path:
    """Build a workspace mimicking a fast-mode round in progress.

    The cached champion side has a populated events.jsonl under its own
    generation directory (left over from when it ran live as a
    challenger); the live challenger writes a fresh events.jsonl under
    the in-progress generation directory.
    """
    ws = tmp_path / ".zicato"
    runtime = ws / "runtime"
    (runtime / "active_runs").mkdir(parents=True)
    (runtime / "control").mkdir(parents=True)
    _write(ws / "current_epoch", epoch_id)

    _write_json(
        runtime / "heartbeat.json",
        {
            "pid": 4242,
            "instance_id": "default",
            "started_at": "2026-05-18T04:00:00Z",
            "last_heartbeat": "2026-05-18T04:30:00Z",
            "epoch_id": epoch_id,
            "generation_id": challenger_gen,
            "phase": "tournament",
            "round_index": 1,
            "round_started_at": "2026-05-18T04:25:00Z",
        },
    )

    _write_json(
        runtime / "active_tournament.json",
        {
            "tournament_id": f"tourn_{epoch_id}_{challenger_gen}",
            "parent_generation_id": cached_gen,
            "child_generation_id": challenger_gen,
            "epoch_id": epoch_id,
            "started_at": "2026-05-18T04:25:00Z",
            "phase": "running",
            "round_index": 1,
            "total_rounds": 3,
            "entries": [
                {"entry_id": entry_id, "side": "parent", "status": champion_status},
                {"entry_id": entry_id, "side": "child", "status": challenger_status},
            ],
            "partial_champion_agg": {"generation_id": cached_gen},
            "partial_challenger_agg": {},
        },
    )

    # Cached champion events: persisted from the original round in which
    # this generation was the live challenger. Path is the canonical
    # ``epochs/{epoch}/generations/{cached_gen}/runs/{entry}/events.jsonl``.
    cached_events_path = (
        ws / "epochs" / epoch_id / "generations" / cached_gen / "runs" / entry_id / "events.jsonl"
    )
    cached_events_path.parent.mkdir(parents=True, exist_ok=True)
    lines = (
        cached_events
        if cached_events is not None
        else [
            json.dumps(
                {
                    "emittedAt": "2026-05-17T10:00:01Z",
                    "eventId": "cached:0:a",
                    "runId": "cached-run-v1",
                    "sequence": "0",
                    "runStarted": {"goalSummary": "Make a deck about waffles."},
                }
            ),
            json.dumps(
                {
                    "emittedAt": "2026-05-17T10:01:02Z",
                    "eventId": "cached:1:b",
                    "runId": "cached-run-v1",
                    "sequence": "1",
                    "runCompleted": {"outcomeSummary": "deck produced"},
                }
            ),
        ]
    )
    cached_events_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # The live challenger's events file — may be empty (just started)
    # or partially populated. Callers control via ``challenger_events``.
    if challenger_events is not None:
        challenger_path = (
            ws
            / "epochs"
            / epoch_id
            / "generations"
            / challenger_gen
            / "runs"
            / entry_id
            / "events.jsonl"
        )
        challenger_path.parent.mkdir(parents=True, exist_ok=True)
        challenger_path.write_text(
            ("\n".join(challenger_events) + "\n") if challenger_events else "",
            encoding="utf-8",
        )

    return ws


def test_matchup_conversations_cached_champion_reads_cached_generation(
    tmp_path: Path,
    static_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cached champion side reads events from its cached source generation.

    The on-disk events.jsonl for the cached side lives under its OWN
    generation directory (the round it ran live). The fetcher must
    resolve the champion side's generation_id to that directory — not
    leave the column empty just because the cached side did not run
    this round.
    """
    ws = _populate_fast_mode_cached_workspace(tmp_path)
    _install_stub_transcript(monkeypatch)
    app = create_app(ws, tmp_path / "static", read_only=True)
    (tmp_path / "static").mkdir(exist_ok=True)
    with TestClient(app) as c:
        r = c.get("/api/matchup/waffles_single/conversations")
        assert r.status_code == 200
        body = r.json()
        # Cached champion side resolves to the cached generation id.
        assert body["champion"] is not None
        assert body["champion"]["generation_id"] == "v1"
        # The transcript came from the cached gen's persisted events
        # (the stub reports event_count = number of non-blank lines).
        assert body["champion"]["transcript"] is not None
        assert body["champion"]["transcript"]["event_count"] == 2
        # Sanity: challenger side still resolves to the in-progress gen.
        assert body["challenger"]["generation_id"] == "v2"


def test_matchup_conversations_running_challenger_in_progress_placeholder(
    tmp_path: Path,
    static_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A running side with no events yet keeps the in-progress placeholder.

    The fetcher must NOT pretend the run is complete when its events
    file is missing or empty — the JS rendering path keys "Waiting for
    the first turn…" off ``turns.length === 0 && !complete``, so the
    transcript must report empty turns and ``complete: False``.
    """
    ws = _populate_fast_mode_cached_workspace(
        tmp_path,
        challenger_events=[],  # write an empty events.jsonl
    )
    # Use the REAL transcript reconstructor here so the empty / not-
    # complete state is what the live dashboard would actually see.
    app = create_app(ws, tmp_path / "static", read_only=True)
    (tmp_path / "static").mkdir(exist_ok=True)
    with TestClient(app) as c:
        r = c.get("/api/matchup/waffles_single/conversations")
        assert r.status_code == 200
        body = r.json()
        # Cached champion still resolves with its real cached events.
        assert body["champion"] is not None
        assert body["champion"]["generation_id"] == "v1"
        # Challenger side resolved to a run path but the events are
        # empty — transcript reports zero turns and not complete.
        assert body["challenger"] is not None
        assert body["challenger"]["generation_id"] == "v2"
        challenger_transcript = body["challenger"]["transcript"]
        assert challenger_transcript is not None
        # The transcript is present but empty — the JS placeholder
        # ("Waiting for the first turn…") fires off this exact shape.
        assert challenger_transcript.get("turns") == []
        assert challenger_transcript.get("complete") is False


def test_matchup_conversations_full_mode_both_sides_live(
    tmp_path: Path,
    static_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: full-mode (both sides ran live this round) still works.

    The legacy path — both ``parent`` and ``child`` entries with
    ``status="completed"`` and events under their respective generation
    directories — must keep resolving via the per-entry generation_id.
    """
    ws = _populate_fast_mode_cached_workspace(
        tmp_path,
        champion_status="completed",
        challenger_status="completed",
        challenger_events=[
            json.dumps(
                {
                    "emittedAt": "2026-05-18T04:25:01Z",
                    "eventId": "live:0:a",
                    "runId": "live-run-v2",
                    "sequence": "0",
                    "runStarted": {"goalSummary": "Make a deck about waffles."},
                }
            ),
            json.dumps(
                {
                    "emittedAt": "2026-05-18T04:25:02Z",
                    "eventId": "live:1:b",
                    "runId": "live-run-v2",
                    "sequence": "1",
                    "runCompleted": {"outcomeSummary": "deck produced"},
                }
            ),
        ],
    )
    _install_stub_transcript(monkeypatch)
    app = create_app(ws, tmp_path / "static", read_only=True)
    (tmp_path / "static").mkdir(exist_ok=True)
    with TestClient(app) as c:
        r = c.get("/api/matchup/waffles_single/conversations")
        assert r.status_code == 200
        body = r.json()
        assert body["champion"]["generation_id"] == "v1"
        assert body["challenger"]["generation_id"] == "v2"
        # Both sides have populated event files.
        assert body["champion"]["transcript"]["event_count"] == 2
        assert body["challenger"]["transcript"]["event_count"] == 2


def test_active_tournament_entries_carry_generation_id(
    tmp_path: Path,
) -> None:
    """``read_active_tournament_dict`` stamps generation_id on every entry.

    Without this stamp, the matchup-conversations fetcher cannot route
    cached entries to the right events file (the cached generation id
    is implicit in the tournament-level ``parent_generation_id`` but a
    consumer should not have to re-derive it per-entry).
    """
    from zicato.dashboard.state_reader import (
        WorkspacePaths,
        read_active_tournament_dict,
    )

    ws = _populate_fast_mode_cached_workspace(tmp_path)
    paths = WorkspacePaths(ws)
    t = read_active_tournament_dict(paths)
    assert t is not None
    entries_by_side = {e["side"]: e for e in t["entries"]}
    assert entries_by_side["parent"]["generation_id"] == "v1"
    assert entries_by_side["parent"]["status_raw"] == "cached"
    assert entries_by_side["parent"]["status"] == "done"
    assert entries_by_side["child"]["generation_id"] == "v2"
    assert entries_by_side["child"]["status_raw"] == "running"
    assert entries_by_side["child"]["status"] == "running"


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


# ---------------------------------------------------------------------------
# ADK session id utility — read_adk_session_id_from_events
# ---------------------------------------------------------------------------


def test_read_adk_session_id_from_events_camel(tmp_path: Path) -> None:
    """Reads ``sessionId`` (camelCase) from the first line of an events.jsonl."""
    from zicato.dashboard.state_reader import read_adk_session_id_from_events

    p = tmp_path / "events.jsonl"
    _write(
        p,
        json.dumps({"eventId": "e0", "runId": "r0", "sessionId": "sid-camel-xyz"}) + "\n",
    )
    assert read_adk_session_id_from_events(str(p)) == "sid-camel-xyz"


def test_read_adk_session_id_from_events_snake(tmp_path: Path) -> None:
    """Reads ``session_id`` (snake_case) when ``sessionId`` is absent."""
    from zicato.dashboard.state_reader import read_adk_session_id_from_events

    p = tmp_path / "events.jsonl"
    _write(
        p,
        json.dumps({"event_id": "e0", "run_id": "r0", "session_id": "sid-snake-abc"}) + "\n",
    )
    assert read_adk_session_id_from_events(str(p)) == "sid-snake-abc"


def test_read_adk_session_id_from_events_missing_file(tmp_path: Path) -> None:
    """A missing file degrades to ``""``."""
    from zicato.dashboard.state_reader import read_adk_session_id_from_events

    assert read_adk_session_id_from_events(str(tmp_path / "no-such-file.jsonl")) == ""


def test_read_adk_session_id_from_events_none_path() -> None:
    """A ``None`` path degrades to ``""``."""
    from zicato.dashboard.state_reader import read_adk_session_id_from_events

    assert read_adk_session_id_from_events(None) == ""


def test_read_adk_session_id_from_events_no_session_field(tmp_path: Path) -> None:
    """An events.jsonl with no session envelope key degrades to ``""``."""
    from zicato.dashboard.state_reader import read_adk_session_id_from_events

    p = tmp_path / "events.jsonl"
    _write(p, json.dumps({"event_id": "e0", "run_id": "r0"}) + "\n")
    assert read_adk_session_id_from_events(str(p)) == ""


# ---------------------------------------------------------------------------
# Phase-0 redesign: L0 workspace view + L1 contract diff
# ---------------------------------------------------------------------------


def _seed_second_epoch(ws: Path) -> str:
    """Add a second epoch directory to the workspace fixture.

    Returns the new epoch id. The directory is sorted-AFTER the
    fixture's ``2026-05-16_e0`` so build_contract_diff resolves it as
    the successor with ``2026-05-16_e0`` as the predecessor.
    """
    epoch_id = "2026-05-17_e1"
    epoch_dir = ws / "epochs" / epoch_id
    (epoch_dir / "generations" / "v0").mkdir(parents=True, exist_ok=True)
    _write(epoch_dir / "brief.md", "# brief\n\n## Goal\n\nIterate further.\n")
    _write_json(epoch_dir / "config.json", {"closed": False})
    return epoch_id


def test_build_workspace_view_returns_per_epoch_rows(workspace: Path) -> None:
    """build_workspace_view enumerates every epoch directory on disk."""
    from zicato.dashboard.state_reader import WorkspacePaths, build_workspace_view

    _seed_second_epoch(workspace)
    view = build_workspace_view(WorkspacePaths(workspace))

    assert view["current_epoch_id"] == "2026-05-16_e0"
    ids = [row["epoch_id"] for row in view["epochs"]]
    assert ids == ["2026-05-16_e0", "2026-05-17_e1"]
    # Each row carries the contract fields the L0 view needs.
    for row in view["epochs"]:
        for key in (
            "epoch_id",
            "goal",
            "best_scalar",
            "best_generation_id",
            "generation_count",
            "promoted_count",
            "closed",
        ):
            assert key in row
    # Sparkline mirrors the epochs list one-to-one.
    spark_ids = [pt["epoch_id"] for pt in view["sparkline"]]
    assert spark_ids == ids


def test_build_workspace_view_no_epochs_directory(tmp_path: Path) -> None:
    """A workspace without an ``epochs/`` dir degrades to an empty list."""
    from zicato.dashboard.state_reader import WorkspacePaths, build_workspace_view

    ws = tmp_path / ".zicato"
    (ws / "runtime").mkdir(parents=True)
    view = build_workspace_view(WorkspacePaths(ws))
    assert view["epochs"] == []
    assert view["sparkline"] == []
    assert view["current_epoch_id"] is None


def test_build_workspace_view_closed_flag_reads_config(workspace: Path) -> None:
    """A closed epoch surfaces ``closed: True``."""
    from zicato.dashboard.state_reader import WorkspacePaths, build_workspace_view

    epoch_dir = workspace / "epochs" / "2026-05-16_e0"
    _write_json(epoch_dir / "config.json", {"closed": True})

    view = build_workspace_view(WorkspacePaths(workspace))
    row = next(r for r in view["epochs"] if r["epoch_id"] == "2026-05-16_e0")
    assert row["closed"] is True


def test_build_contract_diff_no_predecessor(workspace: Path) -> None:
    """The first epoch on disk reports ``predecessor_epoch_id = None``."""
    from zicato.dashboard.state_reader import WorkspacePaths, build_contract_diff

    diff = build_contract_diff(WorkspacePaths(workspace), "2026-05-16_e0")
    assert diff["epoch_id"] == "2026-05-16_e0"
    assert diff["predecessor_epoch_id"] is None
    assert diff["any_changed"] is False
    # Stable five-row matrix even when no diff is possible.
    names = [c["name"] for c in diff["components"]]
    assert names == ["board", "brief", "scoring", "entrypoint", "mutable_trees"]
    for c in diff["components"]:
        assert c["changed"] is False


def test_build_contract_diff_flags_changed_components(workspace: Path) -> None:
    """Components with differing hashes between epochs are marked changed."""
    from zicato.dashboard.state_reader import WorkspacePaths, build_contract_diff

    # Predecessor — the fixture's epoch.
    pred_dir = workspace / "epochs" / "2026-05-16_e0"
    _write_json(
        pred_dir / "contract_components.json",
        {
            "board": "boardhash_v0",
            "brief": "briefhash_v0",
            "scoring": "scoringhash_v0",
            "entrypoint": "entryhash_v0",
            "mutable_trees": "treeshash_v0",
        },
    )
    # Successor with only the brief changed.
    succ_id = _seed_second_epoch(workspace)
    succ_dir = workspace / "epochs" / succ_id
    _write_json(
        succ_dir / "contract_components.json",
        {
            "board": "boardhash_v0",
            "brief": "briefhash_v1",
            "scoring": "scoringhash_v0",
            "entrypoint": "entryhash_v0",
            "mutable_trees": "treeshash_v0",
        },
    )

    diff = build_contract_diff(WorkspacePaths(workspace), succ_id)
    assert diff["predecessor_epoch_id"] == "2026-05-16_e0"
    assert diff["any_changed"] is True
    changed = {c["name"]: c for c in diff["components"] if c["changed"]}
    assert set(changed) == {"brief"}
    assert changed["brief"]["previous_hash"] == "briefhash_v0"
    assert changed["brief"]["current_hash"] == "briefhash_v1"


def test_build_contract_diff_missing_components_file(workspace: Path) -> None:
    """An absent ``contract_components.json`` does not raise — every row reads ``None``."""
    from zicato.dashboard.state_reader import WorkspacePaths, build_contract_diff

    succ_id = _seed_second_epoch(workspace)
    diff = build_contract_diff(WorkspacePaths(workspace), succ_id)
    assert diff["predecessor_epoch_id"] == "2026-05-16_e0"
    # No component hashes anywhere — every row reads (None, None, False).
    for c in diff["components"]:
        assert c["previous_hash"] is None
        assert c["current_hash"] is None
        assert c["changed"] is False
    assert diff["any_changed"] is False


def test_api_workspace_endpoint_returns_view(client: TestClient) -> None:
    """``GET /api/workspace`` returns the L0 workspace view payload."""
    r = client.get("/api/workspace")
    assert r.status_code == 200
    body = r.json()
    assert "current_epoch_id" in body
    assert "epochs" in body and isinstance(body["epochs"], list)
    assert "sparkline" in body and isinstance(body["sparkline"], list)


def test_api_contract_diff_endpoint_returns_payload(client: TestClient) -> None:
    """``GET /api/contract-diff/{epoch_id}`` returns the L1 diff payload."""
    r = client.get("/api/contract-diff/2026-05-16_e0")
    assert r.status_code == 200
    body = r.json()
    assert body["epoch_id"] == "2026-05-16_e0"
    assert "components" in body
    names = [c["name"] for c in body["components"]]
    assert names == ["board", "brief", "scoring", "entrypoint", "mutable_trees"]


def test_api_contract_diff_endpoint_rejects_unsafe_id(client: TestClient) -> None:
    """Malformed epoch ids degrade to an empty diff (no 500)."""
    r = client.get("/api/contract-diff/../etc/passwd")
    # The Starlette path regex may reject the slash before reaching our
    # handler (404), or the handler may return its own empty-diff
    # JSON 200. Both are acceptable degradation modes.
    assert r.status_code in (200, 404, 405)
    if r.status_code == 200:
        body = r.json()
        assert body["components"] == []


# ---------------------------------------------------------------------------
# Variant T (Console IV) — the sole shipping UI: static shell structure
# ---------------------------------------------------------------------------


def test_variant_t_mount_present_in_index_html() -> None:
    """The served ``index.html`` mounts Variant T and nothing else.

    Variant T paints its entire shell at runtime into ``#variant-root``;
    the static page only carries that host + the ``app_T.js`` bootstrap.
    The retired v1 (phase0) and v2 (Notebook/Bench) shells must be gone.
    """
    import zicato.dashboard as _dashboard_pkg

    index_path = Path(_dashboard_pkg.__file__).resolve().parent / "static" / "index.html"
    html = index_path.read_text(encoding="utf-8")
    assert 'id="variant-root"' in html, "Variant-T mount #variant-root must be present"
    assert "'app_T.js'" in html, "Variant-T entry app_T.js must be loaded by the bootstrap"
    # The retired shells and their fallback bootstrap must be gone.
    assert "phase0-shell" not in html, "retired v1 phase0 shell must not be in index.html"
    assert "v2-root" not in html, "retired v2 root must not be in index.html"
    assert (
        "'app.js'" not in html and "'app2.js'" not in html
    ), "retired v1/v2 entries must not be referenced by the bootstrap"


def test_build_workspace_view_promoted_count_reads_experiments(workspace: Path) -> None:
    """build_workspace_view counts a promoted generation from experiment.json."""
    from zicato.dashboard.state_reader import WorkspacePaths, build_workspace_view

    epoch_dir = workspace / "epochs" / "2026-05-16_e0"
    gen_dir = epoch_dir / "generations" / "v1"
    gen_dir.mkdir(parents=True, exist_ok=True)
    _write_json(
        gen_dir / "experiment.json",
        {
            "generation_id": "v1",
            "outcome": {"tournament_decision": "promoted"},
        },
    )

    view = build_workspace_view(WorkspacePaths(workspace))
    row = next(r for r in view["epochs"] if r["epoch_id"] == "2026-05-16_e0")
    assert row["promoted_count"] >= 1, "the promoted generation must be counted"
    assert row["generation_count"] >= 1, "v1 must show up in generation_count"


# ---------------------------------------------------------------------------
# Cross-epoch scoping — ``?epoch=<id>`` resolves a NON-current epoch
# (Class A: the dashboard must view a completed epoch while another is live).
# ---------------------------------------------------------------------------


def _add_second_epoch(workspace: Path, epoch_id: str = "2026-05-17_e1") -> str:
    """Add a SECOND, non-current epoch to the fixture workspace.

    The fixture's current epoch is ``2026-05-16_e0``; this layers a distinct
    epoch with its own contract, lineage rows, and index rows so a
    ``?epoch=<id>`` read can be checked against the current-epoch default.
    """
    epoch_dir = workspace / "epochs" / epoch_id
    for gen in ("v0", "v1"):
        (epoch_dir / "generations" / gen).mkdir(parents=True, exist_ok=True)
    _write(
        epoch_dir / "board.jsonl",
        json.dumps(
            {
                "id": "second_board_entry",
                "kind": "single_turn",
                "input": "A different board for the second epoch.",
                "wall_clock_budget_seconds": 240,
                "weight": 2.0,
                "tags": ["e1"],
                "expectation": {"kind": "rubric"},
            }
        )
        + "\n",
    )
    _write(epoch_dir / "brief.md", "# Second epoch brief\nDifferent goal.\n")
    _write_json(epoch_dir / "scoring.json", {"weights": {"drift_loss": 1.0}})
    _write_json(epoch_dir / "config.json", {"contract_hash": "h2", "closed": True})

    # lineage.json: both epochs share gen ids (v0/v1) — a leak would surface.
    _write_json(
        workspace / "lineage.json",
        {
            "epochs": [
                {
                    "id": "2026-05-16_e0",
                    "generations": [
                        {
                            "id": "v0",
                            "parent_id": None,
                            "promoted": True,
                            "created_at": "2026-05-16T04:00:00Z",
                        },
                    ],
                },
                {
                    "id": epoch_id,
                    "generations": [
                        {
                            "id": "v0",
                            "parent_id": None,
                            "promoted": True,
                            "created_at": "2026-05-17T04:00:00Z",
                        },
                        {
                            "id": "v1",
                            "parent_id": "v0",
                            "promoted": True,
                            "created_at": "2026-05-17T04:30:00Z",
                        },
                    ],
                },
            ]
        },
    )

    # index rows for the second epoch (its own generations / tournament / runs).
    conn = sqlite3.connect(workspace / "index.db")
    conn.executemany(
        "INSERT INTO generations VALUES(?,?,?,?)",
        [(epoch_id, "v0", None, 1), (epoch_id, "v1", "v0", 1)],
    )
    conn.execute(
        "INSERT INTO tournaments VALUES(?,?,?,?,?,?,?,?,?,?)",
        ("t2", epoch_id, "v0", "v1", "promoted", 0.9, 0.4, -0.5, None, "2026-05-17T04:30:00Z"),
    )
    conn.executemany(
        "INSERT INTO loss_profiles VALUES(?,?,?,?,?,?,?)",
        [
            ("r2", epoch_id, "v0", "second_board_entry", 0.9, "fail", "{}"),
            ("r3", epoch_id, "v1", "second_board_entry", 0.4, "pass", "{}"),
        ],
    )
    conn.executemany(
        "INSERT INTO runs VALUES(?,?,?,?,?,?,?,?)",
        [
            ("r2", epoch_id, "v0", "second_board_entry", "", "", 0, 100),
            ("r3", epoch_id, "v1", "second_board_entry", "", "", 0, 100),
        ],
    )
    conn.commit()
    conn.close()
    return epoch_id


def test_epoch_view_scoped_to_non_current_epoch(client: TestClient, workspace: Path) -> None:
    """``/api/epoch?epoch=<non-current>`` returns THAT epoch's contract."""
    e1 = _add_second_epoch(workspace)

    # omitted ⇒ current epoch (unchanged).
    current = client.get("/api/epoch").json()
    assert current["epoch_id"] == "2026-05-16_e0"
    assert current["contract_hash"] == "h1"
    assert current["board"][0]["id"] == "waffles_single"

    # scoped ⇒ the SECOND epoch's own contract / board.
    scoped = client.get(f"/api/epoch?epoch={e1}").json()
    assert scoped["epoch_id"] == e1
    assert scoped["contract_hash"] == "h2"
    assert scoped["closed"] is True
    assert scoped["board"][0]["id"] == "second_board_entry"
    assert scoped["board"][0]["id"] != "waffles_single"


def test_score_trajectory_scoped_to_non_current_epoch(client: TestClient, workspace: Path) -> None:
    """``/api/score-trajectory?epoch=<id>`` plots only that epoch's gens."""
    e1 = _add_second_epoch(workspace)

    current = client.get("/api/score-trajectory").json()
    assert current["epoch_id"] == "2026-05-16_e0"
    assert {p["generation_id"] for p in current["points"]} == {"v0", "v1"}

    scoped = client.get(f"/api/score-trajectory?epoch={e1}").json()
    assert scoped["epoch_id"] == e1
    # the second epoch's gens — its own v0/v1, scored from its own loss profiles.
    assert {p["generation_id"] for p in scoped["points"]} == {"v0", "v1"}
    scalars = {p["generation_id"]: p["scalar"] for p in scoped["points"]}
    assert scalars["v1"] == pytest.approx(0.4)
    assert scalars["v0"] == pytest.approx(0.9)


def test_tournaments_scoped_to_non_current_epoch(client: TestClient, workspace: Path) -> None:
    """``/api/tournaments?epoch=<id>`` returns that epoch's matchups."""
    e1 = _add_second_epoch(workspace)

    current = client.get("/api/tournaments").json()
    assert current["epoch_id"] == "2026-05-16_e0"
    assert any(m.get("decision") == "rejected" for m in current["matchups"])

    scoped = client.get(f"/api/tournaments?epoch={e1}").json()
    assert scoped["epoch_id"] == e1
    assert scoped["matchups"], "the second epoch has its own matchup"
    assert scoped["matchups"][0]["champion"] == "v0"
    assert scoped["matchups"][0]["challenger"] == "v1"
    assert scoped["matchups"][0]["decision"] == "promoted"


def test_scoped_endpoints_reject_unknown_and_traversal(client: TestClient) -> None:
    """A bogus / path-traversing ``?epoch=`` is a 404 on every scoped route."""
    for route in ("/api/epoch", "/api/score-trajectory", "/api/tournaments"):
        assert client.get(f"{route}?epoch=does-not-exist").status_code == 404
        # path-traversal must never escape the workspace.
        assert client.get(f"{route}?epoch=../secrets").status_code == 404
        assert client.get(f"{route}?epoch=..").status_code == 404
        # an EMPTY value is treated as omitted → current epoch (200).
        assert client.get(f"{route}?epoch=").status_code == 200
