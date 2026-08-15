import asyncio

import pytest

from zicato.import_path import import_dotted_path
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

CAPABILITIES = ReasoningCapabilities(True, True)


@reasoning_aware_call_llm(capabilities=CAPABILITIES)
async def worker_reasoning_call(request: ModelRequest) -> ModelResponse:
    return ModelResponse(f"{request.system}:{request.user}:{request.model}")


class ScriptedBackend:
    def __init__(self, *responses: ModelResponse) -> None:
        self.responses = list(responses)
        self.requests: list[ModelRequest] = []

    async def __call__(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        return self.responses.pop(0)


def adapt(backend: ScriptedBackend, **kwargs: object):
    return reasoning_aware_call_llm(capabilities=CAPABILITIES, **kwargs)(backend)  # type: ignore[arg-type]


async def test_returns_only_content_and_preserves_callable_identity_guard() -> None:
    backend = ScriptedBackend(ModelResponse("answer", reasoning="private"))
    first, second = adapt(backend), adapt(backend)
    assert await first("system", "user", "model") == "answer"
    assert first is not second
    assert backend.requests == [ModelRequest("system", "user", "model", True, 32_768)]


async def test_exhaustion_falls_back_once_and_accounts_for_both_calls() -> None:
    attempts: list[CallAttempt] = []
    backend = ScriptedBackend(
        ModelResponse("", "private", "length", "exhausted", 10, 20),
        ModelResponse("answer", "must not escape", input_tokens=11, output_tokens=3),
    )
    config = ReasoningCallConfig(12_000, 900)
    assert (
        await adapt(backend, config=config, observe_attempt=attempts.append)("s", "u", "m")
        == "answer"
    )
    assert backend.requests == [
        ModelRequest("s", "u", "m", True, 12_000),
        ModelRequest("s", "u", "m", False, 900),
    ]
    assert [(a.reasoning_enabled, a.input_tokens, a.output_tokens) for a in attempts] == [
        (True, 10, 20),
        (False, 11, 3),
    ]
    assert all(not hasattr(a, "reasoning") for a in attempts)


async def test_empty_terminal_paths_are_bounded_and_never_leak_reasoning() -> None:
    cases = [
        ((ModelResponse("", "secret"),), "completed without answer", 1),
        (
            (
                ModelResponse("", "secret", "length", "exhausted"),
                ModelResponse("", "secret", "length"),
            ),
            "after fallback",
            2,
        ),
    ]
    for responses, message, calls in cases:
        backend = ScriptedBackend(*responses)
        with pytest.raises(EmptyModelContent, match=message) as caught:
            await adapt(backend)("s", "u", "m")
        assert "secret" not in str(caught.value)
        assert len(backend.requests) == calls


def test_invalid_budgets_and_capabilities_are_rejected() -> None:
    actions = [
        lambda: ReasoningCallConfig(0, 1),
        lambda: ReasoningCallConfig(1, 0),
        lambda: reasoning_aware_call_llm(capabilities=ReasoningCapabilities(False, True)),
        lambda: reasoning_aware_call_llm(capabilities=ReasoningCapabilities(True, False)),
    ]
    for action in actions:
        with pytest.raises(ValueError):
            action()


async def test_cancellation_propagates_without_fallback() -> None:
    started = asyncio.Event()

    async def backend(_: ModelRequest) -> ModelResponse:
        started.set()
        await asyncio.Future()
        raise AssertionError("unreachable")

    task = asyncio.create_task(
        reasoning_aware_call_llm(capabilities=CAPABILITIES)(backend)("s", "u", "m")
    )
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_concurrent_calls_are_isolated() -> None:
    async def backend(request: ModelRequest) -> ModelResponse:
        await asyncio.sleep(0)
        return ModelResponse(
            request.user if not request.reasoning_enabled else "", answer_status="exhausted"
        )

    call = reasoning_aware_call_llm(capabilities=CAPABILITIES)(backend)
    assert await asyncio.gather(call("s", "one", "m"), call("s", "two", "m")) == ["one", "two"]


async def test_decorator_reconstructs_across_worker_import_boundary() -> None:
    dotted = _callable_dotted_path(worker_reasoning_call)
    assert dotted == "tests.test_reasoning:worker_reasoning_call"
    rebuilt = import_dotted_path(dotted, label="reasoning callable")
    assert await rebuilt("system", "user", "model") == "system:user:model"
