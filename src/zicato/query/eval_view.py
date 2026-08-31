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

Every reader is best-effort and honest (EVAL-VIEW.md §3): a
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
from zicato.query.replicate_scores import cell_replicate_draws

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

    A tie is ambiguous, so it degrades to ``None`` rather than
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


def _opt_score_val(value: Any) -> float | None:
    """Coerce a raw ``score`` field into a finite float, else ``None``.

    Mirrors ``tournament_view._opt_score``: a bool, a non-number, or a
    non-finite value degrades to ``None`` so a cell built from replicate files
    reads its continuous score exactly as the matchup grid does.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    f = float(value)
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return f


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
    from zicato.tournament.calibration import CALIBRATION_REPLICATE_BASE  # noqa: PLC0415
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
            replicate = CALIBRATION_REPLICATE_BASE + i
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


def _lineage_nodes(paths: WorkspacePaths, epoch_id: str) -> dict[str, dict[str, Any]]:
    """The epoch's lineage nodes keyed by generation id (EVAL-VIEW.md §3.1).

    The lineage view is THE promotion authority. ``lineage.json`` owns parent
    and promoted state; experiment outcomes are journal detail, never a second
    topology source.
    Best-effort: an unreadable workspace yields ``{}`` and the caller
    falls back to the index bool.
    """
    from zicato.query.lineage_view import build_lineage_view  # noqa: PLC0415

    try:
        view = build_lineage_view(paths, epoch_id, include_ratings=False)
    except Exception:  # noqa: BLE001 — best-effort
        return {}
    return {
        node["generation_id"]: node
        for node in view.get("generations", [])
        if isinstance(node, dict) and isinstance(node.get("generation_id"), str)
    }


def _seed_id(nodes: dict[str, dict[str, Any]]) -> str | None:
    """The epoch's SEED — the parentless generation the reign starts from.

    A recorded parent may be absent, ``None``, or the empty string older
    workspaces write; all three mean "no parent". When an epoch records more
    than one root (a re-seeded epoch) the sorted-first id wins, matching
    :func:`~zicato.query.tournament_view._champion_lineage`'s root choice.
    """
    roots = sorted(gid for gid, node in nodes.items() if not node.get("parent_generation_id"))
    return roots[0] if roots else None


def _spine_ids(nodes: dict[str, dict[str, Any]]) -> list[str]:
    """The champion spine in reign order — the promoted chain, ANCHORED AT THE SEED.

    The seed is the epoch's baseline champion: it reigns from round 0 and keeps
    reigning until a challenger beats it. So an epoch whose challengers were all
    rejected still HAS a spine — ``[v0]`` — and a one-generation spine is a real
    trajectory (the seed's own outcome on each entry) rather than an absent
    one. Dropping it would make the dossier claim "no champion-spine
    trajectory" for an epoch whose spine is plainly recorded.

    The promoted chain itself comes from the shared
    :func:`~zicato.query.tournament_view._champion_lineage` walk; the seed is
    prepended only when the chain does not already start there.
    """
    from zicato.query.tournament_view import _champion_lineage  # noqa: PLC0415

    chain = _champion_lineage(list(nodes.values()))
    seed = _seed_id(nodes)
    if seed is not None and seed not in chain:
        chain = [seed, *chain]
    return chain


def _candidate_axis(paths: WorkspacePaths, epoch_id: str) -> list[dict[str, Any]]:
    """The ordered candidate columns for the epoch (EVAL-VIEW.md §3.1).

    Column order is ``(round_index ?? +inf, created_at, generation_id)`` — the
    index already returns generations in ``(created_at, generation_id)`` order,
    so a stable sort by ``round_index`` finishes the ordering. Each column
    carries ``promoted`` / ``seed`` / ``champion_spine`` (the seed-anchored
    reign) and the read-only rating triple.
    """
    from zicato.index.query import generations_for_epoch  # noqa: PLC0415

    try:
        rows = generations_for_epoch(paths.index_db, epoch_id)
    except Exception:  # noqa: BLE001 — best-effort
        rows = []

    # The generation graph the seed + spine are derived from: the index rows,
    # OVERLAID by the lineage nodes. Lineage is authoritative wherever it has a
    # node; the
    # index row carries a generation lineage never walked (the degrade path).
    nodes: dict[str, dict[str, Any]] = {}
    for r in rows:
        gid = _row_get(r, "generation_id")
        if isinstance(gid, str) and gid:
            nodes[gid] = {
                "generation_id": gid,
                "parent_generation_id": _row_get(r, "parent_generation_id"),
                "promoted": _opt_bool(_row_get(r, "promoted")),
            }
    nodes.update(_lineage_nodes(paths, epoch_id))
    spine = set(_spine_ids(nodes))
    seed = _seed_id(nodes)

    axis: list[dict[str, Any]] = []
    for r in rows:
        gid = _row_get(r, "generation_id")
        if not isinstance(gid, str) or not gid:
            continue
        # TRISTATE promoted (EVAL-VIEW.md §3.1), from the ONE classifier the
        # lineage payload serves — so the matrix and /api/lineage cannot disagree
        # about the same generation. An in-flight / never-raced candidate serves
        # ``null``, NEVER a collapsed ``false``, which would report an
        # undecided promotion as a rejection (:mod:`zicato.query.decisions`).
        promoted = nodes[gid].get("promoted")
        axis.append(
            {
                "generation_id": gid,
                "round_index": _opt_int(_row_get(r, "round_index")),
                "promoted": promoted if isinstance(promoted, bool) else None,
                # The epoch's baseline. It faced no gate, so it is on the spine
                # WITHOUT a promotion — the UI reads it as the seed rather
                # than as a win.
                "seed": gid == seed,
                # The reign: the promoted chain anchored at the seed (§3.1).
                "champion_spine": gid in spine,
                "decision": nodes[gid].get("decision"),
                "decision_label": nodes[gid].get("decision_label"),
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


def _aggregate_cell(rows: list[Any], draws: list[Any] | None = None) -> dict[str, Any] | None:
    """Fold ONE (entry, candidate) cell (§3.1 aggregation).

    EVIDENCE + replicate count come from the DURABLE replicate FILES
    (:func:`zicato.query.replicate_scores.cell_replicate_draws`, EVAL-VIEW.md
    §4.1) — the ``loss.json`` +
    ``loss.r<N>.json`` that actually exist — NOT the ``loss_profiles`` row count,
    which is always 1 (the table's PK is ``run_id`` = one row per (gen, entry)).
    ``pass_ratio`` / ``pass_fail`` / ``drift_loss`` / ``score`` are averaged over
    those same replicate draws. When no replicate file is on disk (a pruned
    ``runs/`` dir), the cell falls back to the single index row so an index-only
    read still renders. ``cached`` / ``latest_run_id`` stay index-derived.
    """
    draws = draws or []
    # The evidence samples: prefer the on-disk replicate files; fall back to the
    # index row(s) when the run dir was pruned. Each sample is
    # (pass_fail, drift_loss, score, runtime_ms).
    samples: list[tuple[bool | None, float | None, float | None, Any]] = []
    if draws:
        for d in draws:
            samples.append(
                (
                    _opt_bool(getattr(d, "pass_fail", None)),
                    coerce_float(getattr(d, "drift_loss", None)),
                    _opt_score_val(getattr(d, "score", None)),
                    getattr(d, "runtime_ms", None),
                )
            )
    else:
        for r in rows:
            samples.append(
                (
                    _opt_bool(_row_get(r, "pass_fail")),
                    coerce_float(_row_get(r, "drift_loss")),
                    _score_from_loss_json(r),
                    _row_get(r, "runtime_ms"),
                )
            )
    if not samples:
        return None
    bits = [s[0] for s in samples]
    drift_vals = [s[1] for s in samples if s[1] is not None]
    score_vals = [s[2] for s in samples if s[2] is not None]
    runtimes = [s[3] for s in samples]
    n = len(samples)
    # latest_run_id: the last run id in the index' (entry_id, run_id) order.
    run_ids = [_row_get(r, "run_id") for r in rows if isinstance(_row_get(r, "run_id"), str)]
    cached_any = any(bool(_row_get(r, "cached")) for r in rows) or any(
        bool(getattr(d, "cached", False)) for d in draws
    )
    return {
        "drift_loss": (sum(drift_vals) / len(drift_vals)) if drift_vals else None,
        "pass_ratio": pass_ratio(bits),
        "pass_fail": majority_verdict(bits),
        "score": (sum(score_vals) / len(score_vals)) if score_vals else None,
        "replicates": n,
        "cached": cached_any,
        "latest_run_id": run_ids[-1] if run_ids else None,
        "runtime_ms_mean": runtime_aggregates(runtimes)["mean"],
        "evidence": evidence_of(n),
    }


# ---------------------------------------------------------------------------
# Discrimination — rebound to the DURABLE matchup records (EVAL-VIEW.md §2.3)
# ---------------------------------------------------------------------------


def _reign_matchups(experiments: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """The reign's settled ``(champion, challenger)`` matchups, deduped, in record order.

    A matchup is *settled* when the challenger's experiment recorded a decision
    (it actually raced); the champion side is the experiment's
    ``parent_generation_id`` (the generation it challenged). A parentless seed
    has no matchup. Deduped on ``(champion, challenger)`` so a pooled grid read
    is done at most once per pair — the reign is bounded, so this is cheap.
    """
    from zicato.query.decisions import experiment_decision  # noqa: PLC0415

    seen: set[tuple[str, str]] = set()
    out: list[tuple[str, str]] = []
    for exp in experiments:
        if not isinstance(exp, dict) or experiment_decision(exp) is None:
            continue  # unsettled — never raced, so not a comparison
        child = exp.get("generation_id")
        champ = exp.get("parent_generation_id")
        if not (isinstance(child, str) and child and isinstance(champ, str) and champ):
            continue
        pair = (champ, child)
        if pair in seen:
            continue
        seen.add(pair)
        out.append(pair)
    return out


def _discrimination_by_entry(
    paths: WorkspacePaths, epoch_id: str, experiments: list[dict[str, Any]]
) -> dict[str, tuple[float | None, int]]:
    """Per-entry ``(rate, comparisons)`` over the reign's settled matchups (§2.3).

    Discrimination is read from the DURABLE matchup records rather than from
    ``loss_profiles`` row pairs. The ``loss_profiles`` PK is
    ``run_id`` (one row per ``(gen, entry)``, ``match_id`` last-wins at ingest),
    so "same-match row pairs" are impossible. Instead, for each settled matchup
    :func:`build_matchup_grid` reads BOTH sides' per-entry ``loss.json`` (the
    recombination builder's proven source); an entry is *compared* in a matchup
    when both sides have a usable verdict, and *discriminates* it when the two
    verdicts differ. The grid reads are pooled per pair (one per matchup). The
    per-entry ``[(matchup_key, verdict), ...]`` list is folded through the pure,
    unit-tested :func:`discrimination` so ``rate = discriminating / comparisons``
    and the second element is the both-sides comparison count. Returns
    ``{entry_id: (rate, comparisons)}``; an entry never both-sided is absent.
    """
    from zicato.query.tournament_view import build_matchup_grid  # noqa: PLC0415

    pairs_by_entry: dict[str, list[tuple[Any, bool | None]]] = {}
    for champ, child in _reign_matchups(experiments):
        grid = build_matchup_grid(paths, epoch_id, champ, child)
        key = (champ, child)
        for row in grid.get("entry_grid", []):
            eid = row.get("entry_id") if isinstance(row, dict) else None
            if not isinstance(eid, str) or not eid:
                continue
            bucket = pairs_by_entry.setdefault(eid, [])
            bucket.append((key, _opt_bool(row.get("parent_pass"))))
            bucket.append((key, _opt_bool(row.get("child_pass"))))
    return {eid: discrimination(pairs) for eid, pairs in pairs_by_entry.items()}


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
            cell = _aggregate_cell(rows, cell_replicate_draws(paths, resolved, gen, eid))
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

    # The calibration generation the flip rates ride on — served on every
    # row so the badge can show WHICH champion was calibrated, making a stale
    # (older-champion) flip rate visible; None when no calibration was measured.
    calibration_gen = calibration["generation_id"] if calibration["measured"] else None
    entries_out: list[dict[str, Any]] = []
    cells: list[list[dict[str, Any] | None]] = []
    for eid in ordered_entries:
        # Honesty: MEASURED iff a real rate was computed — an entry present
        # in the flip map but with a None rate (<2 usable draws) is NOT measured.
        measured = flips.get(eid) is not None
        entries_out.append(
            {
                "entry_id": eid,
                "slice": "holdout" if eid in holdout else "train",
                "tag": "holdout" if eid in holdout else None,
                "flip_rate": flips.get(eid) if measured else None,
                "flip_rate_measured": measured,
                "calibration_runs": calibration["runs"] if calibration["measured"] else 0,
                "calibration_generation": calibration_gen,
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
            "calibration_generation": None,
            "discrimination": None,
            "discrimination_pairs": 0,
            "runtime_ms_mean": None,
            "runtime_ms_p50": None,
            "runtime_ms_max": None,
            "replicate_total": 0,
            "cached_share": None,
        },
        "trajectory": [],
        # The empty-state REASON travels with the empty shape (issue #207 §3) so
        # a cold / unknown coordinate explains itself exactly like a real one.
        "trajectory_reason": "No records for this epoch and entry were found.",
        "attribution": {
            "first_passed_by": None,
            "regressed_by": [],
            "first_passed_reason": "No records for this epoch and entry were found.",
            "regressed_reason": "No records for this epoch and entry were found.",
        },
        "reflection_findings": [],
        "note": "no such epoch / entry",
    }


def _spine_reasons(
    trajectory: list[dict[str, Any]],
    spine_verdicts: dict[str, bool | None],
    seed_id: str | None,
    first_passed_by: str | None,
    regressed_by: list[str],
) -> dict[str, str | None]:
    """WHY the spine trajectory / attribution panels are empty (issue #207 §3).

    An empty panel that says only "nothing yet" is unactionable: it cannot be
    told apart from a panel that is empty because the epoch promoted nothing,
    because the champion never ran this entry, or because the loss records were
    pruned. Each of those is a DIFFERENT fact about the workspace, so each gets
    its own sentence — derived here, where the records are, rather than guessed
    at by the client.

    Every reason is past-tense and unhedged: a settled workspace that says
    "not yet" is claiming a future that is not coming. ``None`` on any key means
    the panel has real content and needs no explanation.
    """
    spine = [t for t in trajectory if t.get("champion_spine")]
    seed_note = f"the seed ({seed_id})" if seed_id else "the seed"

    # ── the trajectory figure: it draws drift loss along the spine ──────────
    trajectory_reason: str | None
    if not trajectory:
        trajectory_reason = "This epoch has no generations on record."
    elif not spine:
        trajectory_reason = (
            "No generation was promoted in this epoch and no seed is on record, "
            "so the epoch has no champion spine to plot."
        )
    elif any(isinstance(t.get("drift_loss"), int | float) for t in spine):
        trajectory_reason = None
    elif any((t.get("replicates") or 0) > 0 for t in spine):
        trajectory_reason = (
            "The champion spine ran this entry but recorded no drift loss — "
            "the loss records are unavailable."
        )
    else:
        names = ", ".join(str(t["generation_id"]) for t in spine)
        trajectory_reason = f"The champion spine ({names}) never ran this entry."

    # ── first-passed-by: the first spine generation to pass this entry ──────
    first_reason: str | None
    if first_passed_by is not None:
        first_reason = None
    elif not spine:
        first_reason = "No generation was promoted in this epoch, so nothing could pass it."
    elif not any(v is not None for v in spine_verdicts.values()):
        first_reason = "No champion-spine generation has a recorded verdict on this entry."
    elif len(spine) == 1 and seed_id is not None and spine[0]["generation_id"] == seed_id:
        # THE honest reading of a one-generation spine: the seed failed the
        # entry and every challenger that could have fixed it was rejected.
        first_reason = (
            f"{seed_note[0].upper()}{seed_note[1:]} did not pass this entry, "
            "and no later generation was promoted."
        )
    else:
        first_reason = "No champion-spine generation passed this entry."

    # ── regressed-by: a spine generation that flipped a pass back to a fail ─
    regressed_reason: str | None
    if regressed_by:
        regressed_reason = None
    elif not spine:
        regressed_reason = "No generation was promoted in this epoch, so nothing could regress it."
    elif len(spine) < 2:
        regressed_reason = f"A one-generation spine cannot regress — only {seed_note} ever reigned."
    else:
        regressed_reason = "No champion-spine generation regressed a prior pass on this entry."

    return {
        "trajectory": trajectory_reason,
        "first_passed": first_reason,
        "regressed": regressed_reason,
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

    Assembles the entry's A/A flip rate, its discrimination (the fraction of the
    reign's settled matchups on which this entry's verdict split the two sides —
    read from the durable matchup records, §2.3), its runtime cost
    aggregates, a per-candidate trajectory in round order, and the
    first-passed-by / regressed-by attribution along the champion spine. An
    unknown epoch/entry degrades to a same-shape payload with ``found: False``.
    """
    from zicato.query.epoch_view import _read_epoch_experiments  # noqa: PLC0415

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

    # Discrimination: bound to the DURABLE matchup records — the reign's
    # settled champion-vs-challenger matchups, each entry compared over the pairs
    # where BOTH sides have a verdict (see :func:`_discrimination_by_entry`). NOT
    # ``loss_profiles`` match_id pairs (the PK is one row per (gen, entry), so
    # those pairs cannot exist).
    experiments = _read_epoch_experiments(layout_of(paths).epoch_dir(resolved))
    disc_rate, disc_n = _discrimination_by_entry(paths, resolved, experiments).get(
        entry_id, (None, 0)
    )

    runtimes = [_row_get(r, "runtime_ms") for r in all_rows]
    rt = runtime_aggregates(runtimes)
    cached_count = sum(1 for r in all_rows if bool(_row_get(r, "cached")))
    cached_share = (cached_count / len(all_rows)) if all_rows else None
    # Honesty: MEASURED iff a real rate was computed (a None rate is unmeasured).
    measured = flips.get(entry_id) is not None

    # Trajectory + attribution along the champion spine (round order).
    trajectory: list[dict[str, Any]] = []
    first_passed_by: str | None = None
    regressed_by: list[str] = []
    prev_pass: bool | None = None
    spine_verdicts: dict[str, bool | None] = {}
    for cand in candidates:
        gen = cand["generation_id"]
        rows = per_candidate_rows.get(gen, [])
        cell = _aggregate_cell(rows, cell_replicate_draws(paths, resolved, gen, entry_id))
        bits = [_opt_bool(_row_get(r, "pass_fail")) for r in rows]
        verdict = majority_verdict(bits)
        trajectory.append(
            {
                "generation_id": gen,
                "round_index": cand["round_index"],
                "champion_spine": cand["champion_spine"],
                "seed": cand["seed"],
                "drift_loss": cell["drift_loss"] if cell else None,
                # The continuous per-entry outcome, averaged over the same
                # replicate draws as ``drift_loss``. ``None`` on a bool-only
                # board, so the trajectory figure falls back to drift.
                # Carried so a reader plotting this entry's history
                # can use the channel the contract actually populates.
                "score": cell["score"] if cell else None,
                "pass_ratio": cell["pass_ratio"] if cell else None,
                "replicates": cell["replicates"] if cell else 0,
                "cached": cell["cached"] if cell else False,
            }
        )
        # Attribution walks the champion spine only (the reign).
        if cand["champion_spine"]:
            spine_verdicts[gen] = verdict
            if verdict is not None:
                if verdict and first_passed_by is None:
                    first_passed_by = gen
                if prev_pass is True and verdict is False:
                    regressed_by.append(gen)
                prev_pass = verdict

    seed_id = next((c["generation_id"] for c in candidates if c["seed"]), None)
    reasons = _spine_reasons(trajectory, spine_verdicts, seed_id, first_passed_by, regressed_by)

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
            "calibration_generation": (
                calibration["generation_id"] if calibration["measured"] else None
            ),
            "discrimination": disc_rate,
            "discrimination_pairs": disc_n,
            "runtime_ms_mean": rt["mean"],
            "runtime_ms_p50": rt["p50"],
            "runtime_ms_max": rt["max"],
            "replicate_total": len(all_rows),
            "cached_share": cached_share,
        },
        "trajectory": trajectory,
        "trajectory_reason": reasons["trajectory"],
        "attribution": {
            "first_passed_by": first_passed_by,
            "regressed_by": regressed_by,
            "first_passed_reason": reasons["first_passed"],
            "regressed_reason": reasons["regressed"],
        },
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
    paths: WorkspacePaths,
    epoch_id: str,
    candidates: list[dict[str, Any]],
    experiments: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Per-entry discrimination + runtime, folded once across every candidate.

    Returns ``{entry_id: {discrimination, discrimination_pairs, runtime_ms_mean,
    replicate_total}}``. Discrimination is the DURABLE matchup-record binding
    (:func:`_discrimination_by_entry`, §2.3) — the same source the dossier
    reads, so the two surfaces agree — NOT ``loss_profiles`` match_id pairs (the
    PK forbids them). Runtime + ``replicate_total`` fold the index loss rows in
    one pass. The matchup grid reads are pooled inside
    :func:`_discrimination_by_entry`; this feeds the threadpool-wrapped endpoint.
    """
    disc_by_entry = _discrimination_by_entry(paths, epoch_id, experiments)
    runtimes: dict[str, list[Any]] = {}
    totals: dict[str, int] = {}
    for cand in candidates:
        for r in _rows_for_candidate(paths, epoch_id, cand["generation_id"]):
            eid = _row_get(r, "entry_id")
            if not isinstance(eid, str) or not eid:
                continue
            totals[eid] = totals.get(eid, 0) + 1
            runtimes.setdefault(eid, []).append(_row_get(r, "runtime_ms"))

    out: dict[str, dict[str, Any]] = {}
    for eid in set(totals) | set(disc_by_entry):
        rate, n_pairs = disc_by_entry.get(eid, (None, 0))
        out[eid] = {
            "discrimination": rate,
            "discrimination_pairs": n_pairs,
            "runtime_ms_mean": runtime_aggregates(runtimes.get(eid, []))["mean"],
            "replicate_total": totals.get(eid, 0),
        }
    return out


def _rotation_status(
    paths: WorkspacePaths, epoch_id: str, experiments: list[dict[str, Any]]
) -> dict[str, Any]:
    """Rotation-cadence status — BOUND to the shipped surfaces rather than re-derived (§5).

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
    pointer into ``reflect`` / the builder rather than an action. A never-indexed
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
    experiments = _read_epoch_experiments(layout_of(paths).epoch_dir(resolved))
    instrument = _instrument_by_entry(paths, resolved, candidates, experiments)

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
    "_empty_dossier",
    "_empty_health",
    "_empty_matrix",
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


# ---------------------------------------------------------------------------
# Facet slices — board entries grouped by their `facet:` tag
# ---------------------------------------------------------------------------

#: The reserved ``BoardEntry.tags`` prefix that puts an entry in a named
#: diagnostic slice. ``facet:data_cleaning`` names the ``data_cleaning``
#: facet. Metadata tags (``smoke``, ``single_turn``) and the reserved
#: ``holdout`` tag are NOT facets — see BOARD-FORMAT.md §1.4.
FACET_TAG_PREFIX = "facet:"


def facets_by_entry(paths: WorkspacePaths, epoch_id: str) -> dict[str, tuple[str, ...]]:
    """``{entry_id: (facet_name, ...)}`` off the frozen board, best-effort.

    Reads through the tolerant raw scan, so one stale entry costs its own
    row rather than the whole payload.
    """
    from zicato.query.board_scan import (  # noqa: PLC0415
        board_entry_id,
        board_entry_tags,
        iter_board_rows,
    )

    out: dict[str, tuple[str, ...]] = {}
    for row in iter_board_rows(layout_of(paths).board(epoch_id)):
        entry_id = board_entry_id(row)
        if entry_id is None:
            continue
        names = sorted(
            {
                t[len(FACET_TAG_PREFIX) :]
                for t in board_entry_tags(row)
                if t.startswith(FACET_TAG_PREFIX) and len(t) > len(FACET_TAG_PREFIX)
            }
        )
        if names:
            out[entry_id] = tuple(names)
    return out


def _generation_loss_profiles(
    paths: WorkspacePaths, epoch_id: str, generation_id: str
) -> list[Any]:
    """Hydrate one generation's persisted ``loss.json`` files, best-effort.

    Reads the run directories rather than the index, for the same reason
    :func:`zicato.query.tournament_view.build_matchup_grid` does: the files
    are canonical, so a completed generation's per-entry losses are
    recoverable even when the index was never built. A missing or malformed
    profile is skipped; nothing raises.
    """
    from zicato.telemetry.reducer import loss_profile_from_dict  # noqa: PLC0415

    runs_dir = layout_of(paths).runs_dir(epoch_id, generation_id)
    if not runs_dir.is_dir():
        return []
    out: list[Any] = []
    for run_dir in sorted(runs_dir.iterdir()):
        if not run_dir.is_dir():
            continue
        raw = _read_json_value(run_dir / "loss.json")
        if not isinstance(raw, dict):
            continue
        try:
            out.append(loss_profile_from_dict(raw))
        except Exception:  # noqa: BLE001 — best-effort; a torn profile is skipped
            continue
    return out


def _epoch_scoring_weights(paths: WorkspacePaths, epoch_id: str) -> Any:
    """The epoch's FROZEN ``ScoringWeights``, or the defaults.

    The facet scalar must be computed at the same weights the candidate's
    own scalar was, or the two are not comparable — which is the whole
    point of reporting them side by side. A missing / malformed
    ``scoring.json`` degrades to the dataclass defaults rather than
    raising — the facet numbers stay readable, they are simply computed at
    the defaults rather than the epoch's own weights.
    """
    from zicato.core import ScoringWeights  # noqa: PLC0415
    from zicato.workspace_loader import scoring_weights_from_dict  # noqa: PLC0415

    raw = _read_json_value(layout_of(paths).scoring(epoch_id))
    if not isinstance(raw, dict):
        return ScoringWeights()
    try:
        return scoring_weights_from_dict(raw)
    except Exception:  # noqa: BLE001 — best-effort; defaults keep the read alive
        return ScoringWeights()


def facet_scores_for_generation(
    paths: WorkspacePaths,
    epoch_id: str,
    generation_id: str,
    facets_by_entry_map: dict[str, tuple[str, ...]] | None = None,
) -> dict[str, Any]:
    """Per-``facet:`` aggregates for one candidate, on the SAME terms as its own.

    Returns::

        {"facets": {name: {scalar, mean_score, scored_count, entry_count}},
         "overall": {scalar, mean_score, scored_count, entry_count} | None}

    Each facet runs :func:`~zicato.tournament.scoring.aggregate_generation_score`
    over just the entries carrying that tag, at the epoch's FROZEN weights.
    So a facet's ``scalar`` is the same quantity, in the same units and the
    same direction (a loss — lower is better), as the candidate's overall
    ``scalar``: it folds the drift term (including every custom judge's
    weighted contribution), the outcome miss, and the namespace terms. That
    comparability is the point — a facet scalar can be read directly against
    the ``overall`` row, which is the same aggregate over every entry.

    THE TRAIN SLICE rather than the whole board. The number the gate compares and
    the number ``gen_score.json`` caches are BOTH the train-slice aggregate
    (:func:`zicato.tournament.governance._train_aggs`), because the holdout
    is confirm-only and default-on: a board of six or more entries hands
    ~30% of itself to the holdout with no tag in sight. Aggregating the
    whole board here would put a second, larger "candidate scalar" on the
    same screen as the gate's — measurably different, identically labelled.
    So holdout entries are excluded from every block, and ``overall`` is
    exactly the candidate's own headline number. A facet is then that
    number restricted to a slice, which is the only reading that makes the
    side-by-side comparison mean anything.

    The exclusion is also what keeps this screen out of the holdout. The
    holdout is meant to stay un-mined, and the Ladder governs how often the
    loop may query it (OVERFITTING.md §4); a dossier that broke the holdout
    out by facet on every page load would be an ungoverned query against it,
    read by the operator who steers the next proposal.

    Two consequences worth naming. A facet whose train entries all failed to
    run keeps its row, with null numbers and ``ran_count`` zero — dropping it
    would make an unrun slice indistinguishable from an untagged one, the
    exact collapse this payload exists to prevent. A facet whose entries are
    ALL in the holdout has no scored slice at all and reports no row; it has
    nothing to say about the number the gate compares.

    ``mean_score`` is the outcome axis (higher is better, ``[0, 1]``), the
    same field the generation's own aggregate carries. ``None`` when nothing
    in the slice produced an outcome — an absent measurement, never a
    fabricated ``0.0`` (EVAL-VIEW.md §3).

    ``scored_count`` is ``mean_score``'s denominator, ``ran_count`` the
    scalar's, and ``entry_count`` the slice's size on the board. All three
    travel because a facet is a SLICE: a racing rung that ran a board subset
    can thin one to a single entry, and a scalar over one entry must not
    read like a scalar over twenty. The weights were calibrated board-wide,
    so a thin facet's scalar is noisy — the counts are what make that
    visible.

    NOT threaded: the opt-in ``diff_complexity`` term, which the gate folds
    into the CHALLENGER's aggregate only (from its diff size). At the
    default weight of ``0.0`` the term is absent from both and nothing
    differs; under a non-zero weight a facet scalar omits a per-candidate
    constant that the headline scalar carries. Per-slice parsimony is not a
    defined quantity — a diff is not attributable to a board tag — so the
    term is left out rather than invented.

    DIAGNOSTIC ONLY. Nothing here feeds the scalar the gate reads, the gate
    itself, scheduling, or Pareto admission. A facet number carries no noise
    threshold; making one drive a decision means first measuring that
    decision's error rates (04-evaluation-statistics.md §3.2).

    ``facets_by_entry_map`` is an optional prebuilt
    ``{entry_id: (facet, ...)}`` — pass it when the caller already read the
    board, so one request does not walk ``board.jsonl`` twice.

    Best-effort: an unreadable board or absent run files yield
    ``{"facets": {}, "overall": None}`` and the dossier's facet table simply
    does not paint.
    """
    from zicato.tournament.scoring import aggregate_generation_score  # noqa: PLC0415

    empty: dict[str, Any] = {"facets": {}, "overall": None}
    # The caller may already hold the map (the dossier feed stamps each entry
    # row with its facets), so accept it and skip a second board read.
    if facets_by_entry_map is None:
        facets_by_entry_map = facets_by_entry(paths, epoch_id)
    if not facets_by_entry_map:
        return empty
    losses = _generation_loss_profiles(paths, epoch_id, generation_id)
    if not losses:
        return empty
    weights = _epoch_scoring_weights(paths, epoch_id)

    # The gate's split, read the way every other eval_view surface reads it,
    # so a facet row and the per-entry `slice` badge beside it can never
    # disagree about which entries are held out.
    board_entries = _load_board_entries(paths, epoch_id)
    holdout = _holdout_ids(paths, epoch_id, board_entries)
    board_ids = {e.id for e in board_entries}

    def _is_train(entry_id: str) -> bool:
        """Does this entry feed the scored (train) slice?

        An id the board load did not yield is treated as train: the loader
        VALIDATES, so it blanks on one stale row, and a blank board must not
        silently reclassify the whole workspace as held out.
        """
        return entry_id not in holdout

    losses = [loss for loss in losses if _is_train(str(getattr(loss, "entry_id", "")))]
    if not losses:
        return empty

    def _block(subset: list[Any], tagged: int) -> dict[str, Any]:
        """One aggregate block. ``tagged`` is the slice's size ON THE BOARD.

        ``tagged`` is passed in rather than derived from ``subset`` because a
        tagged entry that never RAN is absent from ``subset`` entirely. Sizing
        the slice by what ran would report a slice as fully covered while
        hiding the entries missing from it — which is the one thing these
        counts exist to expose (a racing rung runs a board subset).

        An EMPTY subset still returns a block, with null numbers and
        ``ran_count`` zero. A slice can empty out legitimately (its entries
        all landed in the holdout, or none of them ran) and dropping the row
        would make that indistinguishable from a facet nobody ever tagged —
        the collapse this whole payload exists to prevent.
        """
        agg: dict[str, Any] = {}
        if subset:
            try:
                agg = aggregate_generation_score(subset, weights)
            except Exception:  # noqa: BLE001 — best-effort; a bad slice reads null
                agg = {}
        scored = int(agg.get("expectation_count") or 0)
        return {
            # The scalar is a LOSS and unbounded above, so it goes through the
            # plain finite-float guard — NOT ``_opt_score_val``, whose contract
            # is a score in ``[0, 1]``.
            "scalar": coerce_float(agg.get("scalar")) if agg else None,
            # ``mean_score`` reports 1.0 by convention when NOTHING was
            # scored (so the (1 - mean) term contributes zero). That is the
            # right default for the scalar and the wrong one to display, so
            # the honest ``None`` replaces it here.
            "mean_score": _opt_score_val(agg.get("mean_score")) if scored else None,
            "scored_count": scored,
            # Entries the board puts in this slice — including any that did
            # not run. ``ran_count`` is how many of them produced a profile,
            # and is the scalar's own denominator.
            "entry_count": tagged,
            "ran_count": len(subset),
        }

    by_facet: dict[str, list[Any]] = {}
    for loss in losses:
        for name in facets_by_entry_map.get(getattr(loss, "entry_id", ""), ()):
            by_facet.setdefault(name, []).append(loss)

    # The slice sizes come from the BOARD, minus the holdout — the same
    # entries the aggregate above is allowed to see. So two facets look alike
    # only when they are alike: one whose entries all ran cannot be confused
    # with one whose entries mostly did not.
    tagged_count: dict[str, int] = {}
    for entry_id, names in facets_by_entry_map.items():
        if not _is_train(entry_id):
            continue
        for name in names:
            tagged_count[name] = tagged_count.get(name, 0) + 1

    # Every facet the board declares gets a row, including one whose train
    # slice is empty — `by_facet` alone would omit exactly the slices whose
    # absence is the finding.
    declared = sorted(set(by_facet) | set(tagged_count))
    facets = {name: _block(by_facet.get(name, []), tagged_count.get(name, 0)) for name in declared}
    if not facets:
        return empty
    # ``overall`` is the candidate's own aggregate — the row every facet is
    # read against — so it is sized by the whole TRAIN slice of the board,
    # not by what happened to run. A `3/3` overall above a `1/4` facet would
    # claim a complete board while the slices beneath it say otherwise.
    train_size = len([eid for eid in board_ids if _is_train(eid)]) or len(losses)
    return {"facets": facets, "overall": _block(losses, train_size)}
