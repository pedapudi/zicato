"""Vendored presentation-agent reference, annotated for zicato.

This package is a self-contained copy of an upstream multi-agent
presentation tree (originally
``presentation_agent_orchestrated`` in the harmonograf reference
agents, which in turn shares its tree with the goldfive presentation
example). It is vendored here so that zicato can enumerate mutation
points and run tournaments against a stable, side-effect-free target.

Public surface:

* :data:`root_agent` — the coordinator agent at the root of the tree.
  Lazy: built on first attribute access so that importing the package
  for static introspection (mutation-marker walks, board validation)
  does NOT require ``google-adk`` to be installed.
* :func:`build_agent_tree` — re-builds the tree against a custom
  ``model`` argument (string LiteLLM identifier or a ``BaseLlm``
  instance). Useful when zicato wants to inject a mock model so the
  tree can be exercised offline.

Mutation surface:

The string literals that the proposer is allowed to rewrite are
annotated in :mod:`.agent` with ``# zicato:mutable id="..."`` markers.
See :mod:`.agent` for the list of ids and ``examples/target_1_presentation/README.md``
for the operator-facing overview.

This module deliberately does NOT import any harmonograf or goldfive
client code — the vendored copy is meant to load cleanly under
zicato's adapter without dragging in optional telemetry dependencies.
"""

from __future__ import annotations

from typing import Any

from .agent import build_agent_tree


def __getattr__(name: str) -> Any:
    """PEP 562 lazy attribute — forward ``root_agent`` to the submodule.

    Resolves ``root_agent`` lazily so that ``import
    zicato_examples.target_1_presentation.agent`` is safe even when
    ``google-adk`` is absent (e.g. static-tooling test runs). The
    actual build happens inside :mod:`.agent` on first access.
    """
    if name == "root_agent":
        from . import agent as _agent_mod

        return _agent_mod.root_agent
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["build_agent_tree", "root_agent"]
