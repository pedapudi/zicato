"""ON-path tests for the Bradley--Terry promotion pre-gate (opt-in).

The pre-gate (:mod:`zicato.selection.evidence_gate`) is DEFAULT-OFF: with
``promote_confidence_threshold`` unset every selection decision is byte-
identical to today (proven by the parity suite). These tests exercise it ON, to
prove the new capability works:

* a deferred verdict on a noisy near-tie,
* the closest-CI replication schedule,
* the inconclusive terminal verdict + its dead-letter record,
* the rating-block shape,
* the credibility floor (no override below the minimum duel count).

All synthetic — no live runs.
"""

from __future__ import annotations

from zicato.selection.dead_letter import (
    InconclusiveRecord,
    list_inconclusive,
    read_inconclusive,
    record_inconclusive,
)
from zicato.selection.evidence_gate import (
    DEFAULT_REPLICATE_BUDGET,
    MIN_CREDIBLE_DUELS,
    closest_ci_duel,
    evidence_verdict,
    rating_block,
    read_promote_confidence_threshold,
    read_replicate_budget,
)
from zicato.selection.strategy import MatchupResult
from zicato.tournament.gate import GateOutcome

# ---------------------------------------------------------------------------
# Synthetic audit helpers
# ---------------------------------------------------------------------------


def _duel(parent: str, child: str, *, child_won: bool, margin: float = 0.5) -> MatchupResult:
    """One parent-vs-child duel; ``child`` is ``right``, so a child win is a
    NEGATIVE ``delta_scalar`` (right - left)."""
    if child_won:
        delta = -margin
        left_scalar, right_scalar = 1.0, 1.0 - margin
        decision = "promoted"
    else:
        delta = margin
        left_scalar, right_scalar = 1.0 - margin, 1.0
        decision = "rejected"
    return MatchupResult(
        matchup_id="m",
        left_id=parent,
        right_id=child,
        left_agg={"scalar": left_scalar},
        right_agg={"scalar": right_scalar},
        outcome=GateOutcome(decision, "", delta_scalar=delta, delta_pass_rate=0.0),
    )


def _audit(parent: str, child: str, *, child_wins: int, parent_wins: int) -> list[MatchupResult]:
    out = [_duel(parent, child, child_won=True) for _ in range(child_wins)]
    out += [_duel(parent, child, child_won=False) for _ in range(parent_wins)]
    return out


# ---------------------------------------------------------------------------
# Param readers
# ---------------------------------------------------------------------------


def test_threshold_reader_defaults_to_none() -> None:
    assert read_promote_confidence_threshold({}) is None
    assert read_promote_confidence_threshold({"promote_confidence_threshold": None}) is None
    # Out of range / non-numeric ⇒ no pre-gate (safe degrade).
    assert read_promote_confidence_threshold({"promote_confidence_threshold": 0.0}) is None
    assert read_promote_confidence_threshold({"promote_confidence_threshold": 1.0}) is None
    assert read_promote_confidence_threshold({"promote_confidence_threshold": "x"}) is None


def test_threshold_reader_accepts_valid() -> None:
    assert read_promote_confidence_threshold({"promote_confidence_threshold": 0.9}) == 0.9


def test_replicate_budget_reader() -> None:
    assert read_replicate_budget({}) == DEFAULT_REPLICATE_BUDGET
    assert read_replicate_budget({"promote_confidence_replicates": 5}) == 5
    assert read_replicate_budget({"promote_confidence_replicates": 0}) == 0
    # Bad values fall back to the default.
    assert read_replicate_budget({"promote_confidence_replicates": -2}) == DEFAULT_REPLICATE_BUDGET
    assert read_replicate_budget({"promote_confidence_replicates": "nope"}) == (
        DEFAULT_REPLICATE_BUDGET
    )


# ---------------------------------------------------------------------------
# Deferred verdict
# ---------------------------------------------------------------------------


def test_noisy_near_tie_defers() -> None:
    # A coin-flip record: the child is not confidently stronger and the CIs
    # overlap → defer (budget remains), not promote.
    audit = _audit("v0", "v1", child_wins=3, parent_wins=3)
    v = evidence_verdict(
        "promoted",
        "",
        audit=audit,
        parent_id="v0",
        child_id="v1",
        threshold=0.9,
        replicate_budget=3,
        replicates_spent=0,
    )
    assert v.decision == "deferred"
    assert v.credible is True
    assert v.ci_overlap is True
    assert v.p_stronger is not None and v.p_stronger < 0.9
    assert "deferred" in v.reason


def test_clearly_separated_win_promotes() -> None:
    # A heavily-replicated lopsided record: the child wins every duel often
    # enough that even the prior-regularised Fisher SEs separate the two 95%
    # CIs. Both the probability bar AND CI clearance are met → crown on
    # evidence. (The CI-clearance arm is the binding constraint on a tiny
    # two-contestant field — which is exactly why the replicate loop exists.)
    audit = _audit("v0", "v1", child_wins=40, parent_wins=0)
    v = evidence_verdict(
        "promoted",
        "",
        audit=audit,
        parent_id="v0",
        child_id="v1",
        threshold=0.9,
        replicate_budget=3,
    )
    assert v.decision == "promoted"
    assert v.credible is True
    assert v.ci_overlap is False
    assert v.p_stronger is not None and v.p_stronger >= 0.9


