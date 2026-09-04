"""Auto-launch and lifecycle of an in-process harmonograf server.

Historically zicato treated the harmonograf console as an external
process the operator was responsible for starting; setting
``ZICATO_HARMONOGRAF_URL`` (or the ``integration.harmonograf_url``
workspace-config key) merely attached a live-streaming sink to every
inner-harness run and surfaced a "watch live" link in the heartbeat.
The default behaviour (env unset) was JSONL-only telemetry.

Self-hosting flips the default: when no URL is configured, zicato
launches a harmonograf server in-process at evolve startup, bound to a
free localhost port, and the rest of the pipeline (heartbeat, worker
sinks, dashboard deep-links) sees that auto-launched URL exactly as it
would see an externally-supplied one. Setting the env var or config
key opts back out — useful for an operator who wants to stream multiple
zicato invocations into a single shared harmonograf instance.

The module exposes one entry point — :func:`start_harmonograf` — which
returns a :class:`HarmonografHandle` carrying the resolved URL and an
idempotent ``shutdown()`` method. The orchestrator registers shutdown
in its evolve teardown ``finally`` block so a Ctrl-C, an unhandled
exception, or a normal completion all leave no orphaned process.

Failure isolation is load-bearing: a missing :mod:`harmonograf_server`
dependency, a port-bind failure, or any startup exception logs a
warning and returns a no-op handle whose ``url`` is the empty string.
The live console is an additive convenience; evolve must continue
without it on a degraded install.

The server and streaming client belong to the ``observability`` extra. A base
install records canonical JSONL telemetry and otherwise runs the same loop.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
import logging
import os
import re
import socket
import tempfile
import threading
from collections.abc import Awaitable
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_STARTUP_HANDOFF_TIMEOUT_S = 30.0


def _wait_for_startup_handoff(ready: threading.Event) -> bool:
    return ready.wait(timeout=_STARTUP_HANDOFF_TIMEOUT_S)


# Default grace period (seconds) for shutdown — matches harmonograf's own
# ServerConfig.grace_seconds default. Tests can override via the
# shutdown() argument; the orchestrator uses the default.
_DEFAULT_SHUTDOWN_GRACE_S: float = 5.0


def _close_rejected_idle_coroutines() -> None:
    """Backport safe task creation for the embedded HTTP server.

    Python 3.11 leaves a coroutine open when ``TaskGroup.create_task`` rejects
    work during shutdown. A late keep-alive update can hit that edge while the
    server task group is closing. Newer runtimes close it; mirror that behavior
    here until the minimum runtime advances.
    """
    try:
        from hypercorn.asyncio.worker_context import AsyncioSingleTask  # noqa: PLC0415
    except ImportError:
        # Best-effort by definition: this is a WORKAROUND for one hypercorn
        # edge case on an older runtime, not a requirement of the server it
        # patches. Raising here propagates into the caller's ImportError
        # guard and disables live telemetry wholesale -- turning "the patch
        # does not apply" into "the feature is unavailable".
        return

    if getattr(AsyncioSingleTask.restart, "_zicato_safe", False):
        return

    async def restart(self: Any, task_group: Any, action: Any) -> None:
        async with self._lock:
            if self._handle is not None:
                self._handle.cancel()
                try:
                    await self._handle
                except asyncio.CancelledError:
                    pass

            coroutine = action()
            try:
                self._handle = task_group._task_group.create_task(coroutine)
            except BaseException:
                coroutine.close()
                raise

    restart._zicato_safe = True  # type: ignore[attr-defined]
    AsyncioSingleTask.restart = restart  # type: ignore[method-assign]


def _pick_free_port() -> int:
    """Bind to ``('127.0.0.1', 0)`` and return the OS-assigned port.

    A standard "ephemeral port" idiom: open a socket on port 0, let the
    kernel pick a free port, read it back, close the socket. The same
    port is then handed to harmonograf-server. There is a small race
    window where another process could grab the port between the close
    here and the bind inside harmonograf — accept it; the alternative
    (hardcoded ports or a retry loop) is worse.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class HarmonografHandle:
    """Lifecycle handle for an auto-launched harmonograf server.

    Attributes
    ----------
    url:
        Browser-resolvable URL of the running harmonograf-web (sonora)
        listener, e.g. ``"http://127.0.0.1:42017"``. The empty string
        when the supervisor decided not to launch (failure isolation
        path) — callers MUST treat an empty URL as "no live console" and
        proceed with JSONL-only telemetry.
    grpc_port:
        Native gRPC port the worker subprocesses dial. The harmonograf
        client library accepts a bare ``host:port`` target; the URL above
        names the web port, the worker-side helper
        :func:`zicato.telemetry.sink._harmonograf_grpc_target` already
        strips the scheme — but auto-launched runs need a way for the
        client to reach the gRPC port too. Both ports are exposed so the
        rest of the wiring stays uniform. ``0`` on the no-op handle.
    """

    def __init__(
        self,
        *,
        url: str,
        grpc_port: int,
        app: Any | None = None,
        loop: asyncio.AbstractEventLoop | None = None,
        thread: threading.Thread | None = None,
    ) -> None:
        self.url = url
        self.grpc_port = grpc_port
        self._app = app
        self._loop = loop
        self._thread = thread
        self._shutdown_called = False
        self._shutdown_lock = threading.Lock()

    def shutdown(self, grace_seconds: float = _DEFAULT_SHUTDOWN_GRACE_S) -> None:
        """Stop the harmonograf server cleanly. Idempotent.

        Schedules :meth:`Harmonograf.stop` on the server's event loop and
        waits up to ``grace_seconds + 2`` for the worker thread to join.
        A no-op when the handle is the failure-isolation no-op (``url``
        is empty / no app / no thread) or when shutdown has already been
        called — re-calling MUST NOT raise so the orchestrator's
        ``finally`` block can call shutdown unconditionally.
        """
        with self._shutdown_lock:
            if self._shutdown_called:
                return
            self._shutdown_called = True

        if self._app is None or self._loop is None or self._thread is None:
            # No-op handle (failure isolation path) — nothing to stop.
            return

        try:
            # The Harmonograf.run() coroutine waits on an internal stop
            # event; request_stop() sets it, the run() awaits stop(), and
            # the worker thread's asyncio.run() returns.
            self._loop.call_soon_threadsafe(self._app.request_stop)
        except RuntimeError as exc:
            # The loop may already be closed (e.g. shutdown races with a
            # worker thread that crashed at startup). Log and fall
            # through to the join — the thread will be dead.
            log.debug("harmonograf shutdown: loop closed before stop: %s", exc)

        self._thread.join(timeout=grace_seconds + 2.0)
        if self._thread.is_alive():
            log.warning(
                "harmonograf worker thread did not exit within %.1fs of shutdown",
                grace_seconds + 2.0,
            )

    def __enter__(self) -> HarmonografHandle:
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.shutdown()


