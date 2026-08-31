"""loop_view — loop-communication reads for the dashboard.

Server-side projections of the optimisation-loop analytics the UI's
loop-communication surfaces render:

* :func:`build_optimization_trajectory` — the promoted-lineage scalar
  trajectory + promotion rate + an UNCERTAINTY-HONEST verdict. Wraps
  :func:`zicato.tournament.detail.optimization_trajectory` and joins the
  epoch's measured A/A ``noise_floor`` (an additive ``EpochConfig``
  field, ``epochs/<id>/config.json``): a "plateaued" flag whose recent
  scalar movement sits BELOW the measured floor is reported as
  ``no_signal`` — the loop cannot distinguish that movement from a
  re-roll of the same generation, so claiming "plateaued" (or
  "improving") would overstate what was measured.
* :func:`build_tournament_cost` — wall-clock + run-count cost accounting
  (``cost_per_promotion_ms``). Wraps
  :func:`zicato.tournament.detail.tournament_cost`.

Both are best-effort like every sibling reader: a never-indexed
workspace (``IndexUnavailableError``) or any sqlite failure degrades to
an empty shape with a ``note`` — never a 500. The ``noise_floor`` block
is read straight off the epoch config (it is independent of the index),
so the floor still surfaces on a degraded read.
"""

from __future__ import annotations

import re
from typing import Any

from zicato.epoch.preflight import PREFLIGHT_PHASE_TOKEN
from zicato.query.paths import (
    WorkspacePaths,
    _iso,
    _read_json_value,
    _utc_now,
    coerce_float,
)
from zicato.tournament.calibration import CALIBRATION_PHASE_TOKEN


def _epoch_noise_floor(paths: WorkspacePaths, epoch_id: str) -> dict[str, Any] | None:
    """The epoch's measured A/A noise floor, or ``None`` when never measured.

    Reads the additive ``noise_floor`` field off
    ``epochs/<id>/config.json`` (the :func:`set_epoch_noise_floor` shape:
    ``{generation_id, epoch_id, runs, scalars, max_abs_delta, delta_std,
    measured_at}``). Best-effort: a missing / malformed config — or a
    floor whose ``max_abs_delta`` is not numeric — reads as ``None``.
    """
    cfg = _read_json_value(paths.epochs / epoch_id / "config.json")
    if not isinstance(cfg, dict):
        return None
    raw = cfg.get("noise_floor")
    if not isinstance(raw, dict):
        return None
    if not isinstance(raw.get("max_abs_delta"), int | float):
        return None
    return {
        "generation_id": str(raw.get("generation_id", "")),
        "runs": raw.get("runs") if isinstance(raw.get("runs"), int) else None,
        "max_abs_delta": float(raw["max_abs_delta"]),
        "delta_std": (coerce_float(raw.get("delta_std"))),
        "measured_at": str(raw.get("measured_at", "")),
    }


def _empty_trajectory(paths: WorkspacePaths, epoch_id: str, note: str) -> dict[str, Any]:
    return {
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
        "noise_floor": _epoch_noise_floor(paths, epoch_id),
        "note": note,
    }


