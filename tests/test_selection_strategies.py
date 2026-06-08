"""Unit tests for the SelectionStrategy abstraction and the five strategies.

These tests drive each strategy's scheduling / advance / stopping on
SYNTHETIC matchup results — no real tournament runs (the project rule is
no live evolve runs). Each strategy interprets a
:class:`~zicato.tournament.gate.GateOutcome` it is handed; the tests build
those outcomes directly so the bracket logic is exercised in isolation
from the gate and the runner.
"""

from __future__ import annotations

import asyncio

import pytest

from zicato.core.types import TournamentStructure
from zicato.selection import (
    Contestant,
    Matchup,
    MatchupResult,
    make_strategy,
    resolve_tournament,
)
from zicato.tournament.gate import GateOutcome

# ---------------------------------------------------------------------------
# Synthetic-result helpers
# ---------------------------------------------------------------------------


def _agg(scalar: float) -> dict[str, object]:
    return {"scalar": scalar, "pass_rate": 1.0}


def _result(
    matchup: Matchup,
    *,
    left_scalar: float,
    right_scalar: float,
    decision: str | None = None,
) -> MatchupResult:
    """Build a synthetic MatchupResult with a gate outcome.

    ``decision`` defaults to the gauntlet-style verdict: ``"promoted"``
    when ``right`` beats ``left`` (lower scalar), else ``"rejected"``.
    The gate's ``delta_scalar`` is ``right - left`` (negative ⇒ right
    better), matching the real gate.
    """
    delta = right_scalar - left_scalar
    if decision is None:
        decision = "promoted" if delta < 0 else "rejected"
    return MatchupResult(
        matchup_id=matchup.matchup_id,
        left_id=matchup.left.generation_id,
        right_id=matchup.right.generation_id,
        left_agg=_agg(left_scalar),
        right_agg=_agg(right_scalar),
        outcome=GateOutcome(
            decision=decision,  # type: ignore[arg-type]
            reason="" if decision == "promoted" else "synthetic reject",
            delta_scalar=delta,
            delta_pass_rate=0.0,
        ),
        stage_index=matchup.stage_index,
        bracket_slot=matchup.bracket_slot,
    )


def _champion(gid: str = "v0") -> Contestant:
    return Contestant(generation_id=gid, role="champion")


def _challenger(gid: str) -> Contestant:
    return Contestant(generation_id=gid, role="challenger")


def _run_strategy(
    strategy,
    champion: Contestant,
    challengers: list[Contestant],
    scalars: dict[str, float],
):
    """Drive a strategy to resolution using a deterministic scalar table.

    ``scalars`` maps generation_id → its (fixed) scalar; each duel uses
    those scalars so the strategy's advance logic is fully determined by
    the test's scenario.
    """
    strategy.seed(champion, challengers)
    guard = 0
    while not strategy.resolved():
        batch = strategy.next_matchups()
        if not batch:
            break
        for m in batch:
            r = _result(
                m,
                left_scalar=scalars[m.left.generation_id],
                right_scalar=scalars[m.right.generation_id],
            )
            strategy.record_result(r)
        guard += 1
        if guard > 100:
            raise AssertionError("strategy did not converge (possible infinite loop)")
    return strategy.champion()


def _run_strategy_to_final(
    strategy, scalars: dict[str, float]
) -> tuple[list[Matchup], Matchup | None]:
    """Drive an already-seeded racing strategy until the crowning duel is scheduled.

    Records every rung result from ``scalars`` but STOPS the moment the
    ``racing-final`` matchup is scheduled, returning ``(rung_matchups,
    final)`` — every rung matchup it scheduled along the way plus the
    crowning matchup (without recording the final's result), so a test can
    inspect both rungs' and the crowning duel's fields (e.g. wall-clock
    budgets). ``final`` is ``None`` if no final is ever scheduled.
    """
    rungs: list[Matchup] = []
    guard = 0
    while not strategy.resolved():
        batch = strategy.next_matchups()
        if not batch:
            break
        if any(m.matchup_id == "racing-final" for m in batch):
            final = next(m for m in batch if m.matchup_id == "racing-final")
            return rungs, final
        for m in batch:
            rungs.append(m)
            strategy.record_result(
                _result(
                    m,
                    left_scalar=scalars[m.left.generation_id],
                    right_scalar=scalars[m.right.generation_id],
                )
            )
        guard += 1
        if guard > 100:
            raise AssertionError("strategy did not converge (possible infinite loop)")
    return rungs, None


