"""Tests for the typed tournament-evaluation boundary.

Two properties, both about the boundary NOT re-deciding anything:

* the gauntlet's single full-board duel reaches the crowning decision
  untouched — its verdict, its reason, and its recorded matchup audit;
* a structure declared with ``field_size == 1`` degrades to exactly that
  same single duel instead of erroring, for every registered structure.
"""

from __future__ import annotations

import asyncio

import pytest

from zicato.core.types import TournamentDecision, TournamentStructure
from zicato.selection import Contestant, evaluate_tournament, make_strategy
from zicato.selection.driver import TournamentEvaluation
from zicato.selection.strategy import Matchup, MatchupResult
from zicato.tournament.gate import GateOutcome

#: The reason the stub gate returns. Asserted verbatim on the crowning
#: decision: the gate's own sentence is what the journal and the dashboard
#: render, so a boundary that summarised or dropped it would leave an
#: operator reading "rejected" with no stated cause.
_GATE_REASON = "challenger regressed"


def _evaluate(
    spec: TournamentStructure,
    gate_decision: TournamentDecision,
) -> TournamentEvaluation:
    """Drive one structure over a champion and a single challenger."""
    strategy = make_strategy(spec, experimental_structures=True)

    async def request_field(n: int) -> tuple[Contestant, list[Contestant]]:
        return (
            Contestant("v0", role="champion"),
            [Contestant(f"v{i + 1}", role="challenger") for i in range(max(1, n))],
        )

    async def run_matchup(matchup: Matchup) -> MatchupResult:
        return MatchupResult(
            matchup_id=matchup.matchup_id,
            left_id=matchup.left.generation_id,
            right_id=matchup.right.generation_id,
            left_agg={"scalar": 1.0},
            right_agg={"scalar": 0.5},
            outcome=GateOutcome(
                decision=gate_decision,
                reason=_GATE_REASON,
                delta_scalar=-0.5,
                delta_pass_rate=0.0,
            ),
        )

    return asyncio.run(
        evaluate_tournament(
            strategy,
            request_field=request_field,
            run_matchup=run_matchup,
        )
    )


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
    evaluation = _evaluate(TournamentStructure(structure="gauntlet"), gate_decision)

    assert evaluation.decision.decision == gate_decision
    assert evaluation.decision.promoted_generation_id == promoted_id
    assert evaluation.evidence is None
    # The gate's own sentence reaches the crowning decision unaltered.
    assert evaluation.decision.reason == _GATE_REASON
    # …and the duel's verdict is recorded as the gate returned it, not
    # re-derived from the crowning decision.
    assert evaluation.decision.matchups[0].outcome.decision == gate_decision


@pytest.mark.parametrize("structure", ["single_elim", "double_elim", "swiss", "racing"])
def test_field_size_one_degrades_to_the_gauntlet_decision(structure: str) -> None:
    """One challenger collapses any structure to the single full-board duel.

    The strategy registry documents this degeneracy; this pins it at the
    evaluation boundary the evolve round actually drives, so a structure
    that grew a mandatory multi-contestant phase would fail here rather
    than at the first live round with a one-candidate field.
    """
    spec = TournamentStructure(structure=structure, params={"field_size": 1})

    evaluation = _evaluate(spec, TournamentDecision.PROMOTED)

    assert evaluation.decision.decision == TournamentDecision.PROMOTED
    assert evaluation.decision.promoted_generation_id == "v1"
    assert evaluation.decision.reason == _GATE_REASON
    assert evaluation.decision.matchups[0].outcome.decision == TournamentDecision.PROMOTED
