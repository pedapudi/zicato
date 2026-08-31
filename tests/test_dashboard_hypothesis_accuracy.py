"""Tests for the hypothesis prediction-accuracy + calibration endpoints.

``GET /api/hypothesis-accuracy/{epoch}/{gen}`` projects one experiment's
falsifiable movement claims joined against the realised movements, lifting
the ``hypothesis_match`` verdict STAMPED at outcome-write time verbatim, so
the endpoint can never disagree with the outcome on disk.

``GET /api/calibration-trend[?epoch=<id>]`` walks the lineage and reports
the per-generation score fraction with rolling aggregates. It is purely
diagnostic and never feeds the gate.

The fixtures build REAL :class:`Experiment` objects and persist them with
the canonical :func:`zicato.epoch.journal.write_experiment`, so the readers
parse the exact on-disk shape the orchestrator writes, stamped
``hypothesis_match`` included.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from starlette.testclient import TestClient

from zicato.core import (
    DriftMovementActual,
    ExpectedDriftMovement,
    ExpectedMetricMovement,
    MetricMovementActual,
    OutcomeRecord,
)
from zicato.dashboard.server import create_app
from zicato.epoch.journal import write_experiment
from zicato.query import (
    WorkspacePaths,
    build_calibration_trend,
    build_hypothesis_accuracy,
)
from zicato.testing.fixtures import make_experiment, make_hypothesis_spec

EPOCH_ID = "2026-05-28_e0"


def _persist(
    ws: Path,
    *,
    generation_id: str,
    hypothesis,
    outcome: OutcomeRecord | None,
    round_index: int = 0,
    parent_generation_id: str = "v0",
) -> None:
    """Write one generation's experiment.json under ``ws`` via the canonical writer."""
    exp = make_experiment(
        id=f"exp_{generation_id}",
        epoch_id=EPOCH_ID,
        generation_id=generation_id,
        parent_generation_id=parent_generation_id,
        hypothesis=hypothesis,
        outcome=outcome,
        round_index=round_index,
    )
    write_experiment(ws, EPOCH_ID, generation_id, exp)


def _make_workspace(tmp_path: Path) -> Path:
    ws = tmp_path / ".zicato"
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "current_epoch").write_text(EPOCH_ID, encoding="utf-8")
    # An epoch config so the epoch resolves in list_epoch_ids ordering.
    epoch_dir = ws / "epochs" / EPOCH_ID
    epoch_dir.mkdir(parents=True, exist_ok=True)
    (epoch_dir / "config.json").write_text(
        '{"created_at": "2026-05-28T00:00:00Z"}', encoding="utf-8"
    )
    return ws


@pytest.fixture
def static_dir(tmp_path: Path) -> Path:
    d = tmp_path / "static"
    d.mkdir()
    (d / "index.html").write_text("<!doctype html><title>z</title>", encoding="utf-8")
    return d


def _client(ws: Path, static_dir: Path) -> TestClient:
    return TestClient(create_app(ws, static_dir, read_only=True))


def _outcome(
    *,
    drift_movements=(),
    metric_movements=(),
    pass_rate_delta: float = 0.0,
    decision: str = "promoted",
) -> OutcomeRecord:
    return OutcomeRecord(
        ran_at="2026-05-28T01:00:00Z",
        drift_movements=tuple(drift_movements),
        metric_movements=tuple(metric_movements),
        pass_rate_delta=pass_rate_delta,
        drift_loss_delta=-0.1,
        scalar_score_delta=-0.1,
        tournament_decision=decision,
    )


# ---------------------------------------------------------------------------
# hypothesis-accuracy: a hit + a miss, scorecard rollup.
# ---------------------------------------------------------------------------


