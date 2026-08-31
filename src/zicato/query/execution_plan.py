"""execution_plan — one served tree of what an epoch's loop actually did.

An evolve epoch is deep: it opens rounds, each round proposes candidates,
applies and validates patches, runs a tournament that sweeps every board
entry for every candidate (possibly several times), gates the result, and
records a decision. Every part of that is on disk, in four different
shapes. This builder joins them into ONE tree so a reader can open any
node and find real substructure below it, instead of joining four
endpoints by hand.

Two sources, two jobs
---------------------
* **The round log** (:mod:`zicato.epoch.round_log`) is authoritative for
  the STAGE/STEP SPINE and its order: which rounds exist, what each round
  attempted, what the gate fired, what was decided. It is NOT a source of
  work units — its ``unit_completed`` events are an aggregate written
  after the duel and always name replicate ``0``
  (``zicato.evolve.round_reporting``), so a tree built from them would
  report replicate counts that never existed.
* **The per-unit files** are authoritative for the WORK UNITS. Each
  ``runs/<entry>/loss.json`` (replicate 0) and ``loss.r<N>.json``
  (replicate ``N``) is one execution of one board entry against one
  candidate, carrying its own coordinates, timing, and verdict. The plan
  enumerates them through :mod:`zicato.query.replicate_scores` — the ONE
  definition of "what counts as a draw for this cell" — so the plan and
  the eval matrix can never disagree about how many times something ran.

Every execution, including the non-duel draws
--------------------------------------------
The replicate namespace is partitioned by owner, and only two of its
ranges are a cell's evidence. The rest ran too: the A/A noise-floor
calibration, the contract pre-flight's deliberately-degraded probes, the
candidate screen, board reflection, and the eval-synthesis admission
probes. They surface as MEASUREMENT BAND steps
(:func:`zicato.query.replicate_scores.measurement_bands`), one per band
per stage, holding one node per draw — so the plan accounts for every
non-attempt loss file on disk and nothing executed is invisible.

A band is not a work unit and must never read as one. The pre-flight band in
particular describes DEGRADED copies of the champion's code, cached under the
champion's own id. Its label and every draw's purpose say so, so that a reader
who meets one of those nodes without its parent still does not mistake a
probe's failure for champion behaviour. An index no owner claims lands in the
``unclaimed`` band rather than being admitted quietly.

Never guess a shape
-------------------
Multiplying a board size by a configured replicate count would draw a
plausible tree that the files never held. A reader who opens
"replicate 2 of 3" believes it. So every node reports only facts a file
records: a node whose facts are incomplete reads ``partial`` and names
what it could not resolve, and a future round shows its existence and
nothing else. The provenance vocabulary reserves ``inferred`` for a
future node that is derived rather than read; nothing here emits it.

Best-effort throughout: every input may be missing, pruned, or torn, and
each failure narrows the tree rather than raising.

Durable model, separate live overlay
------------------------------------
This module builds the typed account of settled files. The live reader
renders this same model with a runtime overlay instead of rewriting it.
Two features are absent by design:

* The ``depth`` parameter. The whole plan is served in one response; a
  client that wants a spine reads the top of the tree and ignores the
  rest. Measured against the largest epoch available for measurement
  (``2026-06-07_e4``, 56 loss files, 64 nodes): the served live payload is
  41.7 KB, well under the 200 KB at which paging the tree would start to
  pay for its own complexity. The payload is close to linear in executed
  draws — those 64 nodes cost ~0.65 KB each — so the number to re-measure
  before revisiting this is executed draws per epoch rather than rounds.
* Absolute-timeline placement for a unit whose loss profile records no
  wall-clock span. Those nodes carry a duration and read
  ``partial``. A start time is NEVER derived from a file mtime — that
  measures when a file was written rather than when the work ran.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from zicato.epoch.round_log import (
    DecisionRecorded,
    GateEvaluated,
    HarnessLoaded,
    HoldoutReleased,
    PatchesApplied,
    ProposalAttempted,
    RoundLog,
    RoundLogEnvelope,
    RoundRecord,
    ValidationFailed,
    fold_round_record,
)
from zicato.query.board_scan import board_entry_id, iter_board_rows
from zicato.query.lineage_view import build_lineage_view
from zicato.query.paths import (
    WorkspacePaths,
    _iso,
    _natural_key,
    _read_json_value,
    _resolve_epoch_id,
    _utc_now,
    coerce_float,
    layout_of,
)
from zicato.query.replicate_scores import (
    UNCLAIMED_BAND,
    MeasurementBand,
    cell_replicate_draws_indexed,
    measurement_band_draws_indexed,
    measurement_bands,
    replicate_index,
)
from zicato.query.runtime_view import read_active_tournament_dict
from zicato.workspace import (
    generation_ids as recorded_generation_ids,
)
from zicato.workspace import (
    round_indices,
    run_entry_ids,
)

# --- the served vocabularies ------------------------------------------------

#: Execution status of a node. ``failed`` describes the EXECUTION rather than
#: the verdict: a board entry that ran cleanly and failed its predicate is
#: ``done`` carrying ``pass_fail: false``, while one that aborted is
#: ``failed``. ``skipped`` is the load-bearing one — a step the loop
#: decided not to take (a holdout it never released) must be visible as an
#: absence the server states rather than as a gap the reader has to notice.
STATUS_PLANNED = "planned"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"

#: How completely a node's facts were resolved. ``exact`` = every field
#: came from a file that records it; ``partial`` = something the node
#: describes could not be resolved, and the node says so in its outcome.
#: ``inferred`` is reserved for derived structure and is never emitted —
#: the plan reports what it reads.
PROVENANCE_EXACT = "exact"
PROVENANCE_PARTIAL = "partial"
PROVENANCE_INFERRED = "inferred"

#: The round's steps, in loop order. Each is a step node under its round
#: whether or not the round reached it, so a round that died in ``apply``
#: shows the three steps it never took rather than ending abruptly.
ROUND_STEPS: tuple[tuple[str, str, str], ...] = (
    ("propose", "Propose", "Draw candidate changes and mint an experiment."),
    ("apply", "Apply", "Apply the experiment's patches into a fresh generation."),
    ("run", "Run", "Execute every board entry against every candidate."),
    ("gate", "Gate", "Decide whether the measured difference clears the bar."),
    ("decide", "Decide", "Record the round's terminal decision."),
)

#: An attempt sibling's file stem suffix (``loss.a3`` / ``loss.r2.a3``) —
#: the provenance record of an execution that was superseded
#: (:func:`zicato.tournament.unit_cache.record_unit_attempt`). Attempts are
#: never work units; they hang under the unit whose slot they lost.
_ATTEMPT_STEM = re.compile(r"^(?P<slot>.+)\.a(?P<attempt>\d+)$")


@dataclass(frozen=True, slots=True)
class PlanNode:
    """One node of the execution plan.

    ``kind`` is an OPEN string. A reader must render a kind it does not
    know from the common fields alone, so a newer server can add a level
    without breaking an older client.

    The two open dicts divide cleanly: ``coordinates`` says WHERE the node
    sits in the workspace (``epoch_id`` / ``generation_id`` / ``entry_id``
    / ``replicate`` / ``match_id``, one spelling each), and
    ``outcome`` says WHAT it produced. ``started_at`` / ``ended_at`` are
    integer milliseconds since the epoch (the ``ts`` wire type), ``None``
    when nothing on disk records the position; ``duration_ms`` can be
    known without them.
    """

    id: str
    kind: str
    label: str
    purpose: str
    status: str
    provenance: str = PROVENANCE_EXACT
    started_at: int | None = None
    ended_at: int | None = None
    duration_ms: int | None = None
    progress: dict[str, int] | None = None
    coordinates: dict[str, Any] = field(default_factory=dict)
    outcome: dict[str, Any] = field(default_factory=dict)
    children: tuple[PlanNode, ...] = ()

    def payload(
        self,
        *,
        status: str | None = None,
        children: list[dict[str, Any]] | None = None,
        active: bool | None = None,
    ) -> dict[str, Any]:
        """Render the node, optionally with live status, children, and activity."""
        payload = {
            "id": self.id,
            "kind": self.kind,
            "label": self.label,
            "purpose": self.purpose,
            # ``is not None`` rather than truthiness: an overlay that resolves a
            # node's status to the empty string is stating a status, and
            # falling back to the node's own would silently overrule it.
            "status": status if status is not None else self.status,
            "provenance": self.provenance,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "duration_ms": self.duration_ms,
            "progress": dict(self.progress) if self.progress is not None else None,
            "coordinates": dict(self.coordinates),
            "outcome": dict(self.outcome),
            "children": children
            if children is not None
            else [child.payload() for child in self.children],
        }
        if active is not None:
            payload["active"] = active
        return payload


@dataclass(frozen=True, slots=True)
class ExecutionPlan:
    """The typed execution plan shared by durable and live readers.

    The live endpoint overlays the frozen tree while rendering and never
    writes present-tense state into the settled account.
    """

    epoch_id: str | None
    generated_at: str
    board_digest: str = ""
    board_entry_count: int = 0
    note: str = ""
    stages: tuple[PlanNode, ...] = ()

    def payload(
        self, render_node: Callable[[PlanNode], dict[str, Any]] | None = None
    ) -> dict[str, Any]:
        """The wire shape, optionally rendered through a node overlay."""
        render = render_node or PlanNode.payload
        return {
            "epoch_id": self.epoch_id,
            "generated_at": self.generated_at,
            "board": {
                "digest": self.board_digest,
                "entry_count": self.board_entry_count,
            },
            "note": self.note,
            "stages": [render(stage) for stage in self.stages],
        }


def _rolled(provenance: str, children: tuple[PlanNode, ...]) -> str:
    """``partial`` when this node or anything under it is partial.

    Provenance propagates UPWARD so a reader who has not opened a branch
    still knows something inside it is unresolved; the leaf that is
    actually incomplete names the reason in its outcome.
    """
    if provenance == PROVENANCE_PARTIAL:
        return PROVENANCE_PARTIAL
    if any(child.provenance == PROVENANCE_PARTIAL for child in children):
        return PROVENANCE_PARTIAL
    return PROVENANCE_EXACT


# ---------------------------------------------------------------------------
# Small readers
# ---------------------------------------------------------------------------


def _ts_ms(value: Any) -> int | None:
    """An ISO-8601 timestamp as integer milliseconds since the epoch."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_dt.UTC)
    return int(parsed.timestamp() * 1000)


