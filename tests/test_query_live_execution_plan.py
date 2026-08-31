"""The live execution plan — the running epoch's tree, and what runs in it.

``build_live_execution_plan`` serves the durable plan for the epoch the
heartbeat names plus a server-owned overlay: the liveness verdict, the
active path, and one node per still-beating ``active_runs`` record.

Two of these tests are the load-bearing ones. The first is the LIVENESS
GATE — a workspace whose runtime files froze months ago still holds a
mid-round phase and a directory of run records, and it must serve its
durable plan with an empty overlay rather than a tree of nodes reading
"running". The second is PLACEMENT: a record whose coordinates the plan
cannot confirm lands in the run-scope stage, because a unit rendered under
a round that never ran it is worse than one the plan admits it cannot
place.

Every fixture that must read LIVE is dated against the wall clock at build
time (the staleness window is 30s and the readers take their own clock), so
the tests state their freshness rather than inheriting it.
"""

from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from zicato.core.loss import LossProfile
from zicato.core.workspace import WorkspaceLayout, loss_profile_path
from zicato.dashboard.server import create_app
from zicato.epoch.preflight import PREFLIGHT_PHASE
from zicato.epoch.round_log import (
    HarnessLoaded,
    PatchesApplied,
    ProposalAttempted,
    RoundLog,
    RoundOpened,
)
from zicato.query import WorkspacePaths, build_live_execution_plan
from zicato.query.contracts import ENDPOINT_PAYLOADS
from zicato.query.execution_plan import build_execution_plan_model
from zicato.telemetry import reducer
from zicato.tournament.calibration import CALIBRATION_PHASE

EPOCH = "2026-08-20_live"
ENTRIES = ("login", "search")
LIVE_ROUTE = "/api/live/execution-plan"

#: A mid-round phase: the tournament is running round 0.
RUNNING_PHASE = "tournament:round_0:rung0_m0"


# ---------------------------------------------------------------------------
# Fixture workspace
# ---------------------------------------------------------------------------


def _iso(ts: _dt.datetime) -> str:
    return ts.isoformat().replace("+00:00", "Z")


def _workspace(tmp_path: Path) -> Path:
    root = tmp_path / ".zicato"
    layout = WorkspaceLayout.from_root(root)
    layout.epoch_dir(EPOCH).mkdir(parents=True, exist_ok=True)
    (root / "runtime" / "active_runs").mkdir(parents=True, exist_ok=True)
    (root / "current_epoch").write_text(EPOCH, encoding="utf-8")
    layout.epoch_config(EPOCH).write_text(
        json.dumps({"id": EPOCH, "created_at": "2026-08-20T00:00:00Z"}), encoding="utf-8"
    )
    layout.board(EPOCH).write_text(
        "\n".join(json.dumps({"id": entry, "input": "go"}) for entry in ENTRIES) + "\n",
        encoding="utf-8",
    )
    return root


def _write_loss(root: Path, generation_id: str, entry_id: str, *, replicate: int = 0) -> None:
    """One settled draw, exactly as the worker writes it."""
    profile = LossProfile(
        run_id=f"{generation_id}:{entry_id}:r{replicate}",
        entry_id=entry_id,
        generation_id=generation_id,
        epoch_id=EPOCH,
        drift_counts=(),
        plan_revisions=0,
        task_failure_ratio=0.0,
        runtime_ms=1200,
        wall_clock_budget_exceeded=False,
        expectation_result=None,
        drift_loss=0.25,
        pass_fail=True,
        match_id="rung0_m0",
        started_at="2026-08-20T00:01:00Z",
        ended_at="2026-08-20T00:01:01Z",
    )
    base = loss_profile_path(root, EPOCH, generation_id, entry_id)
    target = base if replicate == 0 else base.with_name(f"loss.r{replicate}.json")
    reducer.write_loss_profile(profile, target)


def _open_round(root: Path, index: int = 0, *, challenger: str = "v1") -> None:
    """A round that opened, proposed and applied — and has not closed."""
    log = RoundLog(root, EPOCH, index)
    log.append(RoundOpened(contract_hash="hash-1"))
    log.append(ProposalAttempted(errors=(), slot_index=0))
    log.append(PatchesApplied(generation_id=challenger))
    log.append(HarnessLoaded(generation_id=challenger, entrypoint_file="agent.py"))


