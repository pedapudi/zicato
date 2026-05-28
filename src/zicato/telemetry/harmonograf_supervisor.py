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
"""

from __future__ import annotations

import asyncio
import logging
import socket
import tempfile
import threading
from collections.abc import Awaitable
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


# Default grace period (seconds) for shutdown — matches harmonograf's own
# ServerConfig.grace_seconds default. Tests can override via the
# shutdown() argument; the orchestrator uses the default.
_DEFAULT_SHUTDOWN_GRACE_S: float = 5.0


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
    Evolve continues with JSONL-only telemetry — exactly as it did
    before #202.

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
    except ImportError as exc:
        log.warning(
            "harmonograf auto-launch skipped: harmonograf_server not "
            "installed (%s); evolve continues with JSONL-only telemetry",
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
                # Park until shutdown() flips the stop event.
                await app._stop_event.wait()  # noqa: SLF001 — request_stop is the public flip
                await app.stop()

            loop.run_until_complete(_bootstrap())
        except Exception as exc:  # noqa: BLE001 — propagate via state, not the thread
            state["error"] = exc
            ready.set()
        finally:
            try:
                inner_loop = state.get("loop")
                if inner_loop is not None and not inner_loop.is_closed():
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
    if not ready.wait(timeout=30.0):
        log.warning(
            "harmonograf auto-launch did not signal ready within 30s; "
            "evolve continues with JSONL-only telemetry"
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
# Meta-loop sink helpers
# ---------------------------------------------------------------------------


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
    # Sanitize: harmonograf session ids are free-form strings but ':'
    # and ' ' in an ISO timestamp would interact badly with terminal
    # URLs. Replace with safe separators.
    safe = evolve_started_at_iso.replace(":", "-").replace(" ", "_")
    return f"zicato-meta-loop-{safe}"


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
    runtime, alongside the canonical JSONL sink for the meta-loop. As
    of #202 the orchestrator does not yet build that runtime; this
    helper is the seam — once a Runtime exists, attach the sink with
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

        from zicato.telemetry.sink import _harmonograf_grpc_target  # noqa: PLC0415
    except ImportError as exc:
        log.warning("meta-loop harmonograf sink skipped: client unavailable (%s)", exc)
        return None
    try:
        target = _harmonograf_grpc_target(harmonograf_url)
        client = Client(name=f"zicato-meta:{session_id}", server_addr=target)
        # The HarmonografSink does not currently accept a session_id on
        # construction — sessions are derived per-run from goldfive's
        # own metadata. The client name carries the session label so
        # the harmonograf console at least distinguishes meta-loop
        # traffic; a follow-up may thread session_id explicitly once
        # harmonograf_client exposes it.
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
    "build_meta_loop_sink",
    "meta_loop_session_id",
    "start_harmonograf",
]
