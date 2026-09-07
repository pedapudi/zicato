"""Required evidence confirmation across fresh, replayed, and unusable draws.

Synthetic matchups exercise production strategies and the confirmation driver.
Confirmed improvements promote; incomplete evidence remains terminally deferred.
An absent requirement preserves the strategy's original decision.
"""

from __future__ import annotations

import asyncio
import itertools
from dataclasses import replace

import pytest

from zicato.core.measurement import MeasurementDraw
from zicato.core.scoring_config import ScoringWeights, recommended_scaffold_weights
from zicato.core.types import ExperimentalConfig, TournamentStructure
from zicato.selection import Contestant, Matchup, MatchupResult, make_strategy
from zicato.selection.driver import (
    EvidencePreGate,
    EvidenceResolution,
    evaluate_tournament,
    resolve_tournament,
)
from zicato.selection.evidence_gate import EVIDENCE_REPLICATE_BASE
from zicato.tournament.gate import GateOutcome, evaluate_gate


def _champion(gid: str = "v0") -> Contestant:
    return Contestant(generation_id=gid, role="champion")


def _challenger(gid: str) -> Contestant:
    return Contestant(generation_id=gid, role="challenger")


def _result(m: Matchup, *, left_scalar: float, right_scalar: float) -> MatchupResult:
    delta = right_scalar - left_scalar
    decision = "promoted" if delta < 0 else "rejected"
    return MatchupResult(
        matchup_id=m.matchup_id,
        left_id=m.left.generation_id,
        right_id=m.right.generation_id,
        left_agg={"scalar": left_scalar, "pass_rate": 1.0},
        right_agg={"scalar": right_scalar, "pass_rate": 1.0},
        outcome=GateOutcome(decision, "", delta_scalar=delta, delta_pass_rate=0.0),  # type: ignore[arg-type]
    )


_REPLICATE_SLOTS = itertools.count(EVIDENCE_REPLICATE_BASE)


