"""Tests for ``zicato.tournament.gate``."""

from __future__ import annotations

import pytest

from zicato.core import ScoringWeights
from zicato.tournament.gate import GateOutcome, evaluate_gate


def _agg(
    *,
    scalar: float,
    pass_rate: float = 1.0,
    per_entry: dict[str, dict[str, object]] | None = None,
) -> dict[str, object]:
    """Build a minimal aggregate dict that the gate accepts."""
    return {
        "drift_loss_mean": scalar,  # not inspected by the gate; mirror is fine
        "pass_rate": pass_rate,
        "expectation_count": len(per_entry or {}),
        "entry_count": len(per_entry or {}),
        "scalar": scalar,
        "per_entry": per_entry or {},
    }


# ---------------------------------------------------------------------------
# Holdout-confirmation step (OVERFITTING.md §12 #1 / §13). A train-measured
# win must also confirm on the holdout; a failure is just another reject and
# the champion stands. An absent holdout skips the step entirely so the
# decision is byte-identical to the pre-split gate.
# ---------------------------------------------------------------------------


def test_holdout_absent_is_byte_identical_to_pre_split() -> None:
    parent = _agg(scalar=1.0, pass_rate=0.5, per_entry={"a": {"pass_fail": True}})
    child = _agg(scalar=0.5, pass_rate=1.0, per_entry={"a": {"pass_fail": True}})
    weights = ScoringWeights(promote_margin=0.1)

    without = evaluate_gate(parent, child, weights)
    with_none = evaluate_gate(
        parent, child, weights, holdout_parent_agg=None, holdout_child_agg=None
    )
    assert without == with_none
    assert without.decision == "promoted"


def test_holdout_confirms_a_train_win() -> None:
    parent = _agg(scalar=1.0, per_entry={"a": {"pass_fail": True}})
    child = _agg(scalar=0.5, per_entry={"a": {"pass_fail": True}})
    # The holdout slice also improves (or at least does not regress).
    h_parent = _agg(scalar=1.0, per_entry={"h": {"pass_fail": True}})
    h_child = _agg(scalar=0.8, per_entry={"h": {"pass_fail": True}})
    weights = ScoringWeights(promote_margin=0.1)

    outcome = evaluate_gate(
        parent, child, weights, holdout_parent_agg=h_parent, holdout_child_agg=h_child
    )
    assert outcome.decision == "promoted"
    assert outcome.reason == ""


def test_holdout_scalar_regression_flips_a_train_win_to_reject() -> None:
    parent = _agg(scalar=1.0, per_entry={"a": {"pass_fail": True}})
    child = _agg(scalar=0.5, per_entry={"a": {"pass_fail": True}})  # clear train win
    # The holdout REGRESSES past the margin: the challenger memorized train.
    h_parent = _agg(scalar=1.0, per_entry={"h": {"pass_fail": True}})
    h_child = _agg(scalar=1.5, per_entry={"h": {"pass_fail": True}})
    weights = ScoringWeights(promote_margin=0.1)

    outcome = evaluate_gate(
        parent, child, weights, holdout_parent_agg=h_parent, holdout_child_agg=h_child
    )
    assert outcome.decision == "rejected"
    assert "holdout_not_confirmed" in outcome.reason
    # The reported deltas are still the TRAIN-side deltas.
    assert outcome.delta_scalar == pytest.approx(-0.5)


def test_holdout_per_entry_regression_flips_a_train_win_to_reject() -> None:
    parent = _agg(scalar=1.0, per_entry={"a": {"pass_fail": True}})
    child = _agg(scalar=0.5, per_entry={"a": {"pass_fail": True}})
    # Holdout scalar holds flat, but an entry the champion passed regressed.
    h_parent = _agg(scalar=1.0, per_entry={"h": {"pass_fail": True}})
    h_child = _agg(scalar=1.0, per_entry={"h": {"pass_fail": False}})
    weights = ScoringWeights(promote_margin=0.1)

    outcome = evaluate_gate(
        parent, child, weights, holdout_parent_agg=h_parent, holdout_child_agg=h_child
    )
    assert outcome.decision == "rejected"
    assert "holdout_not_confirmed" in outcome.reason
    assert "h" in outcome.reason


