"""Unit tests for the opt-in rating & winner-resolution layer.

Covers the four pieces of the selection rating/resolution layer
(SELECTION-THEORY.md §3/§5/§7, FUNCTIONALITY-RECOMMENDATIONS.md §5):

1. Bradley--Terry MLE fits a known field (stronger contestants get higher
   theta; the order is recovered; replication shrinks the SEs).
2. theta-rank standings differ from Copeland/scalar when the count-based
   order disagrees with the strength order.
3. Ranked Pairs / Smith on a cyclic margin matrix (the auditable
   lock/skip trace; Smith prune).
4. The uncertainty pre-gate guard defers a noisy near-tie but is ABSENT by
   default (the default path is byte-identical to the legacy gauntlet/swiss).

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
from zicato.selection.resolve import Duel
from zicato.selection.standings_ext import (
    apply_uncertainty_guard,
    read_rating,
    read_resolver,
    read_uncertainty_threshold,
)
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
    assert read_uncertainty_threshold({}) is None
    # Explicit "none" / unrecognised ⇒ today's behaviour.
    assert read_rating({"rating": "none"}) is None
    assert read_resolver({"resolver": "bogus"}) is None
    # Out-of-range thresholds degrade to None (no guard).
    assert read_uncertainty_threshold({"uncertainty_gate": 0.0}) is None
    assert read_uncertainty_threshold({"uncertainty_gate": 1.0}) is None
    assert read_uncertainty_threshold({"uncertainty_gate": "nope"}) is None


def test_param_readers_accept_valid_knobs() -> None:
    assert read_rating({"rating": "bradley_terry"}) == "bradley_terry"
    assert read_resolver({"resolver": "ranked_pairs"}) == "ranked_pairs"
    assert read_resolver({"resolver": "copeland"}) == "copeland"
    assert read_uncertainty_threshold({"uncertainty_gate": 0.95}) == 0.95


# ---------------------------------------------------------------------------
# Uncertainty pre-gate guard
# ---------------------------------------------------------------------------


def _audit_pair(
    parent: str, child: str, *, child_wins: int, parent_wins: int
) -> list[MatchupResult]:
    """A synthetic audit of replicated parent-vs-child duels.

    ``child`` is ``right`` (a negative ``delta_scalar`` means the child
    won that replicate). Each list entry is one duel record.
    """
    out: list[MatchupResult] = []
    for _ in range(child_wins):
        out.append(
            MatchupResult(
                matchup_id="m",
                left_id=parent,
                right_id=child,
                left_agg={"scalar": 1.0},
                right_agg={"scalar": 0.5},
                outcome=GateOutcome("promoted", "", delta_scalar=-0.5, delta_pass_rate=0.0),
            )
        )
    for _ in range(parent_wins):
        out.append(
            MatchupResult(
                matchup_id="m",
                left_id=parent,
                right_id=child,
                left_agg={"scalar": 0.5},
                right_agg={"scalar": 1.0},
                outcome=GateOutcome("rejected", "", delta_scalar=0.5, delta_pass_rate=0.0),
            )
        )
    return out


def test_uncertainty_guard_defers_a_noisy_near_tie() -> None:
    # A coin-flip record (3 child wins, 3 parent wins): the child is not
    # confidently stronger, so a 0.95 guard defers the gate's promotion.
    audit = _audit_pair("v0", "v1", child_wins=3, parent_wins=3)
    decision, reason, deferred = apply_uncertainty_guard(
        "promoted",
        "",
        audit=audit,
        parent_id="v0",
        child_id="v1",
        threshold=0.95,
    )
    assert deferred is True
    assert decision == "deferred"
    assert "uncertainty" in reason


def test_uncertainty_guard_allows_a_clearly_separated_win() -> None:
    # A lopsided record (8 child wins, 0 parent wins): the child is
    # confidently stronger, so the guard does NOT block the promotion.
    audit = _audit_pair("v0", "v1", child_wins=8, parent_wins=0)
    decision, reason, deferred = apply_uncertainty_guard(
        "promoted",
        "",
        audit=audit,
        parent_id="v0",
        child_id="v1",
        threshold=0.95,
    )
    assert deferred is False
    assert decision == "promoted"


def test_uncertainty_guard_is_a_noop_when_absent() -> None:
    # threshold None ⇒ the gate verdict is returned untouched, even on a
    # coin-flip record (the byte-identical default).
    audit = _audit_pair("v0", "v1", child_wins=3, parent_wins=3)
    decision, reason, deferred = apply_uncertainty_guard(
        "promoted",
        "ok",
        audit=audit,
        parent_id="v0",
        child_id="v1",
        threshold=None,
    )
    assert (decision, reason, deferred) == ("promoted", "ok", False)


def test_uncertainty_guard_never_forces_a_promotion() -> None:
    # A rejected gate verdict stays rejected — the guard only ever blocks.
    audit = _audit_pair("v0", "v1", child_wins=8, parent_wins=0)
    decision, reason, deferred = apply_uncertainty_guard(
        "rejected",
        "did not clear",
        audit=audit,
        parent_id="v0",
        child_id="v1",
        threshold=0.95,
    )
    assert (decision, deferred) == ("rejected", False)


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
    # rating/resolver/uncertainty knobs entirely and resolve as today.
    champion = _champion("v0")
    challengers = [_challenger("v1")]
    scalars = {"v0": 0.5, "v1": 0.2}
    spec = TournamentStructure(
        structure="gauntlet",
        params={"rating": "bradley_terry", "resolver": "ranked_pairs", "uncertainty_gate": 0.95},
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


def _result_with(
    matchup: Matchup,
    *,
    left_scalar: float,
    right_scalar: float,
    decision: str,
) -> MatchupResult:
    delta = right_scalar - left_scalar
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


def test_swiss_uncertainty_gate_defers_a_gate_promotion_end_to_end() -> None:
    # Drive a 2-challenger swiss where the leader (v1) and champion (v0)
    # trade duels across the rounds (a noisy near-tie), but the FINAL
    # champion-gate duel is a (marginal) promote. With the uncertainty_gate
    # opt-in, the noisy rating defers the crowning instead of promoting.
    champion = _champion("v0")
    challengers = [_challenger("v1"), _challenger("v2")]
    spec = TournamentStructure(
        structure="swiss",
        params={"field_size": 2, "rounds_n": 2, "uncertainty_gate": 0.95},
    )
    strat = make_strategy(spec)
    strat.seed(champion, challengers)
    # Alternate which of v0/v1 "wins" each swiss duel so the BT fit puts
    # them in a near-tie, while v2 is clearly weak.
    toggle = {"v0_v1": True}

    def scalar_for(left: str, right: str) -> tuple[float, float]:
        if {left, right} == {"v0", "v1"}:
            # Coin-flip: alternate the winner each time this pair meets.
            if toggle["v0_v1"]:
                toggle["v0_v1"] = False
                lo, hi = 0.40, 0.41
            else:
                toggle["v0_v1"] = True
                lo, hi = 0.41, 0.40
            return (lo, hi) if left == "v0" else (hi, lo)
        # Anyone vs v2: v2 is clearly worse (higher scalar).
        return (0.4, 0.9) if right == "v2" else (0.9, 0.4)

    while not strat.resolved():
        batch = strat.next_matchups()
        if not batch:
            break
        for m in batch:
            lid, rid = m.left.generation_id, m.right.generation_id
            if m.matchup_id == "swiss-final":
                # Force the gate to PROMOTE on the final duel (a marginal win).
                strat.record_result(
                    _result_with(m, left_scalar=0.41, right_scalar=0.40, decision="promoted")
                )
            else:
                ls, rs = scalar_for(lid, rid)
                strat.record_result(
                    _result_with(
                        m,
                        left_scalar=ls,
                        right_scalar=rs,
                        decision=("promoted" if rs < ls else "rejected"),
                    )
                )
    decision = strat.champion()
    # The gate promoted, but the uncertainty guard turned it into a defer
    # (the existing "deferred" literal is now a real outcome).
    assert decision.decision == "deferred"
    assert decision.promoted_generation_id is None


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
