"""eval_view — the eval-centric (board-as-instrument) read surface.

The transpose of the candidate-centric UI: rows are board **entries** (the
measurement instrument), columns are **candidates** (what the instrument
measured). Two index-first, file-fallback readers back the three eval views
(EVAL-VIEW.md §5):

* :func:`build_eval_matrix` — the OUTCOMES lens: the entries × candidates
  matrix, one cell per (entry, candidate), with replicate-aware aggregation,
  evidence tiers, holdout flagging, and per-entry A/A flip rates.
* :func:`build_eval_dossier` — the INSTRUMENT-QUALITY lens for ONE entry: its
  flip rate, discrimination, runtime cost, per-candidate trajectory, and the
  first-passed-by / regressed-by attribution along the champion spine.

Every reader is best-effort and honest (EVAL-VIEW.md §3 DQ1/DQ2/DQ3): a
never-indexed workspace, an unknown epoch/entry, or absent calibration
degrades to a same-shape payload (``found: False`` / ``null`` fields, a
``"flip rate unmeasured"`` signal — NEVER a fabricated ``0.0``) rather than
raising. This module stays dashboard-free (the ``zicato.query`` import
contract).

The pure analytics helpers (:func:`flip_rate`, :func:`discrimination`,
:func:`runtime_aggregates`, :func:`pass_ratio`, :func:`evidence_of`) do no
I/O and are unit-tested against known answers.
"""

from __future__ import annotations

import json
import math
import statistics
from typing import Any

from zicato.query.paths import (
    WorkspacePaths,
    _read_json_value,
    _resolve_epoch_id,
    coerce_float,
    layout_of,
)

# The A/A calibration draws cache per replicate at this base (base 1000); the
# per-entry flip rate reads those replicate files directly (EVAL-VIEW.md §2.2).
_CALIBRATION_REPLICATE_BASE = 1000

# The live MDE ladder's operating characteristics (EVAL-VIEW.md §4.3, pinned to
# CAMPAIGN.md §3): the two-sample form at α=.05 / power .80 (with a relaxed α=.10
# rung), sd ≈ the measured A/A floor. At n=6, df=10 this reproduces the doc's
# ≈1.79·floor (α=.05) and ≈1.55·floor (α=.10) — the numbers CAMPAIGN.md §3 pins.
_MDE_ALPHA = 0.05
_MDE_ALPHA_RELAXED = 0.10
_MDE_POWER = 0.80
_MDE_FORMULA = "MDE = (t_{α/2,df} + t_{β,df})·sd·√(2/n),  sd ≈ floor,  df = 2·(n−1)"

# The minimum-comparisons honesty threshold for the DEAD-eval finding
# (EVAL-VIEW.md §5 WS-HEALTH): an entry needs at least this many both-sides
# matchups before a zero discrimination is read as "dead" rather than thin
# evidence. Below it the entry reports "insufficient comparisons", never dead —
# §4's no-fabricated-numbers rule extended to the discrimination claim.
_MIN_DISCRIMINATION_COMPARISONS = 3


# ---------------------------------------------------------------------------
# Pure analytics helpers (no I/O — unit-tested against known answers)
# ---------------------------------------------------------------------------


def flip_rate(pass_fail_draws: list[bool | None]) -> float | None:
    """The A/A flip rate over K calibration draws (EVAL-VIEW.md §2.2).

    The fraction of usable (non-``None``) draws whose pass/fail verdict flipped
    from the majority: ``min(#pass, #fail) / n_usable``. ``None`` when fewer
    than two usable draws exist — an unmeasured floor is honest, a ``0.0`` is a
    lie.
    """
    bits = [bool(b) for b in pass_fail_draws if b is not None]
    if len(bits) < 2:
        return None
    passes = sum(1 for b in bits if b)
    fails = len(bits) - passes
    return min(passes, fails) / len(bits)


def pass_ratio(bits: list[bool | None]) -> float | None:
    """Mean of the non-``None`` pass/fail bits, or ``None`` when there are none."""
    vals = [bool(b) for b in bits if b is not None]
    if not vals:
        return None
    return sum(1 for b in vals if b) / len(vals)


def majority_verdict(bits: list[bool | None]) -> bool | None:
    """The majority pass/fail verdict; ``None`` on no bits OR an exact tie.

    A tie is genuinely ambiguous, so it degrades to ``None`` rather than
    silently rounding toward pass.
    """
    ratio = pass_ratio(bits)
    if ratio is None or ratio == 0.5:
        return None
    return ratio > 0.5


def evidence_of(n: int) -> str:
    """Evidence tier for a cell built from ``n`` runs (EVAL-VIEW.md §4.1)."""
    if n <= 0:
        return "none"
    if n == 1:
        return "single"
    return "replicated"


def runtime_aggregates(values: list[float | int | None]) -> dict[str, float | None]:
    """``{mean, p50, max}`` over the non-``None`` runtimes (ms)."""
    vals = [float(v) for v in values if isinstance(v, int | float) and not isinstance(v, bool)]
    if not vals:
        return {"mean": None, "p50": None, "max": None}
    return {
        "mean": sum(vals) / len(vals),
        "p50": float(statistics.median(vals)),
        "max": max(vals),
    }


