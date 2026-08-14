"""Tests for ``zicato.tournament.detail`` — the tournament-detail query layer.

These tests seed a synthetic SQLite analytical index directly (CREATE
TABLE + INSERT) so they do not depend on the index ingester. The seeded
epoch is a four-generation gauntlet:

* ``v0`` — seed champion.
* ``v1`` — rejected challenger (failed to beat ``v0``).
* ``v2`` — promoted challenger (new champion).
* ``v3`` — rejected challenger (failed to beat ``v2``).
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from pathlib import Path

import pytest

from zicato.tournament.detail import (
    IndexUnavailableError,
    assemble_bracket,
    assemble_lineage,
    hypothesis_ledger,
    matchup_detail,
    mutation_heat_map,
    optimization_trajectory,
    per_entry_grid,
    proposer_calibration_rate,
    scalar_breakdown,
    tournament_cost,
)

EPOCH = "2026-05_e0"


# ---------------------------------------------------------------------------
# Synthetic index fixture
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE epochs (epoch_id TEXT, name TEXT, created_at TEXT);
CREATE TABLE generations (
    epoch_id TEXT, generation_id TEXT, parent_generation_id TEXT, promoted INTEGER
);
CREATE TABLE experiments (
    epoch_id TEXT, generation_id TEXT,
    hypothesis_core_idea TEXT, hypothesis_why TEXT, hypothesis_json TEXT,
    tournament_decision TEXT, rejection_reason TEXT,
    scalar_score_delta REAL, drift_loss_delta REAL, pass_rate_delta REAL,
    outcome_json TEXT
);
CREATE TABLE patches (
    patch_id TEXT, epoch_id TEXT, generation_id TEXT,
    mutation_id TEXT, op TEXT, rationale TEXT
);
CREATE TABLE runs (
    run_id TEXT, epoch_id TEXT, generation_id TEXT, entry_id TEXT,
    runtime_ms INTEGER, aborted INTEGER
);
CREATE TABLE loss_profiles (
    run_id TEXT, epoch_id TEXT, generation_id TEXT, entry_id TEXT,
    drift_loss REAL, pass_fail INTEGER, loss_json TEXT
);
CREATE TABLE metric_counts (
    run_id TEXT, namespace TEXT, name TEXT, severity TEXT, count REAL
);
CREATE TABLE tournaments (
    tournament_id TEXT, epoch_id TEXT,
    parent_generation_id TEXT, child_generation_id TEXT,
    decision TEXT, parent_scalar REAL, child_scalar REAL, delta_scalar REAL,
    rejection_reason TEXT, ran_at TEXT
);
"""


def _hypothesis(core: str, movements: list[dict]) -> str:
    return json.dumps(
        {
            "core_idea": core,
            "why": "pattern-driven",
            "modulating": ["mp_a"],
            "expected_metric_movements": movements,
        }
    )


def _outcome(decision: str, movements: list[dict], child_scalar: float) -> str:
    return json.dumps(
        {
            "tournament_decision": decision,
            "metric_movements": movements,
            "child": {
                "scalar": child_scalar,
                "scalar_components": {"drift": child_scalar * 0.6, "pass": child_scalar * 0.4},
                "namespace_aggregates": {"drift:": child_scalar * 0.6},
            },
        }
    )


