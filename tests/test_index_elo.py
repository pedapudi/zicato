"""Pure-fold tests for the read-only Bradley--Terry rating layer
(:mod:`zicato.index.elo`).

The rating is for VISIBILITY (a human-legible candidate-strength number + its
uncertainty across the lineage), never for the promote gate. These tests pin the
deterministic batch fit over small fixture ledgers: transitive ordering
(A > B > C from a dominance chain), the ORDER-INDEPENDENCE the batch MLE buys
over the sequential Elo it replaced, the standard-error output, the zero-games
degrade (no rating, not a fabricated carry-forward), and the game-extraction off
``tournaments`` index rows (gauntlet crowning + non-gauntlet match audit + field
bracket, de-duplicated across the overlapping sources).
"""

from __future__ import annotations

import json
from pathlib import Path

from zicato.index.elo import (
    DEFAULT_RATING,
    EloGame,
    LineageNode,
    compute_elo,
    games_from_tournament_rows,
)


def _node(epoch: str, gid: str, parent: str | None, created: str) -> LineageNode:
    return LineageNode(
        epoch_id=epoch, generation_id=gid, parent_generation_id=parent, created_at=created
    )


# ---------------------------------------------------------------------------
# Core ordering: a consistent winner ends higher
# ---------------------------------------------------------------------------


def test_consistent_winner_ends_higher() -> None:
    lineage = {
        "v0": _node("e", "v0", None, "2026-01-01"),
        "v1": _node("e", "v1", "v0", "2026-01-02"),
    }
    games = [
        EloGame("e", f"e:v0->v1#{i}", "", "v1", "v0", margin=0.5, ran_at=f"2026-01-02T0{i}:00:00Z")
        for i in range(4)
    ]
    ratings = compute_elo(games, lineage)
    assert ratings["v1"].rating > ratings["v0"].rating
    # Both played every game.
    assert ratings["v1"].games == 4
    assert ratings["v0"].games == 4
    # The fit is zero-sum-gauged: the field mean sits at the anchor, so the
    # consistent winner reads above it and the loser below.
    assert ratings["v1"].rating - DEFAULT_RATING > 0
    assert ratings["v0"].rating - DEFAULT_RATING < 0


def test_transitive_chain_orders_a_over_b_over_c() -> None:
    # Known-answer, hand-checkable: A beats B twice, B beats C twice. The
    # Bradley--Terry MLE must order A > B > C strictly (a dominance chain), and
    # by symmetry of the ledger B sits at the field mean (the anchor): A is as
    # far above B as C is below.
    lineage = {
        "A": _node("e", "A", None, "2026-01-01"),
        "B": _node("e", "B", None, "2026-01-01"),
        "C": _node("e", "C", None, "2026-01-01"),
    }
    games = [
        EloGame("e", "t_ab1", "m", "A", "B", ran_at="x"),
        EloGame("e", "t_ab2", "m", "A", "B", ran_at="x"),
        EloGame("e", "t_bc1", "m", "B", "C", ran_at="x"),
        EloGame("e", "t_bc2", "m", "B", "C", ran_at="x"),
    ]
    ratings = compute_elo(games, lineage)
    assert ratings["A"].rating > ratings["B"].rating > ratings["C"].rating
    # A won 2, lost 0; C won 0, lost 2; B is symmetric (won 2 vs C, lost 2 vs A)
    # so it sits at the anchor and the chain is symmetric about it.
    assert round(ratings["B"].rating, 6) == round(DEFAULT_RATING, 6)
    assert round(ratings["A"].rating - DEFAULT_RATING, 6) == round(
        DEFAULT_RATING - ratings["C"].rating, 6
    )
    # Game counts: the ends played their two duels; B played all four.
    assert ratings["A"].games == 2
    assert ratings["C"].games == 2
    assert ratings["B"].games == 4


