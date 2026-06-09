"""Read-only proposer tool registry for custom ADK proposer agents.

A custom proposer (``proposers/<name>/agent.py``) is a real ADK
``LlmAgent`` that may call tools while it reasons about the next
experiment. This module ships the tools it may call. They are plain
module-level functions so a custom agent can simply::

    from zicato.proposer.tools import DEFAULT_PROPOSER_TOOLS
    agent = LlmAgent(name="my_proposer", instruction="...", tools=list(DEFAULT_PROPOSER_TOOLS))

and ADK wraps each as a ``FunctionTool`` automatically.

Why a context var
-----------------
A tool function cannot carry the per-round runtime context (which
generation snapshot to read, the mutation manifest, the epoch's
journal) as a bound argument, because the custom agent is constructed
ONCE at import time — long before any round runs, and reused across
every challenger. The tools therefore read their context from a
module-level :class:`contextvars.ContextVar`, which
:func:`ADKProposerAgent.propose` sets immediately around each agent run
via :func:`bind_proposer_tool_context`. A ``ContextVar`` (not a plain
global) is used so concurrent challengers — each running its own agent
on its own asyncio task — never leak context into one another: a child
task started under one ``bind_proposer_tool_context`` block sees that
block's value, and the reset on block exit restores the prior value.

Read-only by contract
----------------------
Every tool here READS the parent generation's snapshot and the epoch's
narrative — none of them write. A proposer tool that mutated the
snapshot would corrupt the very tree the round is about to patch (and
break the content-hash guard the applier relies on), so the whole
surface is deliberately read-only. ``read_mutable_file`` and
``grep_mutable`` additionally refuse any path that escapes the mutable
subtrees, so a tool call cannot read outside the surface the proposer
is allowed to reason about.
"""

from __future__ import annotations

import contextvars
import json
import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from zicato.core.types import MutationPoint

#: Hard cap on the number of ``grep_mutable`` matches returned, so a
#: pathological pattern cannot flood the agent's context window. The cap
#: is annotated in the returned text when it bites.
_GREP_MATCH_LIMIT = 200

