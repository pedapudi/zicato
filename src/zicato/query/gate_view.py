"""gate_view — extracted from the former dashboard state_reader monolith (pure move)."""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from zicato.query._sqlite import (
    _IndexAbsent,
    _query,
    open_index_ro,
)
from zicato.query.judge_view import build_per_judge_comparison
from zicato.query.lineage_view import build_lineage_view
from zicato.query.paths import (
    WorkspacePaths,
    _read_json_value,
    _resolve_epoch_id,
    coerce_float,
    coerce_numeric_dict,
    layout_of,
    read_current_epoch,
)
from zicato.query.tournament_view import (
    _read_gen_score,
    _read_run_loss_files,
)


def _latest_round_report(directory: Path) -> Path | None:
    if not directory.is_dir():
        return None
    best: tuple[int, Path] | None = None
    for entry in directory.iterdir():
        name = entry.name
        if not name.startswith("round_") or not name.endswith(".json"):
            continue
        num = name[len("round_") : -len(".json")]
        try:
            n = int(num)
        except ValueError:
            continue
        if best is None or n > best[0]:
            best = (n, entry)
    return best[1] if best is not None else None


# ---------------------------------------------------------------------------
# Score trajectory — the environment-wide evolution curve
# ---------------------------------------------------------------------------


def _mean_drift_loss_per_generation(
    conn: sqlite3.Connection, epoch_id: str | None, generation_id: str
) -> tuple[float | None, int]:
    """Return ``(mean_drift_loss, entry_count)`` for one generation.

    A generation can appear in more than one tournament — it is
    re-scored whenever it serves as a later round's champion — so the
    index carries several ``loss_profiles`` rows for the same
    ``(generation_id, entry_id)`` pair, and the index does not record a
    usable per-run timestamp to order them by. To stay deterministic
    regardless of row order, the aggregate is computed in two stages:

    1. Per board entry, average that entry's ``drift_loss`` across every
       run of it (so an entry run twice contributes its mean, not a
       row-order-dependent pick).
    2. The generation's scalar is the mean of those per-entry means.

    Aborted runs ARE included: an aborted run carries a real,
    definite worst-case ``drift_loss`` (the runner synthesises one),
    and the tournament gate's scalar aggregates every entry — excluding
    aborted runs would understate the curve and misrepresent the
    evolution the gate actually saw.

    Returns ``(None, 0)`` when the generation has no loss profiles.
    """
    rows = _query(
        conn,
        "SELECT entry_id, drift_loss FROM loss_profiles "
        "WHERE generation_id = ? AND epoch_id = ?",
        (generation_id, epoch_id),
    )
    per_entry: dict[str, list[float]] = {}
    for r in rows:
        if r["drift_loss"] is None:
            continue
        per_entry.setdefault(r["entry_id"], []).append(float(r["drift_loss"]))
    if not per_entry:
        return None, 0
    entry_means = [sum(v) / len(v) for v in per_entry.values()]
    return sum(entry_means) / len(entry_means), len(entry_means)


def _finite_number(value: Any) -> float | None:
    """Return a finite numeric value, or ``None`` for an unusable one."""
    if isinstance(value, bool):
        return None
    if not isinstance(value, int | float):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _pareto_objectives(
    aggregate: dict[str, Any], profile: Mapping[str, str] | None = None
) -> dict[str, float]:
    """Change a stored generation aggregate into lower-is-better axes.

    This function does not use ``scalar``. The scalar is one weighting of
    these dimensions that the operator selected. That weighting hides the
    trade-offs, but a Pareto frontier must keep them. Each namespace
    aggregate already includes its configured sign. Thus each axis that
    this function returns has the same direction.

    ``profile`` is the objective set that the operator declared
    (:attr:`ScoringWeights.pareto_objectives`, ``{axis_key: label}``). If
    ``profile`` is empty or ``None``, each axis is an objective. This is
    the behavior from before the profile. If ``profile`` is not empty, the
    function keeps only the declared axes. Thus an axis that the operator
    did not declare cannot make a generation dominated.
    """
    objectives: dict[str, float] = {}
    drift = _finite_number(aggregate.get("drift_loss_mean"))
    if drift is not None:
        objectives["drift_loss"] = drift

    mean_score = _finite_number(aggregate.get("mean_score"))
    if mean_score is None:
        # Pre-continuous-score aggregates expose the equivalent binary axis.
        mean_score = _finite_number(aggregate.get("pass_rate"))
    if mean_score is not None:
        objectives["quality_loss"] = 1.0 - mean_score

    namespaces = aggregate.get("namespace_aggregates")
    if isinstance(namespaces, dict):
        for namespace, value in namespaces.items():
            number = _finite_number(value)
            if number is not None:
                objectives[f"namespace:{namespace}"] = number

    if profile:
        objectives = {axis: v for axis, v in objectives.items() if axis in profile}
    return objectives


def _frontier_axes(points: list[dict[str, Any]], profile: Mapping[str, str] | None) -> list[str]:
    """Give the set of axes for the frontier.

    If there is no declared ``profile``, the result is each axis in the
    data, in sorted sequence. This is the behavior from before the profile.

    If there is a ``profile``, the result is the declared axes, in the
    sequence of their declaration. The function removes each axis that the
    data does not supply. An axis that no generation reports has no data.
    If the function kept such an axis, no point could be comparable.
    """
    present = {axis for point in points for axis in point.get("objectives", {})}
    if profile:
        return [axis for axis in profile if axis in present]
    return sorted(present)


def _objective_labels(axes: list[str], profile: Mapping[str, str] | None) -> dict[str, str]:
    """Give the label to show for each axis.

    The label is the one that the operator declared. If the operator
    declared no label, the label is the axis key.

    The result includes each axis in ``axes``. Thus a client can label the
    frontier, and it does not need to know if there is a declared profile.
    If a declared label is empty, the result uses the axis key. Thus the
    client does not show an empty label.
    """
    labels: dict[str, str] = {}
    for axis in axes:
        declared = (profile or {}).get(axis, "")
        labels[axis] = declared.strip() or axis
    return labels


def _annotate_pareto_frontier(
    points: list[dict[str, Any]], profile: Mapping[str, str] | None = None
) -> tuple[list[str], list[str]]:
    """Mark the comparable points and give ``(axes, frontier_ids)``.

    A point is comparable only if it supplies each axis in the set of axes
    of the frontier. If a point does not supply an axis, the function does
    not rank it, and ``pareto_optimal`` is ``None``.

    The function does not use zero for an axis that a point does not
    supply. On a lower-is-better axis, zero is the best possible value.
    Thus a generation from before an axis would incorrectly dominate each
    generation that reports that axis.

    If a generation has no stored aggregate, the function also does not
    rank it. The function does not invent a result.
    """
    axes = _frontier_axes(points, profile)
    comparable = [
        point
        for point in points
        if point.get("objectives") and all(axis in point["objectives"] for axis in axes)
    ]
    frontier: list[str] = []
    for point in comparable:
        vector = point["objectives"]
        dominators: list[str] = []
        for other in comparable:
            if other is point:
                continue
            other_vector = other["objectives"]
            no_worse = all(other_vector[axis] <= vector[axis] for axis in axes)
            strictly_better = any(other_vector[axis] < vector[axis] for axis in axes)
            if no_worse and strictly_better:
                dominators.append(str(other["generation_id"]))
        point["dominated_by"] = sorted(dominators)
        point["pareto_optimal"] = not dominators
        if not dominators:
            frontier.append(str(point["generation_id"]))
    comparable_ids = {id(point) for point in comparable}
    for point in points:
        if id(point) not in comparable_ids:
            point["dominated_by"] = []
            point["pareto_optimal"] = None
    return axes, frontier


