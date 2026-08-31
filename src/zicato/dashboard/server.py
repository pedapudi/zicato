"""Standalone Starlette/ASGI dashboard service for zicato.

The dashboard is its own self-contained process. It runs independently
of any other zicato process — point it at a live workspace a run is
driving, or at a completed epoch for a post-mortem, and it serves a
read-only-or-live view of that environment. ``zicato evolve`` spawns it
for the lifetime of a loop; ``zicato dashboard`` runs it standalone.

The service:

* serves the JSON environment API under ``/api/`` — ``/api/environment``
  is the single coalesced read of the whole environment, with the
  granular per-section endpoints kept alongside it;
* serves the ``/events`` SSE stream — a ``snapshot`` then live
  coalesced ``state_change`` / ``run_log`` frames;
* serves the conversation endpoints for the live and side-by-side
  transcript views;
* serves the static dashboard bundle at ``/`` and ``/static/``.

Public surface:

* :func:`create_app` — build the configured :class:`~starlette.applications.Starlette`
  app.
* :func:`run` — bind a port (walking ``+1`` if taken) and serve via
  uvicorn.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import socket
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Route

from zicato.dashboard.endpoints import READ_ENDPOINTS, make_endpoints
from zicato.dashboard.sse import ChangeBroker, sse_event_stream
from zicato.query import WorkspacePaths

log = logging.getLogger("zicato.dashboard")

# Index-fallback when the static bundle is missing entirely, so an
# operator still sees something useful at the document root.
_PLACEHOLDER_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>zicato-dashboard</title></head>
<body style="font-family:system-ui;padding:2rem;max-width:48rem">
<h1>zicato-dashboard</h1>
<p>The dashboard service is running. The UI bundle was not found.
JSON endpoints under <code>/api/</code> and the <code>/events</code>
SSE stream are available.</p>
<ul>
  <li><a href="/api/state">/api/state</a></li>
  <li><a href="/api/health">/api/health</a></li>
</ul>
</body></html>
"""


def _resolve_workspace(workspace_root: Path, *, harmonograf_url: str = "") -> WorkspacePaths:
    """Normalize a workspace argument to a :class:`WorkspacePaths`.

    Accepts either the ``.zicato`` directory itself or a project root
    that contains one — so callers can pass whichever they have.

    ``harmonograf_url`` is the persistent per-workspace harmonograf URL the
    dashboard process resolved at startup; it is stamped onto the paths so
    the state readers inject it into the heartbeat payload (lighting up the
    standalone deep-links).
    """
    root = Path(workspace_root)
    if root.name != ".zicato" and (root / ".zicato").is_dir():
        root = root / ".zicato"
    return WorkspacePaths(root, harmonograf_url=harmonograf_url)


