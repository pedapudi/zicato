"""Deterministic backend for zicato's tournament builder (B1a).

This package is the config + draft-state + operations + REST API that
both the builder form (B2) and the copilot (B1b) consume. It has NO LLM
dependency and NO frontend — it is the single source of truth for editing
an evaluation contract as a draft and applying it (which lets the existing
auto-epoch machinery roll the epoch).

Public surface:

* :class:`~zicato.builder.config.BuilderConfig` /
  :func:`~zicato.builder.config.load_builder_config` — ``builder.json``.
* :class:`~zicato.builder.draft.TournamentDraft` /
  :class:`~zicato.builder.draft.DraftStore` — the editable draft state.
* the operations in :mod:`zicato.builder.operations` — the one place each
  edit's semantics live (form + copilot share them).
* :func:`~zicato.builder.api.builder_routes` — the REST surface, wired
  into the dashboard server.
"""

from __future__ import annotations

from zicato.builder.config import BuilderConfig, load_builder_config
from zicato.builder.draft import (
    ContractDiff,
    DraftStore,
    TournamentDraft,
)

__all__ = [
    "BuilderConfig",
    "load_builder_config",
    "ContractDiff",
    "DraftStore",
    "TournamentDraft",
]
