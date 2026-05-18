"""Shared per-call budget for auxiliary-LLM calls.

The auxiliary LLM is the proposer/judge/emulator/analysis backend — a
hung endpoint there can wedge a round. Each call site wraps its
``aux_call_llm`` invocation in :func:`asyncio.wait_for` against the
budget exposed by :func:`aux_call_timeout_s`.

The budget is the ``AuxConfig.call_timeout_s`` field of the typed
configuration tree (see :mod:`zicato.config`). It is sourced from the
``ZICATO_AUX_CALL_TIMEOUT`` environment variable when
:func:`zicato.config.load_config` reads the environment, but this module
no longer reads ``os.environ`` itself — it takes a config object.

A caller that has already loaded a :class:`~zicato.config.ZicatoConfig`
threads its ``aux`` sub-config in. A caller that has not passes nothing,
and :func:`aux_call_timeout_s` loads the config itself — which keeps the
env var honoured for the call sites not yet threaded through a config
object.
"""

from __future__ import annotations

from zicato.config import AuxConfig, load_config

#: Default per-call auxiliary-LLM budget in seconds. Mirrors
#: :attr:`zicato.config.AuxConfig.call_timeout_s`'s default; kept as a
#: module constant for the call sites and tests that import it by name.
DEFAULT_AUX_CALL_TIMEOUT_S: float = AuxConfig().call_timeout_s


def aux_call_timeout_s(config: AuxConfig | None = None) -> float:
    """Return the per-call auxiliary-LLM budget in seconds.

    Parameters
    ----------
    config:
        The :class:`~zicato.config.AuxConfig` to read the budget from.
        When ``None`` (the common call-site form, kept for call sites
        not yet threaded through a config object) the configuration is
        loaded via :func:`zicato.config.load_config`, which reads the
        ``ZICATO_AUX_CALL_TIMEOUT`` environment variable and clamps an
        invalid or non-positive value back to the default.

    Returns
    -------
    float
        A strictly-positive number of seconds.
    """
    if config is None:
        config = load_config().aux
    return config.call_timeout_s


__all__ = ["DEFAULT_AUX_CALL_TIMEOUT_S", "aux_call_timeout_s"]
