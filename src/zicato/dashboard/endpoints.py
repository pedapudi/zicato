"""HTTP route handlers for the dashboard service.

Each handler reads the live ``.zicato/`` workspace through
:mod:`zicato.query` and returns a JSON shape the
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

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response

from zicato import query
from zicato.query import WorkspacePaths

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


# A tournament id is the ingester's stable form ``{epoch}:{parent}->{child}``
# (ingest.py ``_upsert_tournament``), so it carries ``:`` and ``->`` that the
# strict ``_SAFE_ID`` rejects. This validator widens the alphabet to admit
# those two separators while still blocking path-traversal (no ``/`` or
# ``..``), so the structure endpoint can resolve a real tournament id.
_SAFE_TOURNAMENT_ID = re.compile(r"^[A-Za-z0-9._:>-]{1,200}$")


def _is_safe_tournament_id(value: str) -> bool:
    return (
        bool(value)
        and value not in (".", "..")
        and ".." not in value
        and _SAFE_TOURNAMENT_ID.match(value) is not None
    )


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


def _epoch_query(request: Request) -> str | None:
    """The optional ``?epoch=<id>`` scoping param, or ``None`` when omitted.

    Malformed / path-traversing values are rejected here (``_BadEpoch``) so the
    handler can answer ``404`` before touching the workspace; the state reader
    re-validates against the on-disk epoch set. An empty/whitespace value is
    treated as omitted (current epoch).
    """
    raw = request.query_params.get("epoch")
    if raw is None:
        return None
    raw = raw.strip()
    if not raw:
        return None
    if not _is_safe_id(raw):
        raise _BadEpoch(raw)
    return raw


class _BadEpoch(Exception):
    """A malformed / path-unsafe ``?epoch=`` value."""


# ---------------------------------------------------------------------------
# GET endpoints
# ---------------------------------------------------------------------------


def _make_state_endpoints(
    paths: WorkspacePaths, *, read_only: bool, started: float
) -> dict[str, Any]:
    """Health / consolidated-state surface."""
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
        return JSONResponse(query.build_snapshot(paths))

    async def api_environment(request: Request) -> JSONResponse:
        # One coalesced read of the whole environment — the front-end
        # refreshes the entire view from this single endpoint instead of
        # fanning out to six. ``?run-log-limit=`` is clamped like the
        # dedicated run-log endpoint.
        limit = query.clamp_run_log_limit(_int_query(request, "run-log-limit"))
        return JSONResponse(query.build_environment(paths, run_log_limit=limit))

    async def api_workspace(_request: Request) -> JSONResponse:
        """L0 (workspace-level) cross-epoch summary for the new shell."""
        return JSONResponse(query.build_workspace_view(paths))

    async def api_health_report(_request: Request) -> JSONResponse:
        return JSONResponse(query.build_health_report(paths))

    async def api_search(request: Request) -> JSONResponse:
        """Sidebar search across entries / judges / patches / mutations.

        The ``?q=`` parameter is the substring to match. An empty or
        whitespace-only query short-circuits to empty result sets so the
        callers cannot trigger a wide scan with a degenerate query.
        """
        q = request.query_params.get("q", "")
        return JSONResponse(query.build_search_results(paths, q))

    async def api_logs(request: Request) -> JSONResponse:
        """The structured operator-log tail (LOGGING.md) for one invocation.

        ``?invocation=latest|<id>`` selects the stream (default latest);
        ``?level=`` filters; ``?limit=`` tails; ``?after=<byte-offset>``
        returns only records appended past that byte cursor so the pane
        appends instead of re-rendering. An empty / no-logs workspace
        degrades to an empty view.

        The read is seek-bounded (log_stream reads at most a few MB), but it
        is still blocking file I/O, so it runs in the threadpool rather than
        on the event loop — a large stream must never stall the dashboard.
        """
        limit = query.clamp_log_limit(_int_query(request, "limit"))
        after = _int_query(request, "after")
        level = request.query_params.get("level") or None
        invocation = request.query_params.get("invocation") or None
        view = await run_in_threadpool(
            query.build_log_view,
            paths,
            limit=limit,
            level=level,
            after=after,
            invocation=invocation,
        )
        return JSONResponse(view)

    return {
        "api_health": api_health,
        "api_state": api_state,
        "api_environment": api_environment,
        "api_workspace": api_workspace,
        "api_health_report": api_health_report,
        "api_search": api_search,
        "api_logs": api_logs,
    }


def _make_epoch_endpoints(paths: WorkspacePaths) -> dict[str, Any]:
    """Epoch surface — contract, lineage feed, trajectories, journal/analysis."""

    async def api_epoch(request: Request) -> JSONResponse:
        # Optional ``?epoch=<id>`` scopes the contract to a NON-current epoch
        # (the dashboard's cross-epoch view); omitted ⇒ current (byte-identical).
        try:
            epoch_id = _epoch_query(request)
            return JSONResponse(query.build_epoch_view(paths, epoch_id))
        except (_BadEpoch, ValueError):
            return JSONResponse({"error": "unknown epoch"}, status_code=404)

    async def api_lineage(request: Request) -> JSONResponse:
        # Optional ``?epoch=<id>`` scopes the feed to ONE epoch's generations
        # (the epoch-scoped generations feed); omitted ⇒ workspace-global.
        try:
            epoch_id = _epoch_query(request)
            return JSONResponse(query.build_lineage_view(paths, epoch_id))
        except (_BadEpoch, ValueError):
            return JSONResponse({"error": "unknown epoch"}, status_code=404)

    async def api_per_judge_trend(request: Request) -> JSONResponse:
        """Per-judge × generation matrix for an epoch (L1 heatmap)."""
        epoch_id = request.path_params["epoch_id"]
        if not _is_safe_id(epoch_id):
            return JSONResponse(
                {"epoch_id": epoch_id, "generations": [], "judges": []},
                status_code=200,
            )
        return JSONResponse(query.build_per_judge_trend(paths, epoch_id))

    async def api_epoch_trajectory(request: Request) -> JSONResponse:
        """Promoted-lineage trajectory + promotion rate + honest verdict.

        ``GET /api/epoch/{epoch_id}/trajectory``. A malformed id degrades
        to the empty trajectory shape (HTTP 200), matching every other
        coordinate handler.
        """
        epoch_id = request.path_params["epoch_id"]
        if not _is_safe_id(epoch_id):
            return JSONResponse(
                {
                    "epoch_id": epoch_id,
                    "points": [],
                    "promotion_rate": None,
                    "promoted_count": 0,
                    "challenger_count": 0,
                    "settled_count": 0,
                    "plateaued": False,
                    "plateau_measurable": False,
                    "verdict": None,
                    "recent_movement": None,
                    "noise_floor": None,
                },
                status_code=200,
            )
        return JSONResponse(query.build_optimization_trajectory(paths, epoch_id))

    async def api_epoch_cost(request: Request) -> JSONResponse:
        """Wall-clock + run-count cost accounting for one epoch.

        ``GET /api/epoch/{epoch_id}/cost``. A malformed id degrades to the
        empty cost shape (HTTP 200).
        """
        epoch_id = request.path_params["epoch_id"]
        if not _is_safe_id(epoch_id):
            return JSONResponse(
                {
                    "epoch_id": epoch_id,
                    "per_matchup": [],
                    "total_runtime_ms": 0,
                    "total_run_count": 0,
                    "total_aborted_count": 0,
                    "promoted_count": 0,
                    "cost_per_promotion_ms": None,
                },
                status_code=200,
            )
        return JSONResponse(query.build_tournament_cost(paths, epoch_id))

    async def api_epoch_racing_field(request: Request) -> JSONResponse:
        """The settled racing-field ladder for one epoch, joined server-side.

        ``GET /api/epoch/{epoch_id}/racing-field``. The per-challenger racing
        records are joined into ONE rung/gate ladder payload here — the
        frontend never reconstructs it. ``present: false`` (HTTP 200) when the
        epoch has no racing records; a malformed id degrades the same way.
        """
        epoch_id = request.path_params["epoch_id"]
        if not _is_safe_id(epoch_id):
            return JSONResponse({"epoch_id": epoch_id, "present": False}, status_code=200)
        return JSONResponse(query.build_racing_field(paths, epoch_id))

    async def api_epoch_round_timeline(request: Request) -> JSONResponse:
        """The epoch's settled round timeline + loss-floor waterfall.

        ``GET /api/epoch/{epoch_id}/round-timeline``. The four-endpoint join
        the frontend used to perform (epoch + lineage + trajectory +
        tournaments -> rounds along the champion spine) is served here; the
        client only overlays its LIVE in-flight round. A malformed id degrades
        to the empty timeline shape (HTTP 200).
        """
        epoch_id = request.path_params["epoch_id"]
        if not _is_safe_id(epoch_id):
            return JSONResponse(
                {
                    "epoch_id": epoch_id,
                    "structure": "gauntlet",
                    "source": "none",
                    "rounds": [],
                    "waterfall": [],
                },
                status_code=200,
            )
        return JSONResponse(query.build_round_timeline(paths, epoch_id))

    async def api_epoch_experiments_ledger(request: Request) -> JSONResponse:
        """The epoch's EXPERIMENTS LEDGER — one row per experiment.

        ``GET /api/epoch/{epoch_id}/experiments-ledger``. The idea, the sites
        it touched, the verdict and its Δ, in round order — joined server-side
        (:func:`zicato.query.build_experiments_ledger`) so the epoch page reads
        an epoch's whole story without opening candidates one at a time. A
        malformed id degrades to the empty ledger shape (HTTP 200), matching
        every other coordinate handler.
        """
        epoch_id = request.path_params["epoch_id"]
        if not _is_safe_id(epoch_id):
            return JSONResponse({"epoch_id": epoch_id, "experiments": []}, status_code=200)
        return JSONResponse(query.build_experiments_ledger(paths, epoch_id))

    async def api_contract_diff(request: Request) -> JSONResponse:
        """L1 (epoch-level) contract diff vs predecessor epoch."""
        epoch_id = request.path_params["epoch_id"]
        if not _is_safe_id(epoch_id):
            return JSONResponse(
                {
                    "epoch_id": epoch_id,
                    "predecessor_epoch_id": None,
                    "components": [],
                    "any_changed": False,
                }
            )
        return JSONResponse(query.build_contract_diff(paths, epoch_id))

    async def api_score_trajectory(request: Request) -> JSONResponse:
        # The environment-wide evolution curve — scalar per generation.
        # Optional ``?epoch=<id>`` scopes to a non-current epoch.
        try:
            epoch_id = _epoch_query(request)
            return JSONResponse(query.build_score_trajectory(paths, epoch_id))
        except (_BadEpoch, ValueError):
            return JSONResponse({"error": "unknown epoch"}, status_code=404)

    async def api_calibration_trend(request: Request) -> JSONResponse:
        """Per-generation calibration trend over the lineage (DIAGNOSTIC).

        ``GET /api/calibration-trend[?epoch=<id>]``. The score fraction per
        generation in lineage order with rolling aggregates. Optional
        ``?epoch=<id>`` scopes to a non-current epoch; omitted ⇒ current.
        Explicitly diagnostic — it never feeds the gate.
        """
        try:
            epoch_id = _epoch_query(request)
            return JSONResponse(query.build_calibration_trend(paths, epoch_id))
        except (_BadEpoch, ValueError):
            return JSONResponse({"error": "unknown epoch"}, status_code=404)

    # -- file-tree / file-browser endpoints --------------------------

    async def api_epoch_journal(request: Request) -> JSONResponse:
        """Return the journal.md text for one epoch as ``{ epoch_id, journal }``."""
        epoch_id = request.path_params["epoch_id"]
        if not _is_safe_id(epoch_id):
            return JSONResponse({"error": "invalid epoch id"}, status_code=400)
        return JSONResponse(query.read_epoch_journal(paths, epoch_id))

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
        text = query.read_epoch_journal_md(paths, epoch_id)
        if text is None:
            return PlainTextResponse(
                f"journal.md not found for epoch {epoch_id}",
                status_code=404,
            )
        return Response(
            content=text,
            media_type="text/markdown; charset=utf-8",
        )

    async def api_epoch_analysis(request: Request) -> JSONResponse:
        """``GET /api/epoch/{id}/analysis`` — the analysis report payload.

        Shape + rendering semantics live on the reader
        (:func:`zicato.query.build_epoch_analysis`).
        """
        epoch_id = request.path_params["epoch_id"]
        if not _is_safe_id(epoch_id):
            return JSONResponse({"error": "invalid epoch id"}, status_code=400)
        return JSONResponse(query.build_epoch_analysis(paths, epoch_id))

    async def api_epoch_analysis_html(request: Request) -> Response:
        """Serve the raw ``analysis.html`` for an epoch.

        Returns the self-contained HTML document so the frontend can
        open it in a new tab or embed it. Returns 404 when absent.
        """
        from starlette.responses import HTMLResponse

        epoch_id = request.path_params["epoch_id"]
        if not _is_safe_id(epoch_id):
            return PlainTextResponse("invalid epoch id", status_code=400)
        html = query.read_epoch_analysis_html(paths, epoch_id)
        if html is None:
            return PlainTextResponse("analysis.html not found for this epoch", status_code=404)
        return HTMLResponse(html)

    async def api_epoch_evals(request: Request) -> JSONResponse:
        """The entries × candidates outcomes matrix for one epoch (EVAL-VIEW.md).

        ``GET /api/epoch/{epoch_id}/evals``. A malformed id degrades to the
        empty matrix shape (HTTP 200), matching every other coordinate handler.
        """
        epoch_id = request.path_params["epoch_id"]
        if not _is_safe_id(epoch_id):
            # Single-source the degrade shape from the reader (N1) so the endpoint
            # and the reader can never drift apart.
            return JSONResponse(query._empty_matrix(epoch_id), status_code=200)
        # The reader does blocking file I/O (the matchup grids + replicate files),
        # so it runs OFF the event loop (the build_log_view precedent, F5).
        view = await run_in_threadpool(query.build_eval_matrix, paths, epoch_id)
        return JSONResponse(view)

    async def api_epoch_eval_entry(request: Request) -> JSONResponse:
        """One board entry's instrument-quality dossier (EVAL-VIEW.md §3.2).

        ``GET /api/epoch/{epoch_id}/eval/{entry_id}``. A malformed id degrades
        to the empty dossier shape (HTTP 200).
        """
        epoch_id = request.path_params["epoch_id"]
        entry_id = request.path_params["entry_id"]
        if not _is_safe_id(epoch_id) or not _is_safe_id(entry_id):
            return JSONResponse(query._empty_dossier(epoch_id, entry_id), status_code=200)
        view = await run_in_threadpool(query.build_eval_dossier, paths, epoch_id, entry_id)
        return JSONResponse(view)

    async def api_epoch_eval_health(request: Request) -> JSONResponse:
        """The WS-HEALTH instrument panel for one epoch (EVAL-VIEW.md §5).

        ``GET /api/epoch/{epoch_id}/eval-health``. A malformed id degrades to the
        empty health shape (HTTP 200), matching every other coordinate handler.
        """
        epoch_id = request.path_params["epoch_id"]
        if not _is_safe_id(epoch_id):
            return JSONResponse(query._empty_health(epoch_id), status_code=200)
        view = await run_in_threadpool(query.build_eval_health, paths, epoch_id)
        return JSONResponse(view)

    async def api_epoch_judge_roster(request: Request) -> JSONResponse:
        """What is armed to judge a run on one epoch's board (#194 §5).

        ``GET /api/epoch/{epoch_id}/judge-roster``. A malformed id degrades to
        the empty roster shape (HTTP 200), matching every other coordinate
        handler. The reader stats the reflection tree, so it runs OFF the
        event loop.
        """
        epoch_id = request.path_params["epoch_id"]
        if not _is_safe_id(epoch_id):
            return JSONResponse(query._empty_judge_roster(epoch_id), status_code=200)
        view = await run_in_threadpool(query.build_judge_roster, paths, epoch_id)
        return JSONResponse(view)

    # -- conversation endpoints --------------------------------------

    return {
        "api_epoch": api_epoch,
        "api_epoch_judge_roster": api_epoch_judge_roster,
        "api_epoch_evals": api_epoch_evals,
        "api_epoch_eval_entry": api_epoch_eval_entry,
        "api_epoch_eval_health": api_epoch_eval_health,
        "api_lineage": api_lineage,
        "api_per_judge_trend": api_per_judge_trend,
        "api_epoch_trajectory": api_epoch_trajectory,
        "api_epoch_cost": api_epoch_cost,
        "api_epoch_racing_field": api_epoch_racing_field,
        "api_epoch_round_timeline": api_epoch_round_timeline,
        "api_epoch_experiments_ledger": api_epoch_experiments_ledger,
        "api_contract_diff": api_contract_diff,
        "api_score_trajectory": api_score_trajectory,
        "api_calibration_trend": api_calibration_trend,
        "api_epoch_journal": api_epoch_journal,
        "api_epoch_journal_md": api_epoch_journal_md,
        "api_epoch_analysis": api_epoch_analysis,
        "api_epoch_analysis_html": api_epoch_analysis_html,
    }


def _make_judge_run_endpoints(paths: WorkspacePaths) -> dict[str, Any]:
    """Per-judge / per-entry / per-run drill-down surface."""

    async def api_per_judge_for_generation(request: Request) -> JSONResponse:
        """Per-judge breakdown for one generation (L2)."""
        epoch_id = request.path_params["epoch_id"]
        generation_id = request.path_params["generation_id"]
        if not _is_safe_id(epoch_id) or not _is_safe_id(generation_id):
            return JSONResponse(
                {"epoch_id": epoch_id, "generation_id": generation_id, "judges": []},
                status_code=200,
            )
        return JSONResponse(query.build_per_judge_for_generation(paths, epoch_id, generation_id))

    async def api_per_entry_for_generation(request: Request) -> JSONResponse:
        """Per-entry breakdown for one generation, via tournament_id FK (L2)."""
        epoch_id = request.path_params["epoch_id"]
        generation_id = request.path_params["generation_id"]
        if not _is_safe_id(epoch_id) or not _is_safe_id(generation_id):
            return JSONResponse(
                {
                    "epoch_id": epoch_id,
                    "generation_id": generation_id,
                    "tournament_id": None,
                    "entries": [],
                },
                status_code=200,
            )
        return JSONResponse(query.build_per_entry_for_generation(paths, epoch_id, generation_id))

    async def api_per_judge_comparison(request: Request) -> JSONResponse:
        """Per-judge Δ between champion and challenger (L3)."""
        epoch_id = request.path_params["epoch_id"]
        champion_id = request.path_params["champion_id"]
        challenger_id = request.path_params["challenger_id"]
        if (
            not _is_safe_id(epoch_id)
            or not _is_safe_id(champion_id)
            or not _is_safe_id(challenger_id)
        ):
            return JSONResponse(
                {
                    "epoch_id": epoch_id,
                    "champion": champion_id,
                    "challenger": challenger_id,
                    "judges": [],
                    "primary_driver": None,
                },
                status_code=200,
            )
        return JSONResponse(
            query.build_per_judge_comparison(paths, epoch_id, champion_id, challenger_id)
        )

    async def api_per_judge_for_run(request: Request) -> JSONResponse:
        """Per-judge breakdown for one run (L4)."""
        run_id = request.path_params["run_id"]
        if not _is_safe_id(run_id):
            return JSONResponse({"run_id": run_id, "judges": []}, status_code=200)
        return JSONResponse(query.build_per_judge_for_run(paths, run_id))

    async def api_per_judge_for_run_by_entry(request: Request) -> JSONResponse:
        """Per-judge breakdown for one run, addressed by (epoch, gen, entry).

        The L4 dashboard view routes by board-entry id; the index keys
        every per-judge row by run id. This resolves the run id from the
        run directory's ``loss.json`` (or falls back to the directory
        name) and delegates to :func:`build_per_judge_for_run`.
        """
        epoch_id = request.path_params["epoch_id"]
        generation_id = request.path_params["generation_id"]
        entry_id = request.path_params["entry_id"]
        if not _is_safe_id(epoch_id) or not _is_safe_id(generation_id) or not _is_safe_id(entry_id):
            return JSONResponse({"run_id": None, "judges": []}, status_code=200)
        run_id = query.resolve_run_id_for_entry(paths, epoch_id, generation_id, entry_id)
        return JSONResponse(query.build_per_judge_for_run(paths, run_id))

    async def api_run_expectations(request: Request) -> JSONResponse:
        """Expectation outcomes for one run (L4)."""
        epoch_id = request.path_params["epoch_id"]
        generation_id = request.path_params["generation_id"]
        entry_id = request.path_params["entry_id"]
        if not _is_safe_id(epoch_id) or not _is_safe_id(generation_id) or not _is_safe_id(entry_id):
            return JSONResponse(
                {
                    "epoch_id": epoch_id,
                    "generation_id": generation_id,
                    "entry_id": entry_id,
                    "outcomes": [],
                },
                status_code=200,
            )
        return JSONResponse(
            query.build_expectation_outcomes_for_run(paths, epoch_id, generation_id, entry_id)
        )

    async def api_run_header(request: Request) -> JSONResponse:
        """Header metrics (runtime/tokens/turns/...) for one run (L4)."""
        epoch_id = request.path_params["epoch_id"]
        generation_id = request.path_params["generation_id"]
        entry_id = request.path_params["entry_id"]
        if not _is_safe_id(epoch_id) or not _is_safe_id(generation_id) or not _is_safe_id(entry_id):
            return JSONResponse(
                {
                    "epoch_id": epoch_id,
                    "generation_id": generation_id,
                    "entry_id": entry_id,
                    "drift_loss": None,
                    "pass_fail": None,
                    "runtime_ms": None,
                    "tokens_spent": None,
                    "output_chars": None,
                    "turns_completed": None,
                    "plan_revisions": None,
                    "wall_clock_budget_exceeded": None,
                    "run_id": None,
                    "adk_session_id": None,
                },
                status_code=200,
            )
        return JSONResponse(query.build_run_header(paths, epoch_id, generation_id, entry_id))

    async def api_run_log(request: Request) -> JSONResponse:
        limit = query.clamp_run_log_limit(_int_query(request, "limit"))
        # ``?after=<cursor>`` requests only events past a cursor so the
        # dashboard appends to its log tail instead of re-rendering it.
        after = _int_query(request, "after")
        return JSONResponse(query.build_run_log(paths, limit, after=after))

    async def api_hypothesis_accuracy(request: Request) -> JSONResponse:
        """Per-experiment hypothesis prediction-accuracy scorecard.

        ``GET /api/hypothesis-accuracy/{epoch_id}/{generation_id}``. Joins
        the proposer's falsifiable movement claims against the realised
        movements and lifts the STAMPED ``hypothesis_match`` verdict
        verbatim (never recomputed — it cannot disagree with the HTML
        report). A malformed coordinate degrades to an empty scorecard
        (HTTP 200), matching every other coordinate handler.
        """
        epoch_id = request.path_params["epoch_id"]
        generation_id = request.path_params["generation_id"]
        if not _is_safe_id(epoch_id) or not _is_safe_id(generation_id):
            return JSONResponse(
                {
                    "epoch_id": epoch_id,
                    "generation_id": generation_id,
                    "claims": [],
                    "score": {"hits": 0, "total": 0, "fraction": None, "brier": None},
                    "pass_rate": {"predicted": "", "observed": None},
                },
                status_code=200,
            )
        return JSONResponse(query.build_hypothesis_accuracy(paths, epoch_id, generation_id))

    return {
        "api_per_judge_for_generation": api_per_judge_for_generation,
        "api_per_entry_for_generation": api_per_entry_for_generation,
        "api_per_judge_comparison": api_per_judge_comparison,
        "api_per_judge_for_run": api_per_judge_for_run,
        "api_per_judge_for_run_by_entry": api_per_judge_for_run_by_entry,
        "api_run_expectations": api_run_expectations,
        "api_run_header": api_run_header,
        "api_run_log": api_run_log,
        "api_hypothesis_accuracy": api_hypothesis_accuracy,
    }


def _make_live_endpoints(paths: WorkspacePaths) -> dict[str, Any]:
    """Live-runtime surface — heartbeat / active runs / active tournament / pipeline."""

    async def api_active_runs(_request: Request) -> JSONResponse:
        return JSONResponse(query.read_active_runs_view(paths))

    async def api_active_tournament(_request: Request) -> JSONResponse:
        return JSONResponse(query.read_active_tournament_dict(paths))

    async def api_heartbeat(_request: Request) -> JSONResponse:
        return JSONResponse(query.read_heartbeat_dict(paths))

    async def api_live_pipeline(_request: Request) -> JSONResponse:
        """The authoritative propose→apply→run→gate pipeline projection.

        ``GET /api/live/pipeline``. The server owns the phase-string
        inference the stepper renders — see ``build_round_pipeline``.
        """
        return JSONResponse(query.build_round_pipeline(paths))

    return {
        "api_active_runs": api_active_runs,
        "api_active_tournament": api_active_tournament,
        "api_heartbeat": api_heartbeat,
        "api_live_pipeline": api_live_pipeline,
    }


# A run_ref is the reflection adjudicator's stable ``{candidate}:{entry}:r{n}``
# decision key (adjudicator.run_ref_for), so it carries ``:`` that the strict
# ``_SAFE_ID`` rejects. This validator widens the alphabet to admit that one
# separator while still blocking path-traversal (no ``/`` or ``..``).
_SAFE_RUN_REF = re.compile(r"^[A-Za-z0-9._:-]{1,200}$")


def _is_safe_run_ref(value: str) -> bool:
    return (
        bool(value)
        and value not in (".", "..")
        and ".." not in value
        and _SAFE_RUN_REF.match(value) is not None
    )


def _make_reflection_endpoints(paths: WorkspacePaths) -> dict[str, Any]:
    """Instrument-lens surface — reflection bill-of-health / scorecards / x-ray.

    Self-contained thin delegates over :mod:`zicato.query.reflection_view`
    (index-first, file-fallback, DQ3 same-shape degrade). Kept in one factory
    + its own routes block so the concurrent endpoints.py thinning (track U2)
    merges additively.
    """

    async def api_reflections(request: Request) -> JSONResponse:
        """Every reflection under the workspace (optional ``?epoch=`` scope)."""
        try:
            epoch_id = _epoch_query(request)
        except (_BadEpoch, ValueError):
            return JSONResponse({"reflections": []}, status_code=404)
        return JSONResponse(query.list_reflections(paths, epoch_id))

    async def api_reflection_summary(request: Request) -> JSONResponse:
        reflection_id = request.path_params["reflection_id"]
        if not _is_safe_id(reflection_id):
            return JSONResponse(
                {"reflection_id": reflection_id, "found": False, "pillars": {}, "findings": []},
                status_code=200,
            )
        return JSONResponse(query.build_reflection_summary(paths, reflection_id))

    async def api_reflection_scorecards(request: Request) -> JSONResponse:
        reflection_id = request.path_params["reflection_id"]
        if not _is_safe_id(reflection_id):
            return JSONResponse({"reflection_id": reflection_id, "judges": []}, status_code=200)
        return JSONResponse(query.build_judge_scorecards(paths, reflection_id))

    async def api_reflection_practices(request: Request) -> JSONResponse:
        reflection_id = request.path_params["reflection_id"]
        if not _is_safe_id(reflection_id):
            return JSONResponse(
                {
                    "reflection_id": reflection_id,
                    "found": False,
                    "checks": [],
                    "verdict_counts": {"sound": 0, "attend": 0, "unsound": 0, "unmeasured": 0},
                },
                status_code=200,
            )
        return JSONResponse(query.build_practice_review(paths, reflection_id))

    async def api_reflection_xray(request: Request) -> JSONResponse:
        reflection_id = request.path_params["reflection_id"]
        judge_name = request.path_params["judge_name"]
        run_ref = request.path_params["run_ref"]
        if (
            not _is_safe_id(reflection_id)
            or not _is_safe_id(judge_name)
            or not _is_safe_run_ref(run_ref)
        ):
            return JSONResponse(
                {
                    "reflection_id": reflection_id,
                    "judge_name": judge_name,
                    "run_ref": run_ref,
                    "found": False,
                    "transcript": {"fidelity": "unavailable", "turns": []},
                    "judge_verdict": None,
                    "adjudication": None,
                },
                status_code=200,
            )
        return JSONResponse(
            query.build_adjudication_xray(paths, reflection_id, judge_name, run_ref)
        )

    async def api_proposer_scorecard(request: Request) -> JSONResponse:
        """The proposer scorecard trend, optionally detailing one ``?epoch=``."""
        try:
            epoch_id = _epoch_query(request)
        except (_BadEpoch, ValueError):
            epoch_id = None
        return JSONResponse(
            await run_in_threadpool(query.build_proposer_scorecard, paths, epoch_id)
        )

    async def api_proposer_recommendations(_request: Request) -> JSONResponse:
        """The pending proposer-recommendation queue (workspace-wide)."""
        return JSONResponse(await run_in_threadpool(query.build_proposer_recommendations, paths))

    async def api_reflection_traces(request: Request) -> JSONResponse:
        """The imported foreign traces for a reflection (TRAJECTORY-UI.md §3.1)."""
        reflection_id = request.path_params["reflection_id"]
        if not _is_safe_id(reflection_id):
            return JSONResponse(
                {
                    "reflection_id": reflection_id,
                    "epoch_id": None,
                    "found": False,
                    "trace_count": 0,
                    "traces": [],
                },
                status_code=200,
            )
        view = await run_in_threadpool(query.build_trace_list, paths, reflection_id)
        return JSONResponse(view)

    async def api_reflection_trace(request: Request) -> JSONResponse:
        """One imported trace: strip + reconstructed conversation (§3.2)."""
        reflection_id = request.path_params["reflection_id"]
        trace_id = request.path_params["trace_id"]
        if not _is_safe_id(reflection_id) or not _is_safe_id(trace_id):
            return JSONResponse(
                {
                    "reflection_id": reflection_id,
                    "epoch_id": None,
                    "found": False,
                    "trace_id": trace_id,
                    "source_file": "",
                    "dialect": "",
                    "line_count": 0,
                    "malformed_line_count": 0,
                    "signal_counts": {},
                    "strip_model": {},
                    "turns": [],
                    "reconstruction_note": "",
                    "episodes": [],
                },
                status_code=200,
            )
        view = await run_in_threadpool(query.build_trace_detail, paths, reflection_id, trace_id)
        return JSONResponse(view)

    async def api_reflection_suggestion_provenance(request: Request) -> JSONResponse:
        """One suggestion's provenance chain: episodes → trace segments (§3.3)."""
        reflection_id = request.path_params["reflection_id"]
        suggestion_id = request.path_params["suggestion_id"]
        if not _is_safe_id(reflection_id) or not _is_safe_id(suggestion_id):
            # single-sourced with the reader's own degrade (the eval endpoints'
            # _empty_* precedent) so the two degrade routes share one shape.
            return JSONResponse(
                query._empty_provenance(reflection_id, suggestion_id),
                status_code=200,
            )
        view = await run_in_threadpool(
            query.build_suggestion_provenance, paths, reflection_id, suggestion_id
        )
        return JSONResponse(view)

    return {
        "api_reflections": api_reflections,
        "api_reflection_summary": api_reflection_summary,
        "api_reflection_scorecards": api_reflection_scorecards,
        "api_reflection_practices": api_reflection_practices,
        "api_reflection_xray": api_reflection_xray,
        "api_reflection_traces": api_reflection_traces,
        "api_reflection_trace": api_reflection_trace,
        "api_reflection_suggestion_provenance": api_reflection_suggestion_provenance,
        "api_proposer_scorecard": api_proposer_scorecard,
        "api_proposer_recommendations": api_proposer_recommendations,
    }


