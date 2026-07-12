"""Tests for the ``GET /api/logs`` operator-log endpoint (LOGGING.md §5).

The endpoint is a thin wrapper over ``query.build_log_view``: it clamps the
limit, reads the level / after / invocation query params, and returns the
tail payload. These tests assert the payload shape, the level filter, and
the honest empty-workspace degrade.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from zicato.dashboard.server import create_app


@pytest.fixture
def static_dir(tmp_path: Path) -> Path:
    d = tmp_path / "static"
    d.mkdir()
    (d / "index.html").write_text("<!doctype html><title>z</title>", encoding="utf-8")
    return d


def _write_stream(ws: Path, name: str, records: list[dict]) -> None:
    logs = ws / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    (logs / f"{name}.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in records), encoding="utf-8"
    )


_TS = "2026-07-12T08:40:01.100Z"
_RECORDS = [
    {"ts": _TS, "level": "INFO", "component": "zicato.orchestrator", "message": "loop booted"},
    {
        "ts": _TS,
        "level": "WARNING",
        "component": "zicato.tournament.runner",
        "message": "over budget",
    },
]


def _client(ws: Path, static_dir: Path) -> TestClient:
    return TestClient(create_app(ws, static_dir, read_only=True))


def test_logs_endpoint_payload_shape(tmp_path: Path, static_dir: Path) -> None:
    ws = tmp_path / ".zicato"
    _write_stream(ws, "20260712T084000Z-1", _RECORDS)
    client = _client(ws, static_dir)
    resp = client.get("/api/logs")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"records", "cursor", "invocation", "invocations", "level"}
    assert [r["message"] for r in body["records"]] == ["loop booted", "over budget"]
    # cursor is now the file's EOF byte offset (the resume point).
    stream = ws / "logs" / "20260712T084000Z-1.jsonl"
    assert body["cursor"] == stream.stat().st_size
    assert body["invocation"] == "20260712T084000Z-1"
    assert body["invocations"][0]["id"] == "20260712T084000Z-1"
    # each record carries an append (byte) cursor.
    assert all("cursor" in r for r in body["records"])


def test_logs_endpoint_level_filter_and_after(tmp_path: Path, static_dir: Path) -> None:
    ws = tmp_path / ".zicato"
    _write_stream(ws, "20260712T084000Z-1", _RECORDS)
    client = _client(ws, static_dir)
    warn = client.get("/api/logs?level=WARNING").json()
    assert [r["message"] for r in warn["records"]] == ["over budget"]
    assert warn["level"] == "WARNING"

    # `after` is a BYTE offset: resume past the first record's cursor to get
    # only the appended tail.
    full = client.get("/api/logs").json()
    first_cursor = full["records"][0]["cursor"]  # byte offset just past record 0
    after = client.get(f"/api/logs?after={first_cursor}").json()
    assert [r["message"] for r in after["records"]] == ["over budget"]


def test_logs_endpoint_empty_workspace(tmp_path: Path, static_dir: Path) -> None:
    ws = tmp_path / ".zicato"
    ws.mkdir()
    client = _client(ws, static_dir)
    body = client.get("/api/logs").json()
    assert body["records"] == []
    assert body["cursor"] is None
    assert body["invocation"] is None
    assert body["invocations"] == []
