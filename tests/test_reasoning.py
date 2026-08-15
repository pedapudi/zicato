from __future__ import annotations

import asyncio

import pytest

from zicato.reasoning import (
    CallAttempt,
    EmptyModelContent,
    ModelRequest,
    ModelResponse,
    ReasoningCallConfig,
    ReasoningCapabilities,
    reasoning_aware_call_llm,
)
from zicato.tournament.worker_transport import _callable_dotted_path

CAPABILITIES = ReasoningCapabilities(separate_channels=True, reasoning_control=True)


class ScriptedBackend:
    def __init__(self, responses: list[ModelResponse]) -> None:
        self.responses = responses
        self.requests: list[ModelRequest] = []

    async def __call__(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        return self.responses.pop(0)


async def test_returns_content_without_exposing_reasoning() -> None:
    backend = ScriptedBackend([ModelResponse(content="answer", reasoning="private")])

    result = await reasoning_aware_call_llm(backend, capabilities=CAPABILITIES)(
        "system", "user", "model"
    )

    assert result == "answer"
    assert len(backend.requests) == 1
    assert backend.requests[0].reasoning_enabled is True


async def test_empty_content_retries_with_reasoning_disabled() -> None:
    backend = ScriptedBackend(
        [
            ModelResponse(
                content="",
                reasoning="unfinished",
                finish_reason="length",
                answer_status="exhausted",
            ),
            ModelResponse(content='{"ok": true}', reasoning="must not escape"),
        ]
    )
    config = ReasoningCallConfig(thinking_tokens=12_000, answer_tokens=900)

    result = await reasoning_aware_call_llm(backend, capabilities=CAPABILITIES, config=config)(
        "sys", "prompt", "m"
    )

    assert result == '{"ok": true}'
    assert backend.requests == [
        ModelRequest("sys", "prompt", "m", reasoning_enabled=True, max_output_tokens=12_000),
        ModelRequest("sys", "prompt", "m", reasoning_enabled=False, max_output_tokens=900),
    ]


async def test_empty_fallback_raises_without_leaking_reasoning() -> None:
    secret = "private scratchpad sentinel"
    backend = ScriptedBackend(
        [
            ModelResponse(
                content=" ",
                reasoning=secret,
                finish_reason="length",
                answer_status="exhausted",
            ),
            ModelResponse(content="", reasoning=secret, finish_reason="length"),
        ]
    )

    try:
        await reasoning_aware_call_llm(backend, capabilities=CAPABILITIES)("sys", "prompt", "m")
    except EmptyModelContent as exc:
        assert secret not in str(exc)
        assert "length" in str(exc)
    else:
        raise AssertionError("empty answer-only fallback must fail")


def test_budgets_must_be_positive() -> None:
    for kwargs in ({"thinking_tokens": 0}, {"answer_tokens": 0}):
        try:
            ReasoningCallConfig(**kwargs)
        except ValueError:
            pass
        else:
            raise AssertionError(f"invalid budget accepted: {kwargs}")


def test_each_adapter_has_distinct_callable_identity() -> None:
    backend = ScriptedBackend([])

    assert reasoning_aware_call_llm(
        backend, capabilities=CAPABILITIES
    ) is not reasoning_aware_call_llm(backend, capabilities=CAPABILITIES)


def test_rejects_backends_without_required_capabilities() -> None:
    backend = ScriptedBackend([])
    for capabilities in (
        ReasoningCapabilities(separate_channels=False, reasoning_control=True),
        ReasoningCapabilities(separate_channels=True, reasoning_control=False),
    ):
        with pytest.raises(ValueError):
            reasoning_aware_call_llm(backend, capabilities=capabilities)


async def test_complete_empty_content_does_not_trigger_fallback() -> None:
    backend = ScriptedBackend([ModelResponse(content="", reasoning="private")])

    with pytest.raises(EmptyModelContent, match="completed without answer"):
        await reasoning_aware_call_llm(backend, capabilities=CAPABILITIES)("s", "u", "m")

    assert len(backend.requests) == 1


async def test_observer_accounts_for_both_attempts_without_reasoning() -> None:
    backend = ScriptedBackend(
        [
            ModelResponse(
                content="",
                reasoning="private",
                answer_status="exhausted",
                input_tokens=10,
                output_tokens=20,
            ),
            ModelResponse(content="answer", input_tokens=11, output_tokens=3),
        ]
    )
    attempts: list[CallAttempt] = []

    await reasoning_aware_call_llm(
        backend, capabilities=CAPABILITIES, observe_attempt=attempts.append
    )("s", "u", "m")

    assert [
        (item.reasoning_enabled, item.input_tokens, item.output_tokens) for item in attempts
    ] == [
        (True, 10, 20),
        (False, 11, 3),
    ]
    assert all(not hasattr(item, "reasoning") for item in attempts)


async def test_cancellation_propagates_without_fallback() -> None:
    started = asyncio.Event()

    async def backend(request: ModelRequest) -> ModelResponse:
        started.set()
        await asyncio.Future()
        raise AssertionError("unreachable")

    call = reasoning_aware_call_llm(backend, capabilities=CAPABILITIES)
    task = asyncio.create_task(call("s", "u", "m"))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_shared_adapter_keeps_concurrent_calls_isolated() -> None:
    async def backend(request: ModelRequest) -> ModelResponse:
        await asyncio.sleep(0)
        if request.reasoning_enabled:
            return ModelResponse(content="", answer_status="exhausted")
        return ModelResponse(content=request.user)

    call = reasoning_aware_call_llm(backend, capabilities=CAPABILITIES)

    assert await asyncio.gather(call("s", "one", "m"), call("s", "two", "m")) == [
        "one",
        "two",
    ]


async def test_decorated_callable_reconstructs_across_worker_import_boundary() -> None:
    from tests._reasoning_worker_support import worker_reasoning_call
    from zicato.import_path import import_dotted_path

    dotted = _callable_dotted_path(worker_reasoning_call)

    assert dotted == "tests._reasoning_worker_support:worker_reasoning_call"
    rebuilt = import_dotted_path(dotted, label="test reasoning callable")
    assert await rebuilt("system", "user", "model") == "system:user:model"
