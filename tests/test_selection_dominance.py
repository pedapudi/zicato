"""Strict dominance on incomplete comparisons, checked by subset enumeration."""

from __future__ import annotations

import asyncio
from itertools import combinations, permutations, product

import pytest

from zicato.core.types import ExperimentalConfig, ScoringWeights, TournamentStructure
from zicato.selection import Contestant, Matchup, MatchupResult, make_strategy
from zicato.selection.driver import resolve_tournament
from zicato.selection.resolve import Duel, MarginMatrix, build_matrix, smith_set
from zicato.selection.standings_ext import audit_matrix
from zicato.tournament.gate import evaluate_gate


def _smallest_dominating_subset(matrix: MarginMatrix) -> frozenset[str]:
    """Enumerate the definition without using the production closure or beats helper."""
    ids = set(matrix.ids)
    for size in range(1, len(ids) + 1):
        qualifying = [
            frozenset(subset)
            for subset in combinations(sorted(ids), size)
            if all(
                matrix.net.get((member, outsider), 0.0) > 0.0
                for member in subset
                for outsider in ids.difference(subset)
            )
        ]
        if qualifying:
            assert len(qualifying) == 1
            return qualifying[0]
    return frozenset()


def test_unresolved_leaders_survive_pruning_in_every_contestant_order() -> None:
    for duels in (
        [Duel("a", "c", 1.0), Duel("b", "c", 1.0)],
        [Duel("a", "c", 1.0), Duel("b", "c", 1.0), Duel("a", "b", 1.0), Duel("b", "a", 1.0)],
    ):
        net = build_matrix(duels).net
        for order in permutations(("a", "b", "c")):
            matrix = MarginMatrix(order, net)
            assert smith_set(matrix) == tuple(gid for gid in order if gid in {"a", "b"})


def test_smith_membership_matches_every_small_asymmetric_comparison_matrix() -> None:
    """Missing edges, ties, decisive rankings, and cycles share one exact oracle."""
    for count in range(5):
        ids = tuple("abcd"[:count])
        pairs = tuple(combinations(ids, 2))
        for states in product((0, 1, -1), repeat=len(pairs)):
            net = {
                (left, right) if state == 1 else (right, left): 1.0
                for (left, right), state in zip(pairs, states, strict=True)
                if state
            }
            expected = _smallest_dominating_subset(MarginMatrix(ids, net))
            for order in permutations(ids):
                actual = smith_set(MarginMatrix(order, net))
                assert actual == tuple(gid for gid in order if gid in expected), (order, net)


def _measured_result(matchup: Matchup, *, right_won: bool | None) -> MatchupResult:
    left = {"scalar": 2.0 if right_won else 1.0, "pass_rate": 1.0}
    right = {"scalar": 1.0 if right_won is not False else 2.0, "pass_rate": 1.0}
    return MatchupResult(
        matchup_id=matchup.matchup_id,
        left_id=matchup.left.generation_id,
        right_id=matchup.right.generation_id,
        left_agg=left,
        right_agg=right,
        outcome=evaluate_gate(left, right, ScoringWeights(promote_margin=0.1)),
        stage_index=matchup.stage_index,
    )


def test_audit_matrix_retains_contestants_seen_only_in_tied_duels() -> None:
    audit = [
        _measured_result(
            Matchup("tie", Contestant("a", "challenger"), Contestant("b", "challenger")),
            right_won=None,
        ),
        _measured_result(
            Matchup("win", Contestant("b", "challenger"), Contestant("c", "challenger")),
            right_won=False,
        ),
    ]
    matrix = audit_matrix(audit)
    assert set(matrix.ids) == {"a", "b", "c"}
    assert matrix.net == {("b", "c"): 1.0}
    assert set(smith_set(matrix)) == {"a", "b", "c"}


@pytest.mark.parametrize("final_win", [False, True])
def test_experimental_swiss_retains_the_resolver_finalist_and_requires_champion_gate(
    final_win: bool,
) -> None:
    # The opposing a/z wins cancel. Neither a nor z dominates the other;
    # z's resolved win over the champion must survive preliminary pruning.
    right_wins = iter((False, True, True, True, True, False))
    observed: list[tuple[str, str]] = []
    champion = Contestant("champion", "champion")

    async def request_field(count: int):
        assert count == 3
        return champion, [Contestant(gid, "challenger") for gid in ("a", "b", "z")]

    async def run_matchup(matchup: Matchup) -> MatchupResult:
        observed.append((matchup.left.generation_id, matchup.right.generation_id))
        return _measured_result(
            matchup,
            right_won=final_win if matchup.matchup_id == "swiss-final" else next(right_wins),
        )

    decision = asyncio.run(
        resolve_tournament(
            make_strategy(
                TournamentStructure(
                    structure="swiss",
                    params={
                        "field_size": 3,
                        "rounds_n": 3,
                        "replicates": 1,
                    },
                ),
                experimental=ExperimentalConfig(
                    tournament_structures=True, resolver="ranked_pairs"
                ),
            ),
            request_field=request_field,
            run_matchup=run_matchup,
        )
    )
    assert observed == [
        ("a", "b"),
        ("champion", "z"),
        ("a", "z"),
        ("b", "champion"),
        ("z", "a"),
        ("champion", "b"),
        ("champion", "z"),
    ]
    assert decision.promoted_generation_id == ("z" if final_win else None)
    assert decision.decision == ("promoted" if final_win else "rejected")
