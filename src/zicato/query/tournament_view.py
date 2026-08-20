"""tournament_view — extracted from the former dashboard state_reader monolith (pure move)."""

from __future__ import annotations

import sqlite3
from typing import Any

from zicato.query._sqlite import (
    _IndexAbsent,
    _opt_json,
    _query,
    _rget,
    open_index_ro,
)
from zicato.query.epoch_view import (
    _normalize_structure,
    _tournament_block_from_scoring,
)
from zicato.query.paths import (
    WorkspacePaths,
    _opt_bool,
    _read_json_value,
    _resolve_epoch_id,
    coerce_float,
    layout_of,
    read_current_epoch,
)
from zicato.query.ratings import RATING_FIELDS, rating_by_generation
from zicato.query.replicate_scores import replicate_scores, standard_error
from zicato.query.runtime_view import read_active_tournament_dict
from zicato.workspace import read_gen_score


def _champion_lineage(generations: list[dict[str, Any]]) -> list[str]:
    promoted = {
        g["generation_id"] for g in generations if g.get("promoted") and g.get("generation_id")
    }
    if not promoted:
        return []
    parent = {
        g["generation_id"]: g.get("parent_generation_id")
        for g in generations
        if g.get("promoted") and g.get("generation_id")
    }
    roots = sorted(
        gid for gid in promoted if parent.get(gid) is None or parent.get(gid) not in promoted
    )
    if not roots:
        return []
    root = roots[0]
    child_of: dict[str, str] = {}
    for g in generations:
        if not g.get("promoted"):
            continue
        p = g.get("parent_generation_id")
        c = g.get("generation_id")
        if isinstance(p, str) and isinstance(c, str) and p in promoted:
            child_of[p] = c
    chain = [root]
    seen = {root}
    cur = root
    while cur in child_of:
        nxt = child_of[cur]
        if nxt in seen:
            break
        chain.append(nxt)
        seen.add(nxt)
        cur = nxt
    return chain


def build_bracket(paths: WorkspacePaths, epoch_id: str | None = None) -> dict[str, Any]:
    """``GET /api/tournaments`` — the bracket for an epoch.

    ``epoch_id`` defaults to the current epoch; a validated id scopes to that
    epoch instead.
    """
    epoch_id = _resolve_epoch_id(paths, epoch_id)
    try:
        with open_index_ro(paths.index_db) as conn:
            return _bracket_from_conn(paths, conn, epoch_id)
    except _IndexAbsent:
        return {
            "epoch_id": epoch_id,
            "champion_lineage": [],
            "matchups": [],
            "note": "index not built; run zicato repair index",
        }
    except sqlite3.Error:
        return {"epoch_id": epoch_id, "champion_lineage": [], "matchups": []}


