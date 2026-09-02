"""Every below-noise-floor surface derives from the one calibration owner.

Four places report that ``promote_margin`` sits inside the measured A/A noise:
the round-0 evolve log line, the ``margin_below_noise_floor`` health finding,
the board-reflection finding, and the ``promotion_hygiene`` practice check.
All four render one
:class:`zicato.tournament.calibration.MarginNoiseAssessment`, so they cannot
disagree about whether the condition holds or what the margin should be raised
to.

The checks here hold that structure in place. A static scan asserts the 2.5
sigma multiple has a single definition under ``src/``; behavioural checks
substitute a value into the domain assessment and assert every surface reports
the substituted value rather than one it derived itself; the rest pin the
domain function's own tolerant reads and guards.
"""

from __future__ import annotations

import ast
import logging
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from zicato.core.board import BoardEntry, Expectation, ExpectationKind
from zicato.core.scoring_config import ScoringWeights
from zicato.reflection.findings import derive_findings
from zicato.reflection.practices import check_promotion_hygiene
from zicato.tournament import calibration as cal

SRC_ROOT = Path(__file__).resolve().parents[1] / "src" / "zicato"

#: A floor whose two statistics are both usable and far apart, so a surface
#: that scaled the wrong one would be visible in the assertions.
FLOOR: dict[str, Any] = {
    "generation_id": "g0",
    "epoch_id": "e0",
    "runs": 5,
    "scalars": [1.0, 1.1, 1.2, 1.05, 1.15],
    "max_abs_delta": 0.20,
    "delta_std": 0.04,
    "measured_at": "2026-09-01T00:00:00+00:00",
}
MARGIN: float = 0.01

#: Values no real measurement produces, injected into the domain assessment so
#: a surface that re-derived its own numbers would keep reporting the real ones.
SENTINEL_FLOOR: float = 0.424242
SENTINEL_RECOMMENDATION: float = 0.777777


def _numeric_literals(path: Path, value: float) -> list[str]:
    """Every occurrence of ``value`` as a NUMBER in ``path``, named by context.

    Parses rather than greps, so the same digits appearing in a docstring or a
    comment (where the module explains the multiple in prose) do not count as a
    second declaration.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    hits: list[str] = []
    for parent in ast.walk(tree):
        for node in ast.iter_child_nodes(parent):
            if not isinstance(node, ast.Constant):
                continue
            if not isinstance(node.value, float) or node.value != value:
                continue
            if isinstance(parent, ast.Assign) and len(parent.targets) == 1:
                hits.append(f"{path.name}:{ast.unparse(parent.targets[0])}")
            elif isinstance(parent, ast.AnnAssign):
                hits.append(f"{path.name}:{ast.unparse(parent.target)}")
            else:
                hits.append(f"{path.name}:{node.lineno}")
    return hits


def test_the_noise_multiple_has_exactly_one_definition() -> None:
    """The 2.5 sigma multiple is declared once, in the calibration domain."""
    found = sorted(
        hit
        for path in SRC_ROOT.rglob("*.py")
        for hit in _numeric_literals(path, cal.MARGIN_NOISE_MULTIPLE)
    )
    assert found == ["calibration.py:MARGIN_NOISE_MULTIPLE"]


def _pin_assessment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the domain assessment report the sentinels, for every caller.

    Patches the single constructor the whole assessment funnels through:
    ``assess_margin_against_floor_record`` builds on it, ``margin_below_floor``
    delegates to that, and the reflection finding calls it directly.
    """
    real = cal.assess_margin_against_floor

    def pinned(**kwargs: Any) -> cal.MarginNoiseAssessment | None:
        assessment = real(**kwargs)
        if assessment is None:
            return None
        return replace(
            assessment,
            max_abs_delta=SENTINEL_FLOOR,
            recommended_margin=SENTINEL_RECOMMENDATION,
        )

    monkeypatch.setattr(cal, "assess_margin_against_floor", pinned)


