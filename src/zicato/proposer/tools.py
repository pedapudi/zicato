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
    generation_id:
        The id of the generation ``generation_root`` snapshots — the
        round's champion (the proposer's PARENT generation). Lets
        ``read_parent_diff`` resolve the generation-store coordinates of
        the tree the tools are already reading. Empty (the legacy
        default) degrades that tool to an explicit "coordinates
        unavailable" answer.
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


def mutation_track_record(mutation_id: str) -> str:
    """Return one mutation point's banded per-epoch track record as JSON.

    The tool surface of the mutation-point fertility map
    (:func:`zicato.index.query.mutation_point_track_record`): how many
    settled experiments this epoch touched ``mutation_id``, how many were
    promoted, a BUCKETED Δscalar summary, and a coarse recency flag.
    AGGREGATES ONLY — the same restricted-visibility banding as the
    manifest annotation (bucketed deltas via the prompt renderer's bands,
    no exact experiment-level delta, no board identity), so a tool-using
    proposer learns nothing the annotated manifest would not already show.

    HONESTY (load-bearing): every figure is **experiment-level** — the
    ``basis`` field says "experiments touching this point" and the payload
    counts how many of those experiments also touched other points (their
    credit is confounded). Nothing here is causal.

    ``mutation_id`` must name a point in the CURRENT round's manifest
    (:func:`list_mutation_points`); an unknown id raises
    :class:`ValueError` so the agent gets an actionable retry signal. A
    point no settled experiment has touched (or a workspace whose index
    was never built) returns a zeroed record rather than an error — "no
    track record yet" is a real answer.
    """
    ctx = _active_context()
    if mutation_id not in {mp.id for mp in ctx.mutations}:
        raise ValueError(
            f"mutation_track_record: unknown mutation id {mutation_id!r}; "
            "only ids in the current manifest (see list_mutation_points) "
            "are valid"
        )
    from zicato.index.query import mutation_point_track_record  # noqa: PLC0415
    from zicato.proposer.prompts import render_mutation_track_annotation  # noqa: PLC0415

    basis = (
        "experiments touching this point (experiment-level attribution; "
        "multi-patch experiments confound credit — not causal)"
    )
    records = mutation_point_track_record(
        ctx.workspace_root / "index.db", ctx.epoch_id, mutation_id
    )
    record = records.get(mutation_id)
    if record is None:
        return json.dumps(
            {
                "mutation_id": mutation_id,
                "basis": basis,
                "experiments_touching": 0,
                "promoted": 0,
                "confounded_experiments": 0,
                "recent": False,
                "summary": "(no settled experiment has touched this point yet)",
            },
            indent=2,
        )
    return json.dumps(
        {
            "mutation_id": mutation_id,
            "basis": basis,
            "experiments_touching": record.experiments_touching,
            "promoted": record.promoted,
            "confounded_experiments": record.confounded_experiments,
            "recent": record.recent_touching > 0,
            "summary": render_mutation_track_annotation(record),
        },
        indent=2,
    )


#: Hard cap on the characters ``read_journal`` returns, matching
#: ``_PARENT_DIFF_LIMIT_CHARS``. The journal grew unbounded once it stopped
#: truncating the proposer's own ``why`` at write time (issue #123) — the
#: budget moved here, to the reader, where dropping text is recoverable.
#: The other two journal-consuming LLM paths already cap independently
#: (``epoch.analysis`` at 60_000, ``analyzer.report_data`` at 40_000).
_JOURNAL_LIMIT_CHARS = 20_000


#: An entry heading as :func:`zicato.epoch.journal._render_section` writes it:
#: ``## `` then a ``_version_label`` (``v`` + digits, or a backticked free-form
#: generation id) then the em-dash separator. Deliberately NARROWER than a bare
#: ``\n## `` scan: since issue #123 stopped truncating, a ``core_idea`` / ``why``
#: reaches the journal verbatim, so an ordinary markdown heading inside the
#: proposer's own prose (``## Approach``) sits in the file looking exactly like
#: an entry boundary. Anchoring on the label shape keeps :func:`_tail_entries`
#: from opening the window mid-body on a line the proposer wrote as prose.
_ENTRY_HEADING = re.compile(r"\n## (?:v\d+|`[^`\n]+`) —")


