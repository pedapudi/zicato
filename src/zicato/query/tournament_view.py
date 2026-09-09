"""tournament_view — extracted from the former dashboard state_reader monolith (pure move)."""

from __future__ import annotations

import sqlite3
from typing import Any

from zicato.epoch._storage import RecordError
from zicato.query._sqlite import (
    INDEX_NOT_BUILT_NOTE,
    _IndexAbsent,
    _opt_json,
    _query,
    open_index_ro,
    with_index_not_built_note,
)
from zicato.query.epoch_view import (
    _normalize_structure,
    _tournament_block_from_scoring,
)
from zicato.query.inputs import EpochInputs
from zicato.query.paths import (
    WorkspacePaths,
    _opt_bool,
    _read_json_value,
    _resolve_epoch_id,
    coerce_float,
    finite_float,
    layout_of,
    read_current_epoch,
)
from zicato.query.promoted_head import champion_history
from zicato.query.ratings import RATING_FIELDS, rating_by_generation
from zicato.query.replicate_scores import replicate_scores, standard_error
from zicato.query.runtime_view import read_active_tournament_dict
from zicato.tournament.scoring import read_gen_score
from zicato.workspace.reads import read_generation_losses


def build_bracket(
    paths: WorkspacePaths, epoch_id: str | None = None, *, inputs: EpochInputs | None = None
) -> dict[str, Any]:
    """``GET /api/tournaments`` — the bracket for an epoch.

    ``epoch_id`` defaults to the current epoch; a validated id scopes to that
    epoch instead.
    """
    epoch_id = _resolve_epoch_id(paths, epoch_id)
    if inputs is not None:
        inputs.check(paths, epoch_id)
    try:
        with open_index_ro(paths.index_db) as conn:
            return _bracket_from_conn(paths, conn, epoch_id, inputs=inputs)
    except _IndexAbsent:
        return with_index_not_built_note(
            {"epoch_id": epoch_id, "champion_lineage": [], "matchups": []}
        )
    except sqlite3.Error:
        return {"epoch_id": epoch_id, "champion_lineage": [], "matchups": []}
    except RecordError as exc:
        return {
            "epoch_id": epoch_id,
            "champion_lineage": [],
            "matchups": [],
            "unreadable": str(exc),
        }


def _bracket_from_conn(
    paths: WorkspacePaths,
    conn: sqlite3.Connection,
    epoch_id: str | None,
    *,
    inputs: EpochInputs | None = None,
) -> dict[str, Any]:
    """The bracket body — reads the open connection, never closes it."""
    if epoch_id is None:
        return {"epoch_id": None, "champion_lineage": [], "matchups": []}

    champion_lineage = champion_history(paths, epoch_id)

    tour_rows = _query(
        conn,
        "SELECT t.tournament_id, t.parent_generation_id, t.child_generation_id, "
        "t.decision, t.delta_scalar, t.rejection_reason, t.ran_at, "
        "e.hypothesis_core_idea "
        "FROM tournaments t "
        "LEFT JOIN experiments e "
        "ON e.epoch_id = t.epoch_id AND e.generation_id = t.child_generation_id "
        "WHERE t.epoch_id = ? "
        "ORDER BY t.ran_at ASC, t.tournament_id ASC",
        (epoch_id,),
    )
    # The per-matchup ladder is the per-challenger crowning rows only.
    # A FIELD-level row (``{epoch}:field:{...}``) is the whole-tournament
    # structure record rather than a champion-vs-challenger duel (it carries no
    # parent/child), so it is excluded here — it surfaces through the
    # structure-aware ``tournaments[]`` envelope below instead.
    matchups = [
        {
            "champion": r["parent_generation_id"],
            "challenger": r["child_generation_id"],
            "decision": r["decision"],
            "delta_scalar": r["delta_scalar"],
            "rejection_reason": r["rejection_reason"],
            "hypothesis_core_idea": r["hypothesis_core_idea"],
            "ran_at": r["ran_at"],
        }
        for r in tour_rows
        if not _is_field_tournament_id(r["tournament_id"])
    ]

    from zicato.epoch.settlement_receipt import iter_settlement_receipts
    from zicato.tournament.records import field_tournament_records

    receipts = {
        receipt.field_record["tournament_id"]: receipt
        for receipt in iter_settlement_receipts(paths.root, epoch_id)
        if receipt.state == "committed" and receipt.field_record is not None
    }
    tournaments: list[dict[str, Any]] = []
    epoch_structure = "gauntlet"
    epoch_structure_params: dict[str, Any] = {}
    for record in field_tournament_records(paths.root, epoch_id):
        body = record.to_dict()
        receipt = receipts.get(record.tournament_id)
        candidate = receipt.candidates[0] if receipt is not None else None
        champion_id = record.champion_generation_id
        body["champion"] = {
            "id": champion_id,
            "scalar": candidate.parent_scalar if candidate is not None else None,
            "eval_mode": candidate.outcome.champion_eval_mode if candidate is not None else None,
            "run_ref": f"epochs/{epoch_id}/generations/{champion_id}" if candidate else None,
        }
        tournaments.append(attach_elim_states(body))
        epoch_structure = body["structure"]
        epoch_structure_params = body["structure_params"]

    # No tournament ROW resolved a non-gauntlet structure — e.g. a run torn
    # down before any bracket completed leaves zero rows, so the scan above
    # never overrides the gauntlet default. Fall back to the epoch's
    # CONTRACT-FROZEN structure (scoring.json, then config.json's scoring)
    # so the API agrees with the configured single_elim/swiss/racing rather
    # than mislabelling the epoch gauntlet.
    if epoch_structure == "gauntlet":
        layout = layout_of(paths)
        scoring = (
            inputs.scoring.copy()
            if inputs is not None
            else _read_json_value(layout.scoring(epoch_id))
        )
        block = _tournament_block_from_scoring(scoring)
        if block is None:
            cfg = (
                inputs.config.copy()
                if inputs is not None
                else _read_json_value(layout.epoch_config(epoch_id))
            )
            block = _tournament_block_from_scoring(
                cfg.get("scoring") if isinstance(cfg, dict) else None
            )
        if isinstance(block, dict) and block.get("structure"):
            epoch_structure = block["structure"]
            if not epoch_structure_params:
                epoch_structure_params = block.get("params") or {}

    return {
        "epoch_id": epoch_id,
        "structure": epoch_structure,
        "structure_params": epoch_structure_params,
        "champion_lineage": champion_lineage,
        "matchups": matchups,
        "tournaments": tournaments,
    }


