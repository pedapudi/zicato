"""HTTP route handlers for the dashboard service.

Each handler reads the live ``.zicato/`` workspace through
:mod:`zicato.query` and returns a JSON shape the
dashboard front-end consumes. ``/api/environment`` is the consolidated
read of the whole environment; the granular per-section endpoints are
kept alongside it.

Most read routes answer with ONE query-library call, and differ only in
their path, the coordinates they take, the reader they call, and the
canned shape they serve when a coordinate is rejected. Those routes are
declared as data in :data:`READ_ENDPOINTS` and built by one handler
factory, so adding a read route is a table row rather than a function.
The routes that are not that shape — the filesystem browser, the
transcripts, the raw markdown and HTML documents, the query-parameter
reads, and the control POSTs — stay hand-written below the table.

GET routes are always available. The POST control routes write a marker
file into ``.zicato/runtime/control/`` (the file-based control-channel
protocol the orchestrator consumes) and return ``403`` when the server
was created with ``read_only=True``.

The conversation endpoints reconstruct goldfive event streams into
transcripts via :mod:`zicato.query.transcript_reconstruction`.
"""

from __future__ import annotations

import datetime as _dt
import json
import re
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response

from zicato import query
from zicato.query import WorkspacePaths
from zicato.query.transcript_reconstruction import reconstruct_transcript

# ---------------------------------------------------------------------------
# Coordinate guards
# ---------------------------------------------------------------------------

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


#: The guard each coordinate is validated with, keyed by its path-parameter
#: name. A guard belongs to the KIND of coordinate rather than to a route: a
#: tournament id is admitted by the same alphabet wherever it appears. Any
#: name absent here takes the strict :func:`_is_safe_id`.
COORDINATE_GUARDS: Final[Mapping[str, Callable[[str], bool]]] = {
    "tournament_id": _is_safe_tournament_id,
    "run_ref": _is_safe_run_ref,
}


def _coordinate_guard(name: str) -> Callable[[str], bool]:
    return COORDINATE_GUARDS.get(name, _is_safe_id)


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
# The read-endpoint table
# ---------------------------------------------------------------------------

#: A rejected coordinate's canned response body, built from the workspace and
#: the raw coordinates the request carried.
Degrade = Callable[[WorkspacePaths, "dict[str, str]"], Any]

#: A route that takes no ``?epoch=`` scope.
EPOCH_SCOPE_NONE: Final = ""
#: Both a malformed ``?epoch=`` value and an epoch the workspace does not hold
#: serve the degrade: the reader raises for the second, and the route answers
#: the same way for both, because a caller cannot act on the difference.
SCOPE_REJECT_UNKNOWN_EPOCH: Final = "reject_unknown_epoch"
#: Only a malformed ``?epoch=`` value serves the degrade; whatever the reader
#: makes of a well-formed but unknown epoch is served as it comes.
SCOPE_REJECT_MALFORMED_EPOCH: Final = "reject_malformed_epoch"
#: A malformed ``?epoch=`` value is read as no scope at all, so the route
#: answers for the current epoch rather than refusing.
SCOPE_IGNORE_MALFORMED_EPOCH: Final = "ignore_malformed_epoch"


@dataclass(frozen=True, slots=True)
class ReadEndpoint:
    """One dashboard read route served by a single query-library call.

    Attributes
    ----------
    path:
        The route as Starlette binds it, path parameters included. It is
        also the key into :data:`zicato.query.contracts.ENDPOINT_PAYLOADS`,
        which every route in this table must have an entry in.
    reader:
        The :mod:`zicato.query` function the route serves. It is called as
        ``reader(paths, *coordinates)``, so the parameters in ``params``
        are declared in the order the reader takes them.
    serves:
        What the route puts on the wire, and what it serves when a
        coordinate is rejected.
    params:
        The path parameters, in reader-argument order. Each is validated by
        the guard :data:`COORDINATE_GUARDS` gives its name.
    degrade:
        The body a rejected coordinate (or a rejected ``?epoch=`` scope)
        answers with, at ``degrade_status``. Required on a route with
        something to reject — a path parameter, or a scope mode that
        refuses — and ``None`` on a route with neither.
    epoch_scope:
        How the optional ``?epoch=<id>`` parameter is handled — one of the
        four ``SCOPE_`` / ``EPOCH_SCOPE_NONE`` values above. When it is not
        ``EPOCH_SCOPE_NONE`` the resolved epoch id (or ``None`` for the
        current epoch) is passed to the reader after the path coordinates.
    off_event_loop:
        Whether the read blocks on files heavy enough to run in the
        threadpool. A read that walks per-run files or stats a tree must
        never stall the event loop the whole dashboard shares.
    """

    path: str
    reader: Callable[..., Any]
    serves: str
    params: tuple[str, ...] = ()
    degrade: Degrade | None = None
    degrade_status: int = 200
    epoch_scope: str = EPOCH_SCOPE_NONE
    off_event_loop: bool = False

    @property
    def rejects_coordinates(self) -> bool:
        """Whether any request to this route can be refused before the read."""
        return bool(self.params) or self.epoch_scope in (
            SCOPE_REJECT_UNKNOWN_EPOCH,
            SCOPE_REJECT_MALFORMED_EPOCH,
        )


