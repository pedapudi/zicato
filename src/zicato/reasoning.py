"""Reasoning-aware adapter for the text-only :class:`~zicato.CallLLM` seam.

Backends opt into this adapter by accepting a structured :class:`ModelRequest`
and returning a :class:`ModelResponse`.  Existing zicato consumers still see
the narrow ``(system, user, model) -> str`` callable.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import wraps
from typing import Literal, overload

from zicato.core.runtime import CallLLM


@dataclass(frozen=True, slots=True)
class ModelRequest:
    """One backend request with explicit reasoning and output controls."""

    system: str
    user: str
    model: str
    reasoning_enabled: bool
    max_output_tokens: int


@dataclass(frozen=True, slots=True)
class ModelResponse:
    """A backend response whose private reasoning and answer stay distinct."""

    content: str
    reasoning: str = ""
    finish_reason: str | None = None
    answer_status: Literal["complete", "exhausted"] = "complete"
    input_tokens: int = 0
    output_tokens: int = 0


StructuredCall = Callable[[ModelRequest], Awaitable[ModelResponse]]


@dataclass(frozen=True, slots=True)
class ReasoningCapabilities:
    """Backend guarantees required for a safe reasoning-aware adaptation."""

    separate_channels: bool
    reasoning_control: bool


@dataclass(frozen=True, slots=True)
class CallAttempt:
    """Scratchpad-free accounting for one backend attempt."""

    reasoning_enabled: bool
    answer_status: Literal["complete", "exhausted"]
    finish_reason: str | None
    input_tokens: int
    output_tokens: int


AttemptObserver = Callable[[CallAttempt], None]


@dataclass(frozen=True, slots=True)
class ReasoningCallConfig:
    """Budgets for the reasoning attempt and reasoning-disabled fallback."""

    thinking_tokens: int = 32_768
    answer_tokens: int = 4_096

    def __post_init__(self) -> None:
        if self.thinking_tokens < 1:
            raise ValueError("thinking_tokens must be >= 1")
        if self.answer_tokens < 1:
            raise ValueError("answer_tokens must be >= 1")


class EmptyModelContent(RuntimeError):
    """Both the reasoning attempt and its answer-only fallback were empty."""


@overload
def reasoning_aware_call_llm(
    backend: None = None,
    *,
    capabilities: ReasoningCapabilities,
    config: ReasoningCallConfig | None = None,
    observe_attempt: AttemptObserver | None = None,
) -> Callable[[StructuredCall], CallLLM]: ...


@overload
def reasoning_aware_call_llm(
    backend: StructuredCall,
    *,
    capabilities: ReasoningCapabilities,
    config: ReasoningCallConfig | None = None,
    observe_attempt: AttemptObserver | None = None,
) -> CallLLM: ...


def reasoning_aware_call_llm(
    backend: StructuredCall | None = None,
    *,
    capabilities: ReasoningCapabilities,
    config: ReasoningCallConfig | None = None,
    observe_attempt: AttemptObserver | None = None,
) -> CallLLM | Callable[[StructuredCall], CallLLM]:
    """Adapt a two-channel backend to zicato's answer-only text seam.

    The first request permits reasoning.  If it yields no answer content, the
    exact request is repeated with reasoning disabled and the smaller answer
    budget.  Private reasoning is never returned, interpolated into the
    fallback, or included in an error.
    """

    if backend is None:

        def decorate(fn: StructuredCall) -> CallLLM:
            return reasoning_aware_call_llm(
                fn,
                capabilities=capabilities,
                config=config,
                observe_attempt=observe_attempt,
            )

        return decorate
    if not capabilities.separate_channels:
        raise ValueError("reasoning-aware calls require separate reasoning and content channels")
    if not capabilities.reasoning_control:
        raise ValueError("reasoning-aware calls require backend-level reasoning control")
    budgets = config or ReasoningCallConfig()

    def observe(response: ModelResponse, *, reasoning_enabled: bool) -> None:
        if observe_attempt is not None:
            observe_attempt(
                CallAttempt(
                    reasoning_enabled=reasoning_enabled,
                    answer_status=response.answer_status,
                    finish_reason=response.finish_reason,
                    input_tokens=max(0, response.input_tokens),
                    output_tokens=max(0, response.output_tokens),
                )
            )

    @wraps(backend)
    async def call(system: str, user: str, model: str) -> str:
        first = await backend(
            ModelRequest(
                system=system,
                user=user,
                model=model,
                reasoning_enabled=True,
                max_output_tokens=budgets.thinking_tokens,
            )
        )
        observe(first, reasoning_enabled=True)
        if first.content.strip():
            return first.content
        if first.answer_status != "exhausted":
            raise EmptyModelContent("model completed without answer content")

        fallback = await backend(
            ModelRequest(
                system=system,
                user=user,
                model=model,
                reasoning_enabled=False,
                max_output_tokens=budgets.answer_tokens,
            )
        )
        observe(fallback, reasoning_enabled=False)
        if fallback.content.strip():
            return fallback.content
        reason = fallback.finish_reason or first.finish_reason
        suffix = f" (finish reason: {reason})" if reason else ""
        raise EmptyModelContent(f"model returned no answer content after fallback{suffix}")

    return call


__all__ = [
    "AttemptObserver",
    "EmptyModelContent",
    "CallAttempt",
    "ModelRequest",
    "ModelResponse",
    "ReasoningCapabilities",
    "ReasoningCallConfig",
    "StructuredCall",
    "reasoning_aware_call_llm",
]