def _noop_handle() -> HarmonografHandle:
    """A handle that did not actually launch a server.

    Returned on failure-isolation paths so callers do not have to handle
    a ``None``. ``url`` is the empty string — the existing harmonograf
    plumbing already treats an empty URL as "JSONL-only telemetry" — and
    ``shutdown()`` is a no-op.
    """
    return HarmonografHandle(url="", grpc_port=0)


def start_harmonograf(
    workspace_root: Path,
    *,
    log_level: str = "WARNING",
) -> HarmonografHandle:
    """Launch an in-process harmonograf server bound to a free port.

    The server runs on a dedicated daemon thread driving its own asyncio
    event loop — harmonograf-server's :class:`Harmonograf` composition
    root expects to own an event loop, and the orchestrator's main
    coroutine already uses ``asyncio.run`` so a shared loop is not
    available. Using a daemon thread means the server is torn down when
    the orchestrator process exits, even if :meth:`HarmonografHandle.shutdown`
    is skipped by an abrupt failure.

    Storage is parked under ``<workspace_root>/.harmonograf/`` so the
    sqlite database and payload files travel with the workspace
    (mirrors how the dashboard reads ``<workspace_root>/.zicato``).
    ``workspace_root`` may either point at the ``.zicato`` directory or
    at its parent — the supervisor materialises a ``.harmonograf``
    sibling either way.

    Failure isolation: any exception during port selection, import, or
    startup is caught and a no-op handle is returned with ``url=""``.
    Evolve then continues with JSONL-only telemetry.

    Parameters
    ----------
    workspace_root:
        The zicato workspace directory (or its parent). Used to
        derive the harmonograf data directory.
    log_level:
        harmonograf-server log level. The default ``"WARNING"`` is
        chosen so the auto-launched server stays quiet in normal
        operation; tests bumping this to ``"DEBUG"`` can inspect server
        startup logs.

    Returns
    -------
    HarmonografHandle
        With a non-empty ``url`` and a working ``shutdown()`` on the
        happy path; with an empty ``url`` and a no-op ``shutdown()`` on
        every isolated failure.
    """
    try:
        # Lazy import: a degraded install without harmonograf-server
        # must still let evolve run. The same tolerance harmonograf-client
        # already has in zicato.telemetry.sink._make_harmonograf_sink.
        from harmonograf_server.config import ServerConfig  # noqa: PLC0415
        from harmonograf_server.main import Harmonograf  # noqa: PLC0415

        _close_rejected_idle_coroutines()
    except ImportError as exc:
        log.warning(
            "live telemetry unavailable: install zicato[observability] (%s); "
            "evolve continues with JSONL-only telemetry",
            exc,
        )
        return _noop_handle()

    try:
        grpc_port = _pick_free_port()
        web_port = _pick_free_port()
        while web_port == grpc_port:
            # The two ports MUST differ — harmonograf binds them
            # independently. The probability of collision is vanishing
            # (two consecutive ephemeral-port picks), but harden anyway.
            web_port = _pick_free_port()

        data_dir = _resolve_data_dir(workspace_root)
        cfg = ServerConfig(
            host="127.0.0.1",
            grpc_port=grpc_port,
            web_port=web_port,
            store_backend="sqlite",
            data_dir=str(data_dir),
            log_level=log_level,
            metrics_interval_seconds=0.0,  # quiet by default for an embedded server
            grace_seconds=_DEFAULT_SHUTDOWN_GRACE_S,
        )
    except Exception as exc:  # noqa: BLE001 — never block evolve on supervisor errors
        log.warning(
            "harmonograf auto-launch skipped: could not build server "
            "config (%s); evolve continues with JSONL-only telemetry",
            exc,
        )
        return _noop_handle()

    # The worker thread owns its asyncio loop. We have to capture both
    # the loop reference (for call_soon_threadsafe at shutdown) and the
    # Harmonograf app instance (for request_stop). Both are set inside
    # the worker's startup coroutine before it parks on the stop event.
    ready = threading.Event()
    abandoned = threading.Event()
    state: dict[str, Any] = {"loop": None, "app": None, "error": None}

    def _serve() -> None:
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            state["loop"] = loop

            async def _bootstrap() -> Any:
                app = await Harmonograf.from_config(cfg)
                await app.start()
                state["app"] = app
                ready.set()
                # A caller that timed out cannot own this server.
                if abandoned.is_set():
                    app.request_stop()
                # Park until shutdown() flips the stop event.
                await app._stop_event.wait()  # noqa: SLF001 — request_stop is the public flip
                await app.stop()

            loop.run_until_complete(_bootstrap())
        except Exception as exc:  # noqa: BLE001 — propagate via state rather than the thread
            state["error"] = exc
            ready.set()
        finally:
            try:
                inner_loop = state.get("loop")
                if inner_loop is not None and not inner_loop.is_closed():
                    pending = asyncio.all_tasks(inner_loop)
                    for task in pending:
                        task.cancel()
                    if pending:
                        inner_loop.run_until_complete(
                            asyncio.gather(*pending, return_exceptions=True)
                        )
                    inner_loop.run_until_complete(inner_loop.shutdown_asyncgens())
                    inner_loop.close()
            except Exception:  # noqa: BLE001 — best-effort
                pass

    thread = threading.Thread(
        target=_serve,
        name="zicato-harmonograf-supervisor",
        daemon=True,
    )
    thread.start()
    # Bounded wait — harmonograf's start() is fast (sub-second on a warm
    # cache); a 30s ceiling is conservative and bails out fast if
    # something is wedged (port permission denied, sqlite init blew up).
    if not _wait_for_startup_handoff(ready):
        abandoned.set()
        # Cover a worker that passed its abandonment check just before timeout.
        late_app = state.get("app")
        late_loop = state.get("loop")
        if late_app is not None and late_loop is not None:
            try:
                late_loop.call_soon_threadsafe(late_app.request_stop)
            except RuntimeError:
                pass  # the worker already exited
        log.warning(
            "harmonograf auto-launch did not signal ready within %.1fs; "
            "evolve continues with JSONL-only telemetry",
            _STARTUP_HANDOFF_TIMEOUT_S,
        )
        return _noop_handle()
    if state.get("error") is not None:
        log.warning(
            "harmonograf auto-launch failed (%s); evolve continues with " "JSONL-only telemetry",
            state["error"],
        )
        return _noop_handle()
    if state.get("app") is None or state.get("loop") is None:
        log.warning(
            "harmonograf auto-launch did not produce a server handle; "
            "evolve continues with JSONL-only telemetry"
        )
        return _noop_handle()

    # Harmonograf.start() owns listener readiness and partial-startup rollback.
    url = f"http://127.0.0.1:{web_port}"
    log.info("harmonograf auto-launched at %s (grpc %d)", url, grpc_port)
    return HarmonografHandle(
        url=url,
        grpc_port=grpc_port,
        app=state["app"],
        loop=state["loop"],
        thread=thread,
    )