def _ensure_index_at_startup(paths: WorkspacePaths) -> None:
    """Build an absent / wrong-schema index once, at server start.

    The read-path half of the self-healing index
    (``docs/design/ANALYTICAL-INDEX.md`` §5.3). Narrow by design:

    * **``ensure_index`` only, never ``heal_index``.** Healing writes. A
      reader healing while an orchestrator dual-writes is exactly the
      contention the single-writer rule (§2.4) exists to prevent, and it
      would put a full workspace walk in front of the first HTTP response.
      Noticing that the index CONTENTS drifted is the writer's job, and the
      writer runs the heal at the top of every ``evolve``.
    * **Once per PROCESS, never per request.** The absence/version check is
      cheap, but it is still a stat plus a pragma read; the SSE-driven
      dashboard would pay it thousands of times. Called from :func:`run`
      rather than :func:`create_app`: ``run`` is the process-start seam both
      real dashboard launches come through, and building an ASGI app must
      not have filesystem side effects.
    * **Skipped when a live evolve holds the workspace lock**, with a log
      line and no retry (§5.3's concurrency rule). The lock holder is
      already building or healing the index at its own start, so the work
      is being done by the process that owns the writes. Waiting would
      block startup for the length of an entire evolve run.
    * **Skipped on a workspace with no epochs at all.** A fresh,
      never-run workspace keeps rendering its "not yet indexed" empty
      state (§7's graceful-absence property) rather than gaining a
      valid-but-empty ``index.db`` that flips every reader's degrade
      branch.

    There is NO schema-version pre-check in front of
    ``ensure_index``. An earlier shape asked ``index_schema_version(...) ==
    SCHEMA_VERSION`` first and returned when it matched — cheap, but it
    decided the question ``ensure_index`` exists to decide, and it decided it
    differently: on a file that is not a SQLite database at all the pre-check
    RAISES, the blanket guard below swallows it at ``debug``, and the
    dashboard never repairs it. ``_rebuild_reason`` classifies that same file
    as ``unreadable`` and rebuilds. So the ``built:unreadable`` outcome §5.1
    documents was unreachable from this path because the pre-check
    ran first. ``ensure_index`` returns without writing when the index is
    current, which is all the pre-check bought.

    Never raises: the dashboard renders a degraded analytical surface far
    more gracefully than it survives a failed startup.
    """
    from zicato.index.ingest import ensure_index  # noqa: PLC0415
    from zicato.index.schema import IndexSchemaNewerError  # noqa: PLC0415
    from zicato.runtime.lock import read_workspace_lock  # noqa: PLC0415

    try:
        epochs_root = paths.root / "epochs"
        if not epochs_root.is_dir() or not any(c.is_dir() for c in epochs_root.iterdir()):
            return
        holder = read_workspace_lock(paths.root)
        if holder is not None:
            log.debug(
                "index: build/refresh skipped, workspace locked by live pid %d "
                "(the running evolve owns the index)",
                holder.pid,
            )
            return
        actions: list[str] = []
        ensure_index(paths.root, action_out=actions)
        # Only report a BUILD. Without the pre-check this runs on every
        # start, and announcing "present" each time would train the operator
        # to ignore the one line that means something happened.
        if actions:
            log.info("index: %s", actions[0])
    except IndexSchemaNewerError as exc:
        log.warning(
            "index: %s — the analytical views render from a stale index. "
            "Recover with: delete the workspace index.db and run `zicato repair index`, "
            "or serve this workspace with the newer zicato that wrote it.",
            exc,
        )
    except Exception as exc:  # noqa: BLE001 — startup index build is best-effort
        log.debug("index: startup build skipped: %s", exc)


def _if_none_match(inm: str | None, etag: str) -> bool:
    """True when the ``If-None-Match`` request header matches ``etag``.

    Handles the ``*`` wildcard and a comma-separated list, and tolerates a
    weak-validator ``W/`` prefix on the client's tag (we send a strong tag, so
    we compare ignoring the prefix). Returns False for an absent header.
    """
    if not inm:
        return False
    if inm.strip() == "*":
        return True
    for tag in inm.split(","):
        if tag.strip().removeprefix("W/") == etag:
            return True
    return False


