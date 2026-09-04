"""Deterministic backend for zicato's tournament builder (B1a).

This package is the config + REST API that both the builder form (B2) and
the copilot (B1b) consume. It has NO LLM dependency and NO frontend — it
serves one editable contract draft over HTTP and applies it on the
operator's confirmation (which lets the existing auto-epoch machinery roll
the epoch).

The draft itself and the operations that edit it are library code and live
in :mod:`zicato.contract_draft`; this package holds no second edit path.

Public surface:

* :class:`~zicato.builder.config.BuilderConfig` /
  :func:`~zicato.builder.config.load_builder_config` — ``builder.json``.
* :func:`~zicato.builder.api.builder_routes` — the REST surface, wired
  into the dashboard server.
"""

from __future__ import annotations

from zicato.builder.config import BuilderConfig, load_builder_config

__all__ = [
    "BuilderConfig",
    "load_builder_config",
]
