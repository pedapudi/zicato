"""The per-round context the proposer tools resolve against.

Split out of :mod:`zicato.proposer.tools` so the context plumbing can be
imported WITHOUT importing the tool bodies. Two reasons, both structural:

* :mod:`zicato.proposer.validate` needs the bound context but must be
  provably unable to reach the board. The read tools pull in the analyzer
  (for ``read_insights``), which pulls in the board loader, so importing
  ``tools`` for the context alone would have put ``zicato.board`` in the
  validator's import closure and made the "no path to board data" contract
  in ``pyproject.toml`` unsatisfiable for a reason that says nothing about
  what the validator does.
* ``tools`` imports ``validate`` to put ``validate_patches`` in
  :data:`~zicato.proposer.tools.DEFAULT_PROPOSER_TOOLS`. With the context
  here, that dependency is a straight line — ``tools -> validate ->
  tool_context`` — instead of a cycle.

Every public name here is re-exported from :mod:`zicato.proposer.tools`,
which remains the import site the rest of the codebase and every custom
``agent.py`` uses.

Why a context var
-----------------
A tool function cannot carry the per-round runtime context (which
generation snapshot to read, the mutation manifest, the epoch's journal)
as a bound argument, because the custom agent is constructed ONCE at
import time — long before any round runs — and reused across every
challenger. The tools therefore read their context from a module-level
:class:`contextvars.ContextVar`, which :meth:`ADKProposerAgent.propose`
sets immediately around each agent run via
:func:`bind_proposer_tool_context`. A ``ContextVar`` (not a plain global)
is used so concurrent challengers — each running its own agent on its own
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
    """The per-round runtime context a proposer tool reads.

    Set immediately around each agent run by
    :func:`bind_proposer_tool_context` and read by every tool via the
    module-level context var. Frozen so a bound context is a stable,
    re-readable value across the agent's tool calls within one round.

    Fields
    ------
    workspace_root:
        The ``.zicato/`` workspace root — used to resolve the epoch's
        journal / insights paths.
    generation_root:
        The parent generation's snapshot directory. ``read_mutable_file``
        and ``grep_mutable`` resolve relative paths under this root's
        mutable subtrees.
    epoch_id:
        The active epoch's id — the journal / insights lookups key on it.
    mutations:
        The resolved mutation manifest for this round — exactly the
        :class:`MutationPoint` tuple the default proposer is offered.
        ``list_mutation_points`` renders it; the read/grep tools derive
        the mutable subtrees from its ``source_root`` set.
    generation_id:
        The id of the generation ``generation_root`` snapshots — the round's
        champion (the proposer's PARENT generation). Lets ``read_parent_diff``
        resolve the generation-store coordinates of the tree the tools are
        already reading. Empty (the default when no id is supplied) degrades
        that tool to an explicit "coordinates unavailable" answer.
    """

    workspace_root: Path
    generation_root: Path
    epoch_id: str
    mutations: tuple[MutationPoint, ...]
    generation_id: str = ""

    def mutable_roots(self) -> tuple[Path, ...]:
        """Return the mutable surface roots under the snapshot.

        Always includes the whole :attr:`generation_root` (the snapshot
        root) FIRST, then each distinct mutable subtree the manifest's
        :attr:`MutationPoint.source_root` values re-base onto it by
        basename (the same re-basing :meth:`ADKHarnessAdapter.mutable_subpaths`
        does — a snapshot copies each registered tree under its basename).

        Why the snapshot root is always present
        ---------------------------------------
        :func:`list_mutation_points` advertises every mutable file RELATIVE TO
        THE WHOLE SNAPSHOT (``file.relative_to(generation_root)`` — e.g.
        ``agent/prompts.py``). Admitting ONLY the narrower declared subtree as
        a readable root when an adapter declares one (``source_root`` basename
        ``agent`` → root ``<snapshot>/agent``) would make the very path the
        manifest hands the proposer resolve to
        ``<snapshot>/agent/agent/prompts.py``, which is not a file, and raise.
        Only the bare subtree-relative ``prompts.py`` would resolve. The
        mismatch would be UNCONDITIONAL, since the surface is identical every
        round, and it shows up as soon as the proposer issues the
        manifest-advertised snapshot-relative form rather than a bare
        filename. Anchoring the
        snapshot root makes the snapshot-relative path resolve too, so the read
        does not depend on which path shape the proposer picked; keeping the
        subtree roots lets the bare subtree-relative form resolve too. The
        escape guard in :func:`_resolve_under_mutable_roots` still
        rejects any ``..`` traversal out of whichever root matched, so widening
        the accepted roots never widens the readable surface beyond the
        snapshot.
        """
        root = self.generation_root.resolve()
        seen: dict[str, Path] = {str(root): root}
        for mp in self.mutations:
            candidate = root / Path(mp.source_root).name
            if candidate.exists():
                seen.setdefault(str(candidate), candidate)
        return tuple(seen.values())


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
    :class:`ADKProposerAgent` to wrap each ``goldfive.run`` of the custom
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
    misleading empty result) makes the misuse obvious — the custom agent
    must be run through :class:`ADKProposerAgent`, which binds the
    context around the run.
    """
    ctx = _TOOL_CONTEXT.get()
    if ctx is None:
        raise RuntimeError(
            "proposer tool called with no bound ProposerToolContext; "
            "proposer tools may only be called from within an "
            "ADKProposerAgent run (see bind_proposer_tool_context)"
        )
    return ctx


__all__ = [
    "ProposerToolContext",
    "bind_proposer_tool_context",
]
