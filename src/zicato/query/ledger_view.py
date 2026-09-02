"""ledger_view — the EXPERIMENTS LEDGER for one epoch.

An epoch is a sequence of experiments: each one a proposed IDEA, applied at
some SITES, run against the board, and settled by the gate. Every part of
that sentence is already recorded, across three index tables: the
``experiments`` rows carry ``hypothesis_core_idea`` /
``tournament_decision`` / ``rejection_reason`` / the three deltas,
``patches`` names the sites each experiment touched, and
``generations.round_index`` says which evolve round minted it.

This reader joins those three server-side and once:
``GET /api/epoch/{id}/experiments-ledger`` returns one row per experiment,
in ROUND ORDER, carrying exactly the columns the ledger table renders.

Discipline, matching every other reader here:

* The decision token is passed through the ONE canonical classifier
  (:mod:`zicato.query.decisions`), so the ledger can never disagree with
  the lineage / epoch feeds about a verdict.
* ``rejection_reason`` is the RECORDED field, verbatim — the frontend
  renders it, never parses it.
* Absence degrades FIELD-BY-FIELD: a never-settled experiment reads with
  null deltas and a null decision rather than as a missing row. A workspace with no
  index degrades to an empty ledger plus an honest ``note``, because the
  ledger is a projection of the index and inventing one from the trees
  would be a second, divergent source.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from zicato.query._sqlite import (
    _IndexAbsent,
    _query,
    _rget,
    open_index_ro,
    with_index_not_built_note,
)
from zicato.query.decisions import canonical_decision, promoted_tristate
from zicato.query.paths import WorkspacePaths, _resolve_epoch_id

#: What a row's ``round_index`` sorts as when the birth round was never
#: stamped (a pre-v7 index): AFTER every stamped round, so the known
#: sequence still reads top-to-bottom and the unstamped tail follows in
#: generation order rather than jumping the queue at round 0.
_UNSTAMPED_ROUND = float("inf")


def _opt_text(value: Any) -> str | None:
    """A non-empty recorded string, else ``None`` (NULL / '' / non-string)."""
    return value if isinstance(value, str) and value.strip() else None


def _opt_float(value: Any) -> float | None:
    """A finite recorded number, else ``None``. Booleans are not numbers."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _empty_ledger(epoch_id: str | None) -> dict[str, Any]:
    return {"epoch_id": epoch_id, "experiments": []}


def build_experiments_ledger(paths: WorkspacePaths, epoch_id: str | None = None) -> dict[str, Any]:
    """One epoch's experiments, joined into the ledger the epoch page renders.

    ``epoch_id`` defaults to the CURRENT epoch. The payload is::

        {"epoch_id": "e0",
         "experiments": [{"generation_id": "v1",
                          "round_index": 1,
                          "core_idea": "…",
                          "mutation_ids": ["prompt.system", …],
                          "decision": "rejected",
                          "promoted": false,
                          "rejection_reason": "…",
                          "scalar_score_delta": 0.02,
                          "drift_loss_delta": …,
                          "pass_rate_delta": …}, …]}

    Rows are in ROUND ORDER — ``round_index`` ascending, an unstamped
    round last, ties broken by the index's own generation ordering
    (created_at, then id) so the ledger reads in the order the epoch
    actually happened.

    A workspace with no epoch reads ``{"epoch_id": null, "experiments":
    []}``; one with no index adds ``note`` and an empty list. ``note`` is
    ABSENT on the healthy path (omit-when-default), so a served ledger
    carries no ceremony.
    """
    try:
        epoch_id = _resolve_epoch_id(paths, epoch_id)
    except ValueError:
        # an unknown / malformed epoch degrades to the empty ledger, exactly
        # as the round timeline does for the same coordinate.
        epoch_id = None
    if epoch_id is None:
        return _empty_ledger(None)

    try:
        with open_index_ro(paths.index_db) as conn:
            return {
                "epoch_id": epoch_id,
                "experiments": _ledger_rows(conn, epoch_id),
            }
    except (_IndexAbsent, sqlite3.Error):
        return with_index_not_built_note(_empty_ledger(epoch_id))


def _ledger_rows(conn: sqlite3.Connection, epoch_id: str) -> list[dict[str, Any]]:
    """The joined, round-ordered experiment rows for one epoch."""
    # ``SELECT *`` rather than a named column list is required on
    # ``generations``: ``round_index`` arrived in schema v7 and an older index
    # may not carry it, and naming a missing column fails the WHOLE query and
    # blanks the ledger. The tolerant ``_rget`` accessor instead reads the
    # absence as a null round.
    gen_rows = _query(
        conn,
        "SELECT * FROM generations WHERE epoch_id = ? ORDER BY created_at, generation_id",
        (epoch_id,),
    )
    order: dict[str, int] = {}
    rounds: dict[str, int | None] = {}
    parents: dict[str, str | None] = {}
    for position, row in enumerate(gen_rows):
        gid = _opt_text(_rget(row, "generation_id"))
        if gid is None:
            continue
        order[gid] = position
        raw_round = _rget(row, "round_index")
        stamped = isinstance(raw_round, int) and not isinstance(raw_round, bool)
        rounds[gid] = int(raw_round) if stamped else None
        parents[gid] = _opt_text(_rget(row, "parent_generation_id"))

    sites: dict[str, list[str]] = {}
    for row in _query(
        conn,
        "SELECT generation_id, mutation_id FROM patches WHERE epoch_id = ? "
        "ORDER BY generation_id, mutation_id",
        (epoch_id,),
    ):
        gid = _opt_text(_rget(row, "generation_id"))
        mid = _opt_text(_rget(row, "mutation_id"))
        if gid is None or mid is None:
            continue
        touched = sites.setdefault(gid, [])
        # One experiment can carry several patches against the SAME site
        # (a re-edit within one proposal); the ledger names each site once.
        if mid not in touched:
            touched.append(mid)

    rows: list[dict[str, Any]] = []
    for row in _query(conn, "SELECT * FROM experiments WHERE epoch_id = ?", (epoch_id,)):
        gid = _opt_text(_rget(row, "generation_id"))
        if gid is None:
            continue
        raw_decision = _opt_text(_rget(row, "tournament_decision"))
        rows.append(
            {
                "generation_id": gid,
                # The seed's parent, carried so the renderer can reach the ONE
                # shared decision classifier: a parentless generation is the
                # BASELINE rather than a candidate still racing. Without it a settled
                # epoch's seed row — which records no tournament decision,
                # because it never faced one — reads as in-flight forever.
                "parent_generation_id": parents.get(gid),
                "round_index": rounds.get(gid),
                "core_idea": _opt_text(_rget(row, "hypothesis_core_idea")),
                "mutation_ids": sites.get(gid, []),
                "decision": canonical_decision(raw_decision),
                "promoted": promoted_tristate(raw_decision),
                "rejection_reason": _opt_text(_rget(row, "rejection_reason")),
                "scalar_score_delta": _opt_float(_rget(row, "scalar_score_delta")),
                "drift_loss_delta": _opt_float(_rget(row, "drift_loss_delta")),
                "pass_rate_delta": _opt_float(_rget(row, "pass_rate_delta")),
            }
        )

    rows.sort(
        key=lambda r: (
            _UNSTAMPED_ROUND if r["round_index"] is None else r["round_index"],
            # an experiment with no generation row of its own sorts last within
            # its round rather than first (the index's ordering is the tiebreak).
            order.get(str(r["generation_id"]), len(order)),
            str(r["generation_id"]),
        )
    )
    return rows


__all__ = ["build_experiments_ledger"]