def test_the_round_0_log_line_renders_the_domain_assessment(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from zicato.evolve.round_prepare import _warn_margin_below_noise_floor
    from zicato.health import inputs as health_inputs

    _pin_assessment(monkeypatch)
    monkeypatch.setattr(
        health_inputs,
        "epoch_noise_floor_inputs",
        lambda workspace_root, epoch_id: (FLOOR, MARGIN, False),
    )
    with caplog.at_level(logging.WARNING, logger="zicato.orchestrator"):
        _warn_margin_below_noise_floor(Path("/nonexistent-workspace"), "epoch-0")
    (line,) = (m for m in caplog.messages if "noise floor" in m)
    assert f"{SENTINEL_FLOOR:.6g}" in line
    assert f"{SENTINEL_RECOMMENDATION:.6g}" in line


def test_the_health_finding_renders_the_domain_assessment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from zicato.health.diagnostics import detect_margin_below_noise_floor

    _pin_assessment(monkeypatch)
    (finding,) = detect_margin_below_noise_floor(FLOOR, MARGIN, evidence_gate_on=False)
    assert finding.detail["noise_floor_max_abs_delta"] == SENTINEL_FLOOR
    assert f"{SENTINEL_FLOOR:.6g}" in finding.summary


def test_the_reflection_finding_renders_the_domain_assessment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _pin_assessment(monkeypatch)
    findings = derive_findings(
        scorecards=[],
        adjudications=[],
        promote_margin=MARGIN,
        noise_floor_max_abs_delta=FLOOR["max_abs_delta"],
        noise_floor_delta_std=FLOOR["delta_std"],
    )
    (margin_finding,) = (f for f in findings if f.pillar == "calibration")
    assert margin_finding.proposed_op == {
        "op": "set_gate",
        "args": {"promote_margin": SENTINEL_RECOMMENDATION},
    }


def test_the_practice_check_renders_the_domain_assessment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _pin_assessment(monkeypatch)
    board = [
        BoardEntry(
            id=f"e{i}",
            kind="single_turn",
            wall_clock_budget_seconds=30,
            input="hi",
            expectation=Expectation(kind=ExpectationKind("expected_text"), spec="x"),
        )
        for i in range(6)
    ]
    check = check_promotion_hygiene(
        weights=ScoringWeights(promote_margin=MARGIN),
        experiments=[{"generation_id": "g1", "outcome": {"tournament_decision": "promoted"}}],
        board_entries=board,
        noise_floor=FLOOR,
    )
    assert check.evidence["margin_below_floor"] is True
    assert check.evidence["recommended_promote_margin"] == SENTINEL_RECOMMENDATION
    assert check.proposed_op == {
        "op": "set_gate",
        "args": {"promote_margin": SENTINEL_RECOMMENDATION},
    }
    assert f"{SENTINEL_FLOOR:.4g}" in check.headline


def test_the_predicate_and_the_assessment_agree_on_every_record() -> None:
    """``margin_below_floor`` fires on exactly the records the assessment does.

    The tolerant reads (absent record, absent statistic, unparseable statistic,
    a margin that exactly equals the floor) are the ones a workspace that never
    calibrated actually hits, so they are pinned together rather than left to
    two independent implementations.
    """
    records: list[dict[str, Any] | None] = [
        None,
        {},
        {"max_abs_delta": "junk"},
        {"max_abs_delta": 0.0},
        {"max_abs_delta": 0.20},
        {"max_abs_delta": 0.20, "delta_std": 0.04},
        {"max_abs_delta": 0.20, "delta_std": "junk"},
    ]
    for record in records:
        for margin in (0.0, 0.01, 0.20, 0.5):
            fires = cal.assess_margin_against_floor_record(margin, record) is not None
            assert cal.margin_below_floor(margin, record) is fires, (record, margin)


def test_a_record_without_delta_std_falls_back_to_the_range() -> None:
    """The pre-#112 record shape still yields a recommendation, from the range."""
    assessment = cal.assess_margin_against_floor_record(0.01, {"max_abs_delta": 0.20})
    assert assessment is not None
    assert assessment.used_delta_std is False
    assert assessment.recommended_margin == pytest.approx(cal.MARGIN_NOISE_MULTIPLE * 0.20)
    assert assessment.recommendation_raises_margin is True


def test_the_two_statistics_can_disagree_and_the_assessment_says_so() -> None:
    """A K-inflated range fires the condition the dispersion does not corroborate.

    ``recommendation_raises_margin`` is the guard every appliable surface
    checks: acting on this assessment would LOWER ``promote_margin``.
    """
    assessment = cal.assess_margin_against_floor_record(
        0.09, {"max_abs_delta": 0.30, "delta_std": 0.02}
    )
    assert assessment is not None
    assert assessment.used_delta_std is True
    assert assessment.rounded_recommended_margin == pytest.approx(0.05)
    assert assessment.recommendation_raises_margin is False


def test_a_zero_floor_is_an_absent_measurement() -> None:
    """``floor_is_measured`` separates "no noise measured" from "margin too low"."""
    assert cal.assess_margin_against_floor_record(0.0, {"max_abs_delta": 0.0}) is None
    assessment = cal.assess_margin_against_floor(promote_margin=-1.0, max_abs_delta=0.0)
    assert assessment is not None
    assert assessment.floor_is_measured is False
