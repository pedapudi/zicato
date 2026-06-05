"""Tests for the harmonograf auto-launch supervisor (task #202).

Covers both the standalone supervisor module and its wiring into the
orchestrator's evolve-startup path. Each test pins one observable
behaviour from the architectural target in the task description.
"""

from __future__ import annotations

import http.client
import os
import socket
from pathlib import Path

import pytest

from zicato.config import IntegrationConfig
from zicato.orchestrator import (
    _EnvVarRestorer,
    _LaunchedHandle,
    _NoopShutdownHandle,
    _resolve_or_launch_harmonograf,
)
from zicato.runtime.heartbeat import HeartbeatBeater
from zicato.runtime.state import read_heartbeat
from zicato.telemetry import harmonograf_supervisor as supervisor
from zicato.telemetry.harmonograf_supervisor import (
    HarmonografHandle,
    WorkspaceHarmonografHandle,
    build_meta_loop_sink,
    ensure_workspace_harmonograf,
    meta_loop_session_id,
    start_harmonograf,
)
from zicato.telemetry.sink import resolve_harmonograf_url


def _wait_for_health(url: str, *, deadline_s: float = 10.0) -> bool:
    """Poll the harmonograf ``/healthz`` endpoint until it returns 200.

    Returns True on the first 200, False if the deadline elapses. We use
    raw http.client so the test has no extra dependency (the project
    does not pull in ``requests``).
    """
    import time
    from urllib.parse import urlparse

    parsed = urlparse(url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port
    if port is None:
        return False
    end = time.monotonic() + deadline_s
    while time.monotonic() < end:
        try:
            conn = http.client.HTTPConnection(host, port, timeout=1.0)
            conn.request("GET", "/healthz")
            resp = conn.getresponse()
            ok = resp.status == 200
            conn.close()
            if ok:
                return True
        except (ConnectionRefusedError, OSError, http.client.HTTPException):
            pass
        time.sleep(0.1)
    return False


def test_start_harmonograf_returns_local_url(tmp_path: Path) -> None:
    """Auto-launch happy path: free localhost port, server answers /healthz."""
    handle = start_harmonograf(tmp_path)
    try:
        assert isinstance(handle, HarmonografHandle)
        assert handle.url.startswith("http://127.0.0.1:")
        # The web port differs from the gRPC port and is a real
        # listener: hit /healthz to prove the server actually came up.
        assert handle.grpc_port > 0
        assert _wait_for_health(
            handle.url, deadline_s=15.0
        ), f"harmonograf at {handle.url} did not become ready"
    finally:
        handle.shutdown()


def test_shutdown_is_idempotent(tmp_path: Path) -> None:
    """Calling shutdown more than once does not raise."""
    handle = start_harmonograf(tmp_path)
    try:
        assert _wait_for_health(handle.url, deadline_s=15.0)
    finally:
        handle.shutdown()
    # Second call must be a no-op, not a raise.
    handle.shutdown()
    handle.shutdown()


def test_distinct_ports_on_repeat_launch(tmp_path: Path) -> None:
    """Two concurrent supervisors get different ports — no hardcoded reuse."""
    h1 = start_harmonograf(tmp_path / "a")
    try:
        h2 = start_harmonograf(tmp_path / "b")
        try:
            assert h1.url and h2.url, "both launches should have produced URLs"
            assert h1.url != h2.url
            assert h1.grpc_port != h2.grpc_port
            # Both servers actually accept TCP.
            assert _wait_for_health(h1.url, deadline_s=15.0)
            assert _wait_for_health(h2.url, deadline_s=15.0)
        finally:
            h2.shutdown()
    finally:
        h1.shutdown()


def test_resolver_opt_out_does_not_launch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Configured URL -> orchestrator returns it verbatim, no launch happens."""
    # The orchestrator's resolver checks ZICATO_HARMONOGRAF_URL via
    # IntegrationConfig; we set it directly so the resolver short-
    # circuits before reaching start_harmonograf.
    monkeypatch.setenv("ZICATO_HARMONOGRAF_URL", "http://external.example/")

    # If start_harmonograf is ever called the test should fail.
    sentinel = {"called": False}

    def _boom(*_a: object, **_kw: object) -> HarmonografHandle:
        sentinel["called"] = True
        raise AssertionError("start_harmonograf must not be invoked on opt-out")

    monkeypatch.setattr(supervisor, "start_harmonograf", _boom)

    url, handle = _resolve_or_launch_harmonograf(tmp_path)
    try:
        assert url == "http://external.example/"
        assert isinstance(handle, _NoopShutdownHandle)
        assert sentinel["called"] is False
    finally:
        handle.shutdown()  # must be a no-op


def test_resolver_auto_launch_sets_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Auto-launch path: orchestrator pushes URL into ZICATO_HARMONOGRAF_URL."""
    monkeypatch.delenv("ZICATO_HARMONOGRAF_URL", raising=False)
    url, handle = _resolve_or_launch_harmonograf(tmp_path)
    try:
        # On a clean install harmonograf-server is present and launch
        # succeeds; if it doesn't (a degraded install), the URL is empty
        # and we skip — we still verified the resolver path doesn't
        # raise.
        if not url:
            pytest.skip("harmonograf-server unavailable; cannot exercise launch path")
        assert isinstance(handle, _LaunchedHandle)
        assert url.startswith("http://127.0.0.1:")
        # Env was rewritten so tournament workers re-resolve to the
        # same URL via load_config().
        assert os.environ.get("ZICATO_HARMONOGRAF_URL") == url
        # And resolve_harmonograf_url with that env in place returns it.
        assert resolve_harmonograf_url(workspace_config=None) == url
    finally:
        handle.shutdown()
    # After shutdown the env var is restored to its prior absence.
    assert "ZICATO_HARMONOGRAF_URL" not in os.environ


def test_failure_isolation_when_supervisor_import_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing harmonograf-server dep yields an empty URL, never raises."""
    monkeypatch.delenv("ZICATO_HARMONOGRAF_URL", raising=False)
    # Simulate harmonograf_server import failure by monkeypatching
    # start_harmonograf to behave as if the package was missing.
    monkeypatch.setattr(
        supervisor,
        "start_harmonograf",
        lambda _root, **_kw: HarmonografHandle(url="", grpc_port=0),
    )

    url, handle = _resolve_or_launch_harmonograf(tmp_path)
    try:
        assert url == ""  # failure isolation: empty URL
        # The handle is the failure-isolation no-op the supervisor returned.
        # shutdown() must be a no-op (no thread, no app to stop).
    finally:
        handle.shutdown()
    # Env was never set on the failure path.
    assert "ZICATO_HARMONOGRAF_URL" not in os.environ


def test_heartbeat_round_trip_carries_resolved_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After the resolver runs, a freshly-written heartbeat carries the URL."""
    monkeypatch.setenv("ZICATO_HARMONOGRAF_URL", "http://carry-me.example/")
    url, handle = _resolve_or_launch_harmonograf(tmp_path)
    try:
        beater = HeartbeatBeater(tmp_path, instance_id="default", interval_s=60.0)
        # Bypass the async start — we only need a single synchronous write.
        beater.update(harmonograf_url=url, phase="test")
        beater.bump_now()

        hb = read_heartbeat(tmp_path)
        assert hb is not None
        assert hb.harmonograf_url == "http://carry-me.example/"
    finally:
        handle.shutdown()


def test_worker_args_use_auto_launched_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Tournament runner's resolver picks up the auto-launched URL via env."""
    # Clear env so the resolver lands on the auto-launch path.
    monkeypatch.delenv("ZICATO_HARMONOGRAF_URL", raising=False)
    url, handle = _resolve_or_launch_harmonograf(tmp_path)
    try:
        if not url:
            pytest.skip("harmonograf-server unavailable; cannot exercise launch path")
        # The tournament runner has its own _resolve_harmonograf_url
        # that builds the worker args file's harmonograf_url field. It
        # MUST resolve to the same URL the orchestrator just established.
        from zicato.tournament.runner import (  # noqa: PLC0415
            _resolve_harmonograf_url as runner_resolve,
        )

        assert runner_resolve(tmp_path) == url
    finally:
        handle.shutdown()


def test_meta_loop_session_id_is_stable_and_safe() -> None:
    """The meta-loop session id is deterministic per evolve start time."""
    iso = "2026-05-28T01:02:03+00:00"
    sid = meta_loop_session_id(iso)
    assert sid.startswith("zicato-meta-loop-")
    # Same input -> same id (stability across re-resolution).
    assert meta_loop_session_id(iso) == sid
    # Sanitisation: ':' replaced (the heartbeat ISO format carries
    # colons; the session id must remain URL-safe).
    assert ":" not in sid
    assert " " not in sid


def test_build_meta_loop_sink_no_url_returns_none() -> None:
    """An empty URL yields no sink — additive, never load-bearing."""
    assert build_meta_loop_sink("", "session-x") is None


def test_build_meta_loop_sink_dials_grpc_port_and_scopes_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Meta-loop sink dials the gRPC port (not the web URL) and scopes the session.

    Same web≠grpc split as the per-run sink: with the auto-launch
    ``ZICATO_HARMONOGRAF_GRPC`` env set, the meta-loop Client must dial
    the native gRPC port, NOT the gRPC-Web port in the URL. The session
    id is threaded into the Client so meta-loop traffic is bucketed under
    one harmonograf session.
    """
    import sys
    import types

    from zicato.telemetry.sink import HARMONOGRAF_GRPC_ENV

    constructed: dict[str, object] = {}

    class _StubClient:
        def __init__(self, *, name: str, server_addr: str, session_id: str = "") -> None:
            constructed["name"] = name
            constructed["server_addr"] = server_addr
            constructed["session_id"] = session_id

    class _StubHarmonografSink:
        def __init__(self, client: object) -> None:
            constructed["client"] = client

    stub_mod = types.ModuleType("harmonograf_client")
    stub_mod.Client = _StubClient  # type: ignore[attr-defined]
    stub_mod.HarmonografSink = _StubHarmonografSink  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "harmonograf_client", stub_mod)

    monkeypatch.setenv(HARMONOGRAF_GRPC_ENV, "127.0.0.1:9090")
    sink = build_meta_loop_sink("http://127.0.0.1:9080", "zicato-meta-loop-sess")

    assert sink is not None
    # Dialed the gRPC port, not the web port.
    assert constructed["server_addr"] == "127.0.0.1:9090"
    # Session scoped on the client.
    assert constructed["session_id"] == "zicato-meta-loop-sess"


def test_env_var_restorer_round_trip(monkeypatch: pytest.MonkeyPatch) -> None:
    """_EnvVarRestorer captures prior value and restores it idempotently."""
    monkeypatch.setenv("__ZICATO_TEST_VAR__", "before")
    r = _EnvVarRestorer("__ZICATO_TEST_VAR__")
    r.set("during")
    assert os.environ["__ZICATO_TEST_VAR__"] == "during"
    r.restore()
    assert os.environ["__ZICATO_TEST_VAR__"] == "before"
    # Second restore is a no-op.
    os.environ["__ZICATO_TEST_VAR__"] = "after-restore"
    r.restore()
    assert os.environ["__ZICATO_TEST_VAR__"] == "after-restore"


def test_env_var_restorer_unset_round_trip(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the variable was absent, restore removes it."""
    monkeypatch.delenv("__ZICATO_TEST_VAR2__", raising=False)
    r = _EnvVarRestorer("__ZICATO_TEST_VAR2__")
    r.set("during")
    assert os.environ.get("__ZICATO_TEST_VAR2__") == "during"
    r.restore()
    assert "__ZICATO_TEST_VAR2__" not in os.environ


def test_pick_free_port_returns_distinct() -> None:
    """_pick_free_port must hand back a usable, currently-free port."""
    p1 = supervisor._pick_free_port()
    p2 = supervisor._pick_free_port()
    assert 1024 < p1 < 65536
    assert 1024 < p2 < 65536
    # The ports may or may not be distinct (kernel can reuse the just-
    # freed port); just verify each is bindable.
    for p in (p1, p2):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("127.0.0.1", p))


