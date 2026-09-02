"""The per-round context the two host tools resolve against.

Both tools an episode is served — :func:`zicato.proposer.tools.mutation_usage`
and :func:`zicato.proposer.validate.validate_patches` — answer about *this*
round's snapshot and manifest, and neither can take that as an argument (see
below). The context they read lives here, in a module of its own, so
:mod:`zicato.proposer.validate` can import it without importing the tool
bodies. The validator must be provably unable to reach the board; a contract
in ``pyproject.toml`` and a runtime closure pin in
``tests/test_proposer_validate.py`` hold it to that. Importing a sibling for
the context alone would put that sibling's whole closure inside the property
being proved.

Why a context var
-----------------
A tool function cannot carry the per-round runtime context (which
generation snapshot to read, the mutation manifest) as a bound argument,
because the implementations are module-level and reused across every
challenger. They therefore read their context from a module-level
:class:`contextvars.ContextVar`, which the proposal episode's host sets
immediately around each tool call via
:func:`bind_proposer_tool_context`. A ``ContextVar`` (not a plain global)
is used so concurrent challengers — each running its own episode on its own
asyncio task — never leak context into one another: a child task started
under one ``bind_proposer_tool_context`` block sees that block's value,
and the reset on block exit restores the prior value.
"""

from __future__ import annotations

import contextvars
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from zicato.core.types import MutationPoint


@dataclass(frozen=True)
class ProposerToolContext:
    """The per-round runtime context a host tool reads.

    Set immediately around each tool call by
    :func:`bind_proposer_tool_context` and read through the module-level
    context var. Frozen so a bound context is a stable, re-readable value
    across the episode's tool calls within one round.

    Fields
    ------
    workspace_root:
        The ``.zicato/`` workspace root. ``validate_patches`` resolves the
        workspace's declared static checks and its load probe from it.
    generation_root:
        The parent generation's snapshot directory — the tree
        ``mutation_usage`` searches and the tree ``validate_patches``
        re-enumerates a draft against.
    epoch_id:
        The epoch the round belongs to — one half of the round coordinates
        the context is stamped with. No tool resolves anything from it.
    mutations:
        The resolved mutation manifest for this round — the
        :class:`MutationPoint` tuple the episode may address a patch to.
    generation_id:
        The id of the generation ``generation_root`` snapshots — the round's
        champion (the proposer's PARENT generation). Carried alongside
        ``epoch_id`` for the same reason.
    """

    workspace_root: Path
    generation_root: Path
    epoch_id: str
    mutations: tuple[MutationPoint, ...]
    generation_id: str = ""


_TOOL_CONTEXT: contextvars.ContextVar[ProposerToolContext | None] = contextvars.ContextVar(
    "zicato_proposer_tool_context",
    default=None,
)


@contextmanager
def bind_proposer_tool_context(ctx: ProposerToolContext) -> Iterator[None]:
    """Bind ``ctx`` as the active proposer-tool context for the block.

    Sets the module-level context var on entry and RESETS it to its prior
    value on exit (even on exception), so a concurrent challenger running
    its own agent under its own ``bind_proposer_tool_context`` never sees
    this block's context, and nothing leaks past the block. Used by
    the proposal episode to wrap each host-tool call of the
    agent.
    """
    token = _TOOL_CONTEXT.set(ctx)
    try:
        yield
    finally:
        _TOOL_CONTEXT.reset(token)


def _active_context() -> ProposerToolContext:
    """Return the bound context or raise a clear out-of-context error.

    A tool called outside a :func:`bind_proposer_tool_context` block has
    no runtime context to read; raising here (rather than returning a
    misleading empty result) makes the misuse obvious — an episode's host
    tools bind the context around every call they answer.
    """
    ctx = _TOOL_CONTEXT.get()
    if ctx is None:
        raise RuntimeError(
            "proposer tool called with no bound ProposerToolContext; "
            "proposer tools may only be called from within "
            "a bound proposer tool context (see bind_proposer_tool_context)"
        )
    return ctx


__all__ = [
    "ProposerToolContext",
    "bind_proposer_tool_context",
]