def _seed_index(path: Path) -> None:
    """Create and populate the synthetic four-generation index."""
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(_SCHEMA)
        conn.execute(
            "INSERT INTO epochs VALUES (?, ?, ?)", (EPOCH, "e0", "2026-05-01T00:00:00+00:00")
        )
        # generations: v0 seed, v1 rejected, v2 promoted, v3 rejected
        conn.executemany(
            "INSERT INTO generations VALUES (?, ?, ?, ?)",
            [
                (EPOCH, "v0", None, 1),
                (EPOCH, "v1", "v0", 0),
                (EPOCH, "v2", "v0", 1),
                (EPOCH, "v3", "v2", 0),
            ],
        )
        # experiments — v1/v2/v3 are challengers; v1 makes a hand-built
        # prediction we grade in test_hypothesis_ledger.
        conn.execute(
            "INSERT INTO experiments VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                EPOCH,
                "v1",
                "tighten the router prompt",
                "router was over-eager",
                _hypothesis(
                    "tighten the router prompt",
                    [
                        # predicted decrease, medium — actual goes 4.0->3.0,
                        # |delta|=1.0, range=8.0 -> fraction 0.125 -> medium. MATCH.
                        {
                            "metric_name": "drift:off_topic",
                            "direction": "decrease",
                            "magnitude": "medium",
                        },
                        # predicted increase but actual decreases -> sign fails.
                        {
                            "metric_name": "cost:tokens_spent",
                            "direction": "increase",
                            "magnitude": "small",
                        },
                    ],
                ),
                "rejected",
                "scalar_regression",
                0.30,  # scalar got worse
                0.10,
                -0.05,
                _outcome(
                    "rejected",
                    [
                        {"metric_name": "drift:off_topic", "from_value": 4.0, "to_value": 3.0},
                        {
                            "metric_name": "cost:tokens_spent",
                            "from_value": 1000.0,
                            "to_value": 900.0,
                        },
                    ],
                    child_scalar=1.30,
                ),
            ),
        )
        conn.execute(
            "INSERT INTO experiments VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                EPOCH,
                "v2",
                "add a planning step",
                "agent skipped planning",
                _hypothesis(
                    "add a planning step",
                    [
                        {
                            "metric_name": "drift:off_topic",
                            "direction": "decrease",
                            "magnitude": "large",
                        }
                    ],
                ),
                "promoted",
                "",
                -0.40,
                -0.20,
                0.15,
                _outcome(
                    "promoted",
                    [{"metric_name": "drift:off_topic", "from_value": 4.0, "to_value": 0.0}],
                    child_scalar=0.60,
                ),
            ),
        )
        # v3 — rejected, NULL outcome_json (partial-data case).
        conn.execute(
            "INSERT INTO experiments VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                EPOCH,
                "v3",
                "experimental rewrite",
                "speculative",
                _hypothesis("experimental rewrite", []),
                "rejected",
                "regression_suite_failed",
                None,
                None,
                None,
                None,  # NULL outcome
            ),
        )
        # patches — mp_a patched by v1+v2, mp_b by v3 only.
        conn.executemany(
            "INSERT INTO patches VALUES (?,?,?,?,?,?)",
            [
                ("p1", EPOCH, "v1", "mp_a", "replace", "tighten"),
                ("p2", EPOCH, "v2", "mp_a", "replace", "add planning"),
                ("p3", EPOCH, "v3", "mp_b", "replace", "rewrite"),
            ],
        )
        # runs — two board entries (e1, e2) per challenger generation.
        conn.executemany(
            "INSERT INTO runs VALUES (?,?,?,?,?,?)",
            [
                ("r_v0_e1", EPOCH, "v0", "e1", 1000, 0),
                ("r_v0_e2", EPOCH, "v0", "e2", 1200, 0),
                ("r_v1_e1", EPOCH, "v1", "e1", 1100, 0),
                ("r_v1_e2", EPOCH, "v1", "e2", 1300, 0),
                ("r_v2_e1", EPOCH, "v2", "e1", 900, 0),
                ("r_v2_e2", EPOCH, "v2", "e2", 950, 0),
                ("r_v3_e1", EPOCH, "v3", "e1", 2000, 1),
            ],
        )
        # loss_profiles — v0 baseline, v2 improves e1 / regresses e2.
        conn.executemany(
            "INSERT INTO loss_profiles VALUES (?,?,?,?,?,?,?)",
            [
                ("r_v0_e1", EPOCH, "v0", "e1", 2.0, 1, "{}"),
                ("r_v0_e2", EPOCH, "v0", "e2", 1.0, 1, "{}"),
                ("r_v2_e1", EPOCH, "v2", "e1", 0.5, 1, "{}"),  # improved
                ("r_v2_e2", EPOCH, "v2", "e2", 3.0, 0, "{}"),  # regressed
                # e2 for v2 also gives us a flat entry below via v3 missing.
            ],
        )
        # metric_counts — give a range so magnitude bucketing has a denom.
        conn.executemany(
            "INSERT INTO metric_counts VALUES (?,?,?,?,?)",
            [
                ("r_v0_e1", "drift:", "drift:off_topic", "info", 4.0),
                ("r_v0_e2", "drift:", "drift:off_topic", "info", 8.0),
                ("r_v2_e1", "drift:", "drift:off_topic", "info", 0.0),
                ("r_v1_e1", "cost:", "cost:tokens_spent", "", 1000.0),
            ],
        )
        # tournaments — v1 vs v0, v2 vs v0, v3 vs v2.
        conn.executemany(
            "INSERT INTO tournaments VALUES (?,?,?,?,?,?,?,?,?,?)",
            [
                ("t1", EPOCH, "v0", "v1", "rejected", 1.0, 1.30, 0.30, "scalar_regression", "x"),
                ("t2", EPOCH, "v0", "v2", "promoted", 1.0, 0.60, -0.40, "", "x"),
                ("t3", EPOCH, "v2", "v3", "rejected", 0.60, 0.60, 0.0, "regression_suite", "x"),
            ],
        )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture()
