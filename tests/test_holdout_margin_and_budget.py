"""The holdout confirmation's own bounds (issue #118).

``tests/test_issue_118_pins.py`` pins the end-to-end claim: the board the issue
reported becomes promotable, and stays unpromotable at the defaults. This file
pins the pieces — the margin resolver, the budget under both monotonicity
scopes, and the pre-flight note that tells an operator the bounds exist before
they meet them as a run of ``holdout_not_confirmed`` rejections.
"""

from __future__ import annotations

from typing import Any

import pytest

from zicato.core import ScoringWeights
from zicato.epoch.preflight import holdout_window_note
from zicato.tournament.gate import effective_holdout_margin, holdout_confirms


def _agg(scalar: float, per_entry: dict[str, float], mean_score: float) -> dict[str, Any]:
    """A slice aggregate in the shape the gate reads."""
    return {
        "scalar": scalar,
        "mean_score": mean_score,
        "pass_rate": mean_score,
        "per_entry": {eid: {"score": score} for eid, score in per_entry.items()},
    }


# ---------------------------------------------------------------------------
# The margin resolver
# ---------------------------------------------------------------------------


def test_the_holdout_margin_falls_back_to_promote_margin() -> None:
    """``None`` is not "zero tolerance" — it is "reuse the train knob".

    This is what keeps every contract written before the field behaving
    identically, and it is why the field is ``float | None`` rather than a
    float with a sentinel default.
    """
    assert effective_holdout_margin(ScoringWeights(promote_margin=0.07)) == pytest.approx(0.07)
    assert effective_holdout_margin(
        ScoringWeights(promote_margin=0.07, holdout_margin=0.2)
    ) == pytest.approx(0.2)
    assert effective_holdout_margin(
        ScoringWeights(promote_margin=0.07, holdout_margin=0.0)
    ) == pytest.approx(0.0), "an explicit 0.0 is a real setting, not an unset field"


def test_negative_bounds_are_rejected_at_contract_load() -> None:
    """Both fields are tolerances; a negative one would invert the check.

    A negative holdout margin would turn the confirmation from "must not
    regress" into "must improve", which is exactly the second-gate semantics
    the holdout is defined not to have. Rejected at construction, like every
    other out-of-domain knob, rather than silently at duel time.
    """
    with pytest.raises(ValueError, match="holdout_margin must be >= 0"):
        ScoringWeights(holdout_margin=-0.01)
    with pytest.raises(ValueError, match="holdout_entry_regression_budget must be >= 0"):
        ScoringWeights(holdout_entry_regression_budget=-1)


def test_the_scalar_bound_uses_the_holdout_margin_and_says_so() -> None:
    """The rejection reason must name the bound it applied, not the train one."""
    parent = _agg(0.10, {"h0": 1.0, "h1": 1.0}, 1.0)
    child = _agg(0.25, {"h0": 1.0, "h1": 1.0}, 1.0)

    tight = ScoringWeights(promote_margin=0.05, pass_rate_monotonicity=False)
    reason = holdout_confirms(parent, child, tight)
    assert reason.startswith("holdout_not_confirmed:")
    assert "0.050000" in reason, "the reason states the bound that rejected"

    loose = ScoringWeights(promote_margin=0.05, holdout_margin=0.2, pass_rate_monotonicity=False)
    assert holdout_confirms(parent, child, loose) == ""


# ---------------------------------------------------------------------------
# The entry-regression budget
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("scope", ["per_entry", "aggregate"])
def test_the_budget_means_one_entry_under_either_scope(scope: str) -> None:
    """One budget unit tolerates one flip, whichever scope the contract pins.

    The two scopes measure different things — a count of regressed entries vs
    the mean-score delta — so the budget has to be converted for the aggregate
    case (``budget / entries``) or an operator's ``1`` would mean something
    different depending on a knob they set for unrelated reasons.
    """
    entries = {f"h{i}": 1.0 for i in range(6)}
    parent = _agg(0.0, entries, 1.0)
    one_flip = _agg(0.0, {**entries, "h0": 0.0}, 5 / 6)
    two_flips = _agg(0.0, {**entries, "h0": 0.0, "h1": 0.0}, 4 / 6)

    def weights(budget: int) -> ScoringWeights:
        return ScoringWeights(
            holdout_margin=1.0,  # take the scalar bound out of the picture
            holdout_entry_regression_budget=budget,
            pass_rate_monotonicity_scope=scope,  # type: ignore[arg-type]
        )

    assert holdout_confirms(parent, one_flip, weights(0)) != "", "budget 0 is today's rule"
    assert holdout_confirms(parent, one_flip, weights(1)) == ""
    assert (
        holdout_confirms(parent, two_flips, weights(1)) != ""
    ), "the budget is a bound, not a bypass"
    assert holdout_confirms(parent, two_flips, weights(2)) == ""


def test_the_budget_never_reaches_a_contract_that_disabled_monotonicity() -> None:
    """``pass_rate_monotonicity=False`` still turns the whole rule off.

    The budget widens the rule; it does not resurrect it. Pinned because the
    two knobs read as adjacent and an operator who disabled the check must not
    find it back on because they set a budget.
    """
    entries = {f"h{i}": 1.0 for i in range(6)}
    weights = ScoringWeights(
        holdout_margin=1.0,
        holdout_entry_regression_budget=0,
        pass_rate_monotonicity=False,
    )
    flipped = _agg(0.0, {**entries, "h0": 0.0}, 5 / 6)
    assert holdout_confirms(_agg(0.0, entries, 1.0), flipped, weights) == ""


# ---------------------------------------------------------------------------
# The pre-flight note
# ---------------------------------------------------------------------------


def test_the_note_is_silent_without_a_holdout() -> None:
    """No split, nothing to say — the note must not fire on unsplit boards."""
    assert holdout_window_note(ScoringWeights(), 0) is None


def test_the_note_names_the_zero_budget_trap_before_the_margin_one() -> None:
    """At budget 0 the note must say no margin can fix it.

    This is the residual the issue itself did not name: raising the holdout
    margin is the obvious response to a rejected confirmation, and at budget 0
    it accomplishes nothing, because the pass-rate rule fires before any scalar
    bound is consulted.
    """
    note = holdout_window_note(ScoringWeights(promote_margin=0.01), 6)
    assert note is not None
    assert "EVERY margin" in note
    assert "holdout_entry_regression_budget=1" in note


def test_the_note_names_the_step_size_when_the_margin_is_under_it() -> None:
    """A margin below the slice's own 1/N step cannot tolerate any regression."""
    weights = ScoringWeights(
        promote_margin=0.01,
        holdout_margin=0.02,
        holdout_entry_regression_budget=1,
    )
    note = holdout_window_note(weights, 6)
    assert note is not None
    assert "step size" in note
    assert "N_train/N_holdout" in note, "and points at the commensurable-bounds rule"


def test_the_note_goes_quiet_once_both_bounds_are_comfortable() -> None:
    """A contract that took the advice must stop being told about it."""
    weights = ScoringWeights(
        promote_margin=0.08,
        holdout_margin=0.2,
        holdout_entry_regression_budget=1,
    )
    assert holdout_window_note(weights, 6) is None