def create_app(
    workspace_root: Path,
    static_dir: Path,
    *,
    read_only: bool = True,
    harmonograf_url: str = "",
) -> Starlette:
    """Build the dashboard ASGI application.

    Parameters
    ----------
    workspace_root:
        The ``.zicato`` directory to read live state from (a project root
        containing ``.zicato`` is also accepted).
    static_dir:
        Directory holding the dashboard UI bundle (``index.html``,
        ``app.js``, ``style.css``, ``icons.svg``).
    read_only:
        When ``True`` (the default) the POST control endpoints return
        ``403``; the GET endpoints and SSE stream are always available.
    harmonograf_url:
        The persistent per-workspace harmonograf web URL this dashboard
        process resolved at startup (``""`` when none). Stamped onto the
        workspace paths so the state readers inject it into the heartbeat
        payload, lighting up the standalone deep-links into persisted
        harmonograf sessions. A live evolve's own heartbeat URL still wins
        (see ``zicato.query.read_heartbeat_dict``).
    """
    paths = _resolve_workspace(workspace_root, harmonograf_url=harmonograf_url)
    static_dir = Path(static_dir)
    started = time.monotonic()
    broker = ChangeBroker(paths)

    handlers = make_endpoints(paths, read_only=read_only, started=started)

    async def events(_request: Request) -> Response:
        from starlette.responses import StreamingResponse

        return StreamingResponse(
            sse_event_stream(broker, paths),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    # Static serving. The JS references assets both at the document root
    # (`style.css`, `app.js`) and under `/static/`, so the bundle is
    # mounted at both. An unknown asset falls through to a 404.
    def _serve_static(name: str, inm: str | None = None) -> Response:
        if not name or name in (".", ".."):
            name = "index.html"
        # The traversal guard is LEXICAL: a request may not escape the bundle
        # root, which is a property of the requested path alone. Where the
        # files themselves live is a deployment property — a bundle staged as
        # symlinks into another tree is legitimate and must still serve — so
        # the candidate is never resolved through its links to be checked.
        if "\x00" in name:
            return PlainTextResponse("not found", status_code=404)
        rel = os.path.normpath(name)
        if os.path.isabs(rel) or rel == ".." or rel.startswith(".." + os.sep):
            return PlainTextResponse("not found", status_code=404)
        candidate = static_dir / rel
        if candidate.is_file():
            import mimetypes
            from email.utils import formatdate

            mime, _ = mimetypes.guess_type(str(candidate))
            # The dashboard is served straight off disk and iterated on live, and
            # the asset URLs carry no version/hash to bust — so a plain cache
            # would serve stale CSS/JS. Instead keep `no-cache` (the browser
            # REVALIDATES on every load, so an edit always reaches it) but attach
            # a validator: an ETag/Last-Modified derived from the file's identity
            # (mtime-ns + size, a cheap stat). When the asset is unchanged the
            # revalidation returns a bodyless 304 — no re-download — and the
            # moment a file is edited its ETag changes and the browser gets a
            # fresh 200. Caching efficiency without the stale-asset bug.
            st = candidate.stat()
            etag = f'"{st.st_mtime_ns:x}-{st.st_size:x}"'
            cache_headers = {
                "Cache-Control": "no-cache",
                "ETag": etag,
                "Last-Modified": formatdate(st.st_mtime, usegmt=True),
            }
            if _if_none_match(inm, etag):
                return Response(status_code=304, headers=cache_headers)
            return Response(
                candidate.read_bytes(),
                media_type=mime or "application/octet-stream",
                headers=cache_headers,
            )
        if name == "index.html":
            return Response(_PLACEHOLDER_HTML, media_type="text/html; charset=utf-8")
        return PlainTextResponse("not found", status_code=404)

    async def serve_root(request: Request) -> Response:
        return _serve_static("index.html", request.headers.get("if-none-match"))

    async def serve_static_path(request: Request) -> Response:
        return _serve_static(request.path_params["path"], request.headers.get("if-none-match"))

    async def serve_fallback(request: Request) -> Response:
        # index.html's relative references resolve at the document root.
        return _serve_static(request.url.path.lstrip("/"), request.headers.get("if-none-match"))

    routes = [
        Route("/", serve_root),
        # The table-driven read routes. Each is one row of
        # ``endpoints.READ_ENDPOINTS`` — path, reader, coordinates, degrade —
        # so a read route is added there rather than here.
        *[Route(entry.path, handlers[entry.path]) for entry in READ_ENDPOINTS],
        # The reads whose query parameters shape the response.
        Route("/api/health", handlers["api_health"]),
        Route("/api/environment", handlers["api_environment"]),
        Route("/api/search", handlers["api_search"]),
        Route("/api/logs", handlers["api_logs"]),
        Route("/api/run-log", handlers["api_run_log"]),
        # The two epoch documents served as themselves rather than as JSON.
        Route(
            "/api/epoch/{epoch_id}/journal.md",
            handlers["api_epoch_journal_md"],
        ),
        Route(
            "/api/epoch/{epoch_id}/analysis.html",
            handlers["api_epoch_analysis_html"],
        ),
        # The file-tree / mutation-site browser.
        Route("/api/files", handlers["api_files"]),
        Route(
            "/api/files/{epoch_id}/{generation_id}/tree",
            handlers["api_files_tree"],
        ),
        Route(
            "/api/files/{epoch_id}/{generation_id}/content",
            handlers["api_files_content"],
        ),
        Route(
            "/api/files/{epoch_id}/{generation_id}/patches",
            handlers["api_files_patches"],
        ),
        Route(
            "/api/files/{epoch_id}/{generation_id}/diff",
            handlers["api_files_diff"],
        ),
        Route("/api/mutations/{epoch_id}", handlers["api_mutations"]),
        Route(
            "/api/mutations/{epoch_id}/{mutation_id}",
            handlers["api_mutation_detail"],
        ),
        # The transcript surface.
        Route("/api/conversation/{run_id}", handlers["api_conversation"]),
        Route(
            "/api/matchup/{entry_id}/conversations",
            handlers["api_matchup_conversations"],
        ),
        Route(
            "/api/run/{epoch_id}/{generation_id}/{entry_id}/transcript",
            handlers["api_run_transcript"],
        ),
        # The live conversation pane's cursor-append read (issue #194 §2) —
        # a SEPARATE route so the full-transcript payload above keeps its
        # shape for the side-by-side panes that already read it.
        Route(
            "/api/run/{epoch_id}/{generation_id}/{entry_id}/transcript/delta",
            handlers["api_run_transcript_delta"],
        ),
        Route("/events", events),
        Route("/api/control/pause", handlers["control_pause"], methods=["POST"]),
        Route("/api/control/resume", handlers["control_resume"], methods=["POST"]),
        Route(
            "/api/control/skip-round",
            handlers["control_skip_round"],
            methods=["POST"],
        ),
        Route(
            "/api/control/kill/{run_id}",
            handlers["control_kill"],
            methods=["POST"],
        ),
        Route(
            "/api/control/promote/{generation_id}",
            handlers["control_promote"],
            methods=["POST"],
        ),
        Route(
            "/api/control/reject/{generation_id}",
            handlers["control_reject"],
            methods=["POST"],
        ),
        Route("/api/control/brief", handlers["control_brief"], methods=["POST"]),
        Route("/static/{path:path}", serve_static_path),
    ]

    # Tournament-builder REST surface (B1a). The form (B2) and the copilot
    # (B1b) both drive these handlers; they share the same draft store and
    # the same builder operations, so there is one source of truth for an
    # edit. The POST ops respect the dashboard's read_only flag. Spliced in
    # before the catch-all asset fallback so /builder/* never falls through
    # to the static server.
    from zicato.builder.api import builder_routes  # noqa: PLC0415

    routes.extend(builder_routes(paths.root, read_only=read_only))

    # Unified models / LLM-endpoints settings surface. A model/endpoint is
    # runtime infra (NOT the evaluation contract), so a write here never rolls
    # the epoch. GET returns the secret-safe view (api_key_env NAME + a
    # set/unset flag); POST persists only the ``models`` block of config.json.
    from zicato.dashboard.settings_api import settings_routes  # noqa: PLC0415

    routes.extend(settings_routes(paths.root, read_only=read_only))

    # Any unmatched GET is treated as a request for a bundled asset so
    # index.html's root-relative references resolve. MUST stay last.
    routes.append(Route("/{path:path}", serve_fallback))

    @contextlib.asynccontextmanager
    async def _lifespan(_app: Starlette) -> AsyncIterator[None]:
        await broker.start()
        try:
            yield
        finally:
            await broker.stop()

    app = Starlette(routes=routes, lifespan=_lifespan)
    app.state.bound_port = 0
    app.state.broker = broker
    app.state.workspace = paths
    return app


def _pick_port(host: str, preferred_port: int, max_retries: int = 10) -> int:
    """Return the first free port in ``preferred..preferred+max_retries``.

    A port already in use is skipped and the next is tried. The probe
    socket does NOT set ``SO_REUSEADDR`` so a
    genuinely-bound port is detected as occupied rather than silently
    re-bound.
    """
    last_err: OSError | None = None
    for offset in range(max_retries + 1):
        port = preferred_port + offset
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind((host, port))
            return port
        except OSError as exc:
            last_err = exc
        finally:
            sock.close()
    raise last_err or OSError("no port available")


def _publish_endpoint(workspace_root: Path, host: str, bound_port: int) -> None:
    """Record the host/port the dashboard actually bound to.

    Written to ``runtime/dashboard.json`` so ``zicato evolve`` — which
    spawns this service as a subprocess and cannot otherwise know which
    port the ``+1`` walk settled on — can read the real URL back rather
    than assuming it equals the preferred port. Best-effort: a failure
    to write must not stop the dashboard from serving, so the operator
    still gets a working UI even if the convenience file is missing.
    """
    try:
        from zicato.runtime.paths import (  # noqa: PLC0415
            dashboard_endpoint_path,
            ensure_runtime_dirs,
        )

        root = Path(workspace_root)
        # ``run`` is called with the .zicato directory itself; accept a
        # project root containing one too, matching create_app's lenient
        # resolution.
        if root.name != ".zicato" and (root / ".zicato").is_dir():
            root = root / ".zicato"
        ensure_runtime_dirs(root)
        dashboard_endpoint_path(root).write_text(
            json.dumps({"host": host, "port": bound_port}) + "\n",
            encoding="utf-8",
        )
    except Exception:  # noqa: BLE001 — the endpoint file is a convenience
        return


def _ensure_workspace_harmonograf(workspace_root: Path) -> Any:
    """Reuse-or-launch the persistent per-workspace harmonograf server.

    Returns a ``WorkspaceHarmonografHandle`` (``web_url``/``grpc_target``/
    ``launched``). Fully failure-isolated: any problem — a missing
    harmonograf-server dep, a port-bind error, an unreachable launch —
    yields a no-op handle (``web_url=""``) so the dashboard still serves.

    sqlite double-open: the helper consults ``.harmonograf/server.json``
    first, so a concurrent evolve (which routes through the same helper)
    and this dashboard share ONE server bound to the workspace db — never
    two servers fighting over the same sqlite file.
    """
    try:
        from zicato.telemetry.harmonograf_supervisor import (  # noqa: PLC0415
            ensure_workspace_harmonograf,
        )

        return ensure_workspace_harmonograf(Path(workspace_root))
    except Exception as exc:  # noqa: BLE001 — never block the dashboard
        # Logging is NOT configured at this point in ``run()`` (uvicorn
        # configures it later), so a logger call here would vanish. Carry
        # the reason on the no-op handle instead so the caller can surface
        # it to stdout alongside the startup banner.
        class _NoopHandle:
            web_url = ""
            grpc_target = ""
            launched = False
            reason = f"resolution raised: {exc}"

            def shutdown(self) -> None:
                return None

        return _NoopHandle()


def _echo_harmonograf_status(hg: Any) -> None:
    """Print a visible one-line harmonograf status to stdout.

    Emitted in the SAME stream/style as the ``Dashboard:`` banner so an
    operator always knows whether execution deep-links are available —
    and, on the no-op path, WHY they are not. Logging is not configured
    this early in :func:`run` (uvicorn configures it later), so a logger
    call would silently vanish; this is a deliberate ``click.echo`` to
    stdout. Fully isolated: printing the status must never raise.
    """
    import click  # noqa: PLC0415

    try:
        web_url = getattr(hg, "web_url", "") or ""
        if web_url:
            state = "launched" if getattr(hg, "launched", False) else "reused"
            click.echo(f"harmonograf: {web_url} ({state})")
        else:
            reason = getattr(hg, "reason", "") or "no harmonograf available"
            click.echo(
                "harmonograf: unavailable — continuing without execution " f"deep-links ({reason})"
            )
    except Exception:  # noqa: BLE001 — the status line must never block startup
        return


def run(
    workspace_root: Path,
    host: str,
    port: int,
    static_dir: Path,
) -> None:
    """Serve the dashboard via uvicorn.

    Binds the given ``port``, walking ``+1`` up to ten times if it is
    already in use. The control POST endpoints are enabled here via
    ``create_app(..., read_only=False)``.

    The host/port actually bound is recorded to ``runtime/dashboard.json``
    (see :func:`_publish_endpoint`) so a parent ``zicato evolve`` can
    report the dashboard's real URL.

    A persistent per-workspace harmonograf server is reused-or-launched at
    startup (:func:`_ensure_workspace_harmonograf`) so a standalone /
    post-mortem dashboard can deep-link into the persisted harmonograf
    sessions. Lifecycle ownership: the dashboard shuts the server down
    ONLY when it LAUNCHED it (``handle.launched``); a server it merely
    reused (a live evolve's, or another dashboard process's) is left
    running.
    """
    import uvicorn

    # Self-healing index, read-path half (ANALYTICAL-INDEX.md §5.3): build an
    # absent / wrong-schema index ONCE per PROCESS, never per request, and
    # never heal. Seated in ``run`` rather than ``create_app`` because this is
    # the process-start seam — both real dashboard launches (the ``zicato
    # dashboard`` command and evolve's ``python -m zicato.dashboard`` spawn)
    # come through here, and building an ASGI app must not have filesystem
    # side effects.
    _ensure_index_at_startup(_resolve_workspace(workspace_root))

    hg = _ensure_workspace_harmonograf(workspace_root)
    _echo_harmonograf_status(hg)

    bound_port = _pick_port(host, port)

    # Print the DEFINITIVE dashboard URL now that the real bound port is
    # known — ``_pick_port`` may have walked +1 off the requested port (a
    # TIME_WAIT bounce), so the requested port can be wrong. The command
    # modules do NOT pre-print this URL.
    import click  # noqa: PLC0415

    click.echo(f"Dashboard: http://{host}:{bound_port}")

    app = create_app(
        workspace_root,
        static_dir,
        read_only=False,
        harmonograf_url=getattr(hg, "web_url", "") or "",
    )
    app.state.bound_port = bound_port

    _publish_endpoint(workspace_root, host, bound_port)

    try:
        uvicorn.run(app, host=host, port=bound_port, log_level="info")
    finally:
        # Own the lifecycle only when we launched the server; a reused one
        # (evolve's, or another dashboard's) is left running.
        try:
            hg.shutdown()
        except Exception:  # noqa: BLE001 — teardown is best-effort
            pass