# ---------------------------------------------------------------------------
# Gauntlet
# ---------------------------------------------------------------------------


def test_gauntlet_field_size_is_one() -> None:
    s = make_strategy(TournamentStructure(structure="gauntlet"))
    assert s.field_size() == 1


def test_gauntlet_promotes_when_challenger_wins() -> None:
    s = make_strategy(TournamentStructure(structure="gauntlet"))
    dec = _run_strategy(s, _champion("v0"), [_challenger("v1")], {"v0": 1.0, "v1": 0.5})
    assert dec.decision == "promoted"
    assert dec.promoted_generation_id == "v1"
    assert len(dec.matchups) == 1
    assert dec.crowning_matchup_id == "gauntlet"


def test_gauntlet_champion_stands_when_challenger_loses() -> None:
    s = make_strategy(TournamentStructure(structure="gauntlet"))
    dec = _run_strategy(s, _champion("v0"), [_challenger("v1")], {"v0": 0.5, "v1": 1.0})
    assert dec.decision == "rejected"
    assert dec.promoted_generation_id is None


def test_gauntlet_schedules_exactly_one_full_board_duel() -> None:
    s = make_strategy(TournamentStructure(structure="gauntlet"))
    s.seed(_champion("v0"), [_challenger("v1")])
    batch = s.next_matchups()
    assert len(batch) == 1
    assert batch[0].board_subset is None  # full board
    assert batch[0].replicates == 1
    # No further matchups scheduled before a result lands.
    assert s.next_matchups() == ()


def test_gauntlet_respects_replicates_param() -> None:
    s = make_strategy(TournamentStructure(structure="gauntlet", params={"replicates": 3}))
    s.seed(_champion("v0"), [_challenger("v1")])
    assert s.next_matchups()[0].replicates == 3


# ---------------------------------------------------------------------------
# Single elimination
# ---------------------------------------------------------------------------


def test_single_elim_lowest_challenger_reaches_final_and_promotes() -> None:
    s = make_strategy(TournamentStructure(structure="single_elim", params={"field_size": 4}))
    champ = _champion("v0")
    challengers = [_challenger(f"v{i}") for i in (1, 2, 3, 4)]
    # v3 is the strongest challenger (lowest scalar); champion v0 worst.
    scalars = {"v0": 1.0, "v1": 0.9, "v2": 0.8, "v3": 0.3, "v4": 0.7}
    dec = _run_strategy(s, champ, challengers, scalars)
    # v3 should win the bracket and then beat the champion at the final.
    assert dec.promoted_generation_id == "v3"
    assert dec.decision == "promoted"
    assert dec.crowning_matchup_id == "final"


def test_single_elim_champion_stands_when_no_finalist_beats_it() -> None:
    s = make_strategy(TournamentStructure(structure="single_elim", params={"field_size": 2}))
    champ = _champion("v0")
    challengers = [_challenger("v1"), _challenger("v2")]
    # Champion is the strongest of all — the bracket survivor loses the final.
    scalars = {"v0": 0.1, "v1": 0.5, "v2": 0.7}
    dec = _run_strategy(s, champ, challengers, scalars)
    assert dec.promoted_generation_id is None
    assert dec.decision == "rejected"


def test_single_elim_defaults_to_replicated_duels() -> None:
    s = make_strategy(TournamentStructure(structure="single_elim", params={"field_size": 2}))
    s.seed(_champion("v0"), [_challenger("v1"), _challenger("v2")])
    batch = s.next_matchups()
    assert batch
    assert all(m.replicates >= 2 for m in batch)


