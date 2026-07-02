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

from typing import Any

from zicato.dashboard.readers.paths import WorkspacePaths, _read_json_value


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
        "delta_std": (
            float(raw["delta_std"]) if isinstance(raw.get("delta_std"), int | float) else None
        ),
        "measured_at": str(raw.get("measured_at", "")),
    }


def _empty_trajectory(paths: WorkspacePaths, epoch_id: str, note: str) -> dict[str, Any]:
    return {
        "epoch_id": epoch_id,
        "points": [],
        "promotion_rate": None,
        "promoted_count": 0,
        "challenger_count": 0,
        "plateaued": False,
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
    * ``plateaued`` — the RAW detail-layer flag (no improvement across
      the trailing :data:`~zicato.tournament.detail.PLATEAU_WINDOW`).
    * ``recent_movement`` — max − min of the trailing-window scalars
      (the largest movement the window actually showed), or ``None``
      with fewer than two resolved scalars.
    * ``noise_floor`` — the epoch's measured A/A floor (or ``None``).
    * ``verdict`` — the UNCERTAINTY-HONEST word the UI renders:
      ``"improving"`` when not plateaued; ``"plateaued"`` when
      plateaued and the recent movement is resolvable ABOVE the floor
      (or no floor was measured); ``"no_signal"`` when plateaued but
      the window's movement sits at/below the measured floor — the
      data cannot distinguish that from an A/A re-roll.

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
        return _empty_trajectory(paths, epoch_id, "index not built; run zicato reindex")
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
    if not traj.plateaued:
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
        "plateaued": traj.plateaued,
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
        return _empty_cost(epoch_id, "index not built; run zicato reindex")
    except Exception:  # noqa: BLE001 — best-effort, mirrors sibling readers
        return _empty_cost(epoch_id, "index unreadable")


__all__ = [
    "build_optimization_trajectory",
    "build_tournament_cost",
]