def _make_tournament_endpoints(paths: WorkspacePaths) -> dict[str, Any]:
    """Tournament surface — bracket, structure, matchups, gate."""

    async def api_tournaments(request: Request) -> JSONResponse:
        try:
            epoch_id = _epoch_query(request)
            return JSONResponse(query.build_bracket(paths, epoch_id))
        except (_BadEpoch, ValueError):
            return JSONResponse({"error": "unknown epoch"}, status_code=404)

    async def api_tournament_structure(request: Request) -> JSONResponse:
        """Full bracket / standings / racing state for one tournament.

        ``GET /api/tournament-structure/{epoch_id}/{tournament_id}``. The
        single read Variant T uses to render the actual configured
        structure. A malformed coordinate degrades to an empty gauntlet
        structure (HTTP 200), matching every other coordinate handler.
        """
        epoch_id = request.path_params["epoch_id"]
        tournament_id = request.path_params["tournament_id"]
        if not _is_safe_id(epoch_id) or not _is_safe_tournament_id(tournament_id):
            return JSONResponse(
                {
                    "epoch_id": epoch_id,
                    "tournament_id": tournament_id,
                    "structure": "gauntlet",
                    "structure_params": {},
                    "competitors": [],
                    "rounds": [],
                    "standings": [],
                    "source": "loss_files",
                }
            )
        return JSONResponse(query.build_tournament_structure(paths, epoch_id, tournament_id))

    async def api_tournament_detail(request: Request) -> JSONResponse:
        generation_id = request.path_params["generation_id"]
        if not _is_safe_id(generation_id):
            # A malformed id degrades to "no such matchup".
            return JSONResponse(
                {
                    "epoch_id": query.read_current_epoch(paths),
                    "generation_id": generation_id,
                    "patches": [],
                    "ab_grid": [],
                }
            )
        return JSONResponse(query.build_matchup_detail(paths, generation_id))

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
        return JSONResponse(query.build_matchup_grid(paths, epoch_id, champion_id, challenger_id))

    async def api_gate(request: Request) -> JSONResponse:
        """Structured promote-gate breakdown for one round (L3 decision view).

        ``GET /api/round/{epoch_id}/{champion}/{challenger}/gate``. Decomposes
        the authoritative ``evaluate_gate`` verdict into its ordered rules with
        per-rule status and the real numbers. A malformed coordinate degrades to
        a deferred decision with empty rules (HTTP 200) rather than a 500.
        """
        epoch_id = request.path_params["epoch_id"]
        champion_id = request.path_params["champion_id"]
        challenger_id = request.path_params["challenger_id"]
        if (
            not _is_safe_id(epoch_id)
            or not _is_safe_id(champion_id)
            or not _is_safe_id(challenger_id)
        ):
            return JSONResponse(
                {
                    "epoch_id": epoch_id,
                    "champion": champion_id,
                    "challenger": challenger_id,
                    "decision": "deferred",
                    "reason": "",
                    "deciding_rule": None,
                    "margin": None,
                    "regressed_predicate": None,
                    "regressed_namespace": None,
                    "delta_scalar": None,
                    "delta_pass_rate": None,
                    "champion_scalar": None,
                    "challenger_scalar": None,
                    "live": None,
                    "rules": [],
                    "scalar_components": {"champion": None, "challenger": None},
                    "primary_driver": None,
                    "rating": {"present": False},
                },
                status_code=200,
            )
        return JSONResponse(query.build_gate_breakdown(paths, epoch_id, champion_id, challenger_id))

    async def api_drift_movements(request: Request) -> JSONResponse:
        generation_id = request.path_params["generation_id"]
        if not _is_safe_id(generation_id):
            return JSONResponse(
                {
                    "epoch_id": query.read_current_epoch(paths),
                    "generation_id": generation_id,
                    "champion": None,
                    "challenger": generation_id,
                    "movements": [],
                }
            )
        return JSONResponse(query.build_drift_movements(paths, generation_id))

    return {
        "api_tournaments": api_tournaments,
        "api_tournament_structure": api_tournament_structure,
        "api_tournament_detail": api_tournament_detail,
        "api_matchup_grid": api_matchup_grid,
        "api_gate": api_gate,
        "api_drift_movements": api_drift_movements,
    }


