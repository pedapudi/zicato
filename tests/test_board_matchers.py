"""Tests for the five expectation matchers."""

from __future__ import annotations

import json
import sys
import types
from typing import Any

import pytest

from zicato.board.matchers import evaluate_expectation
from zicato.core.types import Expectation, RunResult


def _result(final_output: str = "", transcript: tuple[str, ...] | None = None) -> RunResult:
    if transcript is None:
        transcript = (final_output,) if final_output else ()
    return RunResult(
        run_id="r_test",
        entry_id="e_test",
        final_output=final_output,
        transcript=transcript,
        runtime_ms=1,
    )


# ---------------------------------------------------------------------------
# predicate
# ---------------------------------------------------------------------------


def _install_predicate_module(name: str, attrs: dict[str, Any]) -> None:
    """Inject a synthetic module into sys.modules so import-by-dotted-path works."""
    module = types.ModuleType(name)
    for attr_name, attr_value in attrs.items():
        setattr(module, attr_name, attr_value)
    sys.modules[name] = module


@pytest.fixture
def predicate_module() -> str:
    """Create a synthetic module exposing both sync and async predicates."""
    module_name = "zicato_test_predicates_synthetic"

    def sync_pass(result: RunResult) -> bool:
        return "ok" in result.final_output

    def sync_fail(result: RunResult) -> bool:
        return False

    async def async_pass(result: RunResult) -> bool:
        return result.final_output == "answer"

    async def async_fail(result: RunResult) -> bool:
        return False

    def raises(result: RunResult) -> bool:
        raise RuntimeError("boom")

    def non_bool(result: RunResult) -> Any:
        return "yes"

    _install_predicate_module(
        module_name,
        {
            "sync_pass": sync_pass,
            "sync_fail": sync_fail,
            "async_pass": async_pass,
            "async_fail": async_fail,
            "raises": raises,
            "non_bool": non_bool,
            "not_callable": 42,
        },
    )
    yield module_name
    sys.modules.pop(module_name, None)


async def test_predicate_sync_pass(predicate_module: str) -> None:
    res = await evaluate_expectation(
        Expectation(kind="predicate", spec=f"{predicate_module}.sync_pass"),
        _result("the answer is ok"),
    )
    assert res.kind == "predicate"
    assert res.passed is True


async def test_predicate_sync_fail(predicate_module: str) -> None:
    res = await evaluate_expectation(
        Expectation(kind="predicate", spec=f"{predicate_module}.sync_fail"),
        _result("anything"),
    )
    assert res.passed is False
    assert "False" in res.detail


async def test_predicate_async_pass(predicate_module: str) -> None:
    """Async predicates are awaited transparently."""
    res = await evaluate_expectation(
        Expectation(kind="predicate", spec=f"{predicate_module}.async_pass"),
        _result("answer"),
    )
    assert res.passed is True


async def test_predicate_async_fail(predicate_module: str) -> None:
    res = await evaluate_expectation(
        Expectation(kind="predicate", spec=f"{predicate_module}.async_fail"),
        _result("answer"),
    )
    assert res.passed is False


async def test_predicate_import_error() -> None:
    res = await evaluate_expectation(
        Expectation(kind="predicate", spec="no.such.module.nope"),
        _result("x"),
    )
    assert res.passed is False
    assert "import" in res.detail.lower()


async def test_predicate_raises_is_caught(predicate_module: str) -> None:
    res = await evaluate_expectation(
        Expectation(kind="predicate", spec=f"{predicate_module}.raises"),
        _result("x"),
    )
    assert res.passed is False
    assert "raised" in res.detail


async def test_predicate_non_bool_returns_false(predicate_module: str) -> None:
    res = await evaluate_expectation(
        Expectation(kind="predicate", spec=f"{predicate_module}.non_bool"),
        _result("x"),
    )
    assert res.passed is False
    assert "expected bool" in res.detail


async def test_predicate_not_callable_returns_false(predicate_module: str) -> None:
    res = await evaluate_expectation(
        Expectation(kind="predicate", spec=f"{predicate_module}.not_callable"),
        _result("x"),
    )
    assert res.passed is False
    assert "callable" in res.detail.lower()


# ---------------------------------------------------------------------------
# expected_text
# ---------------------------------------------------------------------------


async def test_expected_text_substring_present() -> None:
    res = await evaluate_expectation(
        Expectation(kind="expected_text", spec="hello"),
        _result("well, hello world"),
    )
    assert res.passed is True


async def test_expected_text_substring_missing() -> None:
    res = await evaluate_expectation(
        Expectation(kind="expected_text", spec="goodbye"),
        _result("well, hello world"),
    )
    assert res.passed is False
    assert "goodbye" in res.detail


async def test_expected_text_empty_spec_fails() -> None:
    res = await evaluate_expectation(
        Expectation(kind="expected_text", spec=""),
        _result("anything"),
    )
    assert res.passed is False


# ---------------------------------------------------------------------------
# regex
# ---------------------------------------------------------------------------


async def test_regex_matches_with_dotall() -> None:
    res = await evaluate_expectation(
        Expectation(kind="regex", spec=r"start.*end"),
        _result("start\nmiddle\nend"),
    )
    assert res.passed is True
    assert "matched at" in res.detail


