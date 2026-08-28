"""Tests for the typed tournament-evaluation boundary."""

from __future__ import annotations

import asyncio

import pytest

from zicato.core.types import TournamentDecision, TournamentStructure
from zicato.selection import Contestant, evaluate_tournament, make_strategy
from zicato.selection.strategy import Matchup, MatchupResult
from zicato.tournament.gate import GateOutcome


@pytest.mark.parametrize(
    ("gate_decision", "promoted_id"),
    [
        (TournamentDecision.PROMOTED, "v1"),
        (TournamentDecision.REJECTED, None),
        (TournamentDecision.DEFERRED, None),
    ],
)
def test_gauntlet_evaluation_preserves_the_gate_decision(
    gate_decision: TournamentDecision,
    promoted_id: str | None,
) -> None:
    strategy = make_strategy(TournamentStructure(structure="gauntlet"))

    async def request_field(_n: int) -> tuple[Contestant, list[Contestant]]:
        return (
            Contestant("v0", role="champion"),
            [Contestant("v1", role="challenger")],
        )

    async def run_matchup(matchup: Matchup) -> MatchupResult:
        return MatchupResult(
            matchup_id=matchup.matchup_id,
            left_id="v0",
            right_id="v1",
            left_agg={"scalar": 1.0},
            right_agg={"scalar": 0.5},
            outcome=GateOutcome(
                decision=gate_decision,
                reason="gate result",
                delta_scalar=-0.5,
                delta_pass_rate=0.0,
            ),
        )

    evaluation = asyncio.run(
        evaluate_tournament(
            strategy,
            request_field=request_field,
            run_matchup=run_matchup,
        )
    )

    assert evaluation.decision.decision == gate_decision
    assert evaluation.decision.promoted_generation_id == promoted_id
    assert evaluation.evidence is None