def _verdict(parent: float | None, child: float | None) -> str:
    if parent is not None and child is not None:
        if child < parent:
            return "improved"
        if child > parent:
            return "regressed"
    return "flat"


def build_matchup_detail(paths: WorkspacePaths, generation_id: str) -> dict[str, Any]:
    """``GET /api/tournaments/:generation_id`` — full matchup detail."""
    epoch_id = read_current_epoch(paths)
    try:
        with open_index_ro(paths.index_db) as conn:
            tour = _query(
                conn,
                "SELECT t.tournament_id, t.parent_generation_id, t.child_generation_id, "
                "t.decision, t.parent_scalar, t.child_scalar, t.delta_scalar, "
                "t.rejection_reason, t.ran_at "
                "FROM tournaments t WHERE t.child_generation_id = ? LIMIT 1",
                (generation_id,),
            )
            tour_row = tour[0] if tour else None

            exp = _query(
                conn,
                "SELECT hypothesis_core_idea, hypothesis_why, hypothesis_json, "
                "tournament_decision, rejection_reason, scalar_score_delta, "
                "drift_loss_delta, pass_rate_delta "
                "FROM experiments WHERE generation_id = ? LIMIT 1",
                (generation_id,),
            )
            exp_row = exp[0] if exp else None

            champion = tour_row["parent_generation_id"] if tour_row else None

            child_losses = _query(
                conn,
                "SELECT entry_id, drift_loss, pass_fail, loss_json FROM loss_profiles "
                "WHERE generation_id = ? ORDER BY entry_id ASC",
                (generation_id,),
            )
            parent_losses = (
                _query(
                    conn,
                    "SELECT entry_id, drift_loss, pass_fail, loss_json FROM loss_profiles "
                    "WHERE generation_id = ? ORDER BY entry_id ASC",
                    (champion,),
                )
                if champion
                else []
            )
            ab: dict[str, dict[str, Any]] = {}
            for r in parent_losses:
                key = r["entry_id"] or ""
                cell = ab.setdefault(key, {"entry_id": r["entry_id"]})
                cell["entry_id"] = r["entry_id"]
                cell["parent_drift_loss"] = r["drift_loss"]
                cell["parent_pass_fail"] = _opt_bool(r["pass_fail"])
                lj = _opt_json(r["loss_json"])
                if isinstance(lj, dict):
                    sid = lj.get("adk_session_id")
                    if isinstance(sid, str) and sid:
                        cell["parent_adk_session_id"] = sid
            for r in child_losses:
                key = r["entry_id"] or ""
                cell = ab.setdefault(key, {"entry_id": r["entry_id"]})
                cell["entry_id"] = r["entry_id"]
                cell["child_drift_loss"] = r["drift_loss"]
                cell["child_pass_fail"] = _opt_bool(r["pass_fail"])
                lj = _opt_json(r["loss_json"])
                if isinstance(lj, dict):
                    sid = lj.get("adk_session_id")
                    if isinstance(sid, str) and sid:
                        cell["child_adk_session_id"] = sid
            ab_grid = []
            for key in sorted(ab):
                cell = ab[key]
                cell.setdefault("parent_drift_loss", None)
                cell.setdefault("child_drift_loss", None)
                cell.setdefault("parent_pass_fail", None)
                cell.setdefault("child_pass_fail", None)
                cell["verdict"] = _verdict(cell["parent_drift_loss"], cell["child_drift_loss"])
                ab_grid.append(cell)

            patch_rows = _query(
                conn,
                "SELECT patch_id, mutation_id, op, rationale FROM patches "
                "WHERE generation_id = ? ORDER BY patch_id ASC",
                (generation_id,),
            )
            patches = [
                {
                    "patch_id": r["patch_id"],
                    "mutation_id": r["mutation_id"],
                    "op": r["op"],
                    "rationale": r["rationale"],
                }
                for r in patch_rows
            ]

            decision = None
            rejection_reason = None
            if tour_row is not None:
                decision = tour_row["decision"]
                rejection_reason = tour_row["rejection_reason"]
            if decision is None and exp_row is not None:
                decision = exp_row["tournament_decision"]
            if rejection_reason is None and exp_row is not None:
                rejection_reason = exp_row["rejection_reason"]

            delta_scalar = tour_row["delta_scalar"] if tour_row else None
            if delta_scalar is None and exp_row is not None:
                delta_scalar = exp_row["scalar_score_delta"]

            detail: dict[str, Any] = {
                "epoch_id": epoch_id,
                "generation_id": generation_id,
                "champion": champion,
                "decision": decision,
                "rejection_reason": rejection_reason,
                "ran_at": tour_row["ran_at"] if tour_row else None,
                "parent_scalar": tour_row["parent_scalar"] if tour_row else None,
                "child_scalar": tour_row["child_scalar"] if tour_row else None,
                "delta_scalar": delta_scalar,
                "patches": patches,
                "ab_grid": ab_grid,
            }
            if exp_row is not None:
                if exp_row["drift_loss_delta"] is not None:
                    detail["drift_loss_delta"] = exp_row["drift_loss_delta"]
                if exp_row["pass_rate_delta"] is not None:
                    detail["pass_rate_delta"] = exp_row["pass_rate_delta"]
                detail["hypothesis"] = {
                    "core_idea": exp_row["hypothesis_core_idea"],
                    "why": exp_row["hypothesis_why"],
                }
                raw = _opt_json(exp_row["hypothesis_json"])
                if raw is not None:
                    detail["hypothesis"]["raw"] = raw
            return detail
    except _IndexAbsent:
        return {
            "epoch_id": epoch_id,
            "generation_id": generation_id,
            "champion": None,
            "decision": None,
            "rejection_reason": None,
            "ran_at": None,
            "parent_scalar": None,
            "child_scalar": None,
            "delta_scalar": None,
            "patches": [],
            "ab_grid": [],
            "note": INDEX_NOT_BUILT_NOTE,
        }
    except sqlite3.Error:
        return {
            "epoch_id": epoch_id,
            "generation_id": generation_id,
            "champion": None,
            "decision": None,
            "rejection_reason": None,
            "ran_at": None,
            "parent_scalar": None,
            "child_scalar": None,
            "delta_scalar": None,
            "patches": [],
            "ab_grid": [],
        }


