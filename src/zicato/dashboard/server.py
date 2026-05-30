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
import socket
import time
from collections.abc import AsyncIterator
from pathlib import Path

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Route

from zicato.dashboard.endpoints import make_endpoints
from zicato.dashboard.sse import ChangeBroker, sse_event_stream
from zicato.dashboard.state_reader import WorkspacePaths

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


def _resolve_workspace(workspace_root: Path) -> WorkspacePaths:
    """Normalize a workspace argument to a :class:`WorkspacePaths`.

    Accepts either the ``.zicato`` directory itself or a project root
    that contains one — so callers can pass whichever they have.
    """
    root = Path(workspace_root)
    if root.name != ".zicato" and (root / ".zicato").is_dir():
        root = root / ".zicato"
    return WorkspacePaths(root)


def create_app(
    workspace_root: Path,
    static_dir: Path,
    *,
    read_only: bool = True,
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
    """
    paths = _resolve_workspace(workspace_root)
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
    def _serve_static(name: str) -> Response:
        if not name or name in (".", ".."):
            name = "index.html"
        # Reject path traversal.
        candidate = (static_dir / name).resolve()
        try:
            candidate.relative_to(static_dir.resolve())
        except ValueError:
            return PlainTextResponse("not found", status_code=404)
        if candidate.is_file():
            import mimetypes

            mime, _ = mimetypes.guess_type(str(candidate))
            return Response(
                candidate.read_bytes(),
                media_type=mime or "application/octet-stream",
            )
        if name == "index.html":
            return Response(_PLACEHOLDER_HTML, media_type="text/html; charset=utf-8")
        return PlainTextResponse("not found", status_code=404)

    async def serve_root(_request: Request) -> Response:
        return _serve_static("index.html")

    async def serve_static_path(request: Request) -> Response:
        return _serve_static(request.path_params["path"])

    async def serve_fallback(request: Request) -> Response:
        # index.html's relative references resolve at the document root.
        return _serve_static(request.url.path.lstrip("/"))

    routes = [
        Route("/", serve_root),
        Route("/api/health", handlers["api_health"]),
        Route("/api/state", handlers["api_state"]),
        Route("/api/environment", handlers["api_environment"]),
        Route("/api/epoch", handlers["api_epoch"]),
        Route("/api/lineage", handlers["api_lineage"]),
        Route("/api/workspace", handlers["api_workspace"]),
        Route(
            "/api/contract-diff/{epoch_id}",
            handlers["api_contract_diff"],
        ),
        Route(
            "/api/epoch/{epoch_id}/per-judge-trend",
            handlers["api_per_judge_trend"],
        ),
        Route(
            "/api/generation/{epoch_id}/{generation_id}/per-judge",
            handlers["api_per_judge_for_generation"],
        ),
        Route(
            "/api/generation/{epoch_id}/{generation_id}/per-entry",
            handlers["api_per_entry_for_generation"],
        ),
        Route(
            "/api/round/{epoch_id}/{champion_id}/{challenger_id}/per-judge-comparison",
            handlers["api_per_judge_comparison"],
        ),
        Route(
            "/api/run/{run_id}/per-judge",
            handlers["api_per_judge_for_run"],
        ),
        Route(
            "/api/run/{epoch_id}/{generation_id}/{entry_id}/per-judge",
            handlers["api_per_judge_for_run_by_entry"],
        ),
        Route(
            "/api/run/{epoch_id}/{generation_id}/{entry_id}/expectations",
            handlers["api_run_expectations"],
        ),
        Route(
            "/api/run/{epoch_id}/{generation_id}/{entry_id}/header",
            handlers["api_run_header"],
        ),
        Route("/api/run-log", handlers["api_run_log"]),
        Route("/api/active-runs", handlers["api_active_runs"]),
        Route("/api/active-tournament", handlers["api_active_tournament"]),
        Route("/api/heartbeat", handlers["api_heartbeat"]),
        Route("/api/tournaments", handlers["api_tournaments"]),
        Route(
            "/api/tournaments/{generation_id}",
            handlers["api_tournament_detail"],
        ),
        Route(
            "/api/matchup-grid/{epoch_id}/{champion_id}/{challenger_id}",
            handlers["api_matchup_grid"],
        ),
        Route(
            "/api/round/{epoch_id}/{champion_id}/{challenger_id}/gate",
            handlers["api_gate"],
        ),
        Route("/api/health-report", handlers["api_health_report"]),
        Route("/api/search", handlers["api_search"]),
        Route("/api/score-trajectory", handlers["api_score_trajectory"]),
        Route(
            "/api/drift-movements/{generation_id}",
            handlers["api_drift_movements"],
        ),
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
        Route("/api/epoch/{epoch_id}/journal", handlers["api_epoch_journal"]),
        Route(
            "/api/epoch/{epoch_id}/journal.md",
            handlers["api_epoch_journal_md"],
        ),
        Route("/api/epoch/{epoch_id}/analysis", handlers["api_epoch_analysis"]),
        Route(
            "/api/epoch/{epoch_id}/analysis.html",
            handlers["api_epoch_analysis_html"],
        ),
        Route("/api/conversation/{run_id}", handlers["api_conversation"]),
        Route(
            "/api/matchup/{entry_id}/conversations",
            handlers["api_matchup_conversations"],
        ),
        Route(
            "/api/run/{epoch_id}/{generation_id}/{entry_id}/transcript",
            handlers["api_run_transcript"],
        ),
        Route("/events", events),
        Route("/api/control/pause", handlers["control_pause"], methods=["POST"]),
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
        # Any unmatched GET is treated as a request for a bundled asset
        # so index.html's root-relative references resolve.
        Route("/{path:path}", serve_fallback),
    ]

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
    socket deliberately does NOT set ``SO_REUSEADDR`` so a
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
    """
    import uvicorn

    bound_port = _pick_port(host, port)
    app = create_app(workspace_root, static_dir, read_only=False)
    app.state.bound_port = bound_port

    _publish_endpoint(workspace_root, host, bound_port)

    uvicorn.run(app, host=host, port=bound_port, log_level="info")
