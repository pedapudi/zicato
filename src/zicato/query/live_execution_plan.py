"""live_execution_plan — the running epoch's plan, and what is executing in it.

:mod:`zicato.query.execution_plan` serves what an epoch's loop DID: a tree
read entirely off settled files. This module serves the same tree for the
epoch a loop is running right now, with a LIVE OVERLAY on top:

* the served liveness verdict (:func:`zicato.query.runtime_view.derive_liveness`),
* the ACTIVE PATH — the chain of nodes the loop is currently inside, and
* the IN-FLIGHT work units — one node per ``active_runs`` record still
  beating, placed at the position its coordinates name.

All three are decided SERVER-side. A client that joined the plan to the
heartbeat and the active-runs list by itself would have to decode the phase
vocabulary and age the run records. It cannot do the second: a browser is
never the worker's host, so it cannot run the host-locality gate. The two
surfaces would then disagree about what is running.

Nothing here re-derives liveness or the pipeline position
--------------------------------------------------------
The phase string is decoded in exactly one place —
:func:`zicato.query.loop_view.build_round_pipeline`, which the round-pipeline
stepper already renders — and this module PROJECTS that verdict onto plan
nodes. So the stepper cannot say "gate" while the plan marks Run active.
The overlay gates on the same served liveness verdict the stepper gates on.
A workspace whose files froze in June still holds a mid-round phase and
seven ``active_runs`` records. It must serve its durable plan with an EMPTY
overlay rather than a tree full of nodes reading "running".

One id grammar, two tenses
--------------------------
Durable nodes keep their ids. An in-flight unit uses ``run:<run_id>`` because
the record has no replicate index; the durable ``r<replicate>`` node replaces
it when the draw lands. A record is placed only in a sweep the durable plan
already contains. Everything else lands in the explicit run-scope group.

Best-effort throughout: every input may be missing or torn, and each
failure narrows the overlay rather than raising.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from typing import Any

from zicato.epoch.preflight import PREFLIGHT_PHASE_TOKEN
from zicato.query.execution_plan import (
    PROVENANCE_PARTIAL,
    STATUS_PLANNED,
    STATUS_RUNNING,
    ExecutionPlan,
    PlanNode,
    _empty_plan_model,
    _ts_ms,
    build_execution_plan_model,
)
from zicato.query.loop_view import build_round_pipeline
from zicato.query.paths import WorkspacePaths, list_epoch_ids
from zicato.query.runtime_view import (
    LIVENESS_LIVE,
    LIVENESS_SETTLED,
    derive_liveness,
    read_active_runs_view,
)
from zicato.tournament.calibration import CALIBRATION_PHASE_TOKEN

#: Which measurement band each epoch-open step's draws land in. An epoch-open
#: step runs once per epoch, ahead of the round's propose → apply → run → gate
#: (:func:`zicato.query.loop_view._epoch_open_step`), so its live position is
#: the band step holding the draws it is taking rather than a round step. A
#: step with no entry here marks nothing: the plan will not point at a node
#: whose correspondence to the phase is a guess.
#:
#: The two sides are written out rather than fused. A phase token names a
#: stretch of the loop (``zicato.epoch.preflight`` /
#: ``zicato.tournament.calibration``) and a band key names a replicate range
#: (:func:`zicato.query.replicate_scores.measurement_bands`); they are owned by
#: different modules and equal only by coincidence. Deriving one from the
#: other would turn a future rename into a step silently marking the wrong
#: band, where an explicit table turns it into a step marking nothing.
EPOCH_OPEN_STEP_BANDS: dict[str, str] = {
    CALIBRATION_PHASE_TOKEN: "calibration",
    PREFLIGHT_PHASE_TOKEN: "contract_preflight",
}

#: The plan node kind an overlay attaches in-flight work to.
_SWEEP_KIND = "board_sweep"


def _plan_lookups(
    plan: ExecutionPlan,
) -> tuple[
    dict[int, PlanNode],
    dict[str, tuple[PlanNode, ...]],
    dict[str, PlanNode],
]:
    """Index round, measurement-band, and generation coordinates once.

    A band's entry is its full ANCESTRY: every node from the owning stage
    down to the band itself. That chain is what :func:`_active_path` emits.
    A chain naming only the stage and the band would skip whatever real
    parents sit between them, for any band nested deeper than one level.

    Both maps are first-wins: a coordinate the plan holds twice keeps the
    node the plan reached first, so the overlay's answer does not depend
    on walk order.
    """
    rounds: dict[int, PlanNode] = {}
    bands: dict[str, tuple[PlanNode, ...]] = {}
    sweeps: dict[str, PlanNode] = {}

    def visit(ancestry: tuple[PlanNode, ...], node: PlanNode) -> None:
        chain = (*ancestry, node)
        band = str(node.coordinates.get("band") or "")
        generation_id = str(node.coordinates.get("generation_id") or "")
        if node.kind == "measurement_band" and band:
            bands.setdefault(band, chain)
        if node.kind == _SWEEP_KIND and generation_id:
            sweeps.setdefault(generation_id, node)
        for child in node.children:
            visit(chain, child)

    for stage in plan.stages:
        index = stage.coordinates.get("round_index")
        if stage.kind == "round" and isinstance(index, int) and not isinstance(index, bool):
            rounds.setdefault(index, stage)
        visit((), stage)
    return rounds, bands, sweeps


@dataclass(frozen=True, slots=True)
class RuntimePlanOverlay:
    """Present-tense additions and annotations for an immutable plan."""

    liveness: dict[str, Any]
    summary: dict[str, Any]
    children_by_parent: dict[str, tuple[PlanNode, ...]] = field(default_factory=dict)
    extra_stages: tuple[PlanNode, ...] = ()
    active_ids: frozenset[str] = frozenset()
    running_ids: frozenset[str] = frozenset()

    def payload(self, plan: ExecutionPlan) -> dict[str, Any]:
        """Render a live response without changing the durable model."""

        def render(node: PlanNode) -> dict[str, Any]:
            payload, _, _ = self._render_node(node)
            return payload

        payload = plan.payload(render)
        payload["liveness"] = dict(self.liveness)
        payload["overlay"] = dict(self.summary)
        payload["stages"].extend(render(stage) for stage in self.extra_stages)
        return payload

    def _render_node(self, node: PlanNode) -> tuple[dict[str, Any], bool, bool]:
        children = node.children + self.children_by_parent.get(node.id, ())
        rendered: list[dict[str, Any]] = []
        active = node.id in self.active_ids
        running = node.id in self.running_ids
        for child in children:
            child_payload, child_active, child_running = self._render_node(child)
            rendered.append(child_payload)
            active = active or child_active
            running = running or child_running
        status = STATUS_RUNNING if running and node.status == STATUS_PLANNED else node.status
        return node.payload(status=status, children=rendered, active=active), active, running


def build_live_execution_plan(paths: WorkspacePaths) -> dict[str, Any]:
    """``GET /api/live/execution-plan`` — the running epoch's plan and overlay.

    Returns the durable plan's shape
    (:func:`~zicato.query.execution_plan.build_execution_plan`) with three
    additions::

        {
          ...the durable plan...,
          "liveness": {"state": "live"|"settled"|"interrupted", ...},
          "overlay": {
            "in_flight", "placed", "unplaced", "other_epoch",
            "active_path": [<node id>, ...],
            "phase", "round_index", "note"
          },
          "stages": [<node with an extra "active" flag>, ...]
        }

    ``active`` is true on a node the loop is inside: one on the active path,
    an in-flight unit, or an ancestor of either. ``active_path`` names the
    phase-derived chain in order (outermost first); the in-flight units are
    a set rather than a chain, so they are marked in the tree and counted in the
    overlay instead.

    ``overlay.in_flight`` is the same tally ``/api/live/pipeline`` reports,
    partitioned by what the plan could do with each record: ``placed`` into
    a sweep, ``unplaced`` into the run-scope stage, or ``other_epoch``
    (running against an epoch this plan does not describe, so absent from
    the tree). Records on disk whose workers are gone are in none of the
    four — the tally counts what beats, never what a dead run left behind.

    A workspace that is not LIVE serves its durable plan with an empty
    overlay and no node marked active. Degrades to the empty plan shape
    with the same keys on any failure; no reader here raises.
    """
    try:
        return _build_live(paths)
    except Exception:  # noqa: BLE001 — the endpoint never returns a 500
        return _degraded(paths, "live plan could not be read")


def _build_live(paths: WorkspacePaths) -> dict[str, Any]:
    pipeline = build_round_pipeline(paths)
    raw_liveness = pipeline.get("liveness")
    liveness = dict(raw_liveness) if isinstance(raw_liveness, dict) else {"state": LIVENESS_SETTLED}
    plan = build_execution_plan_model(paths, _live_epoch_id(paths, pipeline))

    if liveness.get("state") != LIVENESS_LIVE:
        # The stepper's rule, verbatim: present-tense claims gate on the one
        # served verdict. A dead workspace's frozen runtime files describe
        # work that stopped, and reading them here would repopulate exactly
        # the "seven units running" the tri-state exists to stop.
        return RuntimePlanOverlay(
            liveness=liveness,
            summary=_overlay(pipeline, note="workspace is not live; overlay withheld"),
        ).payload(plan)

    now = _utc_now_of(plan)
    records = read_active_runs_view(paths, now=now)
    rounds, bands, sweeps = _plan_lookups(plan)
    placed, unplaced, other_epoch, children, running_ids = _place_in_flight(
        sweeps, plan.epoch_id, records
    )
    extra_stages = (_run_scope_stage(plan.epoch_id, unplaced),) if unplaced else ()
    active_path = _active_path(rounds, bands, pipeline)
    overlay = RuntimePlanOverlay(
        liveness=liveness,
        summary=_overlay(
            pipeline,
            active_path=active_path,
            placed=placed,
            unplaced=len(unplaced),
            other_epoch=other_epoch,
        ),
        children_by_parent=children,
        extra_stages=extra_stages,
        active_ids=running_ids.union(active_path),
        running_ids=running_ids,
    )
    return overlay.payload(plan)


def _utc_now_of(plan: ExecutionPlan) -> _dt.datetime:
    """The clock the overlay ages records against — the plan's own stamp.

    The plan already reports the moment it was built. Ageing the run records
    against that same instant is what keeps ``generated_at`` an honest
    description of the whole response instead of two reads seconds apart.
    """
    parsed = _ts_ms(plan.generated_at)
    if parsed is None:
        return _dt.datetime.now(_dt.UTC)
    return _dt.datetime.fromtimestamp(parsed / 1000, _dt.UTC)


def _live_epoch_id(paths: WorkspacePaths, pipeline: dict[str, Any]) -> str | None:
    """The epoch to serve: the heartbeat's, when the workspace still holds it.

    The same resolution the round pipeline reports (``epoch_id`` off the
    heartbeat). ``None`` — the workspace's current epoch — covers both the
    workspace with no heartbeat at all and the heartbeat naming an epoch
    that is not on disk; serving the current epoch's plan there is
    strictly more than the empty plan an unresolvable id would produce, and
    the overlay is gated on liveness either way.
    """
    named = str(pipeline.get("epoch_id") or "")
    if named and named in list_epoch_ids(paths):
        return named
    return None


# ---------------------------------------------------------------------------
# The active path — where the loop is, projected onto plan nodes
# ---------------------------------------------------------------------------


def _active_path(
    rounds: dict[int, PlanNode],
    bands: dict[str, tuple[PlanNode, ...]],
    pipeline: dict[str, Any],
) -> list[str]:
    """Project the pipeline phase onto node ids that the plan actually holds.

    Epoch-open work maps to its measurement band. Round work maps to the
    stated round and, when known, its pipeline step. No node is invented for
    an unknown phase or a round absent from the durable plan.
    """
    open_step = pipeline.get("epoch_open_step")
    if isinstance(open_step, dict):
        band = EPOCH_OPEN_STEP_BANDS.get(str(open_step.get("id") or ""))
        chain = bands.get(band or "")
        return [node.id for node in chain] if chain else []

    round_index = pipeline.get("round_index")
    stage = (
        rounds.get(round_index)
        if isinstance(round_index, int) and not isinstance(round_index, bool)
        else None
    )
    if stage is None:
        return []
    step = pipeline.get("active_step")
    child = next((node for node in stage.children if node.kind == f"{step}_step"), None)
    return [stage.id] if child is None else [stage.id, child.id]


# ---------------------------------------------------------------------------
# In-flight work units
# ---------------------------------------------------------------------------


def _place_in_flight(
    sweeps: dict[str, PlanNode], epoch_id: str | None, records: list[dict[str, Any]]
) -> tuple[
    int,
    tuple[PlanNode, ...],
    int,
    dict[str, tuple[PlanNode, ...]],
    frozenset[str],
]:
    """Place records whose shared runtime verdict says they are still fresh."""
    placed = 0
    unplaced: list[PlanNode] = []
    other_epoch = 0
    children: dict[str, list[PlanNode]] = {}
    running_ids: set[str] = set()
    for record in records:
        if not record.get("fresh"):
            continue
        named = str(record.get("epoch_id") or "")
        if named and epoch_id and named != epoch_id:
            # Work against an epoch this plan does not describe. It is
            # running, and the tally says so, but there is no position for it
            # in this tree and resemblance is not a position.
            other_epoch += 1
            continue
        sweep = sweeps.get(str(record.get("generation_id") or ""))
        if sweep is None:
            unplaced.append(_unplaced_node(epoch_id, record))
            continue
        node = _running_node(f"{sweep.id}/{record.get('entry_id') or ''}", record)
        children.setdefault(sweep.id, []).append(node)
        running_ids.add(node.id)
        placed += 1
    running_ids.update(node.id for node in unplaced)
    return (
        placed,
        tuple(unplaced),
        other_epoch,
        {parent: tuple(nodes) for parent, nodes in children.items()},
        frozenset(running_ids),
    )


def _running_node(prefix: str, record: dict[str, Any]) -> PlanNode:
    """One board entry executing right now, as a plan node.

    Keyed ``run:<run_id>`` rather than ``r<replicate>``: the record carries
    no replicate index, so the slot this execution will score into is not
    yet known and must not be claimed. That is also why the node reads
    ``partial`` — everything it reports is read from the record, and the
    one coordinate it cannot fill says so by being ``None``.
    """
    run_id = str(record.get("run_id") or "")
    entry_id = str(record.get("entry_id") or "")
    return PlanNode(
        id=f"{prefix}/run:{run_id}",
        kind="board_entry_run",
        label=f"{entry_id} · in flight",
        purpose="One board entry executing against this candidate right now.",
        status=STATUS_RUNNING,
        provenance=PROVENANCE_PARTIAL,
        started_at=_ts_ms(record.get("started_at")),
        coordinates={
            "epoch_id": str(record.get("epoch_id") or ""),
            "generation_id": str(record.get("generation_id") or ""),
            "entry_id": entry_id,
            "replicate": None,
            "match_id": "",
        },
        outcome={
            "run_id": run_id,
            "elapsed_seconds": record.get("elapsed_seconds"),
            "budget_seconds": record.get("budget_seconds"),
            "deadline_fraction": record.get("progress"),
            "note": "still executing; its replicate slot is recorded when the draw lands",
        },
    )


def _run_scope_id(epoch_id: str | None) -> str:
    """The run-scope stage's id — epoch-scoped when there is an epoch to name.

    The grammar omits a level it cannot fill rather than spelling a missing
    epoch into the id, which is what an id reading ``e:None`` would do.
    """
    return f"e:{epoch_id}/run-scope" if epoch_id else "run-scope"


def _unplaced_node(epoch_id: str | None, record: dict[str, Any]) -> PlanNode:
    """A running unit the plan has no position for, keyed under run scope."""
    generation_id = str(record.get("generation_id") or "")
    entry_id = str(record.get("entry_id") or "")
    return _running_node(f"{_run_scope_id(epoch_id)}/{generation_id}/{entry_id}", record)


def _run_scope_stage(epoch_id: str | None, children: tuple[PlanNode, ...]) -> PlanNode:
    """Hold in-flight work under an explicit group instead of guessing a round."""
    return PlanNode(
        id=_run_scope_id(epoch_id),
        kind="run_scope",
        label="In flight — position unknown",
        purpose=(
            "Executions running now whose recorded coordinates name no position in "
            "this plan. They are shown here rather than placed under a round the "
            "plan cannot confirm ran them."
        ),
        status=STATUS_RUNNING,
        provenance=PROVENANCE_PARTIAL,
        coordinates={"epoch_id": epoch_id},
        outcome={"unit_count": len(children)},
        children=children,
    )


# ---------------------------------------------------------------------------
# The overlay block and the degrade
# ---------------------------------------------------------------------------


def _overlay(
    pipeline: dict[str, Any],
    *,
    active_path: list[str] | None = None,
    placed: int = 0,
    unplaced: int = 0,
    other_epoch: int = 0,
    note: str = "",
) -> dict[str, Any]:
    """The overlay's wire block — every key always present.

    ``in_flight`` is always the round pipeline's own tally, including on the
    withheld path: the two live surfaces must never report different counts
    of the same records. What withholding changes is ``placed`` /
    ``unplaced`` — nothing was put in the tree — and ``note`` says why.
    """
    in_flight = pipeline.get("in_flight")
    return {
        "in_flight": in_flight if isinstance(in_flight, int) else 0,
        "placed": placed,
        "unplaced": unplaced,
        "other_epoch": other_epoch,
        "active_path": list(active_path or []),
        "phase": pipeline.get("phase"),
        "round_index": pipeline.get("round_index"),
        "note": note,
    }


def _degraded(paths: WorkspacePaths, note: str) -> dict[str, Any]:
    """The response shape with nothing in it — the ONE degrade shape.

    The empty plan comes from the durable builder's own
    :func:`~zicato.query.execution_plan._empty_plan_model`, so the live
    degrade and the durable degrade cannot render different empties.
    """
    plan = _empty_plan_model(None, note)
    return RuntimePlanOverlay(
        liveness=_safe_liveness(paths),
        summary=_overlay({}, note=note),
    ).payload(plan)


def _safe_liveness(paths: WorkspacePaths) -> dict[str, Any]:
    """The served verdict, or ``settled`` when even that read failed.

    A reader that cannot see a pulse must not report one, and ``settled``
    is the verdict that already covers "nothing is running here", including
    the workspace that never ran. The overlay is empty on this path anyway.
    """
    try:
        return dict(derive_liveness(paths))
    except Exception:  # noqa: BLE001 — best-effort, mirrors the sibling readers
        return {"state": LIVENESS_SETTLED}


__all__ = ["EPOCH_OPEN_STEP_BANDS", "build_live_execution_plan"]