def build_optimization_trajectory(paths: WorkspacePaths, epoch_id: str) -> dict[str, Any]:
    """Promoted-lineage trajectory + promotion rate + an honest verdict.

    ``GET /api/epoch/{id}/trajectory`` returns this. Fields:

    * ``points`` — ``[{generation_id, scalar, namespace_values}]`` along
      the winners spine (:func:`optimization_trajectory`).
    * ``promotion_rate`` / ``promoted_count`` / ``challenger_count``.
    * ``settled_count`` — challengers a tournament has DECIDED, which is
      ``challenger_count`` minus those still racing (see
      :class:`~zicato.tournament.detail.Trajectory`). Every "nothing
      promoted" reading below counts this one.
    * ``plateaued`` — the RAW detail-layer flag (no improvement across
      the trailing :data:`~zicato.tournament.detail.PLATEAU_WINDOW`).
    * ``plateau_measurable`` — whether that flag rests on enough spine to
      mean anything (see :class:`~zicato.tournament.detail.Trajectory`).
    * ``recent_movement`` — max − min of the trailing-window scalars
      (the largest movement the window actually showed), or ``None``
      with fewer than two resolved scalars.
    * ``noise_floor`` — the epoch's measured A/A floor (or ``None``).
    * ``verdict`` — the UNCERTAINTY-HONEST word the UI renders. The
      vocabulary, in ladder order:

      ``"no_signal"``
        Challengers SETTLED, none promoted, AND a floor was measured —
        every challenger tied inside the A/A spread, so there is no
        detectable signal (issue #84). Also the verdict for a measured
        plateau whose whole trailing movement fits inside the floor: the
        data cannot tell that from an A/A re-roll.
      ``"stalled"``
        Challengers SETTLED and none promoted, with NO floor measured.
        The loop is going nowhere; how far is unmeasured, so this makes
        no claim about noise (issue #129). Both this and ``no_signal`` count
        ``settled_count``, never ``challenger_count``: an in-flight
        challenger has decided nothing, and reading it as a stall would
        alarm on a run that has not finished its first round.
      ``"plateaued"``
        The promoted spine stopped improving, and the movement is
        resolvable above the floor (or no floor was measured).
      ``"improving"``
        The promoted spine actually advanced — at least two points on
        it — and has not plateaued. Requires a real advance: a
        one-node spine is never "improving".
      ``"warming_up"``
        Nothing has been decided yet, so the spine is the seed alone —
        whether the epoch has fielded no challenger at all or its first
        challengers are still racing. Too early to judge, and the verdict
        says so rather than guessing.

    Degrades to an empty shape (with the floor still attached) on a
    missing index / any sqlite failure — never raises.
    """
    from zicato.tournament.detail import (  # noqa: PLC0415
        PLATEAU_WINDOW,
        IndexUnavailableError,
        optimization_trajectory,
    )

    try:
        traj = optimization_trajectory(paths.index_db, epoch_id)
    except IndexUnavailableError:
        return _empty_trajectory(paths, epoch_id, "index not built; run zicato repair index")
    except Exception:  # noqa: BLE001 — best-effort, mirrors sibling readers
        return _empty_trajectory(paths, epoch_id, "index unreadable")

    points = [
        {
            "generation_id": p.generation_id,
            "scalar": p.scalar,
            "namespace_values": dict(p.namespace_values),
        }
        for p in traj.points
    ]

    # The trailing-window movement the plateau flag judged. ``None`` with
    # fewer than two resolved scalars (no movement is measurable at all).
    scalars = [p.scalar for p in traj.points if p.scalar is not None]
    window = scalars[-PLATEAU_WINDOW:]
    recent_movement = (max(window) - min(window)) if len(window) >= 2 else None

    floor = _epoch_noise_floor(paths, epoch_id)
    # Challengers were fielded and NONE promoted: the promoted spine is just
    # the seed, so it has improved nothing. A short spine reads
    # ``not plateaued`` only because it is too short to plateau (< the plateau
    # window), so it must not be reported as improving: that would turn "we
    # cannot tell" into the most reassuring word the UI can print (issue #129).
    #
    # The floor decides WHICH honest word applies rather than whether one
    # applies. With
    # a MEASURED floor the stall is the noise-floor-honest "no detectable
    # signal" (every challenger tied within the A/A spread; issue #84).
    # Without one, "stalled" reports the promotions that did not happen and
    # claims nothing about noise — fabricating no_signal here would assert a
    # measurement that was never taken.
    #
    # The denominator is SETTLED challengers rather than fielded ones. A challenger
    # that has applied its snapshot and is still racing already holds an index
    # row with ``promoted=0``. Keying the stall on ``challenger_count`` would
    # therefore report a fresh run's very first round — nothing decided,
    # nothing possibly promoted yet — as a loop going nowhere, in caution ink,
    # at the moment an operator is most likely watching. Reading an undecided round
    # as a stall is the same error as reading it as an improvement.
    stuck_no_promotions = traj.settled_count >= 1 and traj.promoted_count == 0
    if stuck_no_promotions:
        verdict = "no_signal" if floor is not None else "stalled"
    elif not traj.plateaued and len(traj.points) < 2:
        # Nothing has settled yet: the spine is the seed alone, so there is
        # neither an advance to call "improving" nor a stall to report. Covers
        # both the epoch that has fielded nothing and the one whose first
        # challengers are still in flight.
        verdict = "warming_up"
    elif not traj.plateaued:
        verdict = "improving"
    elif (
        floor is not None
        and recent_movement is not None
        and recent_movement <= float(floor["max_abs_delta"])
    ):
        # The window's whole movement fits inside the measured A/A spread:
        # "plateaued" would overstate the measurement — there is simply no
        # detectable signal above the noise floor.
        verdict = "no_signal"
    else:
        verdict = "plateaued"

    return {
        "epoch_id": traj.epoch_id,
        "points": points,
        "promotion_rate": traj.promotion_rate,
        "promoted_count": traj.promoted_count,
        "challenger_count": traj.challenger_count,
        "settled_count": traj.settled_count,
        "plateaued": traj.plateaued,
        "plateau_measurable": traj.plateau_measurable,
        "verdict": verdict,
        "recent_movement": recent_movement,
        "noise_floor": floor,
    }


