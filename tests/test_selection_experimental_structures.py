"""Tests for the three tournament structures an operator must opt into.

``single_elim``, ``double_elim`` and ``swiss`` live in
``zicato.selection.experimental`` and resolve only when the contract sets
``experimental.tournament_structures`` to ``true``. The tests here drive
each structure's scheduling / advance / stopping on SYNTHETIC matchup
results, the way ``tests/test_selection_strategies.py`` drives the default
structures, and pin the opt-in itself: the refusal without it, the
resolution with it.
"""

from __future__ import annotations

import asyncio

import pytest

from tests.test_selection_strategies import (
    _challenger,
    _champion,
    _result,
    _run_strategy,
)
from zicato.core.scoring_config import ScoringWeights
from zicato.core.tournament import (
    EXPERIMENTAL_STRUCTURES_KEY,
    EXPERIMENTAL_TOURNAMENT_STRUCTURES,
    experimental_structure_refusal,
)
from zicato.core.types import ExperimentalConfig, TournamentStructure
from zicato.selection import (
    EXPERIMENTAL_STRATEGY_REGISTRY,
    STRATEGY_REGISTRY,
    Matchup,
    MatchupResult,
    make_strategy,
    resolve_tournament,
)


def _make(structure: str, params: dict[str, object] | None = None):
    """A strategy for an experimental structure under the opt-in."""
    return make_strategy(
        TournamentStructure(structure=structure, params=params or {}),
        experimental_structures=True,
    )


# ---------------------------------------------------------------------------
# The opt-in
# ---------------------------------------------------------------------------


def test_experimental_registry_matches_the_core_token_set() -> None:
    """The registry and the core token set name the same three structures."""
    assert set(EXPERIMENTAL_STRATEGY_REGISTRY) == EXPERIMENTAL_TOURNAMENT_STRUCTURES
    assert not set(STRATEGY_REGISTRY) & EXPERIMENTAL_TOURNAMENT_STRUCTURES


@pytest.mark.parametrize("structure", sorted(EXPERIMENTAL_TOURNAMENT_STRUCTURES))
def test_make_strategy_refuses_an_experimental_token_without_the_opt_in(structure: str) -> None:
    """Without the opt-in the registry names the token, its tier, and the key."""
    with pytest.raises(ValueError) as exc:
        make_strategy(TournamentStructure(structure=structure, params={"field_size": 2}))
    message = str(exc.value)
    assert message == experimental_structure_refusal(structure)
    assert structure in message
    assert "experimental" in message
    assert EXPERIMENTAL_STRUCTURES_KEY in message


@pytest.mark.parametrize("structure", sorted(EXPERIMENTAL_TOURNAMENT_STRUCTURES))
def test_opted_in_contract_resolves_each_experimental_structure(structure: str) -> None:
    """A contract that sets the flag constructs, and its structure resolves."""
    weights = ScoringWeights(
        tournament_structure=TournamentStructure(structure=structure, params={"field_size": 2}),
        experimental=ExperimentalConfig(tournament_structures=True),
    )
    strategy = make_strategy(
        weights.tournament_structure,
        experimental_structures=weights.experimental.tournament_structures,
    )
    assert isinstance(strategy, EXPERIMENTAL_STRATEGY_REGISTRY[structure])
    assert strategy.field_size() == 2


@pytest.mark.parametrize("structure", sorted(EXPERIMENTAL_TOURNAMENT_STRUCTURES))
def test_contract_refuses_an_experimental_structure_without_the_opt_in(structure: str) -> None:
    """The loader refuses at construction, with the registry's own wording."""
    with pytest.raises(ValueError, match=EXPERIMENTAL_STRUCTURES_KEY):
        ScoringWeights(tournament_structure=TournamentStructure(structure=structure))


# ---------------------------------------------------------------------------
# Single elimination
# ---------------------------------------------------------------------------


def test_single_elim_lowest_challenger_reaches_final_and_promotes() -> None:
    s = _make("single_elim", {"field_size": 4})
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
    s = _make("single_elim", {"field_size": 2})
    champ = _champion("v0")
    challengers = [_challenger("v1"), _challenger("v2")]
    # Champion is the strongest of all — the bracket survivor loses the final.
    scalars = {"v0": 0.1, "v1": 0.5, "v2": 0.7}
    dec = _run_strategy(s, champ, challengers, scalars)
    assert dec.promoted_generation_id is None
    assert dec.decision == "rejected"


def test_single_elim_defaults_to_replicated_duels() -> None:
    s = _make("single_elim", {"field_size": 2})
    s.seed(_champion("v0"), [_challenger("v1"), _challenger("v2")])
    batch = s.next_matchups()
    assert batch
    assert all(m.replicates >= 2 for m in batch)


def test_single_elim_odd_field_gets_a_bye() -> None:
    s = _make("single_elim", {"field_size": 3})
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
    s = _make("single_elim", {"field_size": 4})
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
    s = _make("double_elim", {"field_size": 4})
    champ = _champion("v0")
    challengers = [_challenger(f"v{i}") for i in (1, 2, 3, 4)]
    scalars = {"v0": 1.0, "v1": 0.9, "v2": 0.8, "v3": 0.2, "v4": 0.7}
    dec = _run_strategy(s, champ, challengers, scalars)
    assert dec.promoted_generation_id == "v3"
    assert dec.crowning_matchup_id == "GF"


