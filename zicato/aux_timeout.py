"""Shared per-call budget for auxiliary-LLM calls.

The auxiliary LLM is the proposer/judge/emulator/analysis backend — a
hung endpoint there can wedge a round. Each call site wraps its
``aux_call_llm`` invocation in :func:`asyncio.wait_for` against the
budget exposed by :func:`aux_call_timeout_s`.

The budget is read from the ``ZICATO_AUX_CALL_TIMEOUT`` environment
variable (a float number of seconds). When unset or unparseable the
default :data:`DEFAULT_AUX_CALL_TIMEOUT_S` (120 seconds) applies.

Resolution happens at every call (no caching) so operators can change
the env var between runs without restarting any long-lived orchestrator
process.
"""

from __future__ import annotations

import os

DEFAULT_AUX_CALL_TIMEOUT_S: float = 120.0


def aux_call_timeout_s() -> float:
    """Return the current per-call auxiliary-LLM budget in seconds.

    Reads ``ZICATO_AUX_CALL_TIMEOUT`` and falls back to
    :data:`DEFAULT_AUX_CALL_TIMEOUT_S` on missing or invalid values.
    A non-positive value is treated as invalid (the default applies)
    so an operator cannot accidentally configure a 0-second timeout
    that would short-circuit every call.
    """
    raw = os.environ.get("ZICATO_AUX_CALL_TIMEOUT")
    if raw is None:
        return DEFAULT_AUX_CALL_TIMEOUT_S
    try:
        parsed = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_AUX_CALL_TIMEOUT_S
    if parsed <= 0:
        return DEFAULT_AUX_CALL_TIMEOUT_S
    return parsed


__all__ = ["DEFAULT_AUX_CALL_TIMEOUT_S", "aux_call_timeout_s"]
