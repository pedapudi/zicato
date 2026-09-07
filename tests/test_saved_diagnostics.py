"""Saved diagnostics preserve accepted evidence, coordinates and read tolerance."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from zicato.epoch._storage import RecordError
from zicato.health.diagnostics import (
    HealthFinding,
    LoopHealth,
    health_report_path,
    read_latest_loop_health,
    read_loop_health,
    write_loop_health,
)

STAMP = "2026-01-01T00:00:00+00:00"


def _health() -> dict[str, Any]:
    return {
        "epoch_id": "e1",
        "checked_at": STAMP,
        "healthy": False,
        "findings": [
            {
                "code": "tree_never_imported",
                "severity": "warning",
                "summary": "agent absent",
                "detail": {"count": 1, "rate": 1.0},
                "evidence": {"source": "worker"},
            }
        ],
    }


def test_health_preserves_producer_bytes_extensions_and_historical_omissions(
    tmp_path: Path,
) -> None:
    body = _health()
    assert LoopHealth.from_json(body).to_json() == body
    body.update(round=3, assessed_at=STAMP, summary="Recorded warning wording", has_critical=False)
    body["extension"] = {"threshold": 1.0, "observations": 1}
    accepted = LoopHealth.from_json(body)
    write_loop_health(tmp_path, accepted)
    assert (
        health_report_path(tmp_path, "e1", 3).read_text()
        == json.dumps(body, indent=2, sort_keys=True) + "\n"
    )
    assert read_loop_health(tmp_path, "e1", 3) == accepted
    encoded = accepted.to_json()
    encoded["findings"][0]["detail"]["count"] = 500
    assert accepted.findings[0].detail["count"] == 1
    assert type(accepted.to_json()["findings"][0]["detail"]["rate"]) is float


def test_typed_health_edits_control_flags_summary_and_extensions() -> None:
    body = _health()
    body.update(round=3, assessed_at=STAMP, summary="Archived warning", has_critical=False)
    accepted = LoopHealth.from_json(body)
    assert accepted.to_json()["summary"] == "Archived warning"
    changed = replace(accepted, findings=(HealthFinding("outage", "critical", "No runs"),))
    encoded = changed.to_json()
    assert encoded["healthy"] is False
    assert encoded["has_critical"] is True
    assert encoded["summary"] == "CRITICAL: [outage] No runs"
    cleared = replace(accepted, findings=()).to_json()
    assert cleared["healthy"] is True and cleared["summary"] == "loop healthy"
    finding = replace(accepted.findings[0], summary="Measured absence")
    assert finding.to_json()["summary"] == "Measured absence"
    assert finding.to_json()["evidence"] == {"source": "worker"}


def test_fresh_and_decoded_health_edits_derive_the_same_flags() -> None:
    fresh = LoopHealth("e1", (), True, STAMP).for_round("e1", 0, assessed_at=STAMP)
    decoded = LoopHealth.from_json(fresh.to_json())
    finding = HealthFinding("outage", "critical", "No runs")
    expected = {
        "epoch_id": "e1",
        "findings": [
            {"code": "outage", "severity": "critical", "summary": "No runs", "detail": {}}
        ],
        "healthy": False,
        "checked_at": STAMP,
        "round": 0,
        "assessed_at": STAMP,
        "summary": "CRITICAL: [outage] No runs",
        "has_critical": True,
    }
    for report in (fresh, decoded):
        assert replace(report, findings=(finding,)).to_json() == expected


@pytest.mark.parametrize(
    "changes",
    [
        {"healthy": True},
        {"healthy": 0},
        {"has_critical": True},
        {"has_critical": 0},
        {"round": True},
        {"round": -1},
        {"checked_at": "2026-01-01"},
        {"assessed_at": "invalid"},
        {"epoch_id": "../e1"},
        {"findings": {}},
        {"findings": [{"code": "gap", "severity": "fatal", "summary": "gap"}]},
        {"findings": [{"code": "gap", "severity": "warning", "summary": 42}]},
        {"findings": [{"code": "gap", "severity": "warning", "summary": "gap", "detail": []}]},
    ],
)
def test_health_refuses_malformed_present_fields(changes: dict[str, Any]) -> None:
    with pytest.raises(RecordError):
        LoopHealth.from_json({**_health(), **changes})


def test_health_stamping_and_reader_check_both_coordinates(tmp_path: Path) -> None:
    health = LoopHealth.from_json(_health())
    with pytest.raises(RecordError, match="epoch"):
        health.for_round("e2", 3, assessed_at=STAMP)
    stamped = health.for_round("e1", 3, assessed_at=STAMP)
    write_loop_health(tmp_path, stamped)
    path = health_report_path(tmp_path, "e1", 3)
    for changes in ({"epoch_id": "e2"}, {"round": 4}):
        path.write_text(json.dumps({**stamped.to_json(), **changes}))
        with pytest.raises(RecordError, match="coordinates"):
            read_loop_health(tmp_path, "e1", 3)
    before = path.read_bytes()
    derived = replace(stamped, healthy=True, has_critical=True).to_json()
    assert derived["healthy"] is False
    assert derived["has_critical"] is False
    with pytest.raises(RecordError):
        write_loop_health(tmp_path, replace(stamped, checked_at="invalid"))
    assert path.read_bytes() == before


def test_latest_health_does_not_fall_back_past_corruption(tmp_path: Path) -> None:
    assert read_latest_loop_health(tmp_path, "e1") is None
    write_loop_health(
        tmp_path, LoopHealth.from_json(_health()).for_round("e1", 2, assessed_at=STAMP)
    )
    path = health_report_path(tmp_path, "e1", 10)
    for invalid in (b"{", b"\xff", b"[]"):
        path.write_bytes(invalid)
        with pytest.raises(RecordError):
            read_latest_loop_health(tmp_path, "e1")
    assert read_loop_health(tmp_path, "e2", 2) is None


@pytest.mark.parametrize(
    "body", ["{", "[]", '{"port":true}', '{"port":0}', '{"host":42,"port":7892}']
)
def test_endpoint_reader_tolerates_unavailable_records(tmp_path: Path, body: str) -> None:
    from zicato.runtime.state import read_dashboard_endpoint

    path = tmp_path / "dashboard.json"
    assert read_dashboard_endpoint(path) is None
    path.write_text(body)
    assert read_dashboard_endpoint(path) is None


def test_endpoint_replacement_is_atomic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import zicato.storage._atomic as atomic
    from zicato.runtime.paths import dashboard_endpoint_path
    from zicato.runtime.state import (
        DashboardEndpoint,
        read_dashboard_endpoint,
        write_dashboard_endpoint,
    )

    original = DashboardEndpoint("127.0.0.1", 7892)
    write_dashboard_endpoint(tmp_path, original)
    path = dashboard_endpoint_path(tmp_path)
    assert path.read_bytes() == b'{"host": "127.0.0.1", "port": 7892}\n'

    def interrupt(source: Path, destination: Path) -> None:
        assert read_dashboard_endpoint(path) == original
        assert json.loads(source.read_text()) == {"host": "127.0.0.1", "port": 7893}
        raise OSError("replacement interrupted")

    monkeypatch.setattr(atomic.os, "replace", interrupt)
    with pytest.raises(OSError, match="interrupted"):
        write_dashboard_endpoint(tmp_path, DashboardEndpoint("127.0.0.1", 7893))
    assert read_dashboard_endpoint(path) == original


def test_saved_warning_is_not_counted_twice_and_retries_remain_distinct(tmp_path: Path) -> None:
    from zicato.health.diagnostics import detect_optional_failures
    from zicato.health.inputs import epoch_optional_failures
    from zicato.logging_stream import current_log_context, install_log_stream, round_log_context
    from zicato.query.gate_view import build_health_report
    from zicato.query.paths import WorkspacePaths
    from zicato.util import best_effort
    from zicato.workspace import WorkspaceLayout

    layout = WorkspaceLayout.from_root(tmp_path)
    layout.current_epoch_marker.write_text("e1")
    before = current_log_context()
    handle = install_log_stream(tmp_path)
    try:
        with round_log_context("e1", 2):
            for _ in range(2):
                with best_effort("report capture"):
                    raise OSError("private request text")
        with round_log_context("e2", 0):
            with best_effort("unrelated epoch"):
                raise OSError("unrelated")
    finally:
        handle.close()
    assert current_log_context() == before
    failures = epoch_optional_failures(tmp_path, "e1")
    assert len(failures) == 2
    assert failures[0]["cursor"] != failures[1]["cursor"]
    assert "private request text" not in handle.path.read_text()
    health = LoopHealth("e1", tuple(detect_optional_failures(failures)), False, STAMP)
    write_loop_health(tmp_path, health.for_round("e1", 2, assessed_at=STAMP))
    for _ in range(2):
        report = build_health_report(WorkspacePaths(tmp_path))
        assert len(report["findings"]) == 2
        assert report["healthy"] is False
    health_report_path(tmp_path, "e1", 3).write_text("{")
    report = build_health_report(WorkspacePaths(tmp_path))
    assert report["healthy"] is None
    assert "unreadable" in report


def test_optional_boundary_does_not_swallow_cancellation() -> None:
    import asyncio

    from zicato.util import best_effort

    with pytest.raises(asyncio.CancelledError), best_effort("cleanup"):
        raise asyncio.CancelledError


def test_health_view_preserves_historical_warning_details(tmp_path: Path) -> None:
    from zicato.query.gate_view import build_health_report
    from zicato.query.paths import WorkspacePaths
    from zicato.workspace import WorkspaceLayout

    WorkspaceLayout.from_root(tmp_path).current_epoch_marker.write_text("e1")
    body = _health()
    finding = body["findings"][0]
    finding.update(code="optional_operation_failed", detail={"invocation": [], "cursor": {}})
    health = LoopHealth.from_json(body).for_round("e1", 0, assessed_at=STAMP)
    write_loop_health(tmp_path, health)
    original = health_report_path(tmp_path, "e1", 0).read_bytes()

    report = build_health_report(WorkspacePaths(tmp_path))

    assert report["findings"] == body["findings"]
    assert report["healthy"] is False
    assert health_report_path(tmp_path, "e1", 0).read_bytes() == original


def test_log_filename_owns_warning_invocation(tmp_path: Path) -> None:
    from zicato.health.inputs import epoch_optional_failures
    from zicato.query.gate_view import build_health_report
    from zicato.query.paths import WorkspacePaths
    from zicato.workspace import WorkspaceLayout

    WorkspaceLayout.from_root(tmp_path).current_epoch_marker.write_text("e1")
    (tmp_path / "logs").mkdir()
    path = tmp_path / "logs" / "recorded-invocation.jsonl"
    body = {
        "epoch_id": "e1",
        "level": "WARNING",
        "component": "zicato.util.best_effort",
        "fields": {"operation": "capture", "exception_type": "OSError"},
        "invocation": ["extension value"],
        "extension": {"retained": True},
    }
    path.write_text(json.dumps(body) + "\n")
    original = path.read_bytes()

    failure = epoch_optional_failures(tmp_path, "e1")[0]

    assert failure["invocation"] == path.stem
    assert failure["cursor"] == len(original)
    assert failure["extension"] == body["extension"]
    assert build_health_report(WorkspacePaths(tmp_path))["findings"][0]["detail"] == failure
    assert path.read_bytes() == original
