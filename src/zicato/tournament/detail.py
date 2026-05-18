"""Tournament-detail query layer over the SQLite analytical index.

This module computes the rich tournament/bracket analytics the supervisor
and CLI render. Every function here is a *read* against ``.zicato/index.db``
— the analytical index built by the ingester. None of these functions write.

Design rules encoded here:

* **Pure queries.** Functions open the index, run SQL, and project rows
  into frozen, JSON-serialisable dataclasses. They never mutate the db
  and never run an inner-harness pass.
* **Partial-data tolerance.** A generation may have no resolved outcome,
  a run may be missing its ``loss_profiles`` row, an experiment row may
  carry a ``NULL`` ``outcome_json``. Every function degrades gracefully:
  it returns a record with empty / ``None`` sub-fields rather than
  raising. The *only* hard error is a missing database file — that is an
  operator-actionable condition (run ``zicato reindex``).
* **Frozen dataclasses out.** All return shapes are
  ``frozen=True, slots=True`` dataclasses (or dicts of JSON-native
  values) so the supervisor / CLI can ``asdict`` and emit them straight
  to JSON.

The SQLite contract (tables ``epochs``, ``generations``, ``experiments``,
``patches``, ``runs``, ``loss_profiles``, ``metric_counts``,
``tournaments``) is owned by the index ingester. This module only reads
it; if a column is absent the row-projection helpers default it.

The bracket model
-----------------
A tournament epoch is a *gauntlet*: ``v0`` is the seed champion, and each
subsequent generation challenges the current champion. A ``promoted``
challenger becomes the new champion (and joins the *winners spine*); a
``rejected`` challenger is discarded but hung under the champion it
failed to beat. :func:`assemble_bracket` reconstructs that structure.
"""

from __future__ import annotations

import json
import sqlite3
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
class Matchup:
    """One champion-vs-challenger contest within a bracket.

    Fields
    ------
    epoch_id:
        The epoch this matchup belongs to.
    champion_generation_id:
        The generation that held the lineage head when the challenger ran.
    challenger_generation_id:
        The challenging generation.
    decision:
        The tournament decision for the challenger: ``"promoted"``,
        ``"rejected"``, ``"deferred"`` or ``""`` when no outcome is
        recorded yet.
    rejection_reason:
        Symbolic rejection reason when ``decision == "rejected"``; empty
        otherwise.
    scalar_score_delta:
        Change in the combined tournament scalar (child minus parent), or
        ``None`` when no outcome is recorded.
    """

    epoch_id: str
    champion_generation_id: str
    challenger_generation_id: str
    decision: str
    rejection_reason: str = ""
    scalar_score_delta: float | None = None


@dataclass(frozen=True, slots=True)
class Bracket:
    """The gauntlet structure for one epoch.

    Fields
    ------
    epoch_id:
        The epoch.
    champion_spine:
        Ordered list of generation ids on the winners spine — the seed
        ``v0`` followed by every promoted challenger, in promotion order.
    matchups:
        Every champion-vs-challenger contest. Promoted matchups advance
        the spine; rejected/deferred matchups hang their challenger under
        the champion it failed to beat.
    """

    epoch_id: str
    champion_spine: tuple[str, ...]
    matchups: tuple[Matchup, ...]


@dataclass(frozen=True, slots=True)
class ScalarComponent:
    """One named contribution to a side's scalar score."""

    name: str
    value: float


@dataclass(frozen=True, slots=True)
class EntryComparison:
    """Board entry x {parent, child} loss comparison.

    Fields
    ------
    entry_id:
        The board entry id.
    parent_drift_loss / child_drift_loss:
        Per-entry drift loss for each side, or ``None`` when that side has
        no loss-profile row for this entry.
    parent_pass_fail / child_pass_fail:
        Per-entry pass/fail for each side, ``None`` when no expectation
        fired (or no row).
    verdict:
        ``"improved"`` (child loss strictly lower), ``"regressed"`` (child
        loss strictly higher), ``"flat"`` (equal, or insufficient data to
        compare).
    """

    entry_id: str
    parent_drift_loss: float | None
    child_drift_loss: float | None
    parent_pass_fail: bool | None
    child_pass_fail: bool | None
    verdict: str


