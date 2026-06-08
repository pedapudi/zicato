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

from pathlib import Path

from zicato.core.types import TournamentStructure
from zicato.orchestrator import (
    _overlay_projected_live_progress,
    _serialise_rounds,
    _serialise_standings,
)
from zicato.runtime.state import (
    ActiveTournament,
    update_tournament_projected,
    write_active_tournament,
)
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
        "live_progress",
    }
    for r in settled_rounds:
        for m in r["matches"]:
            assert set(m.keys()) == expected_keys

    settled_standings = _serialise_standings(decision.standings)
    live_standings = _serialise_standings(strategy.live_standings())
    std_keys = {"generation_id", "rank", "scalar", "wins", "losses", "status", "role"}
    for s in settled_standings + live_standings:
        assert set(s.keys()) == std_keys


# ---------------------------------------------------------------------------
# (c) racing per-lane live_progress: producer/consumer parity (B1)
# ---------------------------------------------------------------------------


def _racing(field_size: int, **params):
    strategy = make_strategy(
        TournamentStructure(
            structure="racing",
            params={
                "field_size": field_size,
                "board_ids": [f"e{i}" for i in range(1, 9)],
                "eta": 2,
                "board_fraction": 0.25,
                **params,
            },
        )
    )
    champion = Contestant(generation_id="v0", role="champion")
    challengers = [
        Contestant(generation_id=f"v{i}", role="challenger") for i in range(1, field_size + 1)
    ]
    strategy.seed(champion, challengers)
    return strategy, champion, challengers


def _rung_live_progress(live_rounds: list[dict]) -> dict[str, dict]:
    """The in-flight rung's per-lane live_progress (off matches[0])."""
    pending_round = next((r for r in live_rounds if any(m["pending"] for m in r["matches"])), None)
    assert pending_round is not None, "an in-flight rung must be present"
    first = pending_round["matches"][0]
    return first["live_progress"]


def test_racing_inflight_rung_carries_live_progress_for_every_lane() -> None:
    # queued → in-flight: schedule rung 0 but record nothing yet. The in-flight
    # rung must carry a live_progress entry for the champion + EVERY challenger
    # lane racing this rung.
    strategy, champion, challengers = _racing(4)
    batch = strategy.next_matchups()
    assert batch, "racing schedules a rung-0 batch"

    live = _serialise_rounds(strategy.live_rounds())
    assert _serialise_rounds(strategy.rounds()) == [], "nothing has settled yet"
    progress = _rung_live_progress(live)

    expected_lanes = {"v0", "v1", "v2", "v3", "v4"}
    assert set(progress) == expected_lanes, "every lane in the rung has live_progress"
    for lane in progress.values():
        assert lane["inflight"] == 1
        # board_total = the rung's board slice; boards_done is filled by the
        # runner overlay (not yet present pre-overlay), but never exceeds total.
        assert lane["boards_total"] >= 1
        bd = lane.get("boards_done", 0)
        assert 0 <= bd <= lane["boards_total"], "boards_done ≤ boards_total"


def test_racing_live_progress_seeds_projected_scalar_after_a_rung_settles() -> None:
    # rung-settled → next rung in-flight: after rung 0 cuts, the survivors carry
    # their last-known running scalar vs the champion as a projected_scalar.
    strategy, champion, challengers = _racing(4)
    batch = strategy.next_matchups()
    scalars = {"v0": 1.0, "v1": 0.9, "v2": 0.8, "v3": 0.5, "v4": 0.2}
    for m in batch:
        strategy.record_result(
            _result(
                m,
                left_scalar=scalars[m.left.generation_id],
                right_scalar=scalars[m.right.generation_id],
            )
        )
    # Schedule the NEXT rung (now in-flight) without recording it.
    batch2 = strategy.next_matchups()
    assert batch2, "a survivor rung is scheduled"

    live = _serialise_rounds(strategy.live_rounds())
    settled = _serialise_rounds(strategy.rounds())
    # #16 invariant: the live view = settled rung 0 + the in-flight rung.
    assert len(settled) == 1, "rung 0 has settled"
    assert all(not m["pending"] for m in settled[0]["matches"])
    progress = _rung_live_progress(live)
    # The survivors carry their rung-0 scalar as the seeded projected_scalar.
    for gid, lane in progress.items():
        if gid == "v0":
            continue
        assert "projected_scalar" in lane, "a survivor seeds its last-known scalar"
        assert lane["projected"] is True
        assert lane["projected_scalar"] == scalars[gid]