def _replicate_result(left_id: str, right_id: str, *, child_won: bool) -> MatchupResult:
    """One synthetic replicate duel between a seeded pair.

    The crowning pair is always (champion=left, challenger=right) in the driver
    loop, so a child win is the lower (better) right scalar. Each call mints a
    UNIQUE matchup id encoding a reserved replicate slot — the ReplicateDuel
    contract the orchestrator's implementations satisfy; the driver's audit
    guard drops a re-presented id rather than double-counting one draw.
    """
    if child_won:
        left_scalar, right_scalar, delta, dec = 1.0, 0.5, -0.5, "promoted"
    else:
        left_scalar, right_scalar, delta, dec = 0.5, 1.0, 0.5, "rejected"
    slot = next(_REPLICATE_SLOTS)
    return MatchupResult(
        measurement_draw=MeasurementDraw.from_index(slot, base_seed=None),
        matchup_id=f"bt-replicate:r{slot}:{left_id}:{right_id}",
        left_id=left_id,
        right_id=right_id,
        left_agg={"scalar": left_scalar, "pass_rate": 1.0},
        right_agg={"scalar": right_scalar, "pass_rate": 1.0},
        outcome=GateOutcome(dec, "", delta_scalar=delta, delta_pass_rate=0.0),  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# A single duel cannot satisfy required confirmation without further evidence
# ---------------------------------------------------------------------------


def test_gauntlet_single_duel_keeps_champion_without_confirmation_runner() -> None:
    # A missing runner cannot turn statistical insufficiency into permission.
    s = make_strategy(TournamentStructure(structure="gauntlet"))
    champ = _champion("v0")
    challenger = _challenger("v1")

    async def request_field(n: int):
        return champ, [challenger]

    async def run_matchup(m: Matchup) -> MatchupResult:
        return _result(m, left_scalar=1.0, right_scalar=0.4)

    dec = asyncio.run(
        resolve_tournament(
            s,
            request_field=request_field,
            run_matchup=run_matchup,
            pre_gate=EvidencePreGate(threshold=0.9, replicate_budget=3),
        )
    )
    assert dec.promoted_generation_id is None
    assert dec.decision == "deferred"


@pytest.mark.parametrize(
    "unusable",
    [
        "tie",
        "all_ties",
        "incomplete",
        "nonfinite",
        "unexpected_pair",
        "zero_budget",
        "missing_runner",
    ],
)
def test_required_confirmation_retains_attempts_when_evidence_is_incomplete(
    unusable: str,
) -> None:
    initial: list[MatchupResult] = []
    calls: list[MatchupResult] = []

    async def request_field(_count: int):
        return _champion(), [_challenger("v1")]

    async def run_matchup(matchup: Matchup) -> MatchupResult:
        result = _result(matchup, left_scalar=1.0, right_scalar=0.5)
        if unusable == "all_ties":
            aggregate = {"scalar": 1.0, "pass_rate": 1.0}
            result = replace(
                result,
                left_agg=aggregate,
                right_agg=aggregate,
                outcome=evaluate_gate(aggregate, aggregate, ScoringWeights(promote_margin=0.0)),
            )
        initial.append(result)
        return result

    async def replicate_duel(left: str, right: str) -> MatchupResult:
        result = _replicate_result(left, right, child_won=True)
        if unusable in {"tie", "all_ties"}:
            result = replace(
                result,
                right_agg=dict(result.left_agg),
                outcome=GateOutcome("rejected", "tie", delta_scalar=0.0, delta_pass_rate=0.0),
            )
        elif unusable == "incomplete":
            result = replace(result, right_agg={"incomplete_entries": ["task"], "scalar": 0.0})
        elif unusable == "nonfinite":
            result = replace(result, outcome=replace(result.outcome, delta_scalar=float("nan")))
        elif unusable == "unexpected_pair":
            result = replace(result, right_id="v2")
        calls.append(result)
        return result

    budget = 0 if unusable == "zero_budget" else 5
    result = asyncio.run(
        evaluate_tournament(
            make_strategy(TournamentStructure(structure="gauntlet")),
            request_field=request_field,
            run_matchup=run_matchup,
            pre_gate=EvidencePreGate(threshold=0.8, replicate_budget=budget),
            replicate_duel=None if unusable == "missing_runner" else replicate_duel,
        )
    )
    assert result.decision.decision == "deferred"
    assert result.decision.promoted_generation_id is None
    assert result.evidence is not None
    verdict = result.evidence.verdict
    assert verdict.confirmation_status == "incomplete"
    assert not verdict.credible
    assert verdict.replicates_spent == len(calls)
    assert len(verdict.attempts) == 1 + len(calls)
    assert sum(attempt.budget_spent for attempt in verdict.attempts) == len(calls)
    assert verdict.attempts[0].eligibility == "selection_only"
    expected_eligibility = "tie" if unusable == "all_ties" else unusable
    assert all(attempt.eligibility == expected_eligibility for attempt in verdict.attempts[1:])
    assert result.decision.matchups == tuple(initial + calls)
    if unusable == "all_ties":
        assert verdict.n_duels == 0


# ---------------------------------------------------------------------------
# Defer → replicate → converge to a crown
# ---------------------------------------------------------------------------


def test_pregate_replicates_then_promotes_on_separation() -> None:
    # The gauntlet promotes v1 on a single duel; the pre-gate then replicates
    # the crowning duel (child wins every replicate) until the CIs separate,
    # and finally crowns. The closest-CI duel restricts to the crowning pair.
    s = make_strategy(TournamentStructure(structure="gauntlet"))
    champ = _champion("v0")
    challenger = _challenger("v1")
    replicate_calls: list[tuple[str, str]] = []

    async def request_field(n: int):
        return champ, [challenger]

    async def run_matchup(m: Matchup) -> MatchupResult:
        return _result(m, left_scalar=1.0, right_scalar=0.4)

    async def replicate_duel(left_id: str, right_id: str) -> MatchupResult:
        replicate_calls.append((left_id, right_id))
        return _replicate_result(left_id, right_id, child_won=True)

    dec = asyncio.run(
        resolve_tournament(
            s,
            request_field=request_field,
            run_matchup=run_matchup,
            pre_gate=EvidencePreGate(threshold=0.9, replicate_budget=60),
            replicate_duel=replicate_duel,
        )
    )
    assert dec.decision == "promoted"
    assert dec.promoted_generation_id == "v1"
    # It actually spent replicates, and only ever on the crowning pair.
    assert replicate_calls
    assert all({a, b} == {"v0", "v1"} for a, b in replicate_calls)
    # The replicate duels were appended to the audit trail.
    assert len(dec.matchups) > 1


@pytest.mark.parametrize("applied_count", [1, 2, 4])
def test_recommended_racing_confirms_full_and_partial_fields_within_budget(
    applied_count: int,
) -> None:
    specification = recommended_scaffold_weights().tournament_structure
    strategy = make_strategy(specification, board_ids=[f"entry-{i}" for i in range(10)])
    champion = _champion()
    challengers = [_challenger(f"v{i}") for i in range(1, applied_count + 1)]
    evidence_calls: list[tuple[str, str]] = []

    async def request_field(n: int):
        assert n == 4
        return champion, challengers

    async def run_matchup(matchup: Matchup) -> MatchupResult:
        assert matchup.replicates == 2
        child_scalar = 0.4 if matchup.right.generation_id == "v1" else 0.8
        return _result(matchup, left_scalar=1.0, right_scalar=child_scalar)

    async def replicate_duel(left: str, right: str) -> MatchupResult:
        evidence_calls.append((left, right))
        return _replicate_result(left, right, child_won=True)

    budget = specification.params["promote_confidence_replicates"]
    result = asyncio.run(
        evaluate_tournament(
            strategy,
            request_field=request_field,
            run_matchup=run_matchup,
            pre_gate=EvidencePreGate(
                threshold=specification.params["promote_confidence_threshold"],
                replicate_budget=budget,
            ),
            replicate_duel=replicate_duel,
        )
    )
    assert result.decision.promoted_generation_id == "v1"
    assert result.evidence is not None
    assert result.evidence.verdict.credible
    assert result.evidence.verdict.difference is not None
    assert result.evidence.verdict.difference.ci_lo > 0.0
    # Partial application cannot reduce the family planned before outcomes.
    assert result.evidence.verdict.difference.comparison_count == 4 * (budget + 1)
    assert result.evidence.verdict.replicates_spent == len(evidence_calls)
    assert 0 < len(evidence_calls) <= budget
    assert set(evidence_calls) == {("v0", "v1")}


# ---------------------------------------------------------------------------
# Budget exhausted without separation → inconclusive + dead-letter callback
# ---------------------------------------------------------------------------


def test_pregate_inconclusive_fires_dead_letter_on_unresolvable_tie() -> None:
    # The replicate duels alternate winners (a genuine coin flip), so the CIs
    # never separate; the budget is spent and the verdict lands inconclusive.
    s = make_strategy(TournamentStructure(structure="gauntlet"))
    champ = _champion("v0")
    challenger = _challenger("v1")
    flip = {"n": 0}
    inconclusive: list[EvidenceResolution] = []

    async def request_field(n: int):
        return champ, [challenger]

    async def run_matchup(m: Matchup) -> MatchupResult:
        return _result(m, left_scalar=1.0, right_scalar=0.4)

    async def replicate_duel(left_id: str, right_id: str) -> MatchupResult:
        flip["n"] += 1
        return _replicate_result(left_id, right_id, child_won=(flip["n"] % 2 == 0))

    def on_inconclusive(res: EvidenceResolution) -> None:
        inconclusive.append(res)

    dec = asyncio.run(
        resolve_tournament(
            s,
            request_field=request_field,
            run_matchup=run_matchup,
            pre_gate=EvidencePreGate(threshold=0.99, replicate_budget=4),
            replicate_duel=replicate_duel,
            on_inconclusive=on_inconclusive,
        )
    )
    assert dec.decision == "deferred"  # the closed-enum token for "kept, held"
    assert dec.promoted_generation_id is None
    assert len(inconclusive) == 1
    res = inconclusive[0]
    assert res.verdict.decision == "inconclusive"
    assert res.verdict.ci_overlap is True
    # The CI history traced every refit step.
    assert len(res.ci_history) >= 2


# ---------------------------------------------------------------------------
# A field structure's crowning promote is held on the same evidence
# ---------------------------------------------------------------------------


def test_pregate_holds_a_noisy_swiss_crowning_promote() -> None:
    # The pre-gate runs over every structure's decision, the gauntlet's single
    # duel and a field structure's crowning duel alike. Swiss sends its leader
    # (v1) to the champion gate after trading duels with the champion (v0)
    # round by round, so the two sit on top of each other in the fit while v2
    # is clearly weaker. The crowning duel is a marginal promote, and every
    # replicate the loop spends flips the other way, so the CIs never
    # separate: the crown is held on the evidence rather than awarded on a
    # 0.01 win.
    s = make_strategy(
        TournamentStructure(structure="swiss", params={"field_size": 2, "rounds_n": 2}),
        experimental=ExperimentalConfig(tournament_structures=True),
    )
    champ = _champion("v0")
    challengers = [_challenger("v1"), _challenger("v2")]
    toggle = {"v0_wins": True}
    flip = {"n": 0}
    inconclusive: list[EvidenceResolution] = []

    async def request_field(n: int):
        return champ, challengers

    async def run_matchup(m: Matchup) -> MatchupResult:
        left, right = m.left.generation_id, m.right.generation_id
        if m.matchup_id == "swiss-final":
            # A marginal champion-gate promote: the challenger wins by 0.01.
            return _result(m, left_scalar=0.41, right_scalar=0.40)
        if {left, right} == {"v0", "v1"}:
            # Coin flip: alternate which side takes the lower (better) scalar.
            winner = "v0" if toggle["v0_wins"] else "v1"
            toggle["v0_wins"] = not toggle["v0_wins"]
            return _result(
                m,
                left_scalar=0.40 if left == winner else 0.41,
                right_scalar=0.40 if right == winner else 0.41,
            )
        # Anyone against v2: v2 takes the higher (worse) scalar.
        return _result(
            m,
            left_scalar=0.9 if left == "v2" else 0.4,
            right_scalar=0.9 if right == "v2" else 0.4,
        )

    async def replicate_duel(left_id: str, right_id: str) -> MatchupResult:
        flip["n"] += 1
        return _replicate_result(left_id, right_id, child_won=(flip["n"] % 2 == 0))

    dec = asyncio.run(
        resolve_tournament(
            s,
            request_field=request_field,
            run_matchup=run_matchup,
            pre_gate=EvidencePreGate(threshold=0.95, replicate_budget=4),
            replicate_duel=replicate_duel,
            on_inconclusive=inconclusive.append,
        )
    )
    assert dec.decision == "deferred"
    assert dec.promoted_generation_id is None
    assert len(inconclusive) == 1
    assert inconclusive[0].verdict.ci_overlap is True


# ---------------------------------------------------------------------------
# Duplicate draws never accumulate — identical data must not separate CIs
# ---------------------------------------------------------------------------


def test_pregate_drops_replicates_that_replay_an_audited_draw() -> None:
    # Replaying a draw spends budget but cannot supply another observation.
    # The unresolved requirement therefore retains the champion.
    s = make_strategy(TournamentStructure(structure="gauntlet"))
    champ = _champion("v0")
    challenger = _challenger("v1")
    calls = {"n": 0}

    async def request_field(n: int):
        return champ, [challenger]

    async def run_matchup(m: Matchup) -> MatchupResult:
        return _result(m, left_scalar=1.0, right_scalar=0.4)

    fixed = _replicate_result("v0", "v1", child_won=True)

    async def replaying_replicate_duel(left_id: str, right_id: str) -> MatchupResult:
        calls["n"] += 1
        return fixed  # the same draw, every time

    dec = asyncio.run(
        resolve_tournament(
            s,
            request_field=request_field,
            run_matchup=run_matchup,
            pre_gate=EvidencePreGate(threshold=0.9, replicate_budget=10),
            replicate_duel=replaying_replicate_duel,
        )
    )
    # The whole budget was spent chasing evidence the runner never supplied...
    assert calls["n"] == 10
    # Every attempt remains recorded; only one supplies independent evidence.
    assert dec.decision == "deferred"
    assert dec.promoted_generation_id is None
    assert len(dec.matchups) == 11  # selection plus every returned attempt


# ---------------------------------------------------------------------------
# Missing replicate runner produces a terminal deferred decision
# ---------------------------------------------------------------------------


def test_pregate_without_replicate_runner_terminates() -> None:
    # Missing execution capability produces a durable inconclusive terminal.
    s = make_strategy(TournamentStructure(structure="gauntlet"))
    champ = _champion("v0")
    challenger = _challenger("v1")
    fired: list[EvidenceResolution] = []

    async def request_field(n: int):
        return champ, [challenger]

    async def run_matchup(m: Matchup) -> MatchupResult:
        return _result(m, left_scalar=1.0, right_scalar=0.99)  # a marginal win

    def on_inconclusive(res: EvidenceResolution) -> None:
        fired.append(res)

    dec = asyncio.run(
        resolve_tournament(
            s,
            request_field=request_field,
            run_matchup=run_matchup,
            pre_gate=EvidencePreGate(threshold=0.99, replicate_budget=3),
            replicate_duel=None,  # no runner → cannot reach the credibility floor
            on_inconclusive=on_inconclusive,
        )
    )
    # The terminal remains available through the dead-letter callback.
    assert dec.decision == "deferred"
    assert dec.promoted_generation_id is None
    assert len(fired) == 1
    assert fired[0].verdict.confirmation_status == "incomplete"


# ---------------------------------------------------------------------------
# Default path (pre_gate=None) is unchanged
# ---------------------------------------------------------------------------


def test_default_no_pregate_is_unchanged() -> None:
    s = make_strategy(TournamentStructure(structure="gauntlet"))
    champ = _champion("v0")
    challenger = _challenger("v1")

    async def request_field(n: int):
        return champ, [challenger]

    async def run_matchup(m: Matchup) -> MatchupResult:
        return _result(m, left_scalar=1.0, right_scalar=0.4)

    dec = asyncio.run(resolve_tournament(s, request_field=request_field, run_matchup=run_matchup))
    assert dec.promoted_generation_id == "v1"
    assert dec.decision == "promoted"