def test_below_min_duels_is_not_credible_and_passes_through() -> None:
    # Fewer than MIN_CREDIBLE_DUELS resolved pair duels ⇒ no trustworthy fit;
    # the gate's verdict stands unchanged (no override on noise).
    assert MIN_CREDIBLE_DUELS == 3
    audit = _audit("v0", "v1", child_wins=2, parent_wins=0)
    v = evidence_verdict(
        "promoted",
        "ok",
        audit=audit,
        parent_id="v0",
        child_id="v1",
        threshold=0.9,
        replicate_budget=3,
    )
    assert v.decision == "promoted"
    assert v.credible is False
    assert v.reason == "ok"


def test_non_promote_passes_through_unchanged() -> None:
    # The pre-gate only ever holds a promotion — a reject is returned as-is.
    audit = _audit("v0", "v1", child_wins=10, parent_wins=0)
    v = evidence_verdict(
        "rejected",
        "did not clear",
        audit=audit,
        parent_id="v0",
        child_id="v1",
        threshold=0.9,
        replicate_budget=3,
    )
    assert v.decision == "rejected"
    assert v.reason == "did not clear"
    assert v.credible is False


# ---------------------------------------------------------------------------
# Inconclusive terminal verdict
# ---------------------------------------------------------------------------


def test_exhausted_budget_goes_inconclusive() -> None:
    # Coin-flip record + the budget already spent ⇒ terminal inconclusive.
    audit = _audit("v0", "v1", child_wins=3, parent_wins=3)
    v = evidence_verdict(
        "promoted",
        "",
        audit=audit,
        parent_id="v0",
        child_id="v1",
        threshold=0.9,
        replicate_budget=2,
        replicates_spent=2,
    )
    assert v.decision == "inconclusive"
    assert v.ci_overlap is True
    assert "inconclusive" in v.reason
    assert "dead-letter" in v.reason


# ---------------------------------------------------------------------------
# Closest-CI replication schedule
# ---------------------------------------------------------------------------


def test_closest_ci_duel_picks_the_tightest_pairing() -> None:
    # Two pairings: (a,b) is a near-tie (overlapping CIs), (a,c) is lopsided
    # (separated). The closest-CI duel — the cheapest replicate — is (a,b).
    audit: list[MatchupResult] = []
    audit += _audit("a", "b", child_wins=3, parent_wins=3)  # near-tie
    # a clearly beats c (a is "right"? use child=a so a wins): build c-vs-a
    audit += _audit("c", "a", child_wins=8, parent_wins=0)  # a >> c
    cand = closest_ci_duel(audit)
    assert cand is not None
    assert {cand.left_id, cand.right_id} == {"a", "b"}


def test_closest_ci_duel_restrict_to_pins_the_crowning_pair() -> None:
    audit: list[MatchupResult] = []
    audit += _audit("a", "b", child_wins=3, parent_wins=3)
    audit += _audit("c", "a", child_wins=8, parent_wins=0)
    cand = closest_ci_duel(audit, restrict_to=("c", "a"))
    assert cand is not None
    assert {cand.left_id, cand.right_id} == {"a", "c"}


def test_closest_ci_duel_empty_audit_is_none() -> None:
    assert closest_ci_duel([]) is None


# ---------------------------------------------------------------------------
# Rating block shape
# ---------------------------------------------------------------------------


def test_rating_block_shape() -> None:
    audit = _audit("v0", "v1", child_wins=3, parent_wins=3)
    v = evidence_verdict(
        "promoted",
        "",
        audit=audit,
        parent_id="v0",
        child_id="v1",
        threshold=0.9,
        replicate_budget=3,
    )
    block = rating_block(v)
    assert block["present"] is True
    assert set(block) == {
        "present",
        "credible",
        "champion",
        "challenger",
        "p_stronger",
        "threshold",
        "ci_overlap",
        "decision",
        "replicates_spent",
        "n_duels",
    }
    for side in ("champion", "challenger"):
        assert set(block[side]) == {"theta", "se", "ci_lo", "ci_hi"}
        assert block[side]["ci_lo"] <= block[side]["ci_hi"]
    assert block["threshold"] == 0.9
    assert block["n_duels"] == 6


# ---------------------------------------------------------------------------
# Dead-letter queue
# ---------------------------------------------------------------------------


def test_dead_letter_round_trips(tmp_path) -> None:
    ws = tmp_path / ".zicato"
    audit = _audit("v0", "v1", child_wins=3, parent_wins=3)
    v = evidence_verdict(
        "promoted",
        "",
        audit=audit,
        parent_id="v0",
        child_id="v1",
        threshold=0.9,
        replicate_budget=2,
        replicates_spent=2,
    )
    assert v.decision == "inconclusive"
    record = InconclusiveRecord(
        generation_id="v1",
        champion_id="v0",
        epoch_id="ep1",
        rating=rating_block(v),
        ci_history=({"p_stronger": v.p_stronger, "ci_overlap": v.ci_overlap},),
        reason=v.reason,
    )
    path = record_inconclusive(ws, record)
    assert path.exists()

    got = read_inconclusive(ws, "v1")
    assert got is not None
    assert got["generation_id"] == "v1"
    assert got["champion_id"] == "v0"
    assert got["epoch_id"] == "ep1"
    assert got["rating"]["present"] is True
    assert got["ci_history"][0]["ci_overlap"] is True

    listed = list_inconclusive(ws)
    assert len(listed) == 1
    assert listed[0]["generation_id"] == "v1"


def test_dead_letter_absent_returns_none(tmp_path) -> None:
    ws = tmp_path / ".zicato"
    assert read_inconclusive(ws, "missing") is None
    assert list_inconclusive(ws) == []
