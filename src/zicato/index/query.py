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
from collections.abc import Mapping, Sequence
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
        ["round_index"],
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


def prior_experiments_for_epoch(
    db_path: Path,
    epoch_id: str,
    *,
    max_entries: int = EXPERIMENT_MEMORY_MAX_ENTRIES,
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
      then ``deferred`` if budget remains. Every entry is built with
      ``same_contract=True`` — the reader is same-epoch (= same-contract)
      only.

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
        # extension point: a cross-contract branch (same contract_hash,
        # different epoch — see EXPERIMENT-MEMORY.md §3.4) attaches here,
        # yielding same_contract=False entries with scalar_score_delta=None.
        # Not built this phase.

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
    return out[:max_entries]


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
    "IndexNotBuiltError",
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
    "prior_experiments_for_epoch",
    "tournaments_for_epoch",
    "index_counts",
]