def _resolve_data_dir(workspace_root: Path) -> Path:
    """Pick a data directory for harmonograf under the workspace.

    ``workspace_root`` may be the ``.zicato`` directory itself or its
    parent (the orchestrator hands in whichever is available); in either
    case the harmonograf data directory lives at
    ``<parent>/.harmonograf`` so multiple workspaces under the same
    parent get distinct databases.

    A failure to create the directory falls back to a temp dir so a
    read-only workspace does not block evolve.
    """
    try:
        if workspace_root.name == ".zicato":
            parent = workspace_root.parent
        else:
            parent = workspace_root
        data_dir = parent / ".harmonograf"
        data_dir.mkdir(parents=True, exist_ok=True)
        return data_dir
    except Exception as exc:  # noqa: BLE001 — fall back to a temp dir
        log.debug("harmonograf data_dir under workspace failed (%s); using tempdir", exc)
        return Path(tempfile.mkdtemp(prefix="zicato-harmonograf-"))


# ---------------------------------------------------------------------------
# Per-workspace persistent server (standalone dashboard / builder)
# ---------------------------------------------------------------------------
#
# A live ``zicato evolve`` auto-launches a harmonograf server that dies
# with the run. The persisted sessions, however, live on in
# ``<workspace>/.harmonograf/harmonograf.db``. A standalone ``zicato
# dashboard`` / ``zicato dashboard --view builder`` wants to surface those persisted
# sessions for a post-mortem execution view — but the evolve-launched
# server is gone, so there is no URL to deep-link into.
#
# :func:`ensure_workspace_harmonograf` closes that gap: it launches (or
# reuses) ONE persistent harmonograf server per workspace, bound to that
# workspace's existing sqlite db, and records its endpoint in
# ``.harmonograf/server.json`` so a second caller (a concurrent evolve,
# a second dashboard tab's process) reuses the same server instead of
# opening a SECOND server on the same sqlite file.
#
# sqlite double-open resolution
# -----------------------------
# The ``server.json`` record is the single-server-per-workspace contract.
# Every launcher — the standalone dashboard, the standalone builder, AND
# the evolve auto-launch path — routes through this helper, which:
#   1. reads ``server.json``; if it names a LIVE server (pid alive AND
#      the web port answers a TCP connect), reuses it verbatim; else
#   2. launches a fresh server bound to ``harmonograf.db`` and rewrites
#      the record.
# Because both paths consult the same record first, no two servers ever
# bind the same db: whoever wins the race writes the record, the loser
# reuses it. A stale record left by a crashed/killed process fails the
# liveness probe and is overwritten.


