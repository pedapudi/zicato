"""Confirmation cannot borrow selection samples or rename repeated measurements."""

from dataclasses import replace

import pytest

from zicato.core.measurement import UNKNOWN_SEED, MeasurementDraw, MeasurementPurpose
from zicato.selection.driver import EvidencePreGate, confirm_promotion_with_evidence
from zicato.selection.strategy import Contestant, MatchupResult, SelectionDecision
from zicato.testing.fixtures import make_loss_profile
from zicato.tournament.gate import GateOutcome
from zicato.tournament.runner import TournamentResult


def observation(index: int, measurement: MeasurementDraw | None = None) -> MatchupResult:
    return MatchupResult(
        matchup_id=f"match-{index}",
        left_id="parent",
        right_id="child",
        left_agg={"scalar": 1.0},
        right_agg={"scalar": 0.0},
        outcome=GateOutcome("promoted", "lower loss", -1.0, 0.0),
        measurement_draw=measurement,
    )


@pytest.mark.asyncio
async def test_selection_results_never_supply_confirmation_sample_size() -> None:
    ordinary = tuple(observation(index) for index in range(60))
    decision = SelectionDecision("child", "promoted", "lower loss", ordinary)
    final, evidence = await confirm_promotion_with_evidence(
        decision,
        champion=Contestant("parent", "champion"),
        pre_gate=EvidencePreGate(0.8, 0),
        replicate_duel=None,
        planned_candidates=4,
    )
    assert final.decision == "deferred"
    assert evidence is not None
    assert evidence.verdict.n_duels == 0
    assert {attempt.eligibility for attempt in evidence.verdict.attempts} == {"selection_only"}
    assert final.matchups == ordinary


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "provenance",
    ["absent", "unknown_seed", "repeated", "ordinary", "independent", "independent_seeds"],
)
async def test_confirmation_identity_survives_renamed_matchups(provenance: str) -> None:
    calls = 0

    async def draw(_left: str, _right: str) -> MatchupResult:
        nonlocal calls
        index = calls
        calls += 1
        purpose = (
            MeasurementPurpose.TOURNAMENT
            if provenance == "ordinary"
            else MeasurementPurpose.CONFIRMATION
        )
        measurement = (
            None
            if provenance == "absent"
            else MeasurementDraw(
                purpose,
                0 if provenance in {"repeated", "independent_seeds"} else index,
                base_seed=(
                    UNKNOWN_SEED
                    if provenance == "unknown_seed"
                    else index
                    if provenance == "independent_seeds"
                    else None
                ),
            )
        )
        return observation(index + 1, measurement)

    decision = SelectionDecision("child", "promoted", "lower loss", (observation(0),))
    final, evidence = await confirm_promotion_with_evidence(
        decision,
        champion=Contestant("parent", "champion"),
        pre_gate=EvidencePreGate(0.8, 32),
        replicate_duel=draw,
        planned_candidates=4,
    )
    assert evidence is not None
    assert len(final.matchups) == calls + 1
    assert sum(attempt.budget_spent for attempt in evidence.verdict.attempts) == calls
    assert evidence.verdict.attempts[0].eligibility == "selection_only"
    if provenance in {"independent", "independent_seeds"}:
        assert final.decision == "promoted"
        assert evidence.verdict.n_duels == calls
    else:
        assert final.decision == "deferred"
        assert calls == 32
        assert evidence.verdict.n_duels == (1 if provenance == "repeated" else 0)
        if provenance == "unknown_seed":
            import json

            from zicato.selection.evidence_gate import rating_block

            recorded = json.loads(json.dumps(rating_block(evidence.verdict)))
            assert all(
                attempt["eligibility"] == "missing_provenance"
                and "base_seed" not in attempt["measurement_draw"]
                for attempt in recorded["attempts"][1:]
            )


def test_aggregate_draw_identity_requires_every_actual_contribution() -> None:
    draw = MeasurementDraw(MeasurementPurpose.CONFIRMATION, 0, base_seed=17)
    pairs = {
        entry: tuple(
            make_loss_profile(
                generation_id=generation,
                entry_id=entry,
                measurement=draw,
                execution_started=True,
            )
            for generation in ("parent", "child")
        )
        for entry in ("first", "second")
    }
    aggregate = {"per_entry": {entry: {} for entry in pairs}}
    result = TournamentResult(
        "parent", "child", aggregate, aggregate, observation(0).outcome, pairs
    )
    assert result.measurement_draw == draw
    left, right = pairs["second"]
    for changed in (
        replace(right, measurement=None),
        replace(right, measurement=MeasurementDraw(MeasurementPurpose.CONFIRMATION, 1, 17)),
        replace(right, measurement=MeasurementDraw(MeasurementPurpose.CONFIRMATION, 0, 29)),
        replace(right, execution_started=False),
        replace(right, generation_id="different"),
        replace(right, entry_id="different"),
    ):
        assert (
            replace(result, per_entry_losses={**pairs, "second": (left, changed)}).measurement_draw
            is None
        )
    assert replace(result, per_entry_losses={"first": pairs["first"]}).measurement_draw is None
    assert (
        replace(result, child_agg={**aggregate, "incomplete_entries": ["omitted"]}).measurement_draw
        is None
    )
