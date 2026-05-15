"""Tests for the built-in :func:`zicato.board.rubric.evaluate_rubric_judge` matcher."""

from __future__ import annotations

import asyncio
import json

import pytest

from zicato.board.matchers import evaluate_expectation
from zicato.board.rubric import evaluate_rubric_judge
from zicato.board.predicates import Rubric
from zicato.core.types import Expectation, RunResult


def _result(
    final_output: str = "the agent answered concisely",
    transcript: tuple[str, ...] | None = None,
) -> RunResult:
    if transcript is None:
        transcript = (final_output,)
    return RunResult(
        run_id="r_rubric_test",
        entry_id="e_rubric_test",
        final_output=final_output,
        transcript=transcript,
        runtime_ms=10,
    )


def _judge_returning(payload: dict | str):
    """Build an aux_call_llm that returns ``payload`` (or raw string) on every call."""
    raw = payload if isinstance(payload, str) else json.dumps(payload)

    async def _aux(system: str, user: str, model: str) -> str:
        return raw

    return _aux


# ---------------------------------------------------------------------------
# Pass / fail by threshold
# ---------------------------------------------------------------------------


def test_rubric_passes_when_score_meets_threshold() -> None:
    """score >= threshold → passed=True."""
    exp = Rubric.judge("clarity 0-10", threshold=7.0, scale=(0.0, 10.0))
    aux = _judge_returning({"score": 8.5, "dimensions": {}, "reasoning": "good"})
    out = asyncio.run(evaluate_rubric_judge(exp, _result(), aux))
    assert out.kind == "rubric"
    assert out.passed is True
    assert "score=8.50" in out.detail
    assert "reasoning=good" in out.detail


def test_rubric_fails_when_score_below_threshold() -> None:
    """score < threshold → passed=False."""
    exp = Rubric.judge("clarity 0-10", threshold=7.0)
    aux = _judge_returning({"score": 6.0, "dimensions": {}, "reasoning": "meh"})
    out = asyncio.run(evaluate_rubric_judge(exp, _result(), aux))
    assert out.passed is False
    assert "score=6.00" in out.detail


def test_rubric_threshold_none_is_advisory_and_always_passes() -> None:
    """Without a threshold, the rubric is advisory — always passes."""
    exp = Rubric.judge("clarity 0-10")  # threshold defaults to None
    aux = _judge_returning({"score": 0.1, "dimensions": {}, "reasoning": "terrible"})
    out = asyncio.run(evaluate_rubric_judge(exp, _result(), aux))
    assert out.passed is True
    # The score still appears in detail so the operator can inspect.
    assert "score=0.10" in out.detail


# ---------------------------------------------------------------------------
# Malformed responses
# ---------------------------------------------------------------------------


def test_rubric_malformed_json_response_fails_cleanly() -> None:
    """Non-JSON response → passed=False with descriptive detail."""
    exp = Rubric.judge("rubric text", threshold=5.0)
    aux = _judge_returning("not-json-at-all")
    out = asyncio.run(evaluate_rubric_judge(exp, _result(), aux))
    assert out.passed is False
    assert "not valid JSON" in out.detail


def test_rubric_missing_score_field_fails_cleanly() -> None:
    """Response without ``score`` → passed=False."""
    exp = Rubric.judge("rubric text", threshold=5.0)
    aux = _judge_returning({"reasoning": "forgot the score"})
    out = asyncio.run(evaluate_rubric_judge(exp, _result(), aux))
    assert out.passed is False
    assert "must be" in out.detail


def test_rubric_non_numeric_score_fails_cleanly() -> None:
    """``score`` field that isn't a number → passed=False."""
    exp = Rubric.judge("rubric text", threshold=5.0)
    aux = _judge_returning({"score": "not-a-number", "reasoning": "x"})
    out = asyncio.run(evaluate_rubric_judge(exp, _result(), aux))
    assert out.passed is False
    assert "not a number" in out.detail


# ---------------------------------------------------------------------------
# Scale parameter
# ---------------------------------------------------------------------------


def test_rubric_scale_parameter_respected_in_user_prompt() -> None:
    """The rubric's scale appears in the user prompt sent to the judge."""
    exp = Rubric.judge("clarity", threshold=3.0, scale=(0.0, 5.0))

    captured: dict[str, str] = {}

    async def aux(system: str, user: str, model: str) -> str:
        captured["user"] = user
        return json.dumps({"score": 4.0, "dimensions": {}, "reasoning": "ok"})

    out = asyncio.run(evaluate_rubric_judge(exp, _result(), aux))
    assert out.passed is True
    assert "0.0 to 5.0" in captured["user"]