def _bracket_from_conn(
    paths: WorkspacePaths, conn: sqlite3.Connection, epoch_id: str | None
) -> dict[str, Any]:
    """The bracket body — reads the open connection, never closes it."""
    if epoch_id is None:
        return {"epoch_id": None, "champion_lineage": [], "matchups": []}

    gen_rows = _query(
        conn,
        "SELECT epoch_id, generation_id, parent_generation_id, promoted "
        "FROM generations WHERE epoch_id = ?",
        (epoch_id,),
    )
    generations = [
        {
            "generation_id": r["generation_id"],
            "parent_generation_id": r["parent_generation_id"],
            "promoted": bool(r["promoted"]),
        }
        for r in gen_rows
    ]
    champion_lineage = _champion_lineage(generations)

    # Select the structure-aware columns alongside the legacy
    # per-matchup ones. The v3 columns (structure / *_json) may be
    # absent on an index that predates the migration — the SELECT is
    # split so a missing-column error on the structure columns does
    # not blank out the legacy matchups (back-compat: gauntlet reads
    # must stay intact). ``_query`` swallows the sqlite error and
    # returns [] for the structure-aware query in that case.
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
    # structure record, not a champion-vs-challenger duel (it carries no
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

    # Structure-aware envelope (§3.1). Read the v3 columns
    # defensively: if they are absent (pre-migration index) the query
    # returns [] and the structure degenerates to gauntlet — leaving
    # the legacy ``matchups`` / ``champion_lineage`` byte-identical.
    # The champion-eval columns are v8; a pre-v8 (or fixture) index lacks
    # them, and a SELECT naming a missing column errors → _query returns []
    # → the whole structure envelope degrades. So include them only when the
    # table actually has them (PRAGMA), and read them defensively per-row.
    _tcols = {row["name"] for row in _query(conn, "PRAGMA table_info(tournaments)", ())}
    _champ_sel = (
        ", champion_eval_mode, champion_run_ref"
        if {"champion_eval_mode", "champion_run_ref"} <= _tcols
        else ""
    )
    struct_rows = _query(
        conn,
        "SELECT tournament_id, structure, structure_params_json, "
        "competitors_json, rounds_json, standings_json, ran_at, "
        "parent_generation_id, child_generation_id, parent_scalar"
        + _champ_sel
        + " FROM tournaments WHERE epoch_id = ? ORDER BY ran_at ASC, tournament_id ASC",
        (epoch_id,),
    )

    # ``_rget`` (tolerant additive-column read) is the shared accessor in
    # ``zicato.query._sqlite``.

    # The per-round CHAMPION (id + scalar + eval provenance: champion_eval_mode
    # / champion_run_ref — cached vs re-run) is carried on the per-CHALLENGER
    # rows: each has parent_generation_id = the round's champion. A FIELD row
    # has an EMPTY parent (a field is a round, not a duel), so resolve a field
    # record's champion from a sibling per-challenger row keyed by the
    # CHALLENGER (whose child is one of the field's competitors).
    champ_by_child: dict[str, dict[str, Any]] = {}
    for r in struct_rows:
        cg = r["child_generation_id"]
        pg = r["parent_generation_id"]
        if cg and pg:
            champ_by_child[str(cg)] = {
                "id": str(pg),
                "scalar": r["parent_scalar"],
                # A legacy row (pre-v8 index) has the column as NULL, and the
                # schema's rule for that is "mode unknown, treat as full". The
                # default belongs HERE, on the read of a real row — not on the
                # assembled champion, where it would also fire for a round that
                # has NO row yet and claim an evaluation that never happened.
                "eval_mode": _rget(r, "champion_eval_mode") or "full",
                "run_ref": _rget(r, "champion_run_ref"),
            }

    def _field_champion(comps: list[Any]) -> dict[str, Any] | None:
        """The champion of a FIELD row, whose own parent column is empty.

        A field row is a round, not a duel, so ``_upsert_field_tournament``
        leaves its parent/child columns empty on purpose. The champion has to
        come from the competitor list, and the two ways of reading that list
        are NOT equivalent:

        * The record TAGS the champion (``role: "champion"`` — the shape
          ``competitors_meta`` writes, champion first). Read the tag.
        * Borrowing "the first competitor that appears in ``champ_by_child``"
          reads the champion's OWN crowning duel, whose parent is the champion
          it BEAT. That named the PREVIOUS champion on every round after a
          promotion, so a beaten champion went on defending every later round.

        The champion's scalar and eval provenance (cached vs re-run) still ride
        on the crowning row of a CHALLENGER IN THIS FIELD, whose parent IS this
        round's champion — that borrow is correct and is what the old code was
        reaching for. Keying it to this field's own challengers is what keeps a
        HELD champion's provenance on the CURRENT round: one champion defends
        several rounds, so an unrestricted search finds its earliest defence and
        reports that round's scalar and cached-vs-fresh mode instead.

        For a record whose competitors carry no role (hand-built, or written
        before the tag), fall back on the structural fact that a field's
        champion COMPETES in the field: prefer a borrowed champion that is
        itself one of the competitors. Competitor order drives the walk, so the
        answer is deterministic.
        """
        ids = [str(c.get("generation_id") if isinstance(c, dict) else c) for c in comps]
        in_field = set(ids)
        tagged = next(
            (
                str(c.get("generation_id") or "")
                for c in comps
                if isinstance(c, dict) and str(c.get("role") or "") == "champion"
            ),
            "",
        )
        if tagged:
            sibling = next(
                (
                    v
                    for k, v in champ_by_child.items()
                    if str(v.get("id")) == tagged and k in in_field and k != tagged
                ),
                None,
            )
            base = dict(sibling) if sibling else {}
            base["id"] = tagged
            return base
        for key in ids:
            borrowed = champ_by_child.get(key)
            if borrowed is not None and str(borrowed.get("id")) in in_field:
                return dict(borrowed)
        for key in ids:
            if key in champ_by_child:
                return dict(champ_by_child[key])
        return None

    def _champion_for(row: sqlite3.Row, comps: list[Any]) -> dict[str, Any] | None:
        # a per-challenger / gauntlet row carries the champion directly; a
        # field row has no parent of its own, so ``_field_champion`` reads it
        # off the competitor list.
        cid = row["parent_generation_id"]
        base = (
            {
                "id": str(cid),
                "scalar": row["parent_scalar"],
                "eval_mode": _rget(row, "champion_eval_mode") or "full",
                "run_ref": _rget(row, "champion_run_ref"),
            }
            if cid is not None and str(cid) != ""
            else None
        )
        if base is None:
            base = _field_champion(comps)
        if base is None:
            return None
        sc = base.get("scalar")
        return {
            "id": base["id"],
            "scalar": coerce_float(sc),
            # No default here: every path that read a ROW already applied the
            # legacy "NULL ⇒ full" rule above, so an absent mode means there was
            # no row to read — a round whose champion has not been evaluated yet
            # (the field row is written at OPEN, before any crowning row). That
            # is genuinely unknown, and the round timeline already carries
            # ``eval_mode: None`` for it; the tree renders plain "defends"
            # rather than claiming "defends · re-run".
            "eval_mode": base.get("eval_mode"),
            "run_ref": base.get("run_ref"),
        }

    # FIELD-level rows (``{epoch}:field:{first_challenger}``) carry the
    # whole round's settled structure — round pairings + Copeland
    # standings + competitor field — for swiss / elim. When one exists
    # for a structure, the per-challenger ``{epoch}:{parent}->{child}``
    # rows of THAT structure are NOT the structure view's source (they
    # flatten one challenger's crowning duel, the wrong shape for the
    # ladder), so we drop them from the structure list and let the
    # field record stand. The per-challenger rows remain in the index
    # (the gauntlet matchup list + crowning columns still read them);
    # they are merely excluded from this structure-aware envelope.
    # Racing has no field record, so its per-challenger rows survive —
    # ``reconstructRacing`` aggregates them on the read side.
    field_structures = {
        _normalize_structure(r["structure"])
        for r in struct_rows
        if _is_field_tournament_id(r["tournament_id"])
    }
    tournaments: list[dict[str, Any]] = []
    epoch_structure = "gauntlet"
    epoch_structure_params: dict[str, Any] = {}
    for r in struct_rows:
        structure = _normalize_structure(r["structure"])
        params = _opt_json(r["structure_params_json"])
        params = params if isinstance(params, dict) else {}
        # The epoch's structure is the contract-frozen value; every
        # tournament in the epoch shares it, so the last non-gauntlet
        # value wins (they should all agree).
        if structure != "gauntlet":
            epoch_structure = structure
            epoch_structure_params = params
        # Suppress a per-challenger row whose structure has a field
        # record — the field record is the authoritative view.
        if structure in field_structures and not _is_field_tournament_id(r["tournament_id"]):
            continue
        competitors = _opt_json(r["competitors_json"])
        rounds = _opt_json(r["rounds_json"])
        standings = _opt_json(r["standings_json"])
        comp_list = competitors if isinstance(competitors, list) else []
        # The per-round CHAMPION — id + loss + eval provenance (cached vs
        # re-run) read CANONICALLY from the records, so the frontend reads the
        # champion spine instead of reconstructing it.
        # An elim record is enriched with the served elim model (sorted
        # rounds + bracket_side/loser + gen_states) — the per-round minis
        # read these entries by tournamentRef, so the model must ride here
        # exactly as it does on /api/tournament-structure (DQ1).
        tournaments.append(
            attach_elim_states(
                {
                    "tournament_id": r["tournament_id"],
                    "structure": structure,
                    "structure_params": params,
                    "competitors": comp_list,
                    "rounds": rounds if isinstance(rounds, list) else [],
                    "standings": standings if isinstance(standings, list) else [],
                    "champion": _champion_for(r, comp_list),
                }
            )
        )

    # No tournament ROW resolved a non-gauntlet structure — e.g. a run torn
    # down before any bracket completed leaves zero rows, so the scan above
    # never overrides the gauntlet default. Fall back to the epoch's
    # CONTRACT-FROZEN structure (scoring.json, then config.json's scoring)
    # so the API agrees with the configured single_elim/swiss/racing rather
    # than mislabelling the epoch gauntlet.
    if epoch_structure == "gauntlet":
        layout = layout_of(paths)
        block = _tournament_block_from_scoring(_read_json_value(layout.scoring(epoch_id)))
        if block is None:
            cfg = _read_json_value(layout.epoch_config(epoch_id))
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
            "note": "index not built; run zicato repair index",
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


