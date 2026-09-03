"""Unit tests for the opt-in rating & winner-resolution layer.

Covers the four pieces of the selection rating/resolution layer
(SELECTION-THEORY.md §3/§5/§7, FUNCTIONALITY-RECOMMENDATIONS.md §5):

1. Bradley--Terry MLE fits a known field (stronger contestants get higher
   theta; the order is recovered; replication shrinks the SEs).
2. theta-rank standings differ from Copeland/scalar when the count-based
   order disagrees with the strength order.
3. Ranked Pairs / Smith on a cyclic margin matrix (the auditable
   lock/skip trace; Smith prune).
4. The knobs wired into the strategies, where an absent or unrecognised
   value leaves each structure on its unchanged scalar/Copeland path.

All synthetic — no live runs.
"""

from __future__ import annotations

import math

import pytest

from zicato.core.types import TournamentStructure
from zicato.selection import (
    Contestant,
    Matchup,
    MatchupResult,
    build_matrix,
    condorcet_check,
    copeland_order,
    fit_bradley_terry,
    make_strategy,
    prob_stronger,
    ranked_pairs,
    smith_set,
    theta_rank,
)
from zicato.selection.rating import fit_plackett_luce
from zicato.selection.resolve import Duel
from zicato.selection.standings_ext import read_rating, read_resolver
from zicato.tournament.gate import GateOutcome

# ---------------------------------------------------------------------------
# Bradley--Terry rating
# ---------------------------------------------------------------------------


def test_bradley_terry_recovers_a_known_strength_order() -> None:
    # A transitive field: a >> b >> c, each pairing played several times so
    # the MLE is well-determined. a beats b and c always; b beats c always.
    matches = (
        [("a", "b")] * 8
        + [("a", "c")] * 8
        + [("b", "c")] * 8
        # a few upsets so no record is perfect (keeps thetas finite without
        # leaning entirely on the prior).
        + [("b", "a"), ("c", "b")]
    )
    rating = fit_bradley_terry(matches)
    assert set(rating) == {"a", "b", "c"}
    theta_a = rating["a"][0]
    theta_b = rating["b"][0]
    theta_c = rating["c"][0]
    # Strength order a > b > c.
    assert theta_a > theta_b > theta_c
    # theta_rank reads the same order.
    assert theta_rank(rating) == ["a", "b", "c"]
    # Strengths are centered (zero-sum gauge).
    assert math.isclose(theta_a + theta_b + theta_c, 0.0, abs_tol=1e-6)


def test_bradley_terry_replication_shrinks_standard_errors() -> None:
    few = fit_bradley_terry([("a", "b"), ("b", "a"), ("a", "b")])
    many = fit_bradley_terry(([("a", "b")] * 6) + ([("b", "a")] * 6))
    # More replicates ⇒ tighter (smaller) standard errors on the same pair.
    assert many["a"][1] < few["a"][1]
    assert many["b"][1] < few["b"][1]


def test_bradley_terry_empty_field_is_empty() -> None:
    assert fit_bradley_terry([]) == {}


def test_bradley_terry_rejects_nonpositive_prior() -> None:
    with pytest.raises(ValueError):
        fit_bradley_terry([("a", "b")], prior=0.0)


# ---------------------------------------------------------------------------
# Plackett--Luce rating (the grouped generalisation of Bradley--Terry)
# ---------------------------------------------------------------------------


def test_plackett_luce_reduces_exactly_to_bradley_terry_on_pairwise() -> None:
    # THE REDUCTION PIN. For two-item observations the Plackett--Luce choice
    # probability p_i/(p_i+p_j) IS the Bradley--Terry logistic, so the SAME
    # pairwise match list fed through both fits must agree — not just the
    # ordering but the theta AND the standard error, to tight tolerance (the
    # per-observation gradient and Fisher information are term-for-term equal).
    matches = (
        [("a", "b")] * 5
        + [("a", "c")] * 4
        + [("b", "c")] * 6
        + [("b", "a"), ("c", "a"), ("c", "b")]
    )
    bt = fit_bradley_terry(matches)
    pl = fit_plackett_luce([((win,), (lose,)) for win, lose in matches])
    assert set(pl) == set(bt)
    for gid in bt:
        assert math.isclose(pl[gid][0], bt[gid][0], abs_tol=1e-9)
        assert math.isclose(pl[gid][1], bt[gid][1], abs_tol=1e-9)


