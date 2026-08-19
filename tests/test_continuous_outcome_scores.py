"""Tests for Capability 1 — continuous per-entry outcome scores (#18).

The scoring chain is: a scorer (or bool predicate) -> ExpectationResult
(``score`` / ``metrics``) -> reducer (clamp + carry onto LossProfile) ->
tournament scalar (``mean_score``) -> gate (per-entry + aggregate scope).

The load-bearing invariant proved here: when every entry is bool
(``score=None``), the continuous chain is BYTE-IDENTICAL to the historical
binary path — same scalar, same gate decision. A continuous score then
moves the scalar smoothly with no threshold cliff.
"""

from __future__ import annotations

import math

from zicato.board.matchers import evaluate_expectation
from zicato.core import (
    DriftCount,
    Expectation,
    ExpectationKind,
    ExpectationResult,
    LossProfile,
    RunResult,
    ScoringWeights,
)
from zicato.telemetry.reducer import _continuous_score
from zicato.tournament.gate import (
    PER_ENTRY_SCORE_MONOTONICITY_TOLERANCE,
    evaluate_gate,
)
from zicato.tournament.scoring import aggregate_generation_score, entry_score

# ---------------------------------------------------------------------------
# Factories
# ---------------------------------------------------------------------------


def _loss(
    entry_id: str,
    *,
    drift_loss: float = 0.0,
    pass_fail: bool | None = None,
    score: float | None = None,
    metrics: dict[str, float] | None = None,
) -> LossProfile:
    expectation = (
        ExpectationResult(kind="predicate", passed=bool(pass_fail), score=score, metrics=metrics)
        if pass_fail is not None or score is not None
        else None
    )
    return LossProfile(
        run_id=f"run-{entry_id}",
        entry_id=entry_id,
        generation_id="v0",
        epoch_id="e0",
        drift_counts=(DriftCount(kind="off_topic", severity="info", count=0),),
        plan_revisions=0,
        task_failure_ratio=0.0,
        runtime_ms=1000,
        wall_clock_budget_exceeded=False,
        expectation_result=expectation,
        drift_loss=drift_loss,
        pass_fail=pass_fail,
        score=score,
        metrics=metrics,
    )


def _binary_agg(scalar: float, *, pass_rate: float, per_entry: dict[str, dict]) -> dict:
    """A pre-score aggregate carrying ONLY pass_rate + pass_fail rows.

    This is the shape the gate saw before this feature — no ``mean_score``,
    no per-entry ``score``. Used to prove the new gate reads it identically.
    """
    return {
        "drift_loss_mean": scalar,
        "pass_rate": pass_rate,
        "expectation_count": len(per_entry),
        "entry_count": len(per_entry),
        "scalar": scalar,
        "per_entry": per_entry,
    }


# ---------------------------------------------------------------------------
# (a) all-bool board: byte-identical scalar + gate decision
# ---------------------------------------------------------------------------


def test_all_bool_mean_score_equals_pass_rate_byte_for_byte() -> None:
    """On an all-bool board, mean_score == pass_rate exactly, so the scalar is identical."""
    losses = [
        _loss("a", drift_loss=0.0, pass_fail=True),
        _loss("b", drift_loss=2.0, pass_fail=False),
        _loss("c", drift_loss=1.0, pass_fail=True),
        _loss("d", drift_loss=0.5, pass_fail=None),  # no expectation
    ]
    weights = ScoringWeights(pass_weight=2.0)
    agg = aggregate_generation_score(losses, weights)

    # 2 of 3 expectation entries passed.
    assert agg["pass_rate"] == 2.0 / 3.0
    # mean_score is computed over scores (1,0,1) / 3 == pass_rate, bit-exact.
    assert agg["mean_score"] == agg["pass_rate"]
    # The pass component is identical to the historical (1 - pass_rate) form.
    expected_scalar = 1.0 * agg["drift_loss_mean"] + 2.0 * (1.0 - agg["pass_rate"])
    assert agg["scalar"] == expected_scalar