def test_rubric_threshold_outside_scale_rejected_at_construction() -> None:
    """The builder rejects a threshold outside the declared scale."""
    with pytest.raises(ValueError, match="outside scale"):
        Rubric.judge("clarity", threshold=11.0, scale=(0.0, 10.0))


def test_rubric_invalid_scale_rejected_at_construction() -> None:
    """An empty / reversed scale is rejected at construction."""
    with pytest.raises(ValueError, match="lo < hi"):
        Rubric.judge("clarity", scale=(10.0, 0.0))


# ---------------------------------------------------------------------------
# Code-fence stripping
# ---------------------------------------------------------------------------


def test_rubric_code_fence_stripping_json_language_tag() -> None:
    """A ```json ... ``` fenced response is parsed."""
    exp = Rubric.judge("clarity", threshold=5.0)
    fenced = "```json\n" + json.dumps({"score": 7.0, "reasoning": "ok"}) + "\n```"
    aux = _judge_returning(fenced)
    out = asyncio.run(evaluate_rubric_judge(exp, _result(), aux))
    assert out.passed is True


def test_rubric_code_fence_stripping_bare_backticks() -> None:
    """A ``` ... ``` fenced response without language tag is parsed."""
    exp = Rubric.judge("clarity", threshold=5.0)
    fenced = "```\n" + json.dumps({"score": 7.0, "reasoning": "ok"}) + "\n```"
    aux = _judge_returning(fenced)
    out = asyncio.run(evaluate_rubric_judge(exp, _result(), aux))
    assert out.passed is True


# ---------------------------------------------------------------------------
# Transcript handling
# ---------------------------------------------------------------------------


def test_rubric_multi_turn_transcript_joined_with_newlines() -> None:
    """Multi-turn transcripts are joined and embedded into the user prompt."""
    exp = Rubric.judge("clarity", threshold=5.0)
    multi = _result(
        final_output="final turn",
        transcript=("turn one", "turn two", "final turn"),
    )

    captured: dict[str, str] = {}

    async def aux(system: str, user: str, model: str) -> str:
        captured["user"] = user
        return json.dumps({"score": 8.0, "reasoning": "ok"})

    out = asyncio.run(evaluate_rubric_judge(exp, multi, aux))
    assert out.passed is True
    assert "turn one\nturn two\nfinal turn" in captured["user"]


def test_rubric_single_turn_uses_final_output_only() -> None:
    """Single-turn transcripts pass ``final_output`` instead of joining."""
    exp = Rubric.judge("clarity", threshold=5.0)
    single = _result(final_output="only turn", transcript=("only turn",))

    captured: dict[str, str] = {}

    async def aux(system: str, user: str, model: str) -> str:
        captured["user"] = user
        return json.dumps({"score": 8.0, "reasoning": "ok"})

    asyncio.run(evaluate_rubric_judge(exp, single, aux))
    assert "only turn" in captured["user"]


# ---------------------------------------------------------------------------
# Aux callable missing / raising
# ---------------------------------------------------------------------------


def test_rubric_without_aux_call_llm_fails_gracefully() -> None:
    """Missing aux_call_llm → passed=False, descriptive detail."""
    exp = Rubric.judge("clarity", threshold=5.0)
    out = asyncio.run(evaluate_rubric_judge(exp, _result(), None))
    assert out.passed is False
    assert "aux_call_llm" in out.detail


def test_rubric_aux_call_raising_is_captured() -> None:
    """An aux callable that raises is captured into the result."""
    exp = Rubric.judge("clarity", threshold=5.0)

    async def aux(system: str, user: str, model: str) -> str:
        raise RuntimeError("backend down")

    out = asyncio.run(evaluate_rubric_judge(exp, _result(), aux))
    assert out.passed is False
    assert "backend down" in out.detail


# ---------------------------------------------------------------------------
# Dispatcher integration
# ---------------------------------------------------------------------------


def test_evaluate_expectation_dispatches_rubric_kind() -> None:
    """The matchers dispatcher routes ``rubric`` to the rubric judge."""
    exp = Rubric.judge("clarity", threshold=5.0)
    aux = _judge_returning({"score": 9.0, "dimensions": {}, "reasoning": "good"})
    out = asyncio.run(evaluate_expectation(exp, _result(), aux))
    assert out.kind == "rubric"
    assert out.passed is True


def test_rubric_dimensions_rendered_in_detail() -> None:
    """Per-dimension scores show up in the detail string."""
    exp = Rubric.judge("clarity", threshold=5.0)
    aux = _judge_returning(
        {
            "score": 8.0,
            "dimensions": {"clarity": 9.0, "tone": 7.0},
            "reasoning": "okay",
        }
    )
    out = asyncio.run(evaluate_rubric_judge(exp, _result(), aux))
    assert out.passed is True
    assert "clarity=9.00" in out.detail
    assert "tone=7.00" in out.detail