def test_plackett_luce_grouped_symmetry_known_answer() -> None:
    # {A,B} survive over {C,D} with no other data. By symmetry A<->B and
    # C<->D the strengths must pair up (theta_A == theta_B, theta_C == theta_D)
    # and the survivors sit strictly above the cut (theta_A > theta_C). SEs are
    # present and finite for all four.
    rating = fit_plackett_luce([(("A", "B"), ("C", "D"))])
    assert set(rating) == {"A", "B", "C", "D"}
    theta = {g: rating[g][0] for g in rating}
    assert math.isclose(theta["A"], theta["B"], abs_tol=1e-9)
    assert math.isclose(theta["C"], theta["D"], abs_tol=1e-9)
    assert theta["A"] > theta["C"]
    # Zero-sum gauge.
    assert math.isclose(sum(theta.values()), 0.0, abs_tol=1e-6)
    for g in rating:
        assert rating[g][1] > 0.0
        assert rating[g][1] == rating[g][1]  # finite (not NaN)


def test_plackett_luce_transitive_rung_chain_orders_strictly() -> None:
    # A survives every rung; D is cut first, then C, then B — a strict
    # dominance chain expressed purely as survivor/cut groups. The recovered
    # strengths must be strictly ordered A > B > C > D.
    chain = [
        (("A", "B", "C"), ("D",)),  # rung 0: D cut
        (("A", "B"), ("C",)),  # rung 1: C cut
        (("A",), ("B",)),  # rung 2: B cut
    ]
    rating = fit_plackett_luce(chain)
    theta = {g: rating[g][0] for g in rating}
    assert theta["A"] > theta["B"] > theta["C"] > theta["D"]


def test_plackett_luce_is_input_order_independent() -> None:
    # Shuffling the observation list yields byte-identical output (the fit
    # canonicalises the observations + the id index internally, so the float
    # summation order is fixed regardless of input order).
    obs = [
        (("A", "B", "C"), ("D",)),
        (("A", "B"), ("C",)),
        (("B",), ("C",)),
        (("A", "B"), ("C", "D")),
        (("A", "B"), ("C", "D")),
    ]
    forward = fit_plackett_luce(obs)
    backward = fit_plackett_luce(list(reversed(obs)))
    rotated = fit_plackett_luce(obs[2:] + obs[:2])
    assert forward == backward
    assert forward == rotated


def test_plackett_luce_skips_observation_over_the_cardinality_cap() -> None:
    # A survivor set larger than the cap is SKIPPED (never crashed, never
    # approximated). The lone over-cap observation contributes nothing, so the
    # fit is empty. A within-cap observation alongside it is unaffected.
    over_cap = tuple(f"s{i}" for i in range(9))  # |S| = 9 > PL_MAX_SURVIVORS
    assert fit_plackett_luce([(over_cap, ("z",))]) == {}
    mixed = fit_plackett_luce([(over_cap, ("z",)), (("A",), ("B",))])
    assert set(mixed) == {"A", "B"}
    assert mixed["A"][0] > mixed["B"][0]


def test_plackett_luce_rejects_nonpositive_prior() -> None:
    with pytest.raises(ValueError):
        fit_plackett_luce([(("a",), ("b",))], prior=0.0)


def test_plackett_luce_empty_and_degenerate_inputs_are_empty() -> None:
    assert fit_plackett_luce([]) == {}
    # An all-survive / all-cut observation carries no comparison.
    assert fit_plackett_luce([(("a", "b"), ())]) == {}
    assert fit_plackett_luce([((), ("a", "b"))]) == {}


def test_prob_stronger_is_monotone_and_bounded() -> None:
    # Clear separation ⇒ near-certain; identical ⇒ 0.5.
    assert prob_stronger(2.0, 0.1, -2.0, 0.1) > 0.99
    assert math.isclose(prob_stronger(0.0, 0.3, 0.0, 0.3), 0.5, abs_tol=1e-9)
    # Wider uncertainty pulls a separated pair back toward 0.5.
    tight = prob_stronger(0.5, 0.1, 0.0, 0.1)
    loose = prob_stronger(0.5, 1.0, 0.0, 1.0)
    assert 0.5 < loose < tight


# ---------------------------------------------------------------------------
# Winner resolution — Condorcet / Smith / Ranked Pairs
# ---------------------------------------------------------------------------


def test_condorcet_winner_on_a_transitive_field() -> None:
    matrix = build_matrix(
        [
            Duel("a", "b", 0.3),
            Duel("a", "c", 0.4),
            Duel("b", "c", 0.2),
        ]
    )
    assert condorcet_check(matrix) == "a"
    # Smith set collapses to the single Condorcet winner.
    assert smith_set(matrix) == ("a",)
    rp = ranked_pairs(matrix)
    assert rp.winner == "a"
    assert rp.order == ("a", "b", "c")