def _empty_cost(epoch_id: str, note: str) -> dict[str, Any]:
    return {
        "epoch_id": epoch_id,
        "per_matchup": [],
        "total_runtime_ms": 0,
        "total_run_count": 0,
        "total_aborted_count": 0,
        "promoted_count": 0,
        "cost_per_promotion_ms": None,
        "note": note,
    }


def build_tournament_cost(paths: WorkspacePaths, epoch_id: str) -> dict[str, Any]:
    """Wall-clock + run-count cost accounting for one epoch's tournament.

    ``GET /api/epoch/{id}/cost`` returns this — a straight projection of
    :func:`zicato.tournament.detail.tournament_cost` (per-matchup runtime
    / run counts / aborts, epoch totals, and ``cost_per_promotion_ms``).
    Degrades to an empty shape with a ``note`` on a missing index / any
    sqlite failure — never raises.
    """
    from zicato.tournament.detail import (  # noqa: PLC0415
        IndexUnavailableError,
        tournament_cost,
    )

    try:
        return dict(tournament_cost(paths.index_db, epoch_id))
    except IndexUnavailableError:
        return _empty_cost(epoch_id, "index not built; run zicato repair index")
    except Exception:  # noqa: BLE001 — best-effort, mirrors sibling readers
        return _empty_cost(epoch_id, "index unreadable")


# ---------------------------------------------------------------------------
# The authoritative live ROUND-PIPELINE projection (propose → apply → run →
# gate). The server owns the phase-string inference, so every consumer reads
# ONE verdict.
# ---------------------------------------------------------------------------

#: A trailing ``done/total`` phase segment — the progress an epoch-open step
#: appends to its phase (``…:calibrating_noise_floor:7/18``).
_PROGRESS_SUFFIX = re.compile(r"\d+/\d+")

#: The four pipeline steps, in loop order.
PIPELINE_STEPS: tuple[tuple[str, str], ...] = (
    ("propose", "propose"),
    ("apply", "apply"),
    ("run", "run"),
    ("gate", "gate"),
)


#: The epoch-open steps that own the heartbeat while they run, keyed by the
#: phase segment each stamps: ``(label, the unit its progress counts)``. Both
#: strings are SERVER-owned — the clients render them verbatim
#: (``docs/design/EVAL-VIEW.md``: the server computes, the client renders) —
#: so a step added here reaches the
#: dashboard stepper and the console lifeline without touching either.
_EPOCH_OPEN_STEPS: dict[str, tuple[str, str]] = {
    CALIBRATION_PHASE_TOKEN: ("calibrating noise floor", "draws"),
    PREFLIGHT_PHASE_TOKEN: ("contract pre-flight", "probes"),
}


def _epoch_open_step(segments: list[str]) -> dict[str, str] | None:
    """The epoch-open step running BEFORE the pipeline, or ``None``.

    An epoch-open step (the A/A noise-floor calibration, the contract
    pre-flight) runs once per epoch, inside the first round but ahead of
    propose → apply → run → gate: it is serial, minutes-long, and while it
    runs the four pipeline steps have not started. Reporting it as
    its own step is what keeps that stretch from reading as a wedged round
    (issues #175 and #276).

    ``detail`` carries the live ``done/total`` count the step appends to its
    phase, named in the step's own unit, and is empty when the phase carries
    no progress suffix.
    """
    token = next((seg for seg in segments if seg in _EPOCH_OPEN_STEPS), None)
    if token is None:
        return None
    label, unit = _EPOCH_OPEN_STEPS[token]
    detail = ""
    tail = segments[-1]
    if tail != token and _PROGRESS_SUFFIX.fullmatch(tail):
        done, total = tail.split("/")
        detail = f"{done}/{total} {unit}"
    return {"id": token, "label": label, "detail": detail}