def _make_files_endpoints(paths: WorkspacePaths) -> dict[str, Any]:
    """File-tree / mutation-site browser surface."""

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

    return {
        "api_files": api_files,
        "api_files_tree": api_files_tree,
        "api_files_content": api_files_content,
        "api_files_patches": api_files_patches,
        "api_files_diff": api_files_diff,
        "api_mutations": api_mutations,
        "api_mutation_detail": api_mutation_detail,
    }


def _make_conversation_endpoints(paths: WorkspacePaths) -> dict[str, Any]:
    """Conversation / transcript surface."""

    async def api_conversation(request: Request) -> Response:
        run_id = request.path_params["run_id"]
        if not _is_safe_id(run_id):
            return JSONResponse({"error": "invalid run_id"}, status_code=400)
        if not _HAVE_TRANSCRIPT:
            return JSONResponse(
                {"error": "transcript reconstruction unavailable"},
                status_code=503,
            )
        # Back-compat run_id route, but gen×entry-FIRST when the coordinates
        # are known. The deterministic triple is the primary key: when the
        # caller supplies ``?gen=&entry=`` (and optionally ``?epoch=``), we
        # resolve straight to ``generations/<gen>/runs/<entry>/events.jsonl``
        # — strict to that entry's own run dir, with the run_id only a
        # disambiguator. This inverts the prior run_id-first order, which
        # kept failing on reused / index-only run_ids. We fall back to the
        # opaque run_id lookup only when the triple is absent or resolves to
        # nothing (a pure-run_id caller with no coordinates).
        gen = request.query_params.get("gen")
        entry = request.query_params.get("entry")
        epoch_q = request.query_params.get("epoch")
        triple_ok = bool(gen and entry and _is_safe_id(gen) and _is_safe_id(entry))
        events_path = query.resolve_conversation(
            paths,
            run_id,
            gen=gen if triple_ok else None,
            entry=entry if triple_ok else None,
            epoch=epoch_q if (epoch_q and _is_safe_id(epoch_q)) else "",
        )
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
        result = query.build_matchup_conversations(
            paths, entry_id, reconstruct=reconstruct_transcript
        )
        return JSONResponse(result)

    async def api_run_transcript(request: Request) -> Response:
        """Reconstruct the transcript for one ``(epoch, gen, entry)`` run.

        Powers the L4 conversation diff; resolution + coordinate-stamping
        semantics live on the reader
        (:func:`zicato.query.build_run_transcript`). Always answers 200 —
        an invalid coordinate, an absent run, or a failed reconstruction
        each degrade to the same-shaped empty transcript.
        """
        epoch_id = request.path_params["epoch_id"]
        generation_id = request.path_params["generation_id"]
        entry_id = request.path_params["entry_id"]
        if not _is_safe_id(epoch_id) or not _is_safe_id(generation_id) or not _is_safe_id(entry_id):
            return JSONResponse(
                query.empty_run_transcript(
                    epoch_id,
                    generation_id,
                    entry_id,
                    error="invalid epoch/generation/entry id",
                ),
                status_code=200,
            )
        run_q = request.query_params.get("run")
        match_q = request.query_params.get("match")
        run_q = run_q if (run_q and _is_safe_id(run_q)) else None
        match_q = match_q if (match_q and _is_safe_id(match_q)) else None
        return JSONResponse(
            query.build_run_transcript(
                paths,
                epoch_id,
                generation_id,
                entry_id,
                run_id=run_q,
                match_id=match_q,
                reconstruct=reconstruct_transcript if _HAVE_TRANSCRIPT else None,
            )
        )

    async def api_run_transcript_delta(request: Request) -> Response:
        """The append-only slice of one run's transcript past ``?after=``.

        The live conversation pane's read (issue #194 §2). Cursor
        semantics, the torn-line tolerance and the same-shape degrade all
        live on the reader
        (:func:`zicato.query.build_run_transcript_delta`); this handler
        only validates coordinates and clamps the query. Always answers
        200 — every failure is the ``found: false`` delta.
        """
        epoch_id = request.path_params["epoch_id"]
        generation_id = request.path_params["generation_id"]
        entry_id = request.path_params["entry_id"]
        if not _is_safe_id(epoch_id) or not _is_safe_id(generation_id) or not _is_safe_id(entry_id):
            return JSONResponse(
                query.empty_run_transcript_delta(
                    epoch_id,
                    generation_id,
                    entry_id,
                    error="invalid epoch/generation/entry id",
                ),
                status_code=200,
            )
        run_q = request.query_params.get("run")
        match_q = request.query_params.get("match")
        run_q = run_q if (run_q and _is_safe_id(run_q)) else None
        match_q = match_q if (match_q and _is_safe_id(match_q)) else None
        return JSONResponse(
            query.build_run_transcript_delta(
                paths,
                epoch_id,
                generation_id,
                entry_id,
                after=_int_query(request, "after"),
                limit=_int_query(request, "limit"),
                run_id=run_q,
                match_id=match_q,
                reconstruct=reconstruct_transcript if _HAVE_TRANSCRIPT else None,
            )
        )

    # -- control endpoints (POST) ------------------------------------

    return {
        "api_conversation": api_conversation,
        "api_matchup_conversations": api_matchup_conversations,
        "api_run_transcript": api_run_transcript,
        "api_run_transcript_delta": api_run_transcript_delta,
    }