#: Hard cap on bytes ``read_mutable_file`` returns for a single file —
#: a runaway-context guard mirroring the proposer prompt's mutation
#: content ceiling. Generous enough for any real prompt body / source
#: file; only a pathologically large file is trimmed (with a note).
_READ_FILE_LIMIT_CHARS = 200_000


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
    """

    workspace_root: Path
    generation_root: Path
    epoch_id: str
    mutations: tuple[MutationPoint, ...]

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
        ``agent/prompts.py``). The old derivation, however, admitted ONLY the
        narrower declared subtree as a readable root when an adapter declared
        one (``source_root`` basename ``agent`` → root ``<snapshot>/agent``), so
        the very path the manifest hands the proposer resolved to
        ``<snapshot>/agent/agent/prompts.py`` — not a file — and raised; only
        the bare subtree-relative ``prompts.py`` resolved. The mismatch is
        UNCONDITIONAL (the surface is identical every round); it tends to
        surface once the proposer starts issuing the manifest-advertised
        snapshot-relative form rather than a bare filename. Anchoring the
        snapshot root makes the snapshot-relative path resolve too, so the read
        no longer depends on which path shape the proposer happened to pick;
        keeping the subtree roots lets the bare subtree-relative form resolve as
        before. The escape guard in :func:`_resolve_under_mutable_roots` still
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


def _resolve_under_mutable_roots(relative_path: str, ctx: ProposerToolContext) -> Path:
    """Resolve ``relative_path`` under a mutable root, rejecting escapes.

    The path is interpreted relative to each mutable subtree root in turn
    and accepted only if the resolved target stays *inside* that root
    (``Path.is_relative_to`` after resolution, so ``..`` traversal and
    absolute paths that climb out are rejected) and the target file
    exists. Raises :class:`ValueError` on any path that escapes every
    mutable root or does not resolve to a file — a read-only tool must
    never reach outside the mutable surface.
    """
    candidate = Path(relative_path)
    if candidate.is_absolute():
        raise ValueError(
            "read_mutable_file: path must be relative to a mutable tree, got "
            f"absolute {relative_path!r}"
        )
    for root in ctx.mutable_roots():
        resolved_root = root.resolve()
        target = (resolved_root / candidate).resolve()
        if not target.is_relative_to(resolved_root):
            # Escapes this root via ``..`` — try the next, but never accept.
            continue
        if target.is_file():
            return target
    raise ValueError(
        f"read_mutable_file: {relative_path!r} does not resolve to a file "
        "inside any mutable tree (path traversal outside the mutable "
        "surface is rejected)"
    )


def list_mutation_points() -> str:
    """Return the current mutation manifest as a JSON string.

    Mirrors the manifest the default proposer is handed in its user
    prompt: each entry carries the mutation point's ``id``, ``kind``,
    ``file`` (relative to the snapshot root when possible), ``content``,
    and ``metadata``. The agent reads this to choose which mutation ids
    to target — only ids present here are valid patch targets.
    """
    ctx = _active_context()
    root = ctx.generation_root.resolve()
    entries: list[dict[str, object]] = []
    for mp in ctx.mutations:
        file_path = Path(mp.file)
        try:
            rel_file = str(file_path.resolve().relative_to(root))
        except ValueError:
            rel_file = str(file_path)
        entries.append(
            {
                "id": mp.id,
                "kind": mp.kind,
                "file": rel_file,
                "line_start": mp.line_start,
                "line_end": mp.line_end,
                "content": mp.content,
                "metadata": dict(mp.metadata),
            }
        )
    return json.dumps({"mutation_points": entries}, indent=2)


def read_mutable_file(relative_path: str) -> str:
    """Read a file under the generation root's mutable subtrees.

    ``relative_path`` is interpreted relative to the mutable subtree
    roots (derived from the manifest's source roots). Paths that escape
    the mutable trees — absolute paths, ``..`` traversal — are rejected
    with a :class:`ValueError`. Read-only: the file is never modified.
    The returned text is trimmed (with a note) only for a pathologically
    large file.
    """
    ctx = _active_context()
    target = _resolve_under_mutable_roots(relative_path, ctx)
    text = target.read_text(encoding="utf-8")
    if len(text) > _READ_FILE_LIMIT_CHARS:
        head = text[:_READ_FILE_LIMIT_CHARS]
        return f"{head}\n[... truncated: file exceeds {_READ_FILE_LIMIT_CHARS} chars ...]"
    return text


def grep_mutable(pattern: str) -> str:
    """Regex-search the mutable subtrees, returning ``path:line: text``.

    Walks every file under the mutable subtree roots and returns each
    matching line as ``<relative_path>:<line_no>: <line text>``. The
    match count is capped at :data:`_GREP_MATCH_LIMIT`; truncation is
    annotated in the returned text. Read-only. A line with no matches
    across any file returns the literal ``"(no matches)"`` so the agent
    sees an explicit empty signal rather than a blank string.
    """
    ctx = _active_context()
    try:
        compiled = re.compile(pattern)
    except re.error as exc:
        raise ValueError(f"grep_mutable: invalid regex {pattern!r}: {exc}") from exc

    matches: list[str] = []
    truncated = False
    for root in ctx.mutable_roots():
        resolved_root = root.resolve()
        for path in sorted(resolved_root.rglob("*")):
            if not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            try:
                rel = str(path.relative_to(resolved_root))
            except ValueError:
                rel = str(path)
            for line_no, line in enumerate(text.splitlines(), start=1):
                if compiled.search(line):
                    matches.append(f"{rel}:{line_no}: {line}")
                    if len(matches) >= _GREP_MATCH_LIMIT:
                        truncated = True
                        break
            if truncated:
                break
        if truncated:
            break

    if not matches:
        return "(no matches)"
    body = "\n".join(matches)
    if truncated:
        body += f"\n[... truncated: more than {_GREP_MATCH_LIMIT} matches ...]"
    return body


def read_journal() -> str:
    """Return the epoch's running narrative journal, or ``""`` when absent.

    Reuses :func:`zicato.core.workspace.journal_path`; the empty string
    is the proposer-side sentinel for "no journal yet", matching how the
    default proposer treats a missing insights file.
    """
    ctx = _active_context()
    from zicato.core.workspace import journal_path  # noqa: PLC0415

    path = journal_path(ctx.workspace_root, ctx.epoch_id)
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8")


def read_insights() -> str:
    """Return the epoch's latest analyzer insights, or ``""`` when absent.

    Reuses :func:`zicato.analyzer.load_latest_insights` — the same helper
    the default proposer uses to embed insights into its user prompt — so
    a custom agent sees identical content. Empty string when no insights
    exist.
    """
    ctx = _active_context()
    from zicato.analyzer import load_latest_insights  # noqa: PLC0415

    return load_latest_insights(ctx.workspace_root, ctx.epoch_id)


#: The full read-only tool set a custom proposer agent may opt into at
#: once. A custom ``agent.py`` does ``tools=list(DEFAULT_PROPOSER_TOOLS)``
#: to expose every tool above to its ``LlmAgent``.
DEFAULT_PROPOSER_TOOLS = (
    list_mutation_points,
    read_mutable_file,
    grep_mutable,
    read_journal,
    read_insights,
)


__all__ = [
    "DEFAULT_PROPOSER_TOOLS",
    "ProposerToolContext",
    "bind_proposer_tool_context",
    "grep_mutable",
    "list_mutation_points",
    "read_insights",
    "read_journal",
    "read_mutable_file",
]