#: The per-workspace server record. Holds enough to (a) reuse a live
#: server and (b) probe whether the recorded server is still alive.
_SERVER_RECORD_NAME = "server.json"


def _utc_now_iso() -> str:
    return _dt.datetime.now(_dt.UTC).isoformat().replace("+00:00", "Z")


def _server_record_path(data_dir: Path) -> Path:
    return data_dir / _SERVER_RECORD_NAME


def _pid_alive(pid: int) -> bool:
    """True when ``pid`` names a live process this user can signal.

    ``os.kill(pid, 0)`` raises ``ProcessLookupError`` for a dead pid and
    ``PermissionError`` for a live process owned by another user (treated
    as alive — it exists). A non-positive pid is never alive.
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _port_reachable(host: str, port: int, *, timeout_s: float = 0.5) -> bool:
    """True when a TCP connect to ``host:port`` succeeds within ``timeout_s``.

    The liveness half that a bare pid check cannot give: a recycled pid
    could be alive while the harmonograf port it once owned is gone.
    """
    if port <= 0:
        return False
    try:
        with socket.create_connection((host, port), timeout=timeout_s):
            return True
    except OSError:
        return False


def _harmonograf_healthz_ok(host: str, port: int, *, timeout_s: float = 1.0) -> bool:
    """True when ``host:port`` answers harmonograf's ``/healthz`` with 200.

    A bare TCP connect (:func:`_port_reachable`) is too weak a liveness
    signal for the reuse path: harmonograf binds TWO ports and the
    ``web_url`` we record is the gRPC-Web port, which a *recycled* pid (or
    an unrelated process that grabbed the freed port) can accept a TCP
    connection on without being a live harmonograf at all. The
    consequence is the bug this guards: a ``server.json`` whose owning
    process has died — but whose port a TCP connect still "succeeds"
    against — gets REUSED, and the orchestrator then advertises a dead
    ``harmonograf_url`` (the deep-link 404s, the sinks dial a closed
    gRPC socket).

    harmonograf-server mounts ``/healthz`` on the web port (always 200
    while the process is serving requests, exempt from the bearer guard),
    so a 200 there is positive proof the recorded server is a live
    harmonograf. We use ``http.client`` rather than ``urllib`` — the
    latter mishandles harmonograf's hypercorn listener — and keep the
    probe free of any third-party dependency.
    """
    if port <= 0:
        return False
    import http.client  # noqa: PLC0415 — stdlib, keep the import local to the probe

    conn: http.client.HTTPConnection | None = None
    try:
        conn = http.client.HTTPConnection(host, port, timeout=timeout_s)
        conn.request("GET", "/healthz")
        resp = conn.getresponse()
        # Drain the body so the connection closes cleanly.
        resp.read()
        return resp.status == 200
    except (OSError, http.client.HTTPException):
        return False
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001 — best-effort close
                pass


def _coerce_pid(value: Any) -> int:
    """Coerce a record's ``pid`` field to an int (``0`` when unusable)."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _parse_host_port(target: str) -> tuple[str, int]:
    """Split a ``host:port`` grpc target into its parts (``("", 0)`` on error)."""
    if not target or ":" not in target:
        return "", 0
    host, _, port_s = target.rpartition(":")
    try:
        return host or "127.0.0.1", int(port_s)
    except ValueError:
        return "", 0