def test_single_elim_odd_field_gets_a_bye() -> None:
    s = make_strategy(TournamentStructure(structure="single_elim", params={"field_size": 3}))
    champ = _champion("v0")
    challengers = [_challenger("v1"), _challenger("v2"), _challenger("v3")]
    scalars = {"v0": 1.0, "v1": 0.5, "v2": 0.6, "v3": 0.4}
    dec = _run_strategy(s, champ, challengers, scalars)
    # v3 is strongest challenger and should be crowned.
    assert dec.promoted_generation_id == "v3"
    # A bye match should appear in the round records.
    rounds = s.rounds()
    assert any(m.bye for r in rounds for m in r.matches)


def test_single_elim_records_full_bracket_audit() -> None:
    s = make_strategy(TournamentStructure(structure="single_elim", params={"field_size": 4}))
    champ = _champion("v0")
    challengers = [_challenger(f"v{i}") for i in (1, 2, 3, 4)]
    scalars = {"v0": 1.0, "v1": 0.9, "v2": 0.8, "v3": 0.3, "v4": 0.7}
    dec = _run_strategy(s, champ, challengers, scalars)
    # 4 challengers => 2 semis + 1 challenger-final + 1 champion-final = 4.
    assert len(dec.matchups) == 4


# ---------------------------------------------------------------------------
# Double elimination
# ---------------------------------------------------------------------------


def test_double_elim_promotes_strongest_challenger() -> None:
    s = make_strategy(TournamentStructure(structure="double_elim", params={"field_size": 4}))
    champ = _champion("v0")
    challengers = [_challenger(f"v{i}") for i in (1, 2, 3, 4)]
    scalars = {"v0": 1.0, "v1": 0.9, "v2": 0.8, "v3": 0.2, "v4": 0.7}
    dec = _run_strategy(s, champ, challengers, scalars)
    assert dec.promoted_generation_id == "v3"
    assert dec.crowning_matchup_id == "GF"


def test_double_elim_eliminates_only_on_second_loss() -> None:
    s = make_strategy(TournamentStructure(structure="double_elim", params={"field_size": 4}))
    champ = _champion("v0")
    challengers = [_challenger(f"v{i}") for i in (1, 2, 3, 4)]
    scalars = {"v0": 1.0, "v1": 0.9, "v2": 0.8, "v3": 0.2, "v4": 0.7}
    _run_strategy(s, champ, challengers, scalars)
    # Every challenger that lost a WB match got a losers'-bracket "second
    # life": at least one LB match must have been scheduled (LB- prefix).
    assert any(r.left_id and r.matchup_id.startswith("LB-") for r in s._audit)


def test_double_elim_champion_stands_when_unbeaten() -> None:
    s = make_strategy(TournamentStructure(structure="double_elim", params={"field_size": 2}))
    champ = _champion("v0")
    challengers = [_challenger("v1"), _challenger("v2")]
    scalars = {"v0": 0.1, "v1": 0.5, "v2": 0.6}
    dec = _run_strategy(s, champ, challengers, scalars)
    assert dec.promoted_generation_id is None


# ---------------------------------------------------------------------------
# Swiss
# ---------------------------------------------------------------------------


def test_swiss_runs_configured_number_of_rounds() -> None:
    s = make_strategy(
        TournamentStructure(structure="swiss", params={"field_size": 3, "rounds_n": 3})
    )
    champ = _champion("v0")
    challengers = [_challenger("v1"), _challenger("v2"), _challenger("v3")]
    scalars = {"v0": 0.9, "v1": 0.4, "v2": 0.5, "v3": 0.6}
    _run_strategy(s, champ, challengers, scalars)
    swiss_rounds = [r for r in s.rounds() if r.label.startswith("Swiss")]
    assert len(swiss_rounds) == 3


