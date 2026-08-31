"""Tests for the opt-in diff-complexity (MDL / parsimony) scoring term.

OVERFITTING.md §5 / §12 #4 — a small ``diff_complexity_weight * complexity(diff)``
penalty folded into the challenger's scalar so a shorter-description edit (which
provably overfits the board less) is preferred.

The load-bearing invariant: at the DEFAULT ``diff_complexity_weight == 0.0`` the
term is EXACTLY absent — the scalar, the ``scalar_components`` dict, the returned
aggregate, and the contract canonical form are byte-identical to a contract
without the field. The ON-path tests prove the term works (changes the scalar,
surfaces as a component, echoes the diff size, rolls the epoch) when weighted.

The CEILING half (``diff_complexity_ceiling``) is the paired hard gate rule:
a challenger whose diff complexity exceeds the ceiling is rejected outright
(default 0.0 = OFF, byte-identical). Its gate-decision behaviour is pinned in
``test_tournament_gate.py``; the end-to-end tests here prove the whole thread —
the orchestrator derives the diff size from the child's patches, the aggregate
echoes it, and the gate rejects with the honest reason recorded on the outcome.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from zicato.core import DriftCount, LossProfile, ScoringWeights
from zicato.core.types import Experiment, HypothesisSpec, Patch
from zicato.epoch.contract import round_floats, scoring_to_canon
from zicato.scoring.builtins import builtin_scalar, diff_complexity_component
from zicato.scoring.diff_complexity import (
    EXACT_DIFF_MAX_LINES,
    diff_char_size,
    diff_complexity,
    diff_size,
)
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
    base = builtin_scalar(mean_score=0.5, namespace_aggregates={"drift:": 2.0}, weights=weights)
    with_diff = builtin_scalar(
        mean_score=0.5,
        namespace_aggregates={"drift:": 2.0},
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
        namespace_weights={"drift:": 1.0, "failure:": 1.0, "cost:": 1.0},
    )
    losses = [_loss("e1", drift_loss=1.0)]
    diff = {"added": 2, "removed": 0, "patches": 1}
    on = aggregate_generation_score(losses, weights, diff_size=diff)
    # diff_complexity is the LAST component key (after pass + every channel).
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
# CEILING half (OVERFITTING.md §5 / §12 #4). The ceiling reads the challenger
# diff size off the aggregate; unlike the loss weight it adds NO scalar term.
# ---------------------------------------------------------------------------


def test_ceiling_echoes_diff_size_without_a_scalar_term() -> None:
    # Ceiling ON, weight OFF: the diff size is echoed so the gate's Rule 0 can
    # read it, but the scalar / components are byte-identical to the weight-off
    # path (the ceiling is a gate rule, not a loss nudge).
    weights = ScoringWeights(diff_complexity_ceiling=10.0)
    diff = {"added": 4, "removed": 0, "patches": 3}
    losses = [_loss("e1", drift_loss=2.0)]
    off = aggregate_generation_score(losses, ScoringWeights())
    on = aggregate_generation_score(losses, weights, diff_size=diff)
    assert on["scalar"] == off["scalar"]
    assert "diff_complexity" not in on["scalar_components"]
    assert on["scalar_components"] == off["scalar_components"]
    # ...but the diff size IS echoed for the ceiling to read.
    assert on["diff_size"] == diff


def test_ceiling_off_and_weight_off_is_byte_identical() -> None:
    # BOTH halves off: threading a diff size changes nothing (no echo, no term).
    weights = ScoringWeights()  # ceiling 0.0, weight 0.0
    diff = {"added": 100, "removed": 0, "patches": 5}
    losses = [_loss("e1", drift_loss=2.0)]
    agg_none = aggregate_generation_score(losses, weights)
    agg_with_diff = aggregate_generation_score(losses, weights, diff_size=diff)
    assert agg_with_diff == agg_none
    assert "diff_size" not in agg_none


def test_canon_omits_ceiling_at_default_and_rolls_when_set() -> None:
    off = round_floats(scoring_to_canon(ScoringWeights()))
    assert "diff_complexity_ceiling" not in off
    on = round_floats(scoring_to_canon(ScoringWeights(diff_complexity_ceiling=10.0)))
    assert on["diff_complexity_ceiling"] == 10.0
    assert on != off


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


# ---------------------------------------------------------------------------
# CEILING end-to-end through evolve_once. The orchestrator derives the
# challenger diff size from its patch records and threads it on the full A/B
# path; with the ceiling on, an over-budget diff is rejected pre-persist and
# the champion stands. The reason lands on the outcome (experiment record) and,
# via ``_emit_gate_evaluated``, on the round-log ``gate_evaluated`` rule.
# ---------------------------------------------------------------------------


def _set_ceiling(workspace: Path, epoch_id: str, ceiling: float) -> None:
    scoring_path = workspace / "epochs" / epoch_id / "scoring.json"
    body = json.loads(scoring_path.read_text())
    body["diff_complexity_ceiling"] = ceiling
    scoring_path.write_text(json.dumps(body))


def test_ceiling_rejects_oversized_challenger_diff_e2e(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The one-patch challenger diff (complexity 2) exceeds a ceiling of 1.0,
    so an otherwise-promotable child is REJECTED with the ceiling reason and
    the champion pointer does not advance."""
    from tests._orchestrator_harness import (
        _bootstrap_workspace,
        _install_stub_adapter_factory,
        _install_telemetry_stubs,
        _make_aux_responder,
        _valid_proposer_response,
        run_evolve_once,
    )

    workspace, epoch_id = _bootstrap_workspace(tmp_path)
    _set_ceiling(workspace, epoch_id, 1.0)
    _install_stub_adapter_factory(monkeypatch)
    _install_telemetry_stubs(
        monkeypatch,
        canned_loss_by_gen={"v0": 2.0, "v1": 1.0},  # v1 strictly better — would promote
        canned_pass_by_gen={"v0": True, "v1": True},
    )

    outcome = run_evolve_once(
        workspace, epoch_id, _make_aux_responder([_valid_proposer_response()])
    )

    assert outcome.tournament_decision == "rejected"
    assert outcome.rejection_reason.startswith("diff_complexity_ceiling:")
    assert "exceeds ceiling 1" in outcome.rejection_reason
    marker = workspace / "epochs" / epoch_id / "current_generation"
    assert not marker.exists()


