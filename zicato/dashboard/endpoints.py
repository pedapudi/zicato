"""HTTP route handlers for the dashboard service.

Each handler reads the live ``.zicato/`` workspace through
:mod:`zicato.dashboard.state_reader` and returns a JSON shape that is
byte-compatible with what the retired Rust supervisor served, so the
existing vanilla-JS dashboard works unchanged.

GET routes are always available. The POST control routes write a marker
file into ``.zicato/runtime/control/`` (the file-based control-channel
protocol the orchestrator consumes) and return ``403`` when the server
was created with ``read_only=True``.

The two conversation endpoints reconstruct goldfive event streams into
side-by-side transcripts via :mod:`zicato.dashboard.transcript`. That
module is built by a parallel agent; its import is guarded so this
server still starts (and every other endpoint still works) when it is
not yet present in the worktree.
"""

from __future__ import annotations

import datetime as _dt
import json
import re
from pathlib import Path
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response

from zicato.dashboard import state_reader
from zicato.dashboard.state_reader import WorkspacePaths

# The transcript reconstructor is owned by a parallel agent. Guard the
# import so the whole server still runs (and its tests still pass) if the
# module is not importable yet in this worktree.
_HAVE_TRANSCRIPT = False
reconstruct_transcript: Any = None
try:  # pragma: no cover - import availability varies across worktrees
    from zicato.dashboard.transcript import (
        reconstruct_transcript as _reconstruct_transcript,
    )

    reconstruct_transcript = _reconstruct_transcript
    _HAVE_TRANSCRIPT = True
except Exception:  # pragma: no cover
    pass


# Conservative id validator: rejects path-traversal, separators, spaces.
# Mirrors the Rust ``routes::is_safe_id``.
_SAFE_ID = re.compile(r"^[A-Za-z0-9._-]{1,200}$")


def _is_safe_id(value: str) -> bool:
    return bool(value) and value not in (".", "..") and _SAFE_ID.match(value) is not None


def _now_iso() -> str:
    return _dt.datetime.now(_dt.UTC).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# GET endpoints
# ---------------------------------------------------------------------------


