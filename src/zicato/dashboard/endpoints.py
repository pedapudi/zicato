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

    async def api_epoch(request: Request) -> JSONResponse:
        # Optional ``?epoch=<id>`` scopes the contract to a NON-current epoch
        # (the dashboard's cross-epoch view); omitted ⇒ current (byte-identical).
        try:
            epoch_id = _epoch_query(request)
            return JSONResponse(state_reader.build_epoch_view(paths, epoch_id))
        except (_BadEpoch, ValueError):
            return JSONResponse({"error": "unknown epoch"}, status_code=404)

    async def api_lineage(_request: Request) -> JSONResponse:
        return JSONResponse(state_reader.build_lineage_view(paths))

    async def api_workspace(_request: Request) -> JSONResponse:
        """L0 (workspace-level) cross-epoch summary for the new shell."""
        return JSONResponse(state_reader.build_workspace_view(paths))

    async def api_per_judge_trend(request: Request) -> JSONResponse:
        """Per-judge × generation matrix for an epoch (L1 heatmap)."""
        epoch_id = request.path_params["epoch_id"]
        if not _is_safe_id(epoch_id):
            return JSONResponse(
                {"epoch_id": epoch_id, "generations": [], "judges": []},
                status_code=200,
            )
        return JSONResponse(state_reader.build_per_judge_trend(paths, epoch_id))

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
                    "plateaued": False,
                    "verdict": None,
                    "recent_movement": None,
                    "noise_floor": None,
                },
                status_code=200,
            )
        return JSONResponse(state_reader.build_optimization_trajectory(paths, epoch_id))

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
        return JSONResponse(state_reader.build_tournament_cost(paths, epoch_id))

    async def api_per_judge_for_generation(request: Request) -> JSONResponse:
        """Per-judge breakdown for one generation (L2)."""
        epoch_id = request.path_params["epoch_id"]
        generation_id = request.path_params["generation_id"]
        if not _is_safe_id(epoch_id) or not _is_safe_id(generation_id):
            return JSONResponse(
                {"epoch_id": epoch_id, "generation_id": generation_id, "judges": []},
                status_code=200,
            )
        return JSONResponse(
            state_reader.build_per_judge_for_generation(paths, epoch_id, generation_id)
        )

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
        return JSONResponse(
            state_reader.build_per_entry_for_generation(paths, epoch_id, generation_id)
        )

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
            state_reader.build_per_judge_comparison(paths, epoch_id, champion_id, challenger_id)
        )

    async def api_per_judge_for_run(request: Request) -> JSONResponse:
        """Per-judge breakdown for one run (L4)."""
        run_id = request.path_params["run_id"]
        if not _is_safe_id(run_id):
            return JSONResponse({"run_id": run_id, "judges": []}, status_code=200)
        return JSONResponse(state_reader.build_per_judge_for_run(paths, run_id))

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
        # Read the entry's loss.json to recover the canonical run_id —
        # that is the key the index's judge_losses table is bound to.
        loss_path = (
            paths.epochs
            / epoch_id
            / "generations"
            / generation_id
            / "runs"
            / entry_id
            / "loss.json"
        )
        run_id: str | None = None
        try:
            loss = json.loads(loss_path.read_text(encoding="utf-8"))
            if isinstance(loss, dict):
                raw_run = loss.get("run_id")
                if isinstance(raw_run, str) and raw_run:
                    run_id = raw_run
        except (FileNotFoundError, OSError, json.JSONDecodeError, ValueError):
            run_id = None
        if run_id is None:
            run_id = entry_id  # Best-effort fallback: directory name.
        result = state_reader.build_per_judge_for_run(paths, run_id)
        return JSONResponse(result)

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
            state_reader.build_expectation_outcomes_for_run(
                paths, epoch_id, generation_id, entry_id
            )
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
        return JSONResponse(state_reader.build_run_header(paths, epoch_id, generation_id, entry_id))

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
        return JSONResponse(state_reader.build_contract_diff(paths, epoch_id))

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

    async def api_tournaments(request: Request) -> JSONResponse:
        try:
            epoch_id = _epoch_query(request)
            return JSONResponse(state_reader.build_bracket(paths, epoch_id))
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
        return JSONResponse(state_reader.build_tournament_structure(paths, epoch_id, tournament_id))

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
        return JSONResponse(
            state_reader.build_gate_breakdown(paths, epoch_id, champion_id, challenger_id)
        )

    async def api_health_report(_request: Request) -> JSONResponse:
        return JSONResponse(state_reader.build_health_report(paths))

    async def api_search(request: Request) -> JSONResponse:
        """Sidebar search across entries / judges / patches / mutations.

        The ``?q=`` parameter is the substring to match. An empty or
        whitespace-only query short-circuits to empty result sets so the
        callers cannot trigger a wide scan with a degenerate query.
        """
        q = request.query_params.get("q", "")
        return JSONResponse(state_reader.build_search_results(paths, q))

    async def api_score_trajectory(request: Request) -> JSONResponse:
        # The environment-wide evolution curve — scalar per generation.
        # Optional ``?epoch=<id>`` scopes to a non-current epoch.
        try:
            epoch_id = _epoch_query(request)
            return JSONResponse(state_reader.build_score_trajectory(paths, epoch_id))
        except (_BadEpoch, ValueError):
            return JSONResponse({"error": "unknown epoch"}, status_code=404)

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
        return JSONResponse(state_reader.build_hypothesis_accuracy(paths, epoch_id, generation_id))

    async def api_calibration_trend(request: Request) -> JSONResponse:
        """Per-generation calibration trend over the lineage (DIAGNOSTIC).

        ``GET /api/calibration-trend[?epoch=<id>]``. The score fraction per
        generation in lineage order with rolling aggregates. Optional
        ``?epoch=<id>`` scopes to a non-current epoch; omitted ⇒ current.
        Explicitly diagnostic — it never feeds the gate.
        """
        try:
            epoch_id = _epoch_query(request)
            return JSONResponse(state_reader.build_calibration_trend(paths, epoch_id))
        except (_BadEpoch, ValueError):
            return JSONResponse({"error": "unknown epoch"}, status_code=404)

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

        Returns ``{ epoch_id, analysis_md, analysis_html_inline,
        analysis_html_available }``. ``analysis_html_inline`` is the
        paper-styled HTML fragment (self-contained inline CSS, inline
        SVG figures) the dashboard can drop directly into the Epoch
        view's Analysis section — same renderer as the standalone
        ``analysis.html`` so both surfaces look like a paper. The raw
        markdown ``analysis_md`` is still returned for backward
        compatibility with older frontends that did their own minimal
        rendering.
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

        analysis_html_inline = ""
        if analysis_md.strip():
            try:
                from zicato.analyzer.report import render_report_html_fragment
                from zicato.analyzer.report_data import gather_epoch_report_data

                data = gather_epoch_report_data(paths.root, epoch_id)
                analysis_html_inline = render_report_html_fragment(epoch_id, analysis_md, data=data)
            except Exception:  # noqa: BLE001 — fragment is best-effort
                analysis_html_inline = ""

        return JSONResponse(
            {
                "epoch_id": epoch_id,
                "analysis_md": analysis_md,
                "analysis_html_inline": analysis_html_inline,
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
        events_path: Path | None = None
        if gen and entry and _is_safe_id(gen) and _is_safe_id(entry):
            epoch_for = epoch_q if (epoch_q and _is_safe_id(epoch_q)) else ""
            events_path = state_reader.resolve_transcript_events(
                paths, epoch_for, gen, entry, run_id=run_id
            )
            if events_path is None:
                # Strict to the entry's own run dir — never a sibling's.
                events_path = state_reader.find_generation_entry_events(paths, gen, entry)
        if events_path is None:
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

    async def api_run_transcript(request: Request) -> Response:
        """Reconstruct the transcript for one ``(epoch, gen, entry)`` run.

        Powers the L4 conversation diff: the focused-run side fetches the
        transcript via this endpoint, and the compare side fetches it
        again with the picker's selected generation. Returns the same
        :class:`Transcript` ``.to_dict()`` shape as ``/api/conversation``
        plus the resolved coordinates so the frontend can label the
        column without a second lookup. Always answers 200 with an empty
        transcript when the run is absent — the frontend renders a
        graceful empty column rather than a hard 404, matching the
        zero-turn-complete-run path the matchup endpoint already takes.
        """
        epoch_id = request.path_params["epoch_id"]
        generation_id = request.path_params["generation_id"]
        entry_id = request.path_params["entry_id"]
        if not _is_safe_id(epoch_id) or not _is_safe_id(generation_id) or not _is_safe_id(entry_id):
            return JSONResponse(
                {
                    "epoch_id": epoch_id,
                    "generation_id": generation_id,
                    "entry_id": entry_id,
                    "run_id": None,
                    "turns": [],
                    "annotations": [],
                    "event_count": 0,
                    "complete": False,
                    "error": "invalid epoch/generation/entry id",
                },
                status_code=200,
            )
        if not _HAVE_TRANSCRIPT:
            return JSONResponse(
                {
                    "epoch_id": epoch_id,
                    "generation_id": generation_id,
                    "entry_id": entry_id,
                    "run_id": None,
                    "turns": [],
                    "annotations": [],
                    "event_count": 0,
                    "complete": False,
                    "error": "transcript reconstruction unavailable",
                },
                status_code=200,
            )
        # PRIMARY resolution: the deterministic (epoch, gen, entry) triple
        # → ``generations/<gen>/runs/<entry>/events.jsonl``, strict to this
        # entry's OWN run directory (never a sibling's). An optional
        # ``?run=`` / ``?match=`` disambiguator selects a specific rung when
        # a gen×entry has multiple runs (successive-halving re-races);
        # without one we DEFAULT to the entry's own canonical events file.
        # This inverts the old run_id-first order: the triple — which the
        # pane always knows — is now the primary key, eliminating the
        # run_id-reuse / index-only / multiple-records failure class.
        run_q = request.query_params.get("run")
        match_q = request.query_params.get("match")
        run_q = run_q if (run_q and _is_safe_id(run_q)) else None
        match_q = match_q if (match_q and _is_safe_id(match_q)) else None
        events_path = state_reader.resolve_transcript_events(
            paths,
            epoch_id,
            generation_id,
            entry_id,
            run_id=run_q,
            match_id=match_q,
        )
        if events_path is None:
            # Genuine absence: no events.jsonl exists for this gen×entry at
            # all. Return an honest empty 200 (the frontend renders the
            # "could not be reconstructed" message) rather than a hard 404,
            # matching the zero-turn-complete-run path.
            return JSONResponse(
                {
                    "epoch_id": epoch_id,
                    "generation_id": generation_id,
                    "entry_id": entry_id,
                    "run_id": run_q,
                    "turns": [],
                    "annotations": [],
                    "event_count": 0,
                    "complete": False,
                },
                status_code=200,
            )
        run_id = run_q or entry_id
        try:
            transcript = reconstruct_transcript(events_path, partial_ok=True)
            payload = transcript.to_dict()
        except Exception as exc:  # noqa: BLE001 — best-effort, never 500
            return JSONResponse(
                {
                    "epoch_id": epoch_id,
                    "generation_id": generation_id,
                    "entry_id": entry_id,
                    "run_id": run_id,
                    "turns": [],
                    "annotations": [],
                    "event_count": 0,
                    "complete": False,
                    "error": f"transcript failed: {exc}",
                },
                status_code=200,
            )
        # The reconstructor sets its own run_id from the events stream;
        # surface the directory-name run_id explicitly when the reducer
        # produced no value (empty file).
        if not payload.get("run_id"):
            payload["run_id"] = run_id
        payload["epoch_id"] = epoch_id
        payload["generation_id"] = generation_id
        payload["entry_id"] = entry_id
        return JSONResponse(payload)

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

    async def control_pause(request: Request) -> Response:
        forbidden = _forbidden_if_read_only()
        if forbidden is not None:
            return forbidden
        reason = await _read_reason(request)
        path = _control_path("pause_epoch")
        payload = {"reason": reason, "ts": _now_iso()}
        _atomic_write(path, json.dumps(payload).encode())
        return JSONResponse({"accepted": True, "path": str(path)}, status_code=202)

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
        "api_health": api_health,
        "api_state": api_state,
        "api_environment": api_environment,
        "api_epoch": api_epoch,
        "api_lineage": api_lineage,
        "api_workspace": api_workspace,
        "api_contract_diff": api_contract_diff,
        "api_per_judge_trend": api_per_judge_trend,
        "api_epoch_trajectory": api_epoch_trajectory,
        "api_epoch_cost": api_epoch_cost,
        "api_per_judge_for_generation": api_per_judge_for_generation,
        "api_per_entry_for_generation": api_per_entry_for_generation,
        "api_per_judge_comparison": api_per_judge_comparison,
        "api_per_judge_for_run": api_per_judge_for_run,
        "api_per_judge_for_run_by_entry": api_per_judge_for_run_by_entry,
        "api_run_expectations": api_run_expectations,
        "api_run_header": api_run_header,
        "api_run_log": api_run_log,
        "api_active_runs": api_active_runs,
        "api_active_tournament": api_active_tournament,
        "api_heartbeat": api_heartbeat,
        "api_tournaments": api_tournaments,
        "api_tournament_structure": api_tournament_structure,
        "api_tournament_detail": api_tournament_detail,
        "api_matchup_grid": api_matchup_grid,
        "api_gate": api_gate,
        "api_health_report": api_health_report,
        "api_search": api_search,
        "api_score_trajectory": api_score_trajectory,
        "api_drift_movements": api_drift_movements,
        "api_hypothesis_accuracy": api_hypothesis_accuracy,
        "api_calibration_trend": api_calibration_trend,
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
        "api_run_transcript": api_run_transcript,
        "control_pause": control_pause,
        "control_resume": control_resume,
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

    Fast-mode caveat: in a fast-mode round the champion side is NOT
    actually executed — its ``status_raw`` is ``"cached"`` and the per-
    entry scalar is reused from the cached aggregate. The matching
    transcript on disk is the one this generation produced when it was
    the live challenger in its *original* tournament, persisted under
    its own generation directory. The active-tournament's per-entry
    ``generation_id`` (stamped by :func:`_normalize_tournament_statuses`
    from the tournament-level parent / child fields) is the correct
    lookup key — using it routes cached sides through the cached
    generation's own runs directory, and live sides through the
    in-progress round's runs directory, in one uniform code path.
    """
    result: dict[str, Any] = {"champion": None, "challenger": None}
    tournament = state_reader.read_active_tournament_dict(paths)
    if not isinstance(tournament, dict):
        return result

    # Index per-(entry, side) so the side resolver can read both the
    # generation_id and the producer's status spelling. The normalizer
    # has already stamped a generation_id on every entry — but we keep a
    # tournament-level fallback for older payloads (or a producer that
    # writes only the tournament-level fields).
    entries_index: dict[tuple[str, str], dict[str, Any]] = {}
    raw_entries = tournament.get("entries")
    if isinstance(raw_entries, list):
        for entry in raw_entries:
            if not isinstance(entry, dict):
                continue
            eid = entry.get("entry_id")
            side = entry.get("side")
            if isinstance(eid, str) and isinstance(side, str):
                entries_index[(eid, side)] = entry

    tournament_parent_gen = tournament.get("parent_generation_id")
    tournament_child_gen = tournament.get("child_generation_id")

    def _resolve_generation_id(side: str, fallback: Any) -> Any:
        # Prefer the per-entry generation_id (stamped explicitly so a
        # cached row can carry a generation distinct from the current
        # round's champion-of-this-round id, if those ever differ). Fall
        # back to the tournament-level field for legacy payloads.
        entry = entries_index.get((entry_id, side))
        if entry is not None:
            gen_id = entry.get("generation_id")
            if isinstance(gen_id, str) and gen_id:
                return gen_id
        return fallback

    def _side(side: str, generation_id: Any) -> dict[str, Any] | None:
        if not isinstance(generation_id, str) or not generation_id:
            return None
        located = state_reader.find_generation_run(paths, generation_id, entry_id)
        if located is None:
            return {
                "run_id": None,
                "generation_id": generation_id,
                "transcript": None,
                "result": None,
            }
        run_id, events_path = located
        transcript: Any = None
        if _HAVE_TRANSCRIPT and reconstruct_transcript is not None:
            try:
                transcript = reconstruct_transcript(events_path, partial_ok=True).to_dict()
            except Exception as exc:
                transcript = {"error": f"transcript failed: {exc}"}
        # Surface a small projection of the sibling ``loss.json`` so the
        # frontend can render an honest "timed out" panel for a run that
        # produced no transcript turns. Without this the dashboard's
        # zero-turn complete-run path falls back to "This run produced
        # no transcript turns" — accurate but useless to the operator.
        result = _read_run_result(events_path.parent)
        return {
            "run_id": run_id,
            "generation_id": generation_id,
            "transcript": transcript,
            "result": result,
        }

    champion_gen = _resolve_generation_id("parent", tournament_parent_gen)
    challenger_gen = _resolve_generation_id("child", tournament_child_gen)
    result["champion"] = _side("parent", champion_gen)
    result["challenger"] = _side("child", challenger_gen)
    return result


def _read_run_result(run_dir: Path) -> dict[str, Any] | None:
    """Project a sibling ``loss.json`` into a small dashboard-friendly shape.

    The frontend needs enough to render an honest "what happened" panel
    for a zero-turn complete run — wall-clock budget exceeded, runtime,
    pass/fail verdict, expectation outcome, and the user-visible metric
    counts (LLM calls, output chars, anything else loss.json already
    publicly exposes). The full ``LossProfile`` would leak internal
    fields (the drift scalar's weight breakdown, schema versioning, the
    canonical adk session id) that the dashboard does not render today;
    project to the subset that matters.

    Returns ``None`` when the run directory has no readable
    ``loss.json`` — the frontend then falls back to the existing
    "This run produced no transcript turns" message.
    """
    if not isinstance(run_dir, Path):
        return None
    loss_path = run_dir / "loss.json"
    if not loss_path.exists():
        return None
    try:
        with open(loss_path, encoding="utf-8") as f:
            loss = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(loss, dict):
        return None

    expectation: dict[str, Any] | None = None
    raw_exp = loss.get("expectation_result")
    if isinstance(raw_exp, dict):
        expectation = {
            "kind": str(raw_exp.get("kind") or ""),
            "passed": bool(raw_exp.get("passed", False)),
            "detail": str(raw_exp.get("detail") or ""),
        }

    metric_counts: list[dict[str, Any]] = []
    raw_metrics = loss.get("metric_counts")
    if isinstance(raw_metrics, list):
        for m in raw_metrics:
            if not isinstance(m, dict):
                continue
            name = m.get("name")
            count = m.get("count")
            if not isinstance(name, str) or count is None:
                continue
            try:
                count_f = float(count)
            except (TypeError, ValueError):
                continue
            metric_counts.append(
                {
                    "name": name,
                    "count": count_f,
                    "severity": str(m.get("severity") or ""),
                }
            )

    return {
        "wall_clock_budget_exceeded": bool(loss.get("wall_clock_budget_exceeded", False)),
        "runtime_ms": int(loss.get("runtime_ms") or 0),
        "pass_fail": loss.get("pass_fail"),
        "expectation_result": expectation,
        "metric_counts": metric_counts,
        "drift_loss": (
            float(loss["drift_loss"]) if isinstance(loss.get("drift_loss"), int | float) else None
        ),
    }