def _read_server_record(data_dir: Path) -> dict[str, Any] | None:
    """Read ``server.json``; missing / malformed -> ``None``."""
    path = _server_record_path(data_dir)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _write_server_record(data_dir: Path, record: dict[str, Any]) -> None:
    """Persist ``server.json`` best-effort (a write failure is non-fatal)."""
    try:
        _server_record_path(data_dir).write_text(json.dumps(record) + "\n", encoding="utf-8")
    except OSError as exc:
        log.debug("could not write harmonograf server record: %s", exc)


class WorkspaceHarmonografHandle:
    """Handle for a per-workspace harmonograf server (standalone path).

    Attributes
    ----------
    web_url:
        Browser-resolvable URL of the running harmonograf-web listener,
        e.g. ``"http://127.0.0.1:42017"``. The empty string on the
        failure-isolation no-op path — callers MUST treat an empty URL as
        "no live console" and serve the dashboard anyway.
    grpc_target:
        The native ``host:port`` gRPC target (for sinks); ``""`` on the
        no-op handle.
    launched:
        ``True`` when THIS handle started the server (so the caller owns
        its lifecycle and must :meth:`shutdown`); ``False`` when it reused
        an already-running per-workspace server (leave it running — its
        owner, e.g. a live evolve or another dashboard process, tears it
        down).
    reason:
        Short human-readable explanation for a no-op handle (empty
        ``web_url``) — e.g. ``"workspace absent"`` or a launch error — so
        a caller can surface WHY there are no deep-links. The empty string
        on the live (launched/reused) path.
    """

    def __init__(
        self,
        *,
        web_url: str,
        grpc_target: str,
        launched: bool,
        inner: HarmonografHandle | None = None,
        reason: str = "",
    ) -> None:
        self.web_url = web_url
        self.grpc_target = grpc_target
        self.launched = launched
        self.reason = reason
        self._inner = inner

    def shutdown(self, grace_seconds: float = _DEFAULT_SHUTDOWN_GRACE_S) -> None:
        """Stop the server iff this handle launched it. Idempotent, never raises.

        A reused server (``launched is False``) is left running — its
        owning process is responsible for it. A no-op handle (no inner
        server) does nothing.
        """
        if not self.launched or self._inner is None:
            return
        try:
            self._inner.shutdown(grace_seconds)
        except Exception as exc:  # noqa: BLE001 — never raise from teardown
            log.debug("workspace harmonograf shutdown raised: %s", exc)

    def __enter__(self) -> WorkspaceHarmonografHandle:
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.shutdown()