def _opt_score(value: Any) -> float | None:
    """Coerce a raw ``score`` field into a finite float in ``[0, 1]`` or ``None``.

    The continuous per-entry outcome (#18). ``None`` (the back-compat
    default) when the field is absent, a bool, or a non-finite number —
    every such case degrades to the bool ``pass_fail`` display upstream.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    f = float(value)
    if f != f or f in (float("inf"), float("-inf")):  # NaN / inf guard
        return None
    return f


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
    otherwise — a pre-score loss.json reads exactly as before. Missing /
    malformed files are skipped silently — a generation with no telemetry
    yet yields ``{}``.
    """
    out: dict[str, dict[str, Any]] = {}
    runs_dir = layout_of(paths).runs_dir(epoch_id, generation_id)
    if not runs_dir.is_dir():
        return out
    for run_dir in sorted(runs_dir.iterdir()):
        if not run_dir.is_dir():
            continue
        loss = _read_json_value(run_dir / "loss.json")
        if not isinstance(loss, dict):
            continue
        entry_id = loss.get("entry_id")
        if not isinstance(entry_id, str) or not entry_id:
            entry_id = run_dir.name
        drift = loss.get("drift_loss")
        prov = loss.get("scoring_provenance")
        cell: dict[str, Any] = {
            "entry_id": entry_id,
            "drift_loss": coerce_float(drift),
            "pass_fail": _opt_bool(loss.get("pass_fail")),
            # Continuous per-entry outcome + its optional precision/recall
            # decomposition (#18). ``None`` for a pre-score loss.json.
            "score": _opt_score(loss.get("score")),
            "metrics": _opt_metrics(loss.get("metrics")),
            "run_id": loss.get("run_id") if isinstance(loss.get("run_id"), str) else run_dir.name,
            # Seam-1 drift-reduction provenance (#19). ``None`` on a pre-#19
            # loss.json (the field did not exist) — surfaced so the gate
            # breakdown can show which transform / plugin shaped drift_loss.
            "scoring_provenance": str(prov) if isinstance(prov, str) and prov else None,
            # Did this run OBSERVE drift at all? An adapter that emits no drift
            # stream still writes a structural ``drift_loss`` of 0.0 with an
            # empty ``drift_counts``, which is indistinguishable on the wire
            # from a run that watched for drift and saw none. Either a recorded
            # drift event or a non-zero loss proves the channel carries signal;
            # nothing else does. Internal to this module — the endpoint serves
            # the matchup-wide ``drift_present`` derived from it.
            "drift_observed": bool(loss.get("drift_counts"))
            or bool(coerce_float(drift) not in (None, 0.0)),
        }
        sid = loss.get("adk_session_id")
        if isinstance(sid, str) and sid:
            cell["adk_session_id"] = sid
        out[entry_id] = cell
    return out