def test_swiss_promotes_leader_only_if_it_clears_champion_gate() -> None:
    s = make_strategy(
        TournamentStructure(structure="swiss", params={"field_size": 2, "rounds_n": 2})
    )
    champ = _champion("v0")
    challengers = [_challenger("v1"), _challenger("v2")]
    # v1 is the best contestant overall and beats the champion at the gate.
    scalars = {"v0": 0.9, "v1": 0.2, "v2": 0.5}
    dec = _run_strategy(s, champ, challengers, scalars)
    assert dec.promoted_generation_id == "v1"
    assert dec.crowning_matchup_id == "swiss-final"


def test_swiss_leader_that_loses_champion_gate_is_not_crowned() -> None:
    s = make_strategy(
        TournamentStructure(structure="swiss", params={"field_size": 2, "rounds_n": 2})
    )
    champ = _champion("v0")
    challengers = [_challenger("v1"), _challenger("v2")]
    # The champion is the strongest — the Swiss leader loses the gate.
    scalars = {"v0": 0.1, "v1": 0.4, "v2": 0.6}
    dec = _run_strategy(s, champ, challengers, scalars)
    assert dec.promoted_generation_id is None
    assert dec.decision == "rejected"


def test_swiss_standings_rank_by_copeland() -> None:
    s = make_strategy(
        TournamentStructure(structure="swiss", params={"field_size": 3, "rounds_n": 2})
    )
    champ = _champion("v0")
    challengers = [_challenger("v1"), _challenger("v2"), _challenger("v3")]
    scalars = {"v0": 0.9, "v1": 0.2, "v2": 0.5, "v3": 0.6}
    dec = _run_strategy(s, champ, challengers, scalars)
    # v1 (lowest scalar) should win the most duels and rank first.
    assert dec.standings[0].generation_id == "v1"


# ---------------------------------------------------------------------------
# Racing
# ---------------------------------------------------------------------------


def _racing_spec(**params) -> TournamentStructure:
    base = {"field_size": 4, "board_ids": ["e1", "e2", "e3", "e4"], "eta": 2}
    base.update(params)
    return TournamentStructure(structure="racing", params=base)


def test_racing_cuts_by_rank_each_rung() -> None:
    s = make_strategy(_racing_spec())
    champ = _champion("v0")
    challengers = [_challenger(f"v{i}") for i in (1, 2, 3, 4)]
    # v4 best challenger; v1 worst. Champion v0 placeholder.
    scalars = {"v0": 1.0, "v1": 0.9, "v2": 0.8, "v3": 0.5, "v4": 0.2}
    dec = _run_strategy(s, champ, challengers, scalars)
    # Best challenger should reach the final and clear the gate.
    assert dec.promoted_generation_id == "v4"
    assert dec.crowning_matchup_id == "racing-final"


def test_racing_rung_uses_board_subset_then_full_board() -> None:
    s = make_strategy(_racing_spec(board_fraction=0.5))
    s.seed(_champion("v0"), [_challenger(f"v{i}") for i in (1, 2, 3, 4)])
    first = s.next_matchups()
    # Rung-0 duels run on a board SUBSET (half of 4 = 2 entries).
    assert first
    assert all(m.board_subset is not None for m in first)
    assert all(len(m.board_subset) == 2 for m in first)


def test_racing_final_runs_full_board_gate() -> None:
    s = make_strategy(_racing_spec())
    champ = _champion("v0")
    challengers = [_challenger(f"v{i}") for i in (1, 2, 3, 4)]
    scalars = {"v0": 1.0, "v1": 0.9, "v2": 0.8, "v3": 0.5, "v4": 0.2}
    _run_strategy(s, champ, challengers, scalars)
    # The crowning rung record should carry a full-board fraction.
    gate_rounds = [r for r in s.rounds() if r.label == "Champion gate"]
    assert gate_rounds
    assert gate_rounds[0].matches[0].board_fraction == 1.0