def test_all_bool_gate_decision_byte_identical_to_pre_score_agg() -> None:
    """The gate makes the SAME decision on a score-carrying agg and a pre-score agg."""
    weights = ScoringWeights(promote_margin=0.01, pass_rate_monotonicity=True)

    # Pre-score shape (no mean_score / no per-entry score).
    parent_old = _binary_agg(
        1.0, pass_rate=0.5, per_entry={"a": {"drift_loss": 0.0, "pass_fail": True}}
    )
    child_old = _binary_agg(
        0.5, pass_rate=1.0, per_entry={"a": {"drift_loss": 0.0, "pass_fail": True}}
    )
    old_outcome = evaluate_gate(parent_old, child_old, weights)

    # New shape produced by aggregate_generation_score for the SAME bools.
    parent_new = aggregate_generation_score(
        [_loss("a", drift_loss=0.0, pass_fail=True)],
        ScoringWeights(promote_margin=0.01),
    )
    parent_new["scalar"] = 1.0  # pin the scalars to match the hand-built case
    child_new = aggregate_generation_score(
        [_loss("a", drift_loss=0.0, pass_fail=True)],
        ScoringWeights(promote_margin=0.01),
    )
    child_new["scalar"] = 0.5
    new_outcome = evaluate_gate(parent_new, child_new, weights)

    assert old_outcome.decision == new_outcome.decision == "promoted"


def test_all_bool_per_entry_pass_to_fail_still_rejects() -> None:
    """A bool 1.0 -> 0.0 flip rejects under the continuous per-entry rule (must-still-pass)."""
    weights = ScoringWeights(promote_margin=0.0, pass_rate_monotonicity=True)
    parent = aggregate_generation_score([_loss("a", pass_fail=True)], weights)
    child = aggregate_generation_score([_loss("a", pass_fail=False)], weights)
    # Make the child win on scalar so only the monotonicity rule can reject.
    child["scalar"] = parent["scalar"] - 1.0
    outcome = evaluate_gate(parent, child, weights)
    assert outcome.decision == "rejected"
    assert "pass-rate regression on entries" in outcome.reason
    assert "a" in outcome.reason


# ---------------------------------------------------------------------------
# (b) a continuous score moves the scalar smoothly without a threshold cross
# ---------------------------------------------------------------------------


def test_continuous_score_moves_scalar_without_threshold_cross() -> None:
    """0.55 -> 0.70 F1 on one entry lowers the scalar with no pass/fail flip."""
    weights = ScoringWeights(pass_weight=2.0)
    before = aggregate_generation_score([_loss("a", score=0.55)], weights)
    after = aggregate_generation_score([_loss("a", score=0.70)], weights)

    # Both have the same drift and the same (truthy) pass bit — only the
    # continuous score differs, yet the scalar moves.
    assert before["mean_score"] == 0.55
    assert after["mean_score"] == 0.70
    # Higher score -> lower loss (pass component is pass_weight*(1-mean_score)).
    assert after["scalar"] < before["scalar"]
    # The delta is exactly pass_weight * (0.70 - 0.55), no cliff.
    assert math.isclose(before["scalar"] - after["scalar"], 2.0 * (0.70 - 0.55))


# ---------------------------------------------------------------------------
# (c) clamp / NaN handling — a rogue scorer cannot poison the scalar
# ---------------------------------------------------------------------------