def build_score_trajectory(paths: WorkspacePaths, epoch_id: str | None = None) -> dict[str, Any]:
    """``GET /api/score-trajectory`` — scalar trajectory plus Pareto frontier.

    ``epoch_id`` defaults to the current epoch; a validated id scopes the
    trajectory to that epoch's generations instead.

    The environment-wide evolution curve: one point per generation, in
    lineage (creation) order, plotting the generation's aggregate
    drift-loss scalar (the dominant term of the tournament scalar — the
    quantity the gate compares, lower is better).

    The per-generation scalar is computed by
    :func:`_mean_drift_loss_per_generation` — a deterministic,
    row-order-independent mean of per-entry mean ``drift_loss`` that
    includes aborted runs (they carry a real worst-case loss the gate
    scalar uses). A generation with no loss profiles yet yields
    ``scalar = None`` — still plotted as a gap rather than dropped, so
    the x-axis stays continuous across the lineage.

    Each point also has an ``objectives`` vector, a ``pareto_optimal``
    flag, and a ``dominated_by`` list. Each axis is lower-is-better. At the
    level of the response, ``objective_names`` and ``pareto_frontier`` give
    each generation that is not dominated. These are independent of the
    single weighted scalar. ``objective_labels`` gives the label to show
    for each axis.

    If the operator declared a :attr:`ScoringWeights.pareto_objectives`
    profile for the epoch, the set of axes comes from that profile. If
    not, the set of axes is each axis in the data. If a generation does
    not report each axis, the function does not rank it, and
    ``pareto_optimal`` is ``None``. The function does not assume the best
    possible value for an axis that the generation does not report.

    If the index is not available, the function gives an empty ``points``
    list. It does not raise an exception.
    """
    epoch_id = _resolve_epoch_id(paths, epoch_id)
    # The objective profile that the operator declared. It comes from the
    # scoring contract of the epoch. If it is empty (the default), each axis
    # is an objective. This is the behavior from before the profile.
    profile: Mapping[str, str] = getattr(
        _read_epoch_scoring_weights(paths, str(epoch_id or "")), "pareto_objectives", {}
    )
    # Lineage order is authoritative for the x-axis — the index's
    # ``generations`` rows can carry empty ``created_at`` strings.
    lineage = build_lineage_view(paths, include_ratings=False)
    ordered = [
        g
        for g in lineage.get("generations", [])
        if epoch_id is None or g.get("epoch_id") == epoch_id
    ]

    try:
        with open_index_ro(paths.index_db) as conn:
            points: list[dict[str, Any]] = []
            for g in ordered:
                gid = g["generation_id"]
                scalar, entry_count = _mean_drift_loss_per_generation(conn, g.get("epoch_id"), gid)
                aggregate = _read_gen_score(paths, str(g.get("epoch_id") or ""), gid)
                points.append(
                    {
                        "generation_id": gid,
                        "parent_generation_id": g.get("parent_generation_id"),
                        "promoted": g.get("promoted"),
                        "scalar": scalar,
                        "entry_count": entry_count,
                        "created_at": g.get("created_at"),
                        "objectives": _pareto_objectives(aggregate, profile),
                    }
                )
            objective_names, frontier = _annotate_pareto_frontier(points, profile)
            return {
                "epoch_id": epoch_id,
                "points": points,
                "objective_names": objective_names,
                "objective_labels": _objective_labels(objective_names, profile),
                "pareto_frontier": frontier,
            }
    except _IndexAbsent:
        points = [
            {
                "generation_id": g["generation_id"],
                "parent_generation_id": g.get("parent_generation_id"),
                "promoted": g.get("promoted"),
                "scalar": None,
                "entry_count": 0,
                "created_at": g.get("created_at"),
                "objectives": _pareto_objectives(
                    _read_gen_score(paths, str(g.get("epoch_id") or ""), g["generation_id"]),
                    profile,
                ),
            }
            for g in ordered
        ]
        objective_names, frontier = _annotate_pareto_frontier(points, profile)
        return {
            "epoch_id": epoch_id,
            "points": points,
            "objective_names": objective_names,
            "objective_labels": _objective_labels(objective_names, profile),
            "pareto_frontier": frontier,
            "note": "index not built; run zicato reindex",
        }
    except sqlite3.Error:
        return {
            "epoch_id": epoch_id,
            "points": [],
            "objective_names": [],
            "objective_labels": {},
            "pareto_frontier": [],
        }


# ---------------------------------------------------------------------------
# Drift-kind movements — champion -> challenger per-kind count deltas
# ---------------------------------------------------------------------------


def _drift_counts_for_generation(
    conn: sqlite3.Connection, epoch_id: str | None, generation_id: str
) -> dict[str, int]:
    """Per-drift-kind event totals for one generation, averaged per entry.

    Returns ``{drift_kind: total_count}`` where ``total_count`` is the
    sum, over every board entry the generation ran, of that entry's
    *mean* drift count for the kind (averaged across the entry's runs,
    rounded). Averaging per entry — rather than summing raw rows — keeps
    a generation that was re-scored across two tournaments (duplicate
    ``loss_profiles`` rows) from double-counting its drift, exactly as
    :func:`_mean_drift_loss_per_generation` does for the scalar. The
    drift kind is the bare wire string (``metric_counts.name`` with the
    ``"drift:"`` namespace prefix stripped, including ``custom:<judge>``
    namespaced custom-judge kinds).

    Aborted runs are included: a run that drifted and then aborted
    still produced real drift events the movements view must reflect.
    A generation with no drift events yields an empty mapping.
    """
    # entry_id -> run_id -> {kind: count}. Two index hops: which runs
    # belong to the generation, then those runs' drift metric rows.
    run_rows = _query(
        conn,
        "SELECT entry_id, run_id FROM loss_profiles " "WHERE generation_id = ? AND epoch_id = ?",
        (generation_id, epoch_id),
    )
    runs_by_entry: dict[str, set[str]] = {}
    for r in run_rows:
        runs_by_entry.setdefault(r["entry_id"], set()).add(r["run_id"])
    all_run_ids = {rid for rids in runs_by_entry.values() for rid in rids}
    if not all_run_ids:
        return {}

    placeholders = ",".join("?" for _ in all_run_ids)
    metric_rows = _query(
        conn,
        f"SELECT run_id, name, count FROM metric_counts "
        f"WHERE namespace = 'drift' AND run_id IN ({placeholders})",
        tuple(all_run_ids),
    )
    per_run: dict[str, dict[str, int]] = {}
    for r in metric_rows:
        name = str(r["name"] or "")
        kind = name[len("drift:") :] if name.startswith("drift:") else name
        if not kind:
            continue
        bucket = per_run.setdefault(r["run_id"], {})
        bucket[kind] = bucket.get(kind, 0) + int(r["count"] or 0)

    # Per entry: mean count per kind across the entry's runs; then sum
    # those per-entry means across entries.
    totals: dict[str, float] = {}
    for run_ids in runs_by_entry.values():
        entry_kind_sums: dict[str, int] = {}
        for rid in run_ids:
            for kind, cnt in per_run.get(rid, {}).items():
                entry_kind_sums[kind] = entry_kind_sums.get(kind, 0) + cnt
        n_runs = len(run_ids) or 1
        for kind, total in entry_kind_sums.items():
            totals[kind] = totals.get(kind, 0.0) + total / n_runs
    return {kind: round(v) for kind, v in totals.items() if round(v) != 0}