def test_fold_is_order_independent() -> None:
    # THE ORDER-INDEPENDENCE PIN — the property the batch MLE buys.
    #
    # The Bradley--Terry fit depends ONLY on the win/loss tally per pairing, so
    # the same multiset of games in ANY order yields byte-identical ratings AND
    # standard errors. The sequential margin-K Elo this engine replaced VIOLATED
    # this: each update landed on the RUNNING rating, so it was path-dependent
    # — folding the same games in a different order gave a different final
    # rating (an early win compounds differently than a late one). The old fold
    # masked this by imposing its own deterministic game-ordering pass before
    # folding; the batch MLE needs no such pass because the answer is invariant
    # to order by construction. This test permutes the games directly and pins
    # that invariance for both the rating and its SE.
    lineage = {
        "a": _node("e", "a", None, "2026-01-01"),
        "b": _node("e", "b", "a", "2026-01-02"),
        "c": _node("e", "c", "a", "2026-01-03"),
    }
    games = [
        EloGame("e", "t1", "m1", "b", "a", 0.3, "2026-01-02T00:00:00Z"),
        EloGame("e", "t2", "m2", "c", "b", 0.4, "2026-01-03T00:00:00Z"),
        EloGame("e", "t3", "m3", "c", "a", 0.2, "2026-01-04T00:00:00Z"),
        EloGame("e", "t4", "m4", "a", "c", 0.1, "2026-01-05T00:00:00Z"),
    ]
    forward = compute_elo(games, lineage)
    backward = compute_elo(list(reversed(games)), lineage)
    # A rotation as a third permutation, to rule out a fluke of reversal.
    rotated = compute_elo(games[2:] + games[:2], lineage)
    for gid in ("a", "b", "c"):
        assert round(forward[gid].rating, 9) == round(backward[gid].rating, 9)
        assert round(forward[gid].rating, 9) == round(rotated[gid].rating, 9)
        assert round(forward[gid].se, 9) == round(backward[gid].se, 9)
        assert round(forward[gid].se, 9) == round(rotated[gid].se, 9)


# ---------------------------------------------------------------------------
# Standard error (the v12 addition)
# ---------------------------------------------------------------------------


def test_standard_error_is_populated_and_positive() -> None:
    # Every rated generation gets a finite, strictly-positive SE from the
    # inverse Fisher information (the ridge prior guarantees it).
    lineage = {
        "A": _node("e", "A", None, "2026-01-01"),
        "B": _node("e", "B", None, "2026-01-01"),
    }
    ratings = compute_elo(
        [EloGame("e", "t", "m", "A", "B", ran_at="x")],
        lineage,
    )
    for gid in ("A", "B"):
        assert ratings[gid].se > 0.0
        assert ratings[gid].se == ratings[gid].se  # finite (not NaN)


def test_more_games_shrink_the_standard_error() -> None:
    # More evidence on the same pairing sharpens the fit: the SE with many
    # replicated duels is smaller than with a single one.
    lin = {"A": _node("e", "A", None, "x"), "B": _node("e", "B", None, "x")}
    one = compute_elo([EloGame("e", "t0", "m", "A", "B", ran_at="x")], lin)
    many = compute_elo([EloGame("e", f"t{i}", "m", "A", "B", ran_at="x") for i in range(8)], lin)
    assert many["A"].se < one["A"].se


# ---------------------------------------------------------------------------
# Zero games / degenerate inputs
# ---------------------------------------------------------------------------


def test_zero_game_generation_is_absent_no_carry_forward() -> None:
    # A generation that plays NO game gets NO rating — it is absent from the
    # fit output (the fold writes NULL for it). The batch MLE has nothing to
    # estimate from zero observations, so — unlike the sequential fold it
    # replaced — it does NOT seed a never-played child at its parent's rating.
    lineage = {
        "v0": _node("e", "v0", None, "2026-01-01"),
        "v1": _node("e", "v1", "v0", "2026-01-02"),
        "v2": _node("e", "v2", "v1", "2026-01-03"),  # never plays
    }
    games = [EloGame("e", "e:v0->v1", "", "v1", "v0", margin=0.5, ran_at="2026-01-02T00:00:00Z")]
    ratings = compute_elo(games, lineage)
    assert ratings["v1"].rating > DEFAULT_RATING
    assert "v2" not in ratings  # no games -> no rating, not a carried prior


def test_empty_ledger_yields_no_ratings() -> None:
    assert compute_elo([], {"v0": _node("e", "v0", None, "x")}) == {}
    # Games that are all self-matches / empty-sided also produce nothing.
    assert compute_elo([EloGame("e", "t", "m", "x", "x")], {}) == {}


def test_single_game_still_counts_margin_ignored() -> None:
    # Margins are deliberately NOT an input to the rating (they ride the gate).
    # A single settled game still produces a rating: the winner above the
    # anchor, the loser below — regardless of the margin value.
    razor = compute_elo(
        [EloGame("e", "t", "m", "win", "lose", margin=0.0, ran_at="x")],
        {"win": _node("e", "win", None, "x"), "lose": _node("e", "lose", None, "x")},
    )
    blowout = compute_elo(
        [EloGame("e", "t", "m", "win", "lose", margin=99.0, ran_at="x")],
        {"win": _node("e", "win", None, "x"), "lose": _node("e", "lose", None, "x")},
    )
    assert razor["win"].rating > DEFAULT_RATING
    assert razor["win"].games == 1
    # Identical ledger up to margin => identical rating (margin ignored).
    assert round(razor["win"].rating, 9) == round(blowout["win"].rating, 9)