def _make_control_endpoints(paths: WorkspacePaths, *, read_only: bool) -> dict[str, Any]:
    """Control surface (POST) — the file-based control-channel protocol."""

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

    async def _read_override_body(request: Request) -> dict[str, str]:
        """Read a promote/reject override's structured request body.

        The dashboard's field promote/reject button POSTs a JSON body with
        the override's provenance: ``{reason, epoch, tournament_id,
        structure}`` (all optional — an empty body / a bare ``touch`` yields
        no keys). Only string values are kept; the on-disk control file
        records exactly the keys the operator supplied so the readback can
        reconstruct WHICH field round, structure, and tournament the override
        targeted. A non-JSON / non-object body yields an empty dict, so a
        legacy reason-only POST still works.
        """
        try:
            body = await request.body()
        except Exception:
            return {}
        if not body:
            return {}
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            return {}
        if not isinstance(parsed, dict):
            return {}
        out: dict[str, str] = {}
        for key in ("reason", "epoch", "tournament_id", "structure"):
            val = parsed.get(key)
            if isinstance(val, str) and val:
                out[key] = val
        return out

    def _control_path(*parts: str) -> Path:
        return paths.control_dir.joinpath(*parts)

    def _atomic_write(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(path)

    def _flag_control(flag_name: str) -> Any:
        """A reason-stamped control-flag writer (pause_epoch / skip_round).

        The two flag handlers were byte-identical apart from the flag file
        they write — collapsed into this one factory.
        """

        async def handler(request: Request) -> Response:
            forbidden = _forbidden_if_read_only()
            if forbidden is not None:
                return forbidden
            reason = await _read_reason(request)
            path = _control_path(flag_name)
            payload = {"reason": reason, "ts": _now_iso()}
            _atomic_write(path, json.dumps(payload).encode())
            return JSONResponse({"accepted": True, "path": str(path)}, status_code=202)

        return handler

    async def control_resume(_request: Request) -> Response:
        """Clear the ``pause_epoch`` flag — the dashboard's resume gesture.

        The orchestrator's :func:`block_while_paused` polls the flag until
        it clears (and archives the pause episode itself), so resume is a
        plain atomic unlink of the flag file — never a queued command.
        Idempotent: resuming an unpaused workspace is an accepted no-op
        (``removed: false``) rather than an error, so a double-click /
        raced resume cannot surface a spurious failure.
        """
        forbidden = _forbidden_if_read_only()
        if forbidden is not None:
            return forbidden
        path = _control_path("pause_epoch")
        removed = False
        try:
            path.unlink()
            removed = True
        except FileNotFoundError:
            removed = False
        except OSError:
            return JSONResponse({"error": "could not clear pause flag"}, status_code=500)
        return JSONResponse(
            {"accepted": True, "removed": removed, "path": str(path), "ts": _now_iso()},
            status_code=202,
        )

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
        # Carry the override's provenance (epoch / tournament_id / structure /
        # reason) onto the control file additively — the consumer only reads
        # ``reason``, so the extra keys are inert for the gauntlet path but let
        # a FIELD override's readback name which round/structure it targeted.
        extra = await _read_override_body(request)
        path = _control_path("promote", generation_id)
        payload = {"generation_id": generation_id, "ts": _now_iso(), **extra}
        _atomic_write(path, json.dumps(payload).encode())
        return JSONResponse(payload, status_code=202)

    async def control_reject(request: Request) -> Response:
        forbidden = _forbidden_if_read_only()
        if forbidden is not None:
            return forbidden
        generation_id = request.path_params["generation_id"]
        if not _is_safe_id(generation_id):
            return PlainTextResponse("invalid generation_id", status_code=400)
        extra = await _read_override_body(request)
        path = _control_path("reject", generation_id)
        payload = {"generation_id": generation_id, "ts": _now_iso(), **extra}
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
        "control_pause": _flag_control("pause_epoch"),
        "control_resume": control_resume,
        "control_skip_round": _flag_control("skip_round"),
        "control_kill": control_kill,
        "control_promote": control_promote,
        "control_reject": control_reject,
        "control_brief": control_brief,
    }


def make_endpoints(paths: WorkspacePaths, *, read_only: bool, started: float) -> dict[str, Any]:
    """Build every route handler bound to one workspace.

    Returns a dict of ``name -> handler`` the app wires onto routes,
    composed from the per-surface factories above (state / epoch /
    judge-run / live / tournament / files / conversation / control).
    ``started`` is a ``time.monotonic()`` reference for the health uptime.
    """
    handlers: dict[str, Any] = {}
    handlers.update(_make_state_endpoints(paths, read_only=read_only, started=started))
    handlers.update(_make_epoch_endpoints(paths))
    handlers.update(_make_judge_run_endpoints(paths))
    handlers.update(_make_live_endpoints(paths))
    handlers.update(_make_tournament_endpoints(paths))
    handlers.update(_make_reflection_endpoints(paths))
    handlers.update(_make_files_endpoints(paths))
    handlers.update(_make_conversation_endpoints(paths))
    handlers.update(_make_control_endpoints(paths, read_only=read_only))
    return handlers


def _dashboard_version() -> str:
    """A non-empty build identifier for the dashboard footer."""
    try:
        from importlib.metadata import version

        return version("zicato")
    except Exception:
        return "zicato-dashboard"