def _echo(rename: Mapping[str, str] | None = None, /, **fixed: Any) -> Degrade:
    """A degrade that repeats the route's coordinates, then fixed fields.

    The coordinates come first, in the order the path declares them, each
    under its own parameter name unless ``rename`` gives it a different
    wire key. The fixed fields follow in the order they are written. Key
    order is part of the shape: a client reading the degrade must meet the
    same fields in the same order the reader emits them for a coordinate
    that resolves.
    """
    keys = dict(rename or {})

    def degrade(_paths: WorkspacePaths, coordinates: dict[str, str]) -> dict[str, Any]:
        shape: dict[str, Any] = {keys.get(name, name): value for name, value in coordinates.items()}
        shape.update(deepcopy(fixed))
        return shape

    return degrade


def _fixed(**fields: Any) -> Degrade:
    """A degrade that names no coordinate — every field is a constant."""

    def degrade(_paths: WorkspacePaths, _coordinates: dict[str, str]) -> dict[str, Any]:
        return deepcopy(fields)

    return degrade


def _never_served(_paths: WorkspacePaths, _coordinates: dict[str, str]) -> dict[str, Any]:
    """The degrade of a route with nothing to reject; it is never reached."""
    return {}


def _degrade_execution_plan(_paths: WorkspacePaths, coordinates: dict[str, str]) -> dict[str, Any]:
    """The empty plan, stamped with a read time like a resolved one."""
    return {
        "epoch_id": coordinates["epoch_id"],
        "generated_at": _now_iso(),
        "board": {"digest": "", "entry_count": 0},
        "note": "unknown epoch",
        "stages": [],
    }


def _degrade_matchup_detail(paths: WorkspacePaths, coordinates: dict[str, str]) -> dict[str, Any]:
    """ "No such matchup", still scoped to the epoch the workspace is on."""
    return {
        "epoch_id": query.read_current_epoch(paths),
        "generation_id": coordinates["generation_id"],
        "patches": [],
        "ab_grid": [],
    }


def _degrade_drift_movements(paths: WorkspacePaths, coordinates: dict[str, str]) -> dict[str, Any]:
    """No movements, with the rejected id standing as the challenger."""
    generation_id = coordinates["generation_id"]
    return {
        "epoch_id": query.read_current_epoch(paths),
        "generation_id": generation_id,
        "champion": None,
        "challenger": generation_id,
        "movements": [],
    }


def _degrade_trace_detail(_paths: WorkspacePaths, coordinates: dict[str, str]) -> dict[str, Any]:
    """An unfound trace: the coordinates, and every field of the detail empty."""
    return {
        "reflection_id": coordinates["reflection_id"],
        "epoch_id": None,
        "found": False,
        "trace_id": coordinates["trace_id"],
        "source_file": "",
        "dialect": "",
        "line_count": 0,
        "malformed_line_count": 0,
        "signal_counts": {},
        "strip_model": {},
        "turns": [],
        "reconstruction_note": "",
        "episodes": [],
    }