def _read_gen_score(paths: WorkspacePaths, epoch_id: str, generation_id: str) -> dict[str, Any]:
    """Read a generation's cached ``gen_score.json`` aggregate.

    Returns the raw aggregate dict (``scalar`` / ``drift_loss_mean`` /
    ``pass_rate`` / ``scalar_components`` / ...), or ``{}`` when the
    file is absent or malformed.
    """
    return read_gen_score(layout_of(paths), epoch_id, generation_id)


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
    fail their predicate are still told apart by their drift losses, exactly as
    before this fall-through existed. When no channel separates them the entry is
    ``"flat"`` and ``decided_by`` names the first channel it was READ on, so the
    client knows which quantity the tie is a tie in.

    Resolution is per row (DQ3): a board where only some entries carry a
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
            # precision/recall decomposition. ``None`` for a pre-score
            # loss.json, so a bool-only entry carries score/metrics ==
            # None and renders by its pass bit exactly as before.
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
            # so it bounds how much of ``delta_score`` is readable, not the
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

    parent_score = _read_gen_score(paths, epoch_id, champion_id) if champion_id else {}
    child_score = _read_gen_score(paths, epoch_id, challenger_id)
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
        p_mean = _opt_score(parent_score.get("mean_score"))
        c_mean = _opt_score(child_score.get("mean_score"))
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
        for name in names:
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
# The served ELIM MODEL — rounds canonicalized + per-generation states (DQ1)
# ---------------------------------------------------------------------------