def index_db(tmp_path: Path) -> Path:
    """A populated synthetic analytical index."""
    path = tmp_path / "index.db"
    _seed_index(path)
    return path


# ---------------------------------------------------------------------------
# assemble_bracket / assemble_lineage
# ---------------------------------------------------------------------------


def test_assemble_bracket_spine_and_challengers(index_db: Path) -> None:
    """The champion spine is v0 -> v2; v1 and v3 are discarded challengers."""
    bracket = assemble_bracket(index_db, EPOCH)
    assert bracket.epoch_id == EPOCH
    assert bracket.champion_spine == ("v0", "v2")

    matchups = {m.challenger_generation_id: m for m in bracket.matchups}
    assert set(matchups) == {"v1", "v2", "v3"}

    # v1 challenged v0 and lost.
    assert matchups["v1"].champion_generation_id == "v0"
    assert matchups["v1"].decision == "rejected"
    assert matchups["v1"].rejection_reason == "scalar_regression"

    # v2 challenged v0 and won.
    assert matchups["v2"].champion_generation_id == "v0"
    assert matchups["v2"].decision == "promoted"

    # v3 challenged the new champion v2 and lost.
    assert matchups["v3"].champion_generation_id == "v2"
    assert matchups["v3"].decision == "rejected"

    # JSON-serialisable.
    assert json.dumps(asdict(bracket))


def test_assemble_lineage_links_epochs(index_db: Path) -> None:
    """assemble_lineage returns one bracket per epoch."""
    lineage = assemble_lineage(index_db)
    assert len(lineage) == 1
    assert lineage[0].epoch_id == EPOCH
    assert lineage[0].champion_spine == ("v0", "v2")


def test_assemble_bracket_missing_db_raises() -> None:
    """A missing database raises a clear, operator-actionable error."""
    with pytest.raises(IndexUnavailableError, match="zicato repair index"):
        assemble_bracket("/nonexistent/path/index.db", EPOCH)


# ---------------------------------------------------------------------------
# per_entry_grid
# ---------------------------------------------------------------------------


def test_per_entry_grid_verdicts(index_db: Path) -> None:
    """Per-entry grid classifies improved / regressed / flat correctly."""
    grid = {ec.entry_id: ec for ec in per_entry_grid(index_db, EPOCH, "v0", "v2")}
    assert set(grid) == {"e1", "e2"}

    # e1: v0=2.0 -> v2=0.5 -> improved.
    assert grid["e1"].parent_drift_loss == 2.0
    assert grid["e1"].child_drift_loss == 0.5
    assert grid["e1"].verdict == "improved"

    # e2: v0=1.0 -> v2=3.0 -> regressed.
    assert grid["e2"].parent_drift_loss == 1.0
    assert grid["e2"].child_drift_loss == 3.0
    assert grid["e2"].verdict == "regressed"


