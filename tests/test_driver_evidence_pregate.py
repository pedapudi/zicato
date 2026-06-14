"""ON-path tests for the driver's Bradley--Terry pre-gate replication loop.

The driver (:func:`zicato.selection.resolve_tournament`) is byte-identical to
today when ``pre_gate`` is ``None`` (the default). These tests pass a
``pre_gate`` ON to prove the defer→replicate→refit loop:

* a clearly-separated win still promotes through the pre-gate,
* a noisy near-tie spends its closest-CI replicates, then converges to a crown,
* a duel that never separates exhausts its budget and lands ``inconclusive`` +
  fires the dead-letter callback,
* with no ``replicate_duel`` runner the pre-gate terminates (no dangling defer),
* a gauntlet still promotes/rejects untouched when the pre-gate cannot fit
  (single duel, below the credibility floor),
* the default (``pre_gate=None``) path is unchanged.

All synthetic — no live runs.
"""

from __future__ import annotations

import asyncio

from zicato.core.types import TournamentStructure
from zicato.selection import Contestant, Matchup, MatchupResult, make_strategy
from zicato.selection.driver import EvidencePreGate, EvidenceResolution, resolve_tournament
from zicato.tournament.gate import GateOutcome


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


def _replicate_result(left_id: str, right_id: str, *, child_won: bool) -> MatchupResult:
    """One synthetic replicate duel between a seeded pair.

    The crowning pair is always (champion=left, challenger=right) in the driver
    loop, so a child win is the lower (better) right scalar.
    """
    if child_won:
        left_scalar, right_scalar, delta, dec = 1.0, 0.5, -0.5, "promoted"
    else:
        left_scalar, right_scalar, delta, dec = 0.5, 1.0, 0.5, "rejected"
    return MatchupResult(
        matchup_id=f"replicate:{left_id}:{right_id}",
        left_id=left_id,
        right_id=right_id,
        left_agg={"scalar": left_scalar, "pass_rate": 1.0},
        right_agg={"scalar": right_scalar, "pass_rate": 1.0},
        outcome=GateOutcome(dec, "", delta_scalar=delta, delta_pass_rate=0.0),  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# A single-duel gauntlet is below the credibility floor → pre-gate is a no-op
# ---------------------------------------------------------------------------


def test_gauntlet_single_duel_promotes_untouched_under_pregate() -> None:
    # One duel is < MIN_CREDIBLE_DUELS, so the pre-gate cannot fit a credible
    # rating and leaves the gauntlet's promotion verdict alone.
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
    # Below the credibility floor → the gauntlet promotion stands verbatim.
    assert dec.promoted_generation_id == "v1"
    assert dec.decision == "promoted"


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
# No replicate runner → terminate, never dangle a "deferred"
# ---------------------------------------------------------------------------


def test_pregate_without_replicate_runner_terminates() -> None:
    # The pre-gate cannot reach the credibility floor without a replicate
    # runner, so the gauntlet's single-duel promotion stands verbatim — never a
    # dangling non-terminal "deferred".
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
    # Terminal, never a dangling deferred; below the floor the gate verdict
    # stands unchanged and no dead-letter fires.
    assert dec.decision == "promoted"
    assert dec.promoted_generation_id == "v1"
    assert fired == []


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