def test_holdout_flat_is_a_confirmation_not_a_failure() -> None:
    # A train win whose holdout merely holds flat (no regression) confirms —
    # the holdout is not asked to clear the margin in the improving direction.
    parent = _agg(scalar=1.0, per_entry={"a": {"pass_fail": True}})
    child = _agg(scalar=0.5, per_entry={"a": {"pass_fail": True}})
    h_parent = _agg(scalar=1.0, per_entry={"h": {"pass_fail": True}})
    h_child = _agg(scalar=1.0, per_entry={"h": {"pass_fail": True}})  # exactly flat
    weights = ScoringWeights(promote_margin=0.1)

    outcome = evaluate_gate(
        parent, child, weights, holdout_parent_agg=h_parent, holdout_child_agg=h_child
    )
    assert outcome.decision == "promoted"


def test_train_reject_fires_before_holdout_confirmation() -> None:
    # A train-side reject (insufficient margin) must surface its own reason,
    # not a holdout reason — the holdout step only runs after the train rules
    # would have promoted.
    parent = _agg(scalar=1.0, per_entry={"a": {"pass_fail": True}})
    child = _agg(scalar=0.999, per_entry={"a": {"pass_fail": True}})  # near-miss
    h_parent = _agg(scalar=1.0, per_entry={"h": {"pass_fail": True}})
    h_child = _agg(scalar=5.0, per_entry={"h": {"pass_fail": False}})  # awful holdout
    weights = ScoringWeights(promote_margin=0.1)

    outcome = evaluate_gate(
        parent, child, weights, holdout_parent_agg=h_parent, holdout_child_agg=h_child
    )
    assert outcome.decision == "rejected"
    assert "insufficient improvement" in outcome.reason
    assert "holdout_not_confirmed" not in outcome.reason


def test_gate_promotes_when_child_beats_parent_by_margin() -> None:
    parent = _agg(scalar=1.0, pass_rate=0.5, per_entry={"a": {"pass_fail": True}})
    child = _agg(scalar=0.5, pass_rate=1.0, per_entry={"a": {"pass_fail": True}})
    weights = ScoringWeights(promote_margin=0.1)

    outcome = evaluate_gate(parent, child, weights)

    assert isinstance(outcome, GateOutcome)
    assert outcome.decision == "promoted"
    assert outcome.reason == ""
    assert outcome.delta_scalar == -0.5
    assert outcome.delta_pass_rate == 0.5


def test_gate_rejects_when_margin_insufficient() -> None:
    parent = _agg(scalar=1.0)
    child = _agg(scalar=0.995)  # better, but by less than the 0.01 margin
    weights = ScoringWeights(promote_margin=0.01)

    outcome = evaluate_gate(parent, child, weights)

    assert outcome.decision == "rejected"
    # A child that improved but not enough is a near-miss, NOT a
    # regression — the reason says "insufficient improvement" and
    # states the real loss drop.
    assert "insufficient improvement" in outcome.reason
    assert "regressed" not in outcome.reason
    assert "0.005" in outcome.reason
    # Deltas reported regardless of decision
    assert outcome.delta_scalar == pytest.approx(-0.005)


def test_gate_rejects_when_child_ties_parent() -> None:
    parent = _agg(scalar=1.0)
    child = _agg(scalar=1.0)
    weights = ScoringWeights(promote_margin=0.01)

    outcome = evaluate_gate(parent, child, weights)

    assert outcome.decision == "rejected"
    # A tie is no improvement at all — still a near-miss, not a
    # regression.
    assert "insufficient improvement" in outcome.reason
    assert "regressed" not in outcome.reason