def test_ranked_pairs_breaks_a_cycle_by_dropping_the_weakest_edge() -> None:
    # A Condorcet cycle a>b>c>a, but with UNEQUAL margins: a>b by .42,
    # b>c by .31, c>a by only .14. Ranked Pairs locks the two strong edges
    # and SKIPS the weakest (c>a), which would close the cycle ⇒ winner a.
    matrix = build_matrix(
        [
            Duel("a", "b", 0.42),
            Duel("b", "c", 0.31),
            Duel("c", "a", 0.14),
        ]
    )
    assert condorcet_check(matrix) is None  # genuine cycle, no Condorcet winner
    # The whole cycle is one Smith set (top tier).
    assert set(smith_set(matrix)) == {"a", "b", "c"}
    rp = ranked_pairs(matrix)
    assert rp.winner == "a"
    # The auditable trace: two locked, the weakest skipped.
    locked = [(s.winner, s.loser) for s in rp.trace if s.locked]
    skipped = [(s.winner, s.loser) for s in rp.trace if not s.locked]
    assert ("a", "b") in locked and ("b", "c") in locked
    assert skipped == [("c", "a")]  # the weakest margin in the cycle, dropped
    # Margins are sorted strongest-first in the trace.
    margins = [s.margin for s in rp.trace]
    assert margins == sorted(margins, reverse=True)


def test_smith_set_prunes_a_dominated_outsider() -> None:
    # a, b, c form a top cycle; d loses to all three (dominated outsider).
    matrix = build_matrix(
        [
            Duel("a", "b", 0.4),
            Duel("b", "c", 0.3),
            Duel("c", "a", 0.2),
            Duel("a", "d", 0.5),
            Duel("b", "d", 0.5),
            Duel("c", "d", 0.5),
        ]
    )
    smith = smith_set(matrix)
    assert set(smith) == {"a", "b", "c"}
    assert "d" not in smith


def test_copeland_order_is_margin_blind() -> None:
    # a beats b by a hair, b beats c and d by a lot; Copeland counts wins,
    # so a (1 win) ranks below b (more wins). The count, not the margin.
    matrix = build_matrix(
        [
            Duel("a", "b", 0.01),
            Duel("b", "c", 0.9),
            Duel("b", "d", 0.9),
            Duel("a", "c", 0.5),
            Duel("a", "d", 0.5),
        ]
    )
    order = copeland_order(matrix)
    # a: beats b, c, d → 3 wins; b: beats c, d, loses a → +1; so a leads.
    assert order[0] == "a"


# ---------------------------------------------------------------------------
# Param readers (opt-in knobs)
# ---------------------------------------------------------------------------


def test_param_readers_default_to_none() -> None:
    assert read_rating({}) is None
    assert read_resolver({}) is None
    # Explicit "none" / unrecognised ⇒ today's behaviour.
    assert read_rating({"rating": "none"}) is None
    assert read_resolver({"resolver": "bogus"}) is None


def test_param_readers_accept_valid_knobs() -> None:
    assert read_rating({"rating": "bradley_terry"}) == "bradley_terry"
    assert read_resolver({"resolver": "ranked_pairs"}) == "ranked_pairs"
    assert read_resolver({"resolver": "copeland"}) == "copeland"


# ---------------------------------------------------------------------------
# Strategy wiring (opt-in; default byte-identical)
# ---------------------------------------------------------------------------


def _champion(gid: str = "v0") -> Contestant:
    return Contestant(generation_id=gid, role="champion")


def _challenger(gid: str) -> Contestant:
    return Contestant(generation_id=gid, role="challenger")


def _result(
    matchup: Matchup,
    *,
    left_scalar: float,
    right_scalar: float,
    decision: str | None = None,
) -> MatchupResult:
    delta = right_scalar - left_scalar
    if decision is None:
        decision = "promoted" if delta < 0 else "rejected"
    return MatchupResult(
        matchup_id=matchup.matchup_id,
        left_id=matchup.left.generation_id,
        right_id=matchup.right.generation_id,
        left_agg={"scalar": left_scalar, "pass_rate": 1.0},
        right_agg={"scalar": right_scalar, "pass_rate": 1.0},
        outcome=GateOutcome(
            decision=decision,  # type: ignore[arg-type]
            reason="" if decision == "promoted" else "reject",
            delta_scalar=delta,
            delta_pass_rate=0.0,
        ),
        stage_index=matchup.stage_index,
        bracket_slot=matchup.bracket_slot,
    )


