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
from pathlib import Path

import pytest

from zicato.index.schema import apply_schema
from zicato.tournament.detail import (
    IndexUnavailableError,
    hypothesis_ledger,
    optimization_trajectory,
    tournament_cost,
)

EPOCH = "2026-05_e0"


# ---------------------------------------------------------------------------
# Synthetic index fixture
# ---------------------------------------------------------------------------


def _hypothesis(core: str, movements: list[dict]) -> str:
    return json.dumps(
        {
            "core_idea": core,
            "why": "pattern-driven",
            "modulating": ["mp_a"],
            "expected_metric_movements": movements,
        }
    )


def _outcome(decision: str, movements: list[dict]) -> str:
    return json.dumps(
        {
            "tournament_decision": decision,
            "metric_movements": movements,
        }
    )


def _seed_index(path: Path) -> None:
    """Create and populate the synthetic four-generation index."""
    conn = sqlite3.connect(str(path))
    try:
        apply_schema(conn)
        conn.execute(
            "INSERT INTO epochs (epoch_id, created_at) VALUES (?,?)",
            (EPOCH, "2026-05-01T00:00:00+00:00"),
        )
        # generations: v0 seed, v1 rejected, v2 promoted, v3 rejected
        conn.executemany(
            "INSERT INTO generations (epoch_id, generation_id, parent_generation_id, "
            "promoted) VALUES (?, ?, ?, ?)",
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
            "INSERT INTO experiments (epoch_id, generation_id, hypothesis_core_idea, "
            "hypothesis_why, hypothesis_json, tournament_decision, rejection_reason, "
            "scalar_score_delta, drift_loss_delta, pass_rate_delta, outcome_json) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
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
                ),
            ),
        )
        conn.execute(
            "INSERT INTO experiments (epoch_id, generation_id, hypothesis_core_idea, "
            "hypothesis_why, hypothesis_json, tournament_decision, rejection_reason, "
            "scalar_score_delta, drift_loss_delta, pass_rate_delta, outcome_json) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
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
                ),
            ),
        )
        # v3 — rejected, NULL outcome_json (partial-data case).
        conn.execute(
            "INSERT INTO experiments (epoch_id, generation_id, hypothesis_core_idea, "
            "hypothesis_why, hypothesis_json, tournament_decision, rejection_reason, "
            "scalar_score_delta, drift_loss_delta, pass_rate_delta, outcome_json) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
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
            "INSERT INTO patches (patch_id, epoch_id, generation_id, mutation_id, op, "
            "rationale) VALUES (?,?,?,?,?,?)",
            [
                ("p1", EPOCH, "v1", "mp_a", "replace", "tighten"),
                ("p2", EPOCH, "v2", "mp_a", "replace", "add planning"),
                ("p3", EPOCH, "v3", "mp_b", "replace", "rewrite"),
            ],
        )
        # runs — two board entries (e1, e2) per challenger generation.
        conn.executemany(
            "INSERT INTO runs (run_id, epoch_id, generation_id, entry_id, runtime_ms, "
            "aborted) VALUES (?,?,?,?,?,?)",
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
            "INSERT INTO loss_profiles (run_id, epoch_id, generation_id, entry_id, "
            "drift_loss, pass_fail, loss_json) VALUES (?,?,?,?,?,?,?)",
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
            "INSERT INTO metric_counts (run_id, namespace, name, severity, count) "
            "VALUES (?,?,?,?,?)",
            [
                ("r_v0_e1", "drift:", "drift:off_topic", "info", 4.0),
                ("r_v0_e2", "drift:", "drift:off_topic", "info", 8.0),
                ("r_v2_e1", "drift:", "drift:off_topic", "info", 0.0),
                ("r_v1_e1", "cost:", "cost:tokens_spent", "", 1000.0),
            ],
        )
        # tournaments — v1 vs v0, v2 vs v0, v3 vs v2.
        conn.executemany(
            "INSERT INTO tournaments (tournament_id, epoch_id, parent_generation_id, "
            "child_generation_id, decision, parent_scalar, child_scalar, "
            "delta_scalar, rejection_reason, ran_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
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
# Missing database
# ---------------------------------------------------------------------------


def test_missing_db_raises() -> None:
    """A missing database raises a clear, operator-actionable error."""
    with pytest.raises(IndexUnavailableError, match="zicato repair index"):
        hypothesis_ledger("/nonexistent/path/index.db", EPOCH)


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
        apply_schema(conn)
        conn.execute("INSERT INTO epochs (epoch_id, created_at) VALUES (?,?)", (EPOCH, "x"))
        # v0 seed + v1/v2/v3 all promoted, scalar never improves (1.0 flat).
        conn.executemany(
            "INSERT INTO generations (epoch_id, generation_id, parent_generation_id, "
            "promoted) VALUES (?,?,?,?)",
            [
                (EPOCH, "v0", None, 1),
                (EPOCH, "v1", "v0", 1),
                (EPOCH, "v2", "v1", 1),
                (EPOCH, "v3", "v2", 1),
            ],
        )
        conn.executemany(
            "INSERT INTO tournaments (tournament_id, epoch_id, parent_generation_id, "
            "child_generation_id, decision, parent_scalar, child_scalar, "
            "delta_scalar, rejection_reason, ran_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
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
        apply_schema(conn)
        conn.execute("INSERT INTO epochs (epoch_id, created_at) VALUES (?,?)", (EPOCH, "x"))
        conn.executemany(
            "INSERT INTO generations (epoch_id, generation_id, parent_generation_id, "
            "promoted) VALUES (?,?,?,?)",
            [
                (EPOCH, "v0", None, 1),
                (EPOCH, "v1", "v0", 1),
                (EPOCH, "v2", "v1", 1),
                (EPOCH, "v3", "v2", 1),
            ],
        )
        conn.executemany(
            "INSERT INTO tournaments (tournament_id, epoch_id, parent_generation_id, "
            "child_generation_id, decision, parent_scalar, child_scalar, "
            "delta_scalar, rejection_reason, ran_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
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
        apply_schema(conn)
        conn.execute("INSERT INTO epochs (epoch_id, created_at) VALUES (?,?)", (EPOCH, "x"))
        conn.executemany(
            "INSERT INTO generations (epoch_id, generation_id, parent_generation_id, "
            "promoted) VALUES (?,?,?,?)",
            [(EPOCH, "v0", None, 1), (EPOCH, "v1", "v0", 0)],
        )
        conn.execute(
            "INSERT INTO experiments (epoch_id, generation_id, hypothesis_core_idea, "
            "hypothesis_why, hypothesis_json, tournament_decision, rejection_reason, "
            "scalar_score_delta, drift_loss_delta, pass_rate_delta, outcome_json) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
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
        apply_schema(conn)
        conn.commit()
    finally:
        conn.close()

    assert hypothesis_ledger(path, "ghost") == []
    traj = optimization_trajectory(path, "ghost")
    assert traj.points == ()
    assert traj.plateaued is False
    cost = tournament_cost(path, "ghost")
    assert cost["total_runtime_ms"] == 0