def test_continuous_score_clamps_and_rejects_nonfinite() -> None:
    assert _continuous_score(ExpectationResult(kind="predicate", passed=True)) == 1.0
    assert _continuous_score(ExpectationResult(kind="predicate", passed=False)) == 0.0
    assert _continuous_score(ExpectationResult(kind="predicate", passed=True, score=1.5)) == 1.0
    assert _continuous_score(ExpectationResult(kind="predicate", passed=True, score=-0.3)) == 0.0
    assert _continuous_score(ExpectationResult(kind="predicate", passed=True, score=0.42)) == 0.42
    # NaN / inf are treated as a miss.
    assert (
        _continuous_score(ExpectationResult(kind="predicate", passed=True, score=float("nan")))
        == 0.0
    )
    assert (
        _continuous_score(ExpectationResult(kind="predicate", passed=True, score=float("inf")))
        == 0.0
    )


def test_aggregate_clamps_out_of_range_scores() -> None:
    """entry_score / mean_score clamp out-of-range and non-finite per-entry scores."""
    weights = ScoringWeights(namespace_weights={"drift:": 0.0, "failure:": 1.0}, pass_weight=1.0)
    agg = aggregate_generation_score(
        [
            _loss("hi", score=5.0),  # clamps to 1.0
            _loss("lo", score=-2.0),  # clamps to 0.0
            _loss("nan", score=float("nan")),  # -> 0.0
        ],
        weights,
    )
    # scores clamp to (1.0, 0.0, 0.0); mean == 1/3.
    assert math.isclose(agg["mean_score"], 1.0 / 3.0)
    assert entry_score(_loss("x", score=float("inf"))) == 0.0


# ---------------------------------------------------------------------------
# (d) per-entry continuous monotonicity: regression beyond tolerance rejects;
#     within tolerance passes
# ---------------------------------------------------------------------------


def test_per_entry_continuous_regression_beyond_tolerance_rejects() -> None:
    weights = ScoringWeights(promote_margin=0.0, pass_rate_monotonicity=True)
    drop = PER_ENTRY_SCORE_MONOTONICITY_TOLERANCE * 3.0
    parent = aggregate_generation_score([_loss("a", score=0.80)], weights)
    child = aggregate_generation_score([_loss("a", score=0.80 - drop)], weights)
    child["scalar"] = parent["scalar"] - 1.0  # win on scalar so only mono can reject
    outcome = evaluate_gate(parent, child, weights)
    assert outcome.decision == "rejected"
    assert "pass-rate regression on entries" in outcome.reason


def test_per_entry_continuous_dip_within_tolerance_promotes() -> None:
    weights = ScoringWeights(promote_margin=0.0, pass_rate_monotonicity=True)
    dip = PER_ENTRY_SCORE_MONOTONICITY_TOLERANCE / 2.0
    parent = aggregate_generation_score([_loss("a", score=0.80)], weights)
    child = aggregate_generation_score([_loss("a", score=0.80 - dip)], weights)
    child["scalar"] = parent["scalar"] - 1.0
    outcome = evaluate_gate(parent, child, weights)
    assert outcome.decision == "promoted"


# ---------------------------------------------------------------------------
# (e) aggregate scope runs on mean_score
# ---------------------------------------------------------------------------


def test_aggregate_scope_on_mean_score_rejects_net_score_drop() -> None:
    weights = ScoringWeights(
        promote_margin=0.0,
        pass_rate_monotonicity=True,
        pass_rate_monotonicity_scope="aggregate",
    )
    # Parent net 0.7; child net 0.5 — a net continuous regression even though
    # neither crossed a pass/fail threshold.
    parent = aggregate_generation_score([_loss("a", score=0.7)], weights)
    child = aggregate_generation_score([_loss("a", score=0.5)], weights)
    child["scalar"] = parent["scalar"] - 1.0  # win on scalar
    outcome = evaluate_gate(parent, child, weights)
    assert outcome.decision == "rejected"
    assert "overall pass-rate fell by" in outcome.reason