# ---------------------------------------------------------------------------
# Per-entry A/B grid — read straight off the persisted per-run loss files
# ---------------------------------------------------------------------------
#
# ``build_matchup_detail`` above sources its ``ab_grid`` from the SQLite
# analytical index. That index is a best-effort dual-write: a completed
# tournament whose index was never (re)built — or a workspace inspected
# before ``zicato repair index`` ran — carries no ``loss_profiles`` rows, so
# the matchup-detail panel renders "No per-entry grid recorded" and a
# finished tournament loses its per-board outcomes.
#
# The per-board telemetry is, however, always on disk: every board run
# writes ``generations/{gen}/runs/{entry}/loss.json`` (the reducer's
# :class:`~zicato.core.LossProfile`), and the orchestrator caches a
# ``generations/{gen}/gen_score.json`` aggregate. ``build_matchup_grid``
# reconstructs the champion-vs-challenger comparison directly from those
# files so a completed tournament's outcomes survive without the index.


def _opt_metrics(value: Any) -> dict[str, float] | None:
    """Coerce a raw ``metrics`` field into ``{name: finite float}`` or ``None``.

    The optional precision/recall (etc.) decomposition (#18). Non-finite
    or non-numeric values are dropped; an empty result collapses to
    ``None`` so a missing decomposition reads identically to the
    pre-score path.
    """
    if not isinstance(value, dict):
        return None
    out: dict[str, float] = {}
    for k, v in value.items():
        if isinstance(v, bool) or not isinstance(v, int | float):
            continue
        f = float(v)
        if f != f or f in (float("inf"), float("-inf")):
            continue
        out[str(k)] = f
    return out or None


def _read_run_loss_files(
    paths: WorkspacePaths, epoch_id: str, generation_id: str
) -> dict[str, dict[str, Any]]:
    """Read every ``runs/{entry}/loss.json`` under one generation.

    Returns ``{entry_id: {drift_loss, pass_fail, score, metrics,
    adk_session_id, run_id}}``. The entry id keys on the run directory
    name (the canonical board-run layout) and is overridden by the
    ``entry_id`` field inside the ``loss.json`` payload when present.
    ``score`` (continuous outcome in ``[0, 1]``) and ``metrics`` (e.g.
    precision/recall) are carried through when present and ``None``
    otherwise: a loss.json written before the ``score`` field existed carries
    both as ``None``. Missing /
    malformed files are skipped silently — a generation with no telemetry
    yet yields ``{}``.
    """
    out: dict[str, dict[str, Any]] = {}
    for run_entry_id, loss in read_generation_losses(
        layout_of(paths), epoch_id, generation_id
    ).items():
        entry_id = loss["entry_id"]
        drift = loss.get("drift_loss")
        prov = loss.get("scoring_provenance")
        cell: dict[str, Any] = {
            "entry_id": entry_id,
            "drift_loss": coerce_float(drift),
            "pass_fail": _opt_bool(loss.get("pass_fail")),
            # Continuous per-entry outcome + its optional precision/recall
            # decomposition (#18). ``None`` for a loss.json written before
            # the ``score`` field existed.
            "score": finite_float(loss.get("score")),
            "metrics": _opt_metrics(loss.get("metrics")),
            "run_id": (loss.get("run_id") if isinstance(loss.get("run_id"), str) else run_entry_id),
            # Seam-1 drift-reduction provenance (#19). ``None`` on a
            # loss.json that recorded none — surfaced so the gate
            # breakdown can show which transform / plugin shaped drift_loss.
            "scoring_provenance": str(prov) if isinstance(prov, str) and prov else None,
            # Did this run OBSERVE drift at all? An adapter that emits no drift
            # stream still writes a structural ``drift_loss`` of 0.0 with an
            # empty measured drift metrics, which is indistinguishable on the wire
            # from a run that watched for drift and saw none. Either a recorded
            # drift event or a non-zero loss proves the channel carries signal;
            # nothing else does. Internal to this module — the endpoint serves
            # the matchup-wide ``drift_present`` derived from it.
            "drift_observed": any(m["name"].startswith("drift:") for m in loss["metric_counts"])
            or bool(coerce_float(drift) not in (None, 0.0)),
        }
        sid = loss.get("adk_session_id")
        if isinstance(sid, str) and sid:
            cell["adk_session_id"] = sid
        out[entry_id] = cell
    return out


def _gen_score_view(paths: WorkspacePaths, epoch_id: str, generation_id: str) -> dict[str, Any]:
    """Project the accepted aggregate for query responses."""
    score = read_gen_score(layout_of(paths), epoch_id, generation_id)
    return score.to_dict() if score else {}


def _entry_outcome(
    row: dict[str, Any], champion: str, challenger: str
) -> tuple[str, str | None, str | None]:
    """Resolve ONE board entry's outcome against the signal the contract carries.

    Returns ``(verdict, won_by, decided_by)``. The channels are tried in the
    order the evaluation contract defines an entry's outcome, and the first one
    that SEPARATES the two sides decides:

    1. ``"score"`` — the continuous per-entry outcome, HIGHER is better.
    2. ``"pass"`` — the entry's pass predicate, passing beats failing.
    3. ``"drift"`` — the drift loss, LOWER is better.

    A channel populated on both sides but equal on them has not separated
    anything, so resolution falls through to the next one: two entries that both
    fail their predicate are still told apart by their drift losses. When no
    channel separates them the entry is
    ``"flat"`` and ``decided_by`` names the first channel it was READ on, so the
    client knows which quantity the tie is a tie in.

    Resolution is per row: a board where only some entries carry a
    continuous score, or a champion generation scored before the ``score`` field
    existed, degrades entry by entry rather than dropping the entry or falling
    back to one channel for the whole grid. ``decided_by`` is ``None`` only when
    no channel is populated on both sides.
    """
    ordered: tuple[tuple[str, float | None, float | None], ...] = (
        ("score", _rank_score(row.get("parent_score")), _rank_score(row.get("child_score"))),
        ("pass", _rank_pass(row.get("parent_pass")), _rank_pass(row.get("child_pass"))),
        (
            "drift",
            _rank_drift(row.get("parent_drift_loss")),
            _rank_drift(row.get("child_drift_loss")),
        ),
    )
    read_on: str | None = None
    for channel, parent_rank, child_rank in ordered:
        if parent_rank is None or child_rank is None:
            continue
        if read_on is None:
            read_on = channel
        if child_rank > parent_rank:
            return "improved", challenger, channel
        if child_rank < parent_rank:
            return "regressed", champion, channel
    return "flat", None, read_on


