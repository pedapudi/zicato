"""Tests for the Ladder governor (``zicato.tournament.ladder``, OVERFITTING.md §4 / §12 #2).

The Ladder mediates the per-epoch holdout query: it releases a new
holdout-based signal only when the *train-measured* improvement clears the
threshold beyond the noise band, re-reports the previous best within the band,
charges a per-epoch budget per query, and stops releasing once the budget is
exhausted. These tests pin the pure mechanism; the gate/runner wiring is
covered in ``test_tournament_runner.py`` / ``test_tournament_gate.py``.
"""

from __future__ import annotations

import pytest

from zicato.core import ScoringWeights
from zicato.core.types import LadderConfig
from zicato.tournament.ladder import (
    LadderState,
    effective_threshold,
    holdout_record,
    query_holdout,
)


def _weights(promote_margin: float = 0.1) -> ScoringWeights:
    return ScoringWeights(promote_margin=promote_margin)


def _query(
    state: LadderState,
    *,
    cfg: LadderConfig,
    weights: ScoringWeights,
    improvement: float,
    holdout_scalar: float,
    confirmed: bool,
):
    """Helper: a train-win whose improvement over the champion is ``improvement``."""
    return query_holdout(
        state,
        cfg=cfg,
        weights=weights,
        train_parent_scalar=1.0,
        train_child_scalar=1.0 - improvement,
        holdout_scalar=holdout_scalar,
        holdout_confirmed=confirmed,
    )


# ---------------------------------------------------------------------------
# Threshold derivation
# ---------------------------------------------------------------------------


def test_threshold_none_uses_promote_margin() -> None:
    cfg = LadderConfig(threshold=None, noise_scale=0.0)
    assert effective_threshold(cfg, _weights(promote_margin=0.1)) == pytest.approx(0.1)


def test_threshold_explicit_overrides_promote_margin() -> None:
    cfg = LadderConfig(threshold=0.25)
    assert effective_threshold(cfg, _weights(promote_margin=0.1)) == pytest.approx(0.25)


def test_threshold_adds_noise_band() -> None:
    cfg = LadderConfig(threshold=None, noise_scale=0.05)
    assert effective_threshold(cfg, _weights(promote_margin=0.1)) == pytest.approx(0.15)


# ---------------------------------------------------------------------------
# Release rule
# ---------------------------------------------------------------------------


def test_releases_on_threshold_clearing_improvement() -> None:
    cfg = LadderConfig(budget=8)
    weights = _weights(promote_margin=0.1)
    state = LadderState.seed(cfg)

    # Improvement of 0.2 clears the 0.1 threshold → released.
    rel = _query(
        state, cfg=cfg, weights=weights, improvement=0.2, holdout_scalar=0.5, confirmed=True
    )
    assert rel.released is True
    assert rel.confirmed is True
    assert rel.holdout_scalar == pytest.approx(0.5)
    assert rel.state.best_holdout_scalar == pytest.approx(0.5)
    assert rel.state.best_confirmed is True
    # Charged one unit of budget.
    assert rel.state.budget_remaining == 7


def test_released_nonconfirmation_is_surfaced() -> None:
    # A released query whose holdout did NOT confirm surfaces confirmed=False
    # (the runner flips the train-promote to a holdout reject).
    cfg = LadderConfig(budget=8)
    weights = _weights(promote_margin=0.1)
    rel = _query(
        LadderState.seed(cfg),
        cfg=cfg,
        weights=weights,
        improvement=0.5,
        holdout_scalar=2.0,
        confirmed=False,
    )
    assert rel.released is True
    assert rel.confirmed is False


def test_withholds_within_noise_band_and_reports_prior_best() -> None:
    cfg = LadderConfig(budget=8)
    weights = _weights(promote_margin=0.1)

    # First query: a clear improvement establishes the best (released).
    rel1 = _query(
        LadderState.seed(cfg),
        cfg=cfg,
        weights=weights,
        improvement=0.3,
        holdout_scalar=0.4,
        confirmed=True,
    )
    assert rel1.released is True
    assert rel1.state.best_holdout_scalar == pytest.approx(0.4)

    # Second query: improvement of 0.05 is WITHIN the 0.1 band → withheld.
    # The previous best confirmation is re-reported, not the round's raw bit.
    rel2 = _query(
        rel1.state,
        cfg=cfg,
        weights=weights,
        improvement=0.05,
        holdout_scalar=99.0,  # a noisy raw holdout we must NOT chase
        confirmed=False,  # raw bit says "not confirmed" — must be ignored
    )
    assert rel2.released is False
    assert rel2.confirmed is True  # the prior best, re-reported
    assert rel2.holdout_scalar == pytest.approx(0.4)  # prior best, not 99.0
    # The withheld query still charges the budget (we consulted the holdout).
    assert rel2.state.budget_remaining == rel1.state.budget_remaining - 1
    # The best is unchanged by a withheld query.
    assert rel2.state.best_holdout_scalar == pytest.approx(0.4)
    assert rel2.state.best_confirmed is True


