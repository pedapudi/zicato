"""Tests for the four builder REST endpoints via the dashboard TestClient."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from zicato.core.types import ScoringWeights
from zicato.dashboard.server import create_app
from zicato.epoch.lifecycle import new_epoch
from zicato.workspace.config_io import write_workspace_config


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


# One entry per kind, in the exact whole-entry JSON the board editor's
# bufferToEntryJson posts. Each must round-trip byte-stably through
# edit_board_entry → the entry serializer the draft.board view reads.
_ROUND_TRIP_ENTRIES = {
    "single_turn": {
        "id": "rt_single",
        "kind": "single_turn",
        "wall_clock_budget_seconds": 60,
        "input": "make a presentation",
    },
    "multi_turn_scripted": {
        "id": "rt_scripted",
        "kind": "multi_turn_scripted",
        "wall_clock_budget_seconds": 120,
        "turns": [{"user": "hello"}, {"user": "and then?"}],
        "max_turns": 4,
    },
    "multi_turn_emulated": {
        "id": "rt_emulated",
        "kind": "multi_turn_emulated",
        "wall_clock_budget_seconds": 360,
        "user_persona": {"goal": "g", "constraints": "c", "stop_when": "s"},
        "max_turns": 6,
    },
    "synthetic_adversarial": {
        "id": "rt_adversarial",
        "kind": "synthetic_adversarial",
        "wall_clock_budget_seconds": 90,
        "input": "attack",
        "adversarial_agent_spec": "pkg.mod:bad_agent",
        "required_drift_kinds": ["off_topic"],
    },
    "synthetic_clean": {
        "id": "rt_clean",
        "kind": "synthetic_clean",
        "wall_clock_budget_seconds": 60,
        "input": "clean",
    },
}


@pytest.mark.parametrize("kind", sorted(_ROUND_TRIP_ENTRIES))
def test_builder_op_edit_board_entry_whole_entry_round_trip(client: TestClient, kind: str) -> None:
    """A whole-entry edit_board_entry per kind round-trips byte-stably.

    The board editor posts the whole entry; the server validates + serializes
    it through the SAME entry serializer the draft.board view reads. The
    re-read row must equal entry_to_dict(validate_board_entry(payload)) — the
    exact byte-stability the flagship editor relies on for a save/reopen loop.
    """
    from zicato.board.jsonl import entry_to_dict
    from zicato.core.board import validate_board_entry

    payload = _ROUND_TRIP_ENTRIES[kind]
    resp = client.post(
        "/builder/op",
        json={"session": f"rt_{kind}", "op": "edit_board_entry", "args": {"entry": payload}},
    )
    assert resp.status_code == 200, resp.text
    board = resp.json()["draft"]["board"]
    row = next(e for e in board if e["id"] == payload["id"])
    expected = entry_to_dict(validate_board_entry(payload))
    assert row == expected, f"{kind}: re-read row diverged from the entry serializer"
    # A second identical edit is idempotent — the re-read row is byte-identical.
    resp2 = client.post(
        "/builder/op",
        json={"session": f"rt_{kind}", "op": "edit_board_entry", "args": {"entry": payload}},
    )
    assert resp2.status_code == 200
    row2 = next(e for e in resp2.json()["draft"]["board"] if e["id"] == payload["id"])
    assert row2 == expected, f"{kind}: a re-issued identical edit is not byte-stable"


def test_builder_op_set_board_meta_dispatch(client: TestClient) -> None:
    resp = client.post(
        "/builder/op",
        json={
            "session": "meta1",
            "op": "set_board_meta",
            "args": {"disable_drift": ["off_topic"], "judge_only": True},
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["patch"]["op"] == "set_board_meta"
    assert body["draft"]["board_meta"] == {"disable_drift": ["off_topic"], "judge_only": True}
    # A board-level header change is a board change: it rolls the epoch.
    assert "board" in body["diff"]["changed_components"]


def test_builder_op_set_board_meta_bad_args_are_400(client: TestClient) -> None:
    resp = client.post(
        "/builder/op",
        json={"session": "meta2", "op": "set_board_meta", "args": {"disable_drift": ["bogus"]}},
    )
    assert resp.status_code == 400
    assert "unknown drift kind" in resp.json()["error"]
    resp = client.post(
        "/builder/op",
        json={"session": "meta2", "op": "set_board_meta", "args": {"disable_drift": "off_topic"}},
    )
    assert resp.status_code == 400
    assert "list" in resp.json()["error"]


def test_builder_op_float_knobs_are_typed_not_passed_through(client: TestClient) -> None:
    """A numeric knob lands in the contract as a NUMBER, or the post is a 400.

    The float args used to reach the ops as the raw JSON value, and the
    outcome split by which validator the field happened to have: a string
    ``"0.5"`` landed in the contract intact for ``promote_margin`` and the
    weight scalars (whose validators never compare them), while
    ``holdout_fraction`` raised an uncaught ``TypeError`` — a 500 — from
    the comparison in its validator. Both shapes are the mis-typed contract
    knob the arg coercion exists to refuse.
    """

    def post(op: str, args: dict[str, object]) -> object:
        return client.post("/builder/op", json={"session": "typed", "op": op, "args": args})

    # A numeric string coerces to a real float rather than being stored raw.
    resp = post("set_gate", {"promote_margin": "0.5"})
    assert resp.status_code == 200
    assert resp.json()["draft"]["scoring"]["promote_margin"] == 0.5
    resp = post("set_holdout", {"fraction": "0.4"})
    assert resp.status_code == 200
    assert resp.json()["draft"]["scoring"]["overfitting"]["holdout_fraction"] == 0.4
    # …including inside the ladder's partial mapping, which reaches the op
    # as raw JSON from BOTH the REST dispatch and the copilot.
    resp = post("set_holdout", {"ladder": {"budget": "8"}})
    assert resp.status_code == 200
    assert resp.json()["draft"]["scoring"]["overfitting"]["ladder"]["budget"] == 8

    # Garbage is a field-precise 400, never a 500.
    for op, args, needle in (
        ("set_gate", {"holdout_margin": "x"}, "holdout_margin"),
        ("set_weights", {"drift_weight": "heavy"}, "drift_weight"),
        ("set_holdout", {"ladder": {"threshold": "bad"}}, "ladder.threshold"),
        # A bool floats to 1.0 in Python, so it must be refused explicitly
        # or `true` would read as a silent weight of 1.0.
        ("set_weights", {"pass_weight": True}, "pass_weight"),
    ):
        resp = post(op, args)
        assert resp.status_code == 400, (op, args, resp.status_code)
        assert needle in resp.json()["error"]


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


def test_builder_op_full_knob_dispatch(client: TestClient) -> None:
    """Every new knob-coverage op dispatches through /builder/op and lands on
    the serialized draft: the extended holdout, the extended gate, namespace
    weights, proposer quality, experiment memory."""
    s = {"session": "knobs"}
    r = client.post(
        "/builder/op",
        json={
            **s,
            "op": "set_holdout",
            "args": {
                "min_board_size_for_split": 12,
                "rotate_holdout": False,
                "random_baseline_every_n": 4,
                "ladder": {"budget": 6},
            },
        },
    )
    assert r.status_code == 200
    of = r.json()["draft"]["scoring"]["overfitting"]
    assert of["min_board_size_for_split"] == 12
    assert of["rotate_holdout"] is False
    assert of["random_baseline_every_n"] == 4
    assert of["ladder"]["budget"] == 6

    r = client.post(
        "/builder/op",
        json={
            **s,
            "op": "set_gate",
            "args": {
                "monotonicity_scope": "aggregate",
                "block_on_containment_violation": True,
                "regression_gate_enabled": True,
                "regression_test_command": ["pytest", "-q"],
                "regression_timeout_s": 90,
                "namespace_monotonicity": {"rubric:": True},
            },
        },
    )
    assert r.status_code == 200
    sc = r.json()["draft"]["scoring"]
    assert sc["pass_rate_monotonicity_scope"] == "aggregate"
    assert sc["block_on_containment_violation"] is True
    assert sc["regression_gate_enabled"] is True
    assert sc["regression_test_command"] == ["pytest", "-q"]
    assert sc["regression_timeout_s"] == 90
    assert sc["namespace_monotonicity"] == {"rubric:": True}

    r = client.post(
        "/builder/op",
        json={
            **s,
            "op": "set_namespace_weights",
            "args": {
                "namespace_weights": {"drift:": 1.0, "rubric:": -2.0},
                "diff_complexity_weight": 0.005,
            },
        },
    )
    assert r.status_code == 200
    sc = r.json()["draft"]["scoring"]
    assert sc["namespace_weights"] == {"drift:": 1.0, "rubric:": -2.0}
    assert sc["diff_complexity_weight"] == 0.005

    r = client.post(
        "/builder/op",
        json={
            **s,
            "op": "set_proposer_quality",
            "args": {"best_of_n": 4, "critique_enabled": False, "recombine": True},
        },
    )
    assert r.status_code == 200
    pq = r.json()["draft"]["scoring"]["proposer_quality"]
    assert pq["best_of_n"] == 4
    assert pq["critique_enabled"] is False
    assert pq["recombine"] is True

    r = client.post(
        "/builder/op", json={**s, "op": "set_experiment_memory", "args": {"cross_epoch": True}}
    )
    assert r.status_code == 200
    assert r.json()["draft"]["scoring"]["experiment_memory"]["cross_epoch"] is True


def test_builder_op_knob_dispatch_errors_are_400(client: TestClient) -> None:
    r = client.post(
        "/builder/op",
        json={"session": "kerr", "op": "set_gate", "args": {"regression_timeout_s": "soon"}},
    )
    assert r.status_code == 400
    assert "integer" in r.json()["error"]
    r = client.post(
        "/builder/op",
        json={"session": "kerr", "op": "set_holdout", "args": {"ladder": {"bogus": 1}}},
    )
    assert r.status_code == 400
    assert "ladder" in r.json()["error"]
    r = client.post(
        "/builder/op",
        json={"session": "kerr", "op": "set_proposer_quality", "args": {"best_of_n": 0}},
    )
    assert r.status_code == 400


def test_builder_op_set_proposer_quality_recombine_dispatch(client: TestClient) -> None:
    """The recombine flag round-trips through /builder/op onto the serialized
    draft; a bad co-arg (best_of_n 0) in the same call still 400s and leaves the
    draft untouched (the op validates before applying — recombine never lands)."""
    s = {"session": "recomb"}
    r = client.post(
        "/builder/op",
        json={**s, "op": "set_proposer_quality", "args": {"best_of_n": 2, "recombine": True}},
    )
    assert r.status_code == 200
    assert r.json()["draft"]["scoring"]["proposer_quality"]["recombine"] is True

    # 400 path: an invalid best_of_n co-arg is rejected wholesale — the prior
    # recombine value is unchanged (no partial apply).
    r = client.post(
        "/builder/op",
        json={**s, "op": "set_proposer_quality", "args": {"best_of_n": 0, "recombine": False}},
    )
    assert r.status_code == 400
    r = client.post("/builder/op", json={**s, "op": "set_proposer_quality", "args": {}})
    assert r.json()["draft"]["scoring"]["proposer_quality"]["recombine"] is True


def test_builder_op_set_proposer_quality_genealogy_dispatch(client: TestClient) -> None:
    """The genealogy count round-trips through /builder/op onto the serialized
    draft; a negative count 400s and leaves the prior value untouched."""
    s = {"session": "gene"}
    r = client.post(
        "/builder/op",
        json={**s, "op": "set_proposer_quality", "args": {"genealogy": 4}},
    )
    assert r.status_code == 200
    assert r.json()["draft"]["scoring"]["proposer_quality"]["genealogy"] == 4

    # 400 path: a negative genealogy count is rejected wholesale (no partial apply).
    r = client.post(
        "/builder/op",
        json={**s, "op": "set_proposer_quality", "args": {"genealogy": -1}},
    )
    assert r.status_code == 400
    r = client.post("/builder/op", json={**s, "op": "set_proposer_quality", "args": {}})
    assert r.json()["draft"]["scoring"]["proposer_quality"]["genealogy"] == 4


def test_builder_op_set_proposer_quality_calibration_feedback_dispatch(
    client: TestClient,
) -> None:
    """The calibration_feedback count round-trips through /builder/op; a negative
    count 400s and leaves the prior value untouched."""
    s = {"session": "calib"}
    r = client.post(
        "/builder/op",
        json={**s, "op": "set_proposer_quality", "args": {"calibration_feedback": 5}},
    )
    assert r.status_code == 200
    assert r.json()["draft"]["scoring"]["proposer_quality"]["calibration_feedback"] == 5

    r = client.post(
        "/builder/op",
        json={**s, "op": "set_proposer_quality", "args": {"calibration_feedback": -1}},
    )
    assert r.status_code == 400
    r = client.post("/builder/op", json={**s, "op": "set_proposer_quality", "args": {}})
    assert r.json()["draft"]["scoring"]["proposer_quality"]["calibration_feedback"] == 5


def test_builder_op_set_telemetry_dialect_dispatch(client: TestClient) -> None:
    """The telemetry dialect round-trips through /builder/op onto the serialized
    draft, rolls the epoch (scoring is a changed component), and an unknown name
    400s leaving the prior value untouched."""
    s = {"session": "dialect"}
    r = client.post(
        "/builder/op",
        json={**s, "op": "set_telemetry_dialect", "args": {"dialect": "adk_events"}},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["draft"]["scoring"]["telemetry_dialect"] == "adk_events"
    # A non-default dialect is a scoring/contract change — it rolls the epoch.
    assert "scoring" in body["diff"]["changed_components"]
    assert body["diff"]["rolls_epoch"] is True

    # 400 path: an unknown dialect is rejected wholesale (no partial apply).
    r = client.post(
        "/builder/op",
        json={**s, "op": "set_telemetry_dialect", "args": {"dialect": "mystery"}},
    )
    assert r.status_code == 400
    r = client.post("/builder/op", json={**s, "op": "set_telemetry_dialect", "args": {}})
    assert r.json()["draft"]["scoring"]["telemetry_dialect"] == "adk_events"


def test_builder_op_fork_switch_list_roundtrip(client: TestClient) -> None:
    s = {"session": "life"}
    # Build state, fork it, verify the slot list + the patch shape.
    client.post("/builder/op", json={**s, "op": "set_structure", "args": {"structure": "swiss"}})
    r = client.post("/builder/op", json={**s, "op": "fork", "args": {"name": "variant-a"}})
    assert r.status_code == 200
    body = r.json()
    assert body["patch"]["op"] == "fork"
    assert body["drafts"] == ["variant-a"]
    assert body["draft"]["scoring"]["tournament"]["structure"] == "swiss"

    # Edit the slot, fork B, switch back to A — A's state is intact.
    client.post(
        "/builder/op",
        json={**s, "op": "set_param", "args": {"key": "field_size", "value": 4}},
    )
    client.post("/builder/op", json={**s, "op": "fork", "args": {"name": "variant-b"}})
    client.post("/builder/op", json={**s, "op": "set_structure", "args": {"structure": "racing"}})
    r = client.post("/builder/op", json={**s, "op": "switch", "args": {"name": "variant-a"}})
    body = r.json()
    assert body["draft"]["scoring"]["tournament"]["structure"] == "swiss"
    assert body["draft"]["scoring"]["tournament"]["params"]["field_size"] == 4
    assert body["drafts"] == ["variant-a", "variant-b"]

    r = client.post("/builder/op", json={**s, "op": "list_drafts", "args": {}})
    assert r.json()["drafts"] == ["variant-a", "variant-b"]

    # The GET snapshot carries the slot list too (the picker's first paint).
    snap = client.get("/builder/draft?session=life").json()
    assert snap["drafts"] == ["variant-a", "variant-b"]

    # Errors: duplicate fork name, unknown switch target.
    assert (
        client.post(
            "/builder/op", json={**s, "op": "fork", "args": {"name": "variant-a"}}
        ).status_code
        == 400
    )
    assert (
        client.post("/builder/op", json={**s, "op": "switch", "args": {"name": "nope"}}).status_code
        == 400
    )


def test_builder_op_compare_keyed_diff(client: TestClient) -> None:
    s = {"session": "cmp"}
    client.post("/builder/op", json={**s, "op": "fork", "args": {"name": "base"}})
    client.post("/builder/op", json={**s, "op": "fork", "args": {"name": "tuned"}})
    client.post("/builder/op", json={**s, "op": "set_gate", "args": {"promote_margin": 0.07}})

    r = client.post(
        "/builder/op",
        json={**s, "op": "compare", "args": {"name_a": "base", "name_b": "tuned"}},
    )
    assert r.status_code == 200
    cmp = r.json()["compare"]
    assert cmp["a"] == "base"
    assert cmp["b"] == "tuned"
    assert "scoring" in cmp["changed_components"]
    assert cmp["scoring"]["promote_margin"] == {"a": 0.01, "b": 0.07}
    assert cmp["board"] == {"added": [], "removed": [], "changed": []}

    # "session" and "live" resolve as operands; the tuned session draft
    # differs from the live contract (it carries the margin edit).
    r = client.post(
        "/builder/op",
        json={**s, "op": "compare", "args": {"name_a": "live", "name_b": "session"}},
    )
    assert r.status_code == 200
    assert "scoring" in r.json()["compare"]["changed_components"]

    # An unknown operand is a clear 400.
    r = client.post(
        "/builder/op",
        json={**s, "op": "compare", "args": {"name_a": "base", "name_b": "ghost"}},
    )
    assert r.status_code == 400
    assert "ghost" in r.json()["error"]


def test_builder_apply_writes_the_active_slot(client: TestClient, workspace: Path) -> None:
    """The write path is unchanged: apply writes whichever draft the session
    is on — fork/compare never write anything themselves."""
    import json as _json

    s = {"session": "slotapply"}
    client.post("/builder/op", json={**s, "op": "fork", "args": {"name": "to-apply"}})
    client.post("/builder/op", json={**s, "op": "set_structure", "args": {"structure": "swiss"}})
    live_before = _json.loads((workspace.parent / "scoring.json").read_text(encoding="utf-8"))
    assert "tournament" not in live_before  # forking wrote nothing
    resp = client.post("/builder/apply", json={**s, "confirm": True})
    assert resp.json()["confirmed"] is True
    live = _json.loads((workspace.parent / "scoring.json").read_text(encoding="utf-8"))
    assert live["tournament"]["structure"] == "swiss"


# ---------------------------------------------------------------------------
# REST-dispatch coverage for the previously untested ops (B1) + the new ops.
# ---------------------------------------------------------------------------


def test_builder_op_set_weights_scalar_and_each_mapping(client: TestClient) -> None:
    s = {"session": "w1"}
    r = client.post(
        "/builder/op",
        json={
            **s,
            "op": "set_weights",
            "args": {
                "drift_weight": 2.0,
                "pass_weight": 3.0,
                "default_judge_weight": 1.5,
                "plan_revision_weight": 0.5,
                "runtime_weight": 0.25,
            },
        },
    )
    assert r.status_code == 200
    sc = r.json()["draft"]["scoring"]
    assert sc["drift_weight"] == 2.0
    assert sc["pass_weight"] == 3.0
    assert sc["default_judge_weight"] == 1.5
    assert sc["plan_revision_weight"] == 0.5
    assert sc["runtime_weight"] == 0.25

    # Each mapping field replaces the WHOLE mapping (wholesale semantics).
    for field, first, second in (
        ("per_kind_weights", {"single_turn": 2.0}, {"multi_turn_scripted": 3.0}),
        ("per_judge_weights", {"j1": 1.0, "j2": 2.0}, {"j3": 4.0}),
        ("severity_weights", {"warning": 1.0}, {"critical": 5.0}),
    ):
        r = client.post("/builder/op", json={**s, "op": "set_weights", "args": {field: first}})
        assert r.status_code == 200
        assert r.json()["draft"]["scoring"][field] == first
        r = client.post("/builder/op", json={**s, "op": "set_weights", "args": {field: second}})
        assert r.status_code == 200
        # Wholesale: the first mapping's keys are GONE, not merged.
        assert r.json()["draft"]["scoring"][field] == second


def test_builder_op_set_proposer_dispatch(client: TestClient, tmp_path: Path) -> None:
    pdir = tmp_path / "proposers" / "p1"
    pdir.mkdir(parents=True)
    r = client.post(
        "/builder/op",
        json={"session": "p1", "op": "set_proposer", "args": {"proposer_path": str(pdir)}},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["patch"]["changed"]["proposer_path"]["to"] == str(pdir)
    assert body["draft"]["proposer_path"] == str(pdir)
    assert "proposer" in body["diff"]["changed_components"]
    # null clears back to the built-in default proposer.
    r = client.post(
        "/builder/op",
        json={"session": "p1", "op": "set_proposer", "args": {"proposer_path": None}},
    )
    assert r.json()["draft"]["proposer_path"] is None


def test_builder_op_set_screening_dispatch(client: TestClient) -> None:
    r = client.post(
        "/builder/op",
        json={"session": "scr", "op": "set_screening", "args": {"entries": 3, "veto_only": True}},
    )
    assert r.status_code == 200
    pq = r.json()["draft"]["scoring"]["proposer_quality"]
    assert pq["screen_entries"] == 3
    assert pq["screen_veto_only"] is True
    # 400 path: negative entries.
    r = client.post(
        "/builder/op", json={"session": "scr", "op": "set_screening", "args": {"entries": -1}}
    )
    assert r.status_code == 400


def test_builder_op_set_brief_dispatch(client: TestClient) -> None:
    r = client.post(
        "/builder/op",
        json={"session": "br", "op": "set_brief", "args": {"text": "steer harder"}},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["draft"]["brief"] == "steer harder"
    assert body["patch"]["changed"]["brief_chars"]["to"] == len("steer harder")
    assert "brief" in body["diff"]["changed_components"]
    # Missing arg is a clear 400, not a 500.
    r = client.post("/builder/op", json={"session": "br", "op": "set_brief", "args": {}})
    assert r.status_code == 400
    assert "text" in r.json()["error"]


def test_builder_op_add_and_remove_judge_dispatch(client: TestClient) -> None:
    s = {"session": "jd"}
    r = client.post(
        "/builder/op",
        json={
            **s,
            "op": "add_judge",
            "args": {
                "entry_id": "e1",
                "judge": {
                    "name": "tone",
                    "mode": "inline",
                    "body": "polite?",
                    "severity": "warning",
                },
            },
        },
    )
    assert r.status_code == 200
    entry = next(e for e in r.json()["draft"]["board"] if e["id"] == "e1")
    assert entry["judges"] == [
        {"name": "tone", "mode": "inline", "body": "polite?", "severity": "warning"}
    ]

    # 400 paths: unknown entry, duplicate judge name, bad severity token.
    r = client.post(
        "/builder/op",
        json={
            **s,
            "op": "add_judge",
            "args": {"entry_id": "ghost", "judge": {"name": "x", "body": "b"}},
        },
    )
    assert r.status_code == 400
    r = client.post(
        "/builder/op",
        json={
            **s,
            "op": "add_judge",
            "args": {"entry_id": "e1", "judge": {"name": "tone", "body": "again"}},
        },
    )
    assert r.status_code == 400
    assert "already has a judge" in r.json()["error"]
    r = client.post(
        "/builder/op",
        json={
            **s,
            "op": "add_judge",
            "args": {"entry_id": "e1", "judge": {"name": "n", "body": "b", "severity": "nope"}},
        },
    )
    assert r.status_code == 400

    r = client.post(
        "/builder/op",
        json={**s, "op": "remove_judge", "args": {"entry_id": "e1", "name": "tone"}},
    )
    assert r.status_code == 200
    entry = next(e for e in r.json()["draft"]["board"] if e["id"] == "e1")
    assert "judges" not in entry or entry["judges"] == []
    # Removing an absent judge is a no-op with a note, not an error.
    r = client.post(
        "/builder/op",
        json={**s, "op": "remove_judge", "args": {"entry_id": "e1", "name": "tone"}},
    )
    assert r.status_code == 200
    assert "no judge named" in r.json()["patch"]["note"]
    # Unknown entry IS an error.
    r = client.post(
        "/builder/op",
        json={**s, "op": "remove_judge", "args": {"entry_id": "ghost", "name": "tone"}},
    )
    assert r.status_code == 400


def test_builder_op_remove_board_entry_dispatch(client: TestClient) -> None:
    s = {"session": "rmv"}
    r = client.post(
        "/builder/op", json={**s, "op": "remove_board_entry", "args": {"entry_id": "e2"}}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["patch"]["changed"] == {"entry_id": "e2", "action": "removed"}
    assert {e["id"] for e in body["draft"]["board"]} == {"e1"}
    assert "board" in body["diff"]["changed_components"]
    # Unknown id is a 400.
    r = client.post(
        "/builder/op", json={**s, "op": "remove_board_entry", "args": {"entry_id": "ghost"}}
    )
    assert r.status_code == 400
    assert "ghost" in r.json()["error"]


def test_builder_op_add_board_entry_dispatch(client: TestClient) -> None:
    s = {"session": "addbe"}
    r = client.post(
        "/builder/op",
        json={
            **s,
            "op": "add_board_entry",
            "args": {
                "entry": {
                    "id": "n7",
                    "kind": "single_turn",
                    "wall_clock_budget_seconds": 30,
                    "input": "new probe",
                    "context": {"provenance": '{"miner_version": "eval-synth/1"}'},
                }
            },
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["patch"]["changed"] == {"entry_id": "n7", "action": "added"}
    assert {e["id"] for e in body["draft"]["board"]} == {"e1", "e2", "n7"}
    assert "board" in body["diff"]["changed_components"]
    # A duplicate id is a 400 (the strict-ADD refusal).
    r = client.post(
        "/builder/op",
        json={
            **s,
            "op": "add_board_entry",
            "args": {
                "entry": {
                    "id": "e1",
                    "kind": "single_turn",
                    "wall_clock_budget_seconds": 30,
                    "input": "dup",
                }
            },
        },
    )
    assert r.status_code == 400
    assert "already exists" in r.json()["error"]


def test_builder_suggestions_endpoint_honest_empty(client: TestClient) -> None:
    # A workspace with an epoch but no reflection/suggestions degrades honestly.
    r = client.get("/builder/suggestions")
    assert r.status_code == 200
    body = r.json()
    assert body["suggestions"] == []
    assert "epoch_id" in body and "reflection_id" in body


def test_builder_suggestions_feed_sees_mint_mode(client: TestClient, workspace: Path) -> None:
    # A mint-mode `reflect suggest` writes a reflection dir with ONLY a
    # suggestions.json (no plan.json). The plan.json-keyed reflection discovery
    # skips such dirs — the feed must scan suggestions.json directly.
    from zicato.epoch.lifecycle import current_epoch_id
    from zicato.reflection.suggestions import Suggestion, write_suggestions

    epoch_id = current_epoch_id(workspace)
    assert epoch_id
    sug = Suggestion(
        suggestion_id="sug-mint01",
        suggestion_type="regression_entry",
        artifact_kind="board_entry",
        subject="e1",
        summary="pin a regression",
        rationale="e1 failed",
        target_slice="train",
        draft_artifact={"id": "e1_regression", "kind": "single_turn"},
        proposed_op={"op": "add_board_entry", "args": {"entry": {}}},
    )
    write_suggestions(workspace, epoch_id, "refl-mint-only", [sug])

    r = client.get("/builder/suggestions")
    assert r.status_code == 200
    body = r.json()
    assert body["reflection_id"] == "refl-mint-only"
    assert [s["suggestion_id"] for s in body["suggestions"]] == ["sug-mint01"]


def test_builder_op_revert_to_live_restores_the_draft(client: TestClient) -> None:
    s = {"session": "rvt"}
    client.post("/builder/op", json={**s, "op": "set_structure", "args": {"structure": "swiss"}})
    client.post("/builder/op", json={**s, "op": "remove_board_entry", "args": {"entry_id": "e1"}})
    r = client.post("/builder/op", json={**s, "op": "revert_to_live", "args": {}})
    assert r.status_code == 200
    body = r.json()
    assert body["patch"]["op"] == "revert_to_live"
    assert {e["id"] for e in body["draft"]["board"]} == {"e1", "e2"}
    assert body["draft"]["scoring"]["tournament"]["structure"] == "gauntlet"
    assert body["diff"]["changed_components"] == []
    # A second revert is an honest no-op.
    r = client.post("/builder/op", json={**s, "op": "revert_to_live", "args": {}})
    assert "already matches" in r.json()["patch"]["note"]


def test_builder_op_undo_pops_edits_and_reports_empty(client: TestClient) -> None:
    s = {"session": "und"}
    client.post("/builder/op", json={**s, "op": "set_structure", "args": {"structure": "swiss"}})
    client.post(
        "/builder/op",
        json={**s, "op": "set_param", "args": {"key": "field_size", "value": 4}},
    )

    # Undo the field_size edit.
    r = client.post("/builder/op", json={**s, "op": "undo", "args": {}})
    assert r.status_code == 200
    body = r.json()
    assert body["patch"]["op"] == "undo"
    t = body["draft"]["scoring"]["tournament"]
    assert t["structure"] == "swiss"
    assert "field_size" not in (t.get("params") or {})

    # Undo the structure edit.
    r = client.post("/builder/op", json={**s, "op": "undo", "args": {}})
    assert r.json()["draft"]["scoring"]["tournament"]["structure"] == "gauntlet"

    # History exhausted: an honest note, never an error.
    r = client.post("/builder/op", json={**s, "op": "undo", "args": {}})
    assert r.status_code == 200
    assert r.json()["patch"]["note"] == "nothing to undo"


def test_builder_op_undo_covers_revert_to_live(client: TestClient) -> None:
    s = {"session": "und2"}
    client.post("/builder/op", json={**s, "op": "set_structure", "args": {"structure": "racing"}})
    client.post("/builder/op", json={**s, "op": "revert_to_live", "args": {}})
    # Undo brings the discarded pre-revert state back.
    r = client.post("/builder/op", json={**s, "op": "undo", "args": {}})
    assert r.json()["draft"]["scoring"]["tournament"]["structure"] == "racing"


# ---------------------------------------------------------------------------
# Read plumbing: the config vocab + the draft's proposer_dirs.
# ---------------------------------------------------------------------------


def test_builder_config_carries_server_derived_vocab(client: TestClient) -> None:
    resp = client.get("/builder/config")
    assert resp.status_code == 200
    vocab = resp.json()["vocab"]
    assert set(vocab) == {
        "kinds",
        "expectation_kinds",
        "reads",
        "judge_modes",
        "severities",
        "drift_kinds",
    }
    assert set(vocab["kinds"]) == {
        "single_turn",
        "multi_turn_scripted",
        "multi_turn_emulated",
        "synthetic_adversarial",
        "synthetic_clean",
    }
    assert set(vocab["expectation_kinds"]) == {
        "expected_text",
        "regex",
        "json_schema",
        "predicate",
        "rubric",
    }
    assert set(vocab["reads"]) == {"final_output", "conversation_end"}
    assert set(vocab["judge_modes"]) == {"inline", "python"}
    assert set(vocab["severities"]) == {"info", "warning", "critical"}
    # Server-derived from the registered drift-kind set, sorted.
    assert vocab["drift_kinds"] == sorted(vocab["drift_kinds"])
    assert "off_topic" in vocab["drift_kinds"]
    assert len(vocab["drift_kinds"]) >= 30


def test_builder_draft_carries_proposer_dirs(client: TestClient, tmp_path: Path) -> None:
    # No proposers/ dir yet: honest degrade to [].
    resp = client.get("/builder/draft?session=pd")
    assert resp.status_code == 200
    assert resp.json()["proposer_dirs"] == []

    base = tmp_path / "proposers"
    (base / "agentful").mkdir(parents=True)
    (base / "agentful" / "agent.py").write_text("# custom agent\n", encoding="utf-8")
    (base / "skillful" / "skills").mkdir(parents=True)
    (base / "not-a-proposer").mkdir()  # neither agent.py nor skills/
    (base / "loose-file.txt").write_text("ignored", encoding="utf-8")

    resp = client.get("/builder/draft?session=pd")
    dirs = resp.json()["proposer_dirs"]
    assert [d["name"] for d in dirs] == ["agentful", "skillful"]
    assert dirs[0]["path"].endswith("proposers/agentful")
    assert all(set(d) == {"name", "path"} for d in dirs)