# The three channel readers below all return a HIGHER-IS-BETTER rank, so
# :func:`_entry_outcome` compares them with one rule and the sign conventions
# (score up = better, drift down = better) are stated exactly once.
def _rank_score(value: Any) -> float | None:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def _rank_pass(value: Any) -> float | None:
    return float(value) if isinstance(value, bool) else None


def _rank_drift(value: Any) -> float | None:
    return -float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def build_matchup_grid(
    paths: WorkspacePaths,
    epoch_id: str,
    champion_id: str,
    challenger_id: str,
) -> dict[str, Any]:
    """Per-entry A/B grid for a matchup, read from the persisted loss files.

    ``GET /api/matchup-grid/{epoch_id}/{champion}/{challenger}``. Unlike
    :func:`build_matchup_detail` this never touches the SQLite index — it
    reads ``generations/{gen}/runs/{entry}/loss.json`` for both the
    champion and the challenger generation and the two
    ``gen_score.json`` aggregates, so a *completed* tournament's
    per-board outcomes are recoverable even when the index was never
    built.

    Returns::

        {
          "epoch_id", "champion", "challenger", "drift_present",
          "entry_grid": [ { entry_id, parent_drift_loss, child_drift_loss,
                            parent_pass, child_pass,
                            parent_score, child_score, delta_score,
                            score_replicates, score_se,
                            delta, verdict, won_by, decided_by,
                            parent_session_id?, child_session_id? } ],
          "scalar": { parent, child, delta, components } | null,
          "source": "loss_files"
        }

    ``entry_grid`` rows are sorted by entry id; an entry that only ran on
    one side still appears (the missing side is ``null``). The ``scalar``
    block is composed from the ``gen_score.json`` aggregates — its
    ``components`` is the challenger-minus-champion delta of each
    ``scalar_components`` term so the breakdown shows what moved.

    Each row's ``verdict`` / ``won_by`` resolve against the signal the
    contract actually carries rather than against drift alone, and
    ``decided_by`` names the channel that resolved them (see
    :func:`_entry_outcome`). ``delta_score`` is the per-entry movement on
    the continuous channel, challenger − champion with HIGHER BETTER — the
    entry-level counterpart of the generation-level
    ``scalar.mean_score.delta`` below, and the quantity the promote gate
    aggregates. Rows carrying a ``delta_score`` are exactly the entries both
    sides ran, so summing that column reproduces the gate's comparison on
    the shared board slice; an entry only one side ran contributes ``None``
    and therefore nothing, which is the same restriction the gate applies.
    ``drift_present`` says whether the drift channel carries information at
    all in this workspace, so a client hides it instead of guessing.
    """
    base: dict[str, Any] = {
        "epoch_id": epoch_id,
        "champion": champion_id,
        "challenger": challenger_id,
        "entry_grid": [],
        "scalar": None,
        "drift_present": False,
        "source": "loss_files",
    }
    if not epoch_id or not challenger_id:
        return base

    parent_losses = _read_run_loss_files(paths, epoch_id, champion_id) if champion_id else {}
    child_losses = _read_run_loss_files(paths, epoch_id, challenger_id)

    entry_grid: list[dict[str, Any]] = []
    for entry_id in sorted(set(parent_losses) | set(child_losses)):
        p = parent_losses.get(entry_id)
        c = child_losses.get(entry_id)
        parent_drift = p.get("drift_loss") if p else None
        child_drift = c.get("drift_loss") if c else None
        delta = (
            child_drift - parent_drift
            if isinstance(parent_drift, int | float) and isinstance(child_drift, int | float)
            else None
        )
        parent_score = p.get("score") if p else None
        child_score = c.get("score") if c else None
        child_replicates = replicate_scores(paths, epoch_id, challenger_id, entry_id) if c else []
        if not child_replicates and isinstance(child_score, int | float):
            # The replicate enumeration is best-effort — a pruned run dir or an
            # unreadable sibling file yields nothing. The canonical loss.json
            # this row's score came from is still one draw, so never report zero
            # draws beside a served score.
            child_replicates = [float(child_score)]
        row: dict[str, Any] = {
            "entry_id": entry_id,
            "parent_drift_loss": parent_drift,
            "child_drift_loss": child_drift,
            "parent_pass": p.get("pass_fail") if p else None,
            "child_pass": c.get("pass_fail") if c else None,
            # Continuous per-entry outcome (#18) + its optional
            # precision/recall decomposition. ``None`` for a loss.json written
            # before the ``score`` field existed, so a bool-only entry carries
            # score and metrics as None and renders by its pass bit alone.
            "parent_score": parent_score,
            "child_score": child_score,
            "parent_metrics": p.get("metrics") if p else None,
            "child_metrics": c.get("metrics") if c else None,
            "delta": delta,
            # The per-entry movement on the CONTINUOUS channel, challenger −
            # champion, HIGHER IS BETTER (the opposite sign convention to
            # ``delta``, which is a loss). ``None`` unless both sides carry a
            # score, so an entry the champion never ran, or a generation scored
            # before the field existed, degrades to null on this row alone.
            "delta_score": (
                child_score - parent_score
                if isinstance(parent_score, int | float) and isinstance(child_score, int | float)
                else None
            ),
            # How many qualifying replicate draws the CHALLENGER has on this
            # entry, and the standard error of their mean score. This is the
            # candidate's own measurement precision on the entry — it does NOT
            # fold in the champion side, which is often a single cached draw —
            # so it bounds how much of ``delta_score`` is readable rather than the
            # delta's full variance. ``score_se`` is null below two draws:
            # one draw measures no spread and must never render as ±0.000.
            "score_replicates": len(child_replicates),
            "score_se": standard_error(child_replicates),
        }
        verdict, won_by, decided_by = _entry_outcome(row, champion_id, challenger_id)
        row["verdict"] = verdict
        row["won_by"] = won_by
        row["decided_by"] = decided_by
        if p and p.get("adk_session_id"):
            row["parent_session_id"] = p["adk_session_id"]
        if c and c.get("adk_session_id"):
            row["child_session_id"] = c["adk_session_id"]
        entry_grid.append(row)
    base["entry_grid"] = entry_grid
    # Does the drift channel carry information for THIS matchup? True when any
    # run on either side recorded a drift event or a non-zero drift loss. An
    # adapter that emits no drift stream produces a structural 0.0 on every
    # entry, which reads on the wire exactly like a clean run — so the honest
    # answer for that workspace is "absent", and a client hides the drift
    # columns rather than painting a column of zeroes that mean nothing.
    base["drift_present"] = any(
        cell.get("drift_observed")
        for side in (parent_losses, child_losses)
        for cell in side.values()
    )

    try:
        parent_score = _gen_score_view(paths, epoch_id, champion_id) if champion_id else {}
        child_score = _gen_score_view(paths, epoch_id, challenger_id)
    except RecordError as exc:
        base["unreadable"] = str(exc)
        return base
    p_scalar, c_scalar, pair_delta = _scalar_pair(
        parent_score.get("scalar"), child_score.get("scalar")
    )
    if p_scalar is not None or c_scalar is not None:
        scalar: dict[str, Any] = {
            "parent": p_scalar,
            "child": c_scalar,
            "delta": pair_delta,
        }
        # Per-generation mean continuous outcome (#18), read straight from
        # the cached gen_score.json — never recomputed. ``None`` when the
        # aggregate predates the field (back-compat); the higher mean is
        # the better side. Folded under the scalar block so the candidate /
        # board views can show a board-level score summary alongside the
        # per-entry scores.
        p_mean = finite_float(parent_score.get("mean_score"))
        c_mean = finite_float(child_score.get("mean_score"))
        if p_mean is not None or c_mean is not None:
            scalar["mean_score"] = {
                "parent": p_mean,
                "child": c_mean,
                "delta": (c_mean - p_mean if p_mean is not None and c_mean is not None else None),
            }
        parent_components = parent_score.get("scalar_components")
        child_components = child_score.get("scalar_components")
        # The breakdown bars are the per-component CHANGE champion ->
        # challenger: a negative bar is a component that improved.
        components: dict[str, float] = {}
        names: set[str] = set()
        if isinstance(parent_components, dict):
            names |= set(parent_components)
        if isinstance(child_components, dict):
            names |= set(child_components)
        for name in sorted(names):  # one order on every read; a set's order varies
            pv = parent_components.get(name) if isinstance(parent_components, dict) else None
            cv = child_components.get(name) if isinstance(child_components, dict) else None
            pv = pv if isinstance(pv, int | float) else 0.0
            cv = cv if isinstance(cv, int | float) else 0.0
            components[name] = cv - pv
        if components:
            scalar["components"] = components
        base["scalar"] = scalar

    return base