def test_gate_reason_says_regressed_when_child_loss_rises() -> None:
    """A child whose loss ROSE is reported as a regression, not a near-miss.

    The scalar is a loss (lower is better). When the child's scalar is
    higher than the parent's, the child is worse — the reason must say
    so plainly and state the real positive delta, rather than the
    misleading "did not beat by margin" phrasing.
    """
    parent = _agg(scalar=1.0)
    child = _agg(scalar=1.3)  # loss ROSE by 0.3 — outright worse
    weights = ScoringWeights(promote_margin=0.01)

    outcome = evaluate_gate(parent, child, weights)

    assert outcome.decision == "rejected"
    assert "regressed" in outcome.reason
    assert "insufficient improvement" not in outcome.reason
    # The real delta (child - parent = +0.3) is stated.
    assert "0.3" in outcome.reason or "0.300000" in outcome.reason
    assert outcome.delta_scalar == pytest.approx(0.3)


def test_gate_rejects_on_pass_rate_regression_even_when_scalar_improves() -> None:
    """The strict-monotonicity rule overrides scalar improvement."""
    parent = _agg(
        scalar=2.0,
        pass_rate=1.0,
        per_entry={
            "a": {"pass_fail": True},
            "b": {"pass_fail": True},
        },
    )
    child = _agg(
        scalar=0.5,
        pass_rate=0.5,
        per_entry={
            "a": {"pass_fail": True},
            "b": {"pass_fail": False},  # regression here
        },
    )
    weights = ScoringWeights(promote_margin=0.01, pass_rate_monotonicity=True)

    outcome = evaluate_gate(parent, child, weights)

    assert outcome.decision == "rejected"
    assert "pass-rate regression on entries" in outcome.reason
    assert "b" in outcome.reason


def test_gate_lists_multiple_regressed_entries_alphabetically() -> None:
    parent = _agg(
        scalar=2.0,
        per_entry={
            "alpha": {"pass_fail": True},
            "bravo": {"pass_fail": True},
            "charlie": {"pass_fail": True},
        },
    )
    child = _agg(
        scalar=0.5,
        per_entry={
            "alpha": {"pass_fail": False},
            "bravo": {"pass_fail": True},
            "charlie": {"pass_fail": None},  # also counts as a regression
        },
    )
    outcome = evaluate_gate(parent, child, ScoringWeights())

    assert outcome.decision == "rejected"
    # Entries listed in sorted order
    assert "alpha, charlie" in outcome.reason


def test_gate_ignores_pass_rate_regression_when_monotonicity_disabled() -> None:
    """When the operator opts out, scalar improvement alone is enough."""
    parent = _agg(
        scalar=2.0,
        pass_rate=1.0,
        per_entry={"a": {"pass_fail": True}},
    )
    child = _agg(
        scalar=0.5,
        pass_rate=0.0,
        per_entry={"a": {"pass_fail": False}},
    )
    weights = ScoringWeights(
        promote_margin=0.01,
        pass_rate_monotonicity=False,
    )

    outcome = evaluate_gate(parent, child, weights)

    assert outcome.decision == "promoted"
    assert outcome.reason == ""


def test_gate_does_not_penalize_entries_parent_failed() -> None:
    """A child improving parent-failed entries is fine; only parent-pass→child-fail trips."""
    parent = _agg(
        scalar=2.0,
        per_entry={
            "a": {"pass_fail": True},
            "b": {"pass_fail": False},
        },
    )
    child = _agg(
        scalar=0.5,
        per_entry={
            "a": {"pass_fail": True},
            "b": {"pass_fail": False},  # still failing — not a regression
        },
    )
    outcome = evaluate_gate(parent, child, ScoringWeights())
    assert outcome.decision == "promoted"


