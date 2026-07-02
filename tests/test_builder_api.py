"""Tests for the four builder REST endpoints via the dashboard TestClient."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from zicato.cli.common import write_workspace_config
from zicato.core.types import ScoringWeights
from zicato.dashboard.server import create_app
from zicato.epoch.lifecycle import new_epoch


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / ".zicato"
    ws.mkdir()
    board = tmp_path / "board.jsonl"
    board.write_text(
        '{"id": "e1", "kind": "single_turn", "budget_s": 60, "input": "hi"}\n'
        '{"id": "e2", "kind": "single_turn", "budget_s": 60, "input": "bye"}\n',
        encoding="utf-8",
    )
    brief = tmp_path / "brief.md"
    brief.write_text("# Brief\n\nsteer\n", encoding="utf-8")
    scoring = tmp_path / "scoring.json"
    scoring.write_text(json.dumps({"drift_weight": 1.0}), encoding="utf-8")
    write_workspace_config(
        ws,
        {
            "instance_id": "default",
            "adk_entrypoint": "pkg.mod:agent",
            "mutable_trees": [],
            "source_roots": [],
            "contract": {
                "board_path": str(board.resolve()),
                "rubric_path": str(brief.resolve()),
                "scoring_path": str(scoring.resolve()),
            },
        },
    )
    new_epoch(
        workspace_root=ws,
        name="alpha",
        board_source=board,
        brief_source=brief,
        weights=ScoringWeights(),
        entrypoint="pkg.mod:agent",
    )
    return ws


@pytest.fixture()
def client(workspace: Path, tmp_path: Path) -> TestClient:
    static = tmp_path / "static"
    static.mkdir()
    app = create_app(workspace, static, read_only=False)
    return TestClient(app)


def test_builder_config_endpoint(client: TestClient) -> None:
    resp = client.get("/builder/config")
    assert resp.status_code == 200
    body = resp.json()
    assert "chat_enabled" in body
    assert body["chat_enabled"] is False  # no builder.json → empty model
    assert "agent" in body
    assert "skills" in body


def test_builder_draft_inits_from_live(client: TestClient) -> None:
    resp = client.get("/builder/draft?session=s1")
    assert resp.status_code == 200
    body = resp.json()
    assert body["session"] == "s1"
    ids = {e["id"] for e in body["draft"]["board"]}
    assert ids == {"e1", "e2"}
    assert "cost" in body
    assert "diff" in body


def test_builder_op_set_structure_returns_full_envelope(client: TestClient) -> None:
    resp = client.post(
        "/builder/op",
        json={"session": "s2", "op": "set_structure", "args": {"structure": "swiss"}},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["patch"]["op"] == "set_structure"
    assert body["draft"]["scoring"]["tournament"]["structure"] == "swiss"
    assert body["cost"]["structure"] == "swiss"
    assert "warnings" in body
    assert "structure" in body["diff"]["changed_components"]


def test_builder_op_accumulates_across_calls_in_a_session(client: TestClient) -> None:
    client.post(
        "/builder/op",
        json={"session": "s3", "op": "set_structure", "args": {"structure": "swiss"}},
    )
    resp = client.post(
        "/builder/op",
        json={"session": "s3", "op": "set_param", "args": {"key": "field_size", "value": 4}},
    )
    body = resp.json()
    params = body["draft"]["scoring"]["tournament"]["params"]
    assert params["field_size"] == 4
    assert body["draft"]["scoring"]["tournament"]["structure"] == "swiss"


def test_builder_op_unknown_op_is_400(client: TestClient) -> None:
    resp = client.post("/builder/op", json={"session": "s4", "op": "nope", "args": {}})
    assert resp.status_code == 400
    assert "unknown builder op" in resp.json()["error"]


def test_builder_op_edit_board_entry(client: TestClient) -> None:
    resp = client.post(
        "/builder/op",
        json={
            "session": "s5",
            "op": "edit_board_entry",
            "args": {
                "entry": {
                    "id": "e3",
                    "kind": "single_turn",
                    "wall_clock_budget_seconds": 30,
                    "input": "new entry",
                }
            },
        },
    )
    assert resp.status_code == 200
    ids = {e["id"] for e in resp.json()["draft"]["board"]}
    assert "e3" in ids


def test_builder_apply_dry_run(client: TestClient, workspace: Path) -> None:
    client.post(
        "/builder/op",
        json={"session": "s6", "op": "set_structure", "args": {"structure": "swiss"}},
    )
    resp = client.post("/builder/apply", json={"session": "s6", "confirm": False})
    assert resp.status_code == 200
    body = resp.json()
    assert body["confirmed"] is False
    assert body["rolled"] is False
    # Nothing written: the live scoring.json still has no tournament block.
    live = json.loads((workspace.parent / "scoring.json").read_text(encoding="utf-8"))
    assert "tournament" not in live


def test_builder_apply_confirm_writes(client: TestClient, workspace: Path) -> None:
    client.post(
        "/builder/op",
        json={"session": "s7", "op": "set_structure", "args": {"structure": "racing"}},
    )
    resp = client.post("/builder/apply", json={"session": "s7", "confirm": True})
    assert resp.status_code == 200
    body = resp.json()
    assert body["confirmed"] is True
    assert body["rolled"] is True
    live = json.loads((workspace.parent / "scoring.json").read_text(encoding="utf-8"))
    assert live["tournament"]["structure"] == "racing"


def test_builder_endpoints_read_only_forbids_writes(workspace: Path, tmp_path: Path) -> None:
    static = tmp_path / "static_ro"
    static.mkdir()
    app = create_app(workspace, static, read_only=True)
    ro_client = TestClient(app)
    # GET still works.
    assert ro_client.get("/builder/config").status_code == 200
    assert ro_client.get("/builder/draft").status_code == 200
    # POST ops are forbidden.
    op_resp = ro_client.post(
        "/builder/op", json={"op": "set_structure", "args": {"structure": "swiss"}}
    )
    assert op_resp.status_code == 403
    apply_resp = ro_client.post("/builder/apply", json={"confirm": True})
    assert apply_resp.status_code == 403


def test_builder_op_preflight_degrades_honestly(client: TestClient) -> None:
    """The fixture workspace has an epoch but no seeded baseline generation:
    the preflight op returns 200 with an honest `available: false` + reason,
    alongside the normal draft/cost/warnings/diff envelope."""
    resp = client.post("/builder/op", json={"session": "pf1", "op": "preflight", "args": {}})
    assert resp.status_code == 200
    body = resp.json()
    pf = body["preflight"]
    assert pf["available"] is False
    assert pf["verdict"] is None
    assert pf["reason"]
    assert "draft" in body
    assert "cost" in body
    assert "warnings" in body
    assert "diff" in body


def test_builder_op_preflight_bad_runs_is_400(client: TestClient) -> None:
    resp = client.post(
        "/builder/op", json={"session": "pf2", "op": "preflight", "args": {"runs": "nope"}}
    )
    assert resp.status_code == 400
    assert "runs" in resp.json()["error"]
    resp = client.post(
        "/builder/op", json={"session": "pf2", "op": "preflight", "args": {"runs": 1}}
    )
    assert resp.status_code == 400


def test_builder_op_envelope_carries_noise_floor_refuse_warning(
    client: TestClient, workspace: Path
) -> None:
    """Once the epoch record carries a measured A/A floor, every op envelope's
    warnings include the REFUSE-severity margin_below_noise_floor rule (the
    fixture contract's default margin 0.01 with the evidence gate off)."""
    from zicato.epoch.lifecycle import current_epoch_id, set_epoch_noise_floor

    epoch_id = current_epoch_id(workspace)
    assert epoch_id
    set_epoch_noise_floor(workspace, epoch_id, {"max_abs_delta": 0.5, "runs": 5})

    resp = client.post(
        "/builder/op",
        json={"session": "pf3", "op": "set_structure", "args": {"structure": "swiss"}},
    )
    assert resp.status_code == 200
    warns = {w["code"]: w for w in resp.json()["warnings"]}
    assert "margin_below_noise_floor" in warns
    assert warns["margin_below_noise_floor"]["severity"] == "refuse"

    # The draft snapshot GET carries it too (the Review pane's first paint).
    snap = client.get("/builder/draft?session=pf3").json()
    codes = {w["code"] for w in snap["warnings"]}
    assert "margin_below_noise_floor" in codes