def _span(events: list[RoundLogEnvelope]) -> tuple[int | None, int | None, int | None]:
    """``(started_at, ended_at, duration_ms)`` of a round from its log timestamps."""
    stamps = [ms for ms in (_ts_ms(envelope.ts) for envelope in events) if ms is not None]
    if not stamps:
        return None, None, None
    first, last = min(stamps), max(stamps)
    return first, last, last - first


#: One round log's read: its decoded events, and whether the file read
#: cleanly to the end. Both halves are needed at once — the events build the
#: spine, the flag decides whether the round can claim ``exact`` provenance.
_RoundLogRead = tuple[list[RoundLogEnvelope], bool]


def _read_round_events(paths: WorkspacePaths, epoch_id: str, index: int) -> _RoundLogRead:
    """One round's events plus whether the log read cleanly.

    A torn TAIL is skipped by the log reader itself, so the round still
    folds. Interior corruption raises there (the append-only invariant was
    violated) and lands here as ``(<what nothing>, False)`` — the round
    node then reads ``partial`` instead of pretending the round is empty.
    """
    try:
        return RoundLog(paths.root, epoch_id, index).read(), True
    except Exception:  # noqa: BLE001 — best-effort, mirrors the sibling readers
        return [], False


def _board_facts(paths: WorkspacePaths, epoch_id: str) -> tuple[str, list[str]]:
    """The frozen board's ``(digest, entry ids)``.

    The plan carries the DIGEST, never the entries: a board entry repeated
    on every unit node would dominate the response (entries × candidates ×
    replicates copies of the same JSON). A client fetches the board once
    and keys it by this digest.
    """
    rows = iter_board_rows(layout_of(paths).board(epoch_id))
    if not rows:
        return "", []
    canonical = json.dumps(rows, sort_keys=True, separators=(",", ":"), default=str)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return digest, [eid for eid in (board_entry_id(row) for row in rows) if eid]


