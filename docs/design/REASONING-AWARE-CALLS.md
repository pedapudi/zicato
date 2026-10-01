# Reasoning-aware model calls

## Purpose

`CallLLM` has one small signature: `(system, user, model) -> answer text`.
Some backends produce private reasoning separately from the answer.
If reasoning consumes the output budget, the answer channel can be empty. A
plain text adapter cannot distinguish that condition from a legitimate empty
answer and must not substitute the private reasoning.

`zicato.reasoning` is an opt-in boundary for backends that expose both channels
and can disable reasoning for a bounded fallback. It does not attempt to infer
channels from flattened text and does not change existing `CallLLM` call sites.

## Contract

The backend accepts a `ModelRequest` and returns a `ModelResponse`:

- `reasoning_enabled` is a backend-level control rather than an instruction
  added to the prompt.
- `max_output_tokens` is the attempt's output ceiling.
- `content` is the user-visible answer.
- `reasoning` is private scratch space. The adapter never returns, logs,
  persists, interpolates, or includes it in an exception.
- `answer_status="exhausted"` reports that the answer was not reached before
  the budget ended. Empty `content` alone does not authorize a retry.
- token counts and finish reason may be reported for scratchpad-free accounting.

The adapter requires explicit `ReasoningCapabilities`. It refuses a backend
that cannot guarantee separate channels or true backend-level reasoning
control. A text-only `CallLLM` therefore cannot be made reasoning-aware after
the fact.

## Call sequence

1. Call once with reasoning enabled and `thinking_tokens` as the ceiling.
2. Return `content` unchanged when it holds any non-whitespace text.
3. If `content` is empty and the backend reported
   `answer_status="complete"`, raise `EmptyModelContent` without a second
   call.
4. If and only if the backend reports `answer_status="exhausted"`, call once
   more with reasoning disabled and the smaller `answer_tokens` ceiling.
5. Return fallback content, or raise `EmptyModelContent` (naming the finish
   reason when one was reported) if it is still empty.

There is exactly one fallback. Cancellation, timeouts, and backend exceptions
propagate normally. The adapter owns no mutable per-call state, so concurrent
use is safe.

## Backend example

`ReasoningCallConfig` defaults to `thinking_tokens=32_768` and
`answer_tokens=4_096`; both must be at least 1. Use decorator form for a
callable that crosses tournament worker boundaries.
The decorated module-level name remains importable in a fresh worker process:

```python
from zicato.reasoning import (
    ModelRequest,
    ModelResponse,
    ReasoningCapabilities,
    ReasoningCallConfig,
    reasoning_aware_call_llm,
)


@reasoning_aware_call_llm(
    capabilities=ReasoningCapabilities(
        separate_channels=True,
        reasoning_control=True,
    ),
    config=ReasoningCallConfig(
        thinking_tokens=32_768,
        answer_tokens=4_096,
    ),
)
async def call_llm(request: ModelRequest) -> ModelResponse:
    raw = await backend_request(
        system=request.system,
        user=request.user,
        model=request.model,
        reasoning=request.reasoning_enabled,
        max_output_tokens=request.max_output_tokens,
    )
    return ModelResponse(
        content=raw.answer,
        reasoning=raw.private_reasoning,
        answer_status="exhausted" if raw.answer_budget_exhausted else "complete",
        finish_reason=raw.finish_reason,
        input_tokens=raw.input_tokens,
        output_tokens=raw.output_tokens,
    )
```

The example's `backend_request` is owned by the application, because transport
field names differ between backends. zicato's structured boundary stays stable
and model-agnostic.

## Accounting hook

Pass `observe_attempt=` to receive one `CallAttempt` after every completed
backend request. It contains mode, terminal status, finish reason, and token
counts but no prompts, answer, or reasoning. A fallback therefore produces two
accounting records. The observer is synchronous and should remain cheap. An
observer exception propagates rather than being swallowed, so that cost data is
never lost silently.

## Scope

This adapter covers text consumers such as judges, emulators, analysis
passes, and the best-of-N critique and merge calls when their configured
callable opts in. No zicato call site wraps a callable itself; the operator
applies the decorator to their own backend. The proposal episode's model
calls are made by Foe's own model client ([PROPOSER.md](PROPOSER.md) §2.9)
and do not pass through `CallLLM`. Native agent runtimes
that execute tool calls through their own model objects need equivalent controls
at that native boundary; flattening them through `CallLLM` would discard tool
capabilities and is not a supported shortcut.