def test_gate_treats_missing_child_entry_as_regression() -> None:
    """An entry the parent passed that the child did not run regresses the gate."""
    parent = _agg(
        scalar=2.0,
        per_entry={
            "a": {"pass_fail": True},
            "b": {"pass_fail": True},
        },
    )
    child = _agg(
        scalar=0.5,
        per_entry={"a": {"pass_fail": True}},  # b absent
    )
    outcome = evaluate_gate(parent, child, ScoringWeights())
    assert outcome.decision == "rejected"
    assert "b" in outcome.reason


# ---------------------------------------------------------------------------
# Aggregate-scope pass-rate monotonicity (issue #17). Rule 2 can be selected
# per_entry (default) or aggregate. Under aggregate, a challenger may trade
# WHICH entries pass as long as the OVERALL pass-rate does not drop; under
# per_entry, any champion-passed entry that flips still rejects.
# ---------------------------------------------------------------------------


def _aggregate_weights(*, promote_margin: float = 0.02) -> ScoringWeights:
    return ScoringWeights(
        promote_margin=promote_margin,
        pass_rate_monotonicity=True,
        pass_rate_monotonicity_scope="aggregate",
    )


def test_aggregate_scope_promotes_net_neutral_but_better_challenger() -> None:
    """The filer's scenario: net-neutral on overall pass-rate AND clears the
    scalar margin, but reshuffles which entries pass. Promotes under
    aggregate; still rejected under per_entry."""
    # 9 entries, 4 pass on each side, but a DIFFERENT 4 — net pass-rate equal.
    parent = _agg(
        scalar=9.17,
        pass_rate=4 / 9,
        per_entry={f"E{i}": {"pass_fail": i <= 4} for i in range(1, 10)},
    )
    child = _agg(
        scalar=5.23,  # ~43% loss reduction, well past promote_margin
        pass_rate=4 / 9,
        per_entry={f"E{i}": {"pass_fail": i >= 6} for i in range(1, 10)},
    )

    promoted = evaluate_gate(parent, child, _aggregate_weights())
    assert promoted.decision == "promoted"
    assert promoted.reason == ""

    # Same inputs, per_entry scope (the default) — still rejected, naming the
    # champion-passed entries that flipped.
    rejected = evaluate_gate(
        parent, child, ScoringWeights(promote_margin=0.02, pass_rate_monotonicity=True)
    )
    assert rejected.decision == "rejected"
    assert "pass-rate regression on entries" in rejected.reason
    assert "E1" in rejected.reason and "E4" in rejected.reason


def test_aggregate_scope_promotes_when_overall_pass_rate_improves() -> None:
    """An overall pass-rate improvement promotes even with one entry flip."""
    parent = _agg(
        scalar=2.0,
        pass_rate=2 / 3,
        per_entry={
            "a": {"pass_fail": True},
            "b": {"pass_fail": True},
            "c": {"pass_fail": False},
        },
    )
    child = _agg(
        scalar=0.5,
        pass_rate=1.0,  # b flipped to fail, but a, c, d all pass now → net up
        per_entry={
            "a": {"pass_fail": True},
            "b": {"pass_fail": False},  # individual regression
            "c": {"pass_fail": True},
            "d": {"pass_fail": True},
        },
    )
    outcome = evaluate_gate(parent, child, _aggregate_weights())
    assert outcome.decision == "promoted"


def test_aggregate_scope_rejects_genuine_net_regression() -> None:
    """A real overall pass-rate drop (past tolerance) still rejects — the
    tolerance does not let genuine regressions through."""
    parent = _agg(
        scalar=2.0,
        pass_rate=4 / 9,
        per_entry={f"E{i}": {"pass_fail": i <= 4} for i in range(1, 10)},
    )
    child = _agg(
        scalar=0.5,  # scalar improved, but pass-rate fell from 4/9 to 2/9
        pass_rate=2 / 9,
        per_entry={f"E{i}": {"pass_fail": i >= 8} for i in range(1, 10)},
    )
    outcome = evaluate_gate(parent, child, _aggregate_weights())
    assert outcome.decision == "rejected"
    assert "pass-rate regression: overall pass-rate fell by" in outcome.reason
    # Reports the champion -> challenger overall pass-rates, not entry ids.
    assert "E1" not in outcome.reason