def build_drift_movements(paths: WorkspacePaths, generation_id: str) -> dict[str, Any]:
    """``GET /api/drift-movements/:generation_id`` — champion->challenger drift deltas.

    For the tournament that produced ``generation_id`` (the challenger),
    compares the per-drift-kind event counts of the champion (parent)
    against the challenger and reports the movement of each kind.

    Returns ``{"epoch_id", "generation_id", "champion", "challenger",
    "movements": [{kind, champion_count, challenger_count, delta,
    direction}], "note"?}`` where ``direction`` is ``"worsened"`` (more
    drift on the challenger), ``"improved"`` (fewer), or ``"unchanged"``.
    Movements are sorted by descending ``|delta|`` so the biggest
    regressions and improvements surface first. A kind absent from one
    side counts as zero there.

    Degrades to an empty ``movements`` list (never raises) when the
    index, the tournament, or the parent generation cannot be resolved.
    """
    epoch_id = read_current_epoch(paths)
    empty: dict[str, Any] = {
        "epoch_id": epoch_id,
        "generation_id": generation_id,
        "champion": None,
        "challenger": generation_id,
        "movements": [],
    }
    try:
        with open_index_ro(paths.index_db) as conn:
            tour = _query(
                conn,
                "SELECT parent_generation_id, child_generation_id FROM tournaments "
                "WHERE child_generation_id = ? LIMIT 1",
                (generation_id,),
            )
            if not tour:
                return {**empty, "note": "no tournament found for this generation"}
            parent_id = tour[0]["parent_generation_id"]
            child_id = tour[0]["child_generation_id"]

            champion_counts = _drift_counts_for_generation(conn, epoch_id, parent_id)
            challenger_counts = _drift_counts_for_generation(conn, epoch_id, child_id)

            movements: list[dict[str, Any]] = []
            for kind in sorted(set(champion_counts) | set(challenger_counts)):
                champ = champion_counts.get(kind, 0)
                chall = challenger_counts.get(kind, 0)
                delta = chall - champ
                if delta > 0:
                    direction = "worsened"
                elif delta < 0:
                    direction = "improved"
                else:
                    direction = "unchanged"
                movements.append(
                    {
                        "kind": kind,
                        "champion_count": champ,
                        "challenger_count": chall,
                        "delta": delta,
                        "direction": direction,
                    }
                )
            # Biggest absolute movements first; ties broken alphabetically.
            movements.sort(key=lambda m: (-abs(m["delta"]), m["kind"]))
            return {
                "epoch_id": epoch_id,
                "generation_id": generation_id,
                "champion": parent_id,
                "challenger": child_id,
                "movements": movements,
            }
    except _IndexAbsent:
        return {**empty, "note": "index not built; run zicato reindex"}
    except sqlite3.Error:
        return empty


def build_health_report(paths: WorkspacePaths) -> dict[str, Any]:
    """``GET /api/health-report`` — the latest loop-health report."""
    epoch_id = read_current_epoch(paths)
    healthy_empty: dict[str, Any] = {
        "epoch_id": epoch_id,
        "findings": [],
        "healthy": True,
    }
    if epoch_id is None:
        return healthy_empty
    latest = _latest_round_report(paths.epoch_health_dir(epoch_id))
    if latest is None:
        return healthy_empty
    value = _read_json_value(latest)
    if not isinstance(value, dict):
        return healthy_empty
    report: dict[str, Any] = {
        "epoch_id": value.get("epoch_id") if isinstance(value.get("epoch_id"), str) else epoch_id,
        "findings": value.get("findings") if isinstance(value.get("findings"), list) else [],
        "healthy": value.get("healthy") if isinstance(value.get("healthy"), bool) else True,
    }
    checked_at = value.get("checked_at")
    if isinstance(checked_at, str):
        report["checked_at"] = checked_at
    return report


# ---------------------------------------------------------------------------
# Promote-gate breakdown (L3 decision view).
#
# The gate logic is authoritative in :mod:`zicato.tournament.gate`. This
# reader reconstructs the SAME decision the runner recorded by feeding the
# real :func:`evaluate_gate` (and its helpers) the champion / challenger
# aggregates read off disk, then decomposes the verdict into the gate's
# ordered rules with per-rule status. It never re-implements a threshold.
# ---------------------------------------------------------------------------


def _read_epoch_scoring_weights(paths: WorkspacePaths, epoch_id: str) -> Any:
    """Build the epoch's :class:`ScoringWeights` from its ``scoring.json``.

    The shipped ``workspace_loader`` / ``lifecycle`` parsers intentionally
    drop the gate-only fields (``regression_gate_enabled``,
    ``namespace_weights``, ``namespace_monotonicity``) — they only need the
    scalar weights. The gate breakdown DOES need them, so this reader maps
    every gate-relevant key through, falling back to the dataclass defaults
    when ``scoring.json`` is a partial / legacy document (or absent).
    """
    from zicato.core import ScoringWeights  # noqa: PLC0415

    raw = _read_json_value(layout_of(paths).scoring(epoch_id))
    if not isinstance(raw, dict):
        return ScoringWeights()

    defaults = ScoringWeights()
    kwargs: dict[str, Any] = {}

    if isinstance(raw.get("promote_margin"), int | float):
        kwargs["promote_margin"] = float(raw["promote_margin"])
    if "pass_rate_monotonicity" in raw:
        kwargs["pass_rate_monotonicity"] = bool(raw["pass_rate_monotonicity"])
    raw_scope = raw.get("pass_rate_monotonicity_scope")
    if raw_scope in ("per_entry", "aggregate"):
        kwargs["pass_rate_monotonicity_scope"] = raw_scope
    if "regression_gate_enabled" in raw:
        kwargs["regression_gate_enabled"] = bool(raw["regression_gate_enabled"])

    raw_ns_w = raw.get("namespace_weights")
    if isinstance(raw_ns_w, dict):
        try:
            kwargs["namespace_weights"] = {str(k): float(v) for k, v in raw_ns_w.items()}
        except (TypeError, ValueError):
            pass
    raw_ns_m = raw.get("namespace_monotonicity")
    if isinstance(raw_ns_m, dict):
        kwargs["namespace_monotonicity"] = {str(k): bool(v) for k, v in raw_ns_m.items()}
    raw_objectives = raw.get("pareto_objectives")
    if isinstance(raw_objectives, dict):
        kwargs["pareto_objectives"] = {str(k): str(v) for k, v in raw_objectives.items()}

    try:
        return ScoringWeights(**kwargs)
    except TypeError:
        return defaults