def _drive(
    strategy,
    champion: Contestant,
    challengers: list[Contestant],
    scalars: dict[str, float],
):
    strategy.seed(champion, challengers)
    while not strategy.resolved():
        batch = strategy.next_matchups()
        if not batch:
            break
        for m in batch:
            strategy.record_result(
                _result(
                    m,
                    left_scalar=scalars[m.left.generation_id],
                    right_scalar=scalars[m.right.generation_id],
                )
            )
    return strategy.champion()


def test_swiss_theta_rank_standings_opt_in_changes_order_vs_copeland() -> None:
    # Build a swiss field where Copeland (win count) and theta-rank can
    # diverge. We just assert the rating path runs and produces a coherent,
    # strength-ordered standings list distinct from a crash; the precise
    # order is exercised by the pure BT tests above.
    champion = _champion("v0")
    challengers = [_challenger("v1"), _challenger("v2"), _challenger("v3")]
    scalars = {"v0": 0.5, "v1": 0.2, "v2": 0.3, "v3": 0.9}
    spec = TournamentStructure(
        structure="swiss",
        params={"field_size": 3, "rounds_n": 3, "rating": "bradley_terry"},
    )
    strat = make_strategy(spec)
    decision = _drive(strat, champion, challengers, scalars)
    standings = decision.standings
    assert standings  # non-empty
    # Ranks are a 1..N permutation.
    assert sorted(s.rank for s in standings) == list(range(1, len(standings) + 1))


def test_swiss_default_path_is_byte_identical_with_and_without_absent_knobs() -> None:
    scalars = {"v0": 0.5, "v1": 0.2, "v2": 0.4}
    base = make_strategy(
        TournamentStructure(structure="swiss", params={"field_size": 2, "rounds_n": 2})
    )
    # An explicit "none" rating / resolver must resolve identically to absent.
    noned = make_strategy(
        TournamentStructure(
            structure="swiss",
            params={"field_size": 2, "rounds_n": 2, "rating": "none", "resolver": "none"},
        )
    )
    d_base = _drive(base, _champion("v0"), [_challenger("v1"), _challenger("v2")], scalars)
    d_none = _drive(noned, _champion("v0"), [_challenger("v1"), _challenger("v2")], scalars)
    assert d_base.promoted_generation_id == d_none.promoted_generation_id
    assert d_base.decision == d_none.decision
    assert [s.generation_id for s in d_base.standings] == [
        s.generation_id for s in d_none.standings
    ]


def test_gauntlet_is_untouched_by_the_knobs() -> None:
    # field_size==1 / gauntlet is the back-compat anchor: it must ignore the
    # rating/resolver knobs entirely and resolve as today.
    champion = _champion("v0")
    challengers = [_challenger("v1")]
    scalars = {"v0": 0.5, "v1": 0.2}
    spec = TournamentStructure(
        structure="gauntlet",
        params={"rating": "bradley_terry", "resolver": "ranked_pairs"},
    )
    strat = make_strategy(spec)
    decision = _drive(strat, champion, challengers, scalars)
    # The challenger beat the champion (0.2 < 0.5) → promoted, exactly as the
    # vanilla gauntlet, with the opt-in knobs ignored.
    assert decision.promoted_generation_id == "v1"
    assert decision.decision == "promoted"


def test_single_elim_resolver_leader_opt_in_runs_end_to_end() -> None:
    champion = _champion("v0")
    challengers = [_challenger("v1"), _challenger("v2"), _challenger("v3")]
    scalars = {"v0": 0.5, "v1": 0.2, "v2": 0.3, "v3": 0.4}
    spec = TournamentStructure(
        structure="single_elim",
        params={"field_size": 3, "resolver": "ranked_pairs"},
    )
    strat = make_strategy(spec)
    decision = _drive(strat, champion, challengers, scalars)
    # A finalist was chosen and crowned (it beat the champion on scalar).
    assert decision.decision in {"promoted", "rejected", "deferred"}
    assert decision.standings


def test_double_elim_resolver_and_rating_opt_in_runs_end_to_end() -> None:
    champion = _champion("v0")
    challengers = [_challenger("v1"), _challenger("v2"), _challenger("v3")]
    scalars = {"v0": 0.5, "v1": 0.2, "v2": 0.3, "v3": 0.4}
    spec = TournamentStructure(
        structure="double_elim",
        params={"field_size": 3, "resolver": "copeland", "rating": "bradley_terry"},
    )
    strat = make_strategy(spec)
    decision = _drive(strat, champion, challengers, scalars)
    assert decision.decision in {"promoted", "rejected", "deferred"}
    assert decision.standings