def _heartbeat(root: Path, *, age_s: float, phase: str, round_index: int | None = 0) -> None:
    beat = _dt.datetime.now(_dt.UTC) - _dt.timedelta(seconds=age_s)
    record: dict[str, Any] = {
        "pid": 1,
        "instance_id": "default",
        "started_at": _iso(beat - _dt.timedelta(hours=1)),
        "last_heartbeat": _iso(beat),
        "epoch_id": EPOCH,
        "generation_id": "v1",
        "phase": phase,
    }
    if round_index is not None:
        record["round_index"] = round_index
    (root / "runtime" / "heartbeat.json").write_text(json.dumps(record), encoding="utf-8")


def _active_run(
    root: Path,
    *,
    run_id: str,
    entry_id: str,
    generation_id: str = "v1",
    epoch_id: str = EPOCH,
    age_s: float = 1.0,
) -> None:
    started = _dt.datetime.now(_dt.UTC) - _dt.timedelta(seconds=age_s)
    (root / "runtime" / "active_runs" / f"{run_id}.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "pid": 4242,
                "started_at": _iso(started),
                "last_progress": _iso(started),
                "wall_clock_budget_seconds": 180,
                "deadline": _iso(started + _dt.timedelta(seconds=180)),
                "events_jsonl_path": str(root / "events.jsonl"),
                "entry_id": entry_id,
                "generation_id": generation_id,
                "epoch_id": epoch_id,
            }
        ),
        encoding="utf-8",
    )


@pytest.fixture
def mid_round(tmp_path: Path) -> Path:
    """A live workspace: round 0 open, one draw landed, one entry executing."""
    root = _workspace(tmp_path)
    _open_round(root)
    _write_loss(root, "v1", "login")
    _heartbeat(root, age_s=1.0, phase=RUNNING_PHASE)
    _active_run(root, run_id="v1--search", entry_id="search")
    return root


def _plan(root: Path) -> dict[str, Any]:
    return build_live_execution_plan(WorkspacePaths(root))