def _noop_workspace_handle(reason: str = "") -> WorkspaceHarmonografHandle:
    return WorkspaceHarmonografHandle(web_url="", grpc_target="", launched=False, reason=reason)


def ensure_workspace_harmonograf(workspace_root: Path) -> WorkspaceHarmonografHandle:
    """Reuse-or-launch ONE persistent harmonograf server for a workspace.

    Bound to the workspace's existing ``.harmonograf/harmonograf.db`` so a
    standalone dashboard / builder can deep-link into the persisted
    sessions even when no live evolve is running.

    Resolution:

    1. If ``.harmonograf/server.json`` names a server whose pid is alive
       AND whose web port answers a TCP connect, REUSE it — return a
       handle with ``launched=False`` (the caller must NOT shut it down).
    2. Otherwise (no record, a malformed record, or a stale record whose
       process is dead / port is gone) LAUNCH a fresh server via
       :func:`start_harmonograf`, rewrite ``server.json``, and return a
       handle with ``launched=True`` (the caller owns its lifecycle).

    Failure-isolated: any failure — a missing harmonograf-server dep, a
    port-bind error, an unreachable launch — logs a warning and returns a
    no-op handle (``web_url=""``). The dashboard MUST still run.
    """
    # Resolve to an absolute path up front: a relative ``workspace_root``
    # (e.g. the ``.zicato`` default the CLI passes) would otherwise land
    # the ``.harmonograf`` data dir under the current working directory.
    # The server record + db must travel with the named workspace, never
    # the cwd.
    try:
        workspace_root = Path(workspace_root).resolve()
    except Exception as exc:  # noqa: BLE001 — never block the dashboard
        log.warning("harmonograf workspace path unresolvable (%s)", exc)
        return _noop_workspace_handle(f"workspace path unresolvable: {exc}")

    # Only stand up a persistent server for a workspace that actually
    # exists on disk. A dashboard pointed at a not-yet-created workspace
    # (or a bare default ``.zicato`` that was never initialised) has no
    # persisted sessions to surface, and creating a ``.harmonograf`` next
    # to a phantom workspace would just litter the cwd. A no-op handle
    # keeps the dashboard serving without a harmonograf link.
    ws_dir = workspace_root if workspace_root.name == ".zicato" else workspace_root / ".zicato"
    if not ws_dir.is_dir() and not workspace_root.is_dir():
        log.debug("harmonograf workspace %s absent; no persistent server", workspace_root)
        return _noop_workspace_handle("workspace absent")

    try:
        data_dir = _resolve_data_dir(workspace_root)
    except Exception as exc:  # noqa: BLE001 — never block the dashboard
        log.warning("harmonograf workspace data dir unavailable (%s)", exc)
        return _noop_workspace_handle(f"data dir unavailable: {exc}")

    # 1. Reuse an already-running per-workspace server, if the record is live.
    record = _read_server_record(data_dir)
    if record is not None:
        web_url = str(record.get("web_url") or "")
        grpc_target = str(record.get("grpc_target") or "")
        pid = _coerce_pid(record.get("pid"))
        host, port = _parse_host_port(web_url.split("//", 1)[-1])
        # Liveness is a CONJUNCTION of three checks, weakest-to-strongest:
        #   1. the recorded pid is still a live process (cheap, but a
        #      recycled pid lies),
        #   2. the recorded web port answers harmonograf's ``/healthz``
        #      with 200 — positive proof the live process IS a serving
        #      harmonograf rather than merely *some* process that grabbed the
        #      freed port. A bare TCP connect is too weak here: the port
        #      can accept a connection from an unrelated listener (or a
        #      lingering socket) after the real server died, and a stale
        #      record would then be reused and a dead ``harmonograf_url``
        #      advertised.
        if web_url and _pid_alive(pid) and _harmonograf_healthz_ok(host or "127.0.0.1", port):
            log.debug("reusing live per-workspace harmonograf at %s (pid %d)", web_url, pid)
            return WorkspaceHarmonografHandle(
                web_url=web_url, grpc_target=grpc_target, launched=False
            )
        # Stale record (dead pid, or a web port that no longer answers
        # /healthz) — fall through and relaunch; the write below overwrites
        # it.
        log.debug("ignoring stale harmonograf server record at %s", web_url or "<empty>")

    # 2. Launch a fresh server bound to this workspace's sqlite db.
    try:
        inner = start_harmonograf(workspace_root)
    except Exception as exc:  # noqa: BLE001 — start_harmonograf is itself isolated
        log.warning("harmonograf workspace launch raised (%s)", exc)
        return _noop_workspace_handle(f"launch raised: {exc}")
    if not inner.url:
        # start_harmonograf already logged its own failure-isolation warning.
        return _noop_workspace_handle("server did not start")

    grpc_target = f"127.0.0.1:{inner.grpc_port}" if inner.grpc_port else ""
    _write_server_record(
        data_dir,
        {
            "web_url": inner.url,
            "grpc_target": grpc_target,
            "pid": os.getpid(),
            "started_iso": _utc_now_iso(),
        },
    )
    log.info("launched per-workspace harmonograf at %s (grpc %s)", inner.url, grpc_target)
    return WorkspaceHarmonografHandle(
        web_url=inner.url, grpc_target=grpc_target, launched=True, inner=inner
    )