def _is_field_tournament_id(tournament_id: str | None) -> bool:
    """True for a FIELD-level tournament id (``"{epoch}:field:{...}"``).

    The orchestrator settles one field record per non-gauntlet round under
    this id form; the per-challenger crowning rows use the
    ``"{epoch}:{parent}->{child}"`` form instead. The marker is the
    ``":field:"`` segment, which the per-challenger ``->`` form never
    carries.
    """
    return ":field:" in str(tournament_id or "")


def _structure_envelope(
    epoch_id: str,
    tournament_id: str,
    source: str,
    *,
    structure: Any = "gauntlet",
    structure_params: Any = None,
    competitors: Any = None,
    rounds: Any = None,
    standings: Any = None,
    field_status: Any = None,
) -> dict[str, Any]:
    """THE one tournament-structure envelope builder.

    Every resolver (index / active / loss-files) projects its raw fields
    through here so the payload shape — and the type-guarded degrades —
    live in exactly one place. An elim envelope is enriched with the
    served elim model (:func:`attach_elim_states` — sorted rounds +
    ``bracket_side``/``loser`` + top-level ``gen_states``).
    """
    return attach_elim_states(
        {
            "epoch_id": epoch_id,
            "tournament_id": tournament_id,
            "structure": _normalize_structure(structure),
            "structure_params": structure_params if isinstance(structure_params, dict) else {},
            "competitors": competitors if isinstance(competitors, list) else [],
            "rounds": rounds if isinstance(rounds, list) else [],
            "standings": standings if isinstance(standings, list) else [],
            "field_status": field_status if isinstance(field_status, list) else [],
            "source": source,
        }
    )


def _empty_tournament_structure(epoch_id: str, tournament_id: str, source: str) -> dict[str, Any]:
    return _structure_envelope(epoch_id, tournament_id, source)


# ---------------------------------------------------------------------------
# The served ELIM MODEL — rounds canonicalized + per-generation states
# ---------------------------------------------------------------------------

_ELIM_STRUCTURES = frozenset({"single_elim", "double_elim"})


def _round_sort_key(r: dict[str, Any], position: int) -> tuple[Any, int]:
    """The temporal sort key: ``stage_index``, or ``round_index``.

    The persisted within-tournament stage key is ``stage_index``
    (selection/strategy.py); ``round_index`` is accepted for records
    written before the rename. A round with neither sorts stably by its
    original position.
    """
    for key in ("round_index", "stage_index"):
        v = r.get(key)
        if isinstance(v, bool):  # bool is an int subclass — never a round index
            continue
        if isinstance(v, int | float):
            return (v, position)
    return (position, position)


def _scalar_id(v: Any) -> str | None:
    """A competitor/winner id as the scalar the client renders, or ``None``.

    Only a string or a real number is an id: a ``bool`` (an ``int``
    subclass — dropped explicitly), ``dict``/``list``/``None``/other type
    is NOT a scalar and reads as absent. Twinned line-for-line by the Rust
    (``str|number``-only) and node folds so all three drop the same values.
    """
    if isinstance(v, str):
        return v
    if isinstance(v, int | float) and not isinstance(v, bool):
        return str(v)
    return None


def _match_competitors(m: dict[str, Any]) -> list[str]:
    comps = m.get("competitors")
    if not isinstance(comps, list):
        return []
    out: list[str] = []
    for c in comps:
        s = _scalar_id(c)
        if s and s != "tbd":
            out.append(s)
    return out


