"""Thin read helpers over the zicato analytical index.

This module is the read side of the index — it owns connection
construction and a small set of common ``SELECT`` helpers. It does not
write; all writes go through :mod:`zicato.index.ingest`.

Two design rules:

* **WAL-friendly opens.** :func:`open_index` opens the database in WAL
  journal mode so a reader (R9-2's analytics surface, the Rust
  supervisor) does not block the orchestrator's live dual-writes and
  vice versa. The connection's ``row_factory`` is :class:`sqlite3.Row`
  so callers get name-addressable rows.
* **Tolerate a missing database.** Every helper here is given a
  ``db_path`` and is expected to be called against a workspace that may
  never have been indexed. :func:`open_index` raises a clear
  :class:`IndexNotBuiltError` (whose message points at ``zicato
  reindex``); the convenience selectors below catch that and return an
  empty result so a caller building a dashboard does not have to
  special-case the first run.
"""

from __future__ import annotations

import json
import sqlite3
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from zicato.core.types import EXPERIMENT_MEMORY_MAX_ENTRIES, PriorExperiment
from zicato.index.schema import read_schema_version


class IndexNotBuiltError(FileNotFoundError):
    """Raised when an index database is requested but does not exist.

    A subclass of :class:`FileNotFoundError` so existing
    ``except FileNotFoundError`` handlers keep working, but with a
    message that explicitly tells the operator to run ``zicato
    reindex``.
    """


def open_index(db_path: Path) -> sqlite3.Connection:
    """Open the index database for reading.

    The connection is configured for the index's concurrent-read
    posture:

    * ``row_factory = sqlite3.Row`` — callers index columns by name.
    * WAL journal mode — readers never block the orchestrator's
      dual-writes. ``PRAGMA journal_mode=WAL`` is a no-op when the file
      was already created in WAL mode (the canonical case), and harmless
      otherwise.
    * ``PRAGMA busy_timeout`` — a short wait so a read that races a
      writer's commit retries instead of raising ``database is locked``.

    Raises
    ------
    IndexNotBuiltError
        If ``db_path`` does not exist. The message suggests
        ``zicato reindex``.

    Notes
    -----
    The connection is *read-oriented* but not hard read-only — SQLite's
    URI ``mode=ro`` would refuse to even create the WAL sidecar files,
    which trips up some environments. We instead open normally and
    simply never issue writes from this module.
    """
    if not db_path.exists():
        raise IndexNotBuiltError(
            f"zicato index database not found at {db_path}; "
            "run `zicato reindex` to build it from the workspace files"
        )
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def index_schema_version(db_path: Path) -> int | None:
    """Return the schema version stamped in the index, or ``None``.

    ``None`` means the database file does not exist. A returned integer
    is the value of ``PRAGMA user_version`` — a caller can compare it to
    :data:`zicato.index.schema.SCHEMA_VERSION` to detect a stale index.
    """
    if not db_path.exists():
        return None
    conn = sqlite3.connect(str(db_path))
    try:
        return read_schema_version(conn)
    finally:
        conn.close()