def test_racing_records_cut_and_survivors() -> None:
    s = make_strategy(_racing_spec())
    champ = _champion("v0")
    challengers = [_challenger(f"v{i}") for i in (1, 2, 3, 4)]
    scalars = {"v0": 1.0, "v1": 0.9, "v2": 0.8, "v3": 0.5, "v4": 0.2}
    _run_strategy(s, champ, challengers, scalars)
    rung_records = [m for r in s.rounds() for m in r.matches if m.match_id.startswith("rung")]
    assert rung_records
    # At least one rung must have eliminated someone.
    assert any(m.cut for m in rung_records)


def test_racing_defaults_board_ids_to_epoch_board_when_absent() -> None:
    # No board_ids in the spec params: make_strategy injects the epoch's
    # full board, so the rungs slice it (CLI-flag form works out of the box).
    spec = TournamentStructure(structure="racing", params={"field_size": 4, "eta": 2})
    assert "board_ids" not in spec.params
    epoch_board = ["e1", "e2", "e3", "e4"]
    s = make_strategy(spec, board_ids=epoch_board)
    s.seed(_champion("v0"), [_challenger(f"v{i}") for i in (1, 2, 3, 4)])
    first = s.next_matchups()
    # The strategy now knows the board: rung-0 runs on a real subset, not
    # the whole-board fallback (board_subset would be None if ids were absent).
    assert first
    assert all(m.board_subset is not None for m in first)
    # Default board_fraction 0.25 over 4 entries ⇒ ceil(1) = 1 entry at rung 0.
    assert all(len(m.board_subset) == 1 for m in first)
    assert all(set(m.board_subset) <= set(epoch_board) for m in first)


def test_racing_explicit_board_ids_override_the_epoch_default() -> None:
    # An explicit board_ids subset must win over the injected epoch board.
    subset = ["e2", "e4", "e6", "e8"]
    spec = TournamentStructure(
        structure="racing",
        params={"field_size": 4, "eta": 2, "board_fraction": 0.5, "board_ids": subset},
    )
    # The wider epoch board (8 ids) must NOT leak in; the operator pinned 4.
    s = make_strategy(spec, board_ids=[f"e{i}" for i in range(1, 9)])
    s.seed(_champion("v0"), [_challenger(f"v{i}") for i in (1, 2, 3, 4)])
    first = s.next_matchups()
    assert first
    # Rung-0 slice = ceil(0.5 * 4) = 2, drawn from the operator's subset
    # (in order), not from the wider epoch board.
    assert all(tuple(m.board_subset) == ("e2", "e4") for m in first)


def test_racing_no_budget_leaves_matchups_uncapped() -> None:
    # Default racing: no budget param ⇒ every scheduled matchup is uncapped,
    # backwards-compatible with the unbounded run.
    s = make_strategy(_racing_spec())
    s.seed(_champion("v0"), [_challenger(f"v{i}") for i in (1, 2, 3, 4)])
    rung = s.next_matchups()
    assert rung
    assert all(m.matchup_budget_seconds is None for m in rung)


def test_racing_matchup_budget_stamped_on_rungs_and_final() -> None:
    # A matchup_budget_seconds param caps EVERY duel (rungs + final).
    s = make_strategy(_racing_spec(matchup_budget_seconds=120.0))
    champ = _champion("v0")
    challengers = [_challenger(f"v{i}") for i in (1, 2, 3, 4)]
    s.seed(champ, challengers)
    scalars = {"v0": 1.0, "v1": 0.9, "v2": 0.8, "v3": 0.5, "v4": 0.2}
    rungs, final = _run_strategy_to_final(s, scalars)
    assert rungs
    assert all(m.matchup_budget_seconds == 120.0 for m in rungs)
    assert final is not None
    assert final.matchup_budget_seconds == 120.0