def _match_winner(m: dict[str, Any]) -> str | None:
    """The decided winner id, or ``None`` (undecided / non-scalar).

    A falsy id (``""``, ``0``) reads as undecided, matching the Rust
    ``truthy`` gate and the node ``m.winner ? …`` guard.
    """
    s = _scalar_id(m.get("winner"))
    return s or None


def _match_pending(m: dict[str, Any], winner: str | None) -> bool:
    if m.get("pending"):
        return True
    return not winner and not m.get("bye") and not m.get("decision")


def derive_elim_states(rounds: Any) -> dict[str, Any]:
    """The SERVER-SIDE elim fold — the model the bracket figures render.

    This fold owns the whole derivation: it re-sorts mis-ordered caller
    columns, de-duplicates backend-duplicated matches, classifies each loss as
    an elimination or a winners→losers drop, and guards against phantom
    eliminations. Doing it server-side is what lets every consumer (Python
    service, Rust supervisor, the node mock) serve ONE identical model; a
    client (``svg.js`` elimRadial) that derived it per render
    would be re-deriving what the server already owns. Ported line-for-line
    into ``crates/supervisor/src/elim_states.rs`` — the shared fixture
    ``tests/data/elim_states_fixture.json`` pins the two folds together.

    Input: the raw ``rounds[]`` blob (each round ``{round_index? /
    stage_index?, label?, matches: [{competitors, winner?, bye?,
    decision?, pending?, bracket_slot?, projected?, ...}]}``).

    Output ``{"rounds": [...], "gen_states": [...]}``:

    * ``rounds`` — PRE-SORTED by round index (temporal WB → LB → GF; a
      round without an index keeps its position). Every round gains
      ``bracket_side`` (``"WB"``/``"LB"`` — LB when any match's
      ``bracket_slot`` starts with ``LB``); its matches are DEDUPED (key =
      ``bracket_slot`` + sorted competitors, keeping the MOST-DECIDED
      duplicate) and each match gains ``loser`` (the non-winner of a
      decided two-sided match; ``null`` = undecided / bye). Round
      references below are COLUMN indices into this sorted array.
    * ``gen_states`` — one record per competitor, first-seen order:
      ``{generation_id, played_rounds, advanced_rounds, lost_rounds,
      eliminated_at_round, side_by_round, lb_entry_round, projected}``. The
      elimination-vs-drop rule is the client's, verbatim: a loss with NO later
      appearance is an elimination there; a loss followed by a later appearance
      is a winners→losers drop (the second life). ``null`` = undecided;
      ``side_by_round`` keys are stringified column indices (JSON object keys).

    Pure + best-effort: a malformed blob degrades to empty lists and never
    raises.
    """
    raw = [r for r in (rounds if isinstance(rounds, list) else []) if isinstance(r, dict)]
    ordered = sorted(range(len(raw)), key=lambda i: _round_sort_key(raw[i], i))

    played: dict[str, set[int]] = {}
    advanced: dict[str, set[int]] = {}
    lost_at: dict[str, set[int]] = {}
    side_of: dict[str, dict[int, str]] = {}
    lb_entry: dict[str, int | None] = {}
    projected: dict[str, Any] = {}
    order: list[str] = []

    def _ensure(gid: str) -> None:
        if gid not in played:
            played[gid] = set()
            advanced[gid] = set()
            lost_at[gid] = set()
            side_of[gid] = {}
            lb_entry[gid] = None
            order.append(gid)

    out_rounds: list[dict[str, Any]] = []
    for ci, ri in enumerate(ordered):
        r = raw[ri]
        matches_in = [m for m in r.get("matches") or [] if isinstance(m, dict)]

        # ── DEDUPE (ex-client): a published round can carry the SAME match
        # twice (identical bracket_slot + competitor pair). Key on the slot +
        # the sorted competitor set; keep the MOST-DECIDED instance (a settled
        # winner beats a still-pending duplicate). Distinct matches sharing a
        # column keep distinct keys, so normal data passes through untouched.
        by_key: dict[str, dict[str, Any]] = {}
        key_order: list[str] = []
        for m in matches_in:
            comps = _match_competitors(m)
            winner = _match_winner(m)
            key = str(m.get("bracket_slot") or "") + "|" + "/".join(sorted(comps))
            prev = by_key.get(key)
            if prev is None:
                by_key[key] = m
                key_order.append(key)
            else:
                # Only a still-pending first-seen yields to a decided
                # duplicate. Two DIFFERENT decided winners for the same slot
                # is corrupt data — the first-seen (most-decided) one wins
                # deterministically rather than flapping by iteration order.
                prev_winner = _match_winner(prev)
                if _match_pending(prev, prev_winner) and not _match_pending(m, winner):
                    by_key[key] = m
        deduped = [by_key[k] for k in key_order]

        any_lb = False
        out_matches: list[dict[str, Any]] = []
        for m in deduped:
            comps = _match_competitors(m)
            winner = _match_winner(m)
            pending = _match_pending(m, winner)
            is_lb = str(m.get("bracket_slot") or "").startswith("LB")
            if is_lb:
                any_lb = True
            bye = bool(m.get("bye"))
            loser: str | None = None
            if winner and not bye and len(comps) >= 2:
                loser = next((c for c in comps if c != winner), None)

            proj_map = m.get("projected") if isinstance(m.get("projected"), dict) else None
            for c in comps:
                _ensure(c)
                played[c].add(ci)
                side_of[c][ci] = "LB" if is_lb else "WB"
                if is_lb and lb_entry[c] is None:
                    lb_entry[c] = ci
                if proj_map and pending:
                    p = proj_map.get(c)
                    if (
                        isinstance(p, dict)
                        and isinstance(p.get("scalar"), int | float)
                        and not isinstance(p.get("scalar"), bool)
                    ):
                        projected[c] = p
                if pending:
                    continue
                if bye or (winner and c == winner):
                    advanced[c].add(ci)
                elif winner:
                    lost_at[c].add(ci)

            out_m = dict(m)
            out_m["loser"] = loser
            out_matches.append(out_m)

        out_r = dict(r)
        out_r["matches"] = out_matches
        out_r["bracket_side"] = "LB" if any_lb else "WB"
        out_rounds.append(out_r)

    # ── ELIMINATION vs DROP (ex-client): eliminated at the first loss with
    # no LATER appearance; an earlier loss followed by a later column is a
    # winners→losers drop, never a termination (no phantom ✕ in the WB).
    gen_states: list[dict[str, Any]] = []
    for gid in order:
        lost_sorted = sorted(lost_at[gid])
        last_played = max(played[gid]) if played[gid] else -1
        eliminated_at: int | None = None
        for ci in lost_sorted:
            if ci >= last_played:
                eliminated_at = ci
                break
        gen_states.append(
            {
                "generation_id": gid,
                "played_rounds": sorted(played[gid]),
                "advanced_rounds": sorted(advanced[gid]),
                "lost_rounds": lost_sorted,
                "eliminated_at_round": eliminated_at,
                "side_by_round": {str(ci): side for ci, side in sorted(side_of[gid].items())},
                "lb_entry_round": lb_entry[gid],
                "projected": projected.get(gid),
            }
        )

    return {"rounds": out_rounds, "gen_states": gen_states}


