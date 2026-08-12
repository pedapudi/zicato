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
A tool function cannot carry the per-round runtime context as a bound
argument, because the custom agent is constructed ONCE at import time —
long before any round runs — and reused across every challenger. The
tools therefore read their context from a module-level
:class:`contextvars.ContextVar`. That plumbing lives in
:mod:`zicato.proposer.tool_context` (see its docstring for the full
rationale, including why it is a separate module) and is re-exported
here, so ``from zicato.proposer.tools import ProposerToolContext,
bind_proposer_tool_context`` stays the import site it has always been.

Never write to the snapshot
---------------------------
Every tool here but :func:`~zicato.proposer.validate.validate_patches`
READS the parent generation's snapshot and the epoch's narrative — none
of them write. A proposer tool that mutated the snapshot would corrupt
the very tree the round is about to patch (and break the content-hash
guard the applier relies on), so that prohibition is absolute and
``validate_patches`` does not relax it: it writes only into a disposable
scratch copy in the OS temp root, never into the snapshot, and it
consumes no board data and produces no score (see
:mod:`zicato.proposer.validate`). ``read_mutable_file`` and
``grep_mutable`` additionally refuse any path that escapes the mutable
roots, so a tool call cannot read outside the generation snapshot.

Note the scope this guard does and does not draw. The readable surface is
the *whole snapshot* — :meth:`ProposerToolContext.mutable_roots` anchors
the generation root first, so the proposer can read the non-mutable code
that consumes a mutable string, which is what makes a candidate rewrite
groundable. The guard is snapshot containment (no ``..`` traversal, no
absolute paths), NOT a narrowing of what the proposer may reason about.
What the proposer may *change* stays narrow and operator-owned: patches
are addressed by mutation id, and the applier writes only what an id
covers.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from zicato.proposer.redacted_query import REDACTED_QUERY_TOOLS
from zicato.proposer.tool_context import (
    ProposerToolContext,
    _active_context,
    bind_proposer_tool_context,
)
from zicato.proposer.validate import validate_patches

#: Hard cap on the number of ``grep_mutable`` matches returned, so a
#: pathological pattern cannot flood the agent's context window. The cap
#: is annotated in the returned text when it bites.
_GREP_MATCH_LIMIT = 200

#: Hard cap on bytes ``read_mutable_file`` returns for a single file —
#: a runaway-context guard mirroring the proposer prompt's mutation
#: content ceiling. Generous enough for any real prompt body / source
#: file; only a pathologically large file is trimmed (with a note).
_READ_FILE_LIMIT_CHARS = 200_000


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


def _walk_roots(ctx: ProposerToolContext) -> tuple[Path, ...]:
    """Return the mutable roots to WALK — the outermost ones only.

    :meth:`ProposerToolContext.mutable_roots` deliberately returns the
    snapshot root *and* each declared subtree, so path *resolution* accepts
    both the manifest-advertised shape (``agent/prompts.py``) and the bare
    subtree-relative one (``prompts.py``). A recursive walk must not iterate
    that list directly: the declared subtrees are descendants of the
    snapshot root, so every file under one would be visited once per
    containing root and emitted under a different relative path each time —
    duplicate hits in inconsistent shapes, and a match budget consumed
    several times over by the same lines.

    Filtering to the roots that are not contained in another root keeps the
    walked *file set* identical (the snapshot root already covers every
    subtree) while visiting each file exactly once, under the
    snapshot-relative path :func:`list_mutation_points` advertises.
    """
    resolved = list(dict.fromkeys(root.resolve() for root in ctx.mutable_roots()))
    return tuple(
        root
        for root in resolved
        if not any(other != root and root.is_relative_to(other) for other in resolved)
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

    Walks every file under the mutable surface — the outermost mutable
    roots, see :func:`_walk_roots`, so a file inside a declared subtree is
    visited once rather than once per containing root — and returns each
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
    for root in _walk_roots(ctx):
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


#: The full proposer tool set a custom proposer agent may opt into at once.
#: A custom ``agent.py`` does ``tools=list(DEFAULT_PROPOSER_TOOLS)`` to
#: expose every tool to its ``LlmAgent``. This tuple IS the sanctioned
#: surface: adding to it widens what every proposer can do, so it is pinned
#: by name in ``tests/test_proposer_tools.py`` rather than counted.
#:
#: Three groups, in order:
#:
#: * the SNAPSHOT tools (``list_mutation_points`` … ``mutation_usage``) —
#:   read-only over the parent generation's tree and the epoch's narrative;
#: * ``validate_patches`` — the only tool that writes anything, and the
#:   exception is narrow by construction: a disposable scratch copy in the
#:   OS temp root, never the snapshot the round is about to patch, and it
#:   consumes no board data and produces no score (see
#:   :mod:`zicato.proposer.validate`);
#: * the REDACTED QUERY tools (:data:`~zicato.proposer.redacted_query.REDACTED_QUERY_TOOLS`)
#:   — banded aggregates over the champion's TRAIN slice only, through the
#:   same mechanical redaction the process-exemplar channel performs. They
#:   emit no entry id, no task text and no model output, and they can never
#:   read the holdout (see :mod:`zicato.proposer.redacted_query`).
#:
#: The standing prohibition none of them relax: no tool may write to the
#: generation snapshot, and no tool may widen the proposer's view of the
#: board beyond the marginal aggregates the overfitting envelope allows.
DEFAULT_PROPOSER_TOOLS = (
    list_mutation_points,
    read_mutable_file,
    grep_mutable,
    read_journal,
    read_insights,
    mutation_track_record,
    read_parent_diff,
    mutation_usage,
    validate_patches,
    *REDACTED_QUERY_TOOLS,
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
    "validate_patches",
]