def test_per_entry_grid_missing_side_is_flat(index_db: Path) -> None:
    """A challenger with no loss rows yields flat verdicts, never raises."""
    # v3 has no loss_profiles rows -> every entry flat with None child loss.
    grid = per_entry_grid(index_db, EPOCH, "v0", "v3")
    assert grid  # entries from v0's side still appear
    for ec in grid:
        assert ec.child_drift_loss is None
        assert ec.verdict == "flat"


# ---------------------------------------------------------------------------
# matchup_detail
# ---------------------------------------------------------------------------


def test_matchup_detail_full(index_db: Path) -> None:
    """matchup_detail assembles hypothesis, patches, grid, gate verdict."""
    detail = matchup_detail(index_db, EPOCH, "v2")
    assert detail.challenger_generation_id == "v2"
    assert detail.champion_generation_id == "v0"
    assert detail.hypothesis.core_idea == "add a planning step"
    assert detail.hypothesis.modulating == ("mp_a",)
    assert [p.mutation_id for p in detail.patches] == ["mp_a"]
    assert detail.gate_verdict.decision == "promoted"
    assert "promoted" in detail.gate_verdict.reasoning
    assert {ec.entry_id for ec in detail.entry_grid} == {"e1", "e2"}
    assert json.dumps(asdict(detail))


def test_matchup_detail_null_outcome_graceful(index_db: Path) -> None:
    """A generation with a NULL outcome degrades, never raises."""
    detail = matchup_detail(index_db, EPOCH, "v3")
    assert detail.challenger_generation_id == "v3"
    assert detail.gate_verdict.decision == "rejected"
    # outcome_json was NULL -> child scalar falls back to tournaments row.
    assert detail.scalar_breakdown["child"]["scalar"] == 0.60


# ---------------------------------------------------------------------------
# scalar_breakdown
# ---------------------------------------------------------------------------


def test_scalar_breakdown_components(index_db: Path) -> None:
    """scalar_breakdown surfaces per-side scalar + components."""
    breakdown = scalar_breakdown(index_db, EPOCH, "v2")
    assert breakdown["child"]["scalar"] == 0.60
    assert breakdown["parent"]["scalar"] == 1.0  # from tournaments parent_scalar
    assert "drift" in breakdown["child"]["components"]
    assert breakdown["scalar_score_delta"] == -0.40
    assert json.dumps(breakdown)


# ---------------------------------------------------------------------------
# hypothesis_ledger — match semantics
# ---------------------------------------------------------------------------


def test_hypothesis_ledger_match_semantics(index_db: Path) -> None:
    """Hand-built expected-vs-actual pairs grade correctly.

    v1 predicted:
      * drift:off_topic decrease/medium — actual 4.0->3.0, |delta|=1.0,
        metric range 8.0 -> fraction 0.125 -> medium bucket. Sign and
        magnitude both agree -> MATCH.
      * cost:tokens_spent increase/small — actual 1000->900 decreases ->
        sign disagrees -> NO MATCH.
    """
    grades = {g.generation_id: g for g in hypothesis_ledger(index_db, EPOCH)}
    v1 = grades["v1"]
    assert v1.predictions == 2
    assert v1.matches == 1
    assert v1.accuracy == 0.5

    moves = {m.metric_name: m for m in v1.movements}
    drift = moves["drift:off_topic"]
    assert drift.sign_match is True
    assert drift.actual_magnitude == "medium"
    assert drift.magnitude_match is True
    assert drift.matched is True

    cost = moves["cost:tokens_spent"]
    assert cost.sign_match is False
    assert cost.matched is False


def test_hypothesis_ledger_large_magnitude(index_db: Path) -> None:
    """v2 predicted a large decrease that actually occurred."""
    grades = {g.generation_id: g for g in hypothesis_ledger(index_db, EPOCH)}
    v2 = grades["v2"]
    # drift:off_topic 4.0 -> 0.0, |delta|=4.0, range 8.0 -> fraction 0.5.
    # fraction must be > 0.5 for "large"; 0.5 exactly is "medium".
    move = v2.movements[0]
    assert move.sign_match is True
    assert move.actual_magnitude == "medium"
    # predicted "large" but actual bucket "medium" -> magnitude mismatch.
    assert move.magnitude_match is False
    assert move.matched is False