def _tail_entries(text: str, limit: int) -> str:
    """Keep the NEWEST ``limit``-ish chars of a journal, on an entry boundary.

    The journal is chronological-append, so the tail is the part a proposer
    reasoning about what to try next actually needs. We take the last
    ``limit`` characters, then advance to the first whole section inside
    that window (:data:`_ENTRY_HEADING`, the boundary
    :func:`~zicato.epoch.journal.append_journal_entry` writes) so the
    proposer never reads a section that starts mid-sentence. A single
    section longer than the whole budget has no boundary to find, so it is
    returned clipped rather than dropped entirely.

    A prose line that reproduces the full heading shape (``## v9 — ...``,
    not just ``## Something``) is still indistinguishable from a real
    boundary by construction — ``journal.md`` is markdown, not a framed
    format. The narrowed pattern removes the case that actually occurs;
    nothing short of escaping the body at write time removes the rest, and
    that would cost the verbatim preservation issue #123 exists to give.
    """
    if len(text) <= limit:
        return text
    tail = text[-limit:]
    match = _ENTRY_HEADING.search(tail)
    if match is not None:
        tail = tail[match.start() + 1 :]
    dropped = len(text) - len(tail)
    return (
        f"[... truncated: {dropped} earlier chars dropped from a {len(text)}-char "
        f"journal; the newest entries follow ...]\n\n{tail}"
    )


def read_journal() -> str:
    """Return the epoch's running narrative journal, or ``""`` when absent.

    Reuses :func:`zicato.core.workspace.journal_path`; the empty string
    is the proposer-side sentinel for "no journal yet", matching how the
    default proposer treats a missing insights file.

    Capped at ``_JOURNAL_LIMIT_CHARS``, keeping the newest entries — the
    same runaway-context guard the other unbounded tools carry.
    """
    ctx = _active_context()
    from zicato.core.workspace import journal_path  # noqa: PLC0415

    path = journal_path(ctx.workspace_root, ctx.epoch_id)
    if not path.is_file():
        return ""
    return _tail_entries(path.read_text(encoding="utf-8"), _JOURNAL_LIMIT_CHARS)


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


#: Hard cap on the characters ``read_parent_diff`` returns. A promotion's
#: diff is normally a handful of prompt/config edits; only a pathological
#: whole-tree rewrite is trimmed (with a note), mirroring the other tools'
#: runaway-context guards.
_PARENT_DIFF_LIMIT_CHARS = 20_000

#: Per-patch cap on the new-value text ``read_parent_diff``'s directory-
#: backend fallback echoes. The full value is visible via
#: ``read_mutable_file`` anyway; the fallback only needs enough to show
#: WHAT changed.
_PATCH_VALUE_LIMIT_CHARS = 2_000


def _truncate_note(text: str, limit: int) -> str:
    """Clip ``text`` to ``limit`` chars with an explicit truncation note."""
    if len(text) <= limit:
        return text
    return f"{text[:limit]}\n[... truncated: output exceeds {limit} chars ...]"