def test_hypothesis_accuracy_hit_and_miss(tmp_path: Path) -> None:
    ws = _make_workspace(tmp_path)
    hypothesis = make_hypothesis_spec(
        expected_drift_movements=(
            ExpectedDriftMovement(kind="off_topic", direction="decrease", magnitude="small"),
            ExpectedDriftMovement(kind="hallucination", direction="decrease", magnitude="medium"),
        ),
        expected_pass_rate_delta="+0.05 to +0.10",
    )
    outcome = _outcome(
        drift_movements=(
            # Predicted decrease, realised decrease, stamped MATCH.
            DriftMovementActual(
                kind="off_topic", from_rate=0.40, to_rate=0.20, hypothesis_match=True, note="good"
            ),
            # Predicted decrease, realised increase, stamped MISS.
            DriftMovementActual(
                kind="hallucination", from_rate=0.10, to_rate=0.30, hypothesis_match=False
            ),
        ),
        pass_rate_delta=0.07,
    )
    _persist(ws, generation_id="v1", hypothesis=hypothesis, outcome=outcome)

    result = build_hypothesis_accuracy(WorkspacePaths(ws), EPOCH_ID, "v1")

    assert result["epoch_id"] == EPOCH_ID
    assert result["generation_id"] == "v1"
    claims = {c["target"]: c for c in result["claims"]}
    assert set(claims) == {"off_topic", "hallucination"}

    hit = claims["off_topic"]
    assert hit["kind"] == "drift"
    assert hit["predicted_direction"] == "decrease"
    assert hit["predicted_magnitude"] == "small"
    assert hit["from_rate"] == pytest.approx(0.40)
    assert hit["to_rate"] == pytest.approx(0.20)
    assert hit["observed_direction"] == "decrease"
    assert hit["signed_error"] == pytest.approx(-0.20)
    assert hit["hypothesis_match"] is True
    assert hit["unpredicted"] is False
    assert hit["note"] == "good"

    miss = claims["hallucination"]
    assert miss["observed_direction"] == "increase"
    assert miss["signed_error"] == pytest.approx(0.20)
    assert miss["hypothesis_match"] is False

    score = result["score"]
    assert score["hits"] == 1
    assert score["total"] == 2
    assert score["fraction"] == pytest.approx(0.5)
    assert score["brier"] is None

    assert result["pass_rate"] == {"predicted": "+0.05 to +0.10", "observed": pytest.approx(0.07)}


def test_hypothesis_accuracy_lifts_the_stamped_verdict_over_the_raw_rates(
    tmp_path: Path,
) -> None:
    """The endpoint reports the stamped verdict, never one re-derived from rates.

    The fixture stamps a MATCH on a movement whose realised rates run
    against the prediction (predicted a decrease, the rate rose). A reader
    that recomputed direction agreement from ``from_rate``/``to_rate``
    would call it a miss; lifting the stamped flag reports the match, which
    is what the persisted outcome says.
    """
    ws = _make_workspace(tmp_path)
    hypothesis = make_hypothesis_spec(
        expected_drift_movements=(
            ExpectedDriftMovement(kind="off_topic", direction="decrease", magnitude="small"),
            ExpectedDriftMovement(kind="verbosity", direction="increase", magnitude="large"),
        ),
    )
    outcome = _outcome(
        drift_movements=(
            DriftMovementActual(
                kind="off_topic", from_rate=0.40, to_rate=0.45, hypothesis_match=True
            ),
            DriftMovementActual(
                kind="verbosity", from_rate=0.10, to_rate=0.30, hypothesis_match=False
            ),
        ),
    )
    _persist(ws, generation_id="v1", hypothesis=hypothesis, outcome=outcome)

    result = build_hypothesis_accuracy(WorkspacePaths(ws), EPOCH_ID, "v1")
    claims = {c["target"]: c for c in result["claims"]}

    # The realised direction disagrees with the prediction on both claims;
    # only the stamped flag separates them.
    assert claims["off_topic"]["observed_direction"] == "increase"
    assert claims["off_topic"]["hypothesis_match"] is True
    assert claims["verbosity"]["observed_direction"] == "increase"
    assert claims["verbosity"]["hypothesis_match"] is False
    assert result["score"] == {"hits": 1, "total": 2, "fraction": 0.5, "brier": None}


def test_hypothesis_accuracy_metric_movements_and_unpredicted(tmp_path: Path) -> None:
    """Namespaced metric claims score; a realised-but-unpredicted row rides along."""
    ws = _make_workspace(tmp_path)
    hypothesis = make_hypothesis_spec(
        expected_drift_movements=(),
        expected_metric_movements=(
            ExpectedMetricMovement(
                metric_name="cost:tokens_spent", direction="decrease", magnitude="medium"
            ),
        ),
    )
    outcome = _outcome(
        metric_movements=(
            MetricMovementActual(
                metric_name="cost:tokens_spent",
                from_value=1200.0,
                to_value=900.0,
                hypothesis_match=True,
            ),
            # An UNPREDICTED realised movement: surfaced for context, NOT scored.
            MetricMovementActual(
                metric_name="latency:p95_turn_ms",
                from_value=800.0,
                to_value=850.0,
                hypothesis_match=False,
                note="flagged regression",
            ),
        ),
    )
    _persist(ws, generation_id="v1", hypothesis=hypothesis, outcome=outcome)

    result = build_hypothesis_accuracy(WorkspacePaths(ws), EPOCH_ID, "v1")
    claims = {c["target"]: c for c in result["claims"]}

    predicted = claims["cost:tokens_spent"]
    assert predicted["kind"] == "metric"
    assert predicted["unpredicted"] is False
    assert predicted["hypothesis_match"] is True

    unpredicted = claims["latency:p95_turn_ms"]
    assert unpredicted["unpredicted"] is True
    assert unpredicted["predicted_direction"] is None
    assert unpredicted["predicted_magnitude"] is None
    assert unpredicted["hypothesis_match"] is False
    assert unpredicted["note"] == "flagged regression"

    # Only the predicted claim counts toward the rollup.
    assert result["score"] == {
        "hits": 1,
        "total": 1,
        "fraction": pytest.approx(1.0),
        "brier": None,
    }


