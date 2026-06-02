"""Phase-1.5 dashboard cleanup tests.

These exercise the readers and endpoints that fill three gaps still
showing placeholder copy after Phase-1:

* ``build_expectation_outcomes_for_run`` — projects the reducer's
  ``expectation_result`` block off ``loss.json`` into a stable
  list-of-outcomes shape the L4 view renders as a table.
* ``build_run_header`` — projects ``loss.json``'s numeric / verdict
  header fields so the L4 run page can display runtime, tokens, output
  chars, turns, plan revisions and the budget-exceeded flag for a
  completed run.
* Endpoint routes for the two new readers.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from zicato.dashboard.server import create_app
from zicato.dashboard.state_reader import (
    WorkspacePaths,
    build_expectation_outcomes_for_run,
    build_run_header,
)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _write_json(path: Path, obj: object) -> None:
    _write(path, json.dumps(obj))


@pytest.fixture
def phase15_workspace(tmp_path: Path) -> Path:
    """Minimal workspace with three runs exercising the loss.json fields.

    * ``predicate_fail`` — failed predicate expectation, full numeric
      header (runtime, tokens, output_chars, turns, plan revisions).
    * ``rubric_pass`` — passing rubric expectation with judge_name +
      score, multi-turn header (turns_completed set).
    * ``no_expectation`` — loss.json carries no ``expectation_result``
      so the outcomes list is empty.
    """
    ws = tmp_path / ".zicato"
    (ws / "runtime").mkdir(parents=True)

    epoch_id = "2026-05-20_e0"
    gen_id = "v3"
    _write(ws / "current_epoch", epoch_id)
    epoch_dir = ws / "epochs" / epoch_id
    gen_dir = epoch_dir / "generations" / gen_id
    runs_dir = gen_dir / "runs"
    runs_dir.mkdir(parents=True)

    # Run 1 — predicate failure with full header metrics.
    _write_json(
        runs_dir / "predicate_fail" / "loss.json",
        {
            "run_id": "run_predicate_fail",
            "entry_id": "predicate_fail",
            "epoch_id": epoch_id,
            "generation_id": gen_id,
            "expectation_result": {
                "kind": "predicate",
                "passed": False,
                "detail": "predicate returned False",
            },
            "drift_loss": 0.5,
            "pass_fail": False,
            "runtime_ms": 83160,
            "tokens_spent": 12345,
            "output_chars": 5456,
            "turns_completed": None,
            "plan_revisions": 1,
            "wall_clock_budget_exceeded": False,
        },
    )

    # Run 2 — rubric pass with judge_name + score; multi-turn turns
    # counter set; budget exceeded flag set so the renderer can surface
    # the aborted-by-budget signal.
    _write_json(
        runs_dir / "rubric_pass" / "loss.json",
        {
            "run_id": "run_rubric_pass",
            "entry_id": "rubric_pass",
            "epoch_id": epoch_id,
            "generation_id": gen_id,
            "expectation_result": {
                "kind": "rubric",
                "passed": True,
                "detail": "scored above threshold",
                "judge_name": "presentation_quality",
                "score": 0.875,
            },
            "drift_loss": 0.0,
            "pass_fail": True,
            "runtime_ms": 360000,
            "tokens_spent": 0,
            "output_chars": 13726,
            "turns_completed": 6,
            "plan_revisions": 4,
            "wall_clock_budget_exceeded": True,
        },
    )

    # Run 3 — no expectation attached (entries without ground truth).
    _write_json(
        runs_dir / "no_expectation" / "loss.json",
        {
            "run_id": "run_no_expectation",
            "entry_id": "no_expectation",
            "epoch_id": epoch_id,
            "generation_id": gen_id,
            "drift_loss": 1.2,
            "pass_fail": None,
            "runtime_ms": 42000,
            "tokens_spent": 100,
            "output_chars": 800,
            "turns_completed": None,
            "plan_revisions": 0,
            "wall_clock_budget_exceeded": False,
        },
    )
    return ws


@pytest.fixture
def static_dir(tmp_path: Path) -> Path:
    d = tmp_path / "static_phase15"
    d.mkdir()
    (d / "index.html").write_text("<!doctype html><title>zicato</title>", encoding="utf-8")
    (d / "app_T.js").write_text("// app", encoding="utf-8")
    return d


@pytest.fixture
def phase15_client(phase15_workspace: Path, static_dir: Path) -> TestClient:
    app = create_app(phase15_workspace, static_dir, read_only=True)
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# build_expectation_outcomes_for_run
# ---------------------------------------------------------------------------


def test_expectation_outcomes_predicate_fail(phase15_workspace: Path) -> None:
    payload = build_expectation_outcomes_for_run(
        WorkspacePaths(phase15_workspace),
        "2026-05-20_e0",
        "v3",
        "predicate_fail",
    )
    assert payload["entry_id"] == "predicate_fail"
    outcomes = payload["outcomes"]
    assert len(outcomes) == 1
    o = outcomes[0]
    assert o["kind"] == "predicate"
    assert o["passed"] is False
    assert o["detail"] == "predicate returned False"
    # Predicate kind has no judge or score.
    assert o["judge_name"] is None
    assert o["score"] is None


def test_expectation_outcomes_rubric_carries_judge_and_score(
    phase15_workspace: Path,
) -> None:
    payload = build_expectation_outcomes_for_run(
        WorkspacePaths(phase15_workspace),
        "2026-05-20_e0",
        "v3",
        "rubric_pass",
    )
    o = payload["outcomes"][0]
    assert o["kind"] == "rubric"
    assert o["passed"] is True
    assert o["judge_name"] == "presentation_quality"
    assert o["score"] == pytest.approx(0.875)


def test_expectation_outcomes_empty_when_loss_lacks_expectation(
    phase15_workspace: Path,
) -> None:
    payload = build_expectation_outcomes_for_run(
        WorkspacePaths(phase15_workspace),
        "2026-05-20_e0",
        "v3",
        "no_expectation",
    )
    # The reducer simply omits ``expectation_result`` when the entry had
    # no expectation; the reader returns an empty list, not an error.
    assert payload["outcomes"] == []


def test_expectation_outcomes_empty_when_loss_json_missing(tmp_path: Path) -> None:
    ws = tmp_path / ".zicato"
    (ws / "runtime").mkdir(parents=True)
    payload = build_expectation_outcomes_for_run(
        WorkspacePaths(ws), "fake_epoch", "v0", "missing_entry"
    )
    # Missing on-disk loss.json degrades to an empty outcomes list,
    # never an exception.
    assert payload["outcomes"] == []
    assert payload["entry_id"] == "missing_entry"


def test_expectation_outcomes_handles_list_shaped_legacy(tmp_path: Path) -> None:
    """A forward-compat shape where expectation_result is a list of dicts."""
    ws = tmp_path / ".zicato"
    (ws / "runtime").mkdir(parents=True)
    epoch_id = "fake_epoch"
    run_dir = ws / "epochs" / epoch_id / "generations" / "v0" / "runs" / "entry_x"
    run_dir.mkdir(parents=True)
    _write_json(
        run_dir / "loss.json",
        {
            "run_id": "r1",
            "entry_id": "entry_x",
            "expectation_result": [
                {"kind": "regex", "passed": True, "detail": ""},
                {"kind": "rubric", "passed": False, "judge_name": "j1", "score": 0.3},
            ],
        },
    )
    payload = build_expectation_outcomes_for_run(WorkspacePaths(ws), epoch_id, "v0", "entry_x")
    assert len(payload["outcomes"]) == 2
    assert payload["outcomes"][0]["kind"] == "regex"
    assert payload["outcomes"][1]["judge_name"] == "j1"


# ---------------------------------------------------------------------------
# build_run_header
# ---------------------------------------------------------------------------


def test_run_header_projects_numeric_fields(phase15_workspace: Path) -> None:
    header = build_run_header(
        WorkspacePaths(phase15_workspace),
        "2026-05-20_e0",
        "v3",
        "predicate_fail",
    )
    assert header["run_id"] == "run_predicate_fail"
    assert header["runtime_ms"] == 83160
    assert header["tokens_spent"] == 12345
    assert header["output_chars"] == 5456
    assert header["plan_revisions"] == 1
    assert header["wall_clock_budget_exceeded"] is False
    assert header["pass_fail"] is False
    assert header["drift_loss"] == pytest.approx(0.5)


def test_run_header_surfaces_budget_exceeded(phase15_workspace: Path) -> None:
    header = build_run_header(
        WorkspacePaths(phase15_workspace),
        "2026-05-20_e0",
        "v3",
        "rubric_pass",
    )
    assert header["wall_clock_budget_exceeded"] is True
    assert header["turns_completed"] == 6
    assert header["pass_fail"] is True


def test_run_header_degrades_when_loss_json_missing(tmp_path: Path) -> None:
    ws = tmp_path / ".zicato"
    (ws / "runtime").mkdir(parents=True)
    header = build_run_header(WorkspacePaths(ws), "missing_epoch", "v0", "missing_entry")
    # Every numeric field is ``None`` — the shape is stable so the L4
    # renderer never branches on whether the file exists.
    for key in (
        "drift_loss",
        "pass_fail",
        "runtime_ms",
        "tokens_spent",
        "output_chars",
        "turns_completed",
        "plan_revisions",
        "wall_clock_budget_exceeded",
        "run_id",
        "adk_session_id",
    ):
        assert header[key] is None


def test_run_header_surfaces_adk_session_id(tmp_path: Path) -> None:
    """``adk_session_id`` flows from ``loss.json`` into the header so the
    L4 renderer can deep-link into harmonograf without a second roundtrip
    to ``events.jsonl`` (which the SSE hot path forbids touching)."""
    ws = tmp_path / ".zicato"
    (ws / "runtime").mkdir(parents=True)
    epoch_id = "fake"
    run_dir = ws / "epochs" / epoch_id / "generations" / "v0" / "runs" / "x"
    run_dir.mkdir(parents=True)
    _write_json(
        run_dir / "loss.json",
        {
            "run_id": "r1",
            "drift_loss": 0.1,
            "adk_session_id": "adk-session-abc-123",
        },
    )
    header = build_run_header(WorkspacePaths(ws), epoch_id, "v0", "x")
    assert header["adk_session_id"] == "adk-session-abc-123"
    assert header["run_id"] == "r1"


def test_run_header_skips_nested_loss_fields(tmp_path: Path) -> None:
    """Nested dict / list fields on loss.json never bleed into the header."""
    ws = tmp_path / ".zicato"
    (ws / "runtime").mkdir(parents=True)
    epoch_id = "fake"
    run_dir = ws / "epochs" / epoch_id / "generations" / "v0" / "runs" / "x"
    run_dir.mkdir(parents=True)
    _write_json(
        run_dir / "loss.json",
        {
            "run_id": "r1",
            "drift_loss": 0.1,
            "drift_counts": [{"kind": "off_topic", "count": 3}],  # nested list
            "metric_counts": [{"name": "cost", "count": 1}],
            "per_judge_loss": [],
        },
    )
    header = build_run_header(WorkspacePaths(ws), epoch_id, "v0", "x")
    # ``drift_counts``/``metric_counts`` are not in the header key list,
    # but a careless implementation that copied through every key would
    # leak the nested list. Ensure only the known scalar keys appear.
    assert "drift_counts" not in header
    assert "metric_counts" not in header
    assert header["drift_loss"] == pytest.approx(0.1)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


def test_endpoint_run_expectations(phase15_client: TestClient) -> None:
    r = phase15_client.get("/api/run/2026-05-20_e0/v3/predicate_fail/expectations")
    assert r.status_code == 200
    body = r.json()
    assert body["entry_id"] == "predicate_fail"
    assert len(body["outcomes"]) == 1
    o = body["outcomes"][0]
    assert o["kind"] == "predicate"
    assert o["passed"] is False


def test_endpoint_run_expectations_rubric(phase15_client: TestClient) -> None:
    r = phase15_client.get("/api/run/2026-05-20_e0/v3/rubric_pass/expectations")
    assert r.status_code == 200
    body = r.json()
    o = body["outcomes"][0]
    assert o["kind"] == "rubric"
    assert o["judge_name"] == "presentation_quality"


def test_endpoint_run_expectations_empty(phase15_client: TestClient) -> None:
    r = phase15_client.get("/api/run/2026-05-20_e0/v3/no_expectation/expectations")
    assert r.status_code == 200
    assert r.json()["outcomes"] == []


def test_endpoint_run_header(phase15_client: TestClient) -> None:
    r = phase15_client.get("/api/run/2026-05-20_e0/v3/predicate_fail/header")
    assert r.status_code == 200
    body = r.json()
    assert body["runtime_ms"] == 83160
    assert body["tokens_spent"] == 12345
    assert body["plan_revisions"] == 1
    assert body["pass_fail"] is False


def test_endpoint_run_header_unsafe_id_degrades(phase15_client: TestClient) -> None:
    """A path-traversal id degrades to a None-filled header, not a 500."""
    r = phase15_client.get("/api/run/..%2Fbad/v3/entry/header")
    assert r.status_code in (200, 404)
    if r.status_code == 200:
        body = r.json()
        assert body["runtime_ms"] is None
        assert body["run_id"] is None


def test_endpoint_run_expectations_unsafe_id_degrades(
    phase15_client: TestClient,
) -> None:
    r = phase15_client.get("/api/run/..%2Fbad/v3/entry/expectations")
    assert r.status_code in (200, 404)
    if r.status_code == 200:
        assert r.json()["outcomes"] == []