def read_parent_diff() -> str:
    """Show what the LAST PROMOTION changed — the parent generation's diff.

    The round's champion (the generation whose snapshot the other tools
    read) was minted by the most recent promotion; this tool shows that
    change so the agent can build on — or deliberately depart from — what
    just worked.

    * **Git backend (the default):** one read-only ``git diff`` between
      the parent generation's tag and ITS parent's tag
      (:meth:`~zicato.epoch.git_genstore.GitGenerationStore.diff_generations`
      — tree objects only, nearly free, nothing is written or checked
      out).
    * **Directory backend (or a generation with no commit):** falls back
      to the generation's recorded patch set from the journal
      (``list_patches`` reads ``experiment.json``), rendered compactly —
      the same change, as patch records rather than a line diff.

    A seed generation (no prior promotion) returns an explicit notice.
    Output is capped at :data:`_PARENT_DIFF_LIMIT_CHARS`. Read-only by
    contract, like every tool here.
    """
    ctx = _active_context()
    if not ctx.generation_id:
        return (
            "(parent-generation coordinates unavailable in this context; "
            "no promotion diff to show)"
        )
    from zicato.epoch.genstore import default_generation_store  # noqa: PLC0415
    from zicato.epoch.git_genstore import GitGenerationStore  # noqa: PLC0415

    store = default_generation_store(ctx.workspace_root)
    if isinstance(store, GitGenerationStore) and store.has_generation(
        ctx.epoch_id, ctx.generation_id
    ):
        parent_id = store.parent_generation_id(ctx.epoch_id, ctx.generation_id)
        if parent_id is None:
            return (
                f"(generation {ctx.generation_id} is a seed — there is no "
                "prior promotion to diff)"
            )
        diff = store.diff_generations(ctx.epoch_id, parent_id, ctx.generation_id)
        if not diff.strip():
            return (
                f"(generations {parent_id} -> {ctx.generation_id} are "
                "byte-identical — the promoted patch set changed nothing textually)"
            )
        header = f"# diff {parent_id} -> {ctx.generation_id} (what the last promotion changed)\n"
        return _truncate_note(header + diff, _PARENT_DIFF_LIMIT_CHARS)

    # Directory backend (or no commit for the coordinate): the journal's
    # patch records are the durable account of what derived this generation.
    record = store.list_patches(ctx.epoch_id, ctx.generation_id)
    if not record.patches:
        return (
            f"(generation {ctx.generation_id} has no recorded patch set — a "
            "seed generation; there is no prior promotion to show)"
        )
    lines = [
        f"# patch record for {ctx.generation_id} (what the last promotion "
        "changed; journal patch records — not a line diff)"
    ]
    for patch in record.patches:
        lines.append(f"- {patch.op} {patch.mutation_id}: {patch.rationale or '(no rationale)'}")
        if patch.new_content is not None:
            value = _truncate_note(patch.new_content, _PATCH_VALUE_LIMIT_CHARS)
            lines.append("  new content:\n" + "\n".join(f"    {ln}" for ln in value.splitlines()))
        elif patch.new_numeric is not None:
            lines.append(f"  new numeric: {patch.new_numeric}")
        elif patch.new_enum is not None:
            lines.append(f"  new enum: {patch.new_enum}")
    return _truncate_note("\n".join(lines), _PARENT_DIFF_LIMIT_CHARS)


def mutation_usage(mutation_id: str) -> str:
    """Find where a mutation point's current value/symbol is referenced.

    Grounds a candidate edit in how the point is actually USED: greps the
    parent snapshot's mutable subtrees for (a) the point's symbol — the
    trailing segment of its id, which by the marker convention names the
    variable/kwarg holding the span — and (b) its current content, when
    that content is a short single-line literal (a numeric/enum value, a
    one-line prompt), so the agent sees every consumer of the value it is
    about to change.

    Bounded and sandboxed by construction: each search is delegated to
    :func:`grep_mutable` (regex-escaped), so the mutable-subtree
    resolution, the escape guard, and the :data:`_GREP_MATCH_LIMIT` cap
    all apply unchanged. Read-only. ``mutation_id`` must name a point in
    the current manifest; an unknown id raises :class:`ValueError`.
    """
    ctx = _active_context()
    point = next((mp for mp in ctx.mutations if mp.id == mutation_id), None)
    if point is None:
        raise ValueError(
            f"mutation_usage: unknown mutation id {mutation_id!r}; only ids "
            "in the current manifest (see list_mutation_points) are valid"
        )

    terms: list[str] = []
    symbol = mutation_id.rsplit("__", 1)[-1].strip()
    if symbol:
        terms.append(symbol)
    content = point.content.strip()
    if content and "\n" not in content and len(content) <= 120 and content not in terms:
        terms.append(content)

    root = ctx.generation_root.resolve()
    file_path = Path(point.file)
    try:
        rel_file = str(file_path.resolve().relative_to(root))
    except ValueError:
        rel_file = str(file_path)
    header = (
        f"# usage of mutation point {mutation_id} "
        f"(defined at {rel_file}:{point.line_start}-{point.line_end})"
    )
    sections = [header]
    for term in terms:
        sections.append(f"### references to {term!r}\n{grep_mutable(re.escape(term))}")
    return "\n\n".join(sections)


#: The full read-only tool set a custom proposer agent may opt into at
#: once. A custom ``agent.py`` does ``tools=list(DEFAULT_PROPOSER_TOOLS)``
#: to expose every tool above to its ``LlmAgent``.
DEFAULT_PROPOSER_TOOLS = (
    list_mutation_points,
    read_mutable_file,
    grep_mutable,
    read_journal,
    read_insights,
    mutation_track_record,
    read_parent_diff,
    mutation_usage,
)


__all__ = [
    "DEFAULT_PROPOSER_TOOLS",
    "ProposerToolContext",
    "bind_proposer_tool_context",
    "grep_mutable",
    "list_mutation_points",
    "mutation_track_record",
    "mutation_usage",
    "read_insights",
    "read_journal",
    "read_mutable_file",
    "read_parent_diff",
]
