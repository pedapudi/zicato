"""HTTP route handlers for the dashboard service.

Each handler reads the live ``.zicato/`` workspace through
:mod:`zicato.dashboard.state_reader` and returns a JSON shape the
dashboard front-end consumes. ``/api/environment`` is the consolidated
read of the whole environment; the granular per-section endpoints are
kept alongside it.

GET routes are always available. The POST control routes write a marker
file into ``.zicato/runtime/control/`` (the file-based control-channel
protocol the orchestrator consumes) and return ``403`` when the server
was created with ``read_only=True``.

The conversation endpoints reconstruct goldfive event streams into
transcripts via :mod:`zicato.dashboard.transcript`; its import is
guarded so the server still starts (and every other endpoint still
works) when that module is not present.
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

# Guard the transcript-reconstructor import so the whole server still
# runs (and its tests still pass) even if that module is unavailable.
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


def _int_query(request: Request, name: str) -> int | None:
    """Parse an integer query parameter, or ``None`` if absent/invalid."""
    raw = request.query_params.get(name)
    if raw is None:
        return None
    try:
        return int(raw.strip())
    except ValueError:
        return None


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

    async def api_environment(request: Request) -> JSONResponse:
        # One coalesced read of the whole environment — the front-end
        # refreshes the entire view from this single endpoint instead of
        # fanning out to six. ``?run-log-limit=`` is clamped like the
        # dedicated run-log endpoint.
        limit = state_reader.clamp_run_log_limit(_int_query(request, "run-log-limit"))
        return JSONResponse(state_reader.build_environment(paths, run_log_limit=limit))

    async def api_epoch(_request: Request) -> JSONResponse:
        return JSONResponse(state_reader.build_epoch_view(paths))

    async def api_lineage(_request: Request) -> JSONResponse:
        return JSONResponse(state_reader.build_lineage_view(paths))

    async def api_run_log(request: Request) -> JSONResponse:
        limit = state_reader.clamp_run_log_limit(_int_query(request, "limit"))
        # ``?after=<cursor>`` requests only events past a cursor so the
        # dashboard appends to its log tail instead of re-rendering it.
        after = _int_query(request, "after")
        return JSONResponse(state_reader.build_run_log(paths, limit, after=after))

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

    async def api_matchup_grid(request: Request) -> JSONResponse:
        # Per-entry A/B grid read straight off the persisted per-run
        # loss.json files (and the gen_score.json aggregates) — the read
        # path a *completed* tournament's matchup-detail panel uses when
        # the SQLite index was never built. Given an epoch + champion gen
        # + challenger gen, returns the champion-vs-challenger comparison.
        epoch_id = request.path_params["epoch_id"]
        champion_id = request.path_params["champion_id"]
        challenger_id = request.path_params["challenger_id"]
        if (
            not _is_safe_id(epoch_id)
            or not _is_safe_id(champion_id)
            or not _is_safe_id(challenger_id)
        ):
            # A malformed coordinate degrades to "no grid" rather than 500.
            return JSONResponse(
                {
                    "epoch_id": epoch_id,
                    "champion": champion_id,
                    "challenger": challenger_id,
                    "entry_grid": [],
                    "scalar": None,
                    "source": "loss_files",
                }
            )
        return JSONResponse(
            state_reader.build_matchup_grid(paths, epoch_id, champion_id, challenger_id)
        )

    async def api_health_report(_request: Request) -> JSONResponse:
        return JSONResponse(state_reader.build_health_report(paths))

    async def api_score_trajectory(_request: Request) -> JSONResponse:
        # The environment-wide evolution curve — scalar per generation.
        return JSONResponse(state_reader.build_score_trajectory(paths))

    async def api_drift_movements(request: Request) -> JSONResponse:
        generation_id = request.path_params["generation_id"]
        if not _is_safe_id(generation_id):
            return JSONResponse(
                {
                    "epoch_id": state_reader.read_current_epoch(paths),
                    "generation_id": generation_id,
                    "champion": None,
                    "challenger": generation_id,
                    "movements": [],
                }
            )
        return JSONResponse(state_reader.build_drift_movements(paths, generation_id))

    # -- file-tree / file-browser endpoints --------------------------

    async def api_files(_request: Request) -> JSONResponse:
        from zicato.dashboard import filetree

        return JSONResponse(filetree.build_file_index(paths))

    async def api_files_tree(request: Request) -> JSONResponse:
        from zicato.dashboard import filetree

        epoch_id = request.path_params["epoch_id"]
        generation_id = request.path_params["generation_id"]
        if not _is_safe_id(epoch_id) or not _is_safe_id(generation_id):
            return JSONResponse(
                {"error": "invalid epoch or generation id", "entries": []},
                status_code=400,
            )
        return JSONResponse(filetree.build_generation_tree(paths, epoch_id, generation_id))

    async def api_files_content(request: Request) -> JSONResponse:
        from zicato.dashboard import filetree

        epoch_id = request.path_params["epoch_id"]
        generation_id = request.path_params["generation_id"]
        if not _is_safe_id(epoch_id) or not _is_safe_id(generation_id):
            return JSONResponse({"error": "invalid epoch or generation id"}, status_code=400)
        rel_path = request.query_params.get("path", "")
        if not rel_path:
            return JSONResponse({"error": "missing 'path' query param"}, status_code=400)
        # The store layer rejects traversal; a 200 with an ``error``
        # field keeps the dashboard from surfacing a hard failure.
        return JSONResponse(filetree.read_generation_file(paths, epoch_id, generation_id, rel_path))

    async def api_files_patches(request: Request) -> JSONResponse:
        from zicato.dashboard import filetree

        epoch_id = request.path_params["epoch_id"]
        generation_id = request.path_params["generation_id"]
        if not _is_safe_id(epoch_id) or not _is_safe_id(generation_id):
            return JSONResponse(
                {"error": "invalid epoch or generation id", "patches": []},
                status_code=400,
            )
        return JSONResponse(filetree.build_generation_patches(paths, epoch_id, generation_id))

    async def api_files_diff(request: Request) -> JSONResponse:
        from zicato.dashboard import filetree

        epoch_id = request.path_params["epoch_id"]
        generation_id = request.path_params["generation_id"]
        if not _is_safe_id(epoch_id) or not _is_safe_id(generation_id):
            return JSONResponse(
                {"error": "invalid epoch or generation id", "files": []},
                status_code=400,
            )
        return JSONResponse(filetree.build_generation_diff(paths, epoch_id, generation_id))

    # -- mutation-site browser endpoints -----------------------------

    async def api_mutations(request: Request) -> JSONResponse:
        from zicato.dashboard import mutations

        epoch_id = request.path_params["epoch_id"]
        if not _is_safe_id(epoch_id):
            return JSONResponse(
                {"error": "invalid epoch id", "mutations": []},
                status_code=400,
            )
        return JSONResponse(mutations.build_mutation_index(paths, epoch_id))

    async def api_mutation_detail(request: Request) -> JSONResponse:
        from zicato.dashboard import mutations

        epoch_id = request.path_params["epoch_id"]
        mutation_id = request.path_params["mutation_id"]
        if not _is_safe_id(epoch_id) or not _is_safe_id(mutation_id):
            return JSONResponse({"error": "invalid epoch or mutation id"}, status_code=400)
        return JSONResponse(mutations.build_mutation_detail(paths, epoch_id, mutation_id))

    # -- epoch drill-down endpoints (journal / analysis) ---------

    async def api_epoch_journal(request: Request) -> JSONResponse:
        """Return the journal.md text for one epoch as ``{ epoch_id, journal }``."""
        epoch_id = request.path_params["epoch_id"]
        if not _is_safe_id(epoch_id):
            return JSONResponse({"error": "invalid epoch id"}, status_code=400)
        path = paths.epochs / epoch_id / "journal.md"
        try:
            text = path.read_text(encoding="utf-8")
        except (FileNotFoundError, OSError):
            text = ""
        return JSONResponse({"epoch_id": epoch_id, "journal": text})

    async def api_epoch_journal_md(request: Request) -> Response:
        """Serve the raw ``journal.md`` markdown for one epoch.

        The Epoch view's "View raw journal" link points at this endpoint so
        a fresh tab renders the human-readable markdown directly — not the
        JSON envelope ``api_epoch_journal`` wraps it in (which is hard to
        skim). Served as ``text/markdown`` with UTF-8 charset; browsers
        that do not have a registered markdown handler treat it as
        ``text/plain`` and render the prose unchanged. Returns 404 when
        the file is absent so the link's failure mode is unambiguous.
        """
        epoch_id = request.path_params["epoch_id"]
        if not _is_safe_id(epoch_id):
            return PlainTextResponse("invalid epoch id", status_code=400)
        path = paths.epochs / epoch_id / "journal.md"
        try:
            text = path.read_text(encoding="utf-8")
        except (FileNotFoundError, OSError):
            return PlainTextResponse(
                f"journal.md not found for epoch {epoch_id}",
                status_code=404,
            )
        return Response(
            content=text,
            media_type="text/markdown; charset=utf-8",
        )

    async def api_epoch_analysis(request: Request) -> JSONResponse:
        """Return the analysis report for one epoch.

        Returns ``{ epoch_id, analysis_md, analysis_html_available }`` so
        the frontend can render the markdown inline and link the HTML file
        via ``/api/epoch/{id}/analysis.html``.
        """
        epoch_id = request.path_params["epoch_id"]
        if not _is_safe_id(epoch_id):
            return JSONResponse({"error": "invalid epoch id"}, status_code=400)
        analysis_md_path = paths.epochs / epoch_id / "analysis.md"
        analysis_html_path = paths.epochs / epoch_id / "analysis.html"
        try:
            analysis_md = analysis_md_path.read_text(encoding="utf-8")
        except (FileNotFoundError, OSError):
            analysis_md = ""
        return JSONResponse(
            {
                "epoch_id": epoch_id,
                "analysis_md": analysis_md,
                "analysis_html_available": analysis_html_path.is_file(),
            }
        )

    async def api_epoch_analysis_html(request: Request) -> Response:
        """Serve the raw ``analysis.html`` for an epoch.

        Returns the self-contained HTML document so the frontend can
        open it in a new tab or embed it. Returns 404 when absent.
        """
        from starlette.responses import HTMLResponse

        epoch_id = request.path_params["epoch_id"]
        if not _is_safe_id(epoch_id):
            return PlainTextResponse("invalid epoch id", status_code=400)
        html = state_reader.read_epoch_analysis_html(paths, epoch_id)
        if html is None:
            return PlainTextResponse("analysis.html not found for this epoch", status_code=404)
        return HTMLResponse(html)

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
        "api_environment": api_environment,
        "api_epoch": api_epoch,
        "api_lineage": api_lineage,
        "api_run_log": api_run_log,
        "api_active_runs": api_active_runs,
        "api_active_tournament": api_active_tournament,
        "api_heartbeat": api_heartbeat,
        "api_tournaments": api_tournaments,
        "api_tournament_detail": api_tournament_detail,
        "api_matchup_grid": api_matchup_grid,
        "api_health_report": api_health_report,
        "api_score_trajectory": api_score_trajectory,
        "api_drift_movements": api_drift_movements,
        "api_files": api_files,
        "api_files_tree": api_files_tree,
        "api_files_content": api_files_content,
        "api_files_patches": api_files_patches,
        "api_files_diff": api_files_diff,
        "api_mutations": api_mutations,
        "api_mutation_detail": api_mutation_detail,
        "api_epoch_journal": api_epoch_journal,
        "api_epoch_journal_md": api_epoch_journal_md,
        "api_epoch_analysis": api_epoch_analysis,
        "api_epoch_analysis_html": api_epoch_analysis_html,
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
