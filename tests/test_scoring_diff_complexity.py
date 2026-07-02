"""Tests for the opt-in diff-complexity (MDL / parsimony) scoring term.

OVERFITTING.md §5 / §12 #4 — a small ``diff_complexity_weight * complexity(diff)``
penalty folded into the challenger's scalar so a shorter-description edit (which
provably overfits the board less) is preferred.

The load-bearing invariant: at the DEFAULT ``diff_complexity_weight == 0.0`` the
term is EXACTLY absent — the scalar, the ``scalar_components`` dict, the returned
aggregate, and the contract canonical form are byte-identical to a contract
without the field. The ON-path tests prove the term works (changes the scalar,
surfaces as a component, echoes the diff size, rolls the epoch) when weighted.
"""

from __future__ import annotations

from zicato.core import DriftCount, LossProfile, ScoringWeights
from zicato.core.types import Experiment, HypothesisSpec, Patch
from zicato.epoch.contract import round_floats, scoring_to_canon
from zicato.scoring.builtins import builtin_scalar, diff_complexity_component
from zicato.scoring.diff_complexity import diff_char_size, diff_complexity, diff_size
from zicato.tournament.gate import diff_size_evidence
from zicato.tournament.scoring import aggregate_generation_score

# ---------------------------------------------------------------------------
# Factories
# ---------------------------------------------------------------------------


def _loss(entry_id: str, *, drift_loss: float = 0.0, pass_fail: bool | None = True) -> LossProfile:
    return LossProfile(
        run_id=f"run-{entry_id}",
        entry_id=entry_id,
        generation_id="v1",
        epoch_id="e0",
        drift_counts=(DriftCount(kind="off_topic", severity="info", count=0),),
        plan_revisions=0,
        task_failure_ratio=0.0,
        runtime_ms=1000,
        wall_clock_budget_exceeded=False,
        expectation_result=None,
        drift_loss=drift_loss,
        pass_fail=pass_fail,
        score=None,
        metrics=None,
    )


def _patch(mid: str, *, new_content: str | None, op: str = "replace") -> Patch:
    return Patch(
        id=f"p_{mid}",
        mutation_id=mid,
        op=op,  # type: ignore[arg-type]
        new_content=new_content,
        new_numeric=None,
        new_enum=None,
        rationale="r",
    )


def _experiment(*patches: Patch) -> Experiment:
    return Experiment(
        id="exp",
        epoch_id="e0",
        generation_id="v1",
        parent_generation_id="v0",
        proposed_at="2026-06-09T00:00:00Z",
        hypothesis=HypothesisSpec(
            core_idea="idea",
            modulating=(),
            why="why",
            expected_drift_movements=(),
            expected_pass_rate_delta="+0.0",
        ),
        patches=tuple(patches),
        outcome=None,
    )


# ---------------------------------------------------------------------------
# diff-size proxies
# ---------------------------------------------------------------------------


def test_diff_size_counts_added_lines_and_patches() -> None:
    exp = _experiment(
        _patch("a", new_content="line1\nline2\nline3"),  # 3 lines
        _patch("b", new_content="solo"),  # 1 line
        _patch("c", new_content=None, op="set_numeric"),  # 0 lines, still a patch
    )
    ds = diff_size(exp)
    assert ds == {"added": 4, "removed": 0, "patches": 3}


def test_diff_size_empty_replacement_adds_no_lines() -> None:
    exp = _experiment(_patch("a", new_content=""))
    assert diff_size(exp) == {"added": 0, "removed": 0, "patches": 1}


def test_diff_size_no_patches() -> None:
    assert diff_size(_experiment()) == {"added": 0, "removed": 0, "patches": 0}


def test_diff_complexity_collapses_to_sum() -> None:
    assert diff_complexity({"added": 4, "removed": 0, "patches": 3}) == 7.0
    assert diff_complexity({"added": 0, "removed": 0, "patches": 0}) == 0.0
    # None (no diff size, e.g. a champion side) collapses to zero.
    assert diff_complexity(None) == 0.0


def test_diff_char_size_matches_lifted_proxy() -> None:
    # The lifted best-of-N char proxy: 16/patch + len(new_content).
    exp = _experiment(
        _patch("a", new_content="abcde"),  # 16 + 5
        _patch("b", new_content=None, op="set_numeric"),  # 16
    )
    assert diff_char_size(exp) == 16 + 5 + 16


# ---------------------------------------------------------------------------
# byte-identical-when-off
# ---------------------------------------------------------------------------


def test_term_absent_at_default_weight() -> None:
    weights = ScoringWeights()  # diff_complexity_weight defaults to 0.0
    diff = {"added": 100, "removed": 0, "patches": 5}
    # Even with a diff size threaded, the default-weight component is None.
    assert diff_complexity_component(weights, diff) is None


def test_aggregate_byte_identical_when_off() -> None:
    weights = ScoringWeights()
    losses = [_loss("e1", drift_loss=2.0), _loss("e2", drift_loss=1.0)]
    diff = {"added": 100, "removed": 0, "patches": 5}
    agg_none = aggregate_generation_score(losses, weights)
    agg_with_diff = aggregate_generation_score(losses, weights, diff_size=diff)
    # Threading a diff size with weight 0.0 changes NOTHING — same scalar,
    # same components, no diff_size echo, identical dict.
    assert agg_with_diff == agg_none
    assert "diff_complexity" not in agg_none["scalar_components"]
    assert "diff_size" not in agg_none