def test_hypothesis_accuracy_predicted_without_actual(tmp_path: Path) -> None:
    """A predicted movement the outcome never recorded -> null match, no hit."""
    ws = _make_workspace(tmp_path)
    hypothesis = make_hypothesis_spec(
        expected_drift_movements=(
            ExpectedDriftMovement(kind="off_topic", direction="decrease", magnitude="small"),
        ),
    )
    outcome = _outcome(drift_movements=())  # nothing realised for off_topic
    _persist(ws, generation_id="v1", hypothesis=hypothesis, outcome=outcome)

    result = build_hypothesis_accuracy(WorkspacePaths(ws), EPOCH_ID, "v1")
    claim = result["claims"][0]
    assert claim["target"] == "off_topic"
    assert claim["from_rate"] is None
    assert claim["to_rate"] is None
    assert claim["observed_direction"] is None
    assert claim["signed_error"] is None
    assert claim["hypothesis_match"] is None
    assert result["score"]["hits"] == 0
    assert result["score"]["total"] == 1
    assert result["score"]["fraction"] == pytest.approx(0.0)


def test_hypothesis_accuracy_no_claims(tmp_path: Path) -> None:
    """A hypothesis with no falsifiable movement claims -> empty claims, null fraction."""
    ws = _make_workspace(tmp_path)
    hypothesis = make_hypothesis_spec(
        expected_drift_movements=(), expected_pass_rate_delta="+0.00 to +0.05"
    )
    outcome = _outcome(pass_rate_delta=0.03)
    _persist(ws, generation_id="v1", hypothesis=hypothesis, outcome=outcome)

    result = build_hypothesis_accuracy(WorkspacePaths(ws), EPOCH_ID, "v1")
    assert result["claims"] == []
    assert result["score"]["total"] == 0
    assert result["score"]["fraction"] is None
    assert result["pass_rate"]["predicted"] == "+0.00 to +0.05"
    assert result["pass_rate"]["observed"] == pytest.approx(0.03)


def test_hypothesis_accuracy_missing_experiment(tmp_path: Path) -> None:
    """No experiment.json on disk -> empty scorecard, never raises."""
    ws = _make_workspace(tmp_path)
    result = build_hypothesis_accuracy(WorkspacePaths(ws), EPOCH_ID, "v404")
    assert result == {
        "epoch_id": EPOCH_ID,
        "generation_id": "v404",
        "claims": [],
        "score": {"hits": 0, "total": 0, "fraction": None, "brier": None},
        "pass_rate": {"predicted": "", "observed": None},
    }


# ---------------------------------------------------------------------------
# hypothesis-accuracy: HTTP endpoint.
# ---------------------------------------------------------------------------


def test_hypothesis_accuracy_endpoint(tmp_path: Path, static_dir: Path) -> None:
    ws = _make_workspace(tmp_path)
    hypothesis = make_hypothesis_spec(
        expected_drift_movements=(
            ExpectedDriftMovement(kind="off_topic", direction="decrease", magnitude="small"),
        ),
    )
    outcome = _outcome(
        drift_movements=(
            DriftMovementActual(
                kind="off_topic", from_rate=0.4, to_rate=0.2, hypothesis_match=True
            ),
        ),
    )
    _persist(ws, generation_id="v1", hypothesis=hypothesis, outcome=outcome)

    client = _client(ws, static_dir)
    resp = client.get(f"/api/hypothesis-accuracy/{EPOCH_ID}/v1")
    assert resp.status_code == 200
    body = resp.json()
    assert body["score"] == {
        "hits": 1,
        "total": 1,
        "fraction": 1.0,
        "brier": None,
    }
    assert body["claims"][0]["hypothesis_match"] is True


def test_hypothesis_accuracy_endpoint_unsafe_id(tmp_path: Path, static_dir: Path) -> None:
    ws = _make_workspace(tmp_path)
    client = _client(ws, static_dir)
    # A routable-but-unsafe id (a space the strict ``_SAFE_ID`` rejects)
    # exercises the handler's own guard: it degrades to an empty scorecard
    # at HTTP 200 rather than raising.
    resp = client.get(f"/api/hypothesis-accuracy/{EPOCH_ID}/bad%20id")
    assert resp.status_code == 200
    assert resp.json()["claims"] == []
    assert resp.json()["score"] == {"hits": 0, "total": 0, "fraction": None, "brier": None}


