"""The explicit per-call budget for evaluation calls."""

from __future__ import annotations

from zicato.core.settings import AuxConfig

DEFAULT_AUX_CALL_TIMEOUT_S: float = AuxConfig().call_timeout_s


def aux_call_timeout_s(config: AuxConfig | None = None) -> float:
    """Read the selected evaluation budget, or the declaration's default."""
    return (config or AuxConfig()).call_timeout_s


__all__ = ["DEFAULT_AUX_CALL_TIMEOUT_S", "aux_call_timeout_s"]
