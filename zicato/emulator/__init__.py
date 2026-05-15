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
]