# ---------------------------------------------------------------------------
# calibration-trend.
# ---------------------------------------------------------------------------


def test_calibration_trend_over_lineage(tmp_path: Path) -> None:
    ws = _make_workspace(tmp_path)
    # v1: 1/1 = 1.0 ; v2: 0/2 = 0.0 ; v3: no claims -> null
    _persist(
        ws,
        generation_id="v1",
        round_index=0,
        hypothesis=make_hypothesis_spec(
            expected_drift_movements=(
                ExpectedDriftMovement(kind="off_topic", direction="decrease", magnitude="small"),
            ),
        ),
        outcome=_outcome(
            drift_movements=(
                DriftMovementActual(
                    kind="off_topic", from_rate=0.4, to_rate=0.2, hypothesis_match=True
                ),
            ),
            decision="promoted",
        ),
    )
    _persist(
        ws,
        generation_id="v2",
        round_index=1,
        parent_generation_id="v1",
        hypothesis=make_hypothesis_spec(
            expected_drift_movements=(
                ExpectedDriftMovement(kind="off_topic", direction="decrease", magnitude="small"),
                ExpectedDriftMovement(kind="verbosity", direction="increase", magnitude="large"),
            ),
        ),
        outcome=_outcome(
            drift_movements=(
                DriftMovementActual(
                    kind="off_topic", from_rate=0.2, to_rate=0.3, hypothesis_match=False
                ),
                DriftMovementActual(
                    kind="verbosity", from_rate=0.1, to_rate=0.1, hypothesis_match=False
                ),
            ),
            decision="rejected",
        ),
    )
    _persist(
        ws,
        generation_id="v3",
        round_index=2,
        parent_generation_id="v1",
        hypothesis=make_hypothesis_spec(expected_drift_movements=()),
        outcome=_outcome(decision="rejected"),
    )

    result = build_calibration_trend(WorkspacePaths(ws))
    assert result["epoch_id"] == EPOCH_ID
    points = {p["generation_id"]: p for p in result["points"]}
    assert [p["generation_id"] for p in result["points"]] == ["v1", "v2", "v3"]

    assert points["v1"]["score_fraction"] == pytest.approx(1.0)
    assert points["v1"]["total_claims"] == 1
    assert points["v1"]["round_index"] == 0
    assert points["v1"]["decision"] == "promoted"

    assert points["v2"]["score_fraction"] == pytest.approx(0.0)
    assert points["v2"]["total_claims"] == 2
    assert points["v2"]["decision"] == "rejected"

    assert points["v3"]["score_fraction"] is None
    assert points["v3"]["total_claims"] == 0

    # Rolling aggregates over the two scored points (1.0, 0.0).
    assert result["n_scored"] == 2
    assert result["rolling_mean"] == pytest.approx(0.5)
    assert result["latest_fraction"] == pytest.approx(0.0)
    # Latter half (0.0) < former half (1.0) -> regressing.
    assert result["trend_sign"] == -1


def test_calibration_trend_empty(tmp_path: Path) -> None:
    ws = _make_workspace(tmp_path)
    result = build_calibration_trend(WorkspacePaths(ws))
    assert result["points"] == []
    assert result["rolling_mean"] is None
    assert result["n_scored"] == 0
    assert result["latest_fraction"] is None
    assert result["trend_sign"] == 0


def test_calibration_trend_endpoint(tmp_path: Path, static_dir: Path) -> None:
    ws = _make_workspace(tmp_path)
    _persist(
        ws,
        generation_id="v1",
        hypothesis=make_hypothesis_spec(
            expected_drift_movements=(
                ExpectedDriftMovement(kind="off_topic", direction="decrease", magnitude="small"),
            ),
        ),
        outcome=_outcome(
            drift_movements=(
                DriftMovementActual(
                    kind="off_topic", from_rate=0.4, to_rate=0.2, hypothesis_match=True
                ),
            ),
        ),
    )
    client = _client(ws, static_dir)
    resp = client.get("/api/calibration-trend")
    assert resp.status_code == 200
    body = resp.json()
    assert body["epoch_id"] == EPOCH_ID
    assert body["n_scored"] == 1
    assert body["points"][0]["score_fraction"] == 1.0


def test_calibration_trend_endpoint_unknown_epoch(tmp_path: Path, static_dir: Path) -> None:
    ws = _make_workspace(tmp_path)
    client = _client(ws, static_dir)
    resp = client.get("/api/calibration-trend", params={"epoch": "no-such-epoch"})
    assert resp.status_code == 404
    assert resp.json() == {"error": "unknown epoch"}
