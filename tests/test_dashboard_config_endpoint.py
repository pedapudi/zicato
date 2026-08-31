"""``GET /api/config`` — the settings a run is operating under.

A knob's value is resolved from an order of priority (the dataclass default,
the workspace ``config.json``, a pinned CLI flag, the host's CPU count), and
only the process that resolves it sees the answer. The loop stamps the whole
resolved map onto its heartbeat record; this route serves that map back,
with the tier beside each value, so an operator reading a ceiling can tell
whether it is a number somebody chose.

What these tests pin: the recorded map reaches the wire unchanged, including
a knob no reader here knows about (the map is open); the route serves the
record's identity and write time beside it; and a workspace with no run
record answers ``null`` rather than an invented shape — the same answer the
Rust supervisor gives, which does not serve this route at all.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from zicato.dashboard.server import create_app
from zicato.query import WorkspacePaths, read_effective_settings
from zicato.runtime.effective_settings import (
    SOURCE_HOST_CPU_COUNT,
    SOURCE_PINNED_FLAG,
    SOURCE_WORKSPACE,
)
from zicato.runtime.state import Heartbeat, write_heartbeat

_RECORDED = {
    "runtime.parallelism": {"value": 4, "source": SOURCE_PINNED_FLAG},
    "runtime.host_worker_permits": {"value": 20, "source": SOURCE_HOST_CPU_COUNT},
    "health.max_generation_age_days": {"value": 30, "source": SOURCE_WORKSPACE},
}


@pytest.fixture
def static_dir(tmp_path: Path) -> Path:
    d = tmp_path / "static"
    d.mkdir()
    (d / "index.html").write_text("<!doctype html><title>z</title>", encoding="utf-8")
    return d


def _workspace(tmp_path: Path) -> Path:
    ws = tmp_path / ".zicato"
    ws.mkdir(parents=True, exist_ok=True)
    return ws


def _record(ws: Path, settings: dict[str, dict[str, object]] | None = None) -> Heartbeat:
    beat = Heartbeat(
        pid=41231,
        instance_id="default",
        started_at="2026-08-30T10:00:00Z",
        last_heartbeat="2026-08-30T11:02:07Z",
        epoch_id="e1",
        settings=settings if settings is not None else _RECORDED,
    )
    write_heartbeat(ws, beat)
    return beat


def _get(ws: Path, static_dir: Path) -> tuple[int, object]:
    with TestClient(create_app(ws, static_dir, read_only=True)) as client:
        response = client.get("/api/config")
        return response.status_code, json.loads(response.text)


def test_the_recorded_map_round_trips_through_the_endpoint(
    tmp_path: Path, static_dir: Path
) -> None:
    """Every value and every source reaches the wire as the run recorded it."""
    ws = _workspace(tmp_path)
    _record(ws)

    status, body = _get(ws, static_dir)

    assert status == 200
    assert body == {
        "recorded_at": "2026-08-30T11:02:07Z",
        "pid": 41231,
        "instance_id": "default",
        "settings": _RECORDED,
    }


def test_a_knob_the_reader_does_not_know_is_served_unchanged(
    tmp_path: Path, static_dir: Path
) -> None:
    """The map is open: a knob added later needs no change here to appear."""
    ws = _workspace(tmp_path)
    _record(ws, {"section.knob_added_later": {"value": ["a", "b"], "source": SOURCE_WORKSPACE}})

    _status, body = _get(ws, static_dir)

    assert isinstance(body, dict)
    assert body["settings"] == {
        "section.knob_added_later": {"value": ["a", "b"], "source": SOURCE_WORKSPACE}
    }


def test_a_record_written_before_the_map_existed_serves_an_empty_map(
    tmp_path: Path, static_dir: Path
) -> None:
    """The record is still reported: its pid and write time answer for it."""
    ws = _workspace(tmp_path)
    _record(ws, {})

    _status, body = _get(ws, static_dir)

    assert isinstance(body, dict)
    assert body["settings"] == {}
    assert body["pid"] == 41231


def test_a_workspace_with_no_run_record_serves_null(tmp_path: Path, static_dir: Path) -> None:
    """There is no run whose settings could be reported, and the route says so."""
    ws = _workspace(tmp_path)

    status, body = _get(ws, static_dir)

    assert status == 200
    assert body is None


def test_the_reader_answers_null_for_an_unreadable_record(tmp_path: Path) -> None:
    """A torn record narrows the answer to null instead of raising."""
    ws = _workspace(tmp_path)
    _record(ws)
    (ws / "runtime" / "heartbeat.json").write_text("{not json", encoding="utf-8")

    assert read_effective_settings(WorkspacePaths(ws)) is None