def test_disconnected_components_stay_finite() -> None:
    # Two clusters that never played each other (a disconnected duel graph): the
    # ridge prior anchors each to the field mean rather than diverging, so every
    # rating + SE is finite. A beats B; C beats D; no cross games.
    lineage = {
        "A": _node("e", "A", None, "x"),
        "B": _node("e", "B", None, "x"),
        "C": _node("e", "C", None, "x"),
        "D": _node("e", "D", None, "x"),
    }
    ratings = compute_elo(
        [
            EloGame("e", "t1", "m", "A", "B", ran_at="x"),
            EloGame("e", "t2", "m", "C", "D", ran_at="x"),
        ],
        lineage,
    )
    for gid in ("A", "B", "C", "D"):
        r = ratings[gid]
        assert r.rating == r.rating and r.se == r.se  # finite
        assert r.se > 0.0
    # Winners above the anchor, losers below, within each component.
    assert ratings["A"].rating > DEFAULT_RATING > ratings["B"].rating
    assert ratings["C"].rating > DEFAULT_RATING > ratings["D"].rating


def test_orphan_generation_is_rated_from_its_games() -> None:
    # A generation that plays a game but has no lineage entry is still rated —
    # its epoch is taken from the game. x beats y => x above the anchor, y below.
    ratings = compute_elo(
        [EloGame("e", "t", "m", "x", "y", margin=0.5, ran_at="z")],
        {},  # no lineage at all
    )
    assert ratings["x"].rating > DEFAULT_RATING
    assert ratings["y"].rating < DEFAULT_RATING
    assert ratings["x"].epoch_id == "e"


# ---------------------------------------------------------------------------
# The visibility-only invariant: nothing gate-side ever reads the columns.
# ---------------------------------------------------------------------------


def test_rating_columns_are_never_read_gate_side() -> None:
    # INVARIANT (inviolable): "Elo is for VISIBILITY, never the gate." The rating
    # columns (``elo`` / ``elo_se`` / ``elo_games``) are written by the index
    # fold and read only by the query/dashboard visibility surfaces — NEVER by
    # the promote gate or any selection strategy. This test pins that: no source
    # file under the gate (``tournament/``) or selection (``selection/``) layer
    # may reference a rating column or import the fold module. If this fails, a
    # rating has leaked into the decision path and the invariant is broken.
    src_root = Path(__file__).resolve().parents[1] / "src" / "zicato"
    gate_side = list((src_root / "tournament").rglob("*.py")) + list(
        (src_root / "selection").rglob("*.py")
    )
    assert gate_side, "expected gate/selection source files to scan"
    offenders: list[str] = []
    for path in gate_side:
        text = path.read_text(encoding="utf-8")
        if "elo_se" in text or "elo_games" in text or "index.elo" in text:
            offenders.append(str(path))
    assert not offenders, f"rating columns leaked into the gate/selection path: {offenders}"


# ---------------------------------------------------------------------------
# Game extraction off ``tournaments`` index rows (unchanged by the engine swap)
# ---------------------------------------------------------------------------


def test_gauntlet_promoted_row_makes_child_the_winner() -> None:
    rows = [
        {
            "tournament_id": "e:v0->v1",
            "epoch_id": "e",
            "parent_generation_id": "v0",
            "child_generation_id": "v1",
            "decision": "promoted",
            "delta_scalar": -0.4,  # child better
            "rounds_json": None,  # gauntlet has no per-match audit
            "ran_at": "2026-01-02T00:00:00Z",
        }
    ]
    games = games_from_tournament_rows(rows)
    assert len(games) == 1
    assert games[0].winner == "v1" and games[0].loser == "v0"
    assert games[0].margin == 0.4


def test_gauntlet_rejected_row_makes_parent_the_winner() -> None:
    rows = [
        {
            "tournament_id": "e:v0->v1",
            "epoch_id": "e",
            "parent_generation_id": "v0",
            "child_generation_id": "v1",
            "decision": "rejected",
            "delta_scalar": 0.2,  # child worse
            "rounds_json": None,
            "ran_at": "x",
        }
    ]
    games = games_from_tournament_rows(rows)
    assert len(games) == 1
    assert games[0].winner == "v0" and games[0].loser == "v1"