def test_racing_final_rung_budget_overrides_for_crowning_duel() -> None:
    # final_rung_budget_seconds overrides matchup_budget_seconds for the
    # final full-board crowning duel specifically (the grind to bound),
    # while the rungs keep the general matchup cap.
    s = make_strategy(_racing_spec(matchup_budget_seconds=120.0, final_rung_budget_seconds=600.0))
    champ = _champion("v0")
    challengers = [_challenger(f"v{i}") for i in (1, 2, 3, 4)]
    s.seed(champ, challengers)
    scalars = {"v0": 1.0, "v1": 0.9, "v2": 0.8, "v3": 0.5, "v4": 0.2}
    rungs, final = _run_strategy_to_final(s, scalars)
    assert all(m.matchup_budget_seconds == 120.0 for m in rungs)
    assert final is not None
    assert final.matchup_budget_seconds == 600.0


def test_racing_final_rung_budget_alone_only_caps_the_final() -> None:
    # final_rung_budget_seconds without matchup_budget_seconds caps ONLY the
    # crowning duel; the rungs stay uncapped.
    s = make_strategy(_racing_spec(final_rung_budget_seconds=300.0))
    champ = _champion("v0")
    challengers = [_challenger(f"v{i}") for i in (1, 2, 3, 4)]
    s.seed(champ, challengers)
    scalars = {"v0": 1.0, "v1": 0.9, "v2": 0.8, "v3": 0.5, "v4": 0.2}
    rungs, final = _run_strategy_to_final(s, scalars)
    assert all(m.matchup_budget_seconds is None for m in rungs)
    assert final is not None
    assert final.matchup_budget_seconds == 300.0


def test_make_strategy_without_board_ids_leaves_params_untouched() -> None:
    # Board-agnostic call (gauntlet path, no board passed): params unchanged,
    # so the gauntlet single-duel path stays byte-equivalent.
    spec = TournamentStructure(structure="racing", params={"field_size": 4})
    s = make_strategy(spec)
    assert "board_ids" not in s.params


# ---------------------------------------------------------------------------
# Degeneracy + registry
# ---------------------------------------------------------------------------


def test_unknown_structure_raises_listing_valid_keys() -> None:
    with pytest.raises(ValueError) as exc:
        TournamentStructure(structure="nope")
    assert "gauntlet" in str(exc.value)


def test_field_size_one_degrades_any_structure_to_gauntlet() -> None:
    # A single-elim with field_size 1 has one challenger: bracket survivor
    # is that challenger, then the champion-gate final decides — exactly
    # the gauntlet's single full-board duel.
    s = make_strategy(TournamentStructure(structure="single_elim", params={"field_size": 1}))
    dec = _run_strategy(s, _champion("v0"), [_challenger("v1")], {"v0": 1.0, "v1": 0.5})
    assert dec.promoted_generation_id == "v1"


# ---------------------------------------------------------------------------
# The driver
# ---------------------------------------------------------------------------


def test_resolve_tournament_drives_gauntlet() -> None:
    s = make_strategy(TournamentStructure(structure="gauntlet"))
    champ = _champion("v0")
    challenger = _challenger("v1")

    async def request_field(n: int):
        assert n == 1
        return champ, [challenger]

    async def run_matchup(m: Matchup) -> MatchupResult:
        return _result(m, left_scalar=1.0, right_scalar=0.4)

    dec = asyncio.run(resolve_tournament(s, request_field=request_field, run_matchup=run_matchup))
    assert dec.promoted_generation_id == "v1"


def test_resolve_tournament_drives_single_elim_to_completion() -> None:
    s = make_strategy(TournamentStructure(structure="single_elim", params={"field_size": 4}))
    champ = _champion("v0")
    challengers = [_challenger(f"v{i}") for i in (1, 2, 3, 4)]
    scalars = {"v0": 1.0, "v1": 0.9, "v2": 0.8, "v3": 0.3, "v4": 0.7}

    async def request_field(n: int):
        assert n == 4
        return champ, challengers

    async def run_matchup(m: Matchup) -> MatchupResult:
        return _result(
            m,
            left_scalar=scalars[m.left.generation_id],
            right_scalar=scalars[m.right.generation_id],
        )

    dec = asyncio.run(resolve_tournament(s, request_field=request_field, run_matchup=run_matchup))
    assert dec.promoted_generation_id == "v3"
    assert dec.decision == "promoted"
