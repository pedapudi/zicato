"""Tests for the per-run progress-bump sink wrapper in the runner.

The dashboard renders a run as static because :class:`ActiveRun.last_progress`
was written once at run start. :class:`zicato.tournament.runner._ProgressBumpingSink`
fixes that: it wraps the canonical per-run goldfive sink so every event
``emit`` also bumps ``last_progress`` — throttled so a chatty run cannot
turn into a write storm on the runtime directory.

These tests drive the wrapper directly (no goldfive, no real LLM):

* a stub inner sink records the events it received,
* :func:`zicato.runtime.state.touch_active_run_progress` is the real
  helper, so the bumps land on a real ``ActiveRun`` file in a tmpdir,
* the monotonic clock is monkeypatched so throttle windows are exact.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from zicato.runtime.paths import active_run_path
from zicato.runtime.state import (
    ActiveRun,
    list_active_runs,
    write_active_run,
)
from zicato.tournament.runner import (
    _PROGRESS_BUMP_MIN_INTERVAL_S,
    _ProgressBumpingSink,
    _wrap_sinks_with_progress,
)

# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


class _RecordingSink:
    """Inner sink that records every event it is asked to emit."""

    def __init__(self) -> None:
        self.emitted: list[object] = []
        self.closed = False

    async def emit(self, event: object) -> None:
        self.emitted.append(event)

    async def close(self) -> None:
        self.closed = True


def _make_active_run(workspace_root: Path, run_id: str) -> ActiveRun:
    """Write an ActiveRun state file and return it."""
    run = ActiveRun(
        run_id=run_id,
        pid=4321,
        started_at="2026-05-15T00:00:00Z",
        last_progress="2026-05-15T00:00:00Z",
        wall_clock_budget_seconds=60,
        deadline="2026-05-15T00:01:00Z",
        events_jsonl_path="/tmp/events.jsonl",
        entry_id="entry_a",
        generation_id="v1",
        epoch_id="e0",
    )
    write_active_run(workspace_root, run)
    return run


def _read_run(workspace_root: Path, run_id: str) -> ActiveRun:
    """Read one ActiveRun back from disk."""
    runs = {r.run_id: r for r in list_active_runs(workspace_root)}
    return runs[run_id]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_progress_bump_sink_forwards_every_event(tmp_path: Path) -> None:
    """The wrapper forwards all emits to the inner sink unchanged."""
    workspace = tmp_path / ".zicato"
    workspace.mkdir()
    _make_active_run(workspace, "v1--entry_a")

    inner = _RecordingSink()
    sink = _ProgressBumpingSink(inner, workspace, "v1--entry_a")

    asyncio.run(sink.emit("event-1"))
    asyncio.run(sink.emit("event-2"))
    asyncio.run(sink.emit("event-3"))

    assert inner.emitted == ["event-1", "event-2", "event-3"]


def test_progress_bump_sink_closes_inner_sink(tmp_path: Path) -> None:
    """Closing the wrapper closes the wrapped sink."""
    workspace = tmp_path / ".zicato"
    workspace.mkdir()
    _make_active_run(workspace, "v1--entry_a")

    inner = _RecordingSink()
    sink = _ProgressBumpingSink(inner, workspace, "v1--entry_a")
    asyncio.run(sink.close())

    assert inner.closed is True


def test_progress_bump_sink_bumps_last_progress_on_first_emit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first emit always bumps last_progress so the run animates immediately."""
    workspace = tmp_path / ".zicato"
    workspace.mkdir()
    _make_active_run(workspace, "v1--entry_a")
    before = _read_run(workspace, "v1--entry_a").last_progress
    assert before == "2026-05-15T00:00:00Z"

    # Pin the timestamp the bump stamps so the assertion is exact.
    monkeypatch.setattr(
        "zicato.runtime.state._utc_now_iso",
        lambda: "2026-05-15T00:05:00Z",
    )

    sink = _ProgressBumpingSink(_RecordingSink(), workspace, "v1--entry_a")
    asyncio.run(sink.emit("event-1"))

    after = _read_run(workspace, "v1--entry_a").last_progress
    # The bump landed: last_progress advanced; every other field is intact.
    assert after == "2026-05-15T00:05:00Z"
    run = _read_run(workspace, "v1--entry_a")
    assert run.run_id == "v1--entry_a"
    assert run.entry_id == "entry_a"
    assert run.started_at == "2026-05-15T00:00:00Z"


