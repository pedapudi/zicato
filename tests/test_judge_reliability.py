"""Judge test-retest reliability — scripted-judge tests + CLI surface.

Every judge here is scripted/deterministic (no endpoints): python-mode
judges are plain classes in this module resolved by dotted path through
the SAME builder real runs use, and inline judges run on a scripted aux
callable. The seam the mechanism exposes (``aux_call_llm`` — zicato's
``CallLLM`` shape) is exactly where a real auxiliary endpoint slots in
later, unchanged (endpoint-gated).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import goldfive
import pytest
from goldfive.judges import JudgeContext, JudgeVerdict

from zicato.board.judges import Judge
from zicato.health.diagnostics import detect_noisy_judge
from zicato.judge_runtime.reliability import (
    FIXTURE_TRANSCRIPT,
    JudgeReliability,
    declared_judge_specs,
    pairwise_disagreement,
)
from zicato.judge_runtime.reliability import (
    test_retest as retest,
)
from zicato.judge_runtime.reliability import (
    test_retest_board as retest_board,
)

# ---------------------------------------------------------------------------
# Scripted judge doubles (module-level so dotted paths resolve)
# ---------------------------------------------------------------------------


class NeverFiresJudge:
    """Deterministic python judge: never emits drift."""

    name = "never_fires"

    async def evaluate(self, ctx: JudgeContext) -> JudgeVerdict:
        del ctx
        return JudgeVerdict()


class AlwaysFiresJudge:
    """Deterministic python judge: always emits drift."""

    name = "always_fires"

    async def evaluate(self, ctx: JudgeContext) -> JudgeVerdict:
        del ctx
        return JudgeVerdict(
            drift_emitted=True,
            drift_kind="custom",
            severity="warning",
            detail="always",
        )


class FlipFlopJudge:
    """A judge that alternates verdicts on IDENTICAL input — pure noise."""

    name = "flip_flop"

    def __init__(self) -> None:
        self._calls = 0

    async def evaluate(self, ctx: JudgeContext) -> JudgeVerdict:
        del ctx
        self._calls += 1
        if self._calls % 2 == 1:
            return JudgeVerdict(
                drift_emitted=True,
                drift_kind="custom",
                severity="warning",
                detail="flip",
            )
        return JudgeVerdict()


_FLAKY_AUX_STATE = {"calls": 0}


async def flaky_aux(system: str, user: str, model: str) -> str:
    """Scripted aux double: alternates VIOLATION / OK per call."""
    del system, user, model
    _FLAKY_AUX_STATE["calls"] += 1
    if _FLAKY_AUX_STATE["calls"] % 2 == 1:
        return "VIOLATION sarcasm detected"
    return "OK no violation"


async def steady_aux(system: str, user: str, model: str) -> str:
    """Scripted aux double: identical verdict every call."""
    del system, user, model
    return "OK no violation"


async def _unused_aux(system: str, user: str, model: str) -> str:
    raise AssertionError("aux must not be called for a live/python judge")


# ---------------------------------------------------------------------------
# Pure math
# ---------------------------------------------------------------------------


def test_pairwise_disagreement_math() -> None:
    assert pairwise_disagreement(0, 3) == 0.0
    assert pairwise_disagreement(3, 3) == 0.0
    assert pairwise_disagreement(1, 2) == 1.0  # the k=2 alternator
    assert pairwise_disagreement(1, 3) == pytest.approx(2 / 3)
    assert pairwise_disagreement(2, 4) == pytest.approx(4 / 6)
    assert pairwise_disagreement(0, 1) == 0.0  # degenerate: nothing to pair
    assert pairwise_disagreement(0, 0) == 0.0


def test_retest_requires_at_least_two_judgements() -> None:
    with pytest.raises(ValueError):
        asyncio.run(retest(NeverFiresJudge(), "text", steady_aux, k=1))


# ---------------------------------------------------------------------------
# Deterministic judges → disagreement 0 (both live-object and spec paths)
# ---------------------------------------------------------------------------


def test_deterministic_live_judges_measure_zero_disagreement() -> None:
    for judge in (NeverFiresJudge(), AlwaysFiresJudge()):
        rel = asyncio.run(retest(judge, FIXTURE_TRANSCRIPT, _unused_aux, k=4))
        assert rel.k == 4
        assert rel.disagreement_rate == 0.0
        assert rel.verdicts == (rel.verdicts[0],) * 4


def test_deterministic_python_spec_via_builder_is_zero() -> None:
    """The spec path — the SAME builder real runs use resolves the dotted
    path; the judge name is re-pinned to the spec's."""
    spec = Judge.python(
        "steady_python",
        "tests.test_judge_reliability.AlwaysFiresJudge",
        severity=goldfive.DriftSeverity.WARNING,
    )
    rel = asyncio.run(retest(spec, FIXTURE_TRANSCRIPT, _unused_aux, k=3))
    assert rel.judge_name == "steady_python"
    assert rel.fired == 3
    assert rel.disagreement_rate == 0.0


# ---------------------------------------------------------------------------
# Flip-flopping judges → the finding fires
# ---------------------------------------------------------------------------


def test_flip_flop_python_judge_maximal_disagreement() -> None:
    rel = asyncio.run(retest(FlipFlopJudge(), FIXTURE_TRANSCRIPT, _unused_aux, k=2))
    assert rel.verdicts == (True, False)
    assert rel.disagreement_rate == 1.0
    (finding,) = detect_noisy_judge([rel])
    assert finding.code == "noisy_judge"
    assert finding.severity == "warning"
    assert "per_judge_weights" in finding.detail["recommendation"]


