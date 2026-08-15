"""Importable reasoning-aware callable used by worker-boundary tests."""

from zicato.reasoning import (
    ModelRequest,
    ModelResponse,
    ReasoningCapabilities,
    reasoning_aware_call_llm,
)


@reasoning_aware_call_llm(
    capabilities=ReasoningCapabilities(separate_channels=True, reasoning_control=True)
)
async def worker_reasoning_call(request: ModelRequest) -> ModelResponse:
    return ModelResponse(content=f"{request.system}:{request.user}:{request.model}")
