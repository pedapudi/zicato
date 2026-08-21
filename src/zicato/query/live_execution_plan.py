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
vocabulary and age the run records, and a browser cannot run the second of
the two in-flight gates at all (it is never the worker's host), so the two
surfaces would disagree about what is running.

Nothing here re-derives liveness or the pipeline position
--------------------------------------------------------
The phase string is decoded in exactly one place —
:func:`zicato.query.loop_view.build_round_pipeline`, which the round-pipeline
stepper already renders — and this module PROJECTS that verdict onto plan
nodes. So the stepper cannot say "gate" while the plan marks Run active.
The overlay gates on the same served liveness verdict the stepper gates on:
a workspace whose files froze in June still holds a mid-round phase and
seven ``active_runs`` records, and it must serve its durable plan with an
EMPTY overlay rather than a tree full of nodes reading "running".

One id grammar, two tenses
--------------------------
Every node keeps the durable plan's id
(``e:<epoch>[/round:<n>|/baseline][/<step>][/<key>...]``), so a client can
diff the live tree against a durable one it already holds and keep a node
open across the switch. An in-flight unit is keyed ``run:<run_id>`` under
the sweep it belongs to rather than ``r<replicate>``: an ``ActiveRun``
records no replicate index, and inventing one would put a node at the id a
different draw will later occupy. When the draw lands, the durable
``r<replicate>`` node appears and the ``run:<run_id>`` node is gone — the
diff a client sees is exactly that swap.

A record is placed only where the plan already put its candidate. That is
enough on its own: the durable sweep is drawn from the round log, not from
the loss files, so a candidate exists in the tree before its first draw
lands. A record whose generation has no sweep is therefore one no round says
it applied, and it is NEVER placed by resemblance — it lands in the
run-scope group, an explicit stage saying the work is running and the plan
cannot say where.

Depth
-----
No ``depth`` parameter is built. Measured against the largest epoch
available (``2026-06-07_e4``, 56 loss files): the whole served payload is
37.7 KB, well under the 200 KB at which paging the tree would start to pay
for its own complexity. The payload is close to linear in executed draws —
that epoch's 64 nodes cost ~0.59 KB each — so the number to re-measure
before revisiting this is executed draws per epoch, not rounds.

Best-effort throughout (DQ3): every input may be missing or torn, and each
failure narrows the overlay rather than raising.
"""

from __future__ import annotations

import datetime as _dt
from collections.abc import Callable
from typing import Any

from zicato.epoch.preflight import PREFLIGHT_PHASE_TOKEN
from zicato.query.execution_plan import (
    PROVENANCE_PARTIAL,
    STATUS_PLANNED,
    STATUS_RUNNING,
    _empty_plan,
    _ts_ms,
    build_execution_plan,
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
#: the band step holding the draws it is taking, not a round step. A step with
#: no entry here marks nothing: the plan will not point at a node whose
#: correspondence to the phase is a guess.
#:
#: The two sides are written out rather than fused. A phase token names a
#: stretch of the loop (``zicato.epoch.preflight`` /
#: ``zicato.tournament.calibration``) and a band key names a replicate range
#: (:func:`zicato.query.replicate_scores.measurement_bands`); they are owned by
#: different modules and equal only by coincidence today. Deriving one from the
#: other would turn a future rename into a step silently marking the wrong
#: band, where an explicit table turns it into a step marking nothing.
EPOCH_OPEN_STEP_BANDS: dict[str, str] = {
    CALIBRATION_PHASE_TOKEN: "calibration",
    PREFLIGHT_PHASE_TOKEN: "contract_preflight",
}

#: The plan node kind an overlay attaches in-flight work to.
_SWEEP_KIND = "board_sweep"


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
    a set, not a chain, so they are marked in the tree and counted in the
    overlay instead.

    ``overlay.in_flight`` is the same tally ``/api/live/pipeline`` reports,
    partitioned by what the plan could do with each record: ``placed`` into
    a sweep, ``unplaced`` into the run-scope stage, or ``other_epoch``
    (running against an epoch this plan does not describe, so absent from
    the tree). Records on disk whose workers are gone are in none of the
    four — the tally counts what beats, never what a dead run left behind.

    A workspace that is not LIVE serves its durable plan with an empty
    overlay and no node marked active. Degrades to the empty plan shape
    with the same keys on any failure (DQ3) — no reader here raises.
    """
    try:
        return _build_live(paths)
    except Exception:  # noqa: BLE001 — DQ3: the endpoint never returns a 500
        return _degraded(paths, "live plan could not be read")


def _build_live(paths: WorkspacePaths) -> dict[str, Any]:
    pipeline = build_round_pipeline(paths)
    liveness = pipeline.get("liveness") or {"state": LIVENESS_SETTLED}
    plan = build_execution_plan(paths, _live_epoch_id(paths, pipeline))
    plan["liveness"] = liveness
    stages: list[dict[str, Any]] = plan["stages"]

    if liveness.get("state") != LIVENESS_LIVE:
        # The stepper's rule, verbatim: present-tense claims gate on the one
        # served verdict. A dead workspace's frozen runtime files describe
        # work that stopped, and reading them here would repopulate exactly
        # the "seven units running" the tri-state exists to stop.
        _stamp_active(stages, set())
        plan["overlay"] = _overlay(pipeline, note="workspace is not live; overlay withheld")
        return plan

    now = _utc_now_of(plan)
    records = read_active_runs_view(paths, now=now)
    placed, unplaced, other, live_ids = _attach_in_flight(stages, plan["epoch_id"], records)
    if unplaced:
        stages.append(_run_scope_stage(plan["epoch_id"], unplaced))
        live_ids.update(node["id"] for node in unplaced)

    _promote_planned(stages, live_ids)
    active_path = _active_path(stages, pipeline)
    _stamp_active(stages, live_ids.union(active_path))
    plan["overlay"] = _overlay(
        pipeline,
        active_path=active_path,
        placed=placed,
        unplaced=len(unplaced),
        other_epoch=other,
    )
    return plan


def _utc_now_of(plan: dict[str, Any]) -> _dt.datetime:
    """The clock the overlay ages records against — the plan's own stamp.

    The plan already reports the moment it was built. Ageing the run records
    against that same instant is what keeps ``generated_at`` an honest
    description of the whole response instead of two reads seconds apart.
    """
    parsed = _ts_ms(plan.get("generated_at"))
    if parsed is None:
        return _dt.datetime.now(_dt.UTC)
    return _dt.datetime.fromtimestamp(parsed / 1000, _dt.UTC)


def _live_epoch_id(paths: WorkspacePaths, pipeline: dict[str, Any]) -> str | None:
    """The epoch to serve: the heartbeat's, when the workspace still holds it.

    The same resolution the round pipeline reports (``epoch_id`` off the
    heartbeat). ``None`` — the workspace's current epoch — covers both the
    workspace with no heartbeat at all and the heartbeat naming an epoch
    that is no longer on disk; serving the current epoch's plan there is
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


def _active_path(stages: list[dict[str, Any]], pipeline: dict[str, Any]) -> list[str]:
    """The chain of node ids the heartbeat's phase points at, outermost first.

    Two shapes, because the loop has two:

    * an EPOCH-OPEN step (the A/A noise-floor calibration) runs ahead of any
      round, so the chain is the stage holding its band step, then the band;
    * otherwise the chain is the round the heartbeat names, plus the
      pipeline step it named — ``propose`` / ``apply`` / ``run`` / ``gate``,
      the same four the stepper renders.

    Each link is only added when the node EXISTS. A phase the pipeline could
    not place in a step (``evolve_n_rounds:start``, an infra backoff, a
    vocabulary this build has never seen) contributes no step, and a round
    the plan has no stage for contributes nothing at all — an active path
    must point at nodes the reader can open, never at a plausible id.
    """
    open_step = pipeline.get("epoch_open_step")
    if isinstance(open_step, dict):
        band = EPOCH_OPEN_STEP_BANDS.get(str(open_step.get("id") or ""))
        return _band_path(stages, band) if band else []

    stage = _round_stage(stages, pipeline.get("round_index"))
    if stage is None:
        return []
    step = pipeline.get("active_step")
    child = _find(stage["children"], lambda n: n["kind"] == f"{step}_step") if step else None
    return [stage["id"]] if child is None else [stage["id"], child["id"]]


def _round_stage(stages: list[dict[str, Any]], index: Any) -> dict[str, Any] | None:
    """The round stage at ``index``, or ``None`` when the plan has no such round."""
    if not isinstance(index, int) or isinstance(index, bool):
        return None
    return _find(
        stages,
        lambda n: n["kind"] == "round" and n["coordinates"].get("round_index") == index,
    )


def _band_path(stages: list[dict[str, Any]], band_key: str) -> list[str]:
    """``[stage id, band step id]`` for the measurement band ``band_key``.

    A band step hangs under the stage that owns the generation its draws sit
    under, which for a pre-loop measurement is the baseline. Only a band
    with draws already on disk has a node, so a step marks nothing during
    its first draw — the plan reports executions it has read, and this path
    points into the plan rather than around it.
    """

    def is_band(node: dict[str, Any]) -> bool:
        return bool(
            node["kind"] == "measurement_band" and node["coordinates"].get("band") == band_key
        )

    for stage in stages:
        step = _find(stage["children"], is_band)
        if step is not None:
            return [stage["id"], step["id"]]
    return []


def _find(
    nodes: list[dict[str, Any]], match: Callable[[dict[str, Any]], bool]
) -> dict[str, Any] | None:
    """The first node satisfying ``match``, or ``None``."""
    for node in nodes:
        if match(node):
            return node
    return None


def _find_deep(
    nodes: list[dict[str, Any]], match: Callable[[dict[str, Any]], bool]
) -> dict[str, Any] | None:
    """The first node satisfying ``match`` anywhere in the tree, or ``None``.

    A board sweep sits under a round's Run step or directly under the
    baseline stage, so the candidate's one home is found by depth, not by a
    fixed level.
    """
    for node in nodes:
        if match(node):
            return node
        found = _find_deep(node["children"], match)
        if found is not None:
            return found
    return None


def _promote_planned(nodes: list[dict[str, Any]], live_ids: set[str]) -> bool:
    """Rewrite ``planned`` to ``running`` above in-flight work; return whether
    this level holds any.

    A sweep whose only draws are still executing has no settled loss file, so
    the durable builder reads it as ``planned`` — correct about the files and
    wrong about the world once a live unit hangs under it. Only ``planned``
    is rewritten: ``done`` / ``failed`` / ``skipped`` are statements about
    work that already happened, and a re-run underneath does not unmake them.
    """
    holds = False
    for node in nodes:
        below = _promote_planned(node["children"], live_ids)
        holds_here = below or node["id"] in live_ids
        if holds_here and node["status"] == STATUS_PLANNED:
            node["status"] = STATUS_RUNNING
        holds = holds or holds_here
    return holds


def _stamp_active(nodes: list[dict[str, Any]], marked: set[str]) -> bool:
    """Stamp ``active`` on every node, bottom-up; return whether any is active.

    A node is active when it is itself marked or anything beneath it is, so
    a reader who has not opened a branch still knows the loop is somewhere
    inside it. Every node carries the key whether or not it is active — a
    field that appears only when true reads as absent data on the nodes that
    need it least ambiguously.
    """
    any_active = False
    for node in nodes:
        below = _stamp_active(node["children"], marked)
        node["active"] = node["id"] in marked or below
        any_active = any_active or node["active"]
    return any_active


# ---------------------------------------------------------------------------
# In-flight work units
# ---------------------------------------------------------------------------


def _attach_in_flight(
    stages: list[dict[str, Any]], epoch_id: str | None, records: list[dict[str, Any]]
) -> tuple[int, list[dict[str, Any]], int, set[str]]:
    """Place every still-beating record; return ``(placed, unplaced, other, ids)``.

    A record is in flight iff the server's own per-record verdict says so
    (``fresh``, stamped by :func:`~zicato.query.runtime_view.read_active_runs_view`
    from the two gates :func:`~zicato.query.runtime_view.fresh_run_count`
    tallies). Reading the timestamps again here would be a second, divergent
    definition of the same question.
    """
    placed = 0
    unplaced: list[dict[str, Any]] = []
    other = 0
    ids: set[str] = set()
    for record in records:
        if not record.get("fresh"):
            continue
        named = str(record.get("epoch_id") or "")
        if named and epoch_id and named != epoch_id:
            # Work against an epoch this plan does not describe. It is
            # running, and the tally says so, but there is no position for it
            # in this tree and resemblance is not a position.
            other += 1
            continue
        sweep = _sweep_for(stages, str(record.get("generation_id") or ""))
        if sweep is None:
            unplaced.append(_unplaced_node(epoch_id, record))
            continue
        node = _running_node(f"{sweep['id']}/{record.get('entry_id') or ''}", record)
        sweep["children"].append(node)
        ids.add(node["id"])
        placed += 1
    return placed, unplaced, other, ids


def _sweep_for(stages: list[dict[str, Any]], generation_id: str) -> dict[str, Any] | None:
    """The board sweep a running unit belongs under, or ``None``.

    The durable plan already gives every candidate any round applied exactly
    one sweep — the round that first named it owns all of its units, and the
    node is drawn from the round log, so it is there before any draw has
    landed (an empty sweep in an open round reads ``planned``). Following
    that placement is the plan's own rule rather than a resemblance, and it
    is why nothing here ever invents a sweep: a generation with no sweep is
    a generation no round says it applied.

    ``None`` for exactly that case, and the caller puts the unit in run
    scope — the honest answer: it is running, and the plan cannot say where.
    """
    if not generation_id:
        return None

    def is_sweep(node: dict[str, Any]) -> bool:
        return bool(
            node["kind"] == _SWEEP_KIND
            and node["coordinates"].get("generation_id") == generation_id
        )

    return _find_deep(stages, is_sweep)


def _running_node(prefix: str, record: dict[str, Any]) -> dict[str, Any]:
    """One board entry executing right now, as a plan node.

    Keyed ``run:<run_id>`` rather than ``r<replicate>``: the record carries
    no replicate index, so the slot this execution will score into is not
    yet known and must not be claimed. That is also why the node reads
    ``partial`` — everything it reports is read from the record, and the
    one coordinate it cannot fill says so by being ``None``.
    """
    run_id = str(record.get("run_id") or "")
    entry_id = str(record.get("entry_id") or "")
    return {
        "id": f"{prefix}/run:{run_id}",
        "kind": "board_entry_run",
        "label": f"{entry_id} · in flight",
        "purpose": "One board entry executing against this candidate right now.",
        "status": STATUS_RUNNING,
        "provenance": PROVENANCE_PARTIAL,
        "started_at": _ts_ms(record.get("started_at")),
        "ended_at": None,
        "duration_ms": None,
        "progress": None,
        "coordinates": {
            "epoch_id": str(record.get("epoch_id") or ""),
            "generation_id": str(record.get("generation_id") or ""),
            "entry_id": entry_id,
            "replicate": None,
            "match_id": "",
        },
        "outcome": {
            "run_id": run_id,
            "elapsed_seconds": record.get("elapsed_seconds"),
            "budget_seconds": record.get("budget_seconds"),
            "deadline_fraction": record.get("progress"),
            "note": "still executing; its replicate slot is recorded when the draw lands",
        },
        "children": [],
    }


def _run_scope_id(epoch_id: str | None) -> str:
    """The run-scope stage's id — epoch-scoped when there is an epoch to name.

    The grammar omits a level it cannot fill rather than spelling a missing
    epoch into the id, which is what an id reading ``e:None`` would do.
    """
    return f"e:{epoch_id}/run-scope" if epoch_id else "run-scope"


def _unplaced_node(epoch_id: str | None, record: dict[str, Any]) -> dict[str, Any]:
    """A running unit the plan has no position for, keyed under run scope."""
    generation_id = str(record.get("generation_id") or "")
    entry_id = str(record.get("entry_id") or "")
    return _running_node(f"{_run_scope_id(epoch_id)}/{generation_id}/{entry_id}", record)


def _run_scope_stage(epoch_id: str | None, children: list[dict[str, Any]]) -> dict[str, Any]:
    """The stage holding in-flight work the plan cannot place.

    An explicit group, never a guess into a round. A record reaches it when
    its generation names no candidate the rounds evaluated, or when the
    epoch has no round stage to hang a sweep from — a run started before its
    round wrote anything, or one left by a loop whose rounds were pruned.
    Saying that plainly is the whole point: the alternative is a unit
    rendered under a round that never ran it.
    """
    return {
        "id": _run_scope_id(epoch_id),
        "kind": "run_scope",
        "label": "In flight — position unknown",
        "purpose": (
            "Executions running now whose recorded coordinates name no position in "
            "this plan. They are shown here rather than placed under a round the "
            "plan cannot confirm ran them."
        ),
        "status": STATUS_RUNNING,
        "provenance": PROVENANCE_PARTIAL,
        "started_at": None,
        "ended_at": None,
        "duration_ms": None,
        "progress": None,
        "coordinates": {"epoch_id": epoch_id},
        "outcome": {"unit_count": len(children)},
        "children": children,
    }


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
    """The response shape with nothing in it — the ONE degrade shape (DQ3)."""
    plan = _empty_plan(None, note)
    plan["liveness"] = _safe_liveness(paths)
    plan["overlay"] = _overlay({}, note=note)
    return plan


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