def _run_result(loss_path: Path) -> dict[str, Any]:
    """The ``result.json`` twin of one loss slot, or an empty dict."""
    from zicato.tournament.unit_cache import read_run_result, unit_result_path  # noqa: PLC0415

    result = read_run_result(unit_result_path(loss_path))
    return result if isinstance(result, dict) else {}


# ---------------------------------------------------------------------------
# Work units — the per-unit files, never the round log
# ---------------------------------------------------------------------------


def _unit_outcome(profile: Any, result: dict[str, Any]) -> dict[str, Any]:
    """What one execution produced, from its loss profile and result twin."""
    aborted = result.get("aborted")
    return {
        "pass_fail": getattr(profile, "pass_fail", None),
        "score": coerce_float(getattr(profile, "score", None)),
        "drift_loss": coerce_float(getattr(profile, "drift_loss", None)),
        "cached": bool(getattr(profile, "cached", False)),
        "aborted": bool(aborted) if isinstance(aborted, bool) else None,
        "abort_reason": str(result.get("abort_reason") or ""),
        "not_completed_reason": getattr(profile, "not_completed_reason", None),
    }


def _timing(profile: Any) -> tuple[int | None, int | None, int | None, str]:
    """``(started_at, ended_at, duration_ms, provenance)`` of ONE execution.

    Without a recorded span the execution has a duration but no position: it
    cannot be placed on a timeline or shown as concurrent, which is exactly
    what ``partial`` is for. Every node that describes one execution — a work
    unit, a superseded attempt, a measurement-band draw — reads its span here,
    so none of them can start reporting a different one.
    """
    started = _ts_ms(getattr(profile, "started_at", None))
    ended = _ts_ms(getattr(profile, "ended_at", None))
    runtime_ms = getattr(profile, "runtime_ms", None)
    return (
        started,
        ended,
        runtime_ms if isinstance(runtime_ms, int) else None,
        PROVENANCE_EXACT if started is not None and ended is not None else PROVENANCE_PARTIAL,
    )


def _unit_status(outcome: dict[str, Any]) -> str:
    """``failed`` for an execution that did not complete, else ``done``.

    A run that completed and failed its predicate is ``done`` — its verdict
    is in ``pass_fail``. Only an abort or an attributed not-completed
    penalty means the work itself did not happen.
    """
    if outcome.get("aborted") or outcome.get("not_completed_reason"):
        return STATUS_FAILED
    return STATUS_DONE


def _attempt_nodes(
    unit_id: str, run_dir: Path, replicate: int, coordinates: dict[str, Any]
) -> tuple[PlanNode, ...]:
    """The superseded executions recorded beside one unit's scoring slot.

    An attempt file is provenance about an execution that lost the slot,
    so it is NEVER a work unit (:func:`is_unit_attempt_slot` is the guard
    every glob-reaching reader owes the run directory). It renders as a
    child of the unit it belongs to, which is the only place it explains
    anything: "this cell passed on its second execution".
    """
    from zicato.telemetry.reducer import read_loss_profile  # noqa: PLC0415
    from zicato.tournament.unit_cache import is_unit_attempt_slot  # noqa: PLC0415

    found: list[tuple[int, PlanNode]] = []
    try:
        children = list(run_dir.iterdir())
    except OSError:
        return ()
    for path in sorted(children):
        if not path.is_file() or not is_unit_attempt_slot(path):
            continue
        match = _ATTEMPT_STEM.match(path.stem)
        if match is None:
            continue
        # The attempt belongs to the slot its stem names minus the ``.a<n>``
        # infix, so it can only ever hang under its own replicate.
        if replicate_index(f"{match.group('slot')}.json") != replicate:
            continue
        index = int(match.group("attempt"))
        try:
            profile = read_loss_profile(path)
        except Exception:  # noqa: BLE001 — an unreadable attempt is dropped
            continue
        outcome = _unit_outcome(profile, _run_result(path))
        started, ended, duration_ms, provenance = _timing(profile)
        found.append(
            (
                index,
                PlanNode(
                    id=f"{unit_id}/a{index}",
                    kind="board_entry_attempt",
                    label=f"Attempt {index}",
                    purpose="An execution of this unit that was superseded.",
                    status=_unit_status(outcome),
                    provenance=provenance,
                    started_at=started,
                    ended_at=ended,
                    duration_ms=duration_ms,
                    coordinates={**coordinates, "attempt": index},
                    outcome=outcome,
                ),
            )
        )
    return tuple(node for _, node in sorted(found, key=lambda pair: pair[0]))


