"""Tournament-detail query layer over the SQLite analytical index.

This module computes the hypothesis ledger, the optimization trajectory, and
the tournament cost. Every function here is a *read* against ``.zicato/index.db``
— the analytical index built by the ingester. None of these functions write.

Design rules encoded here:

* **Pure queries.** Functions open the index, run SQL, and project rows
  into frozen, JSON-serialisable dataclasses. They never mutate the db
  and never run a system-under-test pass.
* **Partial-data tolerance.** A generation may have no resolved outcome,
  a run may be missing its ``loss_profiles`` row, an experiment row may
  carry a ``NULL`` ``outcome_json``. Every function degrades gracefully:
  it returns a record with empty / ``None`` sub-fields rather than
  raising for absent rows. A missing or incompatible database requires
  rebuilding through ``zicato repair index``.
* **Frozen dataclasses out.** All return shapes are
  ``frozen=True, slots=True`` dataclasses (or dicts of JSON-native
  values) so the supervisor / CLI can ``asdict`` and emit them straight
  to JSON.

The SQLite contract (tables ``epochs``, ``generations``, ``experiments``,
``patches``, ``runs``, ``loss_profiles``, ``metric_counts``,
``tournaments``) is owned by the index ingester. This module only reads
it. Connections require the supported schema; nullable values remain unknown.

The lineage model
-----------------
A tournament epoch is a *gauntlet*: ``v0`` is the seed champion, and each
subsequent generation challenges the current champion. A ``promoted``
challenger becomes the new champion and joins the *winners spine*, which
:func:`optimization_trajectory` walks.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Hypothesis-match thresholds
# ---------------------------------------------------------------------------

#: Magnitude-bucket thresholds for :func:`hypothesis_ledger`.
#:
#: A proposer predicts a coarse magnitude bucket (``small`` / ``medium`` /
#: ``large``) for each expected metric movement. To grade the prediction we
#: normalise the *actual* absolute movement by the metric's observed range
#: across the epoch (the max absolute per-generation value seen for that
#: metric; a degenerate zero range falls back to the absolute movement
#: itself). The normalised fraction is then bucketed:
#:
#: * ``small``  — fraction < 0.1
#: * ``medium`` — 0.1 <= fraction <= 0.5
#: * ``large``  — fraction > 0.5
#:
#: A movement *matches* iff BOTH the sign agrees (see
#: :func:`_direction_sign_ok`) AND the actual bucket equals the predicted
#: bucket.
MAGNITUDE_SMALL_MAX = 0.1
MAGNITUDE_LARGE_MIN = 0.5

#: Number of trailing promoted generations :func:`optimization_trajectory`
#: inspects for the plateau flag. If the scalar has not improved (by more
#: than :data:`PLATEAU_EPSILON`) across the last ``PLATEAU_WINDOW`` promoted
#: generations, the trajectory is flagged as plateaued.
PLATEAU_WINDOW = 3

#: Minimum scalar improvement that counts as "not a plateau". Scalar is
#: lower-is-better, so an improvement is a *decrease*.
PLATEAU_EPSILON = 1e-9


class IndexUnavailableError(RuntimeError):
    """Raised when the analytical index database cannot be opened.

    This is the single hard-error path in this module. Everything else
    degrades gracefully on missing rows; a missing *database* is an
    operator-actionable condition.
    """


# ---------------------------------------------------------------------------
# Return dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MovementGrade:
    """The grade for one expected-vs-actual metric movement."""

    metric_name: str
    predicted_direction: str
    predicted_magnitude: str
    actual_from: float | None
    actual_to: float | None
    actual_magnitude: str
    sign_match: bool
    magnitude_match: bool
    matched: bool


@dataclass(frozen=True, slots=True)
class HypothesisGrade:
    """Per-challenger accuracy of the proposer's predictions."""

    generation_id: str
    core_idea: str
    movements: tuple[MovementGrade, ...]
    predictions: int
    matches: int
    accuracy: float