def test_hypothesis_ledger_null_outcome_graceful(index_db: Path) -> None:
    """A challenger with a NULL outcome still appears with zero matches."""
    grades = {g.generation_id: g for g in hypothesis_ledger(index_db, EPOCH)}
    v3 = grades["v3"]
    assert v3.predictions == 0
    assert v3.matches == 0
    assert v3.accuracy == 0.0


def test_proposer_calibration_rate_pooled(index_db: Path) -> None:
    """Overall calibration pools matches / predictions across challengers."""
    grades = hypothesis_ledger(index_db, EPOCH)
    # v1: 1/2, v2: 0/1, v3: 0/0 -> pooled 1/3.
    rate = proposer_calibration_rate(grades)
    assert rate == pytest.approx(1.0 / 3.0)


# ---------------------------------------------------------------------------
# mutation_heat_map
# ---------------------------------------------------------------------------


def test_mutation_heat_map_win_correlation(index_db: Path) -> None:
    """Win-correlation counts per mutation id are correct."""
    stats = {s.mutation_id: s for s in mutation_heat_map(index_db, EPOCH)}
    assert set(stats) == {"mp_a", "mp_b"}

    # mp_a patched by v1 (rejected) and v2 (promoted).
    mp_a = stats["mp_a"]
    assert mp_a.times_patched == 2
    assert mp_a.promoted == 1
    assert mp_a.rejected == 1
    assert mp_a.win_rate == 0.5

    # mp_b patched by v3 only (rejected).
    mp_b = stats["mp_b"]
    assert mp_b.times_patched == 1
    assert mp_b.promoted == 0
    assert mp_b.rejected == 1
    assert mp_b.win_rate == 0.0


# ---------------------------------------------------------------------------
# optimization_trajectory
# ---------------------------------------------------------------------------


def test_optimization_trajectory_basic(index_db: Path) -> None:
    """Trajectory walks the promoted spine and reports promotion rate."""
    traj = optimization_trajectory(index_db, EPOCH)
    assert traj.epoch_id == EPOCH
    assert [p.generation_id for p in traj.points] == ["v0", "v2"]
    # 3 challengers (v1,v2,v3); 1 promoted.
    assert traj.challenger_count == 3
    assert traj.promoted_count == 1
    assert traj.promotion_rate == pytest.approx(1.0 / 3.0)
    # Spine of 2 < PLATEAU_WINDOW=3 -> cannot plateau.
    assert traj.plateaued is False


def test_optimization_trajectory_plateau_flag(tmp_path: Path) -> None:
    """The plateau flag fires when the scalar is flat over the window."""
    path = tmp_path / "flat.db"
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(_SCHEMA)
        conn.execute("INSERT INTO epochs VALUES (?,?,?)", (EPOCH, "e0", "x"))
        # v0 seed + v1/v2/v3 all promoted, scalar never improves (1.0 flat).
        conn.executemany(
            "INSERT INTO generations VALUES (?,?,?,?)",
            [
                (EPOCH, "v0", None, 1),
                (EPOCH, "v1", "v0", 1),
                (EPOCH, "v2", "v1", 1),
                (EPOCH, "v3", "v2", 1),
            ],
        )
        conn.executemany(
            "INSERT INTO tournaments VALUES (?,?,?,?,?,?,?,?,?,?)",
            [
                ("t1", EPOCH, "v0", "v1", "promoted", 1.0, 1.0, 0.0, "", "x"),
                ("t2", EPOCH, "v1", "v2", "promoted", 1.0, 1.0, 0.0, "", "x"),
                ("t3", EPOCH, "v2", "v3", "promoted", 1.0, 1.0, 0.0, "", "x"),
            ],
        )
        conn.commit()
    finally:
        conn.close()

    traj = optimization_trajectory(path, EPOCH)
    # spine v0,v1,v2,v3 — last 3 scalars (1.0,1.0,1.0) flat -> plateaued.
    assert [p.generation_id for p in traj.points] == ["v0", "v1", "v2", "v3"]
    assert traj.plateaued is True