async def test_regex_no_match() -> None:
    res = await evaluate_expectation(
        Expectation(kind="regex", spec=r"^\d+$"),
        _result("not a number"),
    )
    assert res.passed is False


async def test_regex_invalid_pattern_returns_false() -> None:
    res = await evaluate_expectation(
        Expectation(kind="regex", spec="["),
        _result("anything"),
    )
    assert res.passed is False
    assert "invalid regex" in res.detail


# ---------------------------------------------------------------------------
# json_schema
# ---------------------------------------------------------------------------


SCHEMA = json.dumps(
    {
        "type": "object",
        "required": ["summary", "citations"],
        "properties": {
            "summary": {"type": "string"},
            "citations": {"type": "array", "items": {"type": "string"}},
        },
    }
)


async def test_json_schema_valid() -> None:
    payload = json.dumps({"summary": "ok", "citations": ["a", "b"]})
    res = await evaluate_expectation(
        Expectation(kind="json_schema", spec=SCHEMA),
        _result(payload),
    )
    assert res.passed is True


async def test_json_schema_missing_required_field() -> None:
    payload = json.dumps({"summary": "ok"})
    res = await evaluate_expectation(
        Expectation(kind="json_schema", spec=SCHEMA),
        _result(payload),
    )
    assert res.passed is False
    assert "citations" in res.detail or "required" in res.detail


async def test_json_schema_non_json_output_fails() -> None:
    res = await evaluate_expectation(
        Expectation(kind="json_schema", spec=SCHEMA),
        _result("not json at all"),
    )
    assert res.passed is False
    assert "not valid JSON" in res.detail


async def test_json_schema_invalid_schema_fails() -> None:
    res = await evaluate_expectation(
        Expectation(
            kind="json_schema",
            spec=json.dumps({"type": "not_a_real_type"}),
        ),
        _result(json.dumps({})),
    )
    assert res.passed is False


# ---------------------------------------------------------------------------
# judge
# ---------------------------------------------------------------------------


@pytest.fixture
def judge_module() -> str:
    """Synthetic module exposing a judge-prompt factory."""
    module_name = "zicato_test_judges_synthetic"

    def prompt_factory() -> dict[str, str]:
        return {
            "system": "You judge transcripts.",
            "user_template": "Did the agent answer? final={result.final_output}",
        }

    def bad_factory() -> dict[str, str]:
        return {"system": "x"}  # missing user_template

    _install_predicate_module(
        module_name,
        {
            "good": prompt_factory,
            "bad": bad_factory,
            "not_callable": "x",
        },
    )
    yield module_name
    sys.modules.pop(module_name, None)


async def test_judge_passes_with_canned_response(judge_module: str) -> None:
    captured: dict[str, str] = {}

    async def aux(system: str, user: str, model: str) -> str:
        captured["system"] = system
        captured["user"] = user
        captured["model"] = model
        return json.dumps({"pass": True, "reason": "agent answered crisply"})

    res = await evaluate_expectation(
        Expectation(kind="judge", spec=f"{judge_module}.good"),
        _result("yes, answered"),
        aux_call_llm=aux,
    )
    assert res.passed is True
    assert res.detail == "agent answered crisply"
    assert "yes, answered" in captured["user"]
    assert captured["system"] == "You judge transcripts."


async def test_judge_fails_with_canned_response(judge_module: str) -> None:
    async def aux(system: str, user: str, model: str) -> str:
        return json.dumps({"pass": False, "reason": "agent dodged the question"})

    res = await evaluate_expectation(
        Expectation(kind="judge", spec=f"{judge_module}.good"),
        _result("hmm"),
        aux_call_llm=aux,
    )
    assert res.passed is False
    assert "dodged" in res.detail


async def test_judge_without_aux_callable_fails(judge_module: str) -> None:
    res = await evaluate_expectation(
        Expectation(kind="judge", spec=f"{judge_module}.good"),
        _result("hmm"),
        aux_call_llm=None,
    )
    assert res.passed is False
    assert "aux_call_llm" in res.detail


async def test_judge_bad_factory_shape_fails(judge_module: str) -> None:
    async def aux(system: str, user: str, model: str) -> str:
        return json.dumps({"pass": True, "reason": "x"})

    res = await evaluate_expectation(
        Expectation(kind="judge", spec=f"{judge_module}.bad"),
        _result("x"),
        aux_call_llm=aux,
    )
    assert res.passed is False
    assert "user_template" in res.detail


async def test_judge_non_json_response_fails(judge_module: str) -> None:
    async def aux(system: str, user: str, model: str) -> str:
        return "not json"

    res = await evaluate_expectation(
        Expectation(kind="judge", spec=f"{judge_module}.good"),
        _result("x"),
        aux_call_llm=aux,
    )
    assert res.passed is False
    assert "JSON" in res.detail


async def test_judge_missing_pass_field_fails(judge_module: str) -> None:
    async def aux(system: str, user: str, model: str) -> str:
        return json.dumps({"reason": "no decision"})

    res = await evaluate_expectation(
        Expectation(kind="judge", spec=f"{judge_module}.good"),
        _result("x"),
        aux_call_llm=aux,
    )
    assert res.passed is False