#: Every read route served by one query-library call, declared once.
#:
#: A row states the route, the reader behind it, what it serves, the
#: coordinates it takes, and the shape a rejected coordinate answers with —
#: which is everything :func:`_read_handler` needs to build the handler. Five
#: rows take their degrade from the reader's own empty-shape helper, so the
#: route and the reader cannot drift apart.
#:
#: Every row's ``path`` must have an entry in
#: :data:`zicato.query.contracts.ENDPOINT_PAYLOADS`; a correspondence test
#: fails the build in either direction.
READ_ENDPOINTS: Final[tuple[ReadEndpoint, ...]] = (
    # -- workspace-wide state ------------------------------------------
    ReadEndpoint(
        path="/api/state",
        reader=query.build_snapshot,
        serves=(
            "The consolidated live snapshot the front-end opens on: heartbeat, "
            "liveness verdict, lock, active runs, active tournament, lineage, "
            "and the current epoch."
        ),
    ),
    ReadEndpoint(
        path="/api/workspace",
        reader=query.build_workspace_view,
        serves="The workspace-level cross-epoch summary.",
    ),
    ReadEndpoint(
        path="/api/health-report",
        reader=query.build_health_report,
        serves="The workspace health report — what the reader could and could not resolve.",
    ),
    # -- the live runtime surface --------------------------------------
    ReadEndpoint(
        path="/api/active-runs",
        reader=query.read_active_runs_view,
        serves=(
            "One row per active-run record on disk, each stamped with the served "
            "freshness verdict, so a client never ages the records itself."
        ),
    ),
    ReadEndpoint(
        path="/api/active-tournament",
        reader=query.read_active_tournament_dict,
        serves="The live tournament's topology and per-slot field status.",
    ),
    ReadEndpoint(
        path="/api/heartbeat",
        reader=query.read_heartbeat_dict,
        serves=(
            "The orchestrator's heartbeat record, with an always-ageable "
            "timestamp; null for a workspace that never ran."
        ),
    ),
    ReadEndpoint(
        path="/api/config",
        reader=query.read_effective_settings,
        serves=(
            "Every setting the running loop is operating under, each paired "
            "with the tier that set it, read off the heartbeat record; null "
            "when the workspace holds no run record."
        ),
    ),
    ReadEndpoint(
        path="/api/live/pipeline",
        reader=query.build_live_pipeline,
        serves=(
            "The propose → apply → run → gate position, projected from the same "
            "read of the running epoch that serves the live execution plan, so "
            "the two surfaces cannot disagree. The server owns the phase-string "
            "inference; the stepper renders this verdict verbatim."
        ),
        off_event_loop=True,
    ),
    ReadEndpoint(
        path="/api/live/execution-plan",
        reader=query.build_live_execution_plan,
        serves=(
            "The running epoch's plan with a live overlay: the served liveness "
            "verdict, the active path, and one node per still-beating in-flight "
            "run. A workspace that is not live serves its plan with an empty "
            "overlay."
        ),
        off_event_loop=True,
    ),
    # -- reads scoped by the optional ?epoch= parameter -----------------
    ReadEndpoint(
        path="/api/epoch",
        reader=query.build_epoch_view,
        serves=(
            "One epoch's contract — board, brief, scoring, harness. ``?epoch=`` "
            "scopes to a non-current epoch; omitted reads the current one."
        ),
        degrade=_fixed(error="unknown epoch"),
        degrade_status=404,
        epoch_scope=SCOPE_REJECT_UNKNOWN_EPOCH,
    ),
    ReadEndpoint(
        path="/api/lineage",
        reader=query.build_lineage_view,
        serves=(
            "The generations feed. ``?epoch=`` scopes it to one epoch's "
            "generations; omitted reads the whole workspace."
        ),
        degrade=_fixed(error="unknown epoch"),
        degrade_status=404,
        epoch_scope=SCOPE_REJECT_UNKNOWN_EPOCH,
    ),
    ReadEndpoint(
        path="/api/score-trajectory",
        reader=query.build_score_trajectory,
        serves="The evolution curve — one scalar per generation. ``?epoch=`` scopes it.",
        degrade=_fixed(error="unknown epoch"),
        degrade_status=404,
        epoch_scope=SCOPE_REJECT_UNKNOWN_EPOCH,
    ),
    ReadEndpoint(
        path="/api/calibration-trend",
        reader=query.build_calibration_trend,
        serves=(
            "The score fraction per generation in lineage order with rolling "
            "aggregates. Explicitly diagnostic — it never feeds the gate. "
            "``?epoch=`` scopes it."
        ),
        degrade=_fixed(error="unknown epoch"),
        degrade_status=404,
        epoch_scope=SCOPE_REJECT_UNKNOWN_EPOCH,
    ),
    ReadEndpoint(
        path="/api/tournaments",
        reader=query.build_bracket,
        serves="The epoch's tournament bracket. ``?epoch=`` scopes it.",
        degrade=_fixed(error="unknown epoch"),
        degrade_status=404,
        epoch_scope=SCOPE_REJECT_UNKNOWN_EPOCH,
    ),
    ReadEndpoint(
        path="/api/reflections",
        reader=query.list_reflections,
        serves="Every reflection under the workspace. ``?epoch=`` scopes the list.",
        degrade=_fixed(reflections=[]),
        degrade_status=404,
        epoch_scope=SCOPE_REJECT_MALFORMED_EPOCH,
    ),
    ReadEndpoint(
        path="/api/proposer/scorecard",
        reader=query.build_proposer_scorecard,
        serves=(
            "The proposer scorecard trend, detailing the epoch ``?epoch=`` names. "
            "A malformed scope reads as no scope, so the trend still renders."
        ),
        epoch_scope=SCOPE_IGNORE_MALFORMED_EPOCH,
        off_event_loop=True,
    ),
    ReadEndpoint(
        path="/api/proposer/recommendations",
        reader=query.build_proposer_recommendations,
        serves="The pending proposer-recommendation queue, workspace-wide.",
        off_event_loop=True,
    ),
    # -- epoch-coordinate reads ----------------------------------------
    ReadEndpoint(
        path="/api/epoch/{epoch_id}/per-judge-trend",
        reader=query.build_per_judge_trend,
        serves="The per-judge × generation matrix for one epoch (the epoch-level heatmap).",
        params=("epoch_id",),
        degrade=_echo(generations=[], judges=[]),
    ),
    ReadEndpoint(
        path="/api/epoch/{epoch_id}/trajectory",
        reader=query.build_optimization_trajectory,
        serves=(
            "The promoted-lineage trajectory for one epoch, with the promotion "
            "rate and the honest plateau verdict."
        ),
        params=("epoch_id",),
        degrade=_echo(
            points=[],
            promotion_rate=None,
            promoted_count=0,
            challenger_count=0,
            settled_count=0,
            plateaued=False,
            plateau_measurable=False,
            verdict=None,
            recent_movement=None,
            noise_floor=None,
        ),
    ),
    ReadEndpoint(
        path="/api/epoch/{epoch_id}/cost",
        reader=query.build_tournament_cost,
        serves="Wall-clock and run-count cost accounting for one epoch.",
        params=("epoch_id",),
        degrade=_echo(
            per_matchup=[],
            total_runtime_ms=0,
            total_run_count=0,
            total_aborted_count=0,
            promoted_count=0,
            cost_per_promotion_ms=None,
        ),
    ),
    ReadEndpoint(
        path="/api/epoch/{epoch_id}/racing-field",
        reader=query.build_racing_field,
        serves=(
            "The settled racing-field ladder for one epoch, joined server-side "
            "into one rung/gate payload the front-end never reconstructs. "
            "``present: false`` when the epoch has no racing records."
        ),
        params=("epoch_id",),
        degrade=_echo(present=False),
    ),
    ReadEndpoint(
        path="/api/epoch/{epoch_id}/round-timeline",
        reader=query.build_round_timeline,
        serves=(
            "The epoch's settled round timeline and loss-floor waterfall. The "
            "server owns the four-way join (epoch, lineage, trajectory, "
            "tournaments) along the champion spine; the client only overlays "
            "its live in-flight round."
        ),
        params=("epoch_id",),
        degrade=_echo(structure="gauntlet", source="none", rounds=[], waterfall=[]),
    ),
    ReadEndpoint(
        path="/api/epoch/{epoch_id}/execution-plan",
        reader=query.build_execution_plan,
        serves=(
            "The epoch's whole loop as one tree of stages, steps and work "
            "units: what ran, under which candidate, in which round, and what "
            "each step decided — so a reader answers what a run did without "
            "joining four endpoints."
        ),
        params=("epoch_id",),
        degrade=_degrade_execution_plan,
    ),
    ReadEndpoint(
        path="/api/epoch/{epoch_id}/experiments-ledger",
        reader=query.build_experiments_ledger,
        serves=(
            "One row per experiment in round order: the idea, the sites it "
            "touched, the verdict and its delta — the epoch's whole story "
            "without opening candidates one at a time."
        ),
        params=("epoch_id",),
        degrade=_echo(experiments=[]),
    ),
    ReadEndpoint(
        path="/api/contract-diff/{epoch_id}",
        reader=query.build_contract_diff,
        serves="The epoch-level contract diff against the predecessor epoch.",
        params=("epoch_id",),
        degrade=_echo(predecessor_epoch_id=None, components=[], any_changed=False),
    ),
    ReadEndpoint(
        path="/api/epoch/{epoch_id}/journal",
        reader=query.read_epoch_journal,
        serves="One epoch's ``journal.md`` text, as ``{epoch_id, journal}``.",
        params=("epoch_id",),
        degrade=_fixed(error="invalid epoch id"),
        degrade_status=400,
    ),
    ReadEndpoint(
        path="/api/epoch/{epoch_id}/analysis",
        reader=query.build_epoch_analysis,
        serves="One epoch's analysis report payload; the reader owns its rendering semantics.",
        params=("epoch_id",),
        degrade=_fixed(error="invalid epoch id"),
        degrade_status=400,
    ),
    ReadEndpoint(
        path="/api/epoch/{epoch_id}/evals",
        reader=query.build_eval_matrix,
        serves="The entries × candidates outcomes matrix for one epoch.",
        params=("epoch_id",),
        degrade=lambda _paths, c: query._empty_matrix(c["epoch_id"]),
        off_event_loop=True,
    ),
    ReadEndpoint(
        path="/api/epoch/{epoch_id}/eval/{entry_id}",
        reader=query.build_eval_dossier,
        serves="One board entry's instrument-quality dossier.",
        params=("epoch_id", "entry_id"),
        degrade=lambda _paths, c: query._empty_dossier(c["epoch_id"], c["entry_id"]),
        off_event_loop=True,
    ),
    ReadEndpoint(
        path="/api/epoch/{epoch_id}/eval-health",
        reader=query.build_eval_health,
        serves="The instrument-quality panel for one epoch.",
        params=("epoch_id",),
        degrade=lambda _paths, c: query._empty_health(c["epoch_id"]),
        off_event_loop=True,
    ),
    ReadEndpoint(
        path="/api/epoch/{epoch_id}/judge-roster",
        reader=query.build_judge_roster,
        serves="What is armed to judge a run on one epoch's board.",
        params=("epoch_id",),
        degrade=lambda _paths, c: query._empty_judge_roster(c["epoch_id"]),
        off_event_loop=True,
    ),
    # -- generation- and run-coordinate reads ---------------------------
    ReadEndpoint(
        path="/api/generation/{epoch_id}/{generation_id}/per-judge",
        reader=query.build_per_judge_for_generation,
        serves="The per-judge breakdown for one generation.",
        params=("epoch_id", "generation_id"),
        degrade=_echo(judges=[]),
    ),
    ReadEndpoint(
        path="/api/generation/{epoch_id}/{generation_id}/per-entry",
        reader=query.build_per_entry_for_generation,
        serves="The per-entry breakdown for one generation, keyed by its tournament id.",
        params=("epoch_id", "generation_id"),
        degrade=_echo(tournament_id=None, entries=[]),
    ),
    ReadEndpoint(
        path="/api/round/{epoch_id}/{champion_id}/{challenger_id}/per-judge-comparison",
        reader=query.build_per_judge_comparison,
        serves="The per-judge delta between champion and challenger for one decision.",
        params=("epoch_id", "champion_id", "challenger_id"),
        degrade=_echo(
            {"champion_id": "champion", "challenger_id": "challenger"},
            judges=[],
            primary_driver=None,
        ),
    ),
    ReadEndpoint(
        path="/api/run/{run_id}/per-judge",
        reader=query.build_per_judge_for_run,
        serves="The per-judge breakdown for one run, addressed by run id.",
        params=("run_id",),
        degrade=_echo(judges=[]),
    ),
    ReadEndpoint(
        path="/api/run/{epoch_id}/{generation_id}/{entry_id}/per-judge",
        reader=query.build_per_judge_for_entry,
        serves=(
            "The per-judge breakdown for one run, addressed by board-entry "
            "coordinates. The reader resolves the entry's canonical "
            "replicate-0 slot; selecting a sibling replicate is a "
            "query-layer keyword no view calls."
        ),
        params=("epoch_id", "generation_id", "entry_id"),
        degrade=_fixed(run_id=None, judges=[]),
    ),
    ReadEndpoint(
        path="/api/run/{epoch_id}/{generation_id}/{entry_id}/expectations",
        reader=query.build_expectation_outcomes_for_run,
        serves="The expectation outcomes for one run.",
        params=("epoch_id", "generation_id", "entry_id"),
        degrade=_echo(outcomes=[]),
    ),
    ReadEndpoint(
        path="/api/run/{epoch_id}/{generation_id}/{entry_id}/header",
        reader=query.build_run_header,
        serves="The header metrics for one run — runtime, tokens, turns, budget.",
        params=("epoch_id", "generation_id", "entry_id"),
        degrade=_echo(
            drift_loss=None,
            pass_fail=None,
            runtime_ms=None,
            tokens_spent=None,
            output_chars=None,
            turns_completed=None,
            plan_revisions=None,
            wall_clock_budget_exceeded=None,
            run_id=None,
            adk_session_id=None,
        ),
    ),
    ReadEndpoint(
        path="/api/hypothesis-accuracy/{epoch_id}/{generation_id}",
        reader=query.build_hypothesis_accuracy,
        serves=(
            "The per-experiment hypothesis prediction scorecard: the proposer's "
            "falsifiable movement claims against the realised movements, lifting "
            "the stamped verdict verbatim so it cannot disagree with the report."
        ),
        params=("epoch_id", "generation_id"),
        degrade=_echo(
            claims=[],
            score={"hits": 0, "total": 0, "fraction": None, "brier": None},
            pass_rate={"predicted": "", "observed": None},
        ),
    ),
    # -- tournament reads ------------------------------------------------
    ReadEndpoint(
        path="/api/tournament-structure/{epoch_id}/{tournament_id}",
        reader=query.build_tournament_structure,
        serves=(
            "The full bracket, standings and racing state for one tournament — "
            "the single read the console renders the configured structure from."
        ),
        params=("epoch_id", "tournament_id"),
        degrade=_echo(
            structure="gauntlet",
            structure_params={},
            competitors=[],
            rounds=[],
            standings=[],
            source="loss_files",
        ),
    ),
    ReadEndpoint(
        path="/api/tournaments/{generation_id}",
        reader=query.build_matchup_detail,
        serves="One matchup's patches and A/B grid.",
        params=("generation_id",),
        degrade=_degrade_matchup_detail,
    ),
    ReadEndpoint(
        path="/api/matchup-grid/{epoch_id}/{champion_id}/{challenger_id}",
        reader=query.build_matchup_grid,
        serves=(
            "The per-entry A/B grid read straight off the persisted per-run loss "
            "files — the read path a completed tournament's matchup panel uses "
            "when the index was never built."
        ),
        params=("epoch_id", "champion_id", "challenger_id"),
        degrade=_echo(
            {"champion_id": "champion", "challenger_id": "challenger"},
            entry_grid=[],
            scalar=None,
            source="loss_files",
        ),
    ),
    ReadEndpoint(
        path="/api/round/{epoch_id}/{champion_id}/{challenger_id}/gate",
        reader=query.build_gate_breakdown,
        serves=(
            "The promote gate for one round, decomposed into its ordered rules "
            "with per-rule status and the real numbers, from the authoritative "
            "verdict rather than a second evaluation."
        ),
        params=("epoch_id", "champion_id", "challenger_id"),
        degrade=_echo(
            {"champion_id": "champion", "challenger_id": "challenger"},
            decision="deferred",
            reason="",
            deciding_rule=None,
            margin=None,
            regressed_predicate=None,
            regressed_namespace=None,
            delta_scalar=None,
            delta_pass_rate=None,
            champion_scalar=None,
            challenger_scalar=None,
            live=None,
            rules=[],
            scalar_components={"champion": None, "challenger": None},
            primary_driver=None,
            rating={"present": False},
        ),
    ),
    ReadEndpoint(
        path="/api/drift-movements/{generation_id}",
        reader=query.build_drift_movements,
        serves="The per-channel movements one candidate produced against its champion.",
        params=("generation_id",),
        degrade=_degrade_drift_movements,
    ),
    # -- the instrument lens (board reflection) --------------------------
    ReadEndpoint(
        path="/api/reflection/{reflection_id}/summary",
        reader=query.build_reflection_summary,
        serves="One reflection's bill of health: per-pillar verdicts and findings.",
        params=("reflection_id",),
        degrade=_echo(found=False, pillars={}, findings=[]),
    ),
    ReadEndpoint(
        path="/api/reflection/{reflection_id}/scorecards",
        reader=query.build_judge_scorecards,
        serves="One reflection's per-judge scorecards.",
        params=("reflection_id",),
        degrade=_echo(judges=[]),
    ),
    ReadEndpoint(
        path="/api/reflection/{reflection_id}/practices",
        reader=query.build_practice_review,
        serves="One reflection's practice review — each check with its verdict.",
        params=("reflection_id",),
        degrade=_echo(
            found=False,
            checks=[],
            verdict_counts={"sound": 0, "attend": 0, "unsound": 0, "unmeasured": 0},
        ),
    ),
    ReadEndpoint(
        path="/api/reflection/{reflection_id}/xray/{judge_name}/{run_ref}",
        reader=query.build_adjudication_xray,
        serves=(
            "One adjudicated decision opened up: the run's transcript beside the "
            "judge's verdict and the adjudicator's ruling on it."
        ),
        params=("reflection_id", "judge_name", "run_ref"),
        degrade=_echo(
            found=False,
            transcript={"fidelity": "unavailable", "turns": []},
            judge_verdict=None,
            adjudication=None,
        ),
    ),
    ReadEndpoint(
        path="/api/reflection/{reflection_id}/traces",
        reader=query.build_trace_list,
        serves="The foreign traces imported for one reflection.",
        params=("reflection_id",),
        degrade=_echo(epoch_id=None, found=False, trace_count=0, traces=[]),
        off_event_loop=True,
    ),
    ReadEndpoint(
        path="/api/reflection/{reflection_id}/trace/{trace_id}",
        reader=query.build_trace_detail,
        serves="One imported trace: its strip model and the reconstructed conversation.",
        params=("reflection_id", "trace_id"),
        degrade=_degrade_trace_detail,
        off_event_loop=True,
    ),
    ReadEndpoint(
        path="/api/reflection/{reflection_id}/suggestion/{suggestion_id}/provenance",
        reader=query.build_suggestion_provenance,
        serves="One suggestion's provenance chain, from episodes back to trace segments.",
        params=("reflection_id", "suggestion_id"),
        degrade=lambda _paths, c: query._empty_provenance(c["reflection_id"], c["suggestion_id"]),
        off_event_loop=True,
    ),
)