def _phase_round_index(segments: list[str]) -> int | None:
    """Extract the evolve-round index from phase segments, or ``None``."""
    for seg in segments:
        for prefix in ("round_", "after_round_"):
            if seg.startswith(prefix):
                tail = seg[len(prefix) :]
                if tail.isdigit():
                    return int(tail)
    return None


def _field_counts(tournament: dict[str, Any] | None) -> dict[str, int] | None:
    """Tally the active tournament's ``field_status`` slots, or ``None``.

    Returns ``{proposing, applied, rejected, total}`` when the (epoch-
    scoped) live tournament carries per-slot proposing-step outcomes.
    """
    if not isinstance(tournament, dict):
        return None
    raw = tournament.get("field_status")
    if not isinstance(raw, list) or not raw:
        return None
    counts = {"proposing": 0, "applied": 0, "rejected": 0, "total": 0}
    for slot in raw:
        if not isinstance(slot, dict):
            continue
        status = str(slot.get("status", ""))
        counts["total"] += 1
        if status == "proposing":
            counts["proposing"] += 1
        elif status == "applied":
            counts["applied"] += 1
        else:
            counts["rejected"] += 1
    return counts if counts["total"] else None


def _project_pipeline(
    phase: str,
    *,
    field_counts: dict[str, int] | None = None,
    tournament_phase: str | None = None,
    run_count: int = 0,
) -> tuple[list[dict[str, str]], str | None, str | None]:
    """The pure propose→apply→run→gate inference (unit-testable).

    Returns ``(steps, active_step, decision)`` where each step is
    ``{id, label, state, detail}`` with ``state`` ∈ pending | active |
    done. This is the single place the phase-string vocabulary
    (``proposing:… / tournament:… / done:… / after_round_…``) is decoded
    for the pipeline display — the JS renders the verdict verbatim.
    """
    states = {sid: "pending" for sid, _ in PIPELINE_STEPS}
    details = {sid: "" for sid, _ in PIPELINE_STEPS}
    active: str | None = None
    decision: str | None = None

    segments = [s for s in str(phase or "").strip().lower().split(":") if s]
    head = segments[0] if segments else ""

    if any(seg in _EPOCH_OPEN_STEPS for seg in segments):
        # An epoch-open step runs ahead of the pipeline (its phase still heads
        # with ``evolve_once``, which would otherwise read as proposing): every
        # step is pending, and :func:`_epoch_open_step` reports it.
        steps = [
            {"id": sid, "label": label, "state": "pending", "detail": ""}
            for sid, label in PIPELINE_STEPS
        ]
        return steps, None, None

    fc = field_counts
    field_detail = ""
    if fc is not None:
        field_detail = f"{fc['applied']} applied · {fc['rejected']} rejected"

    if head in ("proposing", "evolve_once"):
        if fc is not None and fc["proposing"] == 0:
            # every slot settled but the tournament has not opened yet —
            # the patches are applied/validated; the field is being staged.
            states["propose"] = "done"
            states["apply"] = "active"
            details["propose"] = f"{fc['total']} slot" + ("s" if fc["total"] != 1 else "")
            details["apply"] = field_detail
            active = "apply"
        else:
            states["propose"] = "active"
            if fc is not None:
                settled = fc["total"] - fc["proposing"]
                details["propose"] = f"{settled}/{fc['total']} slots settled"
                if settled:
                    details["apply"] = field_detail
            active = "propose"
    elif head == "tournament":
        states["propose"] = "done"
        states["apply"] = "done"
        if fc is not None:
            details["apply"] = field_detail
        at_completed = str(tournament_phase or "").lower() in ("completed", "complete", "done")
        if any("bt-replicate" in s for s in segments):
            # the evidence pre-gate's replicate audit — the GATE is deciding.
            states["run"] = "done"
            states["gate"] = "active"
            details["gate"] = "evidence audit · replicate duels"
            active = "gate"
        elif at_completed:
            # every unit settled, verdict pending — the gate is deciding.
            states["run"] = "done"
            states["gate"] = "active"
            details["gate"] = "deciding"
            active = "gate"
        else:
            states["run"] = "active"
            if run_count > 0:
                plural = "s" if run_count != 1 else ""
                details["run"] = f"{run_count} unit{plural} in flight"
            active = "run"
    elif head == "done" or head.startswith("after_round_"):
        for sid, _ in PIPELINE_STEPS:
            states[sid] = "done"
        if fc is not None:
            details["apply"] = field_detail
        tail = segments[-1] if segments else ""
        if tail in ("promoted", "rejected", "deferred", "no_decision", "crowned"):
            decision = tail
            details["gate"] = tail
    # any other head (evolve_n_rounds:start/done, idle, unknown) → all pending.

    steps = [
        {"id": sid, "label": label, "state": states[sid], "detail": details[sid]}
        for sid, label in PIPELINE_STEPS
    ]
    return steps, active, decision