def test_builtin_scalar_byte_identical_when_off() -> None:
    weights = ScoringWeights()
    base = builtin_scalar(
        mean_score=0.5, drift_loss_mean=2.0, namespace_aggregates={}, weights=weights
    )
    with_diff = builtin_scalar(
        mean_score=0.5,
        drift_loss_mean=2.0,
        namespace_aggregates={},
        weights=weights,
        diff_size={"added": 50, "removed": 0, "patches": 3},
    )
    assert with_diff == base


# ---------------------------------------------------------------------------
# ON-path: the term changes the scalar + surfaces as a component
# ---------------------------------------------------------------------------


def test_component_value_and_scalar_delta_when_weighted() -> None:
    weights = ScoringWeights(diff_complexity_weight=0.1)
    diff = {"added": 4, "removed": 0, "patches": 3}  # complexity 7
    # The component is exactly weight * complexity.
    assert diff_complexity_component(weights, diff) == 0.1 * 7.0

    losses = [_loss("e1", drift_loss=2.0)]
    off = aggregate_generation_score(losses, ScoringWeights())
    on = aggregate_generation_score(losses, weights, diff_size=diff)
    # The scalar rose by exactly the diff-complexity contribution.
    assert on["scalar"] == off["scalar"] + 0.1 * 7.0
    # It surfaces as a component that sums into the scalar.
    comps = on["scalar_components"]
    assert comps["diff_complexity"] == 0.1 * 7.0
    assert abs(sum(comps.values()) - on["scalar"]) < 1e-12
    # And the diff size is echoed for gate evidence.
    assert on["diff_size"] == diff


def test_component_appended_last_in_fixed_position() -> None:
    weights = ScoringWeights(
        diff_complexity_weight=0.1,
        namespace_weights={"drift:": 1.0, "cost:": 1.0},
    )
    losses = [_loss("e1", drift_loss=1.0)]
    diff = {"added": 2, "removed": 0, "patches": 1}
    on = aggregate_generation_score(losses, weights, diff_size=diff)
    # diff_complexity is the LAST component key (after drift/pass/namespaces).
    assert list(on["scalar_components"].keys())[-1] == "diff_complexity"


def test_zero_complexity_still_surfaces_component_when_weighted() -> None:
    # A zero-size diff (no patches) with weight > 0 still surfaces the term
    # (as 0.0) because the term is ACTIVE — only the default weight removes it.
    weights = ScoringWeights(diff_complexity_weight=0.5)
    diff = {"added": 0, "removed": 0, "patches": 0}
    losses = [_loss("e1", drift_loss=1.0)]
    on = aggregate_generation_score(losses, weights, diff_size=diff)
    assert on["scalar_components"]["diff_complexity"] == 0.0
    assert on["diff_size"] == diff


def test_no_diff_size_means_no_term_even_when_weighted() -> None:
    # The champion side: weight > 0 but no diff size threaded ⇒ term absent.
    weights = ScoringWeights(diff_complexity_weight=0.5)
    assert diff_complexity_component(weights, None) is None
    losses = [_loss("e1", drift_loss=1.0)]
    on = aggregate_generation_score(losses, weights)  # no diff_size
    assert "diff_complexity" not in on["scalar_components"]
    assert "diff_size" not in on


# ---------------------------------------------------------------------------
# gate evidence
# ---------------------------------------------------------------------------


def test_diff_size_evidence_challenger_only() -> None:
    weights = ScoringWeights(diff_complexity_weight=0.1)
    losses = [_loss("e1", drift_loss=1.0)]
    parent = aggregate_generation_score(losses, weights)  # champion: no diff
    child = aggregate_generation_score(
        losses, weights, diff_size={"added": 4, "removed": 0, "patches": 3}
    )
    evidence = diff_size_evidence(parent, child)
    assert evidence == ["diff_size:challenger:added=4,removed=0,patches=3"]


def test_diff_size_evidence_empty_when_off() -> None:
    weights = ScoringWeights()
    losses = [_loss("e1", drift_loss=1.0)]
    agg = aggregate_generation_score(losses, weights)
    assert diff_size_evidence(agg, agg) == []


# ---------------------------------------------------------------------------
# contract canonicalization / epoch roll
# ---------------------------------------------------------------------------


def test_canon_omits_field_at_default() -> None:
    canon = round_floats(scoring_to_canon(ScoringWeights()))
    assert "diff_complexity_weight" not in canon


def test_canon_includes_and_rolls_when_set() -> None:
    off = round_floats(scoring_to_canon(ScoringWeights()))
    on = round_floats(scoring_to_canon(ScoringWeights(diff_complexity_weight=0.25)))
    assert on["diff_complexity_weight"] == 0.25
    # Setting the weight changes the canonical form ⇒ rolls the epoch.
    assert on != off


def test_scoring_weights_round_trips_field() -> None:
    w = ScoringWeights(diff_complexity_weight=0.3)
    assert ScoringWeights.from_json(w.to_json()) == w
    # The default still round-trips (and serialises the field as 0.0).
    d = ScoringWeights()
    assert ScoringWeights.from_json(d.to_json()) == d