def attach_elim_states(payload: dict[str, Any]) -> dict[str, Any]:
    """Enrich an elim payload with the served elim model, in place.

    For a ``single_elim`` / ``double_elim`` payload carrying a ``rounds``
    list: replaces ``rounds`` with the canonicalized (sorted / deduped /
    ``loser``+``bracket_side``-stamped) copy and attaches the top-level
    ``gen_states`` fold. Any other payload passes through untouched —
    the enrichment is KEY-ABSENT for non-elim structures (additive).
    """
    structure = _normalize_structure(payload.get("structure"))
    if structure in _ELIM_STRUCTURES and isinstance(payload.get("rounds"), list):
        derived = derive_elim_states(payload["rounds"])
        payload["rounds"] = derived["rounds"]
        payload["gen_states"] = derived["gen_states"]
    return payload


def _scalar_pair(
    parent_raw: Any, child_raw: Any
) -> tuple[float | None, float | None, float | None]:
    """Normalize a champion/challenger scalar pair -> ``(parent, child, delta)``.

    ``delta`` (child - parent) only when both sides carry a real number.
    """
    parent = coerce_float(parent_raw)
    child = coerce_float(child_raw)
    delta = child - parent if parent is not None and child is not None else None
    return parent, child, delta


def _structure_from_active(
    paths: WorkspacePaths, epoch_id: str, tournament_id: str
) -> dict[str, Any] | None:
    """The structure state from the live ``active_tournament.events.jsonl``.

    Returns ``None`` unless the live record matches the requested
    ``(epoch_id, tournament_id)`` coordinate.
    """
    active = read_active_tournament_dict(paths)
    if not isinstance(active, dict):
        return None
    if active.get("tournament_id") != tournament_id:
        return None
    if epoch_id and active.get("epoch_id") not in (None, epoch_id):
        return None
    return _structure_envelope(
        active.get("epoch_id") or epoch_id,
        tournament_id,
        "active",
        structure=active.get("structure"),
        structure_params=active.get("structure_params"),
        competitors=active.get("competitors"),
        rounds=active.get("rounds"),
        standings=active.get("standings"),
        field_status=active.get("field_status"),
    )


def _structure_from_records(
    paths: WorkspacePaths, epoch_id: str, tournament_id: str
) -> dict[str, Any] | None:
    """Read a recorded tournament or select its recorded matches for one pair."""
    from zicato.tournament.records import field_tournament_records

    champion, challenger = _decode_crowning_pair(tournament_id)
    pair = {champion, challenger} if champion and challenger else set()
    try:
        records = field_tournament_records(paths.root, epoch_id)
    except RecordError as exc:
        return {"epoch_id": epoch_id, "tournament_id": tournament_id, "unreadable": str(exc)}
    for record in records:
        if record.state != "settled":
            continue
        body = record.to_dict()
        if record.tournament_id != tournament_id:
            if not pair:
                continue
            rounds = [
                {**stage, "matches": matches}
                for stage in body["rounds"]
                if (
                    matches := [
                        match for match in stage["matches"] if set(match["competitors"]) == pair
                    ]
                )
            ]
            if not rounds:
                continue
            body["rounds"] = rounds
            for key in ("competitors", "standings", "field_status"):
                body[key] = [row for row in body[key] if row["generation_id"] in pair]
        return attach_elim_states({**body, "tournament_id": tournament_id, "source": "record"})
    return None


def _decode_crowning_pair(tournament_id: str) -> tuple[str, str]:
    """Best-effort decode of ``{epoch}:{champion}->{challenger}``.

    Returns ``(champion, challenger)``; either may be ``""`` when the id
    does not follow the convention.
    """
    if not isinstance(tournament_id, str) or "->" not in tournament_id:
        return ("", "")
    left, _, challenger = tournament_id.partition("->")
    champion = left.rsplit(":", 1)[-1] if ":" in left else left
    return (champion.strip(), challenger.strip())


def build_tournament_structure(
    paths: WorkspacePaths, epoch_id: str, tournament_id: str
) -> dict[str, Any]:
    """Serve recorded bracket, standings, and progress for the visualizations.

    Completed round records contain the actual matches and results. The active
    record supplies running progress. Missing structure stays empty.
    """
    if not epoch_id or not tournament_id:
        return _empty_tournament_structure(epoch_id, tournament_id, "unavailable")
    for resolver in (_structure_from_records, _structure_from_active):
        result = resolver(paths, epoch_id, tournament_id)
        if result is not None:
            enriched = _enrich_diversity(paths, epoch_id, result)
            return _enrich_standings_ratings(paths, epoch_id, enriched)
    return _empty_tournament_structure(epoch_id, tournament_id, "unavailable")


def _challenger_generation_ids(result: dict[str, Any]) -> list[str]:
    """Ordered challenger generation ids for a resolved structure dict.

    Prefers the ``competitors`` roles (champion vs challenger), falling back
    to the ``field_status`` records when competitor roles are absent. The
    order is the field's mint order (seed order) so the returned list is a
    stable, deduplicated slate of the challengers that formed the field.
    """
    seen: set[str] = set()
    ordered: list[str] = []

    def _seed_key(c: dict[str, Any]) -> int:
        seed = c.get("seed")
        return seed if isinstance(seed, int) else 1 << 30

    competitors = result.get("competitors")
    if isinstance(competitors, list) and competitors:
        ranked = sorted(
            (c for c in competitors if isinstance(c, dict)),
            key=_seed_key,
        )
        for c in ranked:
            if str(c.get("role", "")) == "champion":
                continue
            gid = str(c.get("generation_id", ""))
            if gid and gid not in seen:
                seen.add(gid)
                ordered.append(gid)
        if ordered:
            return ordered
    field_status = result.get("field_status")
    if isinstance(field_status, list):
        for f in field_status:
            if not isinstance(f, dict):
                continue
            gid = str(f.get("generation_id", ""))
            if gid and gid not in seen:
                seen.add(gid)
                ordered.append(gid)
    return ordered