def _gen_agg_for_gate(
    paths: WorkspacePaths, epoch_id: str, generation_id: str
) -> dict[str, Any] | None:
    """Assemble the aggregate dict :func:`evaluate_gate` consumes.

    Prefers the cached ``gen_score.json`` (the persisted
    :func:`aggregate_generation_score` output, which already carries
    ``scalar`` / ``pass_rate`` / ``per_entry`` / ``namespace_aggregates`` /
    ``scalar_components``). When ``per_entry`` is missing it is
    reconstructed from the per-run ``loss.json`` files so the pass-rate
    monotonicity rule can still be judged. Returns ``None`` only when there
    is no scalar to compare at all (the rule set then degrades to unknown).
    """
    score = _read_gen_score(paths, epoch_id, generation_id)
    if not isinstance(score, dict):
        score = {}

    agg: dict[str, Any] = dict(score)

    if not isinstance(agg.get("per_entry"), dict):
        # Reconstruct {entry_id: {drift_loss, pass_fail, score}} from loss
        # files so the monotonicity rule has the two points it compares.
        # ``score`` is the continuous per-entry outcome; ``None`` (or
        # absent) falls back to the bool ``pass_fail`` in the gate's reader.
        loss_files = _read_run_loss_files(paths, epoch_id, generation_id)
        per_entry: dict[str, dict[str, Any]] = {}
        for entry_id, cell in loss_files.items():
            per_entry[entry_id] = {
                "drift_loss": cell.get("drift_loss"),
                "pass_fail": cell.get("pass_fail"),
                "score": cell.get("score"),
            }
        if per_entry:
            agg["per_entry"] = per_entry

    if not isinstance(agg.get("scalar"), int | float):
        return None
    return agg


def _parse_scoring_provenance(token: str | None) -> dict[str, Any]:
    """Decompose a scoring provenance token into a structured component view.

    The two scoring seams (#19) each emit a parseable provenance string the
    runner records on disk: the per-generation ``scalar_provenance`` (Seam 2,
    in ``gen_score.json``) and the per-run ``scoring_provenance`` (Seam 1, in
    each ``loss.json``). This is the read-only renderer that turns one such
    token into the shape the dashboard explains a scalar with — WITHOUT
    re-scoring. Token grammar (see ``zicato/scoring/dispatch.py`` +
    ``plugins.py``)::

        "builtin"                                  # the default formula
        "transform:pass=pow(2.0)"                  # Seam-2 pass transform
        "transform:drift{looping_reasoning=harmonic, off_topic=cap(5)}"  # Seam 1
        "plugin:scalar_fn=<dotted spec>"           # Seam-2 dotted plugin
        "plugin:drift_reducer=<dotted spec>"       # Seam-1 dotted plugin
        "<any of the above> (fallback: <reason>)"  # FAIL-OPEN — plugin failed

    Returns ``{kind, source, transforms, fail_open, fallback_reason, raw}``:

    * ``kind`` — ``"builtin"`` / ``"transform"`` / ``"plugin"`` / ``"unknown"``.
    * ``source`` — the human label of what produced the value (the transform
      token, the dotted plugin spec, or ``"built-in formula"``).
    * ``transforms`` — for a drift token, ``[{kind, op}]`` of every reshaped
      drift kind; ``[]`` otherwise.
    * ``fail_open`` — ``True`` iff the token carries the ``(fallback: …)``
      marker: a fired plugin that FAILED OPEN to the built-in / transformed
      default. This is the first-class caution signal the UI surfaces
      prominently — a silently-degraded plugin must never hide in a log.
    * ``fallback_reason`` — the parenthesised reason when ``fail_open``.
    * ``raw`` — the original token (for the title / debugging).

    ``None`` and ``"builtin"`` both yield ``kind="builtin"`` (and
    ``None`` additionally sets ``present=False`` so a pre-#19 run renders
    nothing new). Any unrecognised string degrades to ``kind="unknown"``
    rather than raising — this is a display helper, never fatal.
    """
    out: dict[str, Any] = {
        "present": token is not None,
        "kind": "builtin",
        "source": "built-in formula",
        "transforms": [],
        "fail_open": False,
        "fallback_reason": None,
        "raw": token,
    }
    if not token:
        # None (pre-#19) or "" — nothing to decompose. ``present`` already
        # records whether the field existed at all.
        return out

    body = token
    # Fail-open marker: "<pre-plugin token> (fallback: <reason>)". A fired
    # plugin that fell back to the built-in / transformed default. Strip the
    # marker so we still classify the underlying (pre-plugin) token, but flag
    # the degradation prominently.
    marker = " (fallback: "
    idx = token.find(marker)
    if idx >= 0 and token.endswith(")"):
        out["fail_open"] = True
        out["fallback_reason"] = token[idx + len(marker) : -1]
        body = token[:idx]

    if body == "builtin":
        out["kind"] = "builtin"
        out["source"] = "built-in formula"
    elif body.startswith("plugin:"):
        out["kind"] = "plugin"
        # "plugin:scalar_fn=<spec>" / "plugin:drift_reducer=<spec>"
        rest = body[len("plugin:") :]
        seam, _, spec = rest.partition("=")
        out["source"] = spec or rest
        out["seam"] = seam
    elif body.startswith("transform:drift{") and body.endswith("}"):
        out["kind"] = "transform"
        out["source"] = "drift transform"
        inner = body[len("transform:drift{") : -1]
        transforms: list[dict[str, str]] = []
        for part in inner.split(", "):
            part = part.strip()
            if not part:
                continue
            kind_name, _, op = part.partition("=")
            transforms.append({"kind": kind_name, "op": op or "?"})
        out["transforms"] = transforms
    elif body.startswith("transform:pass="):
        out["kind"] = "transform"
        out["source"] = body[len("transform:pass=") :]
        out["transforms"] = [{"kind": "pass", "op": body[len("transform:pass=") :]}]
    elif body.startswith("transform:"):
        out["kind"] = "transform"
        out["source"] = body[len("transform:") :]
    else:
        out["kind"] = "unknown"
        out["source"] = body
    return out


def _build_scalar_decomposition(
    parent_agg: dict[str, Any] | None,
    child_agg: dict[str, Any] | None,
    parent_drift_prov: str | None,
    child_drift_prov: str | None,
) -> dict[str, Any]:
    """Assemble the gate-breakdown scalar decomposition (#19 phase 4).

    Reads the persisted provenance tokens — the per-generation
    ``scalar_provenance`` (Seam 2, off ``gen_score.json``) and a
    representative per-run ``scoring_provenance`` (Seam 1, off the
    generation's ``loss.json`` files) — and renders, per side, WHICH
    transform / plugin produced the pass term and the drift component, plus a
    first-class ``fail_open`` flag.

    Shape::

        {
          "present": bool,        # any non-None provenance on either side
          "fail_open": bool,      # ANY side / seam failed open (caution)
          "champion": {scalar, drift} | None,
          "challenger": {scalar, drift} | None,
        }

    where each side's ``scalar`` / ``drift`` is the
    :func:`_parse_scoring_provenance` view of that seam's token. A side with
    only built-in / absent provenance still renders (cleanly / quietly); the
    consumer decides whether to show the panel at all based on ``present``.
    """

    def _side(agg: dict[str, Any] | None, drift_prov: str | None) -> dict[str, Any] | None:
        if not isinstance(agg, dict):
            return None
        scalar_prov_raw = agg.get("scalar_provenance")
        scalar_prov = scalar_prov_raw if isinstance(scalar_prov_raw, str) else None
        scalar_view = _parse_scoring_provenance(scalar_prov)
        drift_view = _parse_scoring_provenance(drift_prov)
        return {"scalar": scalar_view, "drift": drift_view}

    champion = _side(parent_agg, parent_drift_prov)
    challenger = _side(child_agg, child_drift_prov)

    def _has_prov(side: dict[str, Any] | None) -> bool:
        if side is None:
            return False
        return bool(side["scalar"]["present"] or side["drift"]["present"])

    def _failed_open(side: dict[str, Any] | None) -> bool:
        if side is None:
            return False
        return bool(side["scalar"]["fail_open"] or side["drift"]["fail_open"])

    return {
        "present": _has_prov(champion) or _has_prov(challenger),
        "fail_open": _failed_open(champion) or _failed_open(challenger),
        "champion": champion,
        "challenger": challenger,
    }


