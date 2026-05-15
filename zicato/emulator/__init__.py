"""zicato.emulator — collusion-proof multi-turn user emulator.

The emulator runs a ``multi_turn_emulated`` board entry as a simulated
conversation between a persona-driven user (the emulator) and the inner
agent (driven by the caller-supplied harness turn-runner). Collusion is
prevented by construction:

* Context-construction is sealed (explicit signatures, no ``**kwargs``).
* The two LLM callables on :class:`zicato.core.types.RuntimeConfig` are
  required to differ by identity at drive time.
* The system prompt bakes in a non-leakage rule paragraph verbatim.
* A post-hoc answer-leak heuristic aborts the run on suspicious output.
* Each turn emits a ``zicato:emulator`` audit span (best-effort).

Downstream callers should import from this package surface::

    from zicato.emulator import EmulatedMultiTurnDriver, EmulationCollusionError
"""

from __future__ import annotations

from typing import Any

from zicato.core.types import BoardEntry, RunResult, RuntimeConfig
from zicato.emulator.answer_leak import LEAK_PATTERNS, check_answer_leak
from zicato.emulator.audit import EmulatorTurnAudit, audit_turn, emit_audit_span
from zicato.emulator.emulator import (
    EmulatedMultiTurnDriver,
    EmulationCollusionError,
)
from zicato.emulator.sealed import (
    END_TOKEN,
    NON_LEAKAGE_PARAGRAPH,
    build_emulator_system_prompt,
    build_emulator_user_prompt,
)


async def run_emulated(
    agent: Any,
    entry: BoardEntry,
    sinks: list[Any],
    config: RuntimeConfig,
    run_id: str,
) -> RunResult:
    """Free-function entrypoint over :class:`EmulatedMultiTurnDriver`.

    Bridges harness-adapter callers (who hold an ``agent`` plus
    ``sinks``) to the driver (which wants a per-turn callable). The
    bridge expects ``agent`` to expose either ``run(user_msg)`` or
    ``__call__(user_msg)`` returning the agent's user-facing reply.
    Adapters that do something richer should call the driver directly
    rather than going through this wrapper.
    """
    del run_id, sinks  # accepted for API parity; not threaded yet
    driver = EmulatedMultiTurnDriver()

    async def _run_harness_turn(user_msg: str) -> str:
        method = getattr(agent, "run", None)
        if method is None and callable(agent):
            method = agent
        if method is None:
            raise TypeError(
                "run_emulated agent must expose run() or be callable; "
                f"got {type(agent).__name__}"
            )
        reply = method(user_msg)
        # Best-effort: ``method`` may return a coroutine or a plain value.
        if hasattr(reply, "__await__"):
            reply = await reply
        return str(reply)

    return await driver.drive(
        run_harness_turn=_run_harness_turn,
        entry=entry,
        config=config,
    )


__all__ = [
    "EmulatedMultiTurnDriver",
    "EmulationCollusionError",
    "EmulatorTurnAudit",
    "LEAK_PATTERNS",
    "NON_LEAKAGE_PARAGRAPH",
    "END_TOKEN",
    "audit_turn",
    "build_emulator_system_prompt",
    "build_emulator_user_prompt",
    "check_answer_leak",
    "emit_audit_span",
    "run_emulated",
]