def _walk(plan: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []

    def visit(node: dict[str, Any]) -> None:
        out.append(node)
        for child in node["children"]:
            visit(child)

    for stage in plan["stages"]:
        visit(stage)
    return out


def _node(plan: dict[str, Any], node_id: str) -> dict[str, Any]:
    return next(node for node in _walk(plan) if node["id"] == node_id)


# ---------------------------------------------------------------------------
# The active path
# ---------------------------------------------------------------------------


def test_a_running_round_marks_its_stage_and_the_step_the_pipeline_names(
    mid_round: Path,
) -> None:
    """The chain the heartbeat points at — round, then the Run step."""
    plan = _plan(mid_round)
    assert plan["liveness"]["state"] == "live"
    assert plan["overlay"]["active_path"] == [
        f"e:{EPOCH}/round:0",
        f"e:{EPOCH}/round:0/run",
    ]
    assert _node(plan, f"e:{EPOCH}/round:0")["active"] is True
    assert _node(plan, f"e:{EPOCH}/round:0/run")["active"] is True
    # A sibling step the loop is not in stays inactive.
    assert _node(plan, f"e:{EPOCH}/round:0/gate")["active"] is False


def test_the_active_step_is_the_one_the_round_pipeline_serves(mid_round: Path) -> None:
    """The plan projects the stepper's verdict rather than re-decoding the phase.

    The pipeline reads this phase as ``gate`` — the evidence pre-gate's
    replicate audit — so the plan must mark Gate, not Run.
    """
    _heartbeat(mid_round, age_s=1.0, phase="tournament:round_0:bt-replicate-2")
    plan = _plan(mid_round)
    assert plan["overlay"]["active_path"][-1] == f"e:{EPOCH}/round:0/gate"


def test_an_unmappable_phase_marks_no_step(mid_round: Path) -> None:
    """A phase this build has no step for contributes no step.

    The round it names is still a stated fact — the heartbeat writes
    ``round_index`` on every beat — so the round stays marked; what the
    unreadable phase does not do is pick one of the five steps inside it.
    """
    _heartbeat(mid_round, age_s=1.0, phase="polishing_brass")
    plan = _plan(mid_round)
    assert plan["liveness"]["state"] == "live"
    assert plan["overlay"]["active_path"] == [f"e:{EPOCH}/round:0"]
    assert _node(plan, f"e:{EPOCH}/round:0/gate")["active"] is False
    assert _node(plan, f"e:{EPOCH}/round:0/propose")["active"] is False


def test_a_phase_naming_a_round_with_no_stage_marks_nothing(tmp_path: Path) -> None:
    """An active path must point at nodes the reader can open. A round the
    plan has no stage for contributes nothing, not a plausible id."""
    root = _workspace(tmp_path)
    _write_loss(root, "v0", "login")
    _heartbeat(root, age_s=1.0, phase="tournament:round_4:m0", round_index=4)
    plan = _plan(root)
    assert plan["liveness"]["state"] == "live"
    assert plan["overlay"]["round_index"] == 4
    assert plan["overlay"]["active_path"] == []


def test_a_phase_naming_a_round_but_no_step_marks_the_round_alone(mid_round: Path) -> None:
    """``round_index`` is a stated fact; the step is an inference the phase
    did not support. The round is marked, no step is."""
    _heartbeat(mid_round, age_s=1.0, phase="infra_backoff:round_0:5s")
    plan = _plan(mid_round)
    assert plan["overlay"]["active_path"] == [f"e:{EPOCH}/round:0"]


def test_the_calibrating_phase_marks_the_noise_floor_band(tmp_path: Path) -> None:
    """The epoch-open step maps to the band holding the draws it is taking."""
    root = _workspace(tmp_path)
    _write_loss(root, "v0", "login", replicate=1000)
    _heartbeat(root, age_s=1.0, phase=f"{CALIBRATION_PHASE}:3/18", round_index=None)
    _active_run(root, run_id="v0--login", entry_id="login", generation_id="v0")
    plan = _plan(root)
    assert plan["overlay"]["active_path"] == [
        f"e:{EPOCH}/baseline",
        f"e:{EPOCH}/baseline/band:calibration",
    ]
    assert _node(plan, f"e:{EPOCH}/baseline/band:calibration")["active"] is True


def test_the_contract_preflight_phase_marks_its_probe_band(tmp_path: Path) -> None:
    """The second epoch-open step maps the same way, through the same table.

    The pre-flight's probes are cached under the champion's own id, so the
    band a reader is sent to is the one that says the draws are deliberately
    degraded — which is the whole reason the path points at a band node
    rather than at the generation the probes were drawn from.
    """
    root = _workspace(tmp_path)
    _write_loss(root, "v0", "login", replicate=2000)
    _heartbeat(root, age_s=1.0, phase=f"{PREFLIGHT_PHASE}:3/12", round_index=None)
    plan = _plan(root)
    assert plan["overlay"]["active_path"] == [
        f"e:{EPOCH}/baseline",
        f"e:{EPOCH}/baseline/band:contract_preflight",
    ]
    assert _node(plan, f"e:{EPOCH}/baseline/band:contract_preflight")["active"] is True


def test_every_epoch_open_step_the_pipeline_reports_maps_to_a_band(tmp_path: Path) -> None:
    """No epoch-open step may be added without a band, or it marks nothing.

    The pipeline's step table and this module's band table are edited in
    different files by different changes, so the gap this closes is the one
    that opened when the pre-flight step landed with no band beside it.
    """
    from zicato.query.live_execution_plan import EPOCH_OPEN_STEP_BANDS
    from zicato.query.loop_view import _EPOCH_OPEN_STEPS
    from zicato.query.replicate_scores import measurement_bands

    assert set(_EPOCH_OPEN_STEPS) == set(EPOCH_OPEN_STEP_BANDS)
    known = {band.key for band in measurement_bands()}
    assert set(EPOCH_OPEN_STEP_BANDS.values()) <= known


def test_a_calibrating_phase_with_no_draws_yet_marks_nothing(tmp_path: Path) -> None:
    """A band step exists only once a draw has landed, and the active path
    points into the plan rather than at an id the plan does not hold."""
    root = _workspace(tmp_path)
    _write_loss(root, "v0", "login")
    _heartbeat(root, age_s=1.0, phase=f"{CALIBRATION_PHASE}:0/18", round_index=None)
    assert _plan(root)["overlay"]["active_path"] == []


# ---------------------------------------------------------------------------
# In-flight work units
# ---------------------------------------------------------------------------


def test_a_beating_record_becomes_a_running_unit_beside_the_landed_draw(
    mid_round: Path,
) -> None:
    """The in-flight entry shows under the same sweep as the settled one."""
    plan = _plan(mid_round)
    sweep = _node(plan, f"e:{EPOCH}/round:0/run/v1")
    ids = [child["id"] for child in sweep["children"]]
    assert ids == [
        f"e:{EPOCH}/round:0/run/v1/login/r0",
        f"e:{EPOCH}/round:0/run/v1/search/run:v1--search",
    ]
    running = sweep["children"][1]
    assert running["kind"] == "board_entry_run"
    assert running["status"] == "running"
    assert running["active"] is True
    # The replicate slot is not recorded until the draw lands, so the node
    # neither claims one nor reads exact.
    assert running["coordinates"]["replicate"] is None
    assert running["provenance"] == "partial"
    assert running["outcome"]["run_id"] == "v1--search"
    assert plan["overlay"]["placed"] == 1
    assert plan["overlay"]["in_flight"] == 1


def test_a_candidate_with_no_landed_draw_still_has_its_round_s_sweep(
    tmp_path: Path,
) -> None:
    """The durable sweep is drawn from the round log, not from the loss files,
    so a candidate whose first entry is still executing already has one. The
    running unit goes into it, and the sweep stops reading ``planned``."""
    root = _workspace(tmp_path)
    _open_round(root)
    _heartbeat(root, age_s=1.0, phase=RUNNING_PHASE)
    _active_run(root, run_id="v1--login", entry_id="login")
    plan = _plan(root)
    sweep = _node(plan, f"e:{EPOCH}/round:0/run/v1")
    assert [child["id"] for child in sweep["children"]] == [
        f"e:{EPOCH}/round:0/run/v1/login/run:v1--login"
    ]
    assert sweep["status"] == "running"
    assert _node(plan, f"e:{EPOCH}/round:0/run")["status"] == "running"
    # A step the loop has not reached is still planned — only nodes ABOVE
    # in-flight work are promoted.
    assert _node(plan, f"e:{EPOCH}/round:0/gate")["status"] == "planned"
    assert plan["overlay"]["placed"] == 1
    assert plan["overlay"]["unplaced"] == 0


def test_a_record_no_round_claims_lands_in_run_scope(mid_round: Path) -> None:
    """A generation the round log never applied is not guessed into the round."""
    _active_run(mid_round, run_id="v9--login", entry_id="login", generation_id="v9")
    plan = _plan(mid_round)
    scope = _node(plan, f"e:{EPOCH}/run-scope")
    assert scope["kind"] == "run_scope"
    assert [child["id"] for child in scope["children"]] == [
        f"e:{EPOCH}/run-scope/v9/login/run:v9--login"
    ]
    assert plan["overlay"]["unplaced"] == 1
    assert plan["overlay"]["placed"] == 1
    # Nothing about v9 appears under a round.
    assert not any(
        node["coordinates"].get("generation_id") == "v9"
        for node in _walk(plan)
        if node["id"].startswith(f"e:{EPOCH}/round:")
    )


def test_a_record_from_another_epoch_is_counted_and_not_placed(mid_round: Path) -> None:
    """It is running — the tally says so — but this plan describes another epoch."""
    _active_run(mid_round, run_id="other--login", entry_id="login", epoch_id="2026-01-01_elsewhere")
    plan = _plan(mid_round)
    assert plan["overlay"]["other_epoch"] == 1
    assert plan["overlay"]["in_flight"] == 2
    assert plan["overlay"]["placed"] == 1
    assert not any(node["id"].endswith("run:other--login") for node in _walk(plan))


def test_a_stale_record_is_not_in_flight(mid_round: Path) -> None:
    """The overlay reads the server's own per-record verdict, so a record
    whose worker stopped beating leaves the tree as well as the tally."""
    _active_run(mid_round, run_id="v1--old", entry_id="login", age_s=86_400.0)
    plan = _plan(mid_round)
    assert plan["overlay"]["in_flight"] == 1
    assert not any(node["id"].endswith("run:v1--old") for node in _walk(plan))


# ---------------------------------------------------------------------------
# The liveness gate
# ---------------------------------------------------------------------------


def test_a_dead_workspace_serves_the_durable_plan_with_an_empty_overlay(
    mid_round: Path,
) -> None:
    """The June shape: every runtime file still says "running" and nothing is.

    The plan itself is unchanged — a post-mortem read must still show what
    the epoch did — but no node is active and no record is placed.
    """
    _heartbeat(mid_round, age_s=86_400.0, phase=RUNNING_PHASE)
    _active_run(mid_round, run_id="v1--search", entry_id="search", age_s=86_400.0)
    plan = _plan(mid_round)
    assert plan["liveness"]["state"] == "interrupted"
    assert plan["overlay"]["note"]
    assert plan["overlay"]["active_path"] == []
    assert plan["overlay"]["placed"] == 0
    assert not any(node["active"] for node in _walk(plan))
    units = [node for node in _walk(plan) if node["kind"] == "board_entry_run"]
    assert not any(node["status"] == "running" for node in units)
    # The durable spine is still there.
    assert _node(plan, f"e:{EPOCH}/round:0/run/v1/login/r0")["status"] == "done"


def test_a_never_run_workspace_is_settled_with_an_empty_overlay(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    plan = _plan(root)
    assert plan["liveness"]["state"] == "settled"
    assert plan["overlay"]["in_flight"] == 0
    assert plan["overlay"]["active_path"] == []


# ---------------------------------------------------------------------------
# Shape, degrade, and the wire
# ---------------------------------------------------------------------------


def test_every_node_carries_the_active_flag(mid_round: Path) -> None:
    """Present on every node, both tenses — a flag that appears only when true
    cannot be told apart from a server that does not send it."""
    nodes = _walk(_plan(mid_round))
    assert nodes
    assert all(isinstance(node["active"], bool) for node in nodes)


def test_rendering_the_live_overlay_does_not_mutate_the_durable_plan(
    mid_round: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The two endpoints share one model without sharing mutable wire data."""
    import zicato.query.live_execution_plan as live_plan

    model = build_execution_plan_model(WorkspacePaths(mid_round), EPOCH)
    durable_before = model.payload()
    monkeypatch.setattr(live_plan, "build_execution_plan_model", lambda *_args: model)

    rendered = live_plan.build_live_execution_plan(WorkspacePaths(mid_round))

    assert rendered["overlay"]["placed"] == 1
    assert model.payload() == durable_before
    assert all("active" not in node for node in _walk(durable_before))


@pytest.mark.parametrize(
    "removed",
    ["rounds", "runs", "board.jsonl", "runtime"],
)
def test_every_missing_input_degrades_to_the_response_shape(mid_round: Path, removed: str) -> None:
    """DQ3: each input can vanish and the response keeps its keys."""
    import shutil

    layout = WorkspaceLayout.from_root(mid_round)
    targets = {
        "rounds": layout.epoch_dir(EPOCH) / "rounds",
        "runs": layout.generations_dir(EPOCH),
        "board.jsonl": layout.board(EPOCH),
        "runtime": mid_round / "runtime",
    }
    target = targets[removed]
    if target.is_dir():
        shutil.rmtree(target)
    elif target.exists():
        target.unlink()
    plan = _plan(mid_round)
    for key in ("epoch_id", "generated_at", "board", "note", "stages", "liveness", "overlay"):
        assert key in plan, f"{removed}: missing {key}"
    for key in ("in_flight", "placed", "unplaced", "other_epoch", "active_path", "note"):
        assert key in plan["overlay"], f"{removed}: missing overlay.{key}"


def test_the_route_declares_a_payload_contract() -> None:
    assert LIVE_ROUTE in ENDPOINT_PAYLOADS


def test_the_endpoint_serves_the_live_plan(mid_round: Path, tmp_path: Path) -> None:
    static = tmp_path / "static"
    static.mkdir(exist_ok=True)
    response = TestClient(create_app(mid_round, static)).get(LIVE_ROUTE)
    assert response.status_code == 200
    body = response.json()
    assert body["epoch_id"] == EPOCH
    assert body["liveness"]["state"] == "live"
    assert body["overlay"]["active_path"][0] == f"e:{EPOCH}/round:0"
