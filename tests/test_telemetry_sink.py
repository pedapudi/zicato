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


# ---------------------------------------------------------------------------
# Multi-sink builder (JSONL + optional harmonograf live stream)
# ---------------------------------------------------------------------------


def test_make_run_sinks_jsonl_only_when_no_harmonograf_url(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """With no harmonograf URL configured, only the JSONL sink is attached."""
    pytest.importorskip("goldfive")
    from goldfive.sinks.persistence import JSONLPersistenceSink  # type: ignore

    from zicato.telemetry.sink import HARMONOGRAF_URL_ENV, make_run_sinks

    monkeypatch.delenv(HARMONOGRAF_URL_ENV, raising=False)
    sinks = make_run_sinks(tmp_path, "ep1", "v0", "entryA")
    assert len(sinks) == 1
    assert isinstance(sinks[0], JSONLPersistenceSink)


def test_make_run_sinks_attaches_harmonograf_when_env_set(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """ZICATO_HARMONOGRAF_URL appends a harmonograf sink alongside JSONL.

    The goldfive-side harmonograf sink ships in ``harmonograf_client``,
    which is not a test dependency — so we install a minimal stub
    ``harmonograf_client`` module exporting ``Client`` + ``HarmonografSink``
    and assert the builder constructs and appends it.
    """
    pytest.importorskip("goldfive")
    import sys
    import types

    from goldfive.sinks.persistence import JSONLPersistenceSink  # type: ignore

    from zicato.telemetry.sink import HARMONOGRAF_URL_ENV, make_run_sinks

    constructed: dict[str, object] = {}

    class _StubClient:
        def __init__(self, *, name: str, server_addr: str) -> None:
            constructed["name"] = name
            constructed["server_addr"] = server_addr

    class _StubHarmonografSink:
        def __init__(self, client: object) -> None:
            constructed["client"] = client

    stub_mod = types.ModuleType("harmonograf_client")
    stub_mod.Client = _StubClient  # type: ignore[attr-defined]
    stub_mod.HarmonografSink = _StubHarmonografSink  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "harmonograf_client", stub_mod)

    monkeypatch.setenv(HARMONOGRAF_URL_ENV, "127.0.0.1:7531")
    sinks = make_run_sinks(tmp_path, "ep1", "v0", "entryA")

    assert len(sinks) == 2
    assert isinstance(sinks[0], JSONLPersistenceSink)
    assert isinstance(sinks[1], _StubHarmonografSink)
    # The harmonograf client was built against the configured URL.
    assert constructed["server_addr"] == "127.0.0.1:7531"


def test_make_run_sinks_falls_back_to_jsonl_when_harmonograf_client_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A missing harmonograf_client never hard-fails — JSONL-only result."""
    pytest.importorskip("goldfive")
    import builtins

    from goldfive.sinks.persistence import JSONLPersistenceSink  # type: ignore

    from zicato.telemetry.sink import HARMONOGRAF_URL_ENV, make_run_sinks

    real_import = builtins.__import__

    def fake_import(name: str, *args, **kwargs):  # type: ignore[no-untyped-def]
        if name == "harmonograf_client":
            raise ImportError("No module named 'harmonograf_client'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    monkeypatch.setenv(HARMONOGRAF_URL_ENV, "127.0.0.1:7531")

    sinks = make_run_sinks(tmp_path, "ep1", "v0", "entryA")
    # Only JSONL — the harmonograf attachment degraded gracefully.
    assert len(sinks) == 1
    assert isinstance(sinks[0], JSONLPersistenceSink)


def test_resolve_harmonograf_url_env_beats_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """resolve_harmonograf_url prefers the env var over the workspace config."""
    from zicato.telemetry.sink import HARMONOGRAF_URL_ENV, resolve_harmonograf_url

    monkeypatch.delenv(HARMONOGRAF_URL_ENV, raising=False)
    # Config-only.
    assert resolve_harmonograf_url({"harmonograf_url": "cfg-host:1234"}) == "cfg-host:1234"
    # No source at all.
    assert resolve_harmonograf_url(None) == ""
    assert resolve_harmonograf_url({}) == ""
    # Env wins over config.
    monkeypatch.setenv(HARMONOGRAF_URL_ENV, "env-host:9999")
    assert resolve_harmonograf_url({"harmonograf_url": "cfg-host:1234"}) == "env-host:9999"
