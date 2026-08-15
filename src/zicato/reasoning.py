"""Reasoning-aware adapter for zicato's answer-only ``CallLLM`` seam."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import wraps
from typing import Literal

from zicato.core.runtime import CallLLM


@dataclass(frozen=True, slots=True)
class ModelRequest:
    system: str
    user: str
    model: str
    reasoning_enabled: bool
    max_output_tokens: int


@dataclass(frozen=True, slots=True)
class ModelResponse:
    content: str
    reasoning: str = ""
    finish_reason: str | None = None
    answer_status: Literal["complete", "exhausted"] = "complete"
    input_tokens: int = 0
    output_tokens: int = 0


StructuredCall = Callable[[ModelRequest], Awaitable[ModelResponse]]


@dataclass(frozen=True, slots=True)
class ReasoningCapabilities:
    separate_channels: bool
    reasoning_control: bool


@dataclass(frozen=True, slots=True)
class CallAttempt:
    reasoning_enabled: bool
    answer_status: Literal["complete", "exhausted"]
    finish_reason: str | None
    input_tokens: int
    output_tokens: int


AttemptObserver = Callable[[CallAttempt], None]


@dataclass(frozen=True, slots=True)
class ReasoningCallConfig:
    thinking_tokens: int = 32_768
    answer_tokens: int = 4_096

    def __post_init__(self) -> None:
        if self.thinking_tokens < 1 or self.answer_tokens < 1:
            raise ValueError("reasoning call budgets must be >= 1")


class EmptyModelContent(RuntimeError):
    """No answer content was produced."""


def reasoning_aware_call_llm(
    *,
    capabilities: ReasoningCapabilities,
    config: ReasoningCallConfig | None = None,
    observe_attempt: AttemptObserver | None = None,
) -> Callable[[StructuredCall], CallLLM]:
    """Decorate a two-channel backend as an answer-only, worker-safe call."""
    if not capabilities.separate_channels or not capabilities.reasoning_control:
        raise ValueError("reasoning-aware calls require separate channels and reasoning control")
    budgets = config or ReasoningCallConfig()

    def decorate(backend: StructuredCall) -> CallLLM:
        def observe(response: ModelResponse, enabled: bool) -> None:
            if observe_attempt:
                observe_attempt(
                    CallAttempt(
                        enabled,
                        response.answer_status,
                        response.finish_reason,
                        max(0, response.input_tokens),
                        max(0, response.output_tokens),
                    )
                )

        @wraps(backend)
        async def call(system: str, user: str, model: str) -> str:
            request = ModelRequest(system, user, model, True, budgets.thinking_tokens)
            first = await backend(request)
            observe(first, True)
            if first.content.strip():
                return first.content
            if first.answer_status != "exhausted":
                raise EmptyModelContent("model completed without answer content")
            fallback = await backend(
                ModelRequest(system, user, model, False, budgets.answer_tokens)
            )
            observe(fallback, False)
            if fallback.content.strip():
                return fallback.content
            reason = fallback.finish_reason or first.finish_reason
            detail = f" (finish reason: {reason})" if reason else ""
            raise EmptyModelContent(f"model returned no answer content after fallback{detail}")

        return call

    return decorate