def test_non_gauntlet_crowning_row_yields_one_game_per_opponent() -> None:
    rounds = [
        {"match_id": "r1_m1", "opponent": "c2", "won": True, "delta_scalar": -0.3},
        {"match_id": "r2_m1", "opponent": "c3", "won": False, "delta_scalar": 0.1},
    ]
    rows = [
        {
            "tournament_id": "e:v0->v1",
            "epoch_id": "e",
            "parent_generation_id": "v0",
            "child_generation_id": "v1",
            "decision": "promoted",
            "delta_scalar": -0.3,
            "rounds_json": json.dumps(rounds),
            "ran_at": "x",
        }
    ]
    games = games_from_tournament_rows(rows)
    # Two per-match games (the gauntlet fallback is NOT used when rounds exist).
    assert len(games) == 2
    by_match = {g.match_id: g for g in games}
    assert by_match["r1_m1"].winner == "v1" and by_match["r1_m1"].loser == "c2"
    assert by_match["r2_m1"].winner == "c3" and by_match["r2_m1"].loser == "v1"


def test_field_row_yields_a_game_per_settled_two_sided_match() -> None:
    bracket = [
        {
            "stage_index": 0,
            "label": "Bracket round 0",
            "matches": [
                {"match_id": "m1", "competitors": ["a", "b"], "winner": "a", "delta_scalar": -0.2},
                {"match_id": "m2", "competitors": ["c"], "winner": "", "bye": True},
                {
                    "match_id": "m3",
                    "competitors": ["d", "e"],
                    "winner": "",
                    "pending": True,
                },
            ],
        }
    ]
    rows = [
        {
            "tournament_id": "e:field:a",
            "epoch_id": "e",
            "parent_generation_id": "",  # field row leaves these empty
            "child_generation_id": "",
            "decision": "",
            "delta_scalar": None,
            "rounds_json": json.dumps(bracket),
            "ran_at": "x",
        }
    ]
    games = games_from_tournament_rows(rows)
    # Only the settled two-sided match m1 counts (bye + pending skipped).
    assert len(games) == 1
    assert games[0].match_id == "m1"
    assert games[0].winner == "a" and games[0].loser == "b"
    assert games[0].margin == 0.2


def test_racing_rung_set_cut_contributes_zero_games() -> None:
    # A racing intermediate rung that persists a survivor/cut SET with no single
    # named winner (winner=None, N competitors) is not a pairwise outcome — the
    # documented racing-rung zero-games limitation. It contributes nothing.
    bracket = [
        {
            "stage_index": 0,
            "label": "rung 0",
            "matches": [
                {"match_id": "rung0", "competitors": ["a", "b", "c"], "winner": None},
            ],
        }
    ]
    rows = [
        {
            "tournament_id": "e:field:a",
            "epoch_id": "e",
            "parent_generation_id": "",
            "child_generation_id": "",
            "decision": "",
            "delta_scalar": None,
            "rounds_json": json.dumps(bracket),
            "ran_at": "x",
        }
    ]
    assert games_from_tournament_rows(rows) == []


def test_overlapping_sources_are_deduplicated() -> None:
    # The SAME physical match surfaces as a crowning-row game AND a
    # field-bracket game (same tournament_id + match_id + sides). The fold
    # must count it exactly once.
    rounds = [{"match_id": "m1", "opponent": "b", "won": True, "delta_scalar": -0.2}]
    field = [
        {
            "stage_index": 0,
            "label": "r",
            "matches": [
                {"match_id": "m1", "competitors": ["a", "b"], "winner": "a", "delta_scalar": -0.2}
            ],
        }
    ]
    rows = [
        {
            "tournament_id": "e:tourn",
            "epoch_id": "e",
            "parent_generation_id": "x",
            "child_generation_id": "a",
            "decision": "promoted",
            "delta_scalar": -0.2,
            "rounds_json": json.dumps(rounds),
            "ran_at": "x",
        },
        {
            "tournament_id": "e:tourn",
            "epoch_id": "e",
            "parent_generation_id": "",
            "child_generation_id": "",
            "decision": "",
            "delta_scalar": None,
            "rounds_json": json.dumps(field),
            "ran_at": "x",
        },
    ]
    games = games_from_tournament_rows(rows)
    # games_from_tournament_rows emits both; compute_elo de-dups. Confirm
    # the fold counts the single match once for each side.
    lineage = {
        "a": _node("e", "a", "x", "2026-01-02"),
        "b": _node("e", "b", None, "2026-01-01"),
        "x": _node("e", "x", None, "2026-01-01"),
    }
    ratings = compute_elo(games, lineage)
    assert ratings["a"].games == 1
    assert ratings["b"].games == 1