def test_optimization_trajectory_improving_not_plateau(tmp_path: Path) -> None:
    """A steadily improving scalar does not trip the plateau flag."""
    path = tmp_path / "improving.db"
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(_SCHEMA)
        conn.execute("INSERT INTO epochs VALUES (?,?,?)", (EPOCH, "e0", "x"))
        conn.executemany(
            "INSERT INTO generations VALUES (?,?,?,?)",
            [
                (EPOCH, "v0", None, 1),
                (EPOCH, "v1", "v0", 1),
                (EPOCH, "v2", "v1", 1),
                (EPOCH, "v3", "v2", 1),
            ],
        )
        conn.executemany(
            "INSERT INTO tournaments VALUES (?,?,?,?,?,?,?,?,?,?)",
            [
                ("t1", EPOCH, "v0", "v1", "promoted", 3.0, 2.5, -0.5, "", "x"),
                ("t2", EPOCH, "v1", "v2", "promoted", 2.5, 2.0, -0.5, "", "x"),
                ("t3", EPOCH, "v2", "v3", "promoted", 2.0, 1.0, -1.0, "", "x"),
            ],
        )
        conn.commit()
    finally:
        conn.close()

    traj = optimization_trajectory(path, EPOCH)
    assert traj.plateaued is False


# ---------------------------------------------------------------------------
# tournament_cost
# ---------------------------------------------------------------------------


def test_tournament_cost(index_db: Path) -> None:
    """tournament_cost sums runtime and computes cost-per-promotion."""
    cost = tournament_cost(index_db, EPOCH)
    assert cost["epoch_id"] == EPOCH

    per = {m["challenger_generation_id"]: m for m in cost["per_matchup"]}
    assert set(per) == {"v1", "v2", "v3"}

    # v1: r_v1_e1 (1100) + r_v1_e2 (1300) = 2400, 2 runs, 0 aborted.
    assert per["v1"]["runtime_ms"] == 2400
    assert per["v1"]["run_count"] == 2
    # v3: r_v3_e1 (2000), aborted.
    assert per["v3"]["aborted_count"] == 1

    # totals: 2400 + (900+950) + 2000 = 6250.
    assert cost["total_runtime_ms"] == 6250
    assert cost["promoted_count"] == 1
    # cost_per_promotion = total / promoted.
    assert cost["cost_per_promotion_ms"] == 6250.0
    assert json.dumps(cost)


def test_tournament_cost_no_promotions(tmp_path: Path) -> None:
    """cost_per_promotion is None when nothing was promoted."""
    path = tmp_path / "norp.db"
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(_SCHEMA)
        conn.execute("INSERT INTO epochs VALUES (?,?,?)", (EPOCH, "e0", "x"))
        conn.executemany(
            "INSERT INTO generations VALUES (?,?,?,?)",
            [(EPOCH, "v0", None, 1), (EPOCH, "v1", "v0", 0)],
        )
        conn.execute(
            "INSERT INTO experiments VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (EPOCH, "v1", "x", "y", "{}", "rejected", "r", None, None, None, None),
        )
        conn.commit()
    finally:
        conn.close()

    cost = tournament_cost(path, EPOCH)
    assert cost["promoted_count"] == 0
    assert cost["cost_per_promotion_ms"] is None


# ---------------------------------------------------------------------------
# Empty / partial database tolerance
# ---------------------------------------------------------------------------


def test_empty_epoch_graceful(tmp_path: Path) -> None:
    """An epoch with no generations yields empty results, never raises."""
    path = tmp_path / "empty.db"
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(_SCHEMA)
        conn.commit()
    finally:
        conn.close()

    bracket = assemble_bracket(path, "ghost")
    assert bracket.champion_spine == ()
    assert bracket.matchups == ()
    assert hypothesis_ledger(path, "ghost") == []
    assert mutation_heat_map(path, "ghost") == []
    traj = optimization_trajectory(path, "ghost")
    assert traj.points == ()
    assert traj.plateaued is False
    cost = tournament_cost(path, "ghost")
    assert cost["total_runtime_ms"] == 0
