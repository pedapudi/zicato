"""The live pipeline: where the running epoch's loop is.

``build_live_pipeline`` serves the round pipeline's verdict to
``/api/live/pipeline`` and degrades to an empty object when the phase
cannot be read.

The fixture that must read LIVE is dated against the wall clock at build
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

import zicato.query.live_execution_plan as live_plan
from zicato.core.loss import LossProfile
from zicato.core.measurement import (
    TOURNAMENT_DRAW,
    MeasurementDraw,
    measurement_artifact_path,
)
from zicato.core.workspace import loss_profile_path, run_id_for_unit
from zicato.dashboard.server import create_app
from zicato.epoch.round_log import (
    HarnessLoaded,
    PatchesApplied,
    ProposalAttempted,
    RoundLog,
    RoundOpened,
)
from zicato.query import WorkspacePaths
from zicato.query.contracts import ENDPOINT_PAYLOADS
from zicato.telemetry import reducer
from zicato.workspace import WorkspaceLayout

EPOCH = "2026-08-20_live"
ENTRIES = ("login", "search")

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
        "\n".join(
            json.dumps(
                {"id": entry, "kind": "single_turn", "input": "go", "wall_clock_budget_seconds": 1}
            )
            for entry in ENTRIES
        )
        + "\n",
        encoding="utf-8",
    )
    return root


def _write_loss(
    root: Path,
    generation_id: str,
    entry_id: str,
    *,
    replicate: MeasurementDraw = TOURNAMENT_DRAW,
) -> None:
    """One settled draw, exactly as the worker writes it."""
    profile = LossProfile(
        run_id=run_id_for_unit(generation_id, entry_id, replicate, epoch_id=EPOCH),
        entry_id=entry_id,
        generation_id=generation_id,
        epoch_id=EPOCH,
        metric_counts=(),
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
        measurement=replicate,
    )
    base = loss_profile_path(root, EPOCH, generation_id, entry_id)
    target = measurement_artifact_path(base.parent.parent, "loss", replicate)
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


PIPELINE_ROUTE = "/api/live/pipeline"


def test_a_phase_that_cannot_be_read_serves_an_empty_pipeline(
    mid_round: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without the phase there is no live verdict to serve."""

    def _explode(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("phase unreadable")

    monkeypatch.setattr(live_plan, "build_round_pipeline", _explode)

    assert live_plan.build_live_pipeline(WorkspacePaths(mid_round)) == {}


def test_the_pipeline_endpoint_serves_the_projection(mid_round: Path, tmp_path: Path) -> None:
    static = tmp_path / "static"
    static.mkdir(exist_ok=True)
    response = TestClient(create_app(mid_round, static)).get(PIPELINE_ROUTE)

    assert response.status_code == 200
    body = response.json()
    assert body["epoch_id"] == EPOCH
    assert body["phase"] == RUNNING_PHASE
    assert body["liveness"]["state"] == "live"
    assert [step["id"] for step in body["steps"]] == ["propose", "apply", "run", "gate"]


def test_the_pipeline_route_declares_a_payload_contract() -> None:
    assert PIPELINE_ROUTE in ENDPOINT_PAYLOADS