def test_aggregate_scope_tolerates_float_noise_equal_pass_rate() -> None:
    """A pass-rate equal within float noise is treated as 'held', not regressed."""
    parent = _agg(scalar=2.0, pass_rate=1 / 3, per_entry={"a": {"pass_fail": True}})
    # Reconstruct the same ratio a different way so it differs by float noise.
    child = _agg(
        scalar=0.5,
        pass_rate=(2 / 3) - (1 / 3),  # == 1/3 modulo float noise
        per_entry={"a": {"pass_fail": False}},
    )
    outcome = evaluate_gate(parent, child, _aggregate_weights())
    assert outcome.decision == "promoted"


def test_aggregate_scope_off_via_disable_flag() -> None:
    """pass_rate_monotonicity=False disables the rule regardless of scope —
    there is no separate 'off' scope value."""
    parent = _agg(scalar=2.0, pass_rate=1.0, per_entry={"a": {"pass_fail": True}})
    child = _agg(scalar=0.5, pass_rate=0.0, per_entry={"a": {"pass_fail": False}})
    weights = ScoringWeights(
        promote_margin=0.01,
        pass_rate_monotonicity=False,
        pass_rate_monotonicity_scope="aggregate",
    )
    outcome = evaluate_gate(parent, child, weights)
    assert outcome.decision == "promoted"


def test_per_entry_scope_is_default() -> None:
    """The default scope is per_entry — byte-identical to pre-issue-17 behavior."""
    assert ScoringWeights().pass_rate_monotonicity_scope == "per_entry"


# ---------------------------------------------------------------------------
# Holdout confirmation honors the same scope (issue #17): train and holdout
# apply ONE consistent policy.
# ---------------------------------------------------------------------------


def test_holdout_aggregate_scope_confirms_reshuffled_holdout() -> None:
    """Under aggregate scope, a holdout that reshuffles passing entries but
    holds overall pass-rate confirms the train win (no per-entry veto)."""
    parent = _agg(scalar=2.0, pass_rate=0.5, per_entry={"a": {"pass_fail": True}})
    child = _agg(scalar=0.5, pass_rate=0.5, per_entry={"a": {"pass_fail": True}})
    # Holdout: champion passes h1, challenger passes h2 — net pass-rate equal.
    h_parent = _agg(
        scalar=1.0,
        pass_rate=0.5,
        per_entry={"h1": {"pass_fail": True}, "h2": {"pass_fail": False}},
    )
    h_child = _agg(
        scalar=0.9,
        pass_rate=0.5,
        per_entry={"h1": {"pass_fail": False}, "h2": {"pass_fail": True}},
    )
    weights = _aggregate_weights(promote_margin=0.1)

    outcome = evaluate_gate(
        parent, child, weights, holdout_parent_agg=h_parent, holdout_child_agg=h_child
    )
    assert outcome.decision == "promoted"

    # Per-entry scope would reject the SAME reshuffled holdout (h1 flipped).
    per_entry = ScoringWeights(promote_margin=0.1, pass_rate_monotonicity=True)
    rejected = evaluate_gate(
        parent, child, per_entry, holdout_parent_agg=h_parent, holdout_child_agg=h_child
    )
    assert rejected.decision == "rejected"
    assert "holdout_not_confirmed" in rejected.reason
    assert "h1" in rejected.reason


