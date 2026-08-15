"""Tests for the harmonograf auto-launch supervisor (task #202).

Covers both the standalone supervisor module and its wiring into the
orchestrator's evolve-startup path. Each test pins one observable
behaviour from the architectural target in the task description.
"""

from __future__ import annotations

import asyncio
import http.client
import inspect
import logging
import os
import socket
import sys
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest

from zicato.config import IntegrationConfig
from zicato.evolve.lifecycle_services import (
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


def _boots_or_uses_live_server(func: object) -> object:
    """Mark a test that boots a live harmonograf (or leans on the shared one).

    The launch/liveness behaviour IS the coverage for these tests. Tagged
    ``slow`` + ``integration`` for the opt-in fast lane (``-m "not slow"``);
    the full suite still runs them by default.
    """
    return pytest.mark.slow(pytest.mark.integration(func))


def test_missing_live_telemetry_names_install_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setitem(sys.modules, "harmonograf_server.config", None)

    with caplog.at_level(logging.WARNING, logger=supervisor.__name__):
        handle = start_harmonograf(tmp_path)

    assert handle.url == ""
    assert "install zicato[observability]" in caplog.text


def test_rejected_idle_coroutine_is_closed() -> None:
    """A closing HTTP task group must not leak the coroutine it rejects."""
    from hypercorn.asyncio.worker_context import AsyncioSingleTask

    supervisor._close_rejected_idle_coroutines()
    coroutine = None

    async def idle() -> None:
        return None

    def action() -> object:
        nonlocal coroutine
        coroutine = idle()
        return coroutine

    class _ClosedGroup:
        def create_task(self, _coroutine: object) -> None:
            raise RuntimeError("closed")

    task = AsyncioSingleTask()
    with pytest.raises(RuntimeError, match="closed"):
        asyncio.run(task.restart(SimpleNamespace(_task_group=_ClosedGroup()), action))

    assert inspect.getcoroutinestate(coroutine) == inspect.CORO_CLOSED


@pytest.fixture(scope="session")
def session_harmonograf(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[Path, WorkspaceHarmonografHandle]]:
    """ONE live workspace harmonograf shared by the READ-ONLY probes.

    Booting a real server thread per test is the dominant cost of this
    module; tests that only need "a live harmonograf to look at" (the
    healthz-true probe, the reuse-a-live-record path) share this one.
    Tests whose contract IS the launch/relaunch behaviour (fresh launch,
    distinct ports, stale-record relaunch, shutdown idempotency) keep
    booting their own. Yields ``(workspace_root, handle)``; the handle's
    server stays up for the whole session and is shut down at the end.
    """
    ws = tmp_path_factory.mktemp("harmonograf-session-ws")
    handle = ensure_workspace_harmonograf(ws)
    if not handle.web_url:
        handle.shutdown()
        pytest.skip("harmonograf-server unavailable; cannot boot the shared server")
    # A fresh workspace has no record ⇒ this ensure really launched.
    assert handle.launched is True
    assert _wait_for_health(handle.web_url, deadline_s=15.0)
    try:
        yield ws, handle
    finally:
        handle.shutdown()


@_boots_or_uses_live_server
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


@_boots_or_uses_live_server
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


@_boots_or_uses_live_server
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


@_boots_or_uses_live_server
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


@_boots_or_uses_live_server
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


@_boots_or_uses_live_server
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


@_boots_or_uses_live_server
def test_ensure_workspace_harmonograf_reuses_live_record(
    session_harmonograf: tuple[Path, WorkspaceHarmonografHandle],
) -> None:
    """A second ensure with a LIVE record reuses it (launched=False).

    The "first" server is the session-shared one (whose fixture already
    asserted ``launched is True`` on its fresh workspace); this probe is
    read-only against it.
    """
    ws, first = session_harmonograf

    second = ensure_workspace_harmonograf(ws)
    # Reused — no second server, no shutdown ownership.
    assert second.launched is False
    assert second.web_url == first.web_url
    assert second.grpc_target == first.grpc_target
    # second.shutdown() must be a no-op (it does not own the server).
    second.shutdown()
    # The first server is still alive after the reuser's no-op shutdown.
    assert _wait_for_health(first.web_url, deadline_s=5.0)


@_boots_or_uses_live_server
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


def test_healthz_probe_false_on_dead_and_non_harmonograf_ports() -> None:
    """``_harmonograf_healthz_ok`` is a *true* liveness signal.

    A bare TCP connect would say "alive" for ANY listener that accepts a
    connection on the port. The healthz probe must say "alive" ONLY when
    the port answers harmonograf's ``/healthz`` with 200 — so a recycled
    pid / unrelated process that grabbed the freed web port is correctly
    rejected (the stale-record-reuse bug).
    """
    # 1. A non-positive port -> not ok (cheap guard, no socket).
    assert supervisor._harmonograf_healthz_ok("127.0.0.1", 0) is False
    # 2. A port nobody binds -> not ok (grab+release an ephemeral port).
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as _s:
        _s.bind(("127.0.0.1", 0))
        dead_port = _s.getsockname()[1]
    assert supervisor._harmonograf_healthz_ok("127.0.0.1", dead_port) is False

    # 3. A live listener that is NOT a harmonograf: a bare TCP connect
    #    SUCCEEDS against it, but the healthz probe must still say "not
    #    ok" because it does not answer /healthz with 200. This is the
    #    exact failure mode the probe upgrade guards against.
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    try:
        # The weak check is fooled by any open port...
        assert supervisor._port_reachable("127.0.0.1", port) is True
        # ...the strong check is not.
        assert supervisor._harmonograf_healthz_ok("127.0.0.1", port, timeout_s=1.0) is False
    finally:
        srv.close()


@_boots_or_uses_live_server
def test_healthz_probe_true_on_live_harmonograf(
    session_harmonograf: tuple[Path, WorkspaceHarmonografHandle],
) -> None:
    """``_harmonograf_healthz_ok`` returns True against a real server.

    Read-only probe — shares the session server rather than booting one.
    """
    _ws, handle = session_harmonograf
    from urllib.parse import urlparse

    parsed = urlparse(handle.web_url)
    assert parsed.port is not None
    assert (
        supervisor._harmonograf_healthz_ok(
            parsed.hostname or "127.0.0.1", parsed.port, timeout_s=2.0
        )
        is True
    )


@_boots_or_uses_live_server
def test_ensure_relaunches_when_recorded_port_is_not_harmonograf(tmp_path: Path) -> None:
    """A record whose pid is alive AND port is TCP-reachable but is NOT a
    harmonograf must NOT be reused — it must relaunch.

    This is the live-run bug: the recorded harmonograf process died, and
    an unrelated listener (or this very test's dummy socket) holds the
    freed web port. A bare TCP-connect liveness check would (wrongly)
    reuse it and advertise a dead ``harmonograf_url``; the /healthz probe
    rejects it.
    """
    import json as _json

    data_dir = _harmonograf_data_dir(tmp_path)
    data_dir.mkdir(parents=True, exist_ok=True)

    # A dummy listener that accepts TCP connects but is NOT a harmonograf.
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    decoy_port = srv.getsockname()[1]

    # pid = THIS process (definitely alive) so the pid check passes; the
    # port is TCP-reachable so the *old* bare-connect check would pass too.
    record = {
        "web_url": f"http://127.0.0.1:{decoy_port}",
        "grpc_target": f"127.0.0.1:{decoy_port}",
        "pid": os.getpid(),
        "started_iso": "2020-01-01T00:00:00Z",
    }
    (data_dir / "server.json").write_text(_json.dumps(record))

    handle = ensure_workspace_harmonograf(tmp_path)
    try:
        # Not reused (the decoy is not a harmonograf) ⇒ a fresh launch on
        # a DIFFERENT port that actually answers /healthz.
        assert handle.launched is True
        assert handle.web_url != f"http://127.0.0.1:{decoy_port}"
        assert _wait_for_health(handle.web_url, deadline_s=15.0)
        # The stale record was overwritten to name the live server.
        rec = _json.loads((data_dir / "server.json").read_text())
        assert rec["web_url"] == handle.web_url
    finally:
        handle.shutdown()
        srv.close()


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


@_boots_or_uses_live_server
def test_readiness_timeout_stops_the_server_and_returns_noop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A launch whose web listener never proves healthy is STOPPED, not leaked.

    The probe is forced to fail so the readiness deadline always expires:
    the launcher must tear the just-started server down (the worker thread
    unparks only on request_stop, so its exit IS the proof the app received
    it) and hand back the no-op handle.
    """
    import threading
    import time as _time

    from zicato.telemetry import harmonograf_supervisor as supervisor

    monkeypatch.setattr(supervisor, "_harmonograf_healthz_ok", lambda *a, **k: False)
    # Scope the leak check to the thread THIS launch creates — a sibling
    # test's (daemon) supervisor thread may legitimately still exist.
    before = {t.ident for t in threading.enumerate()}
    handle = start_harmonograf(tmp_path, readiness_timeout_s=0.2)

    assert handle.url == "", "a launch that never became ready must return the no-op handle"
    assert handle.grpc_port == 0
    deadline = _time.monotonic() + 15.0
    while _time.monotonic() < deadline:
        if not any(
            t.name == "zicato-harmonograf-supervisor" and t.ident not in before and t.is_alive()
            for t in threading.enumerate()
        ):
            break
        _time.sleep(0.05)
    else:
        raise AssertionError("the harmonograf worker thread outlived the readiness timeout")


@_boots_or_uses_live_server
def test_workspace_launch_timeout_writes_no_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A timed-out launch must not persist a server.json for later reuse."""
    import functools

    from zicato.telemetry import harmonograf_supervisor as supervisor

    (tmp_path / ".zicato").mkdir()
    monkeypatch.setattr(supervisor, "_harmonograf_healthz_ok", lambda *a, **k: False)
    monkeypatch.setattr(
        supervisor,
        "start_harmonograf",
        functools.partial(start_harmonograf, readiness_timeout_s=0.2),
    )

    handle = supervisor.ensure_workspace_harmonograf(tmp_path)

    assert handle.web_url == ""
    assert not (
        tmp_path / ".harmonograf" / "server.json"
    ).exists(), "a server that never became ready must leave no reusable record"