_ELIM_STRUCTURES = frozenset({"single_elim", "double_elim"})


def _round_sort_key(r: dict[str, Any], position: int) -> tuple[Any, int]:
    """The temporal sort key: ``round_index`` (legacy) / ``stage_index``.

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
    """A competitor/winner id under the DQ1 scalar contract, or ``None``.

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

    The client (``svg.js`` elimFlow, and its radial twin) used to derive
    this whole model per render: re-sorting mis-ordered caller columns,
    de-duplicating backend-duplicated matches, classifying each loss as an
    elimination vs a winners→losers drop, and guarding against phantom
    eliminations. That derivation was a DQ1 breach living behind an
    under-specified payload; this fold is it, moved server-side, so every
    consumer (Python service, Rust supervisor, the node mock) serves ONE
    identical model. Ported line-for-line into
    ``crates/supervisor/src/elim_states.rs`` — the shared fixture
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
      eliminated_at_round, side_by_round, lb_entry_round, projected}``.
      The elimination-vs-drop rule is the client's, verbatim: a loss with
      NO later appearance is an elimination there; a loss followed by a
      later appearance is a winners→losers drop (the second life).
      ``null`` = undecided (DQ2); ``side_by_round`` keys are stringified
      column indices (JSON object keys).

    Pure + best-effort: a malformed blob degrades to empty lists, never
    raises (DQ3).
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
                # F4: only a still-pending first-seen yields to a decided
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


def _structure_from_index(
    paths: WorkspacePaths, epoch_id: str, tournament_id: str
) -> dict[str, Any] | None:
    """The settled structure state from the SQLite ``tournaments`` row.

    Returns ``None`` when the index is absent, the row is missing, or the
    v3 structure columns do not exist (pre-migration index) — every such
    case falls through to the next link in the resolution chain.
    """
    try:
        with open_index_ro(paths.index_db) as conn:
            rows = _query(
                conn,
                "SELECT structure, structure_params_json, competitors_json, "
                "rounds_json, standings_json FROM tournaments "
                "WHERE epoch_id = ? AND tournament_id = ? LIMIT 1",
                (epoch_id, tournament_id),
            )
            if not rows:
                return None
            r = rows[0]
            params = _opt_json(r["structure_params_json"])
            competitors = _opt_json(r["competitors_json"])
            rounds = _opt_json(r["rounds_json"])
            standings = _opt_json(r["standings_json"])
            # ``field_status_json`` is a v5 column. A real index is migrated to
            # v5 on open, but a hand-built / pre-migration index may lack the
            # column — query it separately and degrade to an empty list rather
            # than letting a missing column fail the whole resolution.
            field_status: Any = None
            try:
                fs_rows = _query(
                    conn,
                    "SELECT field_status_json FROM tournaments "
                    "WHERE epoch_id = ? AND tournament_id = ? LIMIT 1",
                    (epoch_id, tournament_id),
                )
                if fs_rows:
                    field_status = _opt_json(fs_rows[0]["field_status_json"])
            except sqlite3.Error:
                field_status = None
            # A row that exists but carries no structure internals (a gauntlet
            # row, or a NULL-backfilled pre-feature row) is not a useful
            # structure read; fall through so the active/loss-file links can
            # offer something richer.
            if rounds is None and standings is None and competitors is None:
                return None
            return _structure_envelope(
                epoch_id,
                tournament_id,
                "index",
                structure=r["structure"],
                structure_params=params,
                competitors=competitors,
                rounds=rounds,
                standings=standings,
                field_status=field_status,
            )
    except (_IndexAbsent, sqlite3.Error):
        return None