def _representative_drift_provenance(
    paths: WorkspacePaths, epoch_id: str, generation_id: str
) -> str | None:
    """Pick one generation's Seam-1 drift provenance token to display.

    Every run in a generation scores its drift through the SAME contract
    (same ``drift_kind_aggregation`` / ``drift_reducer``), so the token is
    homogeneous across the generation EXCEPT that a fail-open event fires
    per-run (a plugin that raised on one board's inputs). To keep a single
    silently-degraded run visible, prefer a fail-open token (the
    ``(fallback: …)`` form) over a clean one; otherwise return the first
    non-``"builtin"`` token, else the first token, else ``None``.
    """
    cells = _read_run_loss_files(paths, epoch_id, generation_id)
    tokens: list[str] = [
        c["scoring_provenance"]
        for c in cells.values()
        if isinstance(c.get("scoring_provenance"), str)
    ]
    if not tokens:
        return None
    for t in tokens:
        if "(fallback: " in t:
            return t
    for t in tokens:
        if t != "builtin":
            return t
    return tokens[0]


def _live_challenger_projection(
    paths: WorkspacePaths,
    epoch_id: str,
    champion_id: str,
    challenger_id: str,
) -> dict[str, Any] | None:
    """The challenger's live projected absolute, or ``None`` when not live.

    A round is "live" iff there is an active tournament for this very
    champion/challenger pair AND its ``projected`` standings map carries a
    row for the challenger (the runner writes one the instant the first
    board of the round settles — see
    :func:`zicato.runtime.state.update_tournament_projected`). The row is a
    pure read-back of the already-computed live aggregate; this reader does
    no scoring of its own.

    Returns ``{"challenger_scalar", "boards_done", "boards_total"}`` — the
    challenger's projected absolute scalar so far and its board progress —
    or ``None`` when no live projection exists (a settled round, a
    different in-flight pair, or no active tournament). Never raises: any
    malformed envelope degrades to ``None`` so the settled breakdown stands
    on its own.
    """
    try:
        from zicato.query.runtime_view import (  # noqa: PLC0415
            read_active_tournament_dict,
        )

        active = read_active_tournament_dict(paths)
    except Exception:  # noqa: BLE001 — the live overlay is best-effort
        return None
    if not isinstance(active, dict):
        return None
    # Only overlay when the active tournament IS this round (same epoch +
    # same champion/challenger pair); otherwise a stale or unrelated live
    # tournament must not bleed into a settled historical round.
    if active.get("epoch_id") not in (None, epoch_id):
        return None
    if active.get("child_generation_id") != challenger_id:
        return None
    active_champion = active.get("parent_generation_id")
    if active_champion not in (None, "", champion_id):
        return None
    projected = active.get("projected")
    if not isinstance(projected, dict):
        return None
    row = projected.get(challenger_id)
    if not isinstance(row, dict):
        return None
    scalar = row.get("scalar")
    if not isinstance(scalar, int | float):
        return None
    out: dict[str, Any] = {"challenger_scalar": float(scalar)}
    boards_done = row.get("boards_done")
    boards_total = row.get("boards_total")
    out["boards_done"] = int(boards_done) if isinstance(boards_done, int | float) else None
    out["boards_total"] = int(boards_total) if isinstance(boards_total, int | float) else None
    return out


def _build_override_block(
    paths: WorkspacePaths, epoch_id: str, challenger_id: str
) -> dict[str, Any]:
    """The ``gate.override`` block for one challenger.

    Reads the challenger's persisted ``experiment.json`` outcome — which now
    carries ``operator_override`` + ``operator_override_reason`` whenever an
    operator force-promoted / force-rejected it through the control protocol —
    and projects it into ``{present, action, reason}``. ``present`` is
    ``False`` (action/reason ``None``) on every gate-decided round and every
    pre-feature run, so a gate-decided breakdown is byte-compatible with the
    pre-override shape. ``action`` is ``"promote"`` / ``"reject"`` derived
    from the recorded ``tournament_decision`` so the L3 view can label the
    override without re-deriving it.
    """
    absent: dict[str, Any] = {"present": False, "action": None, "reason": None}
    if not challenger_id:
        return absent
    exp = _read_json_value(layout_of(paths).experiment(epoch_id, challenger_id))
    if not isinstance(exp, dict):
        return absent
    outcome = exp.get("outcome")
    if not isinstance(outcome, dict) or not outcome.get("operator_override"):
        return absent
    decision = str(outcome.get("tournament_decision", ""))
    return {
        "present": True,
        "action": "promote" if decision == "promoted" else "reject",
        "reason": str(outcome.get("operator_override_reason", "")),
    }


