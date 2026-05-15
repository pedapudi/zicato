"""End-to-end tests for the proposer orchestration loop."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from zicato.core.types import MutationPoint, Pattern
from zicato.proposer.proposer import ProposerError, propose_experiment
from zicato.proposer.prompts import render_system_prompt, render_user_prompt


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _mp(mid: str, *, metadata: dict[str, str] | None = None) -> MutationPoint:
    return MutationPoint(
        id=mid,
        kind="span",
        file=Path(f"/src/{mid}.py"),
        source_root=Path("/src"),
        line_start=1,
        line_end=3,
        content="content",
        content_hash="abc",
        metadata=metadata or {},
    )


_MUTATIONS = [
    _mp("router__sp"),
    _mp("planner__sp"),
]


def _pattern() -> Pattern:
    return Pattern(
        id="pat1",
        kind="drift_kind_frequency",
        summary="off_topic dominates",
        detail={"top_kind": "off_topic", "count": "12"},
        affected_mutation_ids=("router__sp",),
        severity="warning",
    )


def _valid_response() -> str:
    return json.dumps(
        {
            "hypothesis": {
                "core_idea": "tighten router preamble",
                "modulating": ["router__sp"],
                "why": "off_topic dominates",
                "expected_drift_movements": [
                    {"kind": "off_topic", "direction": "decrease", "magnitude": "medium"}
                ],
                "expected_pass_rate_delta": "+0.05",
            },
            "patches": [
                {
                    "mutation_id": "router__sp",
                    "op": "replace",
                    "new_content": "new router prompt",
                    "rationale": "tighter wording",
                }
            ],
        }
    )


class _StubLLM:
    """Records calls and returns scripted responses in order."""

    def __init__(self, responses: list[str | Exception]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, str, str]] = []

    async def __call__(self, system: str, user: str, model: str) -> str:
        self.calls.append((system, user, model))
        if not self._responses:
            raise AssertionError("stub LLM ran out of responses")
        nxt = self._responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_propose_experiment_happy_path() -> None:
    stub = _StubLLM([_valid_response()])
    exp = await propose_experiment(
        epoch_id="e1",
        parent_generation_id="v0",
        new_generation_id="v1",
        patterns=[_pattern()],
        mutations=_MUTATIONS,
        rubric_text="# Rubric\n- Be careful.\n",
        current_loss_summary="loss=2.3, pass_rate=0.6",
        aux_call_llm=stub,
        model="test-model",
        max_retries=2,
    )
    assert exp.id == "exp_e1_v1"
    assert exp.hypothesis.core_idea == "tighten router preamble"
    assert len(exp.patches) == 1
    assert exp.outcome is None
    assert len(stub.calls) == 1


@pytest.mark.asyncio
async def test_propose_experiment_passes_rubric_to_system_prompt() -> None:
    stub = _StubLLM([_valid_response()])
    rubric = "# Rubric\n- Operator hint: prefer router edits.\n"
    await propose_experiment(
        epoch_id="e1",
        parent_generation_id="v0",
        new_generation_id="v1",
        patterns=[],
        mutations=_MUTATIONS,
        rubric_text=rubric,
        current_loss_summary="",
        aux_call_llm=stub,
    )
    system, _, _ = stub.calls[0]
    assert "Operator hint" in system


@pytest.mark.asyncio
async def test_propose_experiment_passes_patterns_and_mutations() -> None:
    stub = _StubLLM([_valid_response()])
    await propose_experiment(
        epoch_id="e1",
        parent_generation_id="v0",
        new_generation_id="v1",
        patterns=[_pattern()],
        mutations=_MUTATIONS,
        rubric_text="",
        current_loss_summary="loss summary text",
        aux_call_llm=stub,
    )
    _, user, _ = stub.calls[0]
    assert "loss summary text" in user
    assert "off_topic dominates" in user
    assert "router__sp" in user
    assert "planner__sp" in user


# ---------------------------------------------------------------------------
# Retry behavior
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_propose_experiment_retries_on_invalid_json() -> None:
    stub = _StubLLM(["this is not json", _valid_response()])
    exp = await propose_experiment(
        epoch_id="e1",
        parent_generation_id="v0",
        new_generation_id="v1",
        patterns=[],
        mutations=_MUTATIONS,
        rubric_text="",
        current_loss_summary="",
        aux_call_llm=stub,
        max_retries=2,
    )
    assert exp.hypothesis.core_idea == "tighten router preamble"
    assert len(stub.calls) == 2
    # The retry's user prompt should mention the previous failure.
    _, retry_user, _ = stub.calls[1]
    assert "Previous attempt was rejected" in retry_user


@pytest.mark.asyncio
async def test_propose_experiment_retries_on_schema_error() -> None:
    bad = json.dumps({"hypothesis": {}, "patches": []})
    stub = _StubLLM([bad, _valid_response()])
    exp = await propose_experiment(
        epoch_id="e1",
        parent_generation_id="v0",
        new_generation_id="v1",
        patterns=[],
        mutations=_MUTATIONS,
        rubric_text="",
        current_loss_summary="",
        aux_call_llm=stub,
        max_retries=2,
    )
    assert exp.id == "exp_e1_v1"
    assert len(stub.calls) == 2


@pytest.mark.asyncio
async def test_propose_experiment_raises_after_exhausting_retries() -> None:
    stub = _StubLLM(["junk1", "junk2", "junk3"])
    with pytest.raises(ProposerError) as exc_info:
        await propose_experiment(
            epoch_id="e1",
            parent_generation_id="v0",
            new_generation_id="v1",
            patterns=[],
            mutations=_MUTATIONS,
            rubric_text="",
            current_loss_summary="",
            aux_call_llm=stub,
            max_retries=2,
        )
    assert len(exc_info.value.attempts) == 3
    assert len(stub.calls) == 3


@pytest.mark.asyncio
async def test_propose_experiment_handles_llm_exception_as_retryable() -> None:
    stub = _StubLLM([RuntimeError("transient"), _valid_response()])
    exp = await propose_experiment(
        epoch_id="e1",
        parent_generation_id="v0",
        new_generation_id="v1",
        patterns=[],
        mutations=_MUTATIONS,
        rubric_text="",
        current_loss_summary="",
        aux_call_llm=stub,
        max_retries=2,
    )
    assert exp.hypothesis.core_idea == "tighten router preamble"
    assert len(stub.calls) == 2


# ---------------------------------------------------------------------------
# Forbidden-id enforcement
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_propose_experiment_rejects_forbidden_patch() -> None:
    stub = _StubLLM([_valid_response()])
    with pytest.raises(ProposerError) as exc_info:
        await propose_experiment(
            epoch_id="e1",
            parent_generation_id="v0",
            new_generation_id="v1",
            patterns=[],
            mutations=_MUTATIONS,
            rubric_text="",
            current_loss_summary="",
            aux_call_llm=stub,
            max_retries=0,
            forbidden_ids=("router__sp",),
        )
    assert any("forbidden" in a.lower() for a in exc_info.value.attempts)


@pytest.mark.asyncio
async def test_propose_experiment_succeeds_when_retry_avoids_forbidden_id() -> None:
    # First response patches the forbidden id; retry patches a different one.
    alt_response = json.dumps(
        {
            "hypothesis": {
                "core_idea": "tighten planner",
                "modulating": ["planner__sp"],
                "why": "switching after forbidden hit",
                "expected_drift_movements": [
                    {"kind": "off_topic", "direction": "decrease", "magnitude": "small"}
                ],
                "expected_pass_rate_delta": "+0.02",
            },
            "patches": [
                {
                    "mutation_id": "planner__sp",
                    "op": "replace",
                    "new_content": "new planner prompt",
                    "rationale": "switched target",
                }
            ],
        }
    )
    stub = _StubLLM([_valid_response(), alt_response])
    exp = await propose_experiment(
        epoch_id="e1",
        parent_generation_id="v0",
        new_generation_id="v1",
        patterns=[],
        mutations=_MUTATIONS,
        rubric_text="",
        current_loss_summary="",
        aux_call_llm=stub,
        max_retries=2,
        forbidden_ids=("router__sp",),
    )
    assert exp.patches[0].mutation_id == "planner__sp"


# ---------------------------------------------------------------------------
# Prompt rendering sanity
# ---------------------------------------------------------------------------


def test_render_system_prompt_embeds_rubric() -> None:
    rendered = render_system_prompt("# Rubric\nSome guidance.")
    assert "Some guidance." in rendered
    # The schema description should mention required field names.
    assert "core_idea" in rendered
    assert "expected_drift_movements" in rendered


def test_render_system_prompt_handles_empty_rubric() -> None:
    rendered = render_system_prompt("")
    assert "(empty)" in rendered


def test_render_user_prompt_includes_feedback_when_present() -> None:
    rendered = render_user_prompt(
        current_loss_summary="loss=1.0",
        patterns=[],
        mutations=_MUTATIONS,
        feedback="bad json",
    )
    assert "Previous attempt was rejected" in rendered
    assert "bad json" in rendered


def test_render_user_prompt_renders_patterns_block_for_empty_list() -> None:
    rendered = render_user_prompt(
        current_loss_summary="",
        patterns=[],
        mutations=_MUTATIONS,
    )
    assert "no patterns" in rendered.lower()


def test_render_user_prompt_mentions_mutation_metadata() -> None:
    mp = _mp("router__sp", metadata={"role": "system_prompt", "language": "text"})
    rendered = render_user_prompt(
        current_loss_summary="",
        patterns=[],
        mutations=[mp],
    )
    assert "role=system_prompt" in rendered
    assert "language=text" in rendered