def test_aggregate_scope_allows_net_score_gain_trading_entries() -> None:
    weights = ScoringWeights(
        promote_margin=0.0,
        pass_rate_monotonicity=True,
        pass_rate_monotonicity_scope="aggregate",
    )
    parent = aggregate_generation_score([_loss("a", score=0.4), _loss("b", score=0.4)], weights)
    # Child trades b down but a up — net mean rises, so aggregate scope allows.
    child = aggregate_generation_score([_loss("a", score=0.9), _loss("b", score=0.1)], weights)
    child["scalar"] = parent["scalar"] - 1.0
    outcome = evaluate_gate(parent, child, weights)
    assert outcome.decision == "promoted"


# ---------------------------------------------------------------------------
# (f) the F1 scorer returns F1 + populates precision/recall in metrics
# ---------------------------------------------------------------------------


def test_f1_scorer_returns_f1_and_decomposition() -> None:
    from zicato_examples.target_1_presentation.predicates import (
        _precision_recall_f1,
        search_f1_score,
    )

    # retrieved = {1,2,3,4}, relevant = {1,2,5}: tp=2.
    retrieved = {"1", "2", "3", "4"}
    relevant = {"1", "2", "5"}
    precision, recall, f1 = _precision_recall_f1(retrieved, relevant)
    assert precision == 2 / 4
    assert recall == 2 / 3
    assert math.isclose(f1, 2 * precision * recall / (precision + recall))

    score, metrics = search_f1_score(retrieved, relevant)
    assert score == f1
    assert metrics == {"precision": precision, "recall": recall}

    # Over-retrieval (everything + noise) tanks precision, F1 < recall.
    over = {"1", "2", "5", "x", "y", "z"}
    p2, r2, f12 = _precision_recall_f1(over, relevant)
    assert r2 == 1.0  # found them all
    assert p2 == 0.5  # but half were noise
    assert f12 < r2  # F1 penalises the over-retrieval


def test_f1_scorer_edge_cases() -> None:
    from zicato_examples.target_1_presentation.predicates import _precision_recall_f1

    assert _precision_recall_f1(set(), set()) == (1.0, 1.0, 1.0)
    # pure over-retrieval against a no-op entry: precision 0, recall 1, F1 0.
    assert _precision_recall_f1({"a"}, set()) == (0.0, 1.0, 0.0)
    # returned nothing when there was something: precision 1 (vacuous), recall 0.
    assert _precision_recall_f1(set(), {"a"}) == (1.0, 0.0, 0.0)


# ---------------------------------------------------------------------------
# Scorer resolver seam: a PREDICATE dotted-path callable may return a float
# or a (float, metrics) tuple, in addition to a bool.
# ---------------------------------------------------------------------------


def _scorer_float(_result: RunResult) -> float:
    return 0.625


def _scorer_tuple(_result: RunResult) -> tuple[float, dict[str, float]]:
    return 0.4, {"precision": 0.3, "recall": 0.6}


def _scorer_bool(_result: RunResult) -> bool:
    return True


def _make_run_result() -> RunResult:
    return RunResult(
        run_id="r",
        entry_id="e",
        final_output="x",
        transcript=("x",),
        runtime_ms=1,
    )


async def _eval(spec: str) -> ExpectationResult:
    exp = Expectation(kind=ExpectationKind.PREDICATE, spec=spec)
    return await evaluate_expectation(exp, _make_run_result())


def test_scorer_seam_float_return() -> None:
    import asyncio

    res = asyncio.run(_eval(f"{__name__}:_scorer_float"))
    assert res.score == 0.625
    assert res.passed is True  # display-only: earned credit
    assert res.metrics is None


def test_scorer_seam_tuple_return() -> None:
    import asyncio

    res = asyncio.run(_eval(f"{__name__}:_scorer_tuple"))
    assert res.score == 0.4
    assert res.metrics == {"precision": 0.3, "recall": 0.6}


def test_scorer_seam_bool_return_stays_score_none() -> None:
    import asyncio

    res = asyncio.run(_eval(f"{__name__}:_scorer_bool"))
    assert res.passed is True
    assert res.score is None  # bool path leaves score None — byte-identical
    assert res.metrics is None