def test_ceiling_high_enough_promotes_the_same_diff_e2e(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The identical improving child promotes when the ceiling is generous
    (complexity 2 <= 100) — the ceiling only vetoes over-budget diffs."""
    from tests._orchestrator_harness import (
        _bootstrap_workspace,
        _install_stub_adapter_factory,
        _install_telemetry_stubs,
        _make_aux_responder,
        _valid_proposer_response,
        run_evolve_once,
    )

    workspace, epoch_id = _bootstrap_workspace(tmp_path)
    _set_ceiling(workspace, epoch_id, 100.0)
    _install_stub_adapter_factory(monkeypatch)
    _install_telemetry_stubs(
        monkeypatch,
        canned_loss_by_gen={"v0": 2.0, "v1": 1.0},
        canned_pass_by_gen={"v0": True, "v1": True},
    )

    outcome = run_evolve_once(
        workspace, epoch_id, _make_aux_responder([_valid_proposer_response()])
    )
    assert outcome.tournament_decision == "promoted"


# ---------------------------------------------------------------------------
# Real delta accounting against parent-side content (issue #120). Without the
# parent text the historical whole-replacement count applies unchanged.
# ---------------------------------------------------------------------------


def test_parent_content_makes_a_verbatim_reemit_free() -> None:
    """The whole-file case from the issue: the proposal changes nothing, so it
    describes nothing and the patch itself does not count either."""
    template = "\n".join(f"line {i}" for i in range(37))
    exp = _experiment(_patch("whole_file", new_content=template))
    assert diff_size(exp, {"whole_file": template}) == {"added": 0, "removed": 0, "patches": 0}
    # ...and the same edit without the parent text still reads the old way.
    assert diff_size(exp) == {"added": 37, "removed": 0, "patches": 1}


def test_parent_content_measures_the_edit_not_the_file() -> None:
    """One line changed inside a 37-line file costs one added + one removed +
    the patch, not the 38 the whole re-emit used to cost."""
    lines = [f"line {i}" for i in range(37)]
    parent = "\n".join(lines)
    lines[10] = "line 10 rewritten"
    exp = _experiment(_patch("whole_file", new_content="\n".join(lines)))
    assert diff_size(exp, {"whole_file": parent}) == {"added": 1, "removed": 1, "patches": 1}


def test_deletions_are_counted_as_removed() -> None:
    parent = "\n".join(f"line {i}" for i in range(10))
    shrunk = "\n".join(f"line {i}" for i in range(4))
    exp = _experiment(_patch("p", new_content=shrunk))
    assert diff_size(exp, {"p": parent}) == {"added": 0, "removed": 6, "patches": 1}


def test_a_patch_against_an_empty_parent_counts_only_additions() -> None:
    exp = _experiment(_patch("p", new_content="one\ntwo"))
    assert diff_size(exp, {"p": ""}) == {"added": 2, "removed": 0, "patches": 1}


def test_unknown_mutation_ids_fall_back_per_patch() -> None:
    """A mixed experiment: the point with parent text is differenced, the one
    without keeps the legacy count. Neither policy leaks into the other."""
    parent = "a\nb\nc"
    exp = _experiment(
        _patch("known", new_content=parent),  # verbatim: free
        _patch("unknown", new_content="x\ny"),  # legacy: 2 added, 1 patch
    )
    assert diff_size(exp, {"known": parent}) == {"added": 2, "removed": 0, "patches": 1}


def test_content_less_patches_still_count_as_patches() -> None:
    """A numeric point's parent text is not comparable to the number replacing
    it, so there is no delta to measure and its presence is all that counts."""
    exp = _experiment(_patch("n", new_content=None, op="set_numeric"))
    assert diff_size(exp, {"n": "0.5"}) == {"added": 0, "removed": 0, "patches": 1}


def test_no_parent_contents_is_byte_identical_to_the_legacy_call() -> None:
    exp = _experiment(
        _patch("a", new_content="line1\nline2\nline3"),
        _patch("b", new_content="solo"),
        _patch("c", new_content=None, op="set_numeric"),
    )
    assert diff_size(exp, None) == diff_size(exp) == {"added": 4, "removed": 0, "patches": 3}


def test_a_large_heavily_rewritten_file_is_measured_in_bounded_time() -> None:
    """The exact diff has no time bound; the size cap is what supplies one.

    ``difflib``'s ``autojunk`` heuristic is what keeps ``SequenceMatcher`` fast
    when a line repeats often, and blank lines repeat often in every real file.
    Disabled, a 2000-line whole-file rewrite with half its lines blank measured
    29 s — inside per-round scoring, with no timeout above it. Pin that the cap
    holds this under a second; the walltime assertion is deliberately loose
    (machine-dependent), it is there to fail if the cap is ever removed.
    """
    n = EXACT_DIFF_MAX_LINES * 4
    parent = "\n".join("" if i % 2 else f"    old{i} = {i}" for i in range(n))
    child = "\n".join("" if i % 2 else f"    new{i} = {i}" for i in range(n))
    exp = _experiment(_patch("whole_file", new_content=child))

    started = time.perf_counter()
    size = diff_size(exp, {"whole_file": parent})
    elapsed = time.perf_counter() - started

    assert elapsed < 5.0, f"differencing {n} lines took {elapsed:.1f}s — is the cap gone?"
    assert size["patches"] == 1
    assert size["added"] > 0 and size["removed"] > 0


def test_a_targeted_edit_measures_the_edit_on_both_sides_of_the_cap() -> None:
    """The cap costs nothing on the case the term exists to reward.

    Above the cap ``autojunk`` returns and can overcount a near-total rewrite,
    but a one-line change in a huge file still measures ``(1, 1)``: the popular
    lines fall inside the matched run, so the heuristic never sees them. Pin
    both sides so the cap can never be read as "big files stop being measured".
    """
    for n in (EXACT_DIFF_MAX_LINES // 2, EXACT_DIFF_MAX_LINES * 3):
        lines = ["" if i % 5 == 0 else f"    x{i} = {i}" for i in range(n)]
        parent = "\n".join(lines)
        lines[n // 2] = "    CHANGED = 1"
        exp = _experiment(_patch("whole_file", new_content="\n".join(lines)))
        assert diff_size(exp, {"whole_file": parent}) == {
            "added": 1,
            "removed": 1,
            "patches": 1,
        }, f"a one-line edit in a {n}-line file must cost one line"


def test_a_verbatim_re_emit_is_free_above_the_cap_too() -> None:
    """Issue #120's headline property must not depend on the file's size."""
    n = EXACT_DIFF_MAX_LINES * 3
    parent = "\n".join("" if i % 5 == 0 else f"    x{i} = {i}" for i in range(n))
    exp = _experiment(_patch("whole_file", new_content=parent))
    assert diff_size(exp, {"whole_file": parent}) == {"added": 0, "removed": 0, "patches": 0}