def _unit_nodes(
    paths: WorkspacePaths, epoch_id: str, generation_id: str, entry_id: str, sweep_id: str
) -> tuple[PlanNode, ...]:
    """Every recorded execution of ONE board entry against ONE candidate.

    One node per replicate draw the cell actually holds on disk — the same
    enumeration the eval matrix and the matchup grid count, so a replicate
    can never be visible to one surface and invisible to another.
    """
    run_dir = layout_of(paths).run_dir(epoch_id, generation_id, entry_id)
    nodes: list[PlanNode] = []
    for replicate, profile in cell_replicate_draws_indexed(
        paths, epoch_id, generation_id, entry_id
    ):
        loss_path = run_dir / ("loss.json" if replicate == 0 else f"loss.r{replicate}.json")
        outcome = _unit_outcome(profile, _run_result(loss_path))
        coordinates = {
            "epoch_id": epoch_id,
            "generation_id": generation_id,
            "entry_id": entry_id,
            "replicate": replicate,
            "match_id": str(getattr(profile, "match_id", "") or ""),
        }
        unit_id = f"{sweep_id}/{entry_id}/r{replicate}"
        attempts = _attempt_nodes(unit_id, run_dir, replicate, coordinates)
        started, ended, duration_ms, own = _timing(profile)
        nodes.append(
            PlanNode(
                id=unit_id,
                kind="board_entry_run",
                label=f"{entry_id} · replicate {replicate}",
                purpose="One board entry executed against this candidate.",
                status=_unit_status(outcome),
                provenance=_rolled(own, attempts),
                started_at=started,
                ended_at=ended,
                duration_ms=duration_ms,
                coordinates=coordinates,
                outcome=outcome,
                children=attempts,
            )
        )
    return tuple(nodes)


def _sweep_node(
    paths: WorkspacePaths,
    epoch_id: str,
    generation_id: str,
    parent_id: str,
    board_entry_ids: list[str],
    *,
    settled: bool,
) -> PlanNode:
    """One candidate's board sweep: its executed units, and what is missing.

    ``settled`` says whether the owning stage has closed. An unfinished
    sweep in an open round is still ``running``; the same shortfall in a
    closed round is a sweep that ended incomplete, and the entries with no
    unit on disk are named rather than left for the reader to diff.
    """
    sweep_id = f"{parent_id}/{generation_id}"
    units: list[PlanNode] = []
    covered: set[str] = set()
    for entry_id in run_entry_ids(layout_of(paths), epoch_id, generation_id):
        entry_units = _unit_nodes(paths, epoch_id, generation_id, entry_id, sweep_id)
        if entry_units:
            covered.add(entry_id)
        units.extend(entry_units)
    total = len(board_entry_ids) if board_entry_ids else None
    missing = [eid for eid in board_entry_ids if eid not in covered]
    # Progress counts BOARD entries only: a run directory left behind by an
    # entry the board does not carry is real work, but counting it would
    # report a sweep as more than complete.
    on_board = covered.intersection(board_entry_ids)
    off_board = sorted(covered.difference(board_entry_ids))
    outcome: dict[str, Any] = {"unit_count": len(units)}
    if missing:
        outcome["entries_without_units"] = missing
    if off_board:
        outcome["entries_not_on_board"] = off_board
    if units:
        status = STATUS_DONE if settled or not missing else STATUS_RUNNING
    else:
        status = STATUS_SKIPPED if settled else STATUS_PLANNED
    own = PROVENANCE_PARTIAL if (total is None or missing or off_board) else PROVENANCE_EXACT
    children = tuple(units)
    return PlanNode(
        id=sweep_id,
        kind="board_sweep",
        label=f"Board sweep — {generation_id}",
        purpose="Every board entry executed against one candidate.",
        status=status,
        provenance=_rolled(own, children),
        progress={"done": len(on_board), "total": total} if total is not None else None,
        coordinates={"epoch_id": epoch_id, "generation_id": generation_id},
        outcome=outcome,
        children=children,
    )


# ---------------------------------------------------------------------------
# Measurement bands — the executions that are not a cell's evidence
# ---------------------------------------------------------------------------


#: What a band step says about WHERE it hangs when nothing on disk states the
#: round. A band draw's loss file records its coordinates, its timing and its
#: verdict, but never a round, so the honest placement is the stage that owns
#: the generation the draws sit under — which is where that generation was
#: minted, and not necessarily when the draws were taken.
_UNSTATED_ATTRIBUTION = (
    "the draw files record no round; this band hangs under the stage that owns "
    "its generation, which is where that generation was minted and not "
    "necessarily when the draws were taken"
)

#: The band step's placement when the generation's own id states the round it
#: served — the ephemeral candidate-screen snapshot
#: (:func:`zicato.epoch.screen.screen_generation_round`).
_STATED_ATTRIBUTION = "the generation id names the round these draws served"


def _band_draw_node(
    band_id: str, band: MeasurementBand, run_dir: Path, coordinates: dict[str, Any], profile: Any
) -> PlanNode:
    """One execution recorded in a measurement band.

    Carries the band's purpose verbatim rather than a generic one: a client
    may render this node without its parent, and a deliberately-degraded
    pre-flight probe that arrives labelled only "one draw" is exactly the
    misreading the band vocabulary exists to prevent.
    """
    generation_id, entry_id = coordinates["generation_id"], coordinates["entry_id"]
    replicate = coordinates["replicate"]
    # Every band index is above zero, so a band draw is always a sibling slot.
    outcome = _unit_outcome(profile, _run_result(run_dir / f"loss.r{replicate}.json"))
    started, ended, duration_ms, provenance = _timing(profile)
    return PlanNode(
        id=f"{band_id}/{generation_id}/{entry_id}/r{replicate}",
        kind="measurement_draw",
        label=f"{generation_id} · {entry_id} · replicate {replicate}",
        purpose=band.purpose,
        status=_unit_status(outcome),
        provenance=provenance,
        started_at=started,
        ended_at=ended,
        duration_ms=duration_ms,
        coordinates=coordinates,
        outcome=outcome,
    )