# ---------------------------------------------------------------------------
# Meta-loop sink helpers
# ---------------------------------------------------------------------------


# harmonograf validates agent / session names against
# ``^[a-zA-Z0-9_-]{1,128}$``; any name handed to
# ``harmonograf_client.Client(name=...)`` MUST match or the sink
# construction raises and the live link points at nothing. The sanitizer
# below substitutes every disallowed character so its output always
# satisfies that rule.
_AGENT_NAME_DISALLOWED = re.compile(r"[^a-zA-Z0-9_-]")


def _sanitize_agent_name(name: str) -> str:
    """Coerce ``name`` to match harmonograf's ``[a-zA-Z0-9_-]{1,128}`` rule.

    Any character outside the allowed class (``:`` and ``+`` from a
    namespaced/ISO-offset label, ``.`` from a dotted label, whitespace,
    etc.) is replaced with ``-``. An empty input becomes a single ``-``
    (the regex forbids zero-length names), and the result is truncated to
    128 characters so it always satisfies the upper bound. The mapping is
    deterministic and readable, so the id stays stable across an evolve.
    """
    safe = _AGENT_NAME_DISALLOWED.sub("-", name)
    if not safe:
        safe = "-"
    return safe[:128]


def meta_loop_session_id(evolve_started_at_iso: str) -> str:
    """Build the stable session id for the orchestrator's meta-loop events.

    The orchestrator's own proposer + judge calls are conceptually a
    distinct "session" from any board-run session (which uses
    ``<gen_id>--<entry_id>``). Bucketing them under a single stable id
    per evolve invocation lets the dashboard render the meta-loop as
    one continuous timeline rather than scattering events across
    per-round sessions.

    The id is fully deterministic for a given evolve start time, so a
    reducer / dashboard view that needs to recover the session id later
    only needs the evolve start ISO timestamp.
    """
    # Sanitize: the id is later handed to harmonograf as an agent name,
    # which must match ``[a-zA-Z0-9_-]{1,128}``. An ISO timestamp carries
    # ':' (time + offset) and '+' (UTC offset sign) — both out-of-class —
    # so coerce every disallowed character to '-' (e.g.
    # ``2026-05-30T17:30:26+00:00`` -> ``2026-05-30T17-30-26-00-00``).
    return _sanitize_agent_name(f"zicato-meta-loop-{evolve_started_at_iso}")


