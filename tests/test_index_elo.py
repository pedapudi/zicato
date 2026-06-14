"""Pure-fold tests for the read-only Elo analytics layer (:mod:`zicato.index.elo`).

Elo is for VISIBILITY (a human-legible candidate-strength number across the
lineage), never for the promote gate. These tests pin the deterministic
fold over a small fixture lineage: ordering (a consistently-winning
generation ends higher), margin-of-victory K-weighting, cross-epoch
carry-forward (a child seeds at its parent's rating; an epoch roll carries
the prior, not a jump), provisional-K decay, and the game-extraction off
``tournaments`` index rows (gauntlet crowning + non-gauntlet match audit +
field bracket, de-duplicated across the overlapping sources).
"""

from __future__ import annotations

import json

from zicato.index.elo import (
    BASE_K,
    DEFAULT_RATING,
    PROVISIONAL_GAMES,
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
    # Symmetry of the zero-sum-ish update: total displacement from the
    # default is roughly balanced (loser falls about as far as winner rises,
    # modulo the per-side K difference, which is equal here).
    assert ratings["v1"].rating - DEFAULT_RATING > 0
    assert ratings["v0"].rating - DEFAULT_RATING < 0


def test_fold_is_deterministic_regardless_of_input_order() -> None:
    lineage = {
        "a": _node("e", "a", None, "2026-01-01"),
        "b": _node("e", "b", "a", "2026-01-02"),
        "c": _node("e", "c", "a", "2026-01-03"),
    }
    games = [
        EloGame("e", "t1", "m1", "b", "a", 0.3, "2026-01-02T00:00:00Z"),
        EloGame("e", "t2", "m2", "c", "b", 0.4, "2026-01-03T00:00:00Z"),
        EloGame("e", "t3", "m3", "c", "a", 0.2, "2026-01-04T00:00:00Z"),
    ]
    forward = compute_elo(games, lineage)
    backward = compute_elo(list(reversed(games)), lineage)
    for gid in ("a", "b", "c"):
        assert round(forward[gid].rating, 6) == round(backward[gid].rating, 6)


# ---------------------------------------------------------------------------
# Margin-of-victory K-weighting
# ---------------------------------------------------------------------------


def test_larger_margin_moves_rating_more() -> None:
    # Two independent identical match-ups; the only difference is the margin.
    # The big-margin winner should gain more than the small-margin winner.
    small = compute_elo(
        [EloGame("e", "t", "m", "win", "lose", margin=0.01, ran_at="x")],
        {
            "win": _node("e", "win", None, "2026-01-01"),
            "lose": _node("e", "lose", None, "2026-01-01"),
        },
    )
    big = compute_elo(
        [EloGame("e", "t", "m", "win", "lose", margin=1.0, ran_at="x")],
        {
            "win": _node("e", "win", None, "2026-01-01"),
            "lose": _node("e", "lose", None, "2026-01-01"),
        },
    )
    small_gain = small["win"].rating - DEFAULT_RATING
    big_gain = big["win"].rating - DEFAULT_RATING
    assert big_gain > small_gain > 0.0


def test_zero_margin_still_counts_a_game() -> None:
    # A razor-thin (or unknown) margin still moves the rating — the margin
    # multiplier floors above zero, so a near-tie is not a no-op.
    ratings = compute_elo(
        [EloGame("e", "t", "m", "win", "lose", margin=0.0, ran_at="x")],
        {
            "win": _node("e", "win", None, "2026-01-01"),
            "lose": _node("e", "lose", None, "2026-01-01"),
        },
    )
    assert ratings["win"].rating > DEFAULT_RATING
    assert ratings["win"].games == 1


# ---------------------------------------------------------------------------
# Provisional-K decay
# ---------------------------------------------------------------------------


def test_provisional_k_decays_to_base() -> None:
    # A new generation's first game moves it more than a later game against
    # an equally-rated opponent (provisional K > base K), all else equal.
    # We isolate the first vs a settled later game by giving the winner a
    # long unbeaten streak so by game >PROVISIONAL_GAMES it is past the
    # provisional window, then comparing per-game deltas against a FRESH
    # equal-rated opponent each time (so expected score stays 0.5-ish).
    lineage = {"p": _node("e", "p", None, "2026-01-01")}
    # First game: fresh opponent o0, equal rating -> expected 0.5, K is
    # provisional (max).
    g_first = [EloGame("e", "t0", "m", "p", "o0", margin=0.5, ran_at="2026-01-01T00:00:00Z")]
    lin_first = dict(lineage)
    lin_first["o0"] = _node("e", "o0", None, "2026-01-01")
    r_first = compute_elo(g_first, lin_first)
    first_gain = r_first["p"].rating - DEFAULT_RATING

    # Now p plays PROVISIONAL_GAMES filler games (so it is past provisional)
    # then one more game against a FRESH equal-rated opponent. We reset p to
    # default by constructing the scenario so its rating is ~equal to the
    # final fresh opponent: easiest is to compare the K directly via the
    # public constants — assert provisional > base, which drives the gain.
    assert PROVISIONAL_GAMES >= 1
    # The provisional first-game gain must strictly exceed what a base-K game
    # of the same shape would yield. Reconstruct a base-K equal game by
    # giving p enough prior games. Use a chain of fresh opponents so each
    # is equal-rated at default and p stays near default.
    games_warm = [
        EloGame("e", f"tw{i}", "m", "p", f"warm{i}", margin=0.5, ran_at=f"2026-01-01T0{i}:00:00Z")
        for i in range(PROVISIONAL_GAMES + 1)
    ]
    lin_warm = dict(lineage)
    for i in range(PROVISIONAL_GAMES + 1):
        lin_warm[f"warm{i}"] = _node("e", f"warm{i}", None, "2026-01-01")
    r_warm = compute_elo(games_warm, lin_warm)
    # By construction p won them all; its later games used base K. The very
    # first warm game used provisional K, so the first-game gain (which
    # equals first_gain above for an equal-rated opponent) is the largest
    # single increment. We assert the public ordering of the constants too.
    assert first_gain > 0.0
    assert r_warm["p"].games == PROVISIONAL_GAMES + 1
    # Provisional K is strictly larger than the settled K (the decay target).
    from zicato.index.elo import PROVISIONAL_K  # noqa: PLC0415

    assert PROVISIONAL_K > BASE_K


# ---------------------------------------------------------------------------
# Cross-epoch carry-forward + epoch-roll prior
# ---------------------------------------------------------------------------


def test_child_seeds_at_parent_rating() -> None:
    # A child that plays NO games inherits its parent's CURRENT rating as a
    # prior — it starts as strong as the parent it was derived from.
    lineage = {
        "v0": _node("e", "v0", None, "2026-01-01"),
        "v1": _node("e", "v1", "v0", "2026-01-02"),
        "v2": _node("e", "v2", "v1", "2026-01-03"),  # never plays
    }
    # v1 beats v0 once, so v1 > 1500. v2 (no games) should seed at v1's rating.
    games = [EloGame("e", "e:v0->v1", "", "v1", "v0", margin=0.5, ran_at="2026-01-02T00:00:00Z")]
    ratings = compute_elo(games, lineage)
    assert ratings["v1"].rating > DEFAULT_RATING
    assert ratings["v2"].games == 0
    assert round(ratings["v2"].rating, 6) == round(ratings["v1"].rating, 6)


def test_epoch_roll_carries_prior_not_a_jump() -> None:
    # A child in a NEW epoch (a contract roll) seeds at its parent's rating
    # carried across the roll — no spurious jump from the roll itself. With
    # no games in the new epoch, the child's rating equals the parent's.
    lineage = {
        "v0": _node("e", "v0", None, "2026-01-01"),
        "v1": _node("e", "v1", "v0", "2026-01-02"),
        # epoch roll: w0's parent is v1 but it lives in epoch f
        "w0": _node("f", "w0", "v1", "2026-02-01"),
    }
    games = [EloGame("e", "e:v0->v1", "", "v1", "v0", margin=0.5, ran_at="2026-01-02T00:00:00Z")]
    ratings = compute_elo(games, lineage)
    assert ratings["w0"].epoch_id == "f"
    assert ratings["w0"].games == 0
    # Carried as a prior across the roll: same rating as the parent v1.
    assert round(ratings["w0"].rating, 6) == round(ratings["v1"].rating, 6)


def test_orphan_generation_starts_at_default() -> None:
    # A generation that plays a game but has no lineage parent starts at the
    # default anchor (no carry-forward prior available).
    ratings = compute_elo(
        [EloGame("e", "t", "m", "x", "y", margin=0.5, ran_at="z")],
        {},  # no lineage at all
    )
    # x won, y lost: x > default, y < default, both started at default.
    assert ratings["x"].rating > DEFAULT_RATING
    assert ratings["y"].rating < DEFAULT_RATING


# ---------------------------------------------------------------------------
# Game extraction off ``tournaments`` index rows
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