def test_progress_bump_is_throttled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Emits inside the throttle window do NOT trigger a state-file write."""
    workspace = tmp_path / ".zicato"
    workspace.mkdir()
    _make_active_run(workspace, "v1--entry_a")

    # Count touch_active_run_progress calls without doing any real I/O.
    calls: list[str] = []

    def _fake_touch(ws: Path, run_id: str) -> None:
        calls.append(run_id)

    sink = _ProgressBumpingSink(_RecordingSink(), workspace, "v1--entry_a")
    # Swap the resolved bump callable on the instance.
    monkeypatch.setattr(sink, "_bump", _fake_touch, raising=False)

    # Drive a controllable monotonic clock.
    fake_now = {"t": 1000.0}
    monkeypatch.setattr(
        "zicato.tournament.runner.time.monotonic",
        lambda: fake_now["t"],
    )

    # First emit — always bumps.
    asyncio.run(sink.emit("e1"))
    assert calls == ["v1--entry_a"]

    # Second emit, still inside the throttle window — no bump.
    fake_now["t"] += _PROGRESS_BUMP_MIN_INTERVAL_S / 2
    asyncio.run(sink.emit("e2"))
    assert calls == ["v1--entry_a"]

    # Third emit, throttle window elapsed — bumps again.
    fake_now["t"] += _PROGRESS_BUMP_MIN_INTERVAL_S + 0.01
    asyncio.run(sink.emit("e3"))
    assert calls == ["v1--entry_a", "v1--entry_a"]

    # Fourth emit, immediately after — throttled again.
    asyncio.run(sink.emit("e4"))
    assert calls == ["v1--entry_a", "v1--entry_a"]


def test_progress_bump_survives_missing_run_file(tmp_path: Path) -> None:
    """A bump against a removed run file is a no-op, never an error.

    Mirrors the benign race where the run already finished and its
    state file was cleaned up before a late event arrived.
    """
    workspace = tmp_path / ".zicato"
    workspace.mkdir()
    # NOTE: no ActiveRun written — the file does not exist.

    sink = _ProgressBumpingSink(_RecordingSink(), workspace, "ghost-run")
    # Must not raise.
    asyncio.run(sink.emit("e1"))
    assert not active_run_path(workspace, "ghost-run").exists()


def test_wrap_sinks_with_progress_wraps_every_sink(tmp_path: Path) -> None:
    """The list helper wraps each sink and preserves ordering."""
    workspace = tmp_path / ".zicato"
    workspace.mkdir()

    inner_a = _RecordingSink()
    inner_b = _RecordingSink()
    wrapped = _wrap_sinks_with_progress([inner_a, inner_b], workspace, "v1--entry_a")

    assert len(wrapped) == 2
    assert all(isinstance(s, _ProgressBumpingSink) for s in wrapped)

    # Each wrapper still forwards to its own inner sink.
    asyncio.run(wrapped[0].emit("x"))
    asyncio.run(wrapped[1].emit("y"))
    assert inner_a.emitted == ["x"]
    assert inner_b.emitted == ["y"]


def test_wrap_sinks_with_progress_empty_list(tmp_path: Path) -> None:
    """An empty sink list (no-goldfive environment) yields an empty list."""
    workspace = tmp_path / ".zicato"
    workspace.mkdir()
    assert _wrap_sinks_with_progress([], workspace, "v1--entry_a") == []