def _mutation_ids_for(conn: sqlite3.Connection, generation_id: str) -> frozenset[str]:
    """The targeted-mutation-id SET a challenger declared, from the index.

    Transposes the already-persisted ``patches`` rows (the same table
    :func:`build_matchup_detail` reads) into the order-insensitive set of
    ``mutation_id`` values a generation's patch set touched — the field-
    diversity signature the orchestrator soft-rejects on. An unindexed /
    patchless generation yields the empty set (it contributes no idea).
    """
    try:
        rows = _query(
            conn,
            "SELECT mutation_id FROM patches WHERE generation_id = ?",
            (generation_id,),
        )
    except sqlite3.Error:
        return frozenset()
    return frozenset(str(r["mutation_id"]) for r in rows if r["mutation_id"])


def _enrich_diversity(
    paths: WorkspacePaths, epoch_id: str, result: dict[str, Any]
) -> dict[str, Any]:
    """Attach a ``diversity`` block + per-slot ``diversity_status`` (additive).

    A multi-challenger field of N collapses when two challengers propose the
    same mutation-id set (FUNCTIONALITY-RECOMMENDATIONS.md §4.3), so this
    surfaces the field's pairwise-overlap structure for the dashboard:
    ``{field_size, distinct_ideas, mean_overlap, max_overlap,
    max_overlap_pair, tolerance, soft_rejected_count}`` plus a
    ``diversity_status`` (``applied`` | ``penalized`` | ``soft_rejected``) on
    each ``field_status`` record.

    KEY-ABSENT for single-challenger / pre-feature runs: the block is only
    attached for a real field (two or more challengers whose mutation-id sets
    resolve from the index), so a gauntlet structure, a pre-feature epoch, or
    an index without a ``patches`` table is byte-compatible with today. The
    ``tolerance`` is read back from any soft-rejected slot's record (the
    orchestrator stamps it on enforcement); ``None`` when enforcement was off,
    and ``soft_rejected_count`` is then ``0``.
    """
    challengers = _challenger_generation_ids(result)
    if len(challengers) < 2:
        return result
    try:
        with open_index_ro(paths.index_db) as conn:
            mutation_sets = [(gid, _mutation_ids_for(conn, gid)) for gid in challengers]
    except (_IndexAbsent, sqlite3.Error):
        return result
    # No challenger resolved any mutation ids (patchless / unindexed field):
    # there is no idea structure to summarise, so stay key-absent.
    if not any(ids for _gid, ids in mutation_sets):
        return result

    field_status = result.get("field_status")
    status_by_gen: dict[str, dict[str, Any]] = {}
    if isinstance(field_status, list):
        for f in field_status:
            if isinstance(f, dict):
                status_by_gen[str(f.get("generation_id", ""))] = f

    # Per-slot diversity status: prefer the orchestrator-stamped value on the
    # field-status record (enforcement on); otherwise default ``applied`` so
    # the dashboard always has a status to render. ``soft_rejected_count`` and
    # ``tolerance`` are read back from the stamped records.
    soft_rejected = 0
    tolerance: float | None = None
    for f in field_status if isinstance(field_status, list) else []:
        if not isinstance(f, dict):
            continue
        stamped = f.get("diversity_status")
        if stamped == "soft_rejected":
            soft_rejected += 1
        if "diversity_status" not in f:
            f["diversity_status"] = "applied"
        tol = f.get("diversity_tolerance")
        if tolerance is None and isinstance(tol, int | float):
            tolerance = float(tol)

    from zicato.selection.diversity import compute_field_diversity  # noqa: PLC0415

    block = compute_field_diversity(
        mutation_sets, tolerance=tolerance, soft_rejected_count=soft_rejected
    )
    result["diversity"] = block
    return result


def _enrich_standings_ratings(
    paths: WorkspacePaths, epoch_id: str, result: dict[str, Any]
) -> dict[str, Any]:
    """Attach the visibility rating triple to every standings entry (additive).

    Each ``standings`` record gains ``elo`` / ``elo_se`` / ``elo_games``, in
    one snake_case spelling, joined server-side from the analytical index so
    the client renders the rating column without re-deriving anything.
    Best-effort by contract: an absent or cold index — or a generation the
    fold has not rated (zero settled duels, or a file written before the last
    reindex) — attaches the null triple, never an error.

    Settled-vs-live: the structure payload is request-scoped (the GET handler
    calls this reader once per fetch — there is no SSE/heartbeat recompute),
    and the rating join reads only the SETTLED index. A LIVE field's standings
    (resolved off ``active_tournament.events.jsonl``) simply carry whatever the index
    derived at the last ingest — typically the null triple for brand-new
    challengers — and the live overlay (projected scalars, in-flight bars)
    keeps riding the active envelope untouched. The rating is visibility-only;
    it never gates promotion.
    """
    standings = result.get("standings")
    if not isinstance(standings, list) or not standings:
        return result
    ratings = rating_by_generation(paths, epoch_id)
    for s in standings:
        if not isinstance(s, dict):
            continue
        gid = str(s.get("generation_id") or "")
        triple = ratings.get((epoch_id, gid)) if gid else None
        for field in RATING_FIELDS:
            s[field] = triple.get(field) if triple else None
    return result


# ---------------------------------------------------------------------------
# Consolidated environment view — the single coalesced dashboard read
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Phase-1 light-up: per-judge / per-entry / per-tournament helpers
# ---------------------------------------------------------------------------


def _tournament_id_for(epoch_id: str, parent_gen_id: str, child_gen_id: str) -> str:
    """Compose the tournament id keying convention used by the ingester.

    Mirrors :func:`zicato.index.ingest._tournament_id_for_run` exactly:
    a tournament round is ``{epoch_id}:{parent_gen}->{child_gen}``. Kept
    co-located with the dashboard reader so a downstream rename of the
    ingester's helper does not silently desync the FK-based endpoints.
    """
    return f"{epoch_id}:{parent_gen_id}->{child_gen_id}"
