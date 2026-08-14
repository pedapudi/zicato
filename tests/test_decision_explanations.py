"""The evidence that rides along with a verdict (issues #126, #129).

Issue #129 generalised eleven reports into one complaint: zicato detects
that something is wrong and does not say what. The material was almost
always in hand at the point the verdict was rendered — a gate reason with
the compared scalars one frame up, a health finding whose ``detail``
carried a remediation the renderer skipped, a stop message counting a
streak beside the outcome that ended it — so these tests pin the numbers
INTO the operator-facing strings rather than pinning the strings
themselves.

Issue #126 is the same defect one layer out: ``parallelism`` bounds every
run and appeared in no log line anywhere, so a run capped at the default 4
on a 64-core host looked exactly like slow work.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from zicato.core import ScoringWeights
from zicato.evolve.decision_support import _field_failure_summary
from zicato.evolve.loop import log_effective_concurrency
from zicato.health.diagnostics import detect_placebo_promoted, detect_stalled_loop
from zicato.runtime_factory import resolve_parallelism
from zicato.selection.standings_ext import uncertainty_blocks_promotion
from zicato.tournament.gate import _namespace_regression_reason, evaluate_gate

# ---------------------------------------------------------------------------
# #126 — the run-start configuration report
# ---------------------------------------------------------------------------


def _workspace(tmp_path: Path, runtime: dict[str, Any]) -> Path:
    root = tmp_path / ".zicato"
    root.mkdir()
    (root / "config.json").write_text(json.dumps({"runtime": runtime}), encoding="utf-8")
    return root


def test_concurrency_report_names_every_knob_and_the_default_source(tmp_path: Path) -> None:
    """A run that configures nothing still says what bounds it."""
    line = log_effective_concurrency(_workspace(tmp_path, {}))

    assert "parallelism=4" in line
    assert "from default" in line
    assert "propose_parallelism=4" in line
    # AUTO is host-derived, so the resolved count — not the word AUTO alone —
    # is what makes the ceiling checkable against the box it ran on.
    assert "host_worker_permits=AUTO -> " in line
    assert "usable CPUs=" in line


def test_concurrency_report_attributes_a_workspace_value(tmp_path: Path) -> None:
    """A deliberate cap must be distinguishable from an unremarked default."""
    line = log_effective_concurrency(
        _workspace(tmp_path, {"parallelism": 12, "propose_parallelism": 2})
    )

    assert "parallelism=12 (from workspace runtime.parallelism)" in line
    assert "propose_parallelism=2" in line


def test_concurrency_report_is_emitted_at_info(tmp_path: Path, caplog: Any) -> None:
    with caplog.at_level(logging.INFO, logger="zicato.orchestrator"):
        line = log_effective_concurrency(_workspace(tmp_path, {}))
    assert line in caplog.text


def test_resolve_parallelism_reports_the_workspace_tier() -> None:
    assert resolve_parallelism({"parallelism": 7}) == (7, "workspace runtime.parallelism")
    value, source = resolve_parallelism({})
    assert (value, source) == (4, "default")


# ---------------------------------------------------------------------------
# #129 — reasons that carry their numbers
# ---------------------------------------------------------------------------


def _experiment(generation_id: str, decision: str, reason: str = "") -> dict[str, Any]:
    return {
        "generation_id": generation_id,
        "outcome": {"tournament_decision": decision, "rejection_reason": reason},
    }


def test_stalled_loop_breaks_the_streak_down_by_cause() -> None:
    """Three rejections for one reason and three for another are different stalls."""
    findings = detect_stalled_loop(
        [
            _experiment("v1", "rejected", "insufficient improvement: loss fell by only 0.001"),
            _experiment("v2", "rejected", "insufficient improvement: loss fell by only 0.002"),
            _experiment("v3", "rejected", "pass-rate regression on entries: e7"),
        ]
    )

    assert len(findings) == 1
    summary = findings[0].summary
    assert "3 consecutive generations rejected" in summary
    # Commonest cause first, and the numbers that made each reason unique are
    # bucketed away rather than producing three groups of one.
    assert "2x insufficient improvement" in summary
    assert "1x pass-rate regression on entries" in summary
    assert findings[0].detail["rejection_causes"]["insufficient improvement"] == 2
    assert findings[0].detail["rejection_reasons"]["v3"] == "pass-rate regression on entries: e7"


def test_stalled_loop_buckets_a_colonless_reason_on_its_rule_not_its_numbers() -> None:
    """The namespace-monotonicity family carries no colon before its numbers.

    Its reason opens straight into the measured parenthetical, so keying
    the bucket on a length clip put the champion's aggregate inside the
    key: six rounds rejected by the same rule on the same namespace
    produced six singleton buckets and a breakdown several times longer
    than the summary it was meant to qualify. The cause is the rule plus
    the namespace, which is the thing a reader counts.

    Uses the reason ``gate`` actually composes so a re-wording cannot
    walk out from under the pin.
    """
    reasons = [
        _namespace_regression_reason(
            {"namespace_aggregates": {"rubric": 0.40 + i * 0.011}},
            {"namespace_aggregates": {"rubric": 0.50 + i * 0.011}},
            ["rubric"],
        )
        for i in range(6)
    ]
    assert ": " not in reasons[0].split(";")[0], "the pin assumes a colonless cause clause"

    findings = detect_stalled_loop(
        [_experiment(f"v{i}", "rejected", r) for i, r in enumerate(reasons, start=1)]
    )

    summary = findings[0].summary
    assert "6x monotonicity_regression on namespace=rubric" in summary
    assert findings[0].detail["rejection_causes"] == {
        "monotonicity_regression on namespace=rubric": 6
    }
    # The whole point of a breakdown is that it condenses. A per-round key
    # would make it grow with the streak instead.
    assert len(summary) < 200, summary


def test_stalled_loop_tolerates_a_missing_reason() -> None:
    findings = detect_stalled_loop(
        [_experiment(f"v{i}", "rejected") for i in range(1, 4)],
    )
    assert "3x (no reason recorded)" in findings[0].summary


def _placebo(generation_id: str, delta: float) -> dict[str, Any]:
    from zicato.core.experiment import PLACEBO_HYPOTHESIS_MARKER

    return {
        "generation_id": generation_id,
        "hypothesis": {"core_idea": f"{PLACEBO_HYPOTHESIS_MARKER} no-op"},
        "outcome": {
            "tournament_decision": "promoted",
            "scalar_score_delta": delta,
            "pass_rate_delta": 0.0,
            "drift_loss_delta": delta,
        },
    }


def test_placebo_alarm_shows_the_comparison_that_failed() -> None:
    """A CRITICAL that names only the generation cannot be triaged."""
    findings = detect_placebo_promoted(
        [_placebo("v9", -0.002)],
        promote_margin=0.001,
        noise_floor={"max_abs_delta": 0.04},
    )

    assert len(findings) == 1
    finding = findings[0]
    assert finding.severity == "critical"
    assert "-0.002" in finding.summary
    assert "promote_margin 0.001" in finding.summary
    assert "noise floor 0.04" in finding.summary
    assert finding.detail["promote_margin"] == 0.001
    assert finding.detail["noise_floor_max_abs_delta"] == 0.04


def test_placebo_alarm_without_calibration_still_reports_the_delta() -> None:
    """Both decision parameters are optional; their absence drops a clause only."""
    finding = detect_placebo_promoted([_placebo("v9", -0.002)])[0]

    assert "-0.002" in finding.summary
    assert "promote_margin" not in finding.summary
    assert finding.detail["noise_floor_max_abs_delta"] is None


def test_uncertainty_guard_returns_the_measured_probability() -> None:
    """The bar alone does not say how far the win fell short of it."""
    blocks, p = uncertainty_blocks_promotion((), "v0", "v1", 0.9)
    assert blocks is False
    # No audit ⇒ nothing was fitted, which is NOT the same as p == 0.0.
    assert p is None


def _agg(scalar: float, namespaces: dict[str, float]) -> dict[str, Any]:
    return {
        "drift_loss_mean": scalar,
        "pass_rate": 1.0,
        "expectation_count": 0,
        "entry_count": 0,
        "scalar": scalar,
        "per_entry": {},
        "namespace_aggregates": namespaces,
    }


def test_namespace_regression_reason_cites_both_aggregates() -> None:
    """Which namespace regressed, and by how much, decide what to look at."""
    outcome = evaluate_gate(
        _agg(2.0, {"rubric:": -4.0}),
        _agg(1.0, {"rubric:": -2.0}),
        ScoringWeights(),
    )

    assert outcome.decision == "rejected"
    assert "rubric: (champion -4.000000 -> challenger -2.000000)" in outcome.reason


def test_field_failure_summary_separates_one_fault_from_many() -> None:
    """Four slots failing identically is a broken prompt; four ways is not."""
    same = _field_failure_summary(
        [{"status": "rejected", "reason": "patch did not apply"} for _ in range(3)]
    )
    assert same == "3 slot(s): 3x patch did not apply"

    mixed = _field_failure_summary(
        [
            {"status": "rejected", "reason": "patch did not apply"},
            {"status": "rejected", "reason": "proposer returned invalid JSON"},
        ]
    )
    assert "1x patch did not apply" in mixed
    assert "1x proposer returned invalid JSON" in mixed


def test_field_failure_summary_ignores_applied_slots() -> None:
    assert _field_failure_summary([{"status": "applied", "generation_id": "v1"}]) == ""


def _contestant(generation_id: str, role: str) -> Any:
    from zicato.selection.strategy import Contestant

    return Contestant(generation_id=generation_id, role=role)  # type: ignore[arg-type]


def test_gauntlet_says_which_precondition_was_missing() -> None:
    """A duel that was scheduled and never reported is not an unfielded field."""
    from zicato.selection.strategies.gauntlet import GauntletStrategy

    strategy = GauntletStrategy()
    strategy.seed(_contestant("v0", "champion"), [_contestant("v1", "challenger")])
    unscheduled = strategy.champion()
    assert "never scheduled" in unscheduled.reason

    strategy.next_matchups()
    assert "reported no result" in strategy.champion().reason


def test_single_elim_names_the_bracket_that_produced_no_finalist() -> None:
    from zicato.selection.strategies.single_elim import SingleEliminationStrategy

    strategy = SingleEliminationStrategy()
    strategy.seed(
        _contestant("v0", "champion"),
        [_contestant("v1", "challenger"), _contestant("v2", "challenger")],
    )
    reason = strategy.champion().reason

    assert reason.startswith("no finalist cleared the champion gate: ")
    assert "2 challenger(s)" in reason
    assert "0 duel(s)" in reason