def test_holdout_aggregate_scope_rejects_net_holdout_regression() -> None:
    """A genuine holdout pass-rate drop still fails confirmation under
    aggregate scope."""
    parent = _agg(scalar=2.0, pass_rate=0.5, per_entry={"a": {"pass_fail": True}})
    child = _agg(scalar=0.5, pass_rate=0.5, per_entry={"a": {"pass_fail": True}})
    h_parent = _agg(
        scalar=1.0,
        pass_rate=1.0,
        per_entry={"h1": {"pass_fail": True}, "h2": {"pass_fail": True}},
    )
    h_child = _agg(
        scalar=0.9,
        pass_rate=0.5,  # holdout pass-rate fell 1.0 -> 0.5
        per_entry={"h1": {"pass_fail": True}, "h2": {"pass_fail": False}},
    )
    outcome = evaluate_gate(
        parent,
        child,
        _aggregate_weights(promote_margin=0.1),
        holdout_parent_agg=h_parent,
        holdout_child_agg=h_child,
    )
    assert outcome.decision == "rejected"
    assert "holdout_not_confirmed" in outcome.reason
    assert "overall pass-rate fell by" in outcome.reason


# ---------------------------------------------------------------------------
# Diff-complexity CEILING (OVERFITTING.md §5 / §12 #4 — the ceiling half of the
# regularizer). An OPT-IN hard gate rule (default 0.0 = OFF): a challenger
# whose diff complexity (added + removed + patches) exceeds the ceiling is
# REJECTED outright, before any scoring rule, with an honest reason. The
# challenger diff size rides on ``child_agg["diff_size"]`` (threaded only when
# the parsimony machinery is active).
# ---------------------------------------------------------------------------


def _agg_with_diff(
    *, scalar: float, diff_size: dict[str, int] | None = None, pass_rate: float = 1.0
) -> dict[str, object]:
    agg = _agg(scalar=scalar, pass_rate=pass_rate)
    if diff_size is not None:
        agg["diff_size"] = dict(diff_size)
    return agg


def test_ceiling_off_by_default_ignores_an_oversized_diff() -> None:
    """At the default ceiling 0.0 the check is skipped — a huge diff that would
    otherwise clear the gate still promotes (byte-identical to no field)."""
    parent = _agg(scalar=2.0)
    child = _agg_with_diff(scalar=1.0, diff_size={"added": 500, "removed": 0, "patches": 9})
    outcome = evaluate_gate(parent, child, ScoringWeights())
    assert outcome.decision == "promoted"
    assert outcome.reason == ""


def test_ceiling_rejects_a_diff_over_the_ceiling_with_an_honest_reason() -> None:
    """complexity = 12 + 0 + 2 = 14 > ceiling 10 → rejected, and the reason
    names the actual complexity and the ceiling."""
    weights = ScoringWeights(diff_complexity_ceiling=10.0)
    parent = _agg(scalar=2.0)
    child = _agg_with_diff(scalar=0.5, diff_size={"added": 12, "removed": 0, "patches": 2})
    outcome = evaluate_gate(parent, child, weights)
    assert outcome.decision == "rejected"
    assert outcome.reason == "diff_complexity_ceiling: diff complexity 14 exceeds ceiling 10"


def test_ceiling_passes_a_diff_at_or_under_the_ceiling() -> None:
    """complexity = 9 + 0 + 1 = 10 == ceiling 10 (not OVER) → the ceiling does
    not fire; the scoring rules decide (this child improves → promoted)."""
    weights = ScoringWeights(diff_complexity_ceiling=10.0)
    parent = _agg(scalar=2.0)
    child = _agg_with_diff(scalar=0.5, diff_size={"added": 9, "removed": 0, "patches": 1})
    outcome = evaluate_gate(parent, child, weights)
    assert outcome.decision == "promoted"