def build_gate_breakdown(
    paths: WorkspacePaths,
    epoch_id: str,
    champion_id: str,
    challenger_id: str,
) -> dict[str, Any]:
    """Structured promote-gate decomposition for the L3 decision view.

    ``GET /api/round/{epoch_id}/{champion}/{challenger}/gate``. Reuses the
    authoritative :func:`zicato.tournament.gate.evaluate_gate` and its
    helpers so the breakdown always agrees with what the runner decided.

    Returns the rule-by-rule shape documented on the route handler. Rules
    are emitted in evaluation order (regression suite -> scalar margin ->
    pass-rate monotonicity -> namespace monotonicity). The first failing
    rule has ``status="fail"`` and ``fired=True``; rules after it are
    ``not_reached``; satisfied rules are ``pass``. Disabled rules are
    ``skipped`` (regression suite) / ``disabled`` (monotonicity flags) and
    never ``fired``. A rule whose inputs are unavailable degrades to
    ``unknown`` rather than guessing.
    """
    from zicato.tournament.gate import (  # noqa: PLC0415
        PASS_RATE_MONOTONICITY_TOLERANCE,
        _regressed_entries,
        _regressed_namespaces,
        evaluate_gate,
    )

    weights = _read_epoch_scoring_weights(paths, epoch_id)

    parent_agg = _gen_agg_for_gate(paths, epoch_id, champion_id) if champion_id else None
    child_agg = _gen_agg_for_gate(paths, epoch_id, challenger_id)

    base: dict[str, Any] = {
        "epoch_id": epoch_id,
        "champion": champion_id,
        "challenger": challenger_id,
        "decision": "deferred",
        "reason": "",
        # The CANONICAL single-token explanation of the verdict: the ONE rule
        # that fired (the server sets ``fired`` on exactly one rule), or
        # ``None`` when nothing fired / the gate could not be reconstructed.
        # The frontend reads this verbatim — it never re-infers the deciding
        # rule from the rule list or scrapes the free-text ``detail``.
        "deciding_rule": None,
        # The promote margin the scalar rule compares against — structured,
        # so no consumer parses it out of the rule detail string.
        "margin": float(getattr(weights, "promote_margin", 0.01)),
        # The regressed predicate / namespace named by a fired monotonicity
        # rule (the first regressed item — the one the gate reports). ``None``
        # when no monotonicity rule fired.
        "regressed_predicate": None,
        "regressed_namespace": None,
        "delta_scalar": None,
        "delta_pass_rate": None,
        # Absolute scalars for each side (pure projection of the already-read
        # aggregates), so the L3 view can show "47.58 → 57.70" without
        # back-deriving the absolutes from the relative ``delta_scalar``. Both
        # ``None`` until the corresponding aggregate is found on disk.
        "champion_scalar": None,
        "challenger_scalar": None,
        # The challenger's LIVE projected standing while a round is in flight:
        # ``{challenger_scalar, boards_done, boards_total}``. ``None`` on a
        # settled round (no active tournament for this pair) so a historical
        # breakdown is byte-identical to before this field existed.
        "live": None,
        "rules": [],
        "scalar_components": {"champion": None, "challenger": None},
        # Scoring provenance decomposition (#19 phase 4): which transform /
        # plugin produced each side's pass term + drift component, parsed from
        # the recorded provenance tokens, with a first-class fail-open flag.
        # ``present=False`` on a pre-#19 run (no provenance recorded) so the UI
        # renders nothing new — back-compat clean.
        "scalar_decomposition": _build_scalar_decomposition(
            parent_agg,
            child_agg,
            _representative_drift_provenance(paths, epoch_id, champion_id) if champion_id else None,
            _representative_drift_provenance(paths, epoch_id, challenger_id),
        ),
        "primary_driver": None,
        # Bradley--Terry uncertainty pre-gate block (#crown-on-evidence). Always
        # present as a key; ``rating.present`` is ``False`` on a pre-BT / disabled
        # run (no ``promote_confidence_threshold`` in the structure params), so a
        # UI that does not know the field renders nothing new — back-compat clean.
        "rating": build_rating_view(paths, epoch_id, champion_id, challenger_id),
        # Operator override block. ``present`` is ``False`` on every
        # gate-decided round (and on every pre-feature run), so a breakdown for
        # a gate-decided pair is byte-compatible with the pre-override shape;
        # ``present=True`` carries ``{action, reason}`` when an operator
        # force-promoted / force-rejected THIS challenger, so the L3 view never
        # presents the override as the gate's own verdict.
        "override": _build_override_block(paths, epoch_id, challenger_id),
    }

    # Echo the per-judge primary driver from the same source the L3
    # per-judge-comparison endpoint uses (best-effort; never fatal).
    try:
        comparison = build_per_judge_comparison(paths, epoch_id, champion_id, challenger_id)
        driver_name = comparison.get("primary_driver")
        if isinstance(driver_name, str) and driver_name:
            driver_delta: float | None = None
            for jrow in comparison.get("judges", []):
                if isinstance(jrow, dict) and jrow.get("judge_name") == driver_name:
                    d = jrow.get("delta")
                    driver_delta = coerce_float(d)
                    break
            base["primary_driver"] = {"judge": driver_name, "delta": driver_delta}
    except Exception:  # noqa: BLE001 — the driver echo is best-effort
        base["primary_driver"] = None

    # Surface the scalar components for both sides regardless of decision.
    if isinstance(parent_agg, dict) and isinstance(parent_agg.get("scalar_components"), dict):
        base["scalar_components"]["champion"] = coerce_numeric_dict(parent_agg["scalar_components"])
    if isinstance(child_agg, dict) and isinstance(child_agg.get("scalar_components"), dict):
        base["scalar_components"]["challenger"] = coerce_numeric_dict(
            child_agg["scalar_components"]
        )

    # Absolute scalars for both sides — pure projection of the aggregates
    # already read above (the same values ``evaluate_gate`` compares). Present
    # regardless of decision so the degraded (one-side-missing) path still
    # surfaces whichever absolute it has.
    if isinstance(parent_agg, dict) and isinstance(parent_agg.get("scalar"), int | float):
        base["champion_scalar"] = float(parent_agg["scalar"])
    if isinstance(child_agg, dict) and isinstance(child_agg.get("scalar"), int | float):
        base["challenger_scalar"] = float(child_agg["scalar"])

    # Live overlay: while this very round is in flight, surface the
    # challenger's projected absolute + board progress. Default-absent
    # (``None``) on a settled round so a historical breakdown is unchanged.
    base["live"] = _live_challenger_projection(paths, epoch_id, champion_id, challenger_id)

    # ---- Build each rule. We assemble all four, then resolve their
    # ---- statuses against the authoritative gate verdict below.
    regression_enabled = bool(getattr(weights, "regression_gate_enabled", False))
    pass_mono_enabled = bool(getattr(weights, "pass_rate_monotonicity", True))

    def _ns_mono_any_enabled() -> bool:
        ns_mono = getattr(weights, "namespace_monotonicity", {}) or {}
        ns_weights = getattr(weights, "namespace_weights", {}) or {}
        return any(
            enabled and float(ns_weights.get(ns, 0.0)) != 0.0 for ns, enabled in ns_mono.items()
        )

    ns_mono_enabled = _ns_mono_any_enabled()

    # Without a comparable scalar on both sides we cannot reconstruct the
    # gate — every numeric rule degrades to "unknown".
    have_both = isinstance(parent_agg, dict) and isinstance(child_agg, dict)

    if not have_both:
        base["rules"] = [
            {
                "id": "regression_suite",
                "label": "Regression suite",
                "status": "skipped" if not regression_enabled else "unknown",
                "detail": (
                    "disabled"
                    if not regression_enabled
                    else "regression-suite outcome not recorded"
                ),
                "fired": False,
            },
            {
                "id": "scalar_margin",
                "label": "Scalar margin",
                "status": "unknown",
                "detail": "champion or challenger aggregate not found",
                "fired": False,
            },
            {
                "id": "pass_rate_monotonicity",
                "label": "Pass-rate monotonicity",
                "status": "disabled" if not pass_mono_enabled else "unknown",
                "detail": "disabled" if not pass_mono_enabled else "aggregates not found",
                "fired": False,
            },
            {
                "id": "namespace_monotonicity",
                "label": "Namespace monotonicity",
                "status": "disabled" if not ns_mono_enabled else "unknown",
                "detail": "disabled" if not ns_mono_enabled else "aggregates not found",
                "fired": False,
            },
        ]
        # No aggregates ⇒ this challenger never ran a tournament (e.g. it was
        # soft-rejected for field diversity during proposing). Surface its
        # PERSISTED rejection so the gate panel documents WHY it was cut — the
        # full "field_diversity_overlap: overlap 0.667 with v9 …" reason — instead
        # of a bare "deferred" with no explanation.
        exp = _read_json_value(layout_of(paths).experiment(epoch_id, challenger_id))
        if isinstance(exp, dict):
            exp_outcome = exp.get("outcome")
            if isinstance(exp_outcome, dict):
                exp_reason = str(exp_outcome.get("rejection_reason", "") or "")
                if str(exp_outcome.get("tournament_decision", "")) == "rejected" and exp_reason:
                    base["decision"] = "rejected"
                    base["reason"] = exp_reason
        return base

    # Both aggregates present — run the real gate.
    assert isinstance(parent_agg, dict) and isinstance(child_agg, dict)
    outcome = evaluate_gate(parent_agg, child_agg, weights)
    base["decision"] = outcome.decision
    base["reason"] = outcome.reason
    base["delta_scalar"] = outcome.delta_scalar
    base["delta_pass_rate"] = outcome.delta_pass_rate

    parent_scalar = float(parent_agg["scalar"])
    child_scalar = float(child_agg["scalar"])
    promote_margin = float(getattr(weights, "promote_margin", 0.01))

    # Which rule fired? Re-derive deterministically (mirrors evaluate_gate's
    # short-circuit order) without re-implementing any threshold — we call
    # the same predicate evaluate_gate uses.
    scalar_failed = child_scalar > parent_scalar - promote_margin
    regressed_entries = _regressed_entries(parent_agg, child_agg) if pass_mono_enabled else []
    regressed_ns = _regressed_namespaces(parent_agg, child_agg, weights) if ns_mono_enabled else []

    # The pass-rate monotonicity rule's granularity is operator-selected.
    # Under "aggregate" the rule fires on an overall pass-rate drop rather
    # than a per-entry flip — mirror the gate's own predicate
    # (delta_pass_rate < -tolerance) here so the dashboard never
    # re-implements a threshold of its own.
    pass_mono_scope = str(getattr(weights, "pass_rate_monotonicity_scope", "per_entry"))
    parent_pass_rate = float(parent_agg.get("pass_rate", 1.0))
    child_pass_rate = float(child_agg.get("pass_rate", 1.0))
    delta_pass_rate = child_pass_rate - parent_pass_rate
    if pass_mono_scope == "aggregate":
        pass_mono_regressed = pass_mono_enabled and (
            delta_pass_rate < -PASS_RATE_MONOTONICITY_TOLERANCE
        )
    else:
        pass_mono_regressed = bool(pass_mono_enabled and regressed_entries)

    # The fired rule is the first that rejects, in gate order. Regression
    # suite is a pre-gate the dashboard cannot replay (no recorded
    # outcome on disk), so it is reported as pass/skipped, never fired.
    fired_rule: str | None = None
    if scalar_failed:
        fired_rule = "scalar_margin"
    elif pass_mono_regressed:
        fired_rule = "pass_rate_monotonicity"
    elif ns_mono_enabled and regressed_ns:
        fired_rule = "namespace_monotonicity"

    # The structured decision surface (the frontend reads these verbatim;
    # the free-text rule ``detail`` is display-only).
    base["deciding_rule"] = fired_rule
    if fired_rule == "pass_rate_monotonicity" and regressed_entries:
        base["regressed_predicate"] = regressed_entries[0]
    if fired_rule == "namespace_monotonicity" and regressed_ns:
        base["regressed_namespace"] = regressed_ns[0]

    order = [
        "regression_suite",
        "scalar_margin",
        "pass_rate_monotonicity",
        "namespace_monotonicity",
    ]
    fired_index = order.index(fired_rule) if fired_rule is not None else len(order)

    # -- regression_suite --------------------------------------------
    if not regression_enabled:
        regression_rule = {
            "id": "regression_suite",
            "label": "Regression suite",
            "status": "skipped",
            "detail": "disabled",
            "fired": False,
        }
    else:
        # Enabled, but the dashboard has no recorded suite outcome to
        # replay. Honest degrade: the gate ran it, we just cannot show
        # which way it went from the on-disk aggregates alone.
        regression_rule = {
            "id": "regression_suite",
            "label": "Regression suite",
            "status": "unknown",
            "detail": "regression-suite outcome not recorded in the dashboard's read path",
            "fired": False,
        }

    # -- scalar_margin -----------------------------------------------
    scalar_detail = (
        f"{parent_scalar:.2f} → {child_scalar:.2f} "
        f"({child_scalar - parent_scalar:+.2f}; needs ≤ "
        f"{-promote_margin:.2f})"
    )
    scalar_rule = {
        "id": "scalar_margin",
        "label": "Scalar margin",
        "status": "fail" if fired_rule == "scalar_margin" else "pass",
        "detail": scalar_detail,
        "fired": fired_rule == "scalar_margin",
    }
    if fired_index < order.index("scalar_margin"):
        scalar_rule["status"] = "not_reached"

    # -- pass_rate_monotonicity --------------------------------------
    if not pass_mono_enabled:
        pass_rule = {
            "id": "pass_rate_monotonicity",
            "label": "Pass-rate monotonicity",
            "status": "disabled",
            "detail": "disabled",
            "fired": False,
        }
    elif fired_index < order.index("pass_rate_monotonicity"):
        pass_rule = {
            "id": "pass_rate_monotonicity",
            "label": "Pass-rate monotonicity",
            "status": "not_reached",
            "detail": "not reached (an earlier rule fired)",
            "fired": False,
        }
    elif pass_mono_scope == "aggregate":
        # Aggregate scope: render the overall pass-rate movement, not the
        # per-entry regressed list — a strictly-better aggregate is allowed
        # to reshuffle which entries pass.
        rate_detail = (
            f"overall {parent_pass_rate:.2f} → {child_pass_rate:.2f} "
            f"({delta_pass_rate:+.2f}; aggregate scope)"
        )
        pass_rule = {
            "id": "pass_rate_monotonicity",
            "label": "Pass-rate monotonicity",
            "status": "fail" if pass_mono_regressed else "pass",
            "detail": rate_detail,
            "fired": fired_rule == "pass_rate_monotonicity",
        }
    elif regressed_entries:
        pass_rule = {
            "id": "pass_rate_monotonicity",
            "label": "Pass-rate monotonicity",
            "status": "fail",
            "detail": "regressed: " + ", ".join(regressed_entries),
            "fired": fired_rule == "pass_rate_monotonicity",
        }
    else:
        pass_rule = {
            "id": "pass_rate_monotonicity",
            "label": "Pass-rate monotonicity",
            "status": "pass",
            "detail": "all preserved",
            "fired": False,
        }

    # -- namespace_monotonicity --------------------------------------
    if not ns_mono_enabled:
        ns_rule = {
            "id": "namespace_monotonicity",
            "label": "Namespace monotonicity",
            "status": "disabled",
            "detail": "disabled",
            "fired": False,
        }
    elif fired_index < order.index("namespace_monotonicity"):
        ns_rule = {
            "id": "namespace_monotonicity",
            "label": "Namespace monotonicity",
            "status": "not_reached",
            "detail": "not reached (an earlier rule fired)",
            "fired": False,
        }
    elif not isinstance(child_agg.get("namespace_aggregates"), dict):
        # The rule is enabled but we lack the namespace aggregates to
        # judge it — degrade honestly rather than claim "all within".
        ns_rule = {
            "id": "namespace_monotonicity",
            "label": "Namespace monotonicity",
            "status": "unknown",
            "detail": "namespace aggregates not recorded",
            "fired": False,
        }
    elif regressed_ns:
        ns_rule = {
            "id": "namespace_monotonicity",
            "label": "Namespace monotonicity",
            "status": "fail",
            "detail": "regressed: " + ", ".join(regressed_ns),
            "fired": fired_rule == "namespace_monotonicity",
        }
    else:
        ns_rule = {
            "id": "namespace_monotonicity",
            "label": "Namespace monotonicity",
            "status": "pass",
            "detail": "all within bounds",
            "fired": False,
        }

    base["rules"] = [regression_rule, scalar_rule, pass_rule, ns_rule]
    return base


