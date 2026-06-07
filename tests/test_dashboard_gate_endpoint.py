"""Tests for the structured promote-gate breakdown endpoint.

``GET /api/round/{epoch_id}/{champion}/{challenger}/gate`` decomposes the
authoritative :func:`zicato.tournament.gate.evaluate_gate` verdict into its
ordered rules. These tests build a minimal ``.zicato/`` workspace with the
on-disk ``scoring.json`` + per-generation ``gen_score.json`` aggregates the
reader consumes, then assert the endpoint agrees with what the gate would
decide — a promotion, a scalar-margin rejection, and a pass-rate
monotonicity rejection.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from zicato.dashboard.server import create_app
from zicato.dashboard.state_reader import WorkspacePaths, build_gate_breakdown

EPOCH_ID = "2026-05-28_e0"


def _write_json(path: Path, obj: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj), encoding="utf-8")


def _gen_score(
    *,
    scalar: float,
    pass_rate: float,
    per_entry: dict[str, dict[str, object]],
    namespace_aggregates: dict[str, float] | None = None,
    scalar_components: dict[str, float] | None = None,
) -> dict[str, object]:
    score: dict[str, object] = {
        "scalar": scalar,
        "pass_rate": pass_rate,
        "per_entry": per_entry,
        "scalar_components": scalar_components or {"drift": scalar, "pass": 0.0},
    }
    if namespace_aggregates is not None:
        score["namespace_aggregates"] = namespace_aggregates
    return score


def _make_workspace(
    tmp_path: Path,
    *,
    champion: dict[str, object],
    challenger: dict[str, object],
    scoring: dict[str, object] | None = None,
) -> Path:
    ws = tmp_path / ".zicato"
    epoch_dir = ws / "epochs" / EPOCH_ID
    (ws).mkdir(parents=True, exist_ok=True)
    (ws / "current_epoch").write_text(EPOCH_ID, encoding="utf-8")
    _write_json(epoch_dir / "scoring.json", scoring or {})
    _write_json(epoch_dir / "generations" / "v0" / "gen_score.json", champion)
    _write_json(epoch_dir / "generations" / "v1" / "gen_score.json", challenger)
    return ws


@pytest.fixture
def static_dir(tmp_path: Path) -> Path:
    d = tmp_path / "static"
    d.mkdir()
    (d / "index.html").write_text("<!doctype html><title>z</title>", encoding="utf-8")
    return d


def _client(ws: Path, static_dir: Path) -> TestClient:
    return TestClient(create_app(ws, static_dir, read_only=True))


# ---------------------------------------------------------------------------
# Promoted: every rule passes.
# ---------------------------------------------------------------------------


def test_gate_promoted_all_rules_pass(tmp_path: Path) -> None:
    champion = _gen_score(
        scalar=0.50,
        pass_rate=1.0,
        per_entry={"e1": {"drift_loss": 0.5, "pass_fail": True}},
        namespace_aggregates={"drift:": 0.5, "cost:": 0.2},
    )
    challenger = _gen_score(
        scalar=0.30,  # clear improvement, beats margin 0.01
        pass_rate=1.0,
        per_entry={"e1": {"drift_loss": 0.3, "pass_fail": True}},
        namespace_aggregates={"drift:": 0.3, "cost:": 0.1},
    )
    ws = _make_workspace(
        tmp_path,
        champion=champion,
        challenger=challenger,
        scoring={"promote_margin": 0.01, "pass_rate_monotonicity": True},
    )
    result = build_gate_breakdown(WorkspacePaths(ws), EPOCH_ID, "v0", "v1")

    assert result["decision"] == "promoted"
    assert result["reason"] == ""
    assert result["delta_scalar"] == pytest.approx(-0.20)
    rules = {r["id"]: r for r in result["rules"]}
    assert [r["id"] for r in result["rules"]] == [
        "regression_suite",
        "scalar_margin",
        "pass_rate_monotonicity",
        "namespace_monotonicity",
    ]
    # regression disabled by default -> skipped, not fired.
    assert rules["regression_suite"]["status"] == "skipped"
    assert rules["regression_suite"]["fired"] is False
    assert rules["scalar_margin"]["status"] == "pass"
    assert rules["scalar_margin"]["fired"] is False
    assert rules["pass_rate_monotonicity"]["status"] == "pass"
    # Default namespace_monotonicity has at least one tracked namespace; it
    # is satisfied here (challenger improved on every namespace).
    assert rules["namespace_monotonicity"]["status"] in {"pass", "disabled"}
    assert all(r["fired"] is False for r in result["rules"])


# ---------------------------------------------------------------------------
# Scalar-margin rejection: scalar fires, later rules not_reached.
# ---------------------------------------------------------------------------


def test_gate_scalar_margin_rejection(tmp_path: Path) -> None:
    champion = _gen_score(
        scalar=47.58,
        pass_rate=1.0,
        per_entry={"e1": {"drift_loss": 47.58, "pass_fail": True}},
        namespace_aggregates={"drift:": 47.58},
    )
    challenger = _gen_score(
        scalar=57.70,  # loss ROSE — regressed
        pass_rate=0.5,  # also a pass-rate regression, but scalar fires first
        per_entry={"e1": {"drift_loss": 57.70, "pass_fail": False}},
        namespace_aggregates={"drift:": 57.70},
    )
    ws = _make_workspace(
        tmp_path,
        champion=champion,
        challenger=challenger,
        scoring={"promote_margin": 0.01, "pass_rate_monotonicity": True},
    )
    result = build_gate_breakdown(WorkspacePaths(ws), EPOCH_ID, "v0", "v1")

    assert result["decision"] == "rejected"
    assert "challenger regressed" in result["reason"]
    assert result["delta_scalar"] == pytest.approx(10.12)
    rules = {r["id"]: r for r in result["rules"]}
    assert rules["scalar_margin"]["status"] == "fail"
    assert rules["scalar_margin"]["fired"] is True
    assert "47.58 → 57.70" in rules["scalar_margin"]["detail"]
    assert "+10.12" in rules["scalar_margin"]["detail"]
    # Rules AFTER the fired one are not_reached, never fired.
    assert rules["pass_rate_monotonicity"]["status"] == "not_reached"
    assert rules["pass_rate_monotonicity"]["fired"] is False
    assert rules["namespace_monotonicity"]["status"] == "not_reached"
    # Only one rule fired.
    assert sum(1 for r in result["rules"] if r["fired"]) == 1


# ---------------------------------------------------------------------------
# Pass-rate monotonicity rejection: scalar passes, monotonicity fires.
# ---------------------------------------------------------------------------


def test_gate_pass_rate_monotonicity_rejection(tmp_path: Path) -> None:
    # Challenger's scalar improves (so scalar_margin passes) but an entry
    # the champion passed now fails -> pass-rate monotonicity rejects.
    champion = _gen_score(
        scalar=0.50,
        pass_rate=1.0,
        per_entry={
            "entry_x": {"drift_loss": 0.5, "pass_fail": True},
            "entry_y": {"drift_loss": 0.5, "pass_fail": True},
        },
        namespace_aggregates={"drift:": 0.5},
    )
    challenger = _gen_score(
        scalar=0.20,  # improved scalar
        pass_rate=0.5,
        per_entry={
            "entry_x": {"drift_loss": 0.2, "pass_fail": False},  # regressed
            "entry_y": {"drift_loss": 0.2, "pass_fail": True},
        },
        namespace_aggregates={"drift:": 0.2},
    )
    ws = _make_workspace(
        tmp_path,
        champion=champion,
        challenger=challenger,
        scoring={"promote_margin": 0.01, "pass_rate_monotonicity": True},
    )
    result = build_gate_breakdown(WorkspacePaths(ws), EPOCH_ID, "v0", "v1")

    assert result["decision"] == "rejected"
    assert "pass-rate regression" in result["reason"]
    rules = {r["id"]: r for r in result["rules"]}
    # Scalar passed.
    assert rules["scalar_margin"]["status"] == "pass"
    assert rules["scalar_margin"]["fired"] is False
    # Pass-rate monotonicity fired.
    assert rules["pass_rate_monotonicity"]["status"] == "fail"
    assert rules["pass_rate_monotonicity"]["fired"] is True
    assert "entry_x" in rules["pass_rate_monotonicity"]["detail"]
    assert "entry_y" not in rules["pass_rate_monotonicity"]["detail"]
    # The later namespace rule is not reached.
    assert rules["namespace_monotonicity"]["status"] == "not_reached"


def test_gate_aggregate_scope_promotes_and_renders_rate_delta(tmp_path: Path) -> None:
    """Under aggregate scope (issue #17) a reshuffled-but-net-neutral
    challenger promotes, and the pass-rate rule renders the overall
    pass-rate delta rather than a per-entry regressed list."""
    champion = _gen_score(
        scalar=0.50,
        pass_rate=0.5,
        per_entry={
            "entry_x": {"drift_loss": 0.5, "pass_fail": True},
            "entry_y": {"drift_loss": 0.5, "pass_fail": False},
        },
        namespace_aggregates={"drift:": 0.5},
    )
    challenger = _gen_score(
        scalar=0.20,  # improved scalar
        pass_rate=0.5,  # net pass-rate held: x flipped off, y flipped on
        per_entry={
            "entry_x": {"drift_loss": 0.2, "pass_fail": False},
            "entry_y": {"drift_loss": 0.2, "pass_fail": True},
        },
        namespace_aggregates={"drift:": 0.2},
    )
    ws = _make_workspace(
        tmp_path,
        champion=champion,
        challenger=challenger,
        scoring={
            "promote_margin": 0.01,
            "pass_rate_monotonicity": True,
            "pass_rate_monotonicity_scope": "aggregate",
        },
    )
    result = build_gate_breakdown(WorkspacePaths(ws), EPOCH_ID, "v0", "v1")

    assert result["decision"] == "promoted"
    rules = {r["id"]: r for r in result["rules"]}
    pass_rule = rules["pass_rate_monotonicity"]
    assert pass_rule["status"] == "pass"
    assert pass_rule["fired"] is False
    # Aggregate detail mentions the overall pass-rate, NOT a regressed id list.
    assert "overall" in pass_rule["detail"]
    assert "aggregate scope" in pass_rule["detail"]
    assert "entry_x" not in pass_rule["detail"]


def test_gate_aggregate_scope_fires_on_net_regression(tmp_path: Path) -> None:
    """A genuine overall pass-rate drop under aggregate scope fires the rule
    and renders the pass-rate delta detail."""
    champion = _gen_score(
        scalar=0.50,
        pass_rate=1.0,
        per_entry={
            "entry_x": {"drift_loss": 0.5, "pass_fail": True},
            "entry_y": {"drift_loss": 0.5, "pass_fail": True},
        },
        namespace_aggregates={"drift:": 0.5},
    )
    challenger = _gen_score(
        scalar=0.20,  # improved scalar, but pass-rate fell 1.0 -> 0.5
        pass_rate=0.5,
        per_entry={
            "entry_x": {"drift_loss": 0.2, "pass_fail": False},
            "entry_y": {"drift_loss": 0.2, "pass_fail": True},
        },
        namespace_aggregates={"drift:": 0.2},
    )
    ws = _make_workspace(
        tmp_path,
        champion=champion,
        challenger=challenger,
        scoring={
            "promote_margin": 0.01,
            "pass_rate_monotonicity": True,
            "pass_rate_monotonicity_scope": "aggregate",
        },
    )
    result = build_gate_breakdown(WorkspacePaths(ws), EPOCH_ID, "v0", "v1")

    assert result["decision"] == "rejected"
    assert "overall pass-rate fell by" in result["reason"]
    rules = {r["id"]: r for r in result["rules"]}
    pass_rule = rules["pass_rate_monotonicity"]
    assert pass_rule["status"] == "fail"
    assert pass_rule["fired"] is True
    assert "overall" in pass_rule["detail"]
    # The later namespace rule is not reached.
    assert rules["namespace_monotonicity"]["status"] == "not_reached"


# ---------------------------------------------------------------------------
# Endpoint wiring + degradation.
# ---------------------------------------------------------------------------


def test_gate_endpoint_serves_breakdown(tmp_path: Path, static_dir: Path) -> None:
    champion = _gen_score(
        scalar=0.50,
        pass_rate=1.0,
        per_entry={"e1": {"drift_loss": 0.5, "pass_fail": True}},
        namespace_aggregates={"drift:": 0.5},
    )
    challenger = _gen_score(
        scalar=0.30,
        pass_rate=1.0,
        per_entry={"e1": {"drift_loss": 0.3, "pass_fail": True}},
        namespace_aggregates={"drift:": 0.3},
    )
    ws = _make_workspace(
        tmp_path,
        champion=champion,
        challenger=challenger,
        scoring={"promote_margin": 0.01},
    )
    with _client(ws, static_dir) as client:
        r = client.get(f"/api/round/{EPOCH_ID}/v0/v1/gate")
        assert r.status_code == 200
        body = r.json()
        assert body["decision"] == "promoted"
        assert [rule["id"] for rule in body["rules"]] == [
            "regression_suite",
            "scalar_margin",
            "pass_rate_monotonicity",
            "namespace_monotonicity",
        ]
        assert "champion" in body["scalar_components"]
        assert "primary_driver" in body


def test_gate_endpoint_invalid_id_degrades(tmp_path: Path, static_dir: Path) -> None:
    ws = _make_workspace(
        tmp_path,
        champion=_gen_score(scalar=0.5, pass_rate=1.0, per_entry={}),
        challenger=_gen_score(scalar=0.3, pass_rate=1.0, per_entry={}),
    )
    with _client(ws, static_dir) as client:
        r = client.get(f"/api/round/{EPOCH_ID}/bad%20id/v1/gate")
        assert r.status_code == 200
        body = r.json()
        assert body["decision"] == "deferred"
        assert body["rules"] == []


def test_gate_missing_aggregate_degrades_to_unknown(tmp_path: Path) -> None:
    # No gen_score.json for the challenger -> scalar unknown, no crash.
    ws = tmp_path / ".zicato"
    (ws / "epochs" / EPOCH_ID).mkdir(parents=True)
    (ws / "current_epoch").write_text(EPOCH_ID, encoding="utf-8")
    _write_json(ws / "epochs" / EPOCH_ID / "scoring.json", {})
    _write_json(
        ws / "epochs" / EPOCH_ID / "generations" / "v0" / "gen_score.json",
        _gen_score(scalar=0.5, pass_rate=1.0, per_entry={}),
    )
    result = build_gate_breakdown(WorkspacePaths(ws), EPOCH_ID, "v0", "v1")
    assert result["decision"] == "deferred"
    rules = {r["id"]: r for r in result["rules"]}
    assert rules["scalar_margin"]["status"] == "unknown"
    assert all(r["fired"] is False for r in result["rules"])
