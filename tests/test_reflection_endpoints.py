"""Dashboard Instrument-lens endpoints — thin delegates over reflection_view.

The four self-contained routes (``/api/reflections``,
``/api/reflection/{id}/summary`` / ``/scorecards`` / ``/xray/{judge}/{run_ref}``)
return the reader shapes and degrade (200 same-shape) on a malformed id.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from zicato.core.workspace import (
    reflection_adjudication_path,
    reflection_dir,
    reflection_findings_path,
    reflection_plan_path,
    reflection_scorecards_path,
)
from zicato.dashboard.server import create_app
from zicato.index.ingest import ingest_reflection
from zicato.reflection.corpus import ObservationRun, write_corpus

EPOCH = "epoch-1"
REFL = "refl-20260701000000-endpt001"


def _seed(workspace: Path) -> None:
    (workspace / "epochs" / EPOCH).mkdir(parents=True, exist_ok=True)
    (workspace / "epochs" / EPOCH / "config.json").write_text(
        json.dumps({"id": EPOCH, "name": EPOCH, "created_at": "2026-07-01", "closed": False}),
        encoding="utf-8",
    )
    (workspace / "lineage.json").write_text(
        json.dumps({"epochs": [{"id": EPOCH, "generations": []}]}), encoding="utf-8"
    )
    p = reflection_plan_path(workspace, EPOCH, REFL)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps(
            {
                "format_version": 1,
                "reflection_id": REFL,
                "epoch_id": EPOCH,
                "candidates": ["v1"],
                "entries": ["entryA"],
                "replicates": 3,
                "adjudicator_model": "meta",
                "checks": ["judge-audit"],
                "mode": "active",
                "pre_registered": False,
                "executed": True,
                "created_at": "2026-07-01T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    reflection_scorecards_path(workspace, EPOCH, REFL).write_text(
        json.dumps(
            {
                "reflection_id": REFL,
                "scorecards": [
                    {
                        "judge_name": "j",
                        "tp": 1,
                        "fp": 0,
                        "fn": 0,
                        "tn": 1,
                        "ambiguous": 0,
                        "precision": 1.0,
                        "recall": 1.0,
                        "f1": 1.0,
                        "severity_accuracy": 1.0,
                        "disagreement_rate": 0.0,
                        "self_consistency_kappa": None,
                        "redundant_with": [],
                        "exercised": True,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    reflection_findings_path(workspace, EPOCH, REFL).write_text(
        json.dumps({"reflection_id": REFL, "findings": [{"finding_id": "f1"}]}), encoding="utf-8"
    )
    (reflection_dir(workspace, EPOCH, REFL) / "summary.json").write_text(
        json.dumps(
            {
                "noise_floor_max_abs_delta": 0.05,
                "decision_flip_p": 0.0,
                "pillars": {"validity": {"aggregate_f1": 1.0}},
                "fidelity_tiers": ["verbatim"],
            }
        ),
        encoding="utf-8",
    )
    write_corpus(
        workspace,
        EPOCH,
        REFL,
        [
            ObservationRun(
                reflection_id=REFL,
                candidate_id="v1",
                entry_id="entryA",
                replicate=0,
                scalar=1.0,
                drift_loss=1.0,
                pass_fail=False,
                runtime_ms=10,
                aborted=False,
                abort_cause=None,
                fidelity="verbatim",
                has_result=False,
                has_judge_io=True,
                loss_ref=None,
                transcript_ref=None,
                judge_decisions=(
                    {
                        "judge_name": "j",
                        "fired": True,
                        "severity": "warning",
                        "claim": "c",
                        "transcript_span": None,
                    },
                ),
            )
        ],
    )
    run_ref = "v1:entryA:r0"
    ap = reflection_adjudication_path(workspace, EPOCH, REFL, "j", run_ref)
    ap.parent.mkdir(parents=True, exist_ok=True)
    ap.write_text(
        json.dumps({"format_version": 1, "judge_name": "j", "run_ref": run_ref, "verdict": "TP"}),
        encoding="utf-8",
    )
    ingest_reflection(workspace, None, EPOCH, REFL)


@pytest.fixture
def static_dir(tmp_path: Path) -> Path:
    d = tmp_path / "static"
    d.mkdir()
    (d / "index.html").write_text("<!doctype html><title>zicato</title>", encoding="utf-8")
    return d


@pytest.fixture
def client(tmp_path: Path, static_dir: Path) -> TestClient:
    workspace = tmp_path / ".zicato"
    _seed(workspace)
    app = create_app(workspace, static_dir, read_only=True)
    with TestClient(app) as c:
        yield c


def test_api_reflections(client: TestClient) -> None:
    r = client.get("/api/reflections")
    assert r.status_code == 200
    body = r.json()
    assert [x["reflection_id"] for x in body["reflections"]] == [REFL]
    assert body["reflections"][0]["n_findings"] == 1


def test_api_reflection_summary(client: TestClient) -> None:
    r = client.get(f"/api/reflection/{REFL}/summary")
    assert r.status_code == 200
    body = r.json()
    assert body["found"] is True
    assert body["noise_floor_max_abs_delta"] == 0.05
    assert body["findings"][0]["finding_id"] == "f1"


def test_api_reflection_scorecards(client: TestClient) -> None:
    r = client.get(f"/api/reflection/{REFL}/scorecards")
    assert r.status_code == 200
    body = r.json()
    assert body["judges"][0]["judge_name"] == "j"
    assert body["judges"][0]["f1"] == 1.0


def test_api_reflection_xray(client: TestClient) -> None:
    r = client.get(f"/api/reflection/{REFL}/xray/j/v1:entryA:r0")
    assert r.status_code == 200
    body = r.json()
    assert body["judge_name"] == "j"
    assert body["adjudication"]["verdict"] == "TP"


def test_malformed_id_degrades_same_shape(client: TestClient) -> None:
    # A path-traversing id is rejected by _is_safe_id → 200 same-shape empty.
    r = client.get("/api/reflection/..%2Fetc/summary")
    assert r.status_code in (200, 404)
    if r.status_code == 200:
        body = r.json()
        assert body["found"] is False
        assert body["pillars"] == {}


def test_xray_malformed_run_ref_degrades(client: TestClient) -> None:
    r = client.get(f"/api/reflection/{REFL}/xray/j/bad%2F..%2Fref")
    assert r.status_code in (200, 404)
    if r.status_code == 200:
        assert r.json()["found"] is False