# ---------------------------------------------------------------------------
# Per-workspace ensure-helper (standalone dashboard / builder)
# ---------------------------------------------------------------------------


def _harmonograf_data_dir(workspace_root: Path) -> Path:
    """Mirror the supervisor's ``.harmonograf`` data-dir resolution."""
    return supervisor._resolve_data_dir(workspace_root)


def test_ensure_workspace_harmonograf_launches_and_records(tmp_path: Path) -> None:
    """No record on disk ⇒ launch a fresh server and write ``server.json``."""
    handle = ensure_workspace_harmonograf(tmp_path)
    try:
        assert isinstance(handle, WorkspaceHarmonografHandle)
        assert handle.web_url.startswith("http://127.0.0.1:")
        assert handle.launched is True
        assert _wait_for_health(handle.web_url, deadline_s=15.0)
        # A live record landed on disk naming this server.
        record_path = _harmonograf_data_dir(tmp_path) / "server.json"
        assert record_path.exists()
        import json as _json

        rec = _json.loads(record_path.read_text())
        assert rec["web_url"] == handle.web_url
        assert rec["grpc_target"].startswith("127.0.0.1:")
        assert int(rec["pid"]) == os.getpid()
        assert rec["started_iso"]
    finally:
        handle.shutdown()


def test_ensure_workspace_harmonograf_reuses_live_record(tmp_path: Path) -> None:
    """A second ensure with a LIVE record reuses it (launched=False)."""
    first = ensure_workspace_harmonograf(tmp_path)
    try:
        assert first.launched is True
        assert _wait_for_health(first.web_url, deadline_s=15.0)

        second = ensure_workspace_harmonograf(tmp_path)
        # Reused — no second server, no shutdown ownership.
        assert second.launched is False
        assert second.web_url == first.web_url
        assert second.grpc_target == first.grpc_target
        # second.shutdown() must be a no-op (it does not own the server).
        second.shutdown()
        # The first server is still alive after the reuser's no-op shutdown.
        assert _wait_for_health(first.web_url, deadline_s=5.0)
    finally:
        first.shutdown()


