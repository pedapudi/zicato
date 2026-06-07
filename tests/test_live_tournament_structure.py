"""The LIVE in-flight tournament structure (Task 1).

These pin the new shared live projection:

* ``SelectionStrategy.live_rounds()`` / ``live_standings()`` carry the
  settled rounds PLUS the current scheduled-but-unresolved round (its
  matches with ``winner=""`` / ``pending=True``) and the standings-so-far,
  for swiss AND single_elim mid-resolution;
* the consolidated serialisers (``_serialise_rounds`` /
  ``_serialise_standings``) yield byte-identical shapes for the live,
  settled, and durable-record producers — so the dashboard renderers work
  identically live and post-run.

The strategies are driven directly (no orchestrator / harness) so the
in-flight state is deterministic.
"""

from __future__ import annotations

from zicato.core.types import TournamentStructure
from zicato.orchestrator import _serialise_rounds, _serialise_standings
from zicato.selection import make_strategy
from zicato.selection.strategy import Contestant, MatchupResult
from zicato.tournament.gate import GateOutcome


def _agg(scalar: float) -> dict[str, float]:
    return {"scalar": scalar}


def _result(matchup, *, left_scalar: float, right_scalar: float) -> MatchupResult:
    delta = right_scalar - left_scalar
    decision = "promoted" if delta < 0 else "rejected"
    return MatchupResult(
        matchup_id=matchup.matchup_id,
        left_id=matchup.left.generation_id,
        right_id=matchup.right.generation_id,
        left_agg=_agg(left_scalar),
        right_agg=_agg(right_scalar),
        outcome=GateOutcome(
            decision=decision,  # type: ignore[arg-type]
            reason="" if decision == "promoted" else "reject",
            delta_scalar=delta,
            delta_pass_rate=0.0,
        ),
        stage_index=matchup.stage_index,
        bracket_slot=matchup.bracket_slot,
    )


def _seed(structure: str, field_size: int, **params):
    strategy = make_strategy(
        TournamentStructure(structure=structure, params={"field_size": field_size, **params})
    )
    champion = Contestant(generation_id="v0", role="champion")
    challengers = [
        Contestant(generation_id=f"v{i}", role="challenger") for i in range(1, field_size + 1)
    ]
    strategy.seed(champion, challengers)
    return strategy


# ---------------------------------------------------------------------------
# (a) live envelope carries in-flight rounds + standings mid-run
# ---------------------------------------------------------------------------


def test_swiss_live_rounds_carry_in_flight_round() -> None:
    strategy = _seed("swiss", 3, rounds_n=2)
    # Schedule the first Swiss round but DO NOT record any result yet.
    batch = strategy.next_matchups()
    assert batch, "swiss should schedule a first round"

    live = _serialise_rounds(strategy.live_rounds())
    # The settled view is still empty — nothing has resolved.
    assert _serialise_rounds(strategy.rounds()) == []
    # The live view carries exactly the in-flight round.
    assert len(live) == 1
    pending_matches = [m for m in live[0]["matches"] if m["pending"]]
    assert pending_matches, "the in-flight round must carry pending matches"
    for m in pending_matches:
        assert m["winner"] is None, "an in-flight match has winner: null"
        assert m["pending"] is True
        assert len(m["competitors"]) == 2

    standings = _serialise_standings(strategy.live_standings())
    assert standings, "live standings should rank the field-so-far"
    assert {s["generation_id"] for s in standings} >= {"v0", "v1", "v2", "v3"}
    # No generation is crowned yet (champion gate has not run).
    assert all(s["status"] != "champion" for s in standings)


def test_single_elim_live_rounds_carry_in_flight_round() -> None:
    strategy = _seed("single_elim", 4)
    batch = strategy.next_matchups()
    assert batch, "single_elim should schedule a first bracket round"

    live = _serialise_rounds(strategy.live_rounds())
    assert _serialise_rounds(strategy.rounds()) == []
    assert len(live) == 1
    pending = [m for m in live[0]["matches"] if m["pending"]]
    assert pending
    for m in pending:
        assert m["winner"] is None
        assert m["pending"] is True
        assert m["bracket_slot"], "an elim bracket match carries a slot"

    standings = _serialise_standings(strategy.live_standings())
    assert {s["generation_id"] for s in standings} >= {"v0", "v1", "v2", "v3", "v4"}


def test_live_rounds_append_settled_plus_pending_across_rounds() -> None:
    """After round 1 settles, the live view shows it settled + the next pending."""
    strategy = _seed("swiss", 3, rounds_n=2)
    batch = strategy.next_matchups()
    scalars = {"v0": 1.0, "v1": 0.5, "v2": 0.9, "v3": 0.7}
    for m in batch:
        strategy.record_result(
            _result(
                m,
                left_scalar=scalars[m.left.generation_id],
                right_scalar=scalars[m.right.generation_id],
            )
        )
    # Schedule round 2 (now pending) WITHOUT recording it.
    batch2 = strategy.next_matchups()
    assert batch2

    live = _serialise_rounds(strategy.live_rounds())
    settled = _serialise_rounds(strategy.rounds())
    assert len(settled) == 1, "round 1 has settled"
    assert all(not m["pending"] for m in settled[0]["matches"])
    assert len(live) == 2, "settled round 1 + in-flight round 2"
    assert live[0] == settled[0], "the settled prefix is byte-identical in the live view"
    assert any(m["pending"] for m in live[1]["matches"]), "round 2 is in-flight"


# ---------------------------------------------------------------------------
# (b) consolidated serialisation: identical shape live vs settle vs durable
# ---------------------------------------------------------------------------


def _run_to_settle(structure: str, field_size: int, scalars: dict[str, float], **params):
    strategy = _seed(structure, field_size, **params)
    guard = 0
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
        guard += 1
        assert guard < 100
    return strategy, strategy.champion()


def test_settled_live_and_durable_share_one_shape() -> None:
    """At resolution, live_rounds == rounds (no pending), and the keys match.

    The live, settled, and durable producers all flow through the same
    ``_serialise_rounds`` / ``_serialise_standings`` — so the contract keys
    are identical and a resolved strategy's live view carries no pending
    matches (it equals the settled view).
    """
    scalars = {"v0": 1.0, "v1": 0.3, "v2": 0.8, "v3": 0.5}
    strategy, decision = _run_to_settle("swiss", 3, scalars, rounds_n=2)

    settled_rounds = _serialise_rounds(strategy.rounds())
    live_rounds = _serialise_rounds(strategy.live_rounds())
    # Resolved ⇒ no pending round ⇒ live == settled, byte for byte.
    assert live_rounds == settled_rounds
    assert settled_rounds, "a settled swiss carries rounds"
    assert all(not m["pending"] for r in settled_rounds for m in r["matches"])

    # Every match dict carries the full contract key set.
    expected_keys = {
        "match_id",
        "competitors",
        "winner",
        "decision",
        "delta_scalar",
        "bracket_slot",
        "bye",
        "survivors",
        "cut",
        "board_fraction",
        "pending",
    }
    for r in settled_rounds:
        for m in r["matches"]:
            assert set(m.keys()) == expected_keys

    settled_standings = _serialise_standings(decision.standings)
    live_standings = _serialise_standings(strategy.live_standings())
    std_keys = {"generation_id", "rank", "scalar", "wins", "losses", "status", "role"}
    for s in settled_standings + live_standings:
        assert set(s.keys()) == std_keys