def _band_steps(
    paths: WorkspacePaths,
    epoch_id: str,
    stage_id: str,
    generation_ids: list[str],
    stated: frozenset[str] = frozenset(),
) -> tuple[PlanNode, ...]:
    """The measurement bands executed under this stage's generations.

    One step per band that actually holds draws — a band with nothing on
    disk yields NO node, because an empty step would assert that a
    measurement was planned, and nothing here records a plan.

    ``stated`` names the generations whose own id places them in this stage
    (the screen snapshots). A band drawn only from those is ``exact``; every
    other band is ``partial`` and says why in its outcome.
    """
    layout = layout_of(paths)
    draws: dict[str, list[PlanNode]] = {}
    contributors: dict[str, set[str]] = {}
    for generation_id in generation_ids:
        for entry_id in run_entry_ids(layout, epoch_id, generation_id):
            run_dir = layout.run_dir(epoch_id, generation_id, entry_id)
            for replicate, band, profile in measurement_band_draws_indexed(
                paths, epoch_id, generation_id, entry_id
            ):
                coordinates = {
                    "epoch_id": epoch_id,
                    "generation_id": generation_id,
                    "entry_id": entry_id,
                    "replicate": replicate,
                    "match_id": str(getattr(profile, "match_id", "") or ""),
                    "band": band.key,
                }
                draws.setdefault(band.key, []).append(
                    _band_draw_node(
                        f"{stage_id}/band:{band.key}", band, run_dir, coordinates, profile
                    )
                )
                contributors.setdefault(band.key, set()).add(generation_id)
    steps: list[PlanNode] = []
    for band in (*measurement_bands(), UNCLAIMED_BAND):
        children = tuple(draws.get(band.key, ()))
        if not children:
            continue
        generations = sorted(contributors.get(band.key, set()), key=_natural_key)
        attributed = bool(generations) and all(gid in stated for gid in generations)
        steps.append(
            PlanNode(
                id=f"{stage_id}/band:{band.key}",
                kind="measurement_band",
                label=band.label,
                purpose=band.purpose,
                # The draws exist, so the band ran. Whether each draw
                # completed is that draw's own status; a band is a group of
                # measurements and has no verdict of its own to fail.
                status=STATUS_DONE,
                provenance=_rolled(
                    PROVENANCE_EXACT if attributed else PROVENANCE_PARTIAL, children
                ),
                coordinates={"epoch_id": epoch_id, "band": band.key},
                outcome={
                    "draw_count": len(children),
                    "replicate_range": [band.start, band.stop - 1]
                    if band.stop > band.start
                    else [],
                    "generation_ids": generations,
                    "attribution": _STATED_ATTRIBUTION if attributed else _UNSTATED_ATTRIBUTION,
                },
                children=children,
            )
        )
    return tuple(steps)


# ---------------------------------------------------------------------------
# Stages and steps — the round log's spine
# ---------------------------------------------------------------------------


def _typed(events: list[RoundLogEnvelope], cls: type) -> list[Any]:
    """Every decoded event of one type, in log order."""
    return [e.event for e in events if isinstance(e.event, cls)]


def _propose_step(
    step_id: str, events: list[RoundLogEnvelope], record: RoundRecord, closed: bool
) -> PlanNode:
    """The round's proposal session: one node per attempt the log recorded."""
    attempts = _typed(events, ProposalAttempted)
    children: list[PlanNode] = []
    for ordinal, attempt in enumerate(attempts, start=1):
        errors = list(attempt.errors)
        children.append(
            PlanNode(
                id=f"{step_id}/attempt:{ordinal}",
                kind="proposal_attempt",
                label=f"Attempt {ordinal}",
                purpose="One proposer draw, and whether it settled.",
                status=STATUS_FAILED if errors else STATUS_DONE,
                coordinates={"slot_index": attempt.slot_index},
                outcome={"errors": errors},
            )
        )
    session = record.proposal
    outcome = {
        "attempts": session.attempts,
        "candidates_sampled": session.candidates_sampled,
        "candidates_screened": session.candidates_screened,
        "screen_vetoes": session.screen_vetoes,
        "critique_index": session.critique_index,
        "critique_reason": session.critique_reason,
        "experiment_ids": list(session.experiment_ids),
    }
    if session.experiment_ids:
        status = STATUS_DONE
    elif attempts:
        status = STATUS_FAILED
    else:
        status = STATUS_SKIPPED if closed else STATUS_PLANNED
    return _step_node(step_id, "propose", status, outcome, tuple(children))


def _apply_step(step_id: str, events: list[RoundLogEnvelope], closed: bool) -> PlanNode:
    """Patch application, plus the validation failure when there was one.

    A PASSING validation writes no event, so this step never draws a
    "validated" node it did not read — the empty ``validation_findings``
    on a step that applied patches is the whole record.
    """
    applied = _typed(events, PatchesApplied)
    failures = _typed(events, ValidationFailed)
    entrypoints = {e.generation_id: e.entrypoint_file for e in _typed(events, HarnessLoaded)}
    children: list[PlanNode] = []
    for applied_event in applied:
        generation_id = applied_event.generation_id
        children.append(
            PlanNode(
                id=f"{step_id}/{generation_id}",
                kind="apply_patches",
                label=f"Patches applied — {generation_id}",
                purpose="The experiment's patches became a generation snapshot.",
                status=STATUS_DONE,
                coordinates={"generation_id": generation_id},
                outcome={"entrypoint_file": entrypoints.get(generation_id, "")},
            )
        )
    findings: list[str] = []
    for failure in failures:
        findings.extend(failure.findings)
    if failures:
        children.append(
            PlanNode(
                id=f"{step_id}/validation",
                kind="validate",
                label="Validation",
                purpose="Snapshot validation rejected the applied patches.",
                status=STATUS_FAILED,
                outcome={"findings": findings},
            )
        )
    if failures:
        status = STATUS_FAILED
    elif applied:
        status = STATUS_DONE
    else:
        status = STATUS_SKIPPED if closed else STATUS_PLANNED
    return _step_node(
        step_id,
        "apply",
        status,
        {"generation_ids": [e.generation_id for e in applied], "validation_findings": findings},
        tuple(children),
    )