@dataclass(frozen=True, slots=True)
class TrajectoryPoint:
    """Scalar + per-namespace metric values at one promoted generation."""

    generation_id: str
    scalar: float | None
    namespace_values: dict[str, float]


@dataclass(frozen=True, slots=True)
class Trajectory:
    """The optimisation trajectory across the promoted lineage.

    ``plateaued`` and ``plateau_measurable`` are read as a PAIR. ``plateaued``
    is a property of the promoted spine, and a spine shorter than
    :data:`PLATEAU_WINDOW` resolved scalars cannot plateau — so
    ``plateaued=False`` alone conflates "the lineage is still improving" with
    "the lineage is too short to tell". A run where every challenger was
    rejected has a one-node spine and reports ``plateaued=False`` for the
    second reason rather than the first. ``plateau_measurable`` names which of
    the two it is: ``False`` means the flag carries no measurement at all.

    ``challenger_count`` and ``settled_count`` are the same kind of pair.
    ``challenger_count`` counts every non-seed generation the index
    holds, which includes challengers that have applied a snapshot and
    are still racing: the propose-side lineage append records an
    in-flight generation with a null ``promoted`` so it renders as
    racing rather than as a dead branch, and the ingest reads that null
    as ``0``. So a run whose FIRST challenger is mid-tournament reports
    ``challenger_count=1, promoted_count=0`` — indistinguishable, on
    those two numbers alone, from a run that fielded a challenger and
    cut it. ``settled_count`` counts only the challengers a tournament
    has actually decided, which is the denominator any "nothing is
    promoting" reading needs.
    """

    epoch_id: str
    points: tuple[TrajectoryPoint, ...]
    promotion_rate: float
    promoted_count: int
    challenger_count: int
    plateaued: bool
    #: Whether the spine holds enough resolved scalars for ``plateaued``
    #: to mean anything (``>= PLATEAU_WINDOW``). See the class docstring.
    plateau_measurable: bool = False
    #: Challengers a tournament has decided (promoted / rejected /
    #: deferred). See the class docstring — the rest are still racing.
    settled_count: int = 0


# ---------------------------------------------------------------------------
# Index connection
# ---------------------------------------------------------------------------


def _open(db_path: str | Path) -> sqlite3.Connection:
    """Open the supported analytical index through its connection owner."""
    from zicato.index.query import open_index  # noqa: PLC0415

    path = Path(db_path)
    if not path.exists():
        raise IndexUnavailableError(
            f"analytical index not found at {path}; run `zicato repair index` first"
        )
    return open_index(path)


