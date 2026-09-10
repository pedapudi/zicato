"""gate_view — extracted from the former dashboard state_reader monolith (pure move)."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from typing import Any

from zicato.epoch._storage import RecordError
from zicato.epoch.journal import read_experiment_body
from zicato.query._sqlite import (
    INDEX_NOT_BUILT_NOTE,
    _IndexAbsent,
    _query,
    open_index_ro,
    with_index_not_built_note,
)
from zicato.query.inputs import EpochInputs
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
from zicato.query.replicate_scores import selected_measurements
from zicato.query.tournament_view import _gen_score_view

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
       run of it (so an entry run twice contributes its mean rather than a
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


def build_score_trajectory(
    paths: WorkspacePaths,
    epoch_id: str | None = None,
    *,
    lineage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """``GET /api/score-trajectory`` — the scalar across generations.

    ``epoch_id`` defaults to the current epoch; a validated id scopes the
    trajectory to that epoch's generations instead.

    ``lineage`` lets a caller that ALREADY built the lineage feed hand it in
    rather than paying for a second workspace walk. ``build_environment``
    serves both this trajectory and the ``generations`` feed in one payload,
    so it built the lineage twice — and the walk reads a JSON file per
    generation, which cProfile put at 84% of that reader. A caller-supplied
    feed may be the workspace-global one OR one already scoped to the SAME
    epoch — the filter below applies either way, and is a no-op on the
    scoped feed; ``include_ratings`` may differ, since the rating triple is
    additive and nothing here reads it.

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

    Returns ``{"epoch_id", "points": [{generation_id, parent_generation_id,
    promoted, scalar, entry_count, created_at}], "note"?}``. Degrades to
    an empty ``points`` list (never raises) when the index is absent.
    """
    epoch_id = _resolve_epoch_id(paths, epoch_id)
    # Lineage order is authoritative for the x-axis — the index's
    # ``generations`` rows can carry empty ``created_at`` strings.
    if lineage is None:
        # Scope the WALK to the epoch this reader is about to filter down to.
        # Walking every epoch and discarding all but one costs a JSON read per
        # generation, so a 60-epoch workspace would pay 60x for one epoch's
        # curve. ``epoch_id`` is None only when the workspace has no current
        # epoch, and the global walk is the right answer there.
        lineage = build_lineage_view(paths, epoch_id, include_ratings=False)
    # The filter STAYS: a caller-supplied feed may be workspace-global.
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
                points.append(
                    {
                        "generation_id": gid,
                        "parent_generation_id": g.get("parent_generation_id"),
                        "promoted": g.get("promoted"),
                        "scalar": scalar,
                        "entry_count": entry_count,
                        "created_at": g.get("created_at"),
                    }
                )
            return {"epoch_id": epoch_id, "points": points}
    except _IndexAbsent:
        return {
            "epoch_id": epoch_id,
            "points": [
                {
                    "generation_id": g["generation_id"],
                    "parent_generation_id": g.get("parent_generation_id"),
                    "promoted": g.get("promoted"),
                    "scalar": None,
                    "entry_count": 0,
                    "created_at": g.get("created_at"),
                }
                for g in ordered
            ],
            "note": INDEX_NOT_BUILT_NOTE,
        }
    except sqlite3.Error:
        return {"epoch_id": epoch_id, "points": []}


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
        return with_index_not_built_note(empty)
    except sqlite3.Error:
        return empty


def build_health_report(paths: WorkspacePaths) -> dict[str, Any]:
    """``GET /api/health-report`` — saved diagnostics plus receipt status."""
    epoch_id = read_current_epoch(paths)
    healthy_empty: dict[str, Any] = {
        "epoch_id": epoch_id,
        "findings": [],
        "healthy": True,
    }
    if epoch_id is None:
        return healthy_empty
    from zicato.health.diagnostics import read_latest_loop_health

    try:
        health = read_latest_loop_health(paths.root, epoch_id)
    except (RecordError, OSError) as exc:
        return _overlay_live_diagnostics(
            paths, {**healthy_empty, "healthy": None, "unreadable": str(exc)}, epoch_id
        )
    if health is None:
        return _overlay_live_diagnostics(paths, healthy_empty, epoch_id)
    report: dict[str, Any] = {
        "epoch_id": health.epoch_id,
        "findings": [finding.to_json() for finding in health.findings],
        "healthy": health.healthy,
        "checked_at": health.checked_at,
    }
    return _overlay_live_diagnostics(paths, report, epoch_id)


def _overlay_live_diagnostics(
    paths: WorkspacePaths,
    report: dict[str, Any],
    epoch_id: str,
) -> dict[str, Any]:
    """Refresh receipt state and merge each retained operational warning once."""
    from zicato.health.diagnostics import (  # noqa: PLC0415
        detect_optional_failures,
        detect_settlement_receipt_attention,
    )
    from zicato.health.inputs import (  # noqa: PLC0415
        epoch_optional_failures,
        epoch_settlement_receipt_attention,
    )

    receipt_codes = {
        "on_promote_hook_delivery_unknown",
        "settlement_index_repair_required",
        "settlement_receipt_corrupt",
    }
    original_findings = report.get("findings", [])
    saved = [
        finding
        for finding in original_findings
        if isinstance(finding, dict) and finding.get("code") not in receipt_codes
    ]
    attention = epoch_settlement_receipt_attention(paths.root, epoch_id)
    fresh = detect_settlement_receipt_attention(attention)
    saved_events = {
        (detail["invocation"], detail["cursor"])
        for finding in saved
        if finding.get("code") == "optional_operation_failed"
        and isinstance(detail := finding.get("detail"), dict)
        and isinstance(detail.get("invocation"), str)
        and type(detail.get("cursor")) is int
    }
    fresh.extend(
        detect_optional_failures(
            tuple(
                record
                for record in epoch_optional_failures(paths.root, epoch_id)
                if (record["invocation"], record["cursor"]) not in saved_events
            )
        )
    )
    saved.extend(
        {
            "code": finding.code,
            "severity": finding.severity,
            "summary": finding.summary,
            "detail": finding.detail,
        }
        for finding in fresh
    )
    report["findings"] = saved
    saved_receipt_was_removed = len(original_findings) != len(saved) - len(fresh)
    if saved_receipt_was_removed:
        report["healthy"] = not any(
            finding.get("severity") in {"warning", "critical"} for finding in saved
        )
    elif any(finding.severity in {"warning", "critical"} for finding in fresh):
        report["healthy"] = False
    if report.get("unreadable"):
        report["healthy"] = None
    return report


# ---------------------------------------------------------------------------
# Promote-gate breakdown (the decision view — one round's promote/reject).
#


def _read_epoch_scoring_weights(
    paths: WorkspacePaths, epoch_id: str, inputs: EpochInputs | None = None
) -> Any:
    """Decode display settings through the shared scoring configuration reader."""
    from zicato.core import ScoringWeights

    raw = (
        inputs.scoring.copy()
        if inputs is not None
        else _read_json_value(layout_of(paths).scoring(epoch_id))
    )
    return ScoringWeights.from_json(raw if isinstance(raw, dict) else {})


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
    ``None`` additionally sets ``present=False``, so a run that recorded no
    scoring provenance renders nothing new; issue #19 added the field). Any
    unrecognised string degrades to ``kind="unknown"``
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
        # None (no provenance recorded) or "" — nothing to decompose.
        # ``present`` already records whether the field existed at all.
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
    cells = selected_measurements(paths, epoch_id, generation_id)
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
    paths: WorkspacePaths, epoch_id: str, challenger_id: str, inputs: EpochInputs | None = None
) -> dict[str, Any]:
    """The ``gate.override`` block for one challenger.

    Reads the challenger's persisted ``experiment.json`` outcome — which now
    carries ``operator_override`` + ``operator_override_reason`` whenever an
    operator force-promoted / force-rejected it through the control protocol —
    and projects it into ``{present, action, reason}``. ``present`` is
    ``False`` (action and reason ``None``) on every round the gate decided and
    on every record that stores no override. The block is therefore additive:
    a gate-decided breakdown keeps the shape it had before the block existed.
    ``action`` is ``"promote"`` / ``"reject"`` derived from the recorded
    ``tournament_decision``, so the decision view labels the override without
    re-deriving it.
    """
    absent: dict[str, Any] = {"present": False, "action": None, "reason": None}
    if not challenger_id:
        return absent
    exp = (
        inputs.experiment(challenger_id)
        if inputs is not None
        else read_experiment_body(paths.root, epoch_id, challenger_id)
    )
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
    *,
    inputs: EpochInputs | None = None,
) -> dict[str, Any]:
    """Read execution's rule results and the final candidate decision.

    Saved aggregates explain the comparison at decision time. Missing gate
    results remain unavailable. Live progress and per-judge comparisons are
    additional observations; neither can authorize or reconstruct a promotion.
    """
    from zicato.epoch.settlement_receipt import read_settlement_receipt

    if inputs is not None:
        inputs.check(paths, epoch_id)
    weights = _read_epoch_scoring_weights(paths, epoch_id, inputs)

    try:
        experiment = (
            inputs.experiment(challenger_id)
            if inputs is not None
            else read_experiment_body(paths.root, epoch_id, challenger_id)
        )
        receipt = (
            read_settlement_receipt(paths.root, epoch_id, experiment["round_index"])
            if isinstance(experiment, dict)
            else None
        )
        recorded_gate = None
        if receipt is not None and receipt.state == "committed":
            comparisons = [
                result
                for result in receipt.to_dict().get("gate_results", [])
                if result["champion"] == champion_id and result["challenger"] == challenger_id
            ]
            crowning_id = (receipt.field_record or {}).get("crowning_matchup_id")
            recorded_gate = next(
                (
                    result
                    for result in comparisons
                    if crowning_id and result.get("matchup_id") == crowning_id
                ),
                comparisons[-1] if comparisons else None,
            )
        if recorded_gate is not None:
            parent_agg, child_agg = (
                recorded_gate["parent_aggregate"],
                recorded_gate["child_aggregate"],
            )
        else:
            parent_agg = (
                (_gen_score_view(paths, epoch_id, champion_id) or None) if champion_id else None
            )
            child_agg = _gen_score_view(paths, epoch_id, challenger_id) or None
    except RecordError as exc:
        return {
            "epoch_id": epoch_id,
            "champion": champion_id,
            "challenger": challenger_id,
            "decision": "deferred",
            "reason": str(exc),
            "unreadable": str(exc),
            "rules": [],
        }

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
        # aggregates), so the decision view can show "47.58 → 57.70" without
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
        # ``present=False`` on a run that recorded no provenance, so a UI that
        # does not know the field renders nothing new.
        "scalar_decomposition": _build_scalar_decomposition(
            parent_agg,
            child_agg,
            _representative_drift_provenance(paths, epoch_id, champion_id) if champion_id else None,
            _representative_drift_provenance(paths, epoch_id, challenger_id),
        ),
        "primary_driver": None,
        # Bradley--Terry uncertainty pre-gate block: crowning may be held until
        # the rating separates the pair. Always present as a key;
        # ``rating.present`` is ``False`` on a run with no
        # ``promote_confidence_threshold`` in its structure params, so a UI that
        # does not know the field renders nothing new.
        "rating": build_rating_view(paths, epoch_id, champion_id, challenger_id, inputs=inputs),
        # Operator override block. ``present`` is ``False`` on every round the
        # gate decided and on every record storing no override, so a
        # gate-decided pair's breakdown keeps the shape it had before the block
        # existed; ``present=True`` carries ``{action, reason}`` when an
        # operator force-promoted or force-rejected THIS challenger, so the
        # decision view never presents the override as the gate's own verdict.
        "override": _build_override_block(paths, epoch_id, challenger_id, inputs),
    }

    # Echo the per-judge primary driver from the same source the decision
    # view's per-judge-comparison endpoint uses (best-effort; never fatal).
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

    if recorded_gate is not None:
        base.update(
            {
                key: value
                for key, value in recorded_gate.items()
                if key not in {"parent_aggregate", "child_aggregate"}
            }
        )
    if not isinstance(experiment, dict):
        return base
    outcome = experiment.get("outcome")
    if isinstance(outcome, dict) and experiment.get("parent_generation_id") == champion_id:
        base["decision"] = outcome["tournament_decision"]
        base["reason"] = outcome.get("rejection_reason", "")
    return base


def _read_evidence_parameters(
    paths: WorkspacePaths, epoch_id: str, inputs: EpochInputs | None = None
) -> tuple[float, int, int] | None:
    """Read the probability bar, replicate budget, and planned candidate count."""
    from zicato.core.types import ExperimentalConfig, TournamentStructure  # noqa: PLC0415
    from zicato.selection import make_strategy  # noqa: PLC0415
    from zicato.selection.evidence_gate import (  # noqa: PLC0415
        read_promote_confidence_threshold as _read_threshold,
    )
    from zicato.selection.evidence_gate import read_replicate_budget  # noqa: PLC0415

    raw = (
        inputs.scoring.copy()
        if inputs is not None
        else _read_json_value(layout_of(paths).scoring(epoch_id))
    )
    if not isinstance(raw, dict):
        return None
    tournament = raw.get("tournament")
    if not isinstance(tournament, dict):
        return None
    params = tournament.get("params")
    if not isinstance(params, dict):
        return None
    threshold = _read_threshold(params)
    if threshold is None:
        return None
    try:
        strategy = make_strategy(
            TournamentStructure(structure=tournament.get("structure", "gauntlet"), params=params),
            experimental=ExperimentalConfig(tournament_structures=True),
        )
    except ValueError:
        return None
    return threshold, read_replicate_budget(params), strategy.field_size()


def _recorded_pair_matches(rating: Mapping[str, Any], champion_id: str, challenger_id: str) -> bool:
    """Require independent evidence to identify the contestants it measured."""
    return (rating.get("champion_id"), rating.get("challenger_id")) == (
        champion_id,
        challenger_id,
    )


def build_rating_view(
    paths: WorkspacePaths,
    epoch_id: str,
    champion_id: str,
    challenger_id: str,
    *,
    inputs: EpochInputs | None = None,
) -> dict[str, Any]:
    """Read recorded independent confirmation without fitting selection matches.

    Strategy matchups may reuse measurements or select an apparent winner.
    Historical summaries without an independent confirmation basis cannot
    establish uncertainty or promotion confidence.
    """
    from zicato.epoch.journal import read_experiment_body  # noqa: PLC0415
    from zicato.selection.dead_letter import read_inconclusive  # noqa: PLC0415

    absent = {"present": False}
    if inputs is not None:
        inputs.check(paths, epoch_id)

    if challenger_id:
        if inputs is not None:
            captured = inputs.generations.get(challenger_id)
            if captured is not None and captured.unreadable is not None:
                return {**absent, "unreadable": captured.unreadable}
            experiment = inputs.experiment(challenger_id)
        else:
            try:
                experiment = read_experiment_body(paths.root, epoch_id, challenger_id)
            except RecordError as exc:
                return {**absent, "unreadable": str(exc)}
        outcome = experiment.get("outcome") if experiment is not None else None
        recorded = outcome.get("evidence") if isinstance(outcome, dict) else None
        if isinstance(recorded, dict) and (
            recorded.get("evidence_basis") == "independent_confirmation"
            or recorded.get("confirmation_status") == "disabled"
        ):
            if experiment is None or experiment.get("generation_id") != challenger_id:
                return {
                    **absent,
                    "unreadable": "confirmation generation differs from requested challenger",
                }
            if experiment is None or experiment.get("parent_generation_id") != champion_id:
                return {
                    **absent,
                    "unreadable": "confirmation parent differs from requested champion",
                }
            if recorded.get("evidence_basis") == "independent_confirmation" and not (
                _recorded_pair_matches(recorded, champion_id, challenger_id)
            ):
                return {
                    **absent,
                    "unreadable": "recorded confirmation pair differs from requested contestants",
                }
            return {**recorded, "next_duel": None}

    parameters = _read_evidence_parameters(paths, epoch_id, inputs)
    if parameters is None or not challenger_id:
        return absent
    threshold, _, _ = parameters

    # Prefer the authoritative dead-letter record for an inconclusive duel — it
    # carries the exact final block the driver computed (incl. the full
    # ci_history), so the dashboard never disagrees with the run's own verdict.
    try:
        dead_letter = read_inconclusive(paths.root, challenger_id)
    except RecordError as exc:
        return {**absent, "unreadable": str(exc)}
    if (
        dead_letter is not None
        and dead_letter.epoch_id == epoch_id
        and dead_letter.champion_id == champion_id
        and dead_letter.rating.get("evidence_basis") == "independent_confirmation"
    ):
        if not _recorded_pair_matches(dead_letter.rating, champion_id, challenger_id):
            return {
                **absent,
                "unreadable": "recorded confirmation pair differs from requested contestants",
            }
        out = dict(dead_letter.rating)
        out["next_duel"] = None
        out["ci_history"] = [dict(row) for row in dead_letter.ci_history]
        return out

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
        "n_duels": 0,
        "confirmation_status": "incomplete",
        "reason": "independent confirmation evidence is unavailable",
        "difference": None,
        "next_duel": None,
        "ci_history": [],
    }

    return block