def test_flaky_inline_judge_via_scripted_aux() -> None:
    """An inline (LLM-backed) judge on a flip-flopping aux endpoint: the
    endpoint seam is the CallLLM callable — a real one slots in here."""
    _FLAKY_AUX_STATE["calls"] = 0
    spec = Judge.custom(
        "tone_check", "No sarcasm in the reasoning.", severity=goldfive.DriftSeverity.WARNING
    )
    rel = asyncio.run(retest(spec, FIXTURE_TRANSCRIPT, flaky_aux, k=2))
    assert rel.judge_name == "tone_check"
    assert rel.verdicts == (True, False)
    assert rel.disagreement_rate == 1.0

    steady = asyncio.run(retest(spec, FIXTURE_TRANSCRIPT, steady_aux, k=3))
    assert steady.fired == 0
    assert steady.disagreement_rate == 0.0


# ---------------------------------------------------------------------------
# Detector thresholding + dict input
# ---------------------------------------------------------------------------


def test_detector_threshold_and_json_shape() -> None:
    quiet = JudgeReliability(
        judge_name="quiet", k=4, fired=0, verdicts=(False,) * 4, disagreement_rate=0.0, details=()
    )
    noisy = JudgeReliability(
        judge_name="noisy",
        k=2,
        fired=1,
        verdicts=(True, False),
        disagreement_rate=1.0,
        details=("flip", ""),
    )
    # Silent at/below threshold; fires above; accepts to_json dicts too.
    assert detect_noisy_judge([quiet]) == []
    assert detect_noisy_judge([noisy], threshold=1.0) == []
    findings = detect_noisy_judge([quiet.to_json(), noisy.to_json()])
    assert [f.detail["judge_name"] for f in findings] == ["noisy"]


# ---------------------------------------------------------------------------
# Board-level sweep (declared judges, unique by name)
# ---------------------------------------------------------------------------


def test_retest_board_dedups_by_judge_name() -> None:
    tone = Judge.custom("tone_check", "No sarcasm.", severity=goldfive.DriftSeverity.WARNING)
    steady = Judge.python(
        "steady_python",
        "tests.test_judge_reliability.NeverFiresJudge",
        severity=goldfive.DriftSeverity.INFO,
    )
    entries = [
        SimpleNamespace(judges=(tone, steady)),
        SimpleNamespace(judges=(tone,)),  # re-declared — measured once
        SimpleNamespace(judges=()),
    ]
    assert [s.name for s in declared_judge_specs(entries)] == ["tone_check", "steady_python"]
    rels = asyncio.run(retest_board(entries, FIXTURE_TRANSCRIPT, steady_aux, k=2))
    assert [r.judge_name for r in rels] == ["tone_check", "steady_python"]
    assert all(r.disagreement_rate == 0.0 for r in rels)


# ---------------------------------------------------------------------------
# CLI surface — `zicato board judges [--test-retest]`
# ---------------------------------------------------------------------------


def _write_default_board(tmp_path: Path) -> Path:
    """A minimal single-epoch workspace whose board declares one judge."""
    from zicato.core.workspace import board_path

    workspace = tmp_path / ".zicato"
    board_file = board_path(workspace, "default")
    board_file.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "id": "j1",
        "kind": "single_turn",
        "wall_clock_budget_seconds": 60,
        "weight": 1.0,
        "tags": [],
        "input": "Say hello politely.",
        "judges": [
            {"name": "tone_check", "mode": "inline", "body": "No sarcasm.", "severity": "warning"}
        ],
    }
    board_file.write_text(json.dumps(entry) + "\n")
    return workspace


def test_cli_lists_judges_without_retest(tmp_path: Path) -> None:
    from click.testing import CliRunner

    from zicato.cli.discovery import build_cli_root

    workspace = _write_default_board(tmp_path)
    result = CliRunner().invoke(
        build_cli_root(), ["board", "judges", "--workspace", str(workspace)]
    )
    assert result.exit_code == 0, result.output
    assert "1 declared judge(s)" in result.output
    assert "tone_check" in result.output
    assert "Test-retest" not in result.output


def test_cli_retest_flags_noisy_judge(tmp_path: Path) -> None:
    from click.testing import CliRunner

    from zicato.cli.discovery import build_cli_root

    _FLAKY_AUX_STATE["calls"] = 0
    workspace = _write_default_board(tmp_path)
    result = CliRunner().invoke(
        build_cli_root(),
        [
            "board",
            "judges",
            "--workspace",
            str(workspace),
            "--test-retest",
            "--retest-k",
            "2",
            "--auxiliary-call-llm",
            "tests.test_judge_reliability:flaky_aux",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "disagreement=100%" in result.output
    # The finding + recommendation are stderr warnings (recommend-only);
    # CliRunner mixes stderr into ``output`` by default.
    assert "noisy_judge" in result.output
    assert "per_judge_weights" in result.output


def test_cli_retest_steady_judge_is_clean(tmp_path: Path) -> None:
    from click.testing import CliRunner

    from zicato.cli.discovery import build_cli_root

    workspace = _write_default_board(tmp_path)
    result = CliRunner().invoke(
        build_cli_root(),
        [
            "board",
            "judges",
            "--workspace",
            str(workspace),
            "--test-retest",
            "--retest-k",
            "3",
            "--auxiliary-call-llm",
            "tests.test_judge_reliability:steady_aux",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "disagreement=0%" in result.output
    assert "self-consistent" in result.output


def test_cli_retest_requires_aux(tmp_path: Path) -> None:
    from click.testing import CliRunner

    from zicato.cli.discovery import build_cli_root

    workspace = _write_default_board(tmp_path)
    result = CliRunner().invoke(
        build_cli_root(),
        ["board", "judges", "--workspace", str(workspace), "--test-retest"],
    )
    assert result.exit_code != 0
    assert "--auxiliary-call-llm" in result.output
