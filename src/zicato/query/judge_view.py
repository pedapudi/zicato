"""judge_view — extracted from the former dashboard state_reader monolith (pure move)."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from zicato.query import gate_view as _gate_view
from zicato.query._sqlite import (
    _opt_json,
    _opt_str,
    _row_bool,
    _row_keys,
    open_index_ro,
    open_index_ro_or_none,
)
from zicato.query.board_scan import iter_board_rows
from zicato.query.epoch_view import (
    _parse_board,
    build_epoch_view,
    build_epochs_summary,
)
from zicato.query.eval_view import facet_scores_for_generation, facets_by_entry
from zicato.query.lineage_view import build_lineage_view
from zicato.query.paths import (
    WorkspacePaths,
    _iso,
    _opt_bool,
    _preview,
    _read_json_value,
    _utc_now,
    coerce_float,
    layout_of,
    read_current_epoch,
)
from zicato.query.run_log import (
    RUN_LOG_DEFAULT_LIMIT,
    build_run_log,
)
from zicato.query.runtime_view import (
    derive_liveness,
    read_active_runs_view,
    read_active_tournament_dict,
    read_heartbeat_dict,
    read_lock_dict,
)
from zicato.query.tournament_view import (
    _champion_lineage,
    _opt_metrics,
    _opt_score,
    _read_gen_score,
    _tournament_id_for,
    build_bracket,
)


def build_per_judge_trend(paths: WorkspacePaths, epoch_id: str) -> dict[str, Any]:
    """Per-judge × generation matrix for one epoch.

    Returns ``{epoch_id, generations, judges: [{judge_name,
    by_generation: {gen_id: weighted_loss}}]}``. ``generations`` is the
    spine in lineage order (the promoted lineage when available, else
    every generation in directory order). The ``by_generation`` map is
    populated from :func:`zicato.index.query.judge_loss_trend` per judge.

    Best-effort: a never-indexed workspace yields empty ``judges`` (the
    lineage-derived ``generations`` spine still renders — this reader
    degrades field-by-field, not whole-payload).
    """
    from zicato.index.query import judge_loss_trend  # noqa: PLC0415

    # Discover the set of judges seen in this epoch by walking the
    # generations directly. The trend query is per-judge so we need a
    # judge list before we can call it. Routed through the READ-ONLY
    # index discipline: the bare write-mode connect this replaced both
    # contended for the write lock with the ingest writer AND created a
    # stray empty ``index.db`` on a never-indexed workspace (whose
    # presence then flipped LATER readers' degrade branches — the
    # order-contamination the U3 golden re-capture retired).
    judges: set[str] = set()
    index_absent = True
    with open_index_ro_or_none(paths.index_db) as conn:
        if conn is not None:
            index_absent = False
            try:
                rows = conn.execute(
                    "SELECT DISTINCT jl.judge_name "
                    "FROM judge_losses AS jl "
                    "JOIN runs AS r ON r.run_id = jl.run_id "
                    "WHERE r.epoch_id = ? "
                    "ORDER BY jl.judge_name",
                    (epoch_id,),
                ).fetchall()
                for r in rows:
                    if isinstance(r["judge_name"], str):
                        judges.add(r["judge_name"])
            except sqlite3.Error:
                pass

    # Resolve the spine — the promoted lineage when available, else
    # every generation in directory order. The L1 heatmap renders only
    # promoted-spine generations so the columns stay narrow.
    #
    # SCOPED at the walk. This used to walk every epoch and then filter
    # down to one, and the walk reads a JSON file per generation
    # directory — so a 60-epoch workspace read all of them to render one
    # epoch's matrix. ``epoch_id`` now does that filtering inside the
    # walk; an unknown id still yields the empty feed it always did.
    lineage_view = build_lineage_view(paths, epoch_id, include_ratings=False)
    epoch_gens = lineage_view.get("generations", [])
    spine = _champion_lineage(epoch_gens)
    if not spine:
        spine = [g["generation_id"] for g in epoch_gens]

    judge_rows: list[dict[str, Any]] = []
    for judge_name in sorted(judges):
        try:
            rows = judge_loss_trend(paths.index_db, epoch_id, judge_name)
        except Exception:  # noqa: BLE001
            rows = []
        by_gen: dict[str, float] = {}
        for r in rows:
            gid = r["generation_id"]
            val = r["total_weighted_loss"]
            if isinstance(gid, str) and isinstance(val, int | float):
                by_gen[gid] = float(val)
        judge_rows.append(
            {
                "judge_name": judge_name,
                "by_generation": by_gen,
            }
        )

    out: dict[str, Any] = {
        "epoch_id": epoch_id,
        "generations": spine,
        "judges": judge_rows,
    }
    if index_absent:
        # Harmonize with the sibling readers (gate_view / loop_view /
        # tournament_view / build_per_judge_for_generation): an ABSENT index
        # carries the actionable degrade note. The generations spine still
        # renders (field-by-field degrade) — a built-but-empty index gets no
        # note, only a genuinely un-built one.
        out["note"] = "index not built; run zicato repair index"
    return out


def build_per_judge_for_generation(
    paths: WorkspacePaths, epoch_id: str, generation_id: str
) -> dict[str, Any]:
    """Per-judge table for one generation.

    Returns ``{epoch_id, generation_id, judges: [{judge_name,
    weighted_loss, raw_loss, run_count, weight}]}`` keyed off
    :func:`zicato.index.query.judge_losses_for_generation`. A never-
    indexed workspace yields empty ``judges`` with a ``note``.
    """
    from zicato.index.query import judge_losses_for_generation  # noqa: PLC0415

    try:
        rows = judge_losses_for_generation(paths.index_db, epoch_id, generation_id)
    except Exception:  # noqa: BLE001
        return {
            "epoch_id": epoch_id,
            "generation_id": generation_id,
            "judges": [],
            "note": "index not built; run zicato repair index",
        }
    judges = [
        {
            "judge_name": r["judge_name"],
            "weighted_loss": (coerce_float(r["total_weighted_loss"])),
            "raw_loss": (coerce_float(r["total_raw_loss"])),
            "run_count": int(r["run_count"]) if isinstance(r["run_count"], int) else None,
            "weight": coerce_float(r["weight"]),
        }
        for r in rows
    ]
    return {
        "epoch_id": epoch_id,
        "generation_id": generation_id,
        "judges": judges,
    }


def build_per_entry_for_generation(
    paths: WorkspacePaths,
    epoch_id: str,
    generation_id: str,
) -> dict[str, Any]:
    """Per-entry breakdown of one generation, scoped via tournament_id FK.

    Returns ``{epoch_id, generation_id, tournament_id, mean_score,
    drift_present, facet_scores, entries: [{entry_id, run_id, drift_loss,
    pass_fail, runtime_ms, wall_clock_budget_exceeded, match_id, rung}]}``.

    ``mean_score`` is the generation's cached board-level mean, read off
    ``gen_score.json``. ``drift_present`` says whether the drift channel
    carries information for this generation at all, so a client hides the
    drift readouts instead of rendering a column of structural zeroes.

    ``facet_scores`` is ``{facets: {name: {scalar, mean_score,
    scored_count, entry_count}}, overall: {...} | None}`` — this candidate
    re-aggregated over each ``facet:`` board tag at the epoch's frozen
    weights, so a facet's ``scalar`` is directly comparable to the
    ``overall`` row beside it (see
    :func:`zicato.query.eval_view.facet_scores_for_generation`). Empty
    facets when the board declares no facet tag. The candidate dossier
    reads both from here.

    The tournament id is
    composed via :func:`_tournament_id_for` from the child generation's
    ``parent_generation_id`` field (its ``experiment.json``); a v0 seed
    with no parent yields ``tournament_id: None`` and the fallback walks
    :func:`zicato.index.query.loss_profiles_for_generation` directly.

    ``match_id`` is the per-board-run tournament-provenance tag — the
    matchup id this run executed within (e.g. ``"rung0_m2"``,
    ``"racing-final"``) — and ``rung`` is the coarser label derived from
    it (e.g. ``"rung 0"``, ``"final"``) via
    :func:`zicato.selection.strategy.rung_for_match_id`. Both are ``None``
    for an untagged run: a gauntlet duel (which never carries a
    ``match_id``) or a legacy run persisted before the tag existed —
    additive, never an error.

    A never-indexed workspace yields empty ``entries`` with a ``note``.
    """
    from zicato.index.query import (  # noqa: PLC0415
        loss_profiles_for_generation,
        loss_profiles_for_tournament,
    )

    # Resolve the parent_generation_id from the child's experiment.json
    # so we can compose the FK. The reader is best-effort: a missing
    # / malformed file falls back to the generation-scoped query.
    exp_path = layout_of(paths).experiment(epoch_id, generation_id)
    parent_gen_id: str | None = None
    raw_exp = _read_json_value(exp_path)
    if isinstance(raw_exp, dict):
        raw_parent = raw_exp.get("parent_generation_id")
        if isinstance(raw_parent, str) and raw_parent:
            parent_gen_id = raw_parent

    tournament_id: str | None = None
    rows: list[Any] = []
    if parent_gen_id is not None:
        tournament_id = _tournament_id_for(epoch_id, parent_gen_id, generation_id)
        try:
            rows = loss_profiles_for_tournament(paths.index_db, tournament_id)
        except Exception:  # noqa: BLE001
            rows = []
    if not rows:
        # Either the FK lookup found nothing (v1 index without
        # backfill) or there is no parent. Walk the generation-scoped
        # query so a completed-but-orphaned tournament still surfaces.
        try:
            rows = loss_profiles_for_generation(paths.index_db, epoch_id, generation_id)
        except Exception:  # noqa: BLE001
            rows = []

    from zicato.selection.strategy import rung_for_match_id  # noqa: PLC0415

    # The tolerant row accessors (``_row_keys`` / ``_opt_str`` / ``_row_bool``)
    # are the shared set in ``zicato.query._sqlite`` — the cached-champion
    # provenance columns (``cached`` / ``source_epoch`` / ``source_run``) are
    # additive in a later schema, so a stale index loads unchanged.

    def _match_id_of(row: Any) -> str | None:
        # ``match_id`` lands in schema v4. A stale index opened before
        # the migration ran would not carry the column; tolerate its
        # absence (and a NULL value) so an old index loads, not errors.
        if "match_id" not in _row_keys(row):
            return None
        value = row["match_id"]
        return value if isinstance(value, str) and value else None

    def _drift_observed(row: Any, drift_loss: float | None) -> bool:
        # Did this run OBSERVE drift at all? An adapter that emits no drift
        # stream still records a structural 0.0 with an empty ``drift_counts``,
        # which is indistinguishable on the wire from a run that watched for
        # drift and saw none. Either a recorded drift event or a non-zero loss
        # proves the channel carries signal; nothing else does. Mirrors the
        # matchup grid's predicate, sourced from the same persisted field —
        # here off the index's verbatim ``loss_json`` blob, with the row's
        # ``drift_loss`` column as the fallback for a blob-less stale index.
        if "loss_json" in _row_keys(row):
            lj = _opt_json(row["loss_json"])
            if isinstance(lj, dict) and lj.get("drift_counts"):
                return True
        return drift_loss not in (None, 0.0)

    def _score_metrics_of(row: Any) -> tuple[float | None, dict[str, float] | None]:
        # The continuous per-entry outcome + its precision/recall
        # decomposition (#18) live in the raw ``loss_json`` blob the index
        # stores verbatim, NOT in a dedicated column — so a stale index
        # without new columns still surfaces the score. Absent / malformed
        # blob -> (None, None), which renders by the bool pass bit exactly
        # as before.
        if "loss_json" not in _row_keys(row):
            return None, None
        lj = _opt_json(row["loss_json"])
        if not isinstance(lj, dict):
            return None, None
        return _opt_score(lj.get("score")), _opt_metrics(lj.get("metrics"))

    entry_facets = facets_by_entry(paths, epoch_id)
    entries = []
    drift_present = False
    for r in rows:
        match_id = _match_id_of(r)
        entry_score, entry_metrics = _score_metrics_of(r)
        entry_drift = coerce_float(r["drift_loss"])
        drift_present = drift_present or _drift_observed(r, entry_drift)
        entries.append(
            {
                "entry_id": r["entry_id"],
                "run_id": r["run_id"],
                "generation_id": r["generation_id"],
                "drift_loss": entry_drift,
                "pass_fail": _opt_bool(r["pass_fail"]),
                # Continuous per-entry outcome + precision/recall (#18),
                # parsed from the row's loss_json blob. ``None`` for a
                # pre-score entry (renders by pass_fail as before).
                "score": entry_score,
                "metrics": entry_metrics,
                "runtime_ms": (int(r["runtime_ms"]) if isinstance(r["runtime_ms"], int) else None),
                "wall_clock_budget_exceeded": bool(r["wall_clock_budget_exceeded"])
                if r["wall_clock_budget_exceeded"] is not None
                else None,
                # Per-board-run tournament provenance (additive). ``None``
                # for an untagged run (gauntlet duel / legacy run).
                "match_id": match_id,
                "rung": rung_for_match_id(match_id),
                # Cached-champion provenance (additive). When the champion was
                # reused in fast mode this row's scalar comes from a PRIOR
                # epoch/run rather than a re-execution this round; the epoch's
                # OWN loss.json / index materializes the provenance so this read
                # stays epoch-local. ``cached`` False / ``source_*`` None for a
                # freshly-executed run.
                "cached": _row_bool(r, "cached"),
                "source_epoch": _opt_str(r, "source_epoch"),
                "source_run": _opt_str(r, "source_run"),
                # The ``facet:`` slices this entry belongs to (BOARD-FORMAT.md
                # §1.4), sorted. Carried on the ROW because it is a property of
                # the entry, not of the run: the per-board drill-down reads it
                # to name the slices the entry feeds without re-reading the
                # board. ``[]`` for an untagged entry.
                "facets": list(entry_facets.get(r["entry_id"], ())),
            }
        )

    # Per-generation mean continuous outcome (#18), read from the cached
    # gen_score.json — never recomputed. ``None`` when the aggregate
    # predates the field, so the candidate view degrades to its pass-rate
    # summary. Folded alongside the per-entry scores so the dossier can
    # show a single board-level score number.
    gen_mean_score = _opt_score(_read_gen_score(paths, epoch_id, generation_id).get("mean_score"))

    return {
        "epoch_id": epoch_id,
        "generation_id": generation_id,
        "tournament_id": tournament_id,
        "mean_score": gen_mean_score,
        # Does the drift channel carry information for this generation? False
        # when every run recorded a structural zero with no drift events — the
        # adapter emits no drift stream, so the per-entry ``drift_loss`` column
        # means nothing and a client hides it rather than painting zeroes.
        "drift_present": drift_present,
        # This candidate re-aggregated per ``facet:`` board tag, plus the
        # same aggregate over every entry as the ``overall`` row to compare
        # against (BOARD-FORMAT.md §1.4). Computed server-side: DQ1 keeps
        # the group-by off the client. Empty facets ⇒ the table does not
        # paint. Diagnostic: nothing downstream of this key feeds a decision.
        "facet_scores": facet_scores_for_generation(paths, epoch_id, generation_id, entry_facets),
        "entries": entries,
    }


def build_per_judge_comparison(
    paths: WorkspacePaths,
    epoch_id: str,
    champion_id: str,
    challenger_id: str,
) -> dict[str, Any]:
    """Per-judge Δ between two generations.

    Returns ``{epoch_id, champion, challenger, judges: [{judge_name,
    champion_weighted_loss, challenger_weighted_loss, delta}],
    primary_driver}``. ``primary_driver`` is the judge_name with the
    largest absolute delta; ``None`` when no judge fired on either side.

    A never-indexed workspace yields empty ``judges`` with a ``note``.
    """
    from zicato.index.query import judge_losses_for_generation  # noqa: PLC0415

    try:
        champ_rows = judge_losses_for_generation(paths.index_db, epoch_id, champion_id)
        chal_rows = judge_losses_for_generation(paths.index_db, epoch_id, challenger_id)
    except Exception:  # noqa: BLE001
        return {
            "epoch_id": epoch_id,
            "champion": champion_id,
            "challenger": challenger_id,
            "judges": [],
            "primary_driver": None,
            "note": "index not built; run zicato repair index",
        }

    by_judge: dict[str, dict[str, float | None]] = {}
    for r in champ_rows:
        name = r["judge_name"]
        if not isinstance(name, str):
            continue
        v = r["total_weighted_loss"]
        by_judge.setdefault(name, {"champion": None, "challenger": None})["champion"] = (
            coerce_float(v)
        )
    for r in chal_rows:
        name = r["judge_name"]
        if not isinstance(name, str):
            continue
        v = r["total_weighted_loss"]
        by_judge.setdefault(name, {"champion": None, "challenger": None})["challenger"] = (
            coerce_float(v)
        )

    judges: list[dict[str, Any]] = []
    primary_driver: str | None = None
    primary_abs: float = -1.0
    for name in sorted(by_judge):
        sides = by_judge[name]
        champ_v = sides.get("champion")
        chal_v = sides.get("challenger")
        delta: float | None = None
        if isinstance(champ_v, int | float) and isinstance(chal_v, int | float):
            delta = float(chal_v) - float(champ_v)
        elif isinstance(chal_v, int | float):
            delta = float(chal_v)
        elif isinstance(champ_v, int | float):
            delta = -float(champ_v)
        judges.append(
            {
                "judge_name": name,
                "champion_weighted_loss": champ_v,
                "challenger_weighted_loss": chal_v,
                "delta": delta,
            }
        )
        if delta is not None and abs(delta) > primary_abs:
            primary_abs = abs(delta)
            primary_driver = name

    return {
        "epoch_id": epoch_id,
        "champion": champion_id,
        "challenger": challenger_id,
        "judges": judges,
        "primary_driver": primary_driver,
    }


def resolve_run_id_for_entry(
    paths: WorkspacePaths, epoch_id: str, generation_id: str, entry_id: str
) -> str:
    """Recover the canonical ``run_id`` for one ``(epoch, gen, entry)`` run.

    The L4 dashboard view routes by board-entry id; the index keys every
    per-judge row by run id. The entry's own ``loss.json`` carries the
    canonical id — the key the ``judge_losses`` table is bound to. Falls
    back to the run-directory name (``entry_id``) when the file is
    missing / malformed, so the caller always has a lookup key (DQ3 —
    never raises). Feeds :func:`build_per_judge_for_run`.
    """
    loss_path = (
        paths.epochs / epoch_id / "generations" / generation_id / "runs" / entry_id / "loss.json"
    )
    try:
        loss = json.loads(loss_path.read_text(encoding="utf-8"))
        if isinstance(loss, dict):
            raw_run = loss.get("run_id")
            if isinstance(raw_run, str) and raw_run:
                return raw_run
    except (FileNotFoundError, OSError, json.JSONDecodeError, ValueError):
        pass
    return entry_id  # Best-effort fallback: directory name.


def build_per_judge_for_run(paths: WorkspacePaths, run_id: str) -> dict[str, Any]:
    """Per-judge breakdown for one run.

    Returns ``{run_id, judges: [{judge_name, weighted_loss, raw_loss,
    weight}]}``. A never-indexed workspace yields empty ``judges``.
    """
    from zicato.index.query import judge_losses_for_run  # noqa: PLC0415

    try:
        rows = judge_losses_for_run(paths.index_db, run_id)
    except Exception:  # noqa: BLE001
        return {"run_id": run_id, "judges": [], "note": "index not built; run zicato repair index"}
    judges = [
        {
            "judge_name": r["judge_name"],
            "weighted_loss": (coerce_float(r["weighted_loss"])),
            "raw_loss": (coerce_float(r["raw_loss"])),
            "weight": coerce_float(r["weight"]),
        }
        for r in rows
    ]
    return {"run_id": run_id, "judges": judges}


def _load_run_loss(
    paths: WorkspacePaths,
    epoch_id: str,
    generation_id: str,
    entry_id: str,
) -> dict[str, Any] | None:
    """Read a board-entry run's ``loss.json`` defensively.

    Returns the parsed dict or ``None`` when the file is absent or
    unreadable. Used by :func:`build_expectation_outcomes_for_run` and
    :func:`build_run_header` to project structured fields without
    requiring an indexed workspace.
    """
    loss_path = layout_of(paths).loss(epoch_id, generation_id, entry_id)
    try:
        loss = json.loads(loss_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError, ValueError):
        return None
    return loss if isinstance(loss, dict) else None


def build_expectation_outcomes_for_run(
    paths: WorkspacePaths,
    epoch_id: str,
    generation_id: str,
    entry_id: str,
) -> dict[str, Any]:
    """Structured expectation outcomes for a single run (L4).

    The reducer stamps a single ``expectation_result`` on each run's
    ``loss.json`` — a dict shaped ``{kind, passed, detail}`` (see
    :class:`zicato.core.types.ExpectationResult`). This reader projects
    it into a list-shaped payload so the L4 view can render a uniform
    table regardless of whether the entry carried zero, one, or
    (forward-compat) several expectations.

    Returns ``{epoch_id, generation_id, entry_id, outcomes: [...]}``
    where each outcome has the fields:

    * ``kind`` — the matcher discriminant (``predicate``, ``regex``,
      ``expected_text``, ``json_schema``, ``rubric``, or a custom
      kind-like string the reducer happened to stamp).
    * ``passed`` — ``True`` / ``False`` / ``None`` (the matcher could
      not produce a verdict).
    * ``detail`` — human-readable explanation (regex match position,
      judge rationale, predicate return). Empty string when the
      matcher had nothing to say.
    * ``judge_name`` — the rubric's judge identity when ``kind`` is
      ``rubric``; ``None`` otherwise.
    * ``score`` — the rubric's numeric score when present; ``None``
      otherwise.

    An entry with no expectation (``expectation_result`` is ``None``)
    or no on-disk ``loss.json`` yields an empty ``outcomes`` list — the
    L4 view shows ``(no expectations recorded for this run)``.
    """
    empty: dict[str, Any] = {
        "epoch_id": epoch_id,
        "generation_id": generation_id,
        "entry_id": entry_id,
        "outcomes": [],
    }
    loss = _load_run_loss(paths, epoch_id, generation_id, entry_id)
    if loss is None:
        return empty
    raw = loss.get("expectation_result")
    if raw is None:
        return empty

    # The reducer stamps a single dict today; we normalise to a list to
    # keep the wire shape stable when multi-expectation entries land.
    if isinstance(raw, dict):
        items: list[Any] = [raw]
    elif isinstance(raw, list):
        items = list(raw)
    else:
        return empty

    outcomes: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        kind = item.get("kind")
        kind_str = str(kind) if isinstance(kind, str) else None
        passed_raw = item.get("passed")
        if isinstance(passed_raw, bool):
            passed: bool | None = passed_raw
        else:
            passed = None
        detail = item.get("detail")
        detail_str = detail if isinstance(detail, str) else ""
        judge_raw = item.get("judge_name")
        judge_name = judge_raw if isinstance(judge_raw, str) and judge_raw else None
        score_raw = item.get("score")
        score: float | None
        if isinstance(score_raw, int | float) and not isinstance(score_raw, bool):
            score = float(score_raw)
        else:
            score = None
        outcomes.append(
            {
                "kind": kind_str,
                "passed": passed,
                "detail": detail_str,
                "judge_name": judge_name,
                "score": score,
            }
        )
    return {
        "epoch_id": epoch_id,
        "generation_id": generation_id,
        "entry_id": entry_id,
        "outcomes": outcomes,
    }


def build_run_header(
    paths: WorkspacePaths,
    epoch_id: str,
    generation_id: str,
    entry_id: str,
) -> dict[str, Any]:
    """Per-run header metrics (L4).

    Projects the numeric / verdict header fields from a board-entry
    run's ``loss.json``. The L4 page already shows ``drift_loss`` and
    ``pass_fail`` from the per-entry table; this reader surfaces the
    remaining header fields the previous placeholder promised:

    * ``runtime_ms`` — total wall-clock duration in ms.
    * ``tokens_spent`` — LLM token cost as recorded by the harness.
    * ``output_chars`` — characters in the run's final output.
    * ``turns_completed`` — conversational turns executed (multi-turn
      only; ``None`` for single-turn).
    * ``plan_revisions`` — count of plan-revision events observed.
    * ``wall_clock_budget_exceeded`` — ``True`` iff the run was force-
      aborted by its budget.

    Plus the headline verdict numbers so the frontend can render the
    full strip from one payload:

    * ``drift_loss``, ``pass_fail``, ``run_id``.

    Also surfaces the ADK session id persisted in ``loss.json`` by the
    reducer, so the L4 header can deep-link into harmonograf at the
    run's execution trace without a second roundtrip to ``events.jsonl``:

    * ``adk_session_id`` — the goldfive/ADK session id for this run.

    Every field defaults to ``None`` when ``loss.json`` is absent or
    missing the key; the response shape is stable so the L4 renderer
    never branches on whether the file exists.
    """
    keys = (
        "drift_loss",
        "pass_fail",
        "runtime_ms",
        "tokens_spent",
        "output_chars",
        "turns_completed",
        "plan_revisions",
        "wall_clock_budget_exceeded",
        "run_id",
        "adk_session_id",
    )
    header: dict[str, Any] = {
        "epoch_id": epoch_id,
        "generation_id": generation_id,
        "entry_id": entry_id,
    }
    for k in keys:
        header[k] = None
    loss = _load_run_loss(paths, epoch_id, generation_id, entry_id)
    if loss is None:
        return header
    for k in keys:
        v = loss.get(k)
        # Pass scalars (numeric / bool / str) and ``None`` through;
        # discard nested dicts / lists which are not header material.
        if v is None or isinstance(v, int | float | str | bool):
            header[k] = v
    # ONE spelling on the wire: pass_fail is a JSON boolean (or null).
    header["pass_fail"] = _opt_bool(header["pass_fail"])
    return header


#: How long a mutation-point count may be reused before it is re-walked.
#:
#: The enumeration is not cheap — it opens and AST-parses every file under
#: every source root — and it sat on the ``/api/environment`` path, which the
#: dashboard hits on every heartbeat, many times a second. Nothing here needs
#: to be that fresh: mutation points change only when a patch lands, which
#: happens at ROUND cadence (seconds to minutes), so a few seconds of staleness
#: on a display counter is honest where a per-request walk was merely wasteful.
#:
#: A TTL rather than a content key because no cheap honest key exists: the
#: count follows the file CONTENTS of a whole tree, and the only key that
#: tracks those is the walk itself. Directory mtimes do not propagate from
#: nested edits, so an mtime key would go stale silently and without bound —
#: strictly worse than a bounded staleness the reader can reason about.
_MUTATION_COUNT_TTL_S: float = 5.0

#: ``{(workspace root, *source roots): (expires_at_monotonic, count)}``. The
#: workspace root is IN the key, not just the trees: the surface is activated
#: from that workspace's contract, so two workspaces over identical trees can
#: legitimately enumerate different counts and must not share an entry.
_MUTATION_COUNT_CACHE: dict[tuple[str, ...], tuple[float, int]] = {}


def _mutation_point_count(workspace_root: Path, source_roots: list[str]) -> int:
    """Mutation points across ``source_roots``, cached for :data:`_MUTATION_COUNT_TTL_S`.

    Best-effort so a malformed source tree never bubbles up to the dashboard
    endpoint as a 500 — the enumerator walks every source root for
    ``# zicato:mutable`` markers plus a goldfive manifest if one exists, and any
    failure reads as ``0``.

    The surface is ACTIVATED from the workspace first, so the count is of the
    surface the RUN sees — the contract's declared file types, not the built-ins
    alone. Counting the built-in surface would under-report every workspace that
    declares extra file types, which is the whole point of declaring them.

    That activation installs a PROCESS-GLOBAL table, and a cache hit skips it —
    safe only because nothing depends on this call for that side effect. Every
    other enumerating caller (the mutations CLI, the dashboard's mutations
    endpoint, the evolve loop, propose) activates the surface itself before its
    own walk, exactly as ``activate_mutation_surface`` documents. If that ever
    stops being true, hoist the activation OUT of the cached path rather than
    widening the key.

    Only the COUNT is cached, never the enumeration. ``mutation/enumerator.py``
    is explicit that spans must not be cached — line numbers drift as patches
    land, and a stale span clobbers the wrong lines — but that hazard belongs to
    the applier, which re-enters :func:`enumerate_mutations` itself. A count
    that is a few seconds behind mis-renders a number; it cannot mis-apply a
    patch.

    A failed walk is cached like any other result. Re-walking a broken tree on
    every heartbeat is the exact cost this exists to avoid, and the TTL bounds
    how long a fixed tree keeps reading ``0``.
    """
    if not source_roots:
        return 0
    key = (str(workspace_root), *source_roots)
    now = time.monotonic()
    cached = _MUTATION_COUNT_CACHE.get(key)
    if cached is not None and cached[0] > now:
        return cached[1]
    try:
        from zicato.mutation.enumerator import enumerate_mutations  # noqa: PLC0415
        from zicato.workspace_loader import activate_mutation_surface  # noqa: PLC0415

        activate_mutation_surface(workspace_root)
        count = len(enumerate_mutations([Path(r) for r in source_roots]))
    except Exception:  # noqa: BLE001 — best-effort
        count = 0
    _MUTATION_COUNT_CACHE[key] = (now + _MUTATION_COUNT_TTL_S, count)
    return count


def build_workspace_identity(paths: WorkspacePaths) -> dict[str, Any]:
    """Structured workspace identity block — Phase 1's L0 env object.

    Replaces the bare ``str(paths.root)`` previously surfaced as
    ``state.workspace``. Returns an object with the fields the L0 view's
    environment-configuration table renders:

    * ``root`` — absolute path to the ``.zicato`` directory.
    * ``adk_entrypoint`` — the adapter's entrypoint (e.g.
      ``mod:agent``) when ``config.json`` carries one, else ``None``.
    * ``source_roots`` — every ``mutable_trees`` entry from
      ``config.json`` (empty list when absent).
    * ``board_path`` / ``brief_path`` / ``scoring_path`` — absolute
      paths to the current epoch's contract files when an epoch is
      live, else ``None``.
    * ``mutation_point_count`` — the count of mutation points the
      enumerator finds across ``source_roots``. ``0`` when the
      enumerator fails or there are no source roots — never raises.
      DELIBERATELY UNRENDERED, and it should stay that way: the Mutations
      view already shows a site count, taken from the per-epoch mutation
      matrix, which is the better-scoped number. A second count computed
      workspace-wide at a different moment can legitimately disagree with
      it, and two near-identical tiles that sometimes contradict each
      other is a worse surface than one. Kept on the payload for API
      consumers, and cheap now (see :func:`_mutation_point_count`).
    * ``instance_id`` — heartbeat's ``instance_id`` when present,
      else ``"default"`` (the runtime's seed default).
    * ``created_at`` — heartbeat's ``started_at`` when present, else
      ``None`` (the workspace is too young to have a heartbeat).
    """
    cfg = _read_json_value(paths.root / "config.json")
    adapter = cfg.get("adapter") if isinstance(cfg, dict) else None
    adapter = adapter if isinstance(adapter, dict) else {}

    entrypoint = adapter.get("entrypoint")
    if not isinstance(entrypoint, str) or not entrypoint:
        if isinstance(cfg, dict):
            for key in ("adk_entrypoint", "entrypoint"):
                val = cfg.get(key)
                if isinstance(val, str) and val:
                    entrypoint = val
                    break
            else:
                entrypoint = None
        else:
            entrypoint = None

    raw_trees = adapter.get("mutable_trees")
    if not isinstance(raw_trees, list) and isinstance(cfg, dict):
        raw_trees = cfg.get("mutable_trees")
    if isinstance(raw_trees, list):
        source_roots = [t for t in raw_trees if isinstance(t, str)]
    else:
        source_roots = []

    epoch_id = read_current_epoch(paths)
    if epoch_id is not None:
        layout = layout_of(paths)
        board_path = str(layout.board(epoch_id))
        brief_path_candidate = layout.brief(epoch_id)
        if not brief_path_candidate.exists():
            legacy = layout.legacy_rubric(epoch_id)
            brief_path = str(legacy) if legacy.exists() else str(brief_path_candidate)
        else:
            brief_path = str(brief_path_candidate)
        scoring_path = str(layout.scoring(epoch_id))
    else:
        board_path = None
        brief_path = None
        scoring_path = None

    mutation_point_count = _mutation_point_count(paths.root, source_roots)

    hb = read_heartbeat_dict(paths)
    instance_id = "default"
    created_at: str | None = None
    if isinstance(hb, dict):
        raw_iid = hb.get("instance_id")
        if isinstance(raw_iid, str) and raw_iid:
            instance_id = raw_iid
        raw_started = hb.get("started_at")
        if isinstance(raw_started, str) and raw_started:
            created_at = raw_started

    return {
        "root": str(paths.root),
        "adk_entrypoint": entrypoint,
        "source_roots": source_roots,
        "board_path": board_path,
        "brief_path": brief_path,
        "scoring_path": scoring_path,
        "mutation_point_count": mutation_point_count,
        "instance_id": instance_id,
        "created_at": created_at,
    }


def build_environment(
    paths: WorkspacePaths, run_log_limit: int = RUN_LOG_DEFAULT_LIMIT
) -> dict[str, Any]:
    """One coalesced snapshot of the whole instantiated zicato environment.

    ``GET /api/environment`` returns this. It folds together every
    cross-view feed the dashboard needs — the epoch contract, the live
    and resolved tournaments, the generation lineage, active runs, loop
    health, the heartbeat, and the run-log tail — so the front-end can
    refresh the entire environment view with ONE request per change
    instead of fanning out to six endpoints many times a second.

    ``epochs`` is a lightweight per-epoch summary list -- ``{epoch_id,
    goal}`` -- so the Overview's epochs table can show what each epoch
    is trying to accomplish without a per-epoch ``/api/epoch`` fetch.

    ``workspace`` is now a structured identity block (see
    :func:`build_workspace_identity`) so the L0 view can render
    entrypoint / source roots / contract paths / mutation-point count
    without a second fetch. The legacy callers that expected a plain
    string still find the root path on ``workspace.root``.

    Every component degrades independently: a missing or unreadable
    input becomes an empty / ``None`` value, never an exception, so this
    function — like every reader here — cannot 500 an endpoint.
    """
    # ``health`` here is the dashboard *service* identity (version /
    # port / build) and is supplied by the /api/health route handler,
    # not this reader — it is intentionally absent from the environment
    # payload. ``heartbeat`` is the orchestrator's runtime heartbeat.
    #
    # ONE lineage walk for the whole payload. Two fields need the generations
    # feed — ``generations`` serves it verbatim and ``score_trajectory`` reads
    # its order — and each used to build it independently. The walk reads a
    # JSON file per generation, so that second build was the single largest
    # cost in this reader (cProfile: 84% of build_environment, ncalls=2).
    generations = build_lineage_view(paths)
    epoch_id = read_current_epoch(paths)
    epoch_generations = {
        "generations": [
            node
            for node in generations.get("generations", [])
            if isinstance(node, dict) and node.get("epoch_id") == epoch_id
        ]
    }
    return {
        "workspace": build_workspace_identity(paths),
        "epoch_id": epoch_id,
        "epoch": build_epoch_view(paths, lineage_view=epoch_generations),
        "epochs": build_epochs_summary(paths),
        "active_tournament": read_active_tournament_dict(paths),
        "tournaments": build_bracket(paths),
        # SERVED verbatim (the environment feed) — the rating triple stays.
        "generations": generations,
        "score_trajectory": _gate_view.build_score_trajectory(paths, lineage=generations),
        "active_runs": read_active_runs_view(paths),
        "health_report": _gate_view.build_health_report(paths),
        "heartbeat": read_heartbeat_dict(paths),
        # The served tri-state (runtime_view.derive_liveness) — the one
        # answer to "is anything running?", so the environment feed and
        # the SSE snapshot cannot disagree about it.
        "liveness": derive_liveness(paths),
        "lock": read_lock_dict(paths),
        "run_log": build_run_log(paths, run_log_limit),
        "generated_at": _iso(_utc_now()),
    }


# ---------------------------------------------------------------------------
# Sidebar search — entries / judges / patches / mutations
# ---------------------------------------------------------------------------
#
# The sidebar exposes an always-visible search bar (no per-page navigation
# away from the current view). ``build_search_results`` walks the live
# workspace for substring + exact matches across four categories:
#
#   * entries   — id substring against the current epoch's ``board.jsonl``
#   * judges    — name substring against in-board judges + index judge_losses
#   * patches   — mutation_id / rationale substring against the index
#   * mutations — mutation_id substring against the index's patches table
#
# Each category is independently capped at :data:`SEARCH_LIMIT_PER_CATEGORY`
# results. Exact matches are sorted before substring matches so a query
# that names an id outright surfaces it first. Empty / whitespace queries
# short-circuit to empty result sets so the caller cannot tax the index
# with a degenerate scan.

#: Per-category cap on the number of results returned. The sidebar UI is
#: narrow; ten matches per category is enough to surface the obvious
#: targets without overwhelming the panel.
SEARCH_LIMIT_PER_CATEGORY = 10


def _empty_search_result() -> dict[str, Any]:
    return {
        "entries": [],
        "judges": [],
        "patches": [],
        "mutations": [],
    }


def _collect_judge_names_from_board_file(path: Path) -> set[str]:
    """Walk a raw ``board.jsonl`` and union every judge name.

    The tolerant row walk lives in :mod:`zicato.query.board_scan` — shared
    with the matchup grid's facet-tag read so both readers degrade the same
    way on a torn or non-UTF-8 board.
    """
    names: set[str] = set()
    for obj in iter_board_rows(path):
        judges = obj.get("judges")
        if not isinstance(judges, list):
            continue
        for j in judges:
            if not isinstance(j, dict):
                continue
            name = j.get("name")
            if isinstance(name, str) and name:
                names.add(name)
    return names


def _collect_judge_names_from_index(db_path: Path) -> set[str]:
    """Union of distinct ``judge_name`` values in the analytical index."""
    names: set[str] = set()
    try:
        with open_index_ro(db_path) as conn:
            cur = conn.execute("SELECT DISTINCT judge_name FROM judge_losses")
            for row in cur.fetchall():
                if isinstance(row[0], str) and row[0]:
                    names.add(row[0])
    except Exception:  # noqa: BLE001 — best-effort; absent index / missing table is OK
        return names
    return names


def _sort_by_match_quality(
    items: list[dict[str, Any]], key: str, q_lower: str
) -> list[dict[str, Any]]:
    """Sort exact matches (case-insensitive) before substring matches.

    Each item carries a ``match_kind`` field set to ``"exact"`` when the
    item's ``key`` equals the query case-insensitively, ``"substring"``
    otherwise. Ties within a kind are sorted by the matched field.
    """

    def _rank(item: dict[str, Any]) -> tuple[int, str]:
        val = str(item.get(key, "") or "")
        is_exact = val.lower() == q_lower
        item["match_kind"] = "exact" if is_exact else "substring"
        return (0 if is_exact else 1, val.lower())

    items.sort(key=_rank)
    return items


def build_search_results(paths: WorkspacePaths, query: str) -> dict[str, Any]:
    """Search entries / judges / patches / mutations for substring matches.

    Returns a dict keyed by category, each value a list of small match
    records carrying enough fields to build a navigation link client-side.
    Every category is independently capped at
    :data:`SEARCH_LIMIT_PER_CATEGORY`; exact (case-insensitive) matches
    sort before substring matches.

    The current epoch (as recorded by ``current_epoch``) bounds the entry
    + judge scans. Patch + mutation scans cover every epoch in the index
    so an operator can locate a historical mutation across the workspace.
    A degenerate query (empty / whitespace) short-circuits to empty
    results so the caller cannot accidentally fan out a wide scan.
    """
    q = (query or "").strip()
    if not q:
        return _empty_search_result()
    q_lower = q.lower()

    result = _empty_search_result()
    epoch_id = read_current_epoch(paths)

    # --- entries: walk the current epoch's board.jsonl ---------------
    layout = layout_of(paths)
    entry_hits: list[dict[str, Any]] = []
    if epoch_id:
        board_path = layout.board(epoch_id)
        board = _parse_board(board_path)
        if board:
            for entry in board:
                eid = entry.get("entry_id")
                if not isinstance(eid, str) or not eid:
                    continue
                if q_lower in eid.lower():
                    entry_hits.append({"id": eid})
    entry_hits = _sort_by_match_quality(entry_hits, "id", q_lower)
    result["entries"] = entry_hits[:SEARCH_LIMIT_PER_CATEGORY]

    # --- judges: board + index union ---------------------------------
    judge_names: set[str] = set()
    if epoch_id:
        judge_names |= _collect_judge_names_from_board_file(layout.board(epoch_id))
    judge_names |= _collect_judge_names_from_index(paths.index_db)
    judge_hits: list[dict[str, Any]] = [{"name": n} for n in judge_names if q_lower in n.lower()]
    judge_hits = _sort_by_match_quality(judge_hits, "name", q_lower)
    result["judges"] = judge_hits[:SEARCH_LIMIT_PER_CATEGORY]

    # --- patches + mutations: index scan ------------------------------
    # Both share the same patches table; one scan populates both, since
    # a mutation_id hit also implies the patch is interesting and a
    # rationale-only hit is a patch-only match.
    if paths.index_db.is_file():
        try:
            with open_index_ro(paths.index_db) as conn:
                # The LIKE patterns mirror the substring semantics the
                # frontend describes to operators. SQLite's LIKE is
                # case-insensitive for ASCII by default — the typical
                # case for mutation ids + rationale text.
                like = f"%{q}%"
                rows = conn.execute(
                    "SELECT patch_id, epoch_id, generation_id, mutation_id, "
                    "       op, rationale FROM patches "
                    "WHERE mutation_id LIKE ? OR rationale LIKE ? "
                    "ORDER BY epoch_id ASC, generation_id ASC",
                    (like, like),
                ).fetchall()
        except Exception:  # noqa: BLE001 — best-effort
            rows = []

        patch_hits: list[dict[str, Any]] = []
        mutation_hits: list[dict[str, Any]] = []
        seen_mutations: set[tuple[str, str, str]] = set()
        for row in rows:
            patch_id = row["patch_id"]
            ep_id = row["epoch_id"]
            gen_id = row["generation_id"]
            mut_id = row["mutation_id"]
            rationale = row["rationale"] or ""
            snippet = _preview(rationale) if rationale else ""
            patch_hits.append(
                {
                    "patch_id": patch_id,
                    "epoch_id": ep_id,
                    "generation_id": gen_id,
                    "mutation_id": mut_id,
                    "rationale_snippet": snippet,
                }
            )
            # A mutation row is interesting only when the substring
            # actually hits the mutation_id (a rationale-only match
            # belongs in patches but not in mutations).
            if isinstance(mut_id, str) and mut_id and q_lower in mut_id.lower():
                key = (mut_id, ep_id or "", gen_id or "")
                if key in seen_mutations:
                    continue
                seen_mutations.add(key)
                mutation_hits.append(
                    {
                        "mutation_id": mut_id,
                        "epoch_id": ep_id,
                        "generation_id": gen_id,
                        "patch_id": patch_id,
                    }
                )

        # Patch records are sorted by mutation_id quality (the most
        # operator-meaningful field); rationale-only hits fall to
        # the substring bucket regardless of whether the mutation_id
        # matches verbatim.
        patch_hits = _sort_by_match_quality(patch_hits, "mutation_id", q_lower)
        mutation_hits = _sort_by_match_quality(mutation_hits, "mutation_id", q_lower)
        result["patches"] = patch_hits[:SEARCH_LIMIT_PER_CATEGORY]
        result["mutations"] = mutation_hits[:SEARCH_LIMIT_PER_CATEGORY]

    return result