def _run_step(
    paths: WorkspacePaths,
    epoch_id: str,
    step_id: str,
    generation_ids: list[str],
    board_entry_ids: list[str],
    *,
    closed: bool,
) -> PlanNode:
    """The tournament: one board sweep per candidate this round evaluated."""
    sweeps = tuple(
        _sweep_node(paths, epoch_id, gid, step_id, board_entry_ids, settled=closed)
        for gid in generation_ids
    )
    done = sum(1 for sweep in sweeps if sweep.status == STATUS_DONE)
    if not sweeps:
        status = STATUS_SKIPPED if closed else STATUS_PLANNED
    elif any(sweep.status == STATUS_RUNNING for sweep in sweeps):
        status = STATUS_RUNNING
    elif done:
        status = STATUS_DONE
    else:
        status = STATUS_SKIPPED if closed else STATUS_PLANNED
    unit_count = sum(len(sweep.children) for sweep in sweeps)
    return _step_node(
        step_id,
        "run",
        status,
        {"unit_count": unit_count},
        sweeps,
        progress={"done": done, "total": len(sweeps)} if sweeps else None,
    )


def _gate_step(
    step_id: str, events: list[RoundLogEnvelope], record: RoundRecord, closed: bool
) -> PlanNode:
    """The gate's evaluations, and the holdout release when it happened.

    A round with no ``holdout_released`` event did not release a holdout;
    on a closed round that absence is a ``skipped`` node the server
    states, because a gap in the tree tells the reader nothing.
    """
    children: list[PlanNode] = []
    for ordinal, gate in enumerate(_typed(events, GateEvaluated), start=1):
        children.append(
            PlanNode(
                id=f"{step_id}/evaluation:{ordinal}",
                kind="gate_evaluation",
                label=f"Gate evaluation {ordinal}",
                purpose="The promote gate compared the two sides.",
                status=STATUS_DONE,
                outcome={
                    "decision": gate.decision,
                    # The gate names the rule that REJECTED; a clean
                    # promotion fires none, so this is empty rather than a
                    # rule name the server made up.
                    "deciding_rule": gate.rule_fired,
                    "champion_scalar": gate.champion_scalar,
                    "challenger_scalar": gate.challenger_scalar,
                    "margin_required": gate.margin_required,
                    "attributable_regressions": list(gate.attributable_regressions),
                },
            )
        )
    holdout: HoldoutReleased | None = record.holdout
    children.append(
        PlanNode(
            id=f"{step_id}/holdout",
            kind="holdout_release",
            label="Holdout release",
            purpose="The Ladder released the holdout-confirmation bit.",
            status=(
                STATUS_DONE
                if holdout is not None
                else (STATUS_SKIPPED if closed else STATUS_PLANNED)
            ),
            outcome={"confirmed": holdout.confirmed if holdout is not None else None},
        )
    )
    gates = _typed(events, GateEvaluated)
    if gates:
        status = STATUS_DONE
    else:
        status = STATUS_SKIPPED if closed else STATUS_PLANNED
    return _step_node(
        step_id,
        "gate",
        status,
        {"evidence_trail": [dict(row) for row in record.evidence_trail]},
        tuple(children),
    )


def _decide_step(
    step_id: str,
    events: list[RoundLogEnvelope],
    generation_ids: list[str],
    lineage: dict[str, dict[str, Any]],
    experiments: dict[str, dict[str, Any]],
    closed: bool,
) -> PlanNode:
    """The round's terminal decision, with each candidate's recorded fate.

    ``promoted`` is the tri-state from ``lineage.json``, which owns
    topology, and the deltas beside it are journal detail from
    the generation's own experiment record.
    """
    decisions = _typed(events, DecisionRecorded)
    candidates: list[dict[str, Any]] = []
    for generation_id in generation_ids:
        node = lineage.get(generation_id, {})
        outcome = experiments.get(generation_id, {})
        candidates.append(
            {
                "generation_id": generation_id,
                "promoted": node.get("promoted"),
                "scalar_score_delta": coerce_float(outcome.get("scalar_score_delta")),
                "rejection_reason": str(outcome.get("rejection_reason") or ""),
            }
        )
    last = decisions[-1] if decisions else None
    return _step_node(
        step_id,
        "decide",
        (STATUS_DONE if last is not None else (STATUS_SKIPPED if closed else STATUS_PLANNED)),
        {
            "decision": last.decision if last is not None else "",
            # Spelled out rather than ``provenance``: on a node that already
            # carries a provenance FIELD, the log's decision-provenance block
            # is a different thing and must not read as the same one.
            "decision_provenance": dict(last.provenance) if last is not None else {},
            "candidates": candidates,
        },
    )


def _step_node(
    step_id: str,
    step: str,
    status: str,
    outcome: dict[str, Any],
    children: tuple[PlanNode, ...] = (),
    *,
    progress: dict[str, int] | None = None,
) -> PlanNode:
    label, purpose = next((lbl, pur) for key, lbl, pur in ROUND_STEPS if key == step)
    return PlanNode(
        id=step_id,
        kind=f"{step}_step",
        label=label,
        purpose=purpose,
        status=status,
        provenance=_rolled(PROVENANCE_EXACT, children),
        progress=progress,
        outcome=outcome,
        children=children,
    )