def route_name(path: str) -> str:
    """A stable handler identifier for one route path.

    Path parameters contribute their names, so two routes that differ only
    in their coordinates get different identifiers.
    """
    slug = re.sub(r"[^a-z0-9]+", "_", path.removeprefix("/api/").lower()).strip("_")
    return f"api_{slug}"


def _read_handler(paths: WorkspacePaths, entry: ReadEndpoint) -> Any:
    """Build the handler one table row describes.

    The order is fixed and is the whole contract: reject an unsafe
    coordinate before anything touches the workspace, resolve the optional
    epoch scope, then call the reader — in the threadpool when the row says
    the read blocks on files.
    """
    degrade = entry.degrade or _never_served

    async def handler(request: Request) -> JSONResponse:
        coordinates = {name: request.path_params[name] for name in entry.params}
        for name, value in coordinates.items():
            if not _coordinate_guard(name)(value):
                return JSONResponse(degrade(paths, coordinates), status_code=entry.degrade_status)
        arguments: list[Any] = list(coordinates.values())
        if entry.epoch_scope:
            try:
                arguments.append(_epoch_query(request))
            except (_BadEpoch, ValueError):
                if entry.epoch_scope != SCOPE_IGNORE_MALFORMED_EPOCH:
                    return JSONResponse(
                        degrade(paths, coordinates), status_code=entry.degrade_status
                    )
                arguments.append(None)
        if entry.epoch_scope == SCOPE_REJECT_UNKNOWN_EPOCH:
            # The reader raises for an epoch the workspace does not hold, and
            # that answer degrades the same way a malformed one does.
            try:
                return JSONResponse(entry.reader(paths, *arguments))
            except ValueError:
                return JSONResponse(degrade(paths, coordinates), status_code=entry.degrade_status)
        if entry.off_event_loop:
            return JSONResponse(await run_in_threadpool(entry.reader, paths, *arguments))
        return JSONResponse(entry.reader(paths, *arguments))

    handler.__name__ = route_name(entry.path)
    handler.__doc__ = f"``GET {entry.path}`` — {entry.serves}"
    return handler