def test_racing_overlay_folds_runner_projected_into_live_progress(tmp_path: Path) -> None:
    # some-boards-landed: the runner's per-board projected map is overlaid onto
    # the strategy-published live_progress topology IN PLACE.
    strategy, champion, challengers = _racing(4)
    strategy.next_matchups()  # schedule rung 0 (in-flight, no results yet)

    write_active_tournament(
        tmp_path,
        ActiveTournament(
            tournament_id="t",
            parent_generation_id="",
            child_generation_id="",
            epoch_id="e",
            started_at="x",
            structure="racing",
        ),
    )
    # The runner's incremental scorer has landed some boards for two lanes.
    update_tournament_projected(
        tmp_path,
        {
            "v0": {"scalar": 1.0, "boards_done": 1, "boards_total": 2, "pass_rate": 1.0},
            "v1": {"scalar": 0.3, "boards_done": 1, "boards_total": 2, "pass_rate": 1.0},
        },
    )

    live = _serialise_rounds(strategy.live_rounds())
    _overlay_projected_live_progress(live, tmp_path)
    progress = _rung_live_progress(live)

    # The overlaid lanes carry the runner's live boards_done + scalar; the
    # strategy's authoritative board-slice total is preserved (not clobbered).
    assert progress["v0"]["boards_done"] == 1
    assert progress["v0"]["projected_scalar"] == 1.0
    assert progress["v0"]["projected"] is True
    assert progress["v1"]["boards_done"] == 1
    assert progress["v1"]["projected_scalar"] == 0.3
    # boards_total stays the strategy's rung-slice size (overlay never shrinks it
    # to the per-duel projected total once the strategy set it).
    assert progress["v0"]["boards_total"] == strategy._rung_board_size()
    for lane in progress.values():
        if "boards_done" in lane:
            assert lane["boards_done"] <= lane["boards_total"]
    # A lane with no projected row yet keeps its topology untouched (graceful
    # fallback) — v2/v3/v4 had no board land, so no boards_done was injected.
    assert "boards_done" not in progress["v3"]


def test_racing_overlay_noop_without_projected(tmp_path: Path) -> None:
    # No active-tournament / no projected map → the strategy-published
    # live_progress is left exactly as-is (best-effort overlay).
    strategy, _champ, _chs = _racing(4)
    strategy.next_matchups()
    live = _serialise_rounds(strategy.live_rounds())
    before = _rung_live_progress(live)
    snapshot = {gid: dict(lane) for gid, lane in before.items()}
    _overlay_projected_live_progress(live, tmp_path)  # empty workspace
    after = _rung_live_progress(live)
    assert after == snapshot, "no projected map ⇒ live_progress unchanged"


def test_racing_live_arrived_fold_matches_overlay(tmp_path: Path) -> None:
    """FIX B: the per-board fold in ``update_tournament_projected`` produces
    the SAME rung ``live_progress`` as a fresh republish + overlay.

    The selection driver only republishes once per scheduled batch (before
    the rung runs), so the overlay path runs at rung START when nothing has
    landed. The fold path refreshes the rung as each board lands. This pins
    that the two paths CONVERGE: a live-arrived rung == republish + overlay.
    """
    strategy, champion, challengers = _racing(4)
    strategy.next_matchups()  # schedule rung 0 (in-flight)

    # Seed the active tournament with the strategy-published rung (the
    # republish path writes exactly this via _publish_active_tournament).
    live_rounds = _serialise_rounds(strategy.live_rounds())
    write_active_tournament(
        tmp_path,
        ActiveTournament(
            tournament_id="t",
            parent_generation_id="",
            child_generation_id="",
            epoch_id="e",
            started_at="x",
            structure="racing",
            competitors=[
                {"generation_id": "v0", "seed": 1, "role": "champion"},
                *[
                    {"generation_id": c.generation_id, "seed": i + 2, "role": "challenger"}
                    for i, c in enumerate(challengers)
                ],
            ],
            rounds=live_rounds,
        ),
    )

    # A board lands for the v1 duel → the FOLD path refreshes the rung.
    update_tournament_projected(
        tmp_path,
        {
            "v0": {"scalar": 1.0, "boards_done": 1, "boards_total": 2, "pass_rate": 1.0},
            "v1": {"scalar": 0.3, "boards_done": 1, "boards_total": 2, "pass_rate": 1.0},
        },
    )
    from zicato.runtime.state import read_active_tournament  # noqa: PLC0415

    folded = read_active_tournament(tmp_path)
    assert folded is not None
    fold_lanes = folded.rounds[0]["matches"][0]["live_progress"]

    # The OVERLAY path: a fresh strategy republish + the orchestrator overlay
    # over the same on-disk projected map.
    overlay_rounds = _serialise_rounds(strategy.live_rounds())
    _overlay_projected_live_progress(overlay_rounds, tmp_path)
    overlay_lanes = overlay_rounds[0]["matches"][0]["live_progress"]

    # The challenger lanes converge byte-for-byte.
    assert fold_lanes["v1"] == overlay_lanes["v1"]
    assert fold_lanes["v1"]["boards_done"] == 1
    assert fold_lanes["v1"]["projected_scalar"] == 0.3
    # The champion lane: the fold KEEPS the strategy benchmark (no scalar in
    # the strategy seed at rung 0 → no projected_scalar), gaining only
    # boards_done. The overlay (no champion-benchmark guard) would write the
    # per-duel scalar — so the fold is the stable one. Both agree on
    # boards_done + boards_total.
    assert fold_lanes["v0"]["boards_done"] == overlay_lanes["v0"]["boards_done"] == 1
    assert fold_lanes["v0"]["boards_total"] == overlay_lanes["v0"]["boards_total"]


def test_racing_settled_matches_carry_empty_live_progress() -> None:
    # Additive-only: a SETTLED racing match (a cut rung / the champion gate)
    # serialises live_progress as an empty dict — old readers ignore it and the
    # durable record is byte-unchanged in meaning.
    strategy, champion, challengers = _racing(4)
    scalars = {"v0": 1.0, "v1": 0.9, "v2": 0.8, "v3": 0.5, "v4": 0.2}
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
    settled = _serialise_rounds(strategy.rounds())
    assert settled, "a resolved racing carries settled rungs + the gate"
    for r in settled:
        for m in r["matches"]:
            assert m["live_progress"] == {}, "a settled match has no live_progress"