def build_meta_loop_sink(harmonograf_url: str, session_id: str) -> Awaitable[Any] | Any | None:
    """Construct a harmonograf sink scoped to the meta-loop session.

    Mirrors :func:`zicato.telemetry.sink._make_harmonograf_sink` but for
    the orchestrator's own goldfive events (the ones the proposer +
    judge calls emit). Returns ``None`` when harmonograf_url is empty,
    when the client library is unavailable, or when sink construction
    raises — every failure is a logged warning, never an exception, so
    the orchestrator's evolve loop never depends on this attaching.

    The orchestrator is expected to attach the returned sink to the
    goldfive ``RuntimeConfig`` it constructs for its own (non-worker)
    runtime, alongside the canonical JSONL sink for the meta-loop. The
    orchestrator does not build that runtime, so nothing calls this yet;
    the helper is the seam. Once a Runtime exists, attach the sink with
    ``session_id`` passed through to harmonograf.

    Parameters
    ----------
    harmonograf_url:
        The resolved URL (auto-launched or external). Empty -> no sink.
    session_id:
        The stable meta-loop session id from
        :func:`meta_loop_session_id`.
    """
    if not harmonograf_url:
        return None
    try:
        from harmonograf_client import Client, HarmonografSink  # noqa: PLC0415

        from zicato.telemetry.sink import (  # noqa: PLC0415
            resolve_harmonograf_grpc_target,
        )
    except ImportError as exc:
        log.warning("meta-loop harmonograf sink skipped: client unavailable (%s)", exc)
        return None
    try:
        # Dial the native gRPC port, NOT the browser-facing gRPC-Web port
        # carried by ``harmonograf_url``. For an auto-launched server the
        # resolver prefers ``ZICATO_HARMONOGRAF_GRPC`` (host:grpc_port);
        # for an external instance it scheme-strips the single-port URL.
        target = resolve_harmonograf_grpc_target(harmonograf_url)
        # The client name is validated by harmonograf against
        # ``[a-zA-Z0-9_-]{1,128}``. A raw ``zicato-meta:{session_id}``
        # injects a ':' (and the session id may already be at the length
        # bound), so sanitize the composed name. ``session_id`` is itself
        # already ``zicato-meta-loop-...``, so the resulting name stays
        # readable, e.g. ``zicato-meta-zicato-meta-loop-...``.
        client_name = _sanitize_agent_name(f"zicato-meta:{session_id}")
        # ``harmonograf_client.Client`` accepts an explicit ``session_id``
        # so the meta-loop's proposer / judge envelopes are bucketed under
        # one stable session on the harmonograf timeline (rather than
        # scattering across per-run sessions). Pass the already-sanitized
        # ``session_id`` through — it satisfies the same name regex.
        client = Client(name=client_name, server_addr=target, session_id=session_id)
        return HarmonografSink(client)
    except Exception as exc:  # noqa: BLE001 — never hard-fail
        log.warning(
            "could not construct meta-loop harmonograf sink for %s (%s)",
            harmonograf_url,
            exc,
        )
        return None


__all__ = [
    "HarmonografHandle",
    "WorkspaceHarmonografHandle",
    "build_meta_loop_sink",
    "ensure_workspace_harmonograf",
    "meta_loop_session_id",
    "start_harmonograf",
]