def _select(db_path: Path, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
    """Run a read query, returning rows; ``[]`` when the index is missing.

    Centralises the "tolerate a missing database" rule for the
    convenience selectors below. A missing file yields an empty list
    rather than an exception so dashboard-style callers can render an
    empty state on a never-indexed workspace.
    """
    try:
        conn = open_index(db_path)
    except IndexNotBuiltError:
        return []
    try:
        return list(conn.execute(sql, tuple(params)).fetchall())
    finally:
        conn.close()


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    """Return the column names of ``table`` (empty when the table is absent)."""
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    except sqlite3.Error:
        return set()
    return {r[1] for r in rows}


def _select_optional_columns(
    db_path: Path,
    table: str,
    base_columns: Sequence[str],
    optional_columns: Sequence[str],
    where: str,
    params: Sequence[Any],
) -> list[sqlite3.Row]:
    """Select ``base_columns`` plus whichever ``optional_columns`` exist.

    A column added in a later schema version (e.g. ``match_id`` in v4)
    may be absent from a legacy index that the read-only dashboard opens
    without migrating. Rather than letting the ``SELECT`` fail with "no
    such column", this probes the live table for each optional column and
    emits ``NULL AS <col>`` for any that are missing — so a legacy row
    still loads with the field present-but-null, exactly the back-compat
    contract the dashboard relies on. Returns ``[]`` for a missing index.
    """
    try:
        conn = open_index(db_path)
    except IndexNotBuiltError:
        return []
    try:
        present = _table_columns(conn, table)
        select_terms = list(base_columns)
        for col in optional_columns:
            if col in present:
                select_terms.append(col)
            else:
                select_terms.append(f"NULL AS {col}")
        sql = f"SELECT {', '.join(select_terms)} FROM {table} {where}"
        return list(conn.execute(sql, tuple(params)).fetchall())
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Common selectors
# ---------------------------------------------------------------------------


def all_epochs(db_path: Path) -> list[sqlite3.Row]:
    """Return every indexed epoch, oldest first.

    The selection includes ``parent_epoch_id`` (v2 column); a v1
    database is upgraded in place on the next write so callers always
    see the column on a read after any write.
    """
    return _select(
        db_path,
        "SELECT epoch_id, contract_hash, created_at, closed, parent_epoch_id "
        "FROM epochs ORDER BY created_at, epoch_id",
    )


def generations_for_epoch(db_path: Path, epoch_id: str) -> list[sqlite3.Row]:
    """Return every generation under ``epoch_id``, oldest first.

    ``round_index`` (the v7 birth-round column) is selected as an
    optional column: a legacy index opened read-only without the
    migration still loads each row with ``round_index`` present-but-null,
    so a consumer can group ``Epoch -> Round -> {challengers}`` and
    degrade on a null.
    """
    return _select_optional_columns(
        db_path,
        "generations",
        ["epoch_id", "generation_id", "parent_generation_id", "promoted", "created_at"],
        ["round_index", "elo", "elo_games"],
        "WHERE epoch_id = ? ORDER BY created_at, generation_id",
        (epoch_id,),
    )


def runs_for_generation(db_path: Path, epoch_id: str, generation_id: str) -> list[sqlite3.Row]:
    """Return every run row under one generation, ordered by entry id."""
    return _select_optional_columns(
        db_path,
        "runs",
        (
            "run_id",
            "epoch_id",
            "generation_id",
            "entry_id",
            "started_at",
            "ended_at",
            "aborted",
            "runtime_ms",
            "tournament_id",
        ),
        ("match_id",),
        "WHERE epoch_id = ? AND generation_id = ? ORDER BY entry_id, run_id",
        (epoch_id, generation_id),
    )


def loss_profiles_for_generation(
    db_path: Path, epoch_id: str, generation_id: str
) -> list[sqlite3.Row]:
    """Return every loss-profile row under one generation."""
    return _select_optional_columns(
        db_path,
        "loss_profiles",
        (
            "run_id",
            "epoch_id",
            "generation_id",
            "entry_id",
            "drift_loss",
            "pass_fail",
            "runtime_ms",
            "wall_clock_budget_exceeded",
            "loss_json",
            "tournament_id",
        ),
        ("match_id", "cached", "source_epoch", "source_run"),
        "WHERE epoch_id = ? AND generation_id = ? ORDER BY entry_id, run_id",
        (epoch_id, generation_id),
    )


def runs_for_tournament(db_path: Path, tournament_id: str) -> list[sqlite3.Row]:
    """Return every ``runs`` row that belongs to one tournament round.

    The FK was added in schema v2; a v1 database returns an empty list
    because every row's ``tournament_id`` is ``NULL``. Run ``zicato
    repair-tournament-fk`` to backfill the column on an existing v1+
    workspace.
    """
    return _select_optional_columns(
        db_path,
        "runs",
        (
            "run_id",
            "epoch_id",
            "generation_id",
            "entry_id",
            "started_at",
            "ended_at",
            "aborted",
            "runtime_ms",
            "tournament_id",
        ),
        ("match_id",),
        "WHERE tournament_id = ? ORDER BY entry_id, run_id",
        (tournament_id,),
    )


def loss_profiles_for_tournament(db_path: Path, tournament_id: str) -> list[sqlite3.Row]:
    """Return every ``loss_profiles`` row that belongs to one tournament round."""
    return _select_optional_columns(
        db_path,
        "loss_profiles",
        (
            "run_id",
            "epoch_id",
            "generation_id",
            "entry_id",
            "drift_loss",
            "pass_fail",
            "runtime_ms",
            "wall_clock_budget_exceeded",
            "loss_json",
            "tournament_id",
        ),
        ("match_id", "cached", "source_epoch", "source_run"),
        "WHERE tournament_id = ? ORDER BY entry_id, run_id",
        (tournament_id,),
    )


def epoch_ancestry(db_path: Path, epoch_id: str) -> list[sqlite3.Row]:
    """Return the chain from ``epoch_id`` back to the workspace's first epoch.

    Walks ``parent_epoch_id`` (the v2 column) one hop at a time,
    starting from the row for ``epoch_id`` and following each row's
    parent until ``parent_epoch_id IS NULL`` (the workspace's first
    epoch) or a cycle is detected (a safety guard — the lineage DAG
    is acyclic by construction).

    The returned list starts at ``epoch_id`` and ends at the root,
    oldest last. A workspace that has never been indexed (or whose
    ``epoch_id`` is unknown) yields an empty list.
    """
    try:
        conn = open_index(db_path)
    except IndexNotBuiltError:
        return []
    try:
        chain: list[sqlite3.Row] = []
        seen: set[str] = set()
        cur_id: str | None = epoch_id
        while cur_id is not None and cur_id not in seen:
            seen.add(cur_id)
            row = conn.execute(
                "SELECT epoch_id, contract_hash, created_at, closed, parent_epoch_id "
                "FROM epochs WHERE epoch_id = ?",
                (cur_id,),
            ).fetchone()
            if row is None:
                break
            chain.append(row)
            next_id = row["parent_epoch_id"]
            cur_id = next_id if isinstance(next_id, str) and next_id else None
        return chain
    finally:
        conn.close()


def metric_counts_for_run(db_path: Path, run_id: str) -> list[sqlite3.Row]:
    """Return every metric-count row recorded for one run."""
    return _select(
        db_path,
        "SELECT run_id, namespace, name, severity, count FROM metric_counts "
        "WHERE run_id = ? ORDER BY name, severity",
        (run_id,),
    )


def judge_losses_for_run(db_path: Path, run_id: str) -> list[sqlite3.Row]:
    """Return every per-judge loss row recorded for one run.

    Rows carry ``run_id`` + ``judge_name`` + ``weighted_loss`` /
    ``raw_loss`` / ``weight``. Ordered by ``judge_name`` so the iteration
    order is deterministic; an empty list means the run had no
    custom-judge drift attributable to any judge.
    """
    return _select(
        db_path,
        "SELECT run_id, judge_name, weighted_loss, raw_loss, weight "
        "FROM judge_losses WHERE run_id = ? ORDER BY judge_name",
        (run_id,),
    )


def judge_losses_for_generation(
    db_path: Path, epoch_id: str, generation_id: str
) -> list[sqlite3.Row]:
    """Return per-judge totals across every run under one generation.

    Sums ``weighted_loss`` / ``raw_loss`` over every ``judge_losses``
    row belonging to a run that landed under ``(epoch_id,
    generation_id)``. The returned rows carry ``judge_name``,
    ``total_weighted_loss``, ``total_raw_loss``, ``run_count`` (number
    of distinct runs the judge fired on), and ``weight`` (the most-
    recently-observed weight for the judge — judges keep a stable
    weight within an epoch so this is unambiguous in practice).
    Ordered so the noisiest judge appears first.
    """
    return _select(
        db_path,
        "SELECT jl.judge_name, "
        "SUM(jl.weighted_loss) AS total_weighted_loss, "
        "SUM(jl.raw_loss) AS total_raw_loss, "
        "COUNT(DISTINCT jl.run_id) AS run_count, "
        "MAX(jl.weight) AS weight "
        "FROM judge_losses AS jl "
        "JOIN runs AS r ON r.run_id = jl.run_id "
        "WHERE r.epoch_id = ? AND r.generation_id = ? "
        "GROUP BY jl.judge_name "
        "ORDER BY total_weighted_loss DESC, jl.judge_name",
        (epoch_id, generation_id),
    )


def judge_loss_trend(db_path: Path, epoch_id: str, judge_name: str) -> list[sqlite3.Row]:
    """Return one judge's per-generation totals along an epoch's timeline.

    For every generation under ``epoch_id`` where ``judge_name`` fired,
    returns one row: ``generation_id``, ``total_weighted_loss``,
    ``total_raw_loss``, ``run_count``. Rows are ordered by
    ``generation_id`` so the caller can plot the trend along the
    promoted spine. A judge that never fired in the epoch yields an
    empty list.
    """
    return _select(
        db_path,
        "SELECT r.generation_id, "
        "SUM(jl.weighted_loss) AS total_weighted_loss, "
        "SUM(jl.raw_loss) AS total_raw_loss, "
        "COUNT(DISTINCT jl.run_id) AS run_count "
        "FROM judge_losses AS jl "
        "JOIN runs AS r ON r.run_id = jl.run_id "
        "WHERE r.epoch_id = ? AND jl.judge_name = ? "
        "GROUP BY r.generation_id "
        "ORDER BY r.generation_id",
        (epoch_id, judge_name),
    )


def experiments_for_epoch(db_path: Path, epoch_id: str) -> list[sqlite3.Row]:
    """Return every experiment row under ``epoch_id``."""
    return _select(
        db_path,
        "SELECT epoch_id, generation_id, hypothesis_core_idea, hypothesis_why, "
        "hypothesis_json, tournament_decision, rejection_reason, scalar_score_delta, "
        "drift_loss_delta, pass_rate_delta, outcome_json FROM experiments "
        "WHERE epoch_id = ? ORDER BY generation_id",
        (epoch_id,),
    )


def _modulating_from_hypothesis_json(raw: Any) -> tuple[str, ...]:
    """Lift the ``modulating`` ids out of a recorded ``hypothesis_json``.

    Best-effort: the column holds the JSON-serialised
    :class:`~zicato.core.types.HypothesisSpec`, whose ``modulating`` key
    is the proposer's declared set of targeted mutation-point ids. A
    ``NULL`` column, a non-string value, malformed JSON, or a missing /
    non-list ``modulating`` key all degrade to the empty tuple — the
    digest never raises on a single malformed row.
    """
    if not isinstance(raw, str) or not raw:
        return ()
    try:
        decoded = json.loads(raw)
    except (ValueError, TypeError):
        return ()
    if not isinstance(decoded, dict):
        return ()
    mod = decoded.get("modulating")
    if not isinstance(mod, list | tuple):
        return ()
    return tuple(str(m) for m in mod)


def _metric_ranges_for_epoch(db_path: Path, epoch_id: str) -> dict[str, float]:
    """Observed per-metric value range across an epoch, for magnitude buckets.

    Mirrors :func:`zicato.tournament.detail._metric_ranges` (the max absolute
    per-run metric value the index has seen) but goes through this module's
    missing-index-tolerant :func:`_select`, so a never-indexed workspace
    yields an empty mapping rather than raising. The range normalises a
    realised movement into a small/medium/large magnitude bucket when grading
    a hypothesis's prediction accuracy; an empty mapping degrades the bucketer
    to the raw absolute movement, which is fine for the advisory, banded
    experiment-memory signal.
    """
    rows = _select(
        db_path,
        "SELECT mc.name AS name, mc.count AS count FROM metric_counts mc "
        "JOIN runs r ON r.run_id = mc.run_id WHERE r.epoch_id = ?",
        (epoch_id,),
    )
    ranges: dict[str, float] = {}
    for row in rows:
        name = str(row["name"] or "")
        if not name:
            continue
        try:
            value = abs(float(row["count"]))
        except (TypeError, ValueError):
            continue
        ranges[name] = max(ranges.get(name, 0.0), value)
    return ranges


def _prediction_accuracy_for_row(row: sqlite3.Row, ranges: Mapping[str, float]) -> float | None:
    """Score one settled experiment's hypothesis predictions against actuals.

    The advisory hypothesis prediction-accuracy signal of
    FUNCTIONALITY-RECOMMENDATIONS.md §4.2: decode the row's ``hypothesis_json``
    (the proposer's ``expected_*`` movements) and ``outcome_json`` (the
    realised movements) and grade them with the SAME match semantics as
    :func:`zicato.tournament.detail.hypothesis_ledger`, via the shared
    :func:`~zicato.tournament.detail.grade_hypothesis_predictions` core.

    Returns ``matches / predictions`` in ``[0, 1]``, or ``None`` when the
    experiment made no graded predictions or its JSON columns are absent /
    malformed. Best-effort: any decode failure degrades to ``None`` (never
    raises) so a single bad row can't break the digest. This is DIAGNOSTIC
    ONLY — it never gates promotion; the reader folds it into the
    experiment-memory entry as advisory calibration.
    """
    # Lazy import: the index reader stays independent of the (heavier)
    # tournament-detail analytics module unless an actual grade is needed.
    from zicato.tournament.detail import grade_hypothesis_predictions  # noqa: PLC0415

    hypothesis_json = _loads_json_obj(_row_value(row, "hypothesis_json"))
    outcome_json = _loads_json_obj(_row_value(row, "outcome_json"))
    if hypothesis_json is None or outcome_json is None:
        return None
    try:
        matches, predictions = grade_hypothesis_predictions(hypothesis_json, outcome_json, ranges)
    except Exception:  # noqa: BLE001 — advisory diagnostic, never fatal
        return None
    if predictions <= 0:
        return None
    return matches / predictions


def _loads_json_obj(raw: Any) -> dict[str, Any] | None:
    """Decode a JSON-object column to a dict; ``None`` on any failure."""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        decoded = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return decoded if isinstance(decoded, dict) else None


def _row_value(row: sqlite3.Row, key: str) -> Any:
    """Best-effort ``row[key]`` that tolerates a column the SELECT omitted."""
    try:
        return row[key]
    except (IndexError, KeyError):
        return None


def _cross_contract_settled_rows(db_path: Path, epoch_id: str) -> list[sqlite3.Row]:
    """Settled experiments from OTHER epochs sharing ``epoch_id``'s contract.

    The cross-epoch half of the experiment-memory reader
    (EXPERIMENT-MEMORY.md §3.4 / §5.2): joins ``experiments`` to
    ``epochs`` on ``epoch_id`` and keeps rows whose epoch carries the
    SAME non-empty ``contract_hash`` as the current epoch. "Sharing the
    workspace" is structural — the index file is per-workspace, so every
    row here is already workspace-scoped. Excluded by construction:

    * the current epoch's own rows (the same-epoch reader owns those);
    * unsettled rows (``tournament_decision IS NULL`` — no learning
      signal);
    * every epoch under a DIFFERENT hash — its mutation ids and losses
      are not comparable and are never surfaced;
    * legacy / pre-hash epochs (``contract_hash`` empty) — an unknown
      contract is never treated as transferable.

    Ordered ``(epoch_id, generation_id)`` ascending so the caller can walk
    it newest-first with ``reversed`` exactly like the same-epoch rows.
    """
    return _select(
        db_path,
        "SELECT e.epoch_id, e.generation_id, e.hypothesis_core_idea, "
        "e.hypothesis_json, e.tournament_decision, e.rejection_reason, "
        "e.scalar_score_delta, e.outcome_json FROM experiments AS e "
        "JOIN epochs AS ep ON ep.epoch_id = e.epoch_id "
        "WHERE ep.contract_hash != '' "
        "AND ep.contract_hash = ("
        "SELECT contract_hash FROM epochs WHERE epoch_id = ?) "
        "AND e.epoch_id != ? "
        "AND e.tournament_decision IS NOT NULL "
        "ORDER BY e.epoch_id, e.generation_id",
        (epoch_id, epoch_id),
    )


def _cross_contract_entries(db_path: Path, epoch_id: str, budget: int) -> list[PriorExperiment]:
    """Build the capped ``same_contract=False`` tail of the digest.

    Every entry carries ``scalar_score_delta=None`` (a Δscalar measured
    under another epoch does not transfer — §3.4; the renderer would omit
    it anyway, and the restricted-visibility envelope must not depend on
    the renderer) and ``prediction_accuracy=None`` (the calibration band
    is same-epoch diagnostics). Curation mirrors the same-epoch reader in
    miniature: promoted wins first (newest-first), then the most recent
    rejections, then deferred — bounded by ``budget``, the room left
    AFTER every same-epoch entry (same-epoch history always keeps
    priority in the cap, which also realises §3.4's "only when same-epoch
    history is sparse": a busy epoch leaves no budget).
    """
    if budget <= 0:
        return []
    rows = _cross_contract_settled_rows(db_path, epoch_id)
    promoted: list[PriorExperiment] = []
    rejected: list[PriorExperiment] = []
    deferred: list[PriorExperiment] = []
    for row in reversed(rows):
        decision = str(row["tournament_decision"])
        entry = PriorExperiment(
            generation_id=str(row["generation_id"]),
            epoch_id=str(row["epoch_id"]),
            core_idea=str(row["hypothesis_core_idea"] or ""),
            modulating=_modulating_from_hypothesis_json(row["hypothesis_json"]),
            decision=decision,
            rejection_reason=str(row["rejection_reason"] or ""),
            scalar_score_delta=None,
            same_contract=False,
            prediction_accuracy=None,
        )
        if decision == "promoted":
            promoted.append(entry)
        elif decision == "rejected":
            rejected.append(entry)
        elif decision == "deferred":
            deferred.append(entry)
    out = promoted[:budget]
    if len(out) < budget:
        out.extend(rejected[: budget - len(out)])
    if len(out) < budget:
        out.extend(deferred[: budget - len(out)])
    return out


def prior_experiments_for_epoch(
    db_path: Path,
    epoch_id: str,
    *,
    max_entries: int = EXPERIMENT_MEMORY_MAX_ENTRIES,
    cross_epoch: bool = False,
) -> list[PriorExperiment]:
    """Return a curated digest of ``epoch_id``'s SETTLED prior experiments.

    The experiment-memory surface (see
    ``docs/design/EXPERIMENT-MEMORY.md`` §3.3) the orchestrator threads to
    the proposer so it stops re-proposing known failures and builds on
    known wins. Layered over :func:`experiments_for_epoch`:

    * **Skip unsettled rows** (``tournament_decision IS NULL``) — an
      experiment with no verdict carries no learning signal, and would
      otherwise surface the current round's own just-written, outcome-less
      experiment. (In-flight sibling entries come from the orchestrator's
      field loop, not from the index.)
    * Lift each row's ``modulating`` ids out of ``hypothesis_json``
      (empty tuple on any decode failure — never raise).
    * Curate + cap to ``max_entries``: **all** ``promoted`` wins
      (most-recent-first — wins are rare and high-value, never dropped
      while budget remains), then the most-recent ``rejected`` ordered by
      sharpest regression (most-negative ``scalar_score_delta``) first,
      then ``deferred`` if budget remains. Every same-epoch entry is built
      with ``same_contract=True``.
    * **Opt-in cross-epoch transfer** (``cross_epoch=True`` — the
      ``experiment_memory.cross_epoch`` contract knob;
      EXPERIMENT-MEMORY.md §3.4 / §5.2): settled experiments from OTHER
      epochs under the SAME ``contract_hash`` fill whatever budget the
      same-epoch entries left, as clearly-flagged ``same_contract=False``
      entries with ``scalar_score_delta=None`` (the number does not
      transfer). The default (``False``) is byte-identical to the
      same-epoch-only reader.

    Tolerates a missing index the same way every selector here does: a
    never-indexed workspace yields ``[]`` rather than raising
    :class:`IndexNotBuiltError`.
    """
    if max_entries <= 0:
        return []

    rows = experiments_for_epoch(db_path, epoch_id)

    # Hypothesis prediction-accuracy (FUNCTIONALITY-RECOMMENDATIONS.md §4.2):
    # the magnitude buckets need the epoch's per-metric value ranges. Computed
    # ONCE here (best-effort — an empty mapping just degrades the bucketer to
    # the raw absolute movement). DIAGNOSTIC only; never gates promotion.
    ranges = _metric_ranges_for_epoch(db_path, epoch_id)

    promoted: list[PriorExperiment] = []
    rejected: list[PriorExperiment] = []
    deferred: list[PriorExperiment] = []
    # ``experiments_for_epoch`` orders by ``generation_id`` ascending; the
    # digest renders most-recent-first, so we walk the rows in reverse to
    # build each block newest-first.
    for row in reversed(rows):
        decision = row["tournament_decision"]
        if decision is None:
            continue  # unsettled — no learning signal
        delta = row["scalar_score_delta"]
        entry = PriorExperiment(
            generation_id=str(row["generation_id"]),
            epoch_id=str(row["epoch_id"]),
            core_idea=str(row["hypothesis_core_idea"] or ""),
            modulating=_modulating_from_hypothesis_json(row["hypothesis_json"]),
            decision=str(decision),
            rejection_reason=str(row["rejection_reason"] or ""),
            scalar_score_delta=None if delta is None else float(delta),
            same_contract=True,
            prediction_accuracy=_prediction_accuracy_for_row(row, ranges),
        )
        if decision == "promoted":
            promoted.append(entry)
        elif decision == "rejected":
            rejected.append(entry)
        elif decision == "deferred":
            deferred.append(entry)

    out: list[PriorExperiment] = []
    out.extend(promoted[:max_entries])

    # Rejections: take the K MOST RECENT (K = remaining budget — recency
    # gates the window since an old marginal rejection is the weakest
    # "avoid" signal), then within that window rank the sharpest
    # regression (most-negative delta) first so the strongest signals are
    # the most visible. A missing delta sorts as least-sharp (0.0) so a
    # near-zero rejection is the first to fall off.
    rejected_budget = max_entries - len(out)
    if rejected_budget > 0:
        window = rejected[:rejected_budget]
        window.sort(
            key=lambda e: (e.scalar_score_delta if e.scalar_score_delta is not None else 0.0)
        )
        out.extend(window)

    if len(out) < max_entries:
        out.extend(deferred[: max_entries - len(out)])

    # Cross-contract branch (EXPERIMENT-MEMORY.md §3.4 / §5.2): fill ONLY
    # the budget the same-epoch entries left, so same-epoch history always
    # keeps priority in the cap and the knob-off path above stays
    # byte-identical.
    if cross_epoch and len(out) < max_entries:
        out.extend(_cross_contract_entries(db_path, epoch_id, max_entries - len(out)))
    return out[:max_entries]


#: How many of the epoch's most recent settled experiments count as
#: "recent" for the mutation-point track record's recency signal. A simple
#: LAST-K WINDOW was chosen over exponential decay deliberately: the
#: consumer surfaces (the manifest annotation, the proposer tool) only need
#: a coarse recent/stale read, a window is auditable by hand against the
#: journal, and a decay constant would be one more tuning knob with no
#: consumer that reads a continuous weight. K = 10 ≈ the experiment-memory
#: digest cap, so "recent" aligns with the history the proposer already
#: sees.
TRACK_RECORD_RECENT_WINDOW = 10


@dataclass(frozen=True)
class MutationTrackRecord:
    """Per-mutation-point track record over one epoch's SETTLED experiments.

    HONESTY CONSTRAINT (load-bearing): every count and delta here is
    **experiment-level** — it describes *experiments that touched this
    point*, and a multi-patch experiment credits (or blames) EVERY point it
    touched with the whole experiment's outcome. Credit is therefore
    CONFOUNDED for multi-patch experiments (:attr:`confounded_experiments`
    counts them) and nothing in this record is causal. Consumers must label
    it accordingly ("experiments touching this point"), never as the
    point's effect.

    Fields
    ------
    mutation_id:
        The mutation point the record describes.
    experiments_touching:
        How many settled experiments carried at least one patch on this
        point (an experiment with several patches on the SAME point counts
        once).
    confounded_experiments:
        Of those, how many also patched at least one OTHER point — the
        experiments whose outcome cannot be attributed to this point.
    promoted:
        How many of the touching experiments were promoted.
    delta_min / delta_median / delta_max:
        Distribution summary of the touching experiments'
        ``scalar_score_delta`` (experiment-level Δscalar; lower is better —
        a negative delta is an improvement). ``None`` when no touching
        experiment recorded a delta.
    recent_touching / recent_promoted:
        The same touch/promotion counts restricted to the epoch's
        :data:`TRACK_RECORD_RECENT_WINDOW` most recent settled experiments
        — the recency-weighted view (see the constant's docstring for why
        a last-K window rather than exponential decay).
    """

    mutation_id: str
    experiments_touching: int
    confounded_experiments: int
    promoted: int
    delta_min: float | None
    delta_median: float | None
    delta_max: float | None
    recent_touching: int
    recent_promoted: int


def mutation_point_track_record(
    db_path: Path,
    epoch_id: str,
    mutation_id: str | None = None,
    *,
    recent_window: int = TRACK_RECORD_RECENT_WINDOW,
) -> dict[str, MutationTrackRecord]:
    """Fold each mutation point's per-epoch track record out of the index.

    The mutation-point *fertility map*: for every point at least one
    settled experiment patched under ``epoch_id``, how often it was
    touched, how those experiments fared (promotion count, Δscalar
    min/median/max), and the same counts over the epoch's
    ``recent_window`` most recent settled experiments (ordered by the
    touching generation's ``created_at`` then ``generation_id``; see
    :data:`TRACK_RECORD_RECENT_WINDOW` for the documented last-K-window
    choice). ``mutation_id`` narrows the result to that single point
    (an untouched / unknown id yields an empty mapping).

    Scope + honesty:

    * **Settled experiments only** (``tournament_decision`` non-null) — an
      in-flight experiment has no outcome to fold, exactly as the
      experiment-memory reader treats it.
    * **Experiment-level attribution only** — see
      :class:`MutationTrackRecord`. Deltas and decisions belong to whole
      experiments; a multi-patch experiment confounds per-point credit,
      and the record counts exactly how many touching experiments are
      confounded so a consumer can say so.

    Tolerates a missing index the same way every selector here does: a
    never-indexed workspace yields ``{}``.
    """
    # Settled experiments in recency order (oldest first), each with its
    # touched mutation-id set. Two queries + a Python fold keeps the
    # recency ranking (epoch-wide, not per-point) straightforward.
    experiment_rows = _select(
        db_path,
        "SELECT e.generation_id, e.tournament_decision, e.scalar_score_delta "
        "FROM experiments AS e "
        "LEFT JOIN generations AS g "
        "ON g.epoch_id = e.epoch_id AND g.generation_id = e.generation_id "
        "WHERE e.epoch_id = ? AND e.tournament_decision IS NOT NULL "
        "ORDER BY COALESCE(g.created_at, ''), e.generation_id",
        (epoch_id,),
    )
    patch_rows = _select(
        db_path,
        "SELECT DISTINCT generation_id, mutation_id FROM patches WHERE epoch_id = ?",
        (epoch_id,),
    )
    touched_by_generation: dict[str, set[str]] = {}
    for row in patch_rows:
        gid = str(row["generation_id"])
        mid = str(row["mutation_id"] or "")
        if mid:
            touched_by_generation.setdefault(gid, set()).add(mid)

    n_settled = len(experiment_rows)
    per_point: dict[str, dict[str, Any]] = {}
    for rank, row in enumerate(experiment_rows):
        gid = str(row["generation_id"])
        touched = touched_by_generation.get(gid, set())
        if mutation_id is not None:
            touched = touched & {mutation_id}
        if not touched:
            continue
        promoted = str(row["tournament_decision"]) == "promoted"
        raw_delta = row["scalar_score_delta"]
        delta = float(raw_delta) if raw_delta is not None else None
        # The last-K window: the K most recent settled experiments epoch-wide.
        recent = (n_settled - rank) <= recent_window
        confounded = len(touched_by_generation.get(gid, set())) > 1
        for mid in touched:
            acc = per_point.setdefault(
                mid,
                {
                    "touching": 0,
                    "confounded": 0,
                    "promoted": 0,
                    "deltas": [],
                    "recent_touching": 0,
                    "recent_promoted": 0,
                },
            )
            acc["touching"] += 1
            acc["confounded"] += int(confounded)
            acc["promoted"] += int(promoted)
            if delta is not None:
                acc["deltas"].append(delta)
            if recent:
                acc["recent_touching"] += 1
                acc["recent_promoted"] += int(promoted)

    out: dict[str, MutationTrackRecord] = {}
    for mid in sorted(per_point):
        acc = per_point[mid]
        deltas: list[float] = acc["deltas"]
        out[mid] = MutationTrackRecord(
            mutation_id=mid,
            experiments_touching=acc["touching"],
            confounded_experiments=acc["confounded"],
            promoted=acc["promoted"],
            delta_min=min(deltas) if deltas else None,
            delta_median=statistics.median(deltas) if deltas else None,
            delta_max=max(deltas) if deltas else None,
            recent_touching=acc["recent_touching"],
            recent_promoted=acc["recent_promoted"],
        )
    return out


def tournaments_for_epoch(db_path: Path, epoch_id: str) -> list[sqlite3.Row]:
    """Return every resolved tournament row under ``epoch_id``.

    ``champion_eval_mode`` / ``champion_run_ref`` (the v8 per-round
    champion-eval-provenance columns) are selected as optional columns: a
    legacy index opened read-only without the migration still loads each
    row with both fields present-but-null, so a consumer can show
    cached-vs-rerun per round and degrade on a null (treating a null mode
    as ``"full"``).
    """
    return _select_optional_columns(
        db_path,
        "tournaments",
        (
            "tournament_id",
            "epoch_id",
            "parent_generation_id",
            "child_generation_id",
            "decision",
            "parent_scalar",
            "child_scalar",
            "delta_scalar",
            "rejection_reason",
            "ran_at",
        ),
        ("champion_eval_mode", "champion_run_ref"),
        "WHERE epoch_id = ? ORDER BY ran_at, tournament_id",
        (epoch_id,),
    )


def elo_for_epoch(db_path: Path, epoch_id: str) -> list[sqlite3.Row]:
    """Return each generation's folded Elo rating under ``epoch_id``.

    The read side of the Elo analytics fold (``index/elo.py``;
    FUNCTIONALITY-RECOMMENDATIONS.md §5): one row per generation carrying
    ``generation_id``, ``parent_generation_id``, ``elo`` (its folded
    rating across the lineage's settled match ledger), and ``elo_games``
    (how many settled duels contributed to it), oldest first.

    Elo is **read-only / for visibility** — it never gates promotion. The
    ``elo`` / ``elo_games`` columns are v9 additions: a legacy index opened
    read-only without the migration still loads each row with both fields
    present-but-null (``elo IS NULL`` = rating not yet computed; run
    ``zicato reindex`` to derive them). A never-indexed workspace yields
    ``[]``.
    """
    return _select_optional_columns(
        db_path,
        "generations",
        ["epoch_id", "generation_id", "parent_generation_id", "created_at"],
        ["elo", "elo_games"],
        "WHERE epoch_id = ? ORDER BY created_at, generation_id",
        (epoch_id,),
    )


def _select_if_table(
    db_path: Path, table: str, sql: str, params: Sequence[Any] = ()
) -> list[sqlite3.Row]:
    """Run a read query, tolerating BOTH a missing index and a missing table.

    The board-reflection tables land in schema v11; a pre-v11 index opened
    read-only (without a migrating write) simply lacks them. This selector
    probes ``sqlite_master`` for ``table`` first and returns ``[]`` when it is
    absent — so a reflection reader degrades on a stale index rather than
    raising ``no such table`` (the additive-migration back-compat contract the
    other optional-column selectors already honour).
    """
    try:
        conn = open_index(db_path)
    except IndexNotBuiltError:
        return []
    try:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        if exists is None:
            return []
        return list(conn.execute(sql, tuple(params)).fetchall())
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def reflections_for_epoch(db_path: Path, epoch_id: str) -> list[sqlite3.Row]:
    """Return every indexed reflection under ``epoch_id``, newest first.

    The read side of the board-reflection projection (schema v11). Each row
    carries the reflection's four-pillar bill-of-health summary
    (``noise_floor_max_abs_delta`` / ``decision_flip_p`` / ``n_findings`` /
    ``n_judges`` / ``verdict_counts_json``) plus its identity
    (``mode`` / ``executed``). A never-indexed workspace — or one whose index
    predates v11 — yields ``[]`` (the ``reflections`` table is simply absent,
    which :func:`_select` tolerates), and the CLI/readers fall back to the
    canonical files. The index is a projection; a reflection is readable with
    no index at all.
    """
    return _select_if_table(
        db_path,
        "reflections",
        "SELECT reflection_id, epoch_id, created_at, mode, executed, "
        "noise_floor_max_abs_delta, decision_flip_p, n_findings, n_judges, "
        "verdict_counts_json FROM reflections WHERE epoch_id = ? "
        "ORDER BY created_at DESC, reflection_id DESC",
        (epoch_id,),
    )


def reflection_row(db_path: Path, reflection_id: str) -> sqlite3.Row | None:
    """Return one reflection's summary row, or ``None`` when absent.

    ``None`` on a missing index, a pre-v11 index (no ``reflections`` table),
    or an unknown ``reflection_id`` — the reader degrades rather than raises,
    and the caller falls back to the canonical ``plan.json`` / ``findings.json``.
    """
    rows = _select_if_table(
        db_path,
        "reflections",
        "SELECT reflection_id, epoch_id, created_at, mode, executed, "
        "noise_floor_max_abs_delta, decision_flip_p, n_findings, n_judges, "
        "verdict_counts_json FROM reflections WHERE reflection_id = ?",
        (reflection_id,),
    )
    return rows[0] if rows else None


def judge_scorecards_for_reflection(db_path: Path, reflection_id: str) -> list[sqlite3.Row]:
    """Return every judge scorecard row for one reflection, by judge name.

    The per-judge confusion-matrix projection (schema v11). A never-indexed
    or pre-v11 workspace yields ``[]``; the reader falls back to the canonical
    ``scorecards.json`` on disk.
    """
    return _select_if_table(
        db_path,
        "judge_scorecards",
        "SELECT reflection_id, judge_name, tp, fp, fn, tn, ambiguous, "
        "precision, recall, f1, severity_accuracy, disagreement_rate, kappa, "
        "exercised, redundant_with_json FROM judge_scorecards "
        "WHERE reflection_id = ? ORDER BY judge_name",
        (reflection_id,),
    )


def index_counts(db_path: Path) -> dict[str, int]:
    """Return a per-table row-count summary of the index.

    Returns a dict keyed by table name. When the index does not exist
    every count is ``0`` — the helper tolerates a missing database the
    same way the selectors do.
    """
    tables = (
        "epochs",
        "generations",
        "experiments",
        "patches",
        "runs",
        "loss_profiles",
        "metric_counts",
        "tournaments",
        "judge_losses",
    )
    out: dict[str, int] = dict.fromkeys(tables, 0)
    try:
        conn = open_index(db_path)
    except IndexNotBuiltError:
        return out
    try:
        for table in tables:
            row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
            out[table] = int(row[0]) if row is not None else 0
    finally:
        conn.close()
    return out


__all__ = [
    "TRACK_RECORD_RECENT_WINDOW",
    "IndexNotBuiltError",
    "MutationTrackRecord",
    "open_index",
    "index_schema_version",
    "all_epochs",
    "generations_for_epoch",
    "runs_for_generation",
    "loss_profiles_for_generation",
    "runs_for_tournament",
    "loss_profiles_for_tournament",
    "epoch_ancestry",
    "metric_counts_for_run",
    "judge_losses_for_run",
    "judge_losses_for_generation",
    "judge_loss_trend",
    "experiments_for_epoch",
    "mutation_point_track_record",
    "prior_experiments_for_epoch",
    "tournaments_for_epoch",
    "elo_for_epoch",
    "reflections_for_epoch",
    "reflection_row",
    "judge_scorecards_for_reflection",
    "index_counts",
]