def _make_read_endpoints(paths: WorkspacePaths) -> dict[str, Any]:
    """One handler per :data:`READ_ENDPOINTS` row, keyed by its route path."""
    return {entry.path: _read_handler(paths, entry) for entry in READ_ENDPOINTS}


# ---------------------------------------------------------------------------
# Hand-written GET endpoints
# ---------------------------------------------------------------------------


def _make_state_endpoints(
    paths: WorkspacePaths, *, read_only: bool, started: float
) -> dict[str, Any]:
    """Health and the reads whose query parameters shape the response."""
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

    async def api_environment(request: Request) -> JSONResponse:
        # One coalesced read of the whole environment — the front-end
        # refreshes the entire view from this single endpoint instead of
        # fanning out to six. ``?run-log-limit=`` is clamped like the
        # dedicated run-log endpoint.
        limit = query.clamp_run_log_limit(_int_query(request, "run-log-limit"))
        return JSONResponse(query.build_environment(paths, run_log_limit=limit))

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

    async def api_run_log(request: Request) -> JSONResponse:
        """The run-event tail. ``?limit=`` clamps it; ``?after=<cursor>``
        returns only events past a cursor so the dashboard appends to its
        log tail instead of re-rendering it."""
        limit = query.clamp_run_log_limit(_int_query(request, "limit"))
        after = _int_query(request, "after")
        return JSONResponse(query.build_run_log(paths, limit, after=after))

    return {
        "api_health": api_health,
        "api_environment": api_environment,
        "api_search": api_search,
        "api_logs": api_logs,
        "api_run_log": api_run_log,
    }