def _read_promote_confidence_threshold(paths: WorkspacePaths, epoch_id: str) -> float | None:
    """Read the epoch's opt-in ``promote_confidence_threshold`` from disk.

    The pre-gate threshold lives in the structure params, persisted under
    ``scoring.json`` → ``tournament.params``, and resolves through the SAME
    :func:`zicato.selection.evidence_gate.read_promote_confidence_threshold`
    the selection layer uses. Returns ``None`` (no pre-gate) when the epoch
    predates the field, the key is absent / an explicit ``null`` / ``0``, or
    the value is out of range — so a disabled run reports ``present=false``.
    """
    from zicato.selection.evidence_gate import (  # noqa: PLC0415
        read_promote_confidence_threshold as _read_threshold,
    )

    raw = _read_json_value(layout_of(paths).scoring(epoch_id))
    if not isinstance(raw, dict):
        return None
    tournament = raw.get("tournament")
    if not isinstance(tournament, dict):
        return None
    params = tournament.get("params")
    if not isinstance(params, dict):
        return None
    return _read_threshold(params)


def _read_pair_duels_from_durable(
    paths: WorkspacePaths,
    epoch_id: str,
    champion_id: str,
    challenger_id: str,
) -> list[Any]:
    """Reconstruct the champion/challenger duel audit from the durable record.

    The settled field-tournament snapshot persists each match with its
    ``competitors`` + ``winner`` + ``delta_scalar`` (see
    ``zicato.evolve.dashboard_projection._serialise_rounds``). Each match
    between exactly the two named contestants is reconstructed into one
    :class:`~zicato.selection.resolve.Duel` ``(winner, loser, |delta_scalar|)``,
    which is the same per-pairing form the live audit feeds Bradley--Terry.
    Returns ``[]`` when no durable record / no matching matches exist (the
    reader then reports an uncredible fit rather than guessing).
    """
    from zicato.core.workspace import (  # noqa: PLC0415
        field_tournaments_dir,
    )
    from zicato.selection.resolve import Duel  # noqa: PLC0415

    pair = {champion_id, challenger_id}
    duels: list[Any] = []
    tdir = field_tournaments_dir(paths.root, epoch_id)
    if not tdir.is_dir():
        return duels
    for record_path in sorted(tdir.glob("field-*.json")):
        record = _read_json_value(record_path)
        if not isinstance(record, dict):
            continue
        for rnd in record.get("rounds") or []:
            if not isinstance(rnd, dict):
                continue
            for match in rnd.get("matches") or []:
                if not isinstance(match, dict):
                    continue
                competitors = match.get("competitors")
                if not isinstance(competitors, list) or set(competitors) != pair:
                    continue
                winner = match.get("winner")
                delta = match.get("delta_scalar")
                if not isinstance(winner, str) or winner not in pair:
                    continue
                loser = (pair - {winner}).pop()
                margin = abs(float(delta)) if isinstance(delta, int | float) else 0.0
                duels.append(Duel(winner=winner, loser=loser, margin=margin))
    return duels