@dataclass(frozen=True, slots=True)
class GateVerdict:
    """The tournament gate's decision and reasoning for one matchup."""

    decision: str
    rejection_reason: str
    scalar_score_delta: float | None
    drift_loss_delta: float | None
    pass_rate_delta: float | None
    reasoning: str


@dataclass(frozen=True, slots=True)
class PatchSummary:
    """A single patch as recorded in the index."""

    patch_id: str
    mutation_id: str
    op: str
    rationale: str


@dataclass(frozen=True, slots=True)
class HypothesisSummary:
    """The challenger's structured hypothesis, flattened for rendering."""

    core_idea: str
    why: str
    modulating: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class MatchupDetail:
    """Full detail for one champion-vs-challenger matchup."""

    epoch_id: str
    champion_generation_id: str
    challenger_generation_id: str
    hypothesis: HypothesisSummary
    patches: tuple[PatchSummary, ...]
    entry_grid: tuple[EntryComparison, ...]
    scalar_breakdown: dict[str, Any]
    gate_verdict: GateVerdict


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
    """The optimisation trajectory across the promoted lineage."""

    epoch_id: str
    points: tuple[TrajectoryPoint, ...]
    promotion_rate: float
    promoted_count: int
    challenger_count: int
    plateaued: bool


@dataclass(frozen=True, slots=True)
class MutationStat:
    """Per-mutation-point win-correlation statistics."""

    mutation_id: str
    times_patched: int
    promoted: int
    rejected: int
    win_rate: float


# ---------------------------------------------------------------------------
# Index connection
# ---------------------------------------------------------------------------


def _try_import_open_index() -> Any:
    """Return ``zicato.index.query.open_index`` if importable, else ``None``.

    The index package is built by a sibling component and may not be
    present (or may lack type stubs) in every environment; the dynamic
    lookup keeps this module's hard dependency surface to stdlib only.
    """
    try:
        import importlib

        module = importlib.import_module("zicato.index.query")
    except Exception:  # noqa: BLE001 — any import failure → fall back.
        return None
    return getattr(module, "open_index", None)


def _open(db_path: str | Path) -> sqlite3.Connection:
    """Open the analytical index.

    Prefers ``zicato.index.query.open_index`` when it is importable at
    runtime (so the index package can layer connection pooling / pragmas
    on top); otherwise opens the SQLite file directly with stdlib.

    Raises :class:`IndexUnavailableError` if the database file does not
    exist — the caller should be pointed at ``zicato reindex``.
    """
    path = Path(db_path)
    if not path.exists():
        raise IndexUnavailableError(
            f"analytical index not found at {path} — run `zicato reindex` "
            f"to build it before querying tournament detail"
        )
    open_index = _try_import_open_index()
    if open_index is None:
        conn = sqlite3.connect(str(path))
    else:
        # zicato.index.query.open_index expects a Path (it calls
        # .exists()); pass the Path object, not its string form.
        conn = open_index(path)
    conn.row_factory = sqlite3.Row
    return conn


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    """Return the set of column names for ``table`` (empty if absent)."""
    try:
        cur = conn.execute(f"PRAGMA table_info({table})")
    except sqlite3.Error:
        return set()
    return {str(row[1]) for row in cur.fetchall()}