def make_endpoints(paths: WorkspacePaths, *, read_only: bool, started: float) -> dict[str, Any]:
    """Build every route handler bound to one workspace.

    Returns a dict of ``name -> handler`` the app wires onto routes.
    ``started`` is a ``time.monotonic()`` reference for the health uptime.
    """
    import time

    async def api_health(_request: Request) -> JSONResponse:
        return JSONResponse(
            {
                "status": "ok",
                "version": _dashboard_version(),
                "uptime_seconds": int(time.monotonic() - started),
                "read_only": read_only,
                "workspace": str(paths.root),
                "port": _request.app.state.bound_port,
                "build": _dashboard_version(),
            }
        )

    async def api_state(_request: Request) -> JSONResponse:
        return JSONResponse(state_reader.build_snapshot(paths))

    async def api_epoch(_request: Request) -> JSONResponse:
        return JSONResponse(state_reader.build_epoch_view(paths))

    async def api_lineage(_request: Request) -> JSONResponse:
        return JSONResponse(state_reader.build_lineage_view(paths))

    async def api_run_log(request: Request) -> JSONResponse:
        raw = request.query_params.get("limit")
        requested: int | None = None
        if raw is not None:
            try:
                requested = int(raw.strip())
            except ValueError:
                requested = None
        limit = state_reader.clamp_run_log_limit(requested)
        return JSONResponse(state_reader.build_run_log(paths, limit))

    async def api_active_runs(_request: Request) -> JSONResponse:
        return JSONResponse(state_reader.read_active_runs_view(paths))

    async def api_active_tournament(_request: Request) -> JSONResponse:
        return JSONResponse(state_reader.read_active_tournament_dict(paths))

    async def api_heartbeat(_request: Request) -> JSONResponse:
        return JSONResponse(state_reader.read_heartbeat_dict(paths))

    async def api_tournaments(_request: Request) -> JSONResponse:
        return JSONResponse(state_reader.build_bracket(paths))

    async def api_tournament_detail(request: Request) -> JSONResponse:
        generation_id = request.path_params["generation_id"]
        if not _is_safe_id(generation_id):
            # A malformed id degrades to "no such matchup".
            return JSONResponse(
                {
                    "epoch_id": state_reader.read_current_epoch(paths),
                    "generation_id": generation_id,
                    "patches": [],
                    "ab_grid": [],
                }
            )
        return JSONResponse(state_reader.build_matchup_detail(paths, generation_id))

    async def api_health_report(_request: Request) -> JSONResponse:
        return JSONResponse(state_reader.build_health_report(paths))

    # -- conversation endpoints --------------------------------------

    async def api_conversation(request: Request) -> Response:
        run_id = request.path_params["run_id"]
        if not _is_safe_id(run_id):
            return JSONResponse({"error": "invalid run_id"}, status_code=400)
        if not _HAVE_TRANSCRIPT:
            return JSONResponse(
                {"error": "transcript reconstruction unavailable"},
                status_code=503,
            )
        events_path = state_reader.find_run_events_path(paths, run_id)
        if events_path is None:
            return JSONResponse({"error": f"no events for run {run_id}"}, status_code=404)
        try:
            transcript = reconstruct_transcript(events_path, partial_ok=True)
            return JSONResponse(transcript.to_dict())
        except Exception as exc:  # best-effort: never 500 the dashboard
            return JSONResponse(
                {"error": f"transcript failed: {exc}", "run_id": run_id},
                status_code=200,
            )

    async def api_matchup_conversations(request: Request) -> Response:
        entry_id = request.path_params["entry_id"]
        if not _is_safe_id(entry_id):
            return JSONResponse({"error": "invalid entry_id"}, status_code=400)
        if not _HAVE_TRANSCRIPT:
            return JSONResponse(
                {"error": "transcript reconstruction unavailable"},
                status_code=503,
            )
        result = _build_matchup_conversations(paths, entry_id)
        return JSONResponse(result)

    # -- control endpoints (POST) ------------------------------------

    def _forbidden_if_read_only() -> JSONResponse | None:
        if read_only:
            return JSONResponse({"error": "dashboard is read-only"}, status_code=403)
        return None

    async def _read_reason(request: Request) -> str:
        try:
            body = await request.body()
        except Exception:
            return ""
        if not body:
            return ""
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            return ""
        if isinstance(parsed, dict):
            reason = parsed.get("reason")
            return reason if isinstance(reason, str) else ""
        return ""

    def _control_path(*parts: str) -> Path:
        return paths.control_dir.joinpath(*parts)

    def _atomic_write(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(path)

    async def control_pause(request: Request) -> Response:
        forbidden = _forbidden_if_read_only()
        if forbidden is not None:
            return forbidden
        reason = await _read_reason(request)
        path = _control_path("pause_epoch")
        payload = {"reason": reason, "ts": _now_iso()}
        _atomic_write(path, json.dumps(payload).encode())
        return JSONResponse({"accepted": True, "path": str(path)}, status_code=202)

    async def control_skip_round(request: Request) -> Response:
        forbidden = _forbidden_if_read_only()
        if forbidden is not None:
            return forbidden
        reason = await _read_reason(request)
        path = _control_path("skip_round")
        payload = {"reason": reason, "ts": _now_iso()}
        _atomic_write(path, json.dumps(payload).encode())
        return JSONResponse({"accepted": True, "path": str(path)}, status_code=202)

    async def control_kill(request: Request) -> Response:
        forbidden = _forbidden_if_read_only()
        if forbidden is not None:
            return forbidden
        run_id = request.path_params["run_id"]
        if not _is_safe_id(run_id):
            return PlainTextResponse("invalid run_id", status_code=400)
        path = _control_path("kill_runs", run_id)
        payload = {"run_id": run_id, "ts": _now_iso()}
        _atomic_write(path, json.dumps(payload).encode())
        return JSONResponse(payload, status_code=202)

    async def control_promote(request: Request) -> Response:
        forbidden = _forbidden_if_read_only()
        if forbidden is not None:
            return forbidden
        generation_id = request.path_params["generation_id"]
        if not _is_safe_id(generation_id):
            return PlainTextResponse("invalid generation_id", status_code=400)
        path = _control_path("promote", generation_id)
        payload = {"generation_id": generation_id, "ts": _now_iso()}
        _atomic_write(path, json.dumps(payload).encode())
        return JSONResponse(payload, status_code=202)

    async def control_reject(request: Request) -> Response:
        forbidden = _forbidden_if_read_only()
        if forbidden is not None:
            return forbidden
        generation_id = request.path_params["generation_id"]
        if not _is_safe_id(generation_id):
            return PlainTextResponse("invalid generation_id", status_code=400)
        path = _control_path("reject", generation_id)
        payload = {"generation_id": generation_id, "ts": _now_iso()}
        _atomic_write(path, json.dumps(payload).encode())
        return JSONResponse(payload, status_code=202)

    async def control_brief(request: Request) -> Response:
        forbidden = _forbidden_if_read_only()
        if forbidden is not None:
            return forbidden
        body = await request.body()
        # The on-disk control file keeps its protocol name
        # (``rubric_replacement.txt``); it is part of the runtime
        # control contract the orchestrator consumes, not a UI label.
        path = _control_path("rubric_replacement.txt")
        _atomic_write(path, body)
        return JSONResponse(
            {"accepted": True, "bytes": len(body), "path": str(path)},
            status_code=202,
        )

    return {
        "api_health": api_health,
        "api_state": api_state,
        "api_epoch": api_epoch,
        "api_lineage": api_lineage,
        "api_run_log": api_run_log,
        "api_active_runs": api_active_runs,
        "api_active_tournament": api_active_tournament,
        "api_heartbeat": api_heartbeat,
        "api_tournaments": api_tournaments,
        "api_tournament_detail": api_tournament_detail,
        "api_health_report": api_health_report,
        "api_conversation": api_conversation,
        "api_matchup_conversations": api_matchup_conversations,
        "control_pause": control_pause,
        "control_skip_round": control_skip_round,
        "control_kill": control_kill,
        "control_promote": control_promote,
        "control_reject": control_reject,
        "control_brief": control_brief,
    }


def _dashboard_version() -> str:
    """A non-empty build identifier for the dashboard footer."""
    try:
        from importlib.metadata import version

        return version("zicato")
    except Exception:
        return "zicato-dashboard"


def _build_matchup_conversations(paths: WorkspacePaths, entry_id: str) -> dict[str, Any]:
    """Locate and reconstruct the champion + challenger conversations.

    For a board entry, the active tournament names a champion-side
    (``parent``) generation and a challenger-side (``child``) generation;
    each ran the entry once. This finds both runs' ``events.jsonl`` files
    and reconstructs both transcripts so the UI can render them side by
    side.
    """
    result: dict[str, Any] = {"champion": None, "challenger": None}
    tournament = state_reader.read_active_tournament_dict(paths)
    if not isinstance(tournament, dict):
        return result

    parent_gen = tournament.get("parent_generation_id")
    child_gen = tournament.get("child_generation_id")

    def _side(generation_id: Any) -> dict[str, Any] | None:
        if not isinstance(generation_id, str) or not generation_id:
            return None
        located = state_reader.find_generation_run(paths, generation_id, entry_id)
        if located is None:
            return {
                "run_id": None,
                "generation_id": generation_id,
                "transcript": None,
            }
        run_id, events_path = located
        transcript: Any = None
        if _HAVE_TRANSCRIPT and reconstruct_transcript is not None:
            try:
                transcript = reconstruct_transcript(events_path, partial_ok=True).to_dict()
            except Exception as exc:
                transcript = {"error": f"transcript failed: {exc}"}
        return {
            "run_id": run_id,
            "generation_id": generation_id,
            "transcript": transcript,
        }

    result["champion"] = _side(parent_gen)
    result["challenger"] = _side(child_gen)
    return result