def build_rating_view(
    paths: WorkspacePaths,
    epoch_id: str,
    champion_id: str,
    challenger_id: str,
) -> dict[str, Any]:
    """The Bradley--Terry ``gate.rating`` block for a champion/challenger pair.

    Wires :mod:`zicato.selection.rating` into the gate breakdown. Shape:

        {present, credible, champion/challenger {theta, se, ci_lo, ci_hi},
         p_stronger, threshold, decision, ci_overlap, replicates_spent,
         n_duels, next_duel, ci_history}

    ``present`` is ``False`` on a pre-BT / disabled run — when the epoch carries
    no ``promote_confidence_threshold`` in its structure params — so a
    breakdown for a run that never opted in is byte-compatible with the
    pre-rating shape (the key exists but every consumer keys off ``present``).

    When the pre-gate WAS active, the block is reconstructed from on disk:

    * the authoritative source is the dead-letter record
      (``runtime/inconclusive/<challenger>.json``) when the duel went terminally
      inconclusive — it carries the final ``rating`` block + ``ci_history`` the
      driver computed;
    * otherwise the duel audit is reconstructed from the durable field-
      tournament matches and re-fitted here. The fit is only credible at
      :data:`~zicato.selection.evidence_gate.MIN_CREDIBLE_DUELS` resolved duels
      (the SE blows up below that), so a thin audit reports
      ``credible=false`` with whatever CIs the fit produced.

    ``next_duel`` is the closest-CI pairing a replicate would sharpen next
    (``None`` when the duel is resolved or unfittable); ``ci_history`` is the
    per-refit convergence trace (a single current point when reconstructed
    live, the full driver trace when read from the dead-letter record).
    """
    from zicato.selection.dead_letter import read_inconclusive  # noqa: PLC0415
    from zicato.selection.evidence_gate import (  # noqa: PLC0415
        CI_Z,
        MIN_CREDIBLE_DUELS,
    )
    from zicato.selection.rating import fit_bradley_terry, prob_stronger  # noqa: PLC0415
    from zicato.selection.resolve import build_matrix  # noqa: PLC0415

    absent = {"present": False}

    threshold = _read_promote_confidence_threshold(paths, epoch_id)
    if threshold is None or not challenger_id:
        return absent

    # Prefer the authoritative dead-letter record for an inconclusive duel — it
    # carries the exact final block the driver computed (incl. the full
    # ci_history), so the dashboard never disagrees with the run's own verdict.
    dead_letter = read_inconclusive(paths.root, challenger_id)
    if isinstance(dead_letter, dict):
        rating = dead_letter.get("rating")
        if isinstance(rating, dict) and rating.get("present"):
            out = dict(rating)
            out["next_duel"] = None  # terminal — nothing more to replicate
            history = dead_letter.get("ci_history")
            out["ci_history"] = history if isinstance(history, list) else []
            return out

    # Else reconstruct the duel audit from the durable record and re-fit.
    duels = _read_pair_duels_from_durable(paths, epoch_id, champion_id, challenger_id)
    # Resolved (non-tie) duels for THIS pair gate credibility.
    pair_duels = [
        (d.winner, d.loser)
        for d in duels
        if champion_id in (d.winner, d.loser) and challenger_id in (d.winner, d.loser)
    ]
    n_duels = len(pair_duels)

    block: dict[str, Any] = {
        "present": True,
        "credible": False,
        "champion": None,
        "challenger": None,
        "p_stronger": None,
        "threshold": threshold,
        "decision": "deferred",
        "ci_overlap": False,
        "replicates_spent": 0,
        "n_duels": n_duels,
        "next_duel": None,
        "ci_history": [],
    }

    if not pair_duels:
        return block

    rating = fit_bradley_terry(pair_duels)

    def _ci(gid: str) -> dict[str, Any] | None:
        if gid not in rating:
            return None
        theta, se = rating[gid]
        half = CI_Z * se
        return {"theta": theta, "se": se, "ci_lo": theta - half, "ci_hi": theta + half}

    champ_ci = _ci(champion_id)
    chal_ci = _ci(challenger_id)
    block["champion"] = champ_ci
    block["challenger"] = chal_ci

    if champ_ci is not None and chal_ci is not None:
        p = prob_stronger(chal_ci["theta"], chal_ci["se"], champ_ci["theta"], champ_ci["se"])
        overlap = champ_ci["ci_lo"] <= chal_ci["ci_hi"] and chal_ci["ci_lo"] <= champ_ci["ci_hi"]
        block["p_stronger"] = p
        block["ci_overlap"] = overlap
        credible = n_duels >= MIN_CREDIBLE_DUELS
        block["credible"] = credible
        if credible:
            block["decision"] = "promoted" if (p >= threshold and not overlap) else "deferred"
            # The next duel a replicate would sharpen: the closest-CI pairing
            # across the reconstructed field (None when already separated).
            matrix = build_matrix(duels)
            if overlap and matrix.ids:
                block["next_duel"] = {"left": champion_id, "right": challenger_id}
        block["ci_history"] = [{"p_stronger": p, "ci_overlap": overlap, "replicates_spent": 0}]

    return block