def build_round_pipeline(paths: WorkspacePaths) -> dict[str, Any]:
    """The authoritative live pipeline state — ``GET /api/live/pipeline``.

    Projects the propose → apply → run → gate position SERVER-SIDE from,
    in preference order: the runtime tournament event-log fold (the
    ``field_status`` slot outcomes + the tournament ``phase``), the
    heartbeat ``phase`` string, and the in-flight ``active_runs`` count.
    The reader owns the phase-string inference; the stepper renders this
    verdict verbatim.

    ``running`` / ``stale`` are folded from the ONE served liveness
    verdict (:func:`zicato.query.runtime_view.derive_liveness`), which
    rides along as ``liveness``; the step projection is still reported
    for a dead workspace so a post-mortem read stays honest.
    Best-effort: every input degrades independently — never raises.
    """
    # Deferred imports: this rides the SSE-adjacent read path, keep it lean.
    from zicato.query.runtime_view import (  # noqa: PLC0415
        LIVENESS_INTERRUPTED,
        LIVENESS_LIVE,
        derive_liveness,
        fresh_run_count,
        read_active_runs_view,
        read_active_tournament_dict,
        read_heartbeat_dict,
    )

    hb = read_heartbeat_dict(paths)
    tournament = read_active_tournament_dict(paths)
    try:
        # Count records still BEATING, never records on disk: an
        # active_runs file outlives the worker that wrote it, so the file
        # count over-reports in flight by every dead unit (#268). One rule,
        # shared with derive_liveness — including the host-local identity
        # gate that reaps a provably dead worker's record at once (#270),
        # which is why the paths go through.
        run_count = fresh_run_count(read_active_runs_view(paths), paths=paths)
    except Exception:  # noqa: BLE001 — best-effort
        run_count = 0

    phase = str(hb.get("phase") or "") if isinstance(hb, dict) else ""
    hb_epoch = str(hb.get("epoch_id") or "") if isinstance(hb, dict) else ""
    round_index: int | None = None
    if isinstance(hb, dict) and isinstance(hb.get("round_index"), int):
        round_index = hb["round_index"]

    # Epoch-scope the tournament exactly like the frontend's
    # liveBelongsToEpoch: a KNOWN-and-different pair is rejected; a side
    # with no epoch id is tolerated (a single-epoch payload records none).
    at: dict[str, Any] | None = tournament if isinstance(tournament, dict) else None
    if at is not None:
        at_epoch = str(at.get("epoch_id") or "")
        if at_epoch and hb_epoch and at_epoch != hb_epoch:
            at = None

    segments = [s for s in phase.strip().lower().split(":") if s]
    if round_index is None:
        round_index = _phase_round_index(segments)

    steps, active, decision = _project_pipeline(
        phase,
        field_counts=_field_counts(at),
        tournament_phase=(str(at.get("phase") or "") if at is not None else None),
        run_count=run_count,
    )
    epoch_open_step = _epoch_open_step(segments)

    # Liveness is NOT re-derived here — the pipeline reads the one served
    # verdict (runtime_view.derive_liveness) so a post-mortem workspace
    # cannot read "running" on this surface and "interrupted" on another.
    # The step projection is still reported for a dead workspace (post-
    # mortem honesty); only the present-tense flags gate off it.
    liveness = derive_liveness(paths)

    return {
        "running": liveness["state"] == LIVENESS_LIVE,
        "stale": liveness["state"] == LIVENESS_INTERRUPTED,
        "liveness": liveness,
        "phase": phase or None,
        "epoch_id": hb_epoch or None,
        "round_index": round_index,
        "steps": steps,
        "epoch_open_step": epoch_open_step,
        "active_step": active,
        "decision": decision,
        "in_flight": run_count,
        "generated_at": _iso(_utc_now()),
    }


__all__ = [
    "PIPELINE_STEPS",
    "build_optimization_trajectory",
    "build_round_pipeline",
    "build_tournament_cost",
]