def _round_stage(
    paths: WorkspacePaths,
    epoch_id: str,
    index: int,
    log: _RoundLogRead,
    board_entry_ids: list[str],
    lineage: dict[str, dict[str, Any]],
    experiments: dict[str, dict[str, Any]],
    generation_ids: list[str],
    band_generation_ids: list[str],
    stated: frozenset[str],
) -> PlanNode:
    """One round, spine-first: its five steps in loop order, then its bands.

    The band steps follow the five loop steps rather than joining them: a
    measurement band is not a turn of the loop, and the round's ``progress``
    stays a count over the five steps it is made of.
    """
    events, readable = log
    record = fold_round_record(events)
    closed = record.closed
    stage_id = f"e:{epoch_id}/round:{index}"
    steps = (
        _propose_step(f"{stage_id}/propose", events, record, closed),
        _apply_step(f"{stage_id}/apply", events, closed),
        _run_step(
            paths, epoch_id, f"{stage_id}/run", generation_ids, board_entry_ids, closed=closed
        ),
        _gate_step(f"{stage_id}/gate", events, record, closed),
        _decide_step(f"{stage_id}/decide", events, generation_ids, lineage, experiments, closed),
    )
    bands = _band_steps(paths, epoch_id, stage_id, band_generation_ids, stated)
    started, ended, duration = _span(events)
    outcome: dict[str, Any] = {
        "contract_hash": record.contract_hash,
        "decision": record.decision,
        "complete": record.complete,
    }
    if not readable:
        outcome["note"] = "round log unreadable past its first defect"
    return PlanNode(
        id=stage_id,
        kind="round",
        label=f"Round {index}",
        purpose="One turn of the outer loop: propose, apply, run, gate, decide.",
        status=STATUS_DONE if closed else STATUS_RUNNING,
        provenance=_rolled(PROVENANCE_EXACT if readable else PROVENANCE_PARTIAL, steps + bands),
        started_at=started,
        ended_at=ended,
        duration_ms=duration,
        progress={
            "done": sum(1 for step in steps if step.status == STATUS_DONE),
            "total": len(steps),
        },
        coordinates={"epoch_id": epoch_id, "round_index": index},
        outcome=outcome,
        children=steps + bands,
    )


def _baseline_stage(
    paths: WorkspacePaths, epoch_id: str, generation_ids: list[str], board_entry_ids: list[str]
) -> PlanNode:
    """The epoch's baseline: the candidates no round minted.

    A generation that no round log claims was not produced by this epoch's
    loop — it is the seed champion the epoch opened on. Assigning each
    generation to exactly one stage is also what makes the plan's unit
    count equal the unit files on disk: nothing is counted twice.
    """
    stage_id = f"e:{epoch_id}/baseline"
    sweeps = tuple(
        _sweep_node(paths, epoch_id, gid, stage_id, board_entry_ids, settled=True)
        for gid in generation_ids
    )
    # The pre-loop measurements — the noise floor and the contract pre-flight's
    # degraded probes — are drawn against the incoming champion, so they land
    # here with the generation they were drawn from.
    bands = _band_steps(paths, epoch_id, stage_id, list(generation_ids))
    if not sweeps:
        status = STATUS_SKIPPED
    elif any(sweep.status == STATUS_DONE for sweep in sweeps):
        status = STATUS_DONE
    else:
        status = STATUS_SKIPPED
    return PlanNode(
        id=stage_id,
        kind="baseline",
        label="Baseline",
        purpose="Evaluate the incoming champion before the loop proposes anything.",
        status=status,
        provenance=_rolled(PROVENANCE_EXACT, sweeps + bands),
        progress={"done": len(sweeps), "total": len(sweeps)} if sweeps else None,
        coordinates={"epoch_id": epoch_id},
        outcome={"generation_ids": list(generation_ids)},
        children=sweeps + bands,
    )


def _planned_rounds(
    paths: WorkspacePaths, epoch_id: str, observed: list[int]
) -> tuple[PlanNode, ...]:
    """The rounds a run still owes, when something on disk states the total.

    The epoch contract records no round count — the requested total is a
    property of the INVOCATION, and the only place it is written down is
    the runtime tournament state's ``total_rounds``. So a workspace whose
    runtime state names no total gets no planned tail at all rather than a
    guessed one. A planned node carries its existence and nothing else:
    no children, no board size, no candidate names, and ``partial``
    provenance, because that is the whole of what is known.
    """
    state = read_active_tournament_dict(paths)
    if not isinstance(state, dict) or state.get("epoch_id") != epoch_id:
        return ()
    total = state.get("total_rounds")
    if not isinstance(total, int) or isinstance(total, bool):
        return ()
    remaining = total - len(observed)
    if remaining <= 0:
        return ()
    first = (max(observed) + 1) if observed else 0
    return tuple(
        PlanNode(
            id=f"e:{epoch_id}/round:{index}",
            kind="round",
            label=f"Round {index}",
            purpose="One turn of the outer loop: propose, apply, run, gate, decide.",
            status=STATUS_PLANNED,
            provenance=PROVENANCE_PARTIAL,
            coordinates={"epoch_id": epoch_id, "round_index": index},
            outcome={"note": "not started; only the requested round count is known"},
        )
        for index in range(first, first + remaining)
    )


# ---------------------------------------------------------------------------
# The plan
# ---------------------------------------------------------------------------


def _empty_plan_model(epoch_id: str | None, note: str) -> ExecutionPlan:
    """The empty typed model behind the one durable degrade shape."""
    return ExecutionPlan(epoch_id=epoch_id, generated_at=_iso(_utc_now()), note=note)


def build_execution_plan(paths: WorkspacePaths, epoch_id: str | None = None) -> dict[str, Any]:
    """``GET /api/epoch/{epoch_id}/execution-plan`` — what the epoch's loop did.

    Returns::

        {
          "epoch_id", "generated_at", "note",
          "board": {"digest", "entry_count"},
          "stages": [<node>, ...]
        }

    where a node is :meth:`PlanNode.payload` and the stages are the
    baseline, then every round on disk in index order, then the rounds the
    run still owes.

    Node ids are stable between responses so a client can diff a served
    tree against the one it holds and keep a node open. The grammar is
    positional, each level omitted where it does not apply::

        e:<epoch_id>[/round:<n>|/baseline][/<step>][/<key>...]

    where a step's child keys are the natural key of that level:
    ``<generation_id>`` for a board sweep or an applied patch set,
    ``<entry_id>/r<replicate>`` for a work unit, ``a<n>`` for one of that
    unit's superseded attempts, and ``attempt:<n>`` / ``evaluation:<n>``
    for a proposal attempt or a gate evaluation (their log order, which is
    append-only and therefore stable). A stage's measurement bands are
    ``band:<band_key>``, each draw under one keyed
    ``<generation_id>/<entry_id>/r<replicate>``.

    Degrades to the empty plan — same shape, a ``note`` saying why — for an
    absent, unknown, or path-unsafe epoch and for any failure underneath:
    no reader here raises.
    """
    return build_execution_plan_model(paths, epoch_id).payload()