def _structure_from_active(
    paths: WorkspacePaths, epoch_id: str, tournament_id: str
) -> dict[str, Any] | None:
    """The structure state from the live ``active_tournament.json``.

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


def _structure_from_loss_files(
    paths: WorkspacePaths, epoch_id: str, tournament_id: str
) -> dict[str, Any] | None:
    """Reconstruct a degenerate single-match view from per-run loss files.

    The last link in the resolution chain (mirrors ``build_matchup_grid``'s
    index-free read). A tournament id encodes its crowning pair as
    ``{epoch}:{champion}->{challenger}`` (the ingester convention); when
    that decodes, render one round / one match between the two sides with
    their settled drift-loss scalars. When it does not decode, return a
    bare envelope so the handler still answers HTTP 200.
    """
    if not epoch_id:
        return None
    champion, challenger = _decode_crowning_pair(tournament_id)
    if not challenger:
        return None
    parent_score = _read_gen_score(paths, epoch_id, champion) if champion else {}
    child_score = _read_gen_score(paths, epoch_id, challenger)
    parent_scalar, child_scalar, delta = _scalar_pair(
        parent_score.get("scalar"), child_score.get("scalar")
    )
    competitors: list[dict[str, Any]] = []
    standings: list[dict[str, Any]] = []
    if champion:
        competitors.append({"generation_id": champion, "seed": 1, "role": "champion"})
        standings.append(
            {"generation_id": champion, "rank": 1, "scalar": parent_scalar, "role": "champion"}
        )
    competitors.append({"generation_id": challenger, "seed": 2, "role": "challenger"})
    standings.append(
        {"generation_id": challenger, "rank": 2, "scalar": child_scalar, "role": "challenger"}
    )
    # The challenger applied (it has a settled scalar), so the proposing
    # step is reconstructed as a single applied entry — never an empty
    # idle tracker for a tournament that actually ran.
    field_status: list[dict[str, Any]] = [
        {"generation_id": challenger, "status": "applied", "reason": "", "seed": 2}
    ]
    match: dict[str, Any] = {
        "match_id": "r0_m0",
        "competitors": [c for c in (champion, challenger) if c],
        "winner": "",
        "decision": "",
        "delta_scalar": delta,
        "bracket_slot": "",
        "bye": False,
    }
    return {
        "epoch_id": epoch_id,
        "tournament_id": tournament_id,
        "structure": "gauntlet",
        "structure_params": {},
        "competitors": competitors,
        "rounds": [{"stage_index": 0, "label": "Gauntlet", "matches": [match]}],
        "standings": standings,
        "field_status": field_status,
        "source": "loss_files",
    }


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
    """``GET /api/tournament-structure/{epoch_id}/{tournament_id}``.

    The single read the UI uses to render a bracket / standings / racing
    ladder for one tournament (TOURNAMENT-DATA-MODEL.md §3.2). Resolution
    order mirrors ``build_matchup_grid``'s fallback chain:

    1. the SQLite ``tournaments`` row's structure columns (``source:
       "index"``);
    2. the live ``active_tournament.json`` when it matches the coordinate
       (``source: "active"``);
    3. a degenerate single-match reconstruction from the per-run
       ``loss.json`` / ``gen_score.json`` files (``source: "loss_files"``).

    A malformed / unresolvable id degrades to an empty gauntlet structure
    at HTTP 200 (matching every other handler in ``endpoints.py``).
    """
    if not epoch_id or not tournament_id:
        return _empty_tournament_structure(epoch_id, tournament_id, "loss_files")
    for resolver in (_structure_from_index, _structure_from_active, _structure_from_loss_files):
        result = resolver(paths, epoch_id, tournament_id)
        if result is not None:
            enriched = _enrich_field_status(paths, epoch_id, tournament_id, result)
            enriched = _enrich_override_status(paths, epoch_id, tournament_id, enriched)
            enriched = _enrich_diversity(paths, epoch_id, enriched)
            return _enrich_standings_ratings(paths, epoch_id, enriched)
    return _empty_tournament_structure(epoch_id, tournament_id, "loss_files")


def _enrich_override_status(
    paths: WorkspacePaths, epoch_id: str, tournament_id: str, result: dict[str, Any]
) -> dict[str, Any]:
    """Attach the operator-override readback from the durable field record.

    A field round's operator promote/reject overrides are recorded on the
    durable ``tournaments/field-*.json`` snapshot (the orchestrator stamps
    ``override_status`` — a ``{generation_id: {action, ts, reason, state}}``
    map — and ``promoted_generation_ids`` — the full advanced SET — at
    settle). The index columns do not carry them, so lift them from the
    durable record onto the structure result the dashboard reads.

    The durable record is keyed on the field's first challenger
    (``field-<gid>.json``), which differs from the queried ``tournament_id``;
    match the record whose competitor field overlaps this result's
    challengers. KEY-ABSENT when no override fired (the common case) or when
    no durable record matches, so a gate-decided field round and every
    pre-feature run are byte-identical to before this readback existed.
    """
    if result.get("override_status") or result.get("promoted_generation_ids"):
        return result  # already carried by the winning resolver — never clobber
    from zicato.core.workspace import field_tournaments_dir  # noqa: PLC0415

    challengers = set(_challenger_generation_ids(result))
    tdir = field_tournaments_dir(paths.root, epoch_id)
    if not tdir.is_dir():
        return result
    for record_path in sorted(tdir.glob("field-*.json")):
        record = _read_json_value(record_path)
        if not isinstance(record, dict):
            continue
        override_status = record.get("override_status")
        promoted_ids = record.get("promoted_generation_ids")
        if not (isinstance(override_status, dict) and override_status) and not (
            isinstance(promoted_ids, list) and promoted_ids
        ):
            continue
        # Match this record to the queried structure: same tournament_id, or
        # (the common case, since the durable id is field-keyed) an overlap of
        # the competitor generation ids.
        rec_competitors = {
            str(c.get("generation_id", ""))
            for c in (record.get("competitors") or [])
            if isinstance(c, dict)
        }
        if record.get("tournament_id") != tournament_id and not (
            challengers and challengers & rec_competitors
        ):
            continue
        if isinstance(override_status, dict) and override_status:
            result["override_status"] = {
                str(gid): dict(prov)
                for gid, prov in override_status.items()
                if isinstance(prov, dict)
            }
        if isinstance(promoted_ids, list) and promoted_ids:
            result["promoted_generation_ids"] = [str(g) for g in promoted_ids]
        break
    return result


def _enrich_field_status(
    paths: WorkspacePaths, epoch_id: str, tournament_id: str, result: dict[str, Any]
) -> dict[str, Any]:
    """Backfill ``field_status`` from the live envelope when the resolved
    structure lacks it.

    The per-experiment index row carries the settled bracket but not the
    proposing-step outcomes (the per-challenger applied/rejected records
    live only on ``active_tournament.json``, which the multi-challenger
    path retains with ``phase="completed"``). So when the winning resolver
    is the index (or any source whose ``field_status`` is empty) but the
    live envelope still matches this coordinate, lift its ``field_status``
    onto the result so a just-completed epoch's proposing step survives.
    Purely additive — never overwrites a non-empty field-status.
    """
    if result.get("field_status"):
        return result
    active = _structure_from_active(paths, epoch_id, tournament_id)
    if active is not None and active.get("field_status"):
        result["field_status"] = active["field_status"]
    return result


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

    Each ``standings`` record gains ``elo`` / ``elo_se`` / ``elo_games`` (DQ2
    snake_case), joined server-side from the analytical index so the client
    renders the rating column without re-deriving anything (DQ1). Best-effort
    by contract (DQ3): an absent / cold index — or a generation the fold has
    not rated (zero settled duels; a pre-reindex file) — attaches the null
    triple, never an error.

    Settled-vs-live: the structure payload is request-scoped (the GET handler
    calls this reader once per fetch — there is no SSE/heartbeat recompute),
    and the rating join reads only the SETTLED index. A LIVE field's standings
    (resolved off ``active_tournament.json``) simply carry whatever the index
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