def test_double_elim_eliminates_only_on_second_loss() -> None:
    s = _make("double_elim", {"field_size": 4})
    champ = _champion("v0")
    challengers = [_challenger(f"v{i}") for i in (1, 2, 3, 4)]
    scalars = {"v0": 1.0, "v1": 0.9, "v2": 0.8, "v3": 0.2, "v4": 0.7}
    _run_strategy(s, champ, challengers, scalars)
    # Every challenger that lost a WB match got a losers'-bracket "second
    # life": at least one LB match must have been scheduled (LB- prefix).
    assert any(r.left_id and r.matchup_id.startswith("LB-") for r in s._audit)


def test_double_elim_champion_stands_when_unbeaten() -> None:
    s = _make("double_elim", {"field_size": 2})
    champ = _champion("v0")
    challengers = [_challenger("v1"), _challenger("v2")]
    scalars = {"v0": 0.1, "v1": 0.5, "v2": 0.6}
    dec = _run_strategy(s, champ, challengers, scalars)
    assert dec.promoted_generation_id is None


# ---------------------------------------------------------------------------
# Swiss
# ---------------------------------------------------------------------------


def test_swiss_runs_configured_number_of_rounds() -> None:
    s = _make("swiss", {"field_size": 3, "rounds_n": 3})
    champ = _champion("v0")
    challengers = [_challenger("v1"), _challenger("v2"), _challenger("v3")]
    scalars = {"v0": 0.9, "v1": 0.4, "v2": 0.5, "v3": 0.6}
    _run_strategy(s, champ, challengers, scalars)
    swiss_rounds = [r for r in s.rounds() if r.label.startswith("Swiss")]
    assert len(swiss_rounds) == 3


def test_swiss_promotes_leader_only_if_it_clears_champion_gate() -> None:
    s = _make("swiss", {"field_size": 2, "rounds_n": 2})
    champ = _champion("v0")
    challengers = [_challenger("v1"), _challenger("v2")]
    # v1 is the best contestant overall and beats the champion at the gate.
    scalars = {"v0": 0.9, "v1": 0.2, "v2": 0.5}
    dec = _run_strategy(s, champ, challengers, scalars)
    assert dec.promoted_generation_id == "v1"
    assert dec.crowning_matchup_id == "swiss-final"


def test_swiss_leader_that_loses_champion_gate_is_not_crowned() -> None:
    s = _make("swiss", {"field_size": 2, "rounds_n": 2})
    champ = _champion("v0")
    challengers = [_challenger("v1"), _challenger("v2")]
    # The champion is the strongest — the Swiss leader loses the gate.
    scalars = {"v0": 0.1, "v1": 0.4, "v2": 0.6}
    dec = _run_strategy(s, champ, challengers, scalars)
    assert dec.promoted_generation_id is None
    assert dec.decision == "rejected"


def test_swiss_standings_rank_by_copeland() -> None:
    s = _make("swiss", {"field_size": 3, "rounds_n": 2})
    champ = _champion("v0")
    challengers = [_challenger("v1"), _challenger("v2"), _challenger("v3")]
    scalars = {"v0": 0.9, "v1": 0.2, "v2": 0.5, "v3": 0.6}
    dec = _run_strategy(s, champ, challengers, scalars)
    # v1 (lowest scalar) should win the most duels and rank first.
    assert dec.standings[0].generation_id == "v1"


def test_swiss_standings_count_a_loss_for_every_pairing_lost() -> None:
    s = _make("swiss", {"field_size": 3, "rounds_n": 2})
    champ = _champion("v0")
    challengers = [_challenger("v1"), _challenger("v2"), _challenger("v3")]
    # An even field pairs (v0,v1) and (v2,v3) in round 1 — v1 and v2 win.
    # Round 2 pairs the two winners and the two losers: v1 beats v2, v3
    # beats v0. Byes never occur, so wins and losses balance per round.
    scalars = {"v0": 0.9, "v1": 0.2, "v2": 0.5, "v3": 0.6}
    dec = _run_strategy(s, champ, challengers, scalars)
    tally = {r.generation_id: (r.wins, r.losses) for r in dec.standings}
    assert tally == {"v1": (2, 0), "v2": (1, 1), "v3": (1, 1), "v0": (0, 2)}


def test_field_size_one_degrades_any_structure_to_gauntlet() -> None:
    # A single-elim with field_size 1 has one challenger: bracket survivor
    # is that challenger, then the champion-gate final decides — exactly
    # the gauntlet's single full-board duel.
    s = _make("single_elim", {"field_size": 1})
    dec = _run_strategy(s, _champion("v0"), [_challenger("v1")], {"v0": 1.0, "v1": 0.5})
    assert dec.promoted_generation_id == "v1"


def test_resolve_tournament_drives_single_elim_to_completion() -> None:
    s = _make("single_elim", {"field_size": 4})
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