def _query(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
    """Run a SELECT, returning ``[]`` on any operational error.

    Tolerates a missing table / column so a partially-populated index
    never crashes a detail query.
    """
    try:
        cur = conn.execute(sql, params)
        return list(cur.fetchall())
    except sqlite3.Error:
        return []


def _row_get(row: sqlite3.Row, key: str, default: Any = None) -> Any:
    """Safe accessor for a :class:`sqlite3.Row` that may lack ``key``."""
    try:
        value = row[key]
    except (IndexError, KeyError):
        return default
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


def assemble_bracket(db_path: str | Path, epoch_id: str) -> Bracket:
    """Reconstruct the gauntlet structure for one epoch.

    The champion spine is the seed generation followed by every promoted
    challenger. Each non-seed generation is a matchup: it challenges the
    champion that held the head when it ran (its ``parent_generation_id``,
    falling back to the most-recent spine member before it). A rejected /
    deferred challenger is hung under that champion; a promoted challenger
    advances the spine.

    Tolerates an epoch with no generations (returns an empty bracket) and
    challengers with no recorded outcome (decision ``""``).
    """
    conn = _open(db_path)
    try:
        gens = _generations(conn, epoch_id)
        spine = _promoted_spine(conn, epoch_id)
        experiments = {
            str(_row_get(r, "generation_id", "")): r
            for r in _query(
                conn,
                "SELECT * FROM experiments WHERE epoch_id = ?",
                (epoch_id,),
            )
        }
        seed = spine[0] if spine else None
        matchups: list[Matchup] = []
        last_champion = seed
        for row in gens:
            gid = str(_row_get(row, "generation_id", ""))
            if gid == seed:
                continue
            parent = _row_get(row, "parent_generation_id")
            champion = str(parent) if parent not in (None, "", "null") else (last_champion or "")
            exp = experiments.get(gid)
            decision = str(_row_get(exp, "tournament_decision", "")) if exp is not None else ""
            rejection = str(_row_get(exp, "rejection_reason", "")) if exp is not None else ""
            delta = _as_float(_row_get(exp, "scalar_score_delta")) if exp is not None else None
            matchups.append(
                Matchup(
                    epoch_id=epoch_id,
                    champion_generation_id=champion,
                    challenger_generation_id=gid,
                    decision=decision,
                    rejection_reason=rejection,
                    scalar_score_delta=delta,
                )
            )
            if bool(_row_get(row, "promoted", 0)):
                last_champion = gid
        return Bracket(
            epoch_id=epoch_id,
            champion_spine=tuple(spine),
            matchups=tuple(matchups),
        )
    finally:
        conn.close()


def assemble_lineage(db_path: str | Path) -> tuple[Bracket, ...]:
    """Walk every epoch and link each epoch's seed to the prior promoted head.

    Returns one :class:`Bracket` per epoch in epoch order. The cross-epoch
    link is expressed implicitly: epoch *N*'s seed generation is the
    continuation of epoch *N-1*'s final spine member. Callers that want
    the explicit edge can pair ``brackets[i].champion_spine[0]`` with
    ``brackets[i - 1].champion_spine[-1]``.

    Tolerates a database with no epochs (returns an empty tuple).
    """
    conn = _open(db_path)
    try:
        epoch_rows = _query(conn, "SELECT * FROM epochs")
        epoch_ids = [str(_row_get(r, "epoch_id", "")) for r in epoch_rows]
        if not epoch_ids:
            # Fall back to whatever epoch ids the generations table knows.
            gen_rows = _query(conn, "SELECT DISTINCT epoch_id FROM generations")
            epoch_ids = sorted({str(_row_get(r, "epoch_id", "")) for r in gen_rows})
    finally:
        conn.close()
    return tuple(assemble_bracket(db_path, eid) for eid in epoch_ids if eid)


def per_entry_grid(
    db_path: str | Path,
    epoch_id: str,
    parent_gen: str,
    child_gen: str,
) -> list[EntryComparison]:
    """Build the per-board-entry A/B comparison grid for one matchup.

    For every board entry that ran under *either* side, projects the
    parent and child ``drift_loss`` + ``pass_fail`` and assigns a verdict:

    * ``"improved"`` — child drift loss strictly below parent's.
    * ``"regressed"`` — child drift loss strictly above parent's.
    * ``"flat"`` — equal losses, or one side is missing a loss-profile
      row so no comparison can be made.

    A missing ``loss_profiles`` row on either side leaves that side's
    fields ``None`` and forces a ``"flat"`` verdict — never raises.
    """
    conn = _open(db_path)
    try:
        parent_rows = _loss_rows_by_entry(conn, epoch_id, parent_gen)
        child_rows = _loss_rows_by_entry(conn, epoch_id, child_gen)
        entry_ids = sorted(set(parent_rows) | set(child_rows))
        grid: list[EntryComparison] = []
        for entry_id in entry_ids:
            p = parent_rows.get(entry_id)
            c = child_rows.get(entry_id)
            p_loss = _as_float(_row_get(p, "drift_loss")) if p is not None else None
            c_loss = _as_float(_row_get(c, "drift_loss")) if c is not None else None
            p_pf = _coerce_pass_fail(_row_get(p, "pass_fail")) if p is not None else None
            c_pf = _coerce_pass_fail(_row_get(c, "pass_fail")) if c is not None else None
            grid.append(
                EntryComparison(
                    entry_id=entry_id,
                    parent_drift_loss=p_loss,
                    child_drift_loss=c_loss,
                    parent_pass_fail=p_pf,
                    child_pass_fail=c_pf,
                    verdict=_entry_verdict(p_loss, c_loss),
                )
            )
        return grid
    finally:
        conn.close()


def _loss_rows_by_entry(
    conn: sqlite3.Connection, epoch_id: str, generation_id: str
) -> dict[str, sqlite3.Row]:
    """Index this generation's ``loss_profiles`` rows by ``entry_id``."""
    rows = _query(
        conn,
        "SELECT * FROM loss_profiles WHERE epoch_id = ? AND generation_id = ?",
        (epoch_id, generation_id),
    )
    return {str(_row_get(r, "entry_id", "")): r for r in rows}


def _coerce_pass_fail(value: Any) -> bool | None:
    """Coerce a SQLite ``pass_fail`` cell to ``bool | None``."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        return bool(value)
    text = str(value).strip().lower()
    if text in ("", "null", "none"):
        return None
    if text in ("1", "true", "pass", "passed", "yes"):
        return True
    if text in ("0", "false", "fail", "failed", "no"):
        return False
    return None


def _entry_verdict(parent_loss: float | None, child_loss: float | None) -> str:
    """Verdict for a per-entry drift-loss comparison (lower is better)."""
    if parent_loss is None or child_loss is None:
        return "flat"
    if child_loss < parent_loss:
        return "improved"
    if child_loss > parent_loss:
        return "regressed"
    return "flat"


def scalar_breakdown(db_path: str | Path, epoch_id: str, generation_id: str) -> dict[str, Any]:
    """Per-namespace contribution to each side's scalar score.

    The challenger ``generation_id`` is compared against its parent
    champion. For each side we surface:

    * ``scalar`` — the combined tournament scalar (from ``outcome_json``'s
      child/parent scalar, falling back to the experiment scalar columns).
    * ``components`` — ``{component_name: contribution}`` parsed from the
      stored ``scalar_components`` block when present.
    * ``namespace_aggregates`` — ``{namespace: weighted_aggregate}`` when
      the outcome recorded it.

    Also derives ``namespace_metric_means`` directly from the index
    ``metric_counts`` / ``loss_profiles`` rows so the scoring math is
    visible even when ``outcome_json`` is sparse.

    Returns a JSON-native dict. A generation with no resolved outcome
    yields a dict whose ``parent`` / ``child`` blocks have ``scalar:
    None`` and empty component maps — never raises.
    """
    conn = _open(db_path)
    try:
        exp = _experiment_row(conn, epoch_id, generation_id)
        parent_gen = _parent_of(conn, epoch_id, generation_id)
        outcome = _loads(_row_get(exp, "outcome_json")) if exp is not None else {}

        child_block = _scalar_side(outcome, "child")
        parent_block = _scalar_side(outcome, "parent")

        # Fall back to the tournaments table for the raw scalars.
        if child_block["scalar"] is None or parent_block["scalar"] is None:
            trow = _tournament_row(conn, epoch_id, generation_id)
            if trow is not None:
                if child_block["scalar"] is None:
                    child_block["scalar"] = _as_float(_row_get(trow, "child_scalar"))
                if parent_block["scalar"] is None:
                    parent_block["scalar"] = _as_float(_row_get(trow, "parent_scalar"))

        return {
            "epoch_id": epoch_id,
            "generation_id": generation_id,
            "parent_generation_id": parent_gen,
            "scalar_score_delta": (
                _as_float(_row_get(exp, "scalar_score_delta")) if exp is not None else None
            ),
            "drift_loss_delta": (
                _as_float(_row_get(exp, "drift_loss_delta")) if exp is not None else None
            ),
            "pass_rate_delta": (
                _as_float(_row_get(exp, "pass_rate_delta")) if exp is not None else None
            ),
            "child": child_block,
            "parent": parent_block,
            "child_namespace_metric_means": _namespace_metric_means(conn, epoch_id, generation_id),
            "parent_namespace_metric_means": (
                _namespace_metric_means(conn, epoch_id, parent_gen)
                if parent_gen is not None
                else {}
            ),
        }
    finally:
        conn.close()


def _scalar_side(outcome: dict[str, Any], side: str) -> dict[str, Any]:
    """Project one side (``"child"`` / ``"parent"``) out of an outcome dict.

    The outcome JSON shape is whatever the ingester stored from the
    tournament runner. We probe several conventional layouts:

    * ``outcome[side]`` is itself a dict with ``scalar`` /
      ``scalar_components`` / ``namespace_aggregates``.
    * ``outcome[f"{side}_scalar"]`` / ``outcome[f"{side}_components"]``.
    * a top-level ``scalar`` (treated as the child's).
    """
    block: dict[str, Any] = {
        "scalar": None,
        "components": {},
        "namespace_aggregates": {},
    }
    nested = outcome.get(side)
    if isinstance(nested, dict):
        block["scalar"] = _as_float(nested.get("scalar"))
        comps = nested.get("scalar_components")
        if isinstance(comps, dict):
            block["components"] = {k: _as_float(v) for k, v in comps.items()}
        ns = nested.get("namespace_aggregates")
        if isinstance(ns, dict):
            block["namespace_aggregates"] = {k: _as_float(v) for k, v in ns.items()}

    if block["scalar"] is None:
        block["scalar"] = _as_float(outcome.get(f"{side}_scalar"))
    if not block["components"]:
        comps = outcome.get(f"{side}_scalar_components") or outcome.get(f"{side}_components")
        if isinstance(comps, dict):
            block["components"] = {k: _as_float(v) for k, v in comps.items()}
    if not block["namespace_aggregates"]:
        ns = outcome.get(f"{side}_namespace_aggregates")
        if isinstance(ns, dict):
            block["namespace_aggregates"] = {k: _as_float(v) for k, v in ns.items()}

    if side == "child" and block["scalar"] is None:
        block["scalar"] = _as_float(outcome.get("scalar"))
    return block


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


def matchup_detail(db_path: str | Path, epoch_id: str, generation_id: str) -> MatchupDetail:
    """Assemble the full detail view for one champion-vs-challenger matchup.

    Combines the challenger's hypothesis, its patches, the per-entry A/B
    grid against its parent champion, the scalar breakdown, and the gate
    verdict. Tolerates a challenger with no outcome (empty hypothesis,
    empty patch list, gate decision ``""``) and a challenger whose parent
    cannot be resolved (grid built against an empty parent).
    """
    conn = _open(db_path)
    try:
        exp = _experiment_row(conn, epoch_id, generation_id)
        parent_gen = _parent_of(conn, epoch_id, generation_id)
        hypothesis = _hypothesis_summary(exp)
        patches = _patch_summaries(conn, epoch_id, generation_id)
        gate = _gate_verdict(exp)
    finally:
        conn.close()

    # The grid + breakdown helpers manage their own connections so this
    # function stays a thin orchestration layer.
    grid = (
        tuple(per_entry_grid(db_path, epoch_id, parent_gen, generation_id))
        if parent_gen is not None
        else tuple(per_entry_grid(db_path, epoch_id, "", generation_id))
    )
    breakdown = scalar_breakdown(db_path, epoch_id, generation_id)

    return MatchupDetail(
        epoch_id=epoch_id,
        champion_generation_id=parent_gen or "",
        challenger_generation_id=generation_id,
        hypothesis=hypothesis,
        patches=patches,
        entry_grid=grid,
        scalar_breakdown=breakdown,
        gate_verdict=gate,
    )


def _experiment_row(
    conn: sqlite3.Connection, epoch_id: str, generation_id: str
) -> sqlite3.Row | None:
    """Fetch the ``experiments`` row for one challenger, or ``None``."""
    rows = _query(
        conn,
        "SELECT * FROM experiments WHERE epoch_id = ? AND generation_id = ?",
        (epoch_id, generation_id),
    )
    return rows[0] if rows else None


def _tournament_row(conn: sqlite3.Connection, epoch_id: str, child_gen: str) -> sqlite3.Row | None:
    """Fetch the ``tournaments`` row for one challenger, or ``None``."""
    rows = _query(
        conn,
        "SELECT * FROM tournaments WHERE epoch_id = ? AND child_generation_id = ?",
        (epoch_id, child_gen),
    )
    return rows[0] if rows else None


def _parent_of(conn: sqlite3.Connection, epoch_id: str, generation_id: str) -> str | None:
    """Resolve a generation's parent (champion) id, or ``None``."""
    rows = _query(
        conn,
        "SELECT parent_generation_id FROM generations WHERE epoch_id = ? AND generation_id = ?",
        (epoch_id, generation_id),
    )
    if rows:
        parent = _row_get(rows[0], "parent_generation_id")
        if parent not in (None, "", "null"):
            return str(parent)
    # Fall back to the tournaments table when the generations row is thin.
    trow = _tournament_row(conn, epoch_id, generation_id)
    if trow is not None:
        parent = _row_get(trow, "parent_generation_id")
        if parent not in (None, "", "null"):
            return str(parent)
    return None


def _hypothesis_summary(exp: sqlite3.Row | None) -> HypothesisSummary:
    """Project an experiment row's hypothesis fields into a summary."""
    if exp is None:
        return HypothesisSummary(core_idea="", why="", modulating=())
    core = str(_row_get(exp, "hypothesis_core_idea", ""))
    why = str(_row_get(exp, "hypothesis_why", ""))
    hjson = _loads(_row_get(exp, "hypothesis_json"))
    raw_mod = hjson.get("modulating")
    modulating: tuple[str, ...]
    if isinstance(raw_mod, list | tuple):
        modulating = tuple(str(m) for m in raw_mod)
    else:
        modulating = ()
    # Prefer the JSON's own core_idea/why when the flat columns are blank.
    if not core:
        core = str(hjson.get("core_idea", ""))
    if not why:
        why = str(hjson.get("why", ""))
    return HypothesisSummary(core_idea=core, why=why, modulating=modulating)


def _patch_summaries(
    conn: sqlite3.Connection, epoch_id: str, generation_id: str
) -> tuple[PatchSummary, ...]:
    """Project this generation's ``patches`` rows into summaries."""
    rows = _query(
        conn,
        "SELECT * FROM patches WHERE epoch_id = ? AND generation_id = ?",
        (epoch_id, generation_id),
    )
    return tuple(
        PatchSummary(
            patch_id=str(_row_get(r, "patch_id", "")),
            mutation_id=str(_row_get(r, "mutation_id", "")),
            op=str(_row_get(r, "op", "")),
            rationale=str(_row_get(r, "rationale", "")),
        )
        for r in rows
    )


def _gate_verdict(exp: sqlite3.Row | None) -> GateVerdict:
    """Build the gate verdict + reasoning string from an experiment row."""
    if exp is None:
        return GateVerdict(
            decision="",
            rejection_reason="",
            scalar_score_delta=None,
            drift_loss_delta=None,
            pass_rate_delta=None,
            reasoning="no experiment outcome recorded for this generation",
        )
    decision = str(_row_get(exp, "tournament_decision", ""))
    rejection = str(_row_get(exp, "rejection_reason", ""))
    scalar_delta = _as_float(_row_get(exp, "scalar_score_delta"))
    drift_delta = _as_float(_row_get(exp, "drift_loss_delta"))
    pass_delta = _as_float(_row_get(exp, "pass_rate_delta"))
    reasoning = _gate_reasoning(decision, rejection, scalar_delta, drift_delta, pass_delta)
    return GateVerdict(
        decision=decision,
        rejection_reason=rejection,
        scalar_score_delta=scalar_delta,
        drift_loss_delta=drift_delta,
        pass_rate_delta=pass_delta,
        reasoning=reasoning,
    )


def _gate_reasoning(
    decision: str,
    rejection: str,
    scalar_delta: float | None,
    drift_delta: float | None,
    pass_delta: float | None,
) -> str:
    """Render a human-readable explanation of a gate decision."""
    if not decision:
        return "no tournament decision recorded yet"
    parts: list[str] = []
    if scalar_delta is not None:
        verb = "improved" if scalar_delta < 0 else "regressed" if scalar_delta > 0 else "flat"
        parts.append(f"scalar {verb} by {abs(scalar_delta):.4g}")
    if pass_delta is not None and pass_delta != 0:
        parts.append(f"pass rate moved {pass_delta:+.4g}")
    if drift_delta is not None and drift_delta != 0:
        parts.append(f"drift loss moved {drift_delta:+.4g}")
    detail = "; ".join(parts) if parts else "no metric deltas recorded"
    if decision == "promoted":
        return f"promoted — {detail}"
    if decision == "rejected":
        reason = rejection or "scoring gate"
        return f"rejected ({reason}) — {detail}"
    return f"{decision} — {detail}"


def hypothesis_ledger(db_path: str | Path, epoch_id: str) -> list[HypothesisGrade]:
    """Grade every challenger's predictions against the realised outcome.

    For each challenger we read the proposer's ``expected_metric_movements``
    (preferred) or ``expected_drift_movements`` from ``hypothesis_json`` and
    join them against the realised movements in ``outcome_json``
    (``metric_movements`` / ``drift_movements``).

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
    ``total matches / total predictions`` across the whole ledger — call
    :func:`proposer_calibration_rate` on the returned list to get it.
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
            actual = _actual_movements(ojson)
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


def proposer_calibration_rate(grades: list[HypothesisGrade]) -> float:
    """Overall proposer calibration: total matches / total predictions.

    Pooled across every challenger so a proposer that made one prediction
    on a hard generation does not weigh the same as one that made ten on
    an easy one. Returns ``0.0`` when no predictions were made anywhere.
    """
    total_pred = sum(g.predictions for g in grades)
    total_match = sum(g.matches for g in grades)
    return total_match / total_pred if total_pred else 0.0


def _expected_movements(hjson: dict[str, Any]) -> dict[str, tuple[str, str]]:
    """Extract ``{metric_name: (direction, magnitude)}`` from a hypothesis.

    Prefers ``expected_metric_movements`` (namespaced) and falls back to
    ``expected_drift_movements`` (drift kind, lifted into the ``drift:``
    namespace) so older journal rows still grade.
    """
    out: dict[str, tuple[str, str]] = {}
    for mv in hjson.get("expected_metric_movements", []) or []:
        if not isinstance(mv, dict):
            continue
        name = str(mv.get("metric_name", ""))
        if name:
            out[name] = (str(mv.get("direction", "")), str(mv.get("magnitude", "")))
    for mv in hjson.get("expected_drift_movements", []) or []:
        if not isinstance(mv, dict):
            continue
        kind = str(mv.get("kind", ""))
        if not kind:
            continue
        name = kind if ":" in kind else f"drift:{kind}"
        out.setdefault(name, (str(mv.get("direction", "")), str(mv.get("magnitude", ""))))
    return out


def _actual_movements(ojson: dict[str, Any]) -> dict[str, tuple[float | None, float | None]]:
    """Extract ``{metric_name: (from_value, to_value)}`` from an outcome."""
    out: dict[str, tuple[float | None, float | None]] = {}
    for mv in ojson.get("metric_movements", []) or []:
        if not isinstance(mv, dict):
            continue
        name = str(mv.get("metric_name", ""))
        if name:
            out[name] = (_as_float(mv.get("from_value")), _as_float(mv.get("to_value")))
    for mv in ojson.get("drift_movements", []) or []:
        if not isinstance(mv, dict):
            continue
        kind = str(mv.get("kind", ""))
        if not kind:
            continue
        name = kind if ":" in kind else f"drift:{kind}"
        out.setdefault(name, (_as_float(mv.get("from_rate")), _as_float(mv.get("to_rate"))))
    return out


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


def optimization_trajectory(db_path: str | Path, epoch_id: str) -> Trajectory:
    """Scalar + per-namespace metric values across the promoted lineage.

    Walks the winners spine and, for each promoted generation, records its
    combined scalar and per-namespace metric means. Reports:

    * ``promotion_rate`` — promoted challengers / total challengers.
    * ``plateaued`` — ``True`` when the scalar has not improved (by more
      than :data:`PLATEAU_EPSILON`) across the last :data:`PLATEAU_WINDOW`
      promoted generations. A spine shorter than the window cannot
      plateau (returns ``False``).

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
        return Trajectory(
            epoch_id=epoch_id,
            points=tuple(points),
            promotion_rate=promotion_rate,
            promoted_count=promoted_count,
            challenger_count=challenger_count,
            plateaued=plateaued,
        )
    finally:
        conn.close()


def _resolve_scalar(conn: sqlite3.Connection, epoch_id: str, generation_id: str) -> float | None:
    """Resolve one generation's combined scalar from the index.

    Probes, in order: the ``tournaments`` row's ``child_scalar``, the
    ``experiments`` ``outcome_json`` child scalar, and (for the seed,
    which never has a tournament row) the next generation's
    ``parent_scalar``.
    """
    trow = _tournament_row(conn, epoch_id, generation_id)
    if trow is not None:
        scalar = _as_float(_row_get(trow, "child_scalar"))
        if scalar is not None:
            return scalar
    exp = _experiment_row(conn, epoch_id, generation_id)
    if exp is not None:
        outcome = _loads(_row_get(exp, "outcome_json"))
        side = _scalar_side(outcome, "child")
        if side["scalar"] is not None:
            return float(side["scalar"])
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


def _is_plateaued(points: list[TrajectoryPoint]) -> bool:
    """Whether the last :data:`PLATEAU_WINDOW` scalars show no improvement.

    Considers only points with a resolved scalar. With fewer than
    ``PLATEAU_WINDOW`` such points, the trajectory cannot plateau.
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


def mutation_heat_map(db_path: str | Path, epoch_id: str) -> list[MutationStat]:
    """Per-mutation-point win-correlation statistics for an epoch.

    For every ``mutation_id`` patched during the epoch, counts how often a
    challenger that patched it was promoted vs rejected. The ``win_rate``
    is ``promoted / (promoted + rejected)`` over *resolved* challengers
    (deferred / unresolved challengers count toward ``times_patched`` but
    not the win-rate denominator).

    Returns one :class:`MutationStat` per mutation id, sorted by
    descending ``win_rate`` then descending ``times_patched`` so the
    hottest, most-successful mutation points surface first. Tolerates
    challengers with no recorded outcome.
    """
    conn = _open(db_path)
    try:
        patch_rows = _query(
            conn,
            "SELECT mutation_id, generation_id FROM patches WHERE epoch_id = ?",
            (epoch_id,),
        )
        decisions = {
            str(_row_get(r, "generation_id", "")): str(_row_get(r, "tournament_decision", ""))
            for r in _query(
                conn,
                "SELECT generation_id, tournament_decision FROM experiments WHERE epoch_id = ?",
                (epoch_id,),
            )
        }
        # A mutation id may be patched more than once by the same
        # generation; the heat map counts the generation once per
        # mutation id so a multi-patch generation does not double-weight.
        per_mutation: dict[str, set[str]] = {}
        for row in patch_rows:
            mid = str(_row_get(row, "mutation_id", ""))
            gid = str(_row_get(row, "generation_id", ""))
            if not mid:
                continue
            per_mutation.setdefault(mid, set()).add(gid)

        stats: list[MutationStat] = []
        for mid, gids in per_mutation.items():
            promoted = sum(1 for g in gids if decisions.get(g, "") == "promoted")
            rejected = sum(1 for g in gids if decisions.get(g, "") == "rejected")
            resolved = promoted + rejected
            win_rate = promoted / resolved if resolved else 0.0
            stats.append(
                MutationStat(
                    mutation_id=mid,
                    times_patched=len(gids),
                    promoted=promoted,
                    rejected=rejected,
                    win_rate=win_rate,
                )
            )
        stats.sort(key=lambda s: (-s.win_rate, -s.times_patched, s.mutation_id))
        return stats
    finally:
        conn.close()


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
    "Matchup",
    "Bracket",
    "ScalarComponent",
    "EntryComparison",
    "GateVerdict",
    "PatchSummary",
    "HypothesisSummary",
    "MatchupDetail",
    "MovementGrade",
    "HypothesisGrade",
    "TrajectoryPoint",
    "Trajectory",
    "MutationStat",
    "assemble_bracket",
    "assemble_lineage",
    "matchup_detail",
    "per_entry_grid",
    "scalar_breakdown",
    "hypothesis_ledger",
    "proposer_calibration_rate",
    "optimization_trajectory",
    "mutation_heat_map",
    "tournament_cost",
]