def test_ensure_workspace_harmonograf_relaunches_on_stale_record(tmp_path: Path) -> None:
    """A stale record (dead pid / unreachable port) is ignored and overwritten."""
    import json as _json

    data_dir = _harmonograf_data_dir(tmp_path)
    data_dir.mkdir(parents=True, exist_ok=True)
    # A record naming a pid that cannot be alive and a port nothing binds.
    stale = {
        "web_url": "http://127.0.0.1:1",
        "grpc_target": "127.0.0.1:2",
        "pid": 2_147_483_646,  # implausibly high; not a live process
        "started_iso": "2020-01-01T00:00:00Z",
    }
    (data_dir / "server.json").write_text(_json.dumps(stale))

    handle = ensure_workspace_harmonograf(tmp_path)
    try:
        # Stale record ignored ⇒ a fresh launch.
        assert handle.launched is True
        assert handle.web_url.startswith("http://127.0.0.1:")
        assert handle.web_url != "http://127.0.0.1:1"
        assert _wait_for_health(handle.web_url, deadline_s=15.0)
        # The record was overwritten to name the live server.
        rec = _json.loads((data_dir / "server.json").read_text())
        assert rec["web_url"] == handle.web_url
    finally:
        handle.shutdown()


def test_ensure_workspace_harmonograf_failure_isolated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A launch failure yields a no-op handle (empty url), never raises."""
    monkeypatch.setattr(
        supervisor,
        "start_harmonograf",
        lambda _root, **_kw: HarmonografHandle(url="", grpc_port=0),
    )
    handle = ensure_workspace_harmonograf(tmp_path)
    assert isinstance(handle, WorkspaceHarmonografHandle)
    assert handle.web_url == ""
    assert handle.grpc_target == ""
    assert handle.launched is False
    # shutdown() on the no-op handle is a no-op.
    handle.shutdown()


def test_resolve_harmonograf_url_pure_resolver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """resolve_harmonograf_url itself does NOT trigger a launch (purity)."""
    monkeypatch.delenv("ZICATO_HARMONOGRAF_URL", raising=False)
    # Pure resolver returns "" — the launch path lives in the orchestrator.
    assert (
        resolve_harmonograf_url(
            workspace_config=None,
            config=IntegrationConfig(harmonograf_url=""),
        )
        == ""
    )
    # And with an explicit config URL, it returns that verbatim.
    assert (
        resolve_harmonograf_url(
            workspace_config=None,
            config=IntegrationConfig(harmonograf_url="http://pinned/"),
        )
        == "http://pinned/"
    )