def build_execution_plan_model(paths: WorkspacePaths, epoch_id: str | None = None) -> ExecutionPlan:
    """Build the typed model used by both execution-plan endpoints.

    This is the shared reader seam. It keeps the public endpoint's
    best-effort guarantee and always returns a model, including for an absent
    or unreadable epoch.
    """
    try:
        resolved = _resolve_epoch_id(paths, epoch_id)
    except ValueError:
        return _empty_plan_model(epoch_id, "unknown epoch")
    if resolved is None:
        return _empty_plan_model(None, "no epoch")
    try:
        return _build(paths, resolved)
    except Exception:  # noqa: BLE001 — the endpoint never returns a 500
        return _empty_plan_model(resolved, "epoch could not be read")


def _build(paths: WorkspacePaths, epoch_id: str) -> ExecutionPlan:
    digest, board_entry_ids = _board_facts(paths, epoch_id)
    indices = round_indices(layout_of(paths), epoch_id)

    # Which candidates each round evaluated, from the round log alone. A
    # generation is claimed by the FIRST round that names it, so a champion
    # carried through later rounds keeps one home in the tree and its units
    # are counted once.
    logs = {index: _read_round_events(paths, epoch_id, index) for index in indices}
    claimed: dict[str, int] = {}
    per_round: dict[int, list[str]] = {}
    for index in indices:
        events, _ = logs[index]
        named: list[str] = []
        for event in events:
            payload = event.event
            generation_id = ""
            if isinstance(payload, PatchesApplied | HarnessLoaded):
                generation_id = payload.generation_id
            if generation_id and generation_id not in named:
                named.append(generation_id)
        per_round[index] = [gid for gid in named if claimed.setdefault(gid, index) == index]

    on_disk = recorded_generation_ids(layout_of(paths), epoch_id)

    # A leftover candidate-screen snapshot is not a baseline champion: it is
    # an ephemeral tree whose name states the round it served, and whose only
    # files are screen-band draws. It gets no board sweep (it never ran the
    # board) and its bands hang under the round it names, when that round is
    # on disk; otherwise it falls back to the baseline like any other
    # generation no round claims.
    screen_home = _screen_generation_rounds(on_disk, claimed, indices)
    baseline_ids = [gid for gid in on_disk if gid not in claimed and gid not in screen_home]
    screened_by_round: dict[int, list[str]] = {}
    for generation_id, round_index in screen_home.items():
        screened_by_round.setdefault(round_index, []).append(generation_id)
    stated = frozenset(screen_home)

    lineage = {
        str(node.get("generation_id")): node
        for node in build_lineage_view(paths, epoch_id, include_ratings=False).get(
            "generations", []
        )
        if isinstance(node, dict)
    }
    experiments = _experiment_outcomes(paths, epoch_id, on_disk)

    stages = [_baseline_stage(paths, epoch_id, baseline_ids, board_entry_ids)]
    for index in indices:
        evaluated = per_round.get(index, [])
        stages.append(
            _round_stage(
                paths,
                epoch_id,
                index,
                logs[index],
                board_entry_ids,
                lineage,
                experiments,
                evaluated,
                evaluated + screened_by_round.get(index, []),
                stated,
            )
        )
    stages.extend(_planned_rounds(paths, epoch_id, indices))

    return ExecutionPlan(
        epoch_id=epoch_id,
        generated_at=_iso(_utc_now()),
        board_digest=digest,
        board_entry_count=len(board_entry_ids),
        stages=tuple(stages),
    )


def _screen_generation_rounds(
    generation_ids: list[str], claimed: dict[str, int], indices: list[int]
) -> dict[str, int]:
    """Each leftover screen snapshot mapped to the round its NAME states.

    Only ids no round log claims and whose stated round has a stage are
    mapped: a screen id naming a round that is not on disk has no home to
    move to, so it stays with the generations the baseline collects and its
    bands read ``partial`` like every other unattributed one.
    """
    from zicato.epoch.screen import screen_generation_round  # noqa: PLC0415

    homes: dict[str, int] = {}
    for generation_id in generation_ids:
        if generation_id in claimed:
            continue
        index = screen_generation_round(generation_id)
        if index is not None and index in indices:
            homes[generation_id] = index
    return homes


def _experiment_outcomes(
    paths: WorkspacePaths, epoch_id: str, generation_ids: list[str]
) -> dict[str, dict[str, Any]]:
    """Each generation's recorded experiment outcome (journal detail)."""
    layout = layout_of(paths)
    out: dict[str, dict[str, Any]] = {}
    for generation_id in generation_ids:
        record = _read_json_value(layout.experiment(epoch_id, generation_id))
        if not isinstance(record, dict):
            continue
        outcome = record.get("outcome")
        if isinstance(outcome, dict):
            out[generation_id] = outcome
    return out


__all__ = [
    "PROVENANCE_EXACT",
    "PROVENANCE_INFERRED",
    "PROVENANCE_PARTIAL",
    "ROUND_STEPS",
    "STATUS_DONE",
    "STATUS_FAILED",
    "STATUS_PLANNED",
    "STATUS_RUNNING",
    "STATUS_SKIPPED",
    "ExecutionPlan",
    "PlanNode",
    "build_execution_plan",
    "build_execution_plan_model",
]