def test_ceiling_vetoes_before_the_scalar_rule_even_when_the_child_regressed() -> None:
    """A structural admissibility veto: an over-budget diff that ALSO regressed
    reports the ceiling reason, not the scalar near-miss — the ceiling is
    checked first."""
    weights = ScoringWeights(diff_complexity_ceiling=5.0)
    parent = _agg(scalar=1.0)
    child = _agg_with_diff(scalar=2.0, diff_size={"added": 20, "removed": 0, "patches": 3})
    outcome = evaluate_gate(parent, child, weights)
    assert outcome.decision == "rejected"
    assert outcome.reason.startswith("diff_complexity_ceiling:")


def test_ceiling_with_no_diff_size_on_child_agg_is_skipped() -> None:
    """When the ceiling is on but no diff size was threaded (e.g. fast-mode /
    matchup scoring), the ceiling cannot fire and the scoring rules decide."""
    weights = ScoringWeights(diff_complexity_ceiling=1.0)
    parent = _agg(scalar=2.0)
    child = _agg(scalar=1.0)  # no diff_size key
    outcome = evaluate_gate(parent, child, weights)
    assert outcome.decision == "promoted"


# ---------------------------------------------------------------------------
# Attributable per-entry regressions (issue #130). Reported on both verdicts,
# never folded into ``reason``, never a veto.
# ---------------------------------------------------------------------------


def _row(*, score: float | None = None, drift: float | None = None) -> dict[str, object]:
    row: dict[str, object] = {}
    if score is not None:
        row["score"] = score
    if drift is not None:
        row["drift_loss"] = drift
    return row


def test_no_regression_reports_nothing() -> None:
    parent = _agg(scalar=1.0, per_entry={"a": _row(score=1.0, drift=0.2)})
    child = _agg(scalar=0.5, per_entry={"a": _row(score=1.0, drift=0.2)})
    outcome = evaluate_gate(parent, child, ScoringWeights(promote_margin=0.1))
    assert outcome.decision == "promoted"
    assert outcome.attributable_regressions == ()


def test_a_rejection_reports_its_regressions_too() -> None:
    """Both decisions carry the report — a reject names the entry as well, so a
    consumer never has to infer it from the verdict."""
    parent = _agg(scalar=1.0, per_entry={"a": _row(score=1.0, drift=0.1)})
    child = _agg(scalar=2.0, per_entry={"a": _row(score=0.0, drift=0.9)})
    outcome = evaluate_gate(parent, child, ScoringWeights(promote_margin=0.1))
    assert outcome.decision == "rejected"
    assert outcome.attributable_regressions == ("a",)


def test_drift_band_needs_both_limbs() -> None:
    """Near zero the ratio alone is meaningless: 0.001 -> 0.010 is a 10x jump
    that clears no absolute band, so it is not reported; 0.10 -> 0.60 is."""
    parent = _agg(
        scalar=1.0,
        per_entry={
            "noisy": _row(score=1.0, drift=0.001),
            "collapsed": _row(score=1.0, drift=0.10),
            "doubled_slightly": _row(score=1.0, drift=1.00),
        },
    )
    child = _agg(
        scalar=0.5,
        per_entry={
            "noisy": _row(score=1.0, drift=0.010),
            "collapsed": _row(score=1.0, drift=0.60),
            # 1.00 -> 1.04 clears the absolute band's arithmetic but not the
            # ratio limb, so the compound condition holds it back.
            "doubled_slightly": _row(score=1.0, drift=1.04),
        },
    )
    outcome = evaluate_gate(parent, child, ScoringWeights(promote_margin=0.1))
    assert outcome.decision == "promoted"
    assert outcome.attributable_regressions == ("collapsed",)


def test_an_entry_with_no_drift_measurement_is_never_reported() -> None:
    """A hand-built or pre-drift aggregate must not manufacture a regression."""
    parent = _agg(scalar=1.0, per_entry={"a": {"pass_fail": True}})
    child = _agg(scalar=0.5, per_entry={"a": {"pass_fail": True, "drift_loss": 0.9}})
    outcome = evaluate_gate(parent, child, ScoringWeights(promote_margin=0.1))
    assert outcome.attributable_regressions == ()