def _make_epoch_document_endpoints(paths: WorkspacePaths) -> dict[str, Any]:
    """The two epoch documents served as themselves rather than as JSON."""

    async def api_epoch_journal_md(request: Request) -> Response:
        """Serve the raw ``journal.md`` markdown for one epoch.

        The Epoch view's "View raw journal" link points at this endpoint so
        a fresh tab renders the human-readable markdown directly — not the
        JSON envelope ``/api/epoch/{id}/journal`` wraps it in (which is hard
        to skim). Served as ``text/markdown`` with UTF-8 charset; browsers
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

    return {
        "api_epoch_journal_md": api_epoch_journal_md,
        "api_epoch_analysis_html": api_epoch_analysis_html,
    }


def _make_proposal_episode_endpoints(paths: WorkspacePaths) -> dict[str, Any]:
    """Foe's static page for one proposal episode: whether there is one, and it."""

    async def api_proposal_episode_export(request: Request) -> Response:
        """Whether one candidate has Foe's static episode page, and how to get it.

        The proposer panel reads this before it renders: an available page
        becomes a link to ``episode-export.html`` below, and an absent one
        becomes the caption naming the episode log and the command that
        renders it by hand.
        """
        epoch_id = request.path_params["epoch_id"]
        generation_id = request.path_params["generation_id"]
        if not _is_safe_id(epoch_id) or not _is_safe_id(generation_id):
            return JSONResponse({"error": "invalid coordinates"}, status_code=400)
        return JSONResponse(
            query.build_proposal_episode_export(
                paths, epoch_id, generation_id, slot=_int_query(request, "slot")
            )
        )

    async def api_proposal_episode_export_html(request: Request) -> Response:
        """Serve Foe's static page for one proposal episode.

        The page is a file the round wrote inside that episode's own
        directory, resolved from the ``(epoch, generation, slot)``
        coordinates like every other route rather than from a path the
        caller supplies, so no request can name a file of its own. Returns
        404 when the candidate has no page, which is the state the panel
        already knows about and captions.
        """
        from starlette.responses import HTMLResponse

        epoch_id = request.path_params["epoch_id"]
        generation_id = request.path_params["generation_id"]
        if not _is_safe_id(epoch_id) or not _is_safe_id(generation_id):
            return PlainTextResponse("invalid coordinates", status_code=400)
        html = query.read_proposal_episode_export(
            paths, epoch_id, generation_id, slot=_int_query(request, "slot")
        )
        if html is None:
            return PlainTextResponse(f"no episode export for {generation_id}", status_code=404)
        return HTMLResponse(html)

    return {
        "api_proposal_episode_export": api_proposal_episode_export,
        "api_proposal_episode_export_html": api_proposal_episode_export_html,
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
        # Back-compat run_id route, but gen×entry-FIRST when the coordinates
        # are known. The deterministic triple is the primary key: when the
        # caller supplies ``?gen=&entry=`` (and optionally ``?epoch=``), we
        # resolve straight to ``generations/<gen>/runs/<entry>/events.jsonl``
        # — strict to that entry's own run dir, with the run_id only a
        # disambiguator. This inverts the prior run_id-first order, which
        # kept failing on reused / index-only run_ids. We fall back to the
        # opaque run_id lookup only when the triple is absent or resolves to
        # nothing (a pure-run_id caller with no coordinates).
        #
        # ``?gen=`` WITHOUT ``?entry=`` asks for that generation's proposal
        # episode rather than one of its board runs; ``?slot=`` names a
        # best-of-N slate slot. The reader decides which record answers.
        gen = request.query_params.get("gen")
        entry = request.query_params.get("entry")
        epoch_q = request.query_params.get("epoch")
        gen_ok = bool(gen and _is_safe_id(gen))
        entry_ok = bool(entry and _is_safe_id(entry))
        events_path = query.resolve_conversation(
            paths,
            run_id,
            gen=gen if gen_ok else None,
            entry=entry if (gen_ok and entry_ok) else None,
            epoch=epoch_q if (epoch_q and _is_safe_id(epoch_q)) else "",
            slot=_int_query(request, "slot"),
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
        return JSONResponse(query.build_matchup_conversations(paths, entry_id))

    async def api_run_transcript(request: Request) -> Response:
        """Reconstruct the transcript for one ``(epoch, gen, entry)`` run.

        Powers the run-level conversation diff; resolution and
        coordinate-stamping
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
            )
        )

    return {
        "api_conversation": api_conversation,
        "api_matchup_conversations": api_matchup_conversations,
        "api_run_transcript": api_run_transcript,
        "api_run_transcript_delta": api_run_transcript_delta,
    }


# ---------------------------------------------------------------------------
# Control endpoints (POST)
# ---------------------------------------------------------------------------


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
        POST carrying only a reason still works.
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
        # control contract the orchestrator consumes rather than a UI label.
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

    Returns a dict of ``name -> handler`` the app wires onto routes. The
    table-driven read routes are keyed by their ROUTE PATH, so the app binds
    them straight from :data:`READ_ENDPOINTS`; the hand-written handlers keep
    their function names as keys. ``started`` is a ``time.monotonic()``
    reference for the health uptime.
    """
    handlers: dict[str, Any] = {}
    handlers.update(_make_read_endpoints(paths))
    handlers.update(_make_state_endpoints(paths, read_only=read_only, started=started))
    handlers.update(_make_epoch_document_endpoints(paths))
    handlers.update(_make_proposal_episode_endpoints(paths))
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