def discrimination(pairs: list[tuple[Any, bool | None]]) -> tuple[float | None, int]:
    """Discrimination rate over same-match groups (EVAL-VIEW.md §2.3).

    ``pairs`` is a list of ``(group_key, pass_fail)`` — the group key is the
    matchup ``(tournament_id, match_id)``. A group *discriminates* when it has
    at least two usable verdicts and they are not all equal. Returns
    ``(discriminating_groups / groups_with_both_sides, groups_with_both_sides)``;
    ``(None, 0)`` when no group has both sides present (nothing to discriminate).
    """
    groups: dict[Any, list[bool]] = {}
    for key, pf in pairs:
        if pf is None:
            continue
        groups.setdefault(key, []).append(bool(pf))
    both = 0
    disc = 0
    for bits in groups.values():
        if len(bits) < 2:
            continue
        both += 1
        if any(b != bits[0] for b in bits):
            disc += 1
    if both == 0:
        return None, 0
    return disc / both, both


# ---------------------------------------------------------------------------
# The live MDE ladder (pure — no I/O, unit-tested against the CAMPAIGN.md numbers)
# ---------------------------------------------------------------------------


def _betacf(a: float, b: float, x: float) -> float:
    """Continued-fraction expansion of the incomplete beta (Lentz's method).

    The Numerical-Recipes ``betacf`` — used by :func:`_reg_incomplete_beta` in
    the region where the fraction converges quickly. Pure standard-library math.
    """
    max_iter = 200
    eps = 3.0e-16
    fpmin = 1.0e-300
    qab = a + b
    qap = a + 1.0
    qam = a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < fpmin:
        d = fpmin
    d = 1.0 / d
    h = d
    for m in range(1, max_iter + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < fpmin:
            d = fpmin
        c = 1.0 + aa / c
        if abs(c) < fpmin:
            c = fpmin
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < fpmin:
            d = fpmin
        c = 1.0 + aa / c
        if abs(c) < fpmin:
            c = fpmin
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < eps:
            break
    return h


def _reg_incomplete_beta(a: float, b: float, x: float) -> float:
    """The regularized incomplete beta ``I_x(a, b)`` (Numerical Recipes ``betai``)."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    lbt = (
        math.lgamma(a + b)
        - math.lgamma(a)
        - math.lgamma(b)
        + a * math.log(x)
        + b * math.log(1.0 - x)
    )
    bt = math.exp(lbt)
    if x < (a + 1.0) / (a + b + 2.0):
        return bt * _betacf(a, b, x) / a
    return 1.0 - bt * _betacf(b, a, 1.0 - x) / b


def students_t_upper_quantile(upper_tail: float, df: int) -> float:
    """The Student-t value ``t`` with ``P(T > t) = upper_tail`` for ``df`` (df ≥ 1).

    Inverts the two-tailed survival ``P(|T| > t) = I_{df/(df+t²)}(df/2, 1/2)`` by
    bisection — pure standard-library math (no SciPy runtime dependency). Exact to
    machine precision against the standard t-tables; unit-tested against them.
    """
    p2 = 2.0 * upper_tail
    lo, hi = 0.0, 1.0e7
    for _ in range(160):
        mid = (lo + hi) / 2.0
        # Survival is monotone decreasing in t; walk toward the target tail mass.
        if _reg_incomplete_beta(df / 2.0, 0.5, df / (df + mid * mid)) > p2:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


def mde_ladder(floor: float | None, replicates: int) -> dict[str, Any]:
    """The two-sample MDE at the epoch's floor + replicate count (EVAL-VIEW.md §4.3).

    ``MDE = (t_{α/2,df} + t_{β,df})·sd·√(2/n)`` with ``sd ≈ floor`` and
    ``n = replicates`` (``df = 2·(n−1)``), served with EVERY input so the view
    states the formula honestly — never a bare number (§4). Degrades honestly:
    an unmeasured floor ⇒ ``floor_measured: False`` + a "floor unmeasured" note;
    ``n < 2`` ⇒ an "insufficient replication" note (the two-sample form needs two
    per arm) — NEVER a fabricated bound.
    """
    n = int(replicates) if isinstance(replicates, int) and not isinstance(replicates, bool) else 0
    n = max(0, n)
    block: dict[str, Any] = {
        "floor_measured": floor is not None,
        "floor": floor,
        "replicates": n,
        "usable": False,
        "formula_n": None,
        "df": None,
        "mde": None,
        "mde_relaxed": None,
        "alpha": _MDE_ALPHA,
        "alpha_relaxed": _MDE_ALPHA_RELAXED,
        "power": _MDE_POWER,
        "formula": _MDE_FORMULA,
        "note": None,
    }
    if floor is None:
        block["note"] = "floor unmeasured — run the A/A calibration to measure the noise floor"
        return block
    if n < 2:
        block["note"] = f"n={n}: the two-sample MDE needs at least 2 replicates per arm"
        return block
    df = 2 * (n - 1)
    t_alpha = students_t_upper_quantile(_MDE_ALPHA / 2.0, df)
    t_alpha_relaxed = students_t_upper_quantile(_MDE_ALPHA_RELAXED / 2.0, df)
    t_beta = students_t_upper_quantile(1.0 - _MDE_POWER, df)
    root = math.sqrt(2.0 / n)
    block.update(
        usable=True,
        formula_n=n,
        df=df,
        mde=(t_alpha + t_beta) * floor * root,
        mde_relaxed=(t_alpha_relaxed + t_beta) * floor * root,
    )
    return block


# ---------------------------------------------------------------------------
# Small internal accessors (tolerant of a stale index / missing columns)
# ---------------------------------------------------------------------------


def _row_get(row: Any, key: str) -> Any:
    """``row[key]`` when present, else ``None`` (tolerates an omitted column)."""
    try:
        keys = row.keys()
    except AttributeError:
        return None
    return row[key] if key in keys else None


def _opt_bool(value: Any) -> bool | None:
    if value is None:
        return None
    return bool(value)


def _opt_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return int(value)


def _score_from_loss_json(row: Any) -> float | None:
    """Lift the continuous ``score`` out of the row's ``loss_json`` blob.

    The continuous per-entry outcome lives in the verbatim ``loss_json`` blob,
    not a dedicated column (mirrors ``judge_view.build_per_entry_for_generation``).
    A missing / malformed blob degrades to ``None``.
    """
    raw = _row_get(row, "loss_json")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        blob = json.loads(raw)
    except (ValueError, json.JSONDecodeError):
        return None
    if not isinstance(blob, dict):
        return None
    return coerce_float(blob.get("score"))


# ---------------------------------------------------------------------------
# Shared workspace loads (board order + holdout split + calibration)
# ---------------------------------------------------------------------------


def _load_board_entries(paths: WorkspacePaths, epoch_id: str) -> list[Any]:
    """Load the epoch's board as ``BoardEntry`` objects (``[]`` on any defect)."""
    from zicato.board.jsonl import load_board  # noqa: PLC0415

    try:
        return list(load_board(layout_of(paths).board(epoch_id)))
    except Exception:  # noqa: BLE001 — best-effort; a missing/bad board degrades
        return []


def _holdout_ids(paths: WorkspacePaths, epoch_id: str, board_entries: list[Any]) -> set[str]:
    """The holdout id set, bound to the SAME split the gate plays (§2.4).

    Uses the canonical :func:`zicato.board.split.split_board` with
    ``seed = rotation_seed(cfg, epoch_id)`` (exactly the gate's call), reading
    the ``overfitting`` block off the epoch's ``scoring.json``. An empty /
    unconfigured / unreadable split yields an empty set (every entry train).
    """
    from zicato.board.split import rotation_seed, split_board  # noqa: PLC0415
    from zicato.workspace_loader import overfitting_config_from_dict  # noqa: PLC0415

    if not board_entries:
        return set()
    scoring = _read_json_value(layout_of(paths).epoch_dir(epoch_id) / "scoring.json")
    raw = scoring.get("overfitting") if isinstance(scoring, dict) else None
    try:
        cfg = overfitting_config_from_dict(raw)
        seed = rotation_seed(cfg, epoch_id)
        _train, holdout = split_board(board_entries, cfg, seed=seed)
    except Exception:  # noqa: BLE001 — best-effort; degrade to no holdout
        return set()
    return set(holdout)


def _calibration(paths: WorkspacePaths, epoch_id: str) -> dict[str, Any]:
    """Read the persisted A/A noise floor off ``config.json`` (§2.2).

    Returns ``{measured, generation_id, runs, max_abs_delta}``. ``measured`` is
    ``False`` when no ``noise_floor`` was persisted — the caller then reports
    flip rate unmeasured rather than fabricating a zero.
    """
    cfg = _read_json_value(layout_of(paths).epoch_dir(epoch_id) / "config.json")
    floor = cfg.get("noise_floor") if isinstance(cfg, dict) else None
    if not isinstance(floor, dict):
        return {"measured": False, "generation_id": None, "runs": 0, "max_abs_delta": None}
    gen = floor.get("generation_id")
    runs = floor.get("runs")
    return {
        "measured": True,
        "generation_id": gen if isinstance(gen, str) and gen else None,
        "runs": int(runs) if isinstance(runs, int) and not isinstance(runs, bool) else 0,
        "max_abs_delta": coerce_float(floor.get("max_abs_delta")),
    }


def _per_entry_flip_rates(
    paths: WorkspacePaths, epoch_id: str, calibration: dict[str, Any]
) -> dict[str, float | None]:
    """Per-entry A/A flip rate from the base-1000 replicate files (§2.2).

    The calibration draws are NOT ingested into ``loss_profiles`` (the index
    reads only replicate-0 canonical ``loss.json``), so this reads
    ``loss.r<1000+i>.json`` for ``i in [0, runs)`` under the champion
    generation directly and folds each entry's per-draw ``pass_fail`` through
    :func:`flip_rate`. Returns ``{entry_id: flip_rate | None}`` — empty when
    calibration was never measured.
    """
    from zicato.telemetry.reducer import read_loss_profile  # noqa: PLC0415
    from zicato.tournament.unit_cache import _unit_loss_path  # noqa: PLC0415

    gen = calibration.get("generation_id")
    runs = calibration.get("runs") or 0
    if not calibration.get("measured") or not isinstance(gen, str) or runs < 2:
        return {}

    # Discover which entries have a champion run dir (the calibration wrote one
    # replicate file per board entry under the champion generation).
    runs_root = layout_of(paths).epoch_dir(epoch_id) / "generations" / gen / "runs"
    if not runs_root.exists():
        return {}

    out: dict[str, float | None] = {}
    for child in sorted(runs_root.iterdir()):
        if not child.is_dir():
            continue
        entry_id = child.name
        draws: list[bool | None] = []
        for i in range(runs):
            replicate = _CALIBRATION_REPLICATE_BASE + i
            path = _unit_loss_path(paths.root, epoch_id, gen, entry_id, replicate)
            try:
                profile = read_loss_profile(path)
            except (OSError, ValueError, KeyError, json.JSONDecodeError):
                profile = None
            if profile is None:
                continue
            draws.append(getattr(profile, "pass_fail", None))
        # Only report a rate when at least one draw was actually read; an entry
        # with no replicate files stays unmeasured (absent from the map).
        if draws:
            out[entry_id] = flip_rate(draws)
    return out


# ---------------------------------------------------------------------------
# Candidate axis (columns) — round order, champion spine marked
# ---------------------------------------------------------------------------


def _candidate_axis(paths: WorkspacePaths, epoch_id: str) -> list[dict[str, Any]]:
    """The ordered candidate columns for the epoch (EVAL-VIEW.md §3.1).

    Column order is ``(round_index ?? +inf, created_at, generation_id)`` — the
    index already returns generations in ``(created_at, generation_id)`` order,
    so a stable sort by ``round_index`` finishes the ordering. Each column
    carries ``promoted`` / ``champion_spine`` (the promoted spine) and the
    read-only rating triple.
    """
    from zicato.index.query import generations_for_epoch  # noqa: PLC0415

    try:
        rows = generations_for_epoch(paths.index_db, epoch_id)
    except Exception:  # noqa: BLE001 — best-effort
        rows = []

    axis: list[dict[str, Any]] = []
    for r in rows:
        gid = _row_get(r, "generation_id")
        if not isinstance(gid, str) or not gid:
            continue
        promoted = bool(_row_get(r, "promoted"))
        axis.append(
            {
                "generation_id": gid,
                "round_index": _opt_int(_row_get(r, "round_index")),
                "promoted": promoted,
                # The promoted generations form the champion spine (§3.1).
                "champion_spine": promoted,
                "elo": coerce_float(_row_get(r, "elo")),
                "elo_se": coerce_float(_row_get(r, "elo_se")),
            }
        )

    # Stable sort by round_index (None sinks last); the index' created_at order
    # is preserved for equal round indices.
    axis.sort(key=lambda c: (c["round_index"] is None, c["round_index"] or 0))
    return axis


def _rows_for_candidate(paths: WorkspacePaths, epoch_id: str, gen: str) -> list[Any]:
    from zicato.index.query import loss_profiles_for_generation  # noqa: PLC0415

    try:
        return list(loss_profiles_for_generation(paths.index_db, epoch_id, gen))
    except Exception:  # noqa: BLE001 — best-effort
        return []


def _aggregate_cell(rows: list[Any]) -> dict[str, Any] | None:
    """Fold multiple (entry, candidate) runs into ONE cell (§3.1 aggregation)."""
    if not rows:
        return None
    drift = [coerce_float(_row_get(r, "drift_loss")) for r in rows]
    drift_vals = [d for d in drift if d is not None]
    scores = [_score_from_loss_json(r) for r in rows]
    score_vals = [s for s in scores if s is not None]
    bits = [_opt_bool(_row_get(r, "pass_fail")) for r in rows]
    runtimes = [_row_get(r, "runtime_ms") for r in rows]
    # latest_run_id: the last run id in the index' (entry_id, run_id) order.
    run_ids = [_row_get(r, "run_id") for r in rows if isinstance(_row_get(r, "run_id"), str)]
    cached_any = any(bool(_row_get(r, "cached")) for r in rows)
    return {
        "drift_loss": (sum(drift_vals) / len(drift_vals)) if drift_vals else None,
        "pass_ratio": pass_ratio(bits),
        "pass_fail": majority_verdict(bits),
        "score": (sum(score_vals) / len(score_vals)) if score_vals else None,
        "replicates": len(rows),
        "cached": cached_any,
        "latest_run_id": run_ids[-1] if run_ids else None,
        "runtime_ms_mean": runtime_aggregates(runtimes)["mean"],
        "evidence": evidence_of(len(rows)),
    }


# ---------------------------------------------------------------------------
# build_eval_matrix — the OUTCOMES lens
# ---------------------------------------------------------------------------


def _empty_matrix(epoch_id: str | None) -> dict[str, Any]:
    return {
        "epoch_id": epoch_id,
        "found": False,
        "candidates": [],
        "entries": [],
        "cells": [],
        "calibration": {"measured": False, "generation_id": None, "runs": 0, "max_abs_delta": None},
        "note": "no such epoch / never indexed",
    }


def build_eval_matrix(paths: WorkspacePaths, epoch_id: str | None = None) -> dict[str, Any]:
    """The entries × candidates outcomes matrix for one epoch (EVAL-VIEW.md §3.1).

    Rows are board entries (board order, holdout-flagged, flip-rate-badged);
    columns are candidates (round order, champion spine marked). Each cell folds
    every (entry, candidate) run into a replicate-aware summary with an evidence
    tier. A never-indexed workspace or an unknown epoch degrades to a same-shape
    payload with ``found: False`` (never raises).
    """
    try:
        resolved = _resolve_epoch_id(paths, epoch_id)
    except ValueError:
        return _empty_matrix(epoch_id)
    if resolved is None:
        return _empty_matrix(epoch_id)

    candidates = _candidate_axis(paths, resolved)
    board_entries = _load_board_entries(paths, resolved)
    holdout = _holdout_ids(paths, resolved, board_entries)
    calibration = _calibration(paths, resolved)
    flips = _per_entry_flip_rates(paths, resolved, calibration)

    # (entry_id, gen) -> aggregated cell. Built from the per-candidate loss rows.
    cell_by: dict[tuple[str, str], dict[str, Any]] = {}
    seen_entries: list[str] = []
    seen_set: set[str] = set()
    for cand in candidates:
        gen = cand["generation_id"]
        by_entry: dict[str, list[Any]] = {}
        for r in _rows_for_candidate(paths, resolved, gen):
            eid = _row_get(r, "entry_id")
            if not isinstance(eid, str) or not eid:
                continue
            by_entry.setdefault(eid, []).append(r)
        for eid, rows in by_entry.items():
            cell = _aggregate_cell(rows)
            if cell is not None:
                cell_by[(eid, gen)] = cell
            if eid not in seen_set:
                seen_set.add(eid)
                seen_entries.append(eid)

    # Row order: board order first (the instrument's own order), then any entry
    # that has loss rows but is absent from the board (defensive — a renamed /
    # dropped entry still surfaces rather than vanishing).
    board_order = [e.id for e in board_entries if isinstance(getattr(e, "id", None), str)]
    board_set = set(board_order)
    ordered_entries = board_order + [e for e in seen_entries if e not in board_set]

    entries_out: list[dict[str, Any]] = []
    cells: list[list[dict[str, Any] | None]] = []
    for eid in ordered_entries:
        measured = eid in flips
        entries_out.append(
            {
                "entry_id": eid,
                "slice": "holdout" if eid in holdout else "train",
                "tag": "holdout" if eid in holdout else None,
                "flip_rate": flips.get(eid) if measured else None,
                "flip_rate_measured": measured,
                "calibration_runs": calibration["runs"] if calibration["measured"] else 0,
            }
        )
        cells.append([cell_by.get((eid, cand["generation_id"])) for cand in candidates])

    return {
        "epoch_id": resolved,
        "found": True,
        "candidates": candidates,
        "entries": entries_out,
        "cells": cells,
        "calibration": calibration,
    }


# ---------------------------------------------------------------------------
# build_eval_dossier — the per-entry INSTRUMENT-QUALITY lens
# ---------------------------------------------------------------------------


def _empty_dossier(epoch_id: str | None, entry_id: str) -> dict[str, Any]:
    return {
        "epoch_id": epoch_id,
        "entry_id": entry_id,
        "found": False,
        "slice": "train",
        "tag": None,
        "instrument": {
            "flip_rate": None,
            "flip_rate_measured": False,
            "calibration_runs": 0,
            "discrimination": None,
            "discrimination_pairs": 0,
            "runtime_ms_mean": None,
            "runtime_ms_p50": None,
            "runtime_ms_max": None,
            "replicate_total": 0,
            "cached_share": None,
        },
        "trajectory": [],
        "attribution": {"first_passed_by": None, "regressed_by": []},
        "reflection_findings": [],
        "note": "no such epoch / entry",
    }


def _reflection_findings_for_entry(
    paths: WorkspacePaths, epoch_id: str, entry_id: str
) -> list[dict[str, Any]]:
    """Best-effort links to reflection findings that NAME this entry (§2.6).

    Cheap and honest: scans the epoch's reflections (via ``reflection_view``)
    and keeps findings whose serialized text mentions ``entry_id``. Empty when
    no reflection exists or none reference the entry — the WS-DOSSIER view links
    into ``reflection_view`` for the full detail; this is only the pointer.
    """
    from zicato.query.reflection_view import (  # noqa: PLC0415
        build_reflection_summary,
        list_reflections,
    )

    out: list[dict[str, Any]] = []
    try:
        listing = list_reflections(paths, epoch_id)
    except Exception:  # noqa: BLE001 — best-effort
        return []
    for item in listing.get("reflections", []):
        rid = item.get("reflection_id")
        if not isinstance(rid, str) or not rid:
            continue
        try:
            summary = build_reflection_summary(paths, rid)
        except Exception:  # noqa: BLE001 — best-effort
            continue
        for finding in summary.get("findings", []):
            if isinstance(finding, dict) and entry_id in json.dumps(finding):
                out.append({"reflection_id": rid, "finding": finding})
    return out


def build_eval_dossier(
    paths: WorkspacePaths, epoch_id: str | None, entry_id: str
) -> dict[str, Any]:
    """One board entry across every candidate — the instrument-quality lens (§3.2).

    Assembles the entry's A/A flip rate, its discrimination (same-match_id
    verdict splits), its runtime cost aggregates, a per-candidate trajectory in
    round order, and the first-passed-by / regressed-by attribution along the
    champion spine. An unknown epoch/entry degrades to a same-shape payload with
    ``found: False``.
    """
    try:
        resolved = _resolve_epoch_id(paths, epoch_id)
    except ValueError:
        return _empty_dossier(epoch_id, entry_id)
    if resolved is None:
        return _empty_dossier(epoch_id, entry_id)

    candidates = _candidate_axis(paths, resolved)
    board_entries = _load_board_entries(paths, resolved)
    holdout = _holdout_ids(paths, resolved, board_entries)
    calibration = _calibration(paths, resolved)
    flips = _per_entry_flip_rates(paths, resolved, calibration)

    # Gather every loss row for THIS entry, per candidate.
    per_candidate_rows: dict[str, list[Any]] = {}
    all_rows: list[Any] = []
    for cand in candidates:
        gen = cand["generation_id"]
        rows = [
            r
            for r in _rows_for_candidate(paths, resolved, gen)
            if _row_get(r, "entry_id") == entry_id
        ]
        if rows:
            per_candidate_rows[gen] = rows
            all_rows.extend(rows)

    board_ids = {e.id for e in board_entries if isinstance(getattr(e, "id", None), str)}
    found = entry_id in board_ids or bool(all_rows)
    if not found:
        payload = _empty_dossier(resolved, entry_id)
        payload["epoch_id"] = resolved
        return payload

    # Discrimination: pair rows by (tournament_id, match_id), restricted to
    # MATCHUP-scoped rows (a real ``match_id``) and EXCLUDING cached rows (a
    # carried-over result is not a fresh measurement of the matchup). A
    # gauntlet/ad-hoc run (no match_id) is not a matchup and never counts.
    disc_pairs: list[tuple[Any, bool | None]] = []
    for r in all_rows:
        if bool(_row_get(r, "cached")):
            continue
        mid = _row_get(r, "match_id")
        if not isinstance(mid, str) or not mid:
            continue
        disc_pairs.append(
            ((_row_get(r, "tournament_id"), mid), _opt_bool(_row_get(r, "pass_fail")))
        )
    disc_rate, disc_n = discrimination(disc_pairs)

    runtimes = [_row_get(r, "runtime_ms") for r in all_rows]
    rt = runtime_aggregates(runtimes)
    cached_count = sum(1 for r in all_rows if bool(_row_get(r, "cached")))
    cached_share = (cached_count / len(all_rows)) if all_rows else None
    measured = entry_id in flips

    # Trajectory + attribution along the champion spine (round order).
    trajectory: list[dict[str, Any]] = []
    first_passed_by: str | None = None
    regressed_by: list[str] = []
    prev_pass: bool | None = None
    for cand in candidates:
        gen = cand["generation_id"]
        rows = per_candidate_rows.get(gen, [])
        cell = _aggregate_cell(rows)
        bits = [_opt_bool(_row_get(r, "pass_fail")) for r in rows]
        verdict = majority_verdict(bits)
        trajectory.append(
            {
                "generation_id": gen,
                "round_index": cand["round_index"],
                "champion_spine": cand["champion_spine"],
                "drift_loss": cell["drift_loss"] if cell else None,
                "pass_ratio": cell["pass_ratio"] if cell else None,
                "replicates": cell["replicates"] if cell else 0,
                "cached": cell["cached"] if cell else False,
            }
        )
        # Attribution walks the champion spine only (the promoted path).
        if cand["champion_spine"] and verdict is not None:
            if verdict and first_passed_by is None:
                first_passed_by = gen
            if prev_pass is True and verdict is False:
                regressed_by.append(gen)
            prev_pass = verdict

    return {
        "epoch_id": resolved,
        "entry_id": entry_id,
        "found": True,
        "slice": "holdout" if entry_id in holdout else "train",
        "tag": "holdout" if entry_id in holdout else None,
        "instrument": {
            "flip_rate": flips.get(entry_id) if measured else None,
            "flip_rate_measured": measured,
            "calibration_runs": calibration["runs"] if calibration["measured"] else 0,
            "discrimination": disc_rate,
            "discrimination_pairs": disc_n,
            "runtime_ms_mean": rt["mean"],
            "runtime_ms_p50": rt["p50"],
            "runtime_ms_max": rt["max"],
            "replicate_total": len(all_rows),
            "cached_share": cached_share,
        },
        "trajectory": trajectory,
        "attribution": {"first_passed_by": first_passed_by, "regressed_by": regressed_by},
        "reflection_findings": _reflection_findings_for_entry(paths, resolved, entry_id),
    }


# ---------------------------------------------------------------------------
# build_eval_health — the WS-HEALTH instrument panel (epoch-wide)
# ---------------------------------------------------------------------------


def _realised_replicates(paths: WorkspacePaths, epoch_id: str) -> int:
    """The epoch's per-arm replicate count — the frozen tournament ``replicates``.

    The MDE ladder's ``n`` (EVAL-VIEW.md §4.3): read off the frozen
    ``scoring.json`` tournament block (``params.replicates``); the contract
    default of ``1`` when absent / malformed (a single sample per arm — the
    ladder then honest-empties with an "insufficient replication" note).
    """
    scoring = _read_json_value(layout_of(paths).epoch_dir(epoch_id) / "scoring.json")
    raw = scoring.get("tournament") if isinstance(scoring, dict) else None
    params = raw.get("params") if isinstance(raw, dict) else None
    rep = params.get("replicates") if isinstance(params, dict) else None
    if isinstance(rep, int) and not isinstance(rep, bool) and rep > 0:
        return rep
    return 1


def _instrument_by_entry(
    paths: WorkspacePaths, epoch_id: str, candidates: list[dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """Per-entry discrimination + runtime, folded once across every candidate.

    Returns ``{entry_id: {discrimination, discrimination_pairs, runtime_ms_mean,
    replicate_total}}``. Discrimination pairs by ``(tournament_id, match_id)``,
    EXCLUDING cached rows (a carried-over result is not a fresh measurement) and
    non-matchup rows (no ``match_id``) — byte-identical to the dossier's rule
    (§2.3). One pass over the candidate loss rows keeps the panel cheap.
    """
    disc_pairs: dict[str, list[tuple[Any, bool | None]]] = {}
    runtimes: dict[str, list[Any]] = {}
    totals: dict[str, int] = {}
    for cand in candidates:
        for r in _rows_for_candidate(paths, epoch_id, cand["generation_id"]):
            eid = _row_get(r, "entry_id")
            if not isinstance(eid, str) or not eid:
                continue
            totals[eid] = totals.get(eid, 0) + 1
            runtimes.setdefault(eid, []).append(_row_get(r, "runtime_ms"))
            if bool(_row_get(r, "cached")):
                continue
            mid = _row_get(r, "match_id")
            if not isinstance(mid, str) or not mid:
                continue
            disc_pairs.setdefault(eid, []).append(
                ((_row_get(r, "tournament_id"), mid), _opt_bool(_row_get(r, "pass_fail")))
            )

    out: dict[str, dict[str, Any]] = {}
    for eid, total in totals.items():
        rate, n_pairs = discrimination(disc_pairs.get(eid, []))
        out[eid] = {
            "discrimination": rate,
            "discrimination_pairs": n_pairs,
            "runtime_ms_mean": runtime_aggregates(runtimes.get(eid, []))["mean"],
            "replicate_total": total,
        }
    return out


def _rotation_status(
    paths: WorkspacePaths, epoch_id: str, experiments: list[dict[str, Any]]
) -> dict[str, Any]:
    """Rotation-cadence status — BOUND to the shipped surfaces, not re-derived (§5).

    ``rotate_holdout`` / ``max_generations_per_contract`` come off the frozen
    ``scoring.json`` overfitting block; the refresh recommendation is the shipped
    :func:`zicato.health.diagnostics.detect_refresh_cadence` finding (the same
    signal the loop logs), so this panel never disagrees with the health surface.
    """
    from zicato.health.diagnostics import detect_refresh_cadence  # noqa: PLC0415

    scoring = _read_json_value(layout_of(paths).epoch_dir(epoch_id) / "scoring.json")
    raw = scoring.get("overfitting") if isinstance(scoring, dict) else None
    rotate = bool(raw.get("rotate_holdout")) if isinstance(raw, dict) else False
    ceiling_raw = raw.get("max_generations_per_contract") if isinstance(raw, dict) else None
    ceiling = (
        int(ceiling_raw)
        if isinstance(ceiling_raw, int) and not isinstance(ceiling_raw, bool) and ceiling_raw >= 1
        else None
    )
    evaluated = sum(
        1 for exp in experiments if isinstance(exp, dict) and exp.get("outcome") is not None
    )

    findings = detect_refresh_cadence(experiments, ceiling)
    recommendation: str | None = None
    refresh_recommended = False
    if findings:
        detail = getattr(findings[0], "detail", {}) or {}
        refresh_recommended = bool(detail.get("refresh_recommended"))
        rec = detail.get("recommendation")
        recommendation = rec if isinstance(rec, str) else None

    return {
        "rotate_holdout": rotate,
        "max_generations_per_contract": ceiling,
        "evaluated_generations": evaluated,
        "refresh_recommended": refresh_recommended,
        "recommendation": recommendation,
    }


def _redundancy_clusters(paths: WorkspacePaths, epoch_id: str) -> dict[str, Any]:
    """Redundancy clusters from an ALREADY-BUILT reflection — else a deferred note.

    EVAL-VIEW.md §5 / §2.6: the ``redundant_with`` clusters live in the reflection
    corpus (``reflection_view.build_judge_scorecards``). This LINKS into the most
    recent reflection when one exists (cheap: one listing + one scorecard read);
    it NEVER runs a reflection to fill the panel — absent a reflection it defers
    with an explicit note pointing at ``reflect``.
    """
    from zicato.query.reflection_view import (  # noqa: PLC0415
        build_judge_scorecards,
        list_reflections,
    )

    deferred = {
        "available": False,
        "clusters": [],
        "note": "no reflection built for this epoch — run `reflect` to surface redundancy clusters",
    }
    try:
        listing = list_reflections(paths, epoch_id)
    except Exception:  # noqa: BLE001 — best-effort; a missing reflection defers
        return deferred
    reflections = listing.get("reflections", []) if isinstance(listing, dict) else []
    latest = next(
        (
            item.get("reflection_id")
            for item in reflections
            if isinstance(item, dict) and isinstance(item.get("reflection_id"), str)
        ),
        None,
    )
    if not isinstance(latest, str) or not latest:
        return deferred
    try:
        scorecards = build_judge_scorecards(paths, latest)
    except Exception:  # noqa: BLE001 — best-effort; a bad scorecard defers
        return deferred
    clusters: list[dict[str, Any]] = []
    for card in scorecards.get("judges", []) if isinstance(scorecards, dict) else []:
        if not isinstance(card, dict):
            continue
        redundant = card.get("redundant_with")
        if isinstance(redundant, list) and redundant:
            clusters.append(
                {
                    "judge_name": card.get("judge_name"),
                    "redundant_with": [r for r in redundant if isinstance(r, str)],
                }
            )
    return {"available": True, "reflection_id": latest, "clusters": clusters, "note": None}


def _empty_health(epoch_id: str | None) -> dict[str, Any]:
    return {
        "epoch_id": epoch_id,
        "found": False,
        "mde": mde_ladder(None, 0),
        "noisiest": [],
        "dead": [],
        "insufficient": [],
        "runtime_cost": [],
        "holdout_budget": None,
        "rotation": {
            "rotate_holdout": False,
            "max_generations_per_contract": None,
            "evaluated_generations": 0,
            "refresh_recommended": False,
            "recommendation": None,
        },
        "redundancy": {
            "available": False,
            "clusters": [],
            "note": "no such epoch / never indexed",
        },
        "note": "no such epoch / never indexed",
    }


def build_eval_health(paths: WorkspacePaths, epoch_id: str | None = None) -> dict[str, Any]:
    """The WS-HEALTH instrument panel for one epoch (EVAL-VIEW.md §5 WS-HEALTH).

    The board read as a measuring device: the measured noise floor + the live MDE
    ladder (§4.3), the ranked noisy / dead / costly evals (§2.2/§2.3), the
    holdout-budget accounting and rotation cadence (bound to the shipped ladder /
    cadence surfaces), and — only when a reflection already exists — its
    redundancy clusters (else a deferred note). Recommend-only; every finding is a
    pointer into ``reflect`` / the builder, not an action. A never-indexed
    workspace or an unknown epoch degrades to a same-shape ``found: False`` payload
    (§4: no fabricated numbers — an unmeasured floor / flip rate reads honest-empty,
    never ``0.0``).
    """
    from zicato.query.epoch_view import (  # noqa: PLC0415
        _latest_holdout_summary,
        _read_epoch_experiments,
    )

    try:
        resolved = _resolve_epoch_id(paths, epoch_id)
    except ValueError:
        return _empty_health(epoch_id)
    if resolved is None:
        return _empty_health(epoch_id)

    candidates = _candidate_axis(paths, resolved)
    board_entries = _load_board_entries(paths, resolved)
    holdout = _holdout_ids(paths, resolved, board_entries)
    calibration = _calibration(paths, resolved)
    flips = _per_entry_flip_rates(paths, resolved, calibration)
    instrument = _instrument_by_entry(paths, resolved, candidates)

    def _slice(eid: str) -> str:
        return "holdout" if eid in holdout else "train"

    # Board order first (the instrument's own order), then any extra measured id.
    board_order = [e.id for e in board_entries if isinstance(getattr(e, "id", None), str)]
    board_set = set(board_order)
    ordered = board_order + [e for e in instrument if e not in board_set]

    # Noisiest — measured flip rate, descending (an unmeasured entry is omitted,
    # never rendered as a 0.0; §4).
    noisiest = [
        {
            "entry_id": eid,
            "flip_rate": flips[eid],
            "slice": _slice(eid),
            "calibration_runs": calibration["runs"] if calibration["measured"] else 0,
        }
        for eid in ordered
        if eid in flips and flips[eid] is not None
    ]
    noisiest.sort(key=lambda r: r["flip_rate"], reverse=True)

    # Dead vs insufficient — a zero-discrimination channel is DEAD only above the
    # minimum-comparisons honesty threshold; below it we say "insufficient
    # comparisons", never "dead" (§5 WS-HEALTH honesty rule).
    dead: list[dict[str, Any]] = []
    insufficient: list[dict[str, Any]] = []
    for eid in ordered:
        info = instrument.get(eid)
        if info is None:
            continue
        rate = info["discrimination"]
        n_pairs = info["discrimination_pairs"]
        if rate is None or n_pairs < _MIN_DISCRIMINATION_COMPARISONS:
            insufficient.append(
                {"entry_id": eid, "discrimination_pairs": n_pairs, "slice": _slice(eid)}
            )
        elif rate == 0.0:
            dead.append({"entry_id": eid, "discrimination_pairs": n_pairs, "slice": _slice(eid)})
    dead.sort(key=lambda r: r["discrimination_pairs"], reverse=True)

    # Runtime cost — mean ms per entry, descending (unmeasured runtimes omitted).
    runtime_cost = [
        {
            "entry_id": eid,
            "runtime_ms_mean": instrument[eid]["runtime_ms_mean"],
            "replicate_total": instrument[eid]["replicate_total"],
            "slice": _slice(eid),
        }
        for eid in ordered
        if eid in instrument and instrument[eid]["runtime_ms_mean"] is not None
    ]
    runtime_cost.sort(key=lambda r: r["runtime_ms_mean"], reverse=True)

    experiments = _read_epoch_experiments(layout_of(paths).epoch_dir(resolved))

    return {
        "epoch_id": resolved,
        "found": True,
        "mde": mde_ladder(calibration["max_abs_delta"], _realised_replicates(paths, resolved)),
        "noisiest": noisiest,
        "dead": dead,
        "insufficient": insufficient,
        "runtime_cost": runtime_cost,
        "holdout_budget": _latest_holdout_summary(experiments),
        "rotation": _rotation_status(paths, resolved, experiments),
        "redundancy": _redundancy_clusters(paths, resolved),
    }


__all__ = [
    "build_eval_dossier",
    "build_eval_health",
    "build_eval_matrix",
    "discrimination",
    "evidence_of",
    "flip_rate",
    "majority_verdict",
    "mde_ladder",
    "pass_ratio",
    "runtime_aggregates",
    "students_t_upper_quantile",
]