def test_the_report_is_not_a_veto_and_never_enters_the_reason() -> None:
    """The whole point: a promotion carrying a reported regression is still a
    promotion, and its reason is still exactly empty."""
    parent = _agg(scalar=1.0, per_entry={"a": _row(score=1.0, drift=0.10)})
    child = _agg(scalar=0.5, per_entry={"a": _row(score=1.0, drift=0.60)})
    outcome = evaluate_gate(parent, child, ScoringWeights(promote_margin=0.1))
    assert outcome.decision == "promoted"
    assert outcome.reason == ""
    assert outcome.attributable_regressions == ("a",)


def test_regression_detail_matches_the_reported_ids() -> None:
    from zicato.tournament.gate import attributable_regression_detail

    parent = _agg(scalar=1.0, per_entry={"a": _row(score=1.0, drift=0.10)})
    child = _agg(scalar=0.5, per_entry={"a": _row(score=1.0, drift=0.60)})
    detail = attributable_regression_detail(parent, child)
    assert list(detail) == ["a"]
    assert detail["a"] == {
        "parent_score": 1.0,
        "child_score": 1.0,
        "parent_drift_loss": 0.10,
        "child_drift_loss": 0.60,
    }


# ---------------------------------------------------------------------------
# Rule 1's parsimony decomposition (issue #120(b)). Inert at the default
# weight; on an active term it splits the delta into board and toll.
# ---------------------------------------------------------------------------


def _agg_with_toll(*, scalar: float, toll: float | None = None) -> dict[str, object]:
    agg = _agg(scalar=scalar)
    components: dict[str, float] = {"drift": 0.0, "pass": scalar}
    if toll is not None:
        components["pass"] = scalar - toll
        components["diff_complexity"] = toll
        agg["diff_size"] = {"added": 37, "removed": 0, "patches": 1}
    agg["scalar_components"] = components
    return agg


def test_rejection_reason_is_unchanged_when_the_parsimony_term_is_off() -> None:
    """Default contracts carry no ``diff_complexity`` component, so the reason
    is byte-identical to the pre-decomposition wording."""
    parent = _agg_with_toll(scalar=1.0)
    child = _agg_with_toll(scalar=1.5)
    outcome = evaluate_gate(parent, child, ScoringWeights(promote_margin=0.01))
    assert outcome.decision == "rejected"
    assert outcome.reason == (
        "challenger regressed: loss rose by 0.500000 "
        "(champion 1.000000 -> challenger 1.500000); a promotion needs the "
        "loss to drop by at least 0.010000"
    )


def test_a_toll_driven_rejection_says_the_board_improved() -> None:
    """The board fell 1.00 -> 0.75 and the challenger paid 0.76 for its edit.
    The operator must be able to read both numbers off the reason."""
    parent = _agg_with_toll(scalar=1.0)
    child = _agg_with_toll(scalar=1.51, toll=0.76)
    outcome = evaluate_gate(parent, child, ScoringWeights(promote_margin=0.01))
    assert outcome.decision == "rejected"
    assert "diff_complexity: raw quality delta -0.250000, toll +0.760000" in outcome.reason
    assert "the raw delta alone would have cleared the promote margin" in outcome.reason
    assert "diff_size:challenger:added=37,removed=0,patches=1" in outcome.reason


def test_a_toll_that_did_not_swing_it_says_so_by_omission() -> None:
    """A challenger that regressed on the board too gets the decomposition but
    NOT the "would have cleared" clause — the toll is not the whole story."""
    parent = _agg_with_toll(scalar=1.0)
    child = _agg_with_toll(scalar=1.30, toll=0.10)
    outcome = evaluate_gate(parent, child, ScoringWeights(promote_margin=0.01))
    assert outcome.decision == "rejected"
    assert "diff_complexity: raw quality delta +0.200000, toll +0.100000" in outcome.reason
    assert "would have cleared" not in outcome.reason
