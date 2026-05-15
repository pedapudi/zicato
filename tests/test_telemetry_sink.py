"""Tests for :mod:`zicato.telemetry.sink`.

The sink module is a thin path-routing wrapper over goldfive's
``JSONLPersistenceSink``. Tests cover two things:

1. Path generation matches the workspace helpers (no drift between
   what the sink writes and what the reducer reads).
2. When goldfive is importable, the sink writes to the right file and
   the round-trip via ``replay_from_jsonl`` recovers the event.

Tests that need goldfive are gated by ``pytest.importorskip("goldfive")``
so the file is importable in environments without it.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from zicato.core.workspace import events_jsonl_path
from zicato.telemetry import make_run_sink, make_run_sink_path


def test_run_sink_path_matches_workspace_helper(tmp_path: Path) -> None:
    """make_run_sink_path agrees with workspace.events_jsonl_path."""
    epoch = "ep1"
    gen = "v0"
    entry = "entryA"
    p = make_run_sink_path(tmp_path, epoch, gen, entry)
    expected = events_jsonl_path(tmp_path, epoch, gen, entry)
    assert p == expected
    # The parent directory must exist after the call so the lazy file
    # open inside the goldfive sink does not fail on first emit.
    assert p.parent.is_dir()


def test_run_sink_path_idempotent(tmp_path: Path) -> None:
    """Calling make_run_sink_path twice is a no-op on the second call."""
    p1 = make_run_sink_path(tmp_path, "ep1", "v0", "entryA")
    p2 = make_run_sink_path(tmp_path, "ep1", "v0", "entryA")
    assert p1 == p2
    assert p1.parent.is_dir()


def test_make_run_sink_writes_to_expected_path(tmp_path: Path) -> None:
    """A constructed sink writes events to the expected per-run path.

    Skipped when goldfive is not importable — the sink factory itself
    raises ModuleNotFoundError in that environment and a separate
    test would have to use mocks, which would lose the value of
    actually exercising the wire path.
    """
    pytest.importorskip("goldfive")
    pytest.importorskip("google.protobuf")
    from goldfive.pb.goldfive.v1 import events_pb2  # type: ignore
    from goldfive.sinks.persistence import replay_from_jsonl  # type: ignore

    epoch = "ep1"
    gen = "v0"
    entry = "entry-sink"
    sink = make_run_sink(tmp_path, epoch, gen, entry)
    expected_path = make_run_sink_path(tmp_path, epoch, gen, entry)

    # Construct a minimal Event proto carrying a RunStarted payload —
    # the smallest typed event the goldfive wire can carry.
    evt = events_pb2.Event()
    evt.event_id = "evt-1"
    evt.run_id = "run-1"
    evt.sequence = 0
    evt.run_started.run_id = "run-1"
    evt.run_started.goal_summary = "hello"

    asyncio.run(sink.emit(evt))
    asyncio.run(sink.close())

    assert expected_path.is_file()
    parsed = replay_from_jsonl(expected_path)
    assert len(parsed) == 1
    assert parsed[0].run_id == "run-1"
    assert parsed[0].run_started.goal_summary == "hello"


def test_make_run_sink_missing_goldfive_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When goldfive cannot be imported, make_run_sink surfaces a clear error.

    We simulate the missing module by patching the lazy import inside
    ``make_run_sink``. The shape of the error matters: callers need to
    distinguish "telemetry needs goldfive" from any other import
    failure.
    """
    import builtins

    real_import = builtins.__import__

    def fake_import(name: str, *args, **kwargs):  # type: ignore[no-untyped-def]
        if name == "goldfive.sinks.persistence":
            raise ModuleNotFoundError("No module named 'goldfive'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    with pytest.raises(ModuleNotFoundError) as exc:
        make_run_sink(tmp_path, "ep1", "v0", "entry-missing")
    # Message must point operators at "install goldfive", not at some
    # generic import-failure noise.
    assert "goldfive" in str(exc.value)