def _query(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
    """Read the supported index; no matching rows yields an empty list."""
    return list(conn.execute(sql, params).fetchall())


def _row_get(row: sqlite3.Row, key: str, default: Any = None) -> Any:
    """Read a declared column, substituting the caller's default only for null."""
    value = row[key]
    return default if value is None else value


def _loads(blob: Any) -> dict[str, Any]:
    """Parse a JSON text/blob column into a dict; ``{}`` on any failure."""
    if blob is None:
        return {}
    if isinstance(blob, dict):
        return blob
    try:
        parsed = json.loads(blob)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _as_float(value: Any) -> float | None:
    """Coerce to float, or ``None`` when the value is missing/invalid."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Generation / lineage helpers
# ---------------------------------------------------------------------------


def _generations(conn: sqlite3.Connection, epoch_id: str) -> list[sqlite3.Row]:
    """Return all generation rows for an epoch.

    Ordered by generation id with a numeric-aware sort on the ``v``-prefix
    convention (``v0``, ``v1``, ..., ``v10``) so the lineage walk is
    deterministic even when SQLite's lexical order would put ``v10``
    before ``v2``.
    """
    rows = _query(
        conn,
        "SELECT * FROM generations WHERE epoch_id = ?",
        (epoch_id,),
    )
    return sorted(rows, key=lambda r: _gen_sort_key(str(_row_get(r, "generation_id", ""))))


def _gen_sort_key(generation_id: str) -> tuple[int, str]:
    """Numeric-aware sort key for ``v``-prefixed generation ids."""
    body = generation_id[1:] if generation_id[:1] == "v" else generation_id
    if body.isdigit():
        return (int(body), generation_id)
    return (1 << 30, generation_id)


def _promoted_spine(conn: sqlite3.Connection, epoch_id: str) -> list[str]:
    """Return the winners spine: v0 then every promoted generation.

    The spine walk follows the ``promoted`` flag in generation order. The
    seed generation (``parent_generation_id`` NULL, conventionally ``v0``)
    always opens the spine even if its ``promoted`` flag is unset, because
    it is the champion every first challenger must beat.
    """
    gens = _generations(conn, epoch_id)
    if not gens:
        return []
    spine: list[str] = []
    seed_id: str | None = None
    for row in gens:
        gid = str(_row_get(row, "generation_id", ""))
        parent = _row_get(row, "parent_generation_id")
        if parent in (None, "", "null") and seed_id is None:
            seed_id = gid
    for row in gens:
        gid = str(_row_get(row, "generation_id", ""))
        promoted = bool(_row_get(row, "promoted", 0))
        if gid == seed_id or promoted:
            spine.append(gid)
    return spine


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _namespace_metric_means(
    conn: sqlite3.Connection, epoch_id: str, generation_id: str
) -> dict[str, float]:
    """Mean metric value per namespace across this generation's runs.

    Reads ``metric_counts`` joined to ``runs`` for the generation. The
    namespace is the colon-prefix of ``metric_counts.namespace`` (or, when
    that column already holds the bare prefix, used verbatim). Returns
    ``{}`` when the index has no metric rows for the generation.
    """
    rows = _query(
        conn,
        """
        SELECT mc.namespace AS namespace, mc.count AS count
        FROM metric_counts mc
        JOIN runs r ON r.run_id = mc.run_id
        WHERE r.epoch_id = ? AND r.generation_id = ?
        """,
        (epoch_id, generation_id),
    )
    if not rows:
        return {}
    sums: dict[str, float] = {}
    counts: dict[str, int] = {}
    for row in rows:
        ns = str(_row_get(row, "namespace", "")).strip()
        if not ns:
            continue
        if not ns.endswith(":"):
            ns = ns if ":" not in ns else ns[: ns.find(":") + 1]
            if not ns.endswith(":"):
                ns = ns + ":"
        value = _as_float(_row_get(row, "count")) or 0.0
        sums[ns] = sums.get(ns, 0.0) + value
        counts[ns] = counts.get(ns, 0) + 1
    return {ns: sums[ns] / counts[ns] for ns in sums if counts[ns]}


def _tournament_row(conn: sqlite3.Connection, epoch_id: str, child_gen: str) -> sqlite3.Row | None:
    """Fetch the ``tournaments`` row for one challenger, or ``None``."""
    rows = _query(
        conn,
        "SELECT * FROM tournaments WHERE epoch_id = ? AND child_generation_id = ?",
        (epoch_id, child_gen),
    )
    return rows[0] if rows else None


def hypothesis_ledger(db_path: str | Path, epoch_id: str) -> list[HypothesisGrade]:
    """Grade every challenger's predictions against the realised outcome.

    For each challenger, read the named predictions from ``hypothesis_json`` and
    join them against the realised movements in ``outcome_json``
    (``metric_movements``).

    Match semantics (see :data:`MAGNITUDE_SMALL_MAX` /
    :data:`MAGNITUDE_LARGE_MIN`):

    * **Sign** — the realised direction must agree with the prediction.
      ``decrease`` requires ``actual_to < actual_from``; ``increase``
      requires ``>``; ``neutral`` requires no movement; the
      ``*_or_neutral`` directions accept their strict direction *or* a
      flat movement.
    * **Magnitude** — the realised absolute movement, normalised by the
      metric's observed range across the epoch, is bucketed: ``small`` if
      the fraction is below 0.1, ``large`` if above 0.5, ``medium`` in
      between. The bucket must equal the prediction's bucket.

    A movement *matches* iff both hold. Per-challenger accuracy is
    ``matches / predictions`` (``0.0`` when the challenger made no
    predictions). The overall proposer calibration rate is the pooled
    ``total matches / total predictions`` across the whole ledger.
    Challengers with a ``NULL`` outcome contribute zero matches but still
    appear in the ledger.
    """
    conn = _open(db_path)
    try:
        experiments = _query(
            conn,
            "SELECT * FROM experiments WHERE epoch_id = ?",
            (epoch_id,),
        )
        ranges = _metric_ranges(conn, epoch_id)
        grades: list[HypothesisGrade] = []
        for exp in experiments:
            gid = str(_row_get(exp, "generation_id", ""))
            core = str(_row_get(exp, "hypothesis_core_idea", ""))
            hjson = _loads(_row_get(exp, "hypothesis_json"))
            ojson = _loads(_row_get(exp, "outcome_json"))
            if not core:
                core = str(hjson.get("core_idea", ""))
            expected = _expected_movements(hjson)
            actual = _actual_movements(ojson) if ojson else {}
            movements = [
                _grade_movement(metric, direction, magnitude, actual.get(metric), ranges)
                for metric, (direction, magnitude) in expected.items()
            ]
            matches = sum(1 for m in movements if m.matched)
            predictions = len(movements)
            accuracy = matches / predictions if predictions else 0.0
            grades.append(
                HypothesisGrade(
                    generation_id=gid,
                    core_idea=core,
                    movements=tuple(movements),
                    predictions=predictions,
                    matches=matches,
                    accuracy=accuracy,
                )
            )
        grades.sort(key=lambda g: _gen_sort_key(g.generation_id))
        return grades
    finally:
        conn.close()


def _expected_movements(hjson: dict[str, Any]) -> dict[str, tuple[str, str]]:
    """Index named predictions by their metric name."""
    return {
        mv["metric_name"]: (mv["direction"], mv["magnitude"])
        for mv in hjson["expected_metric_movements"]
    }


def _actual_movements(ojson: dict[str, Any]) -> dict[str, tuple[float, float]]:
    """Index recorded parent and child values by metric name."""
    return {
        mv["metric_name"]: (mv["from_value"], mv["to_value"]) for mv in ojson["metric_movements"]
    }


def _metric_ranges(conn: sqlite3.Connection, epoch_id: str) -> dict[str, float]:
    """Observed value range per metric across an epoch.

    The range normalises actual movements into magnitude buckets. We take
    the max absolute per-run metric value the index has seen for each
    metric name. A metric absent here gets a zero range; the bucketer then
    falls back to the raw absolute movement.
    """
    rows = _query(
        conn,
        """
        SELECT mc.name AS name, mc.count AS count
        FROM metric_counts mc
        JOIN runs r ON r.run_id = mc.run_id
        WHERE r.epoch_id = ?
        """,
        (epoch_id,),
    )
    ranges: dict[str, float] = {}
    for row in rows:
        name = str(_row_get(row, "name", ""))
        if not name:
            continue
        value = abs(_as_float(_row_get(row, "count")) or 0.0)
        ranges[name] = max(ranges.get(name, 0.0), value)
    return ranges


def _direction_sign_ok(direction: str, delta: float) -> bool:
    """Whether a realised ``delta`` agrees with a predicted direction.

    ``delta`` is ``actual_to - actual_from``. A delta within
    :data:`PLATEAU_EPSILON` of zero counts as flat.
    """
    flat = abs(delta) <= PLATEAU_EPSILON
    if direction == "decrease":
        return delta < -PLATEAU_EPSILON
    if direction == "increase":
        return delta > PLATEAU_EPSILON
    if direction == "neutral":
        return flat
    if direction == "decrease_or_neutral":
        return delta < -PLATEAU_EPSILON or flat
    if direction == "increase_or_neutral":
        return delta > PLATEAU_EPSILON or flat
    return False


def _magnitude_bucket(abs_delta: float, metric_range: float) -> str:
    """Bucket a realised absolute movement into small/medium/large.

    Normalised by ``metric_range``; a zero range falls back to the raw
    absolute delta so a magnitude can still be assigned.
    """
    denom = metric_range if metric_range > 0 else 1.0
    fraction = abs_delta / denom
    if fraction < MAGNITUDE_SMALL_MAX:
        return "small"
    if fraction > MAGNITUDE_LARGE_MIN:
        return "large"
    return "medium"


def _grade_movement(
    metric: str,
    direction: str,
    magnitude: str,
    actual: tuple[float | None, float | None] | None,
    ranges: dict[str, float],
) -> MovementGrade:
    """Grade one expected-vs-actual movement pair."""
    if actual is None:
        # The proposer predicted a movement the outcome never recorded.
        return MovementGrade(
            metric_name=metric,
            predicted_direction=direction,
            predicted_magnitude=magnitude,
            actual_from=None,
            actual_to=None,
            actual_magnitude="",
            sign_match=False,
            magnitude_match=False,
            matched=False,
        )
    from_value, to_value = actual
    if from_value is None or to_value is None:
        return MovementGrade(
            metric_name=metric,
            predicted_direction=direction,
            predicted_magnitude=magnitude,
            actual_from=from_value,
            actual_to=to_value,
            actual_magnitude="",
            sign_match=False,
            magnitude_match=False,
            matched=False,
        )
    delta = to_value - from_value
    sign_ok = _direction_sign_ok(direction, delta)
    actual_bucket = _magnitude_bucket(abs(delta), ranges.get(metric, 0.0))
    magnitude_ok = actual_bucket == magnitude
    return MovementGrade(
        metric_name=metric,
        predicted_direction=direction,
        predicted_magnitude=magnitude,
        actual_from=from_value,
        actual_to=to_value,
        actual_magnitude=actual_bucket,
        sign_match=sign_ok,
        magnitude_match=magnitude_ok,
        matched=sign_ok and magnitude_ok,
    )


def grade_hypothesis_predictions(
    hypothesis_json: Mapping[str, Any],
    outcome_json: Mapping[str, Any],
    ranges: Mapping[str, float] | None = None,
) -> tuple[int, int]:
    """Grade ONE experiment's predictions against its realised outcome.

    The per-experiment core of :func:`hypothesis_ledger`, factored out so a
    caller that already holds the decoded ``hypothesis_json`` /
    ``outcome_json`` of a single experiment (the experiment-memory reader,
    FUNCTIONALITY-RECOMMENDATIONS.md §4.2) can score its prediction accuracy
    without re-opening the index or re-querying the whole epoch.

    Join the named predictions against the realised metric movements in the outcome
    (``metric_movements``). A movement *matches* iff
    both its sign and its (range-normalised) magnitude bucket agree with the
    prediction — identical semantics to :func:`hypothesis_ledger`.

    Returns ``(matches, predictions)``. ``predictions == 0`` when the
    hypothesis made no falsifiable movement claims; the caller decides how to
    treat that (the experiment-memory reader maps it to ``None`` accuracy —
    "made no graded predictions"). ``ranges`` normalises the realised
    magnitude into small/medium/large buckets; an empty / absent mapping
    falls back to the raw absolute movement per :func:`_magnitude_bucket`.
    """
    expected = _expected_movements(dict(hypothesis_json))
    actual = _actual_movements(dict(outcome_json))
    range_map = dict(ranges or {})
    matches = 0
    for metric, (direction, magnitude) in expected.items():
        grade = _grade_movement(metric, direction, magnitude, actual.get(metric), range_map)
        if grade.matched:
            matches += 1
    return matches, len(expected)


def optimization_trajectory(db_path: str | Path, epoch_id: str) -> Trajectory:
    """Scalar + per-namespace metric values across the promoted lineage.

    Walks the winners spine and, for each promoted generation, records its
    combined scalar and per-namespace metric means. Reports:

    * ``promotion_rate`` — promoted challengers / total challengers.
    * ``plateaued`` — ``True`` when the scalar has not improved (by more
      than :data:`PLATEAU_EPSILON`) across the last :data:`PLATEAU_WINDOW`
      promoted generations. A spine shorter than the window cannot
      plateau (returns ``False``).
    * ``plateau_measurable`` — whether that flag rests on a measurement
      at all. A run whose challengers were all rejected has a one-node spine,
      so it reports ``plateaued=False`` for want of data rather than for want
      of a plateau; the pair distinguishes it from an improving lineage.

    Generations whose scalar cannot be resolved contribute a ``None``
    scalar point and are skipped by the plateau check — never raises.
    """
    conn = _open(db_path)
    try:
        spine = _promoted_spine(conn, epoch_id)
        points: list[TrajectoryPoint] = []
        for gid in spine:
            scalar = _resolve_scalar(conn, epoch_id, gid)
            ns_values = _namespace_metric_means(conn, epoch_id, gid)
            points.append(
                TrajectoryPoint(
                    generation_id=gid,
                    scalar=scalar,
                    namespace_values=ns_values,
                )
            )
        gens = _generations(conn, epoch_id)
        seed = spine[0] if spine else None
        challengers = [g for g in gens if str(_row_get(g, "generation_id", "")) != seed]
        challenger_count = len(challengers)
        promoted_count = sum(1 for g in challengers if bool(_row_get(g, "promoted", 0)))
        promotion_rate = promoted_count / challenger_count if challenger_count else 0.0
        plateaued = _is_plateaued(points)
        decided = _decided_generations(conn, epoch_id)
        settled_count = sum(
            1 for g in challengers if str(_row_get(g, "generation_id", "")) in decided
        )
        return Trajectory(
            epoch_id=epoch_id,
            points=tuple(points),
            promotion_rate=promotion_rate,
            promoted_count=promoted_count,
            challenger_count=challenger_count,
            plateaued=plateaued,
            plateau_measurable=_plateau_measurable(points),
            settled_count=settled_count,
        )
    finally:
        conn.close()


def _resolve_scalar(conn: sqlite3.Connection, epoch_id: str, generation_id: str) -> float | None:
    """Read a generation's indexed scalar, including its measurement as a parent."""
    trow = _tournament_row(conn, epoch_id, generation_id)
    if trow is not None:
        scalar = _as_float(_row_get(trow, "child_scalar"))
        if scalar is not None:
            return scalar
    # Seed generation: borrow its scalar from a child's parent_scalar.
    child_rows = _query(
        conn,
        "SELECT parent_scalar FROM tournaments WHERE epoch_id = ? AND parent_generation_id = ?",
        (epoch_id, generation_id),
    )
    for row in child_rows:
        scalar = _as_float(_row_get(row, "parent_scalar"))
        if scalar is not None:
            return scalar
    return None


def _decided_generations(conn: sqlite3.Connection, epoch_id: str) -> set[str]:
    """Generation ids a tournament has actually decided, for this epoch.

    A generation earns a row in ``experiments`` before its tournament
    runs, so presence alone is not settlement — the non-empty decision
    column is, and it stays empty until the round resolves. Both tables
    that record a decision are read, and the union taken: the ingest
    writes them from the same experiment.json, but a ``tournaments`` row
    is written only at settle time, so either one saying "decided" is
    proof, and neither is written for a challenger still racing.
    """
    decided: set[str] = set()
    for sql, id_col in (
        (
            "SELECT generation_id AS gid, tournament_decision AS decision "
            "FROM experiments WHERE epoch_id = ?",
            "gid",
        ),
        (
            "SELECT child_generation_id AS gid, decision FROM tournaments WHERE epoch_id = ?",
            "gid",
        ),
    ):
        for row in _query(conn, sql, (epoch_id,)):
            if str(_row_get(row, "decision", "") or "").strip():
                decided.add(str(_row_get(row, id_col, "")))
    return decided


def _plateau_measurable(points: list[TrajectoryPoint]) -> bool:
    """Whether the spine holds enough resolved scalars to judge a plateau.

    The companion to :func:`_is_plateaued`: below
    :data:`PLATEAU_WINDOW` resolved scalars that function short-circuits
    to ``False``, which reads as the reassuring "not plateaued" but is
    really "no measurement". This says which one it is.
    """
    return sum(1 for p in points if p.scalar is not None) >= PLATEAU_WINDOW


def _is_plateaued(points: list[TrajectoryPoint]) -> bool:
    """Whether the last :data:`PLATEAU_WINDOW` scalars show no improvement.

    Considers only points with a resolved scalar. With fewer than
    ``PLATEAU_WINDOW`` such points, the trajectory cannot plateau —
    ``False`` then means "unmeasurable", which
    :func:`_plateau_measurable` reports alongside it.
    """
    scalars = [p.scalar for p in points if p.scalar is not None]
    if len(scalars) < PLATEAU_WINDOW:
        return False
    window = scalars[-PLATEAU_WINDOW:]
    # No improvement = no strict decrease anywhere in the window.
    for earlier, later in zip(window, window[1:], strict=False):
        if later < earlier - PLATEAU_EPSILON:
            return False
    return True


def tournament_cost(db_path: str | Path, epoch_id: str) -> dict[str, Any]:
    """Wall-clock + run-count cost accounting for an epoch's tournament.

    For every challenger generation, sums the wall-clock runtime
    (``runs.runtime_ms``) and counts runs (total and aborted). Reports a
    per-matchup breakdown plus epoch totals and a ``cost_per_promotion``:
    total runtime divided by the number of promoted challengers (``None``
    when nothing was promoted).

    Returns a JSON-native dict. Tolerates generations with no ``runs``
    rows (zero runtime, zero runs) — never raises.
    """
    conn = _open(db_path)
    try:
        gens = _generations(conn, epoch_id)
        spine = _promoted_spine(conn, epoch_id)
        seed = spine[0] if spine else None
        decisions = {
            str(_row_get(r, "generation_id", "")): str(_row_get(r, "tournament_decision", ""))
            for r in _query(
                conn,
                "SELECT generation_id, tournament_decision FROM experiments WHERE epoch_id = ?",
                (epoch_id,),
            )
        }
        per_matchup: list[dict[str, Any]] = []
        total_runtime = 0
        total_runs = 0
        total_aborted = 0
        promoted = 0
        for row in gens:
            gid = str(_row_get(row, "generation_id", ""))
            if gid == seed:
                continue
            run_rows = _query(
                conn,
                "SELECT runtime_ms, aborted FROM runs WHERE epoch_id = ? AND generation_id = ?",
                (epoch_id, gid),
            )
            runtime = sum(int(_as_float(_row_get(r, "runtime_ms")) or 0) for r in run_rows)
            aborted = sum(1 for r in run_rows if bool(_row_get(r, "aborted", 0)))
            decision = decisions.get(gid, "")
            per_matchup.append(
                {
                    "challenger_generation_id": gid,
                    "decision": decision,
                    "runtime_ms": runtime,
                    "run_count": len(run_rows),
                    "aborted_count": aborted,
                }
            )
            total_runtime += runtime
            total_runs += len(run_rows)
            total_aborted += aborted
            if decision == "promoted":
                promoted += 1
        cost_per_promotion = total_runtime / promoted if promoted else None
        return {
            "epoch_id": epoch_id,
            "per_matchup": per_matchup,
            "total_runtime_ms": total_runtime,
            "total_run_count": total_runs,
            "total_aborted_count": total_aborted,
            "promoted_count": promoted,
            "cost_per_promotion_ms": cost_per_promotion,
        }
    finally:
        conn.close()


__all__ = [
    "IndexUnavailableError",
    "MAGNITUDE_SMALL_MAX",
    "MAGNITUDE_LARGE_MIN",
    "PLATEAU_WINDOW",
    "PLATEAU_EPSILON",
    "MovementGrade",
    "HypothesisGrade",
    "TrajectoryPoint",
    "Trajectory",
    "hypothesis_ledger",
    "grade_hypothesis_predictions",
    "optimization_trajectory",
    "tournament_cost",
]