def test_release_keeps_the_lower_best_holdout_scalar() -> None:
    cfg = LadderConfig(budget=8)
    weights = _weights(promote_margin=0.1)
    rel1 = _query(
        LadderState.seed(cfg),
        cfg=cfg,
        weights=weights,
        improvement=0.3,
        holdout_scalar=0.4,
        confirmed=True,
    )
    # A later release with a WORSE (higher) holdout scalar updates the
    # confirmation bit but keeps the lower best (best = lowest loss seen).
    rel2 = _query(
        rel1.state,
        cfg=cfg,
        weights=weights,
        improvement=0.3,
        holdout_scalar=0.9,
        confirmed=False,
    )
    assert rel2.released is True
    assert rel2.confirmed is False
    assert rel2.state.best_holdout_scalar == pytest.approx(0.4)  # the lower one stands


def test_improvement_exactly_at_threshold_releases() -> None:
    cfg = LadderConfig(budget=8)
    weights = _weights(promote_margin=0.1)
    # Pass scalars whose exact difference is the threshold so the ``>=``
    # boundary is hit cleanly (no float-subtraction artifact).
    rel = query_holdout(
        LadderState.seed(cfg),
        cfg=cfg,
        weights=weights,
        train_parent_scalar=1.1,
        train_child_scalar=1.0,  # improvement == 0.1 exactly
        holdout_scalar=0.5,
        holdout_confirmed=True,
    )
    assert rel.released is True


# ---------------------------------------------------------------------------
# Budget
# ---------------------------------------------------------------------------


def test_budget_decrements_per_query() -> None:
    cfg = LadderConfig(budget=3)
    weights = _weights(promote_margin=0.1)
    state = LadderState.seed(cfg)
    remaining = []
    for _ in range(3):
        rel = _query(
            state, cfg=cfg, weights=weights, improvement=0.5, holdout_scalar=0.5, confirmed=True
        )
        state = rel.state
        remaining.append(state.budget_remaining)
    assert remaining == [2, 1, 0]


def test_budget_exhaustion_stops_releasing() -> None:
    cfg = LadderConfig(budget=1)
    weights = _weights(promote_margin=0.1)

    # First query consumes the only budget unit (released).
    rel1 = _query(
        LadderState.seed(cfg),
        cfg=cfg,
        weights=weights,
        improvement=0.5,
        holdout_scalar=0.5,
        confirmed=True,
    )
    assert rel1.released is True
    assert rel1.state.budget_remaining == 0

    # Second query: budget exhausted → NOT released ("champion stands"),
    # nothing further charged, and the last best confirmation is re-reported.
    rel2 = _query(
        rel1.state,
        cfg=cfg,
        weights=weights,
        improvement=0.5,
        holdout_scalar=0.1,  # even a great holdout cannot be released now
        confirmed=False,
    )
    assert rel2.released is False
    assert rel2.confirmed is True  # the prior best, re-reported
    assert rel2.holdout_scalar == pytest.approx(0.5)
    assert rel2.state.budget_remaining == 0  # unchanged


def test_zero_budget_releases_nothing() -> None:
    cfg = LadderConfig(budget=0)
    weights = _weights(promote_margin=0.1)
    rel = _query(
        LadderState.seed(cfg),
        cfg=cfg,
        weights=weights,
        improvement=0.9,
        holdout_scalar=0.1,
        confirmed=True,
    )
    assert rel.released is False
    assert rel.confirmed is None  # nothing was ever released
    assert rel.state.budget_remaining == 0


# ---------------------------------------------------------------------------
# Config validation + record shape
# ---------------------------------------------------------------------------


def test_ladder_config_rejects_negative_budget() -> None:
    with pytest.raises(ValueError, match="budget"):
        LadderConfig(budget=-1)


def test_ladder_config_rejects_negative_threshold() -> None:
    with pytest.raises(ValueError, match="threshold"):
        LadderConfig(threshold=-0.1)


def test_ladder_config_rejects_negative_noise_scale() -> None:
    with pytest.raises(ValueError, match="noise_scale"):
        LadderConfig(noise_scale=-1.0)


def test_holdout_record_shape() -> None:
    rec = holdout_record(
        confirmed=True,
        train_scalar=0.5,
        holdout_scalar=0.6,
        released=True,
        budget_total=16,
        budget_remaining=15,
        threshold=0.1,
    )
    assert rec == {
        "confirmed": True,
        "train_scalar": 0.5,
        "holdout_scalar": 0.6,
        "ladder_released": True,
        "ladder_budget_total": 16,
        "ladder_budget_remaining": 15,
        "threshold": 0.1,
    }


def test_holdout_record_allows_nulls() -> None:
    rec = holdout_record(
        confirmed=None,
        train_scalar=None,
        holdout_scalar=None,
        released=False,
        budget_total=16,
        budget_remaining=16,
        threshold=0.1,
    )
    assert rec["confirmed"] is None
    assert rec["train_scalar"] is None
    assert rec["holdout_scalar"] is None
    assert rec["ladder_released"] is False
