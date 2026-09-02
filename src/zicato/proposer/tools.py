"""Where a mutation point's value is used, read off the round's snapshot.

:func:`mutation_usage` is the one question this module answers, and a
proposal episode is served it as a host tool
(:func:`zicato.proposer.foe_agent.build_episode_tools`) so an edit can be
grounded in how the value it changes is actually read.
:func:`grep_mutable` is the sandboxed search it is built from, kept a
separate function because the containment guarantee is asserted against it
directly.

What an episode may call is a closed list, declared and asserted by name as
:data:`~zicato.proposer.foe_request.SANCTIONED_TOOLS`: Foe's own ``read``,
``grep``, ``edit`` and ``block`` cover reading the snapshot and changing the
working copy, and zicato answers ``mutation_usage`` and
``validate_patches``.

Why a context var
-----------------
A tool function cannot carry the per-round runtime context as a bound
argument, because the implementations are module-level and reused across
every challenger. The tools therefore read their context from a
module-level :class:`contextvars.ContextVar`. That plumbing lives in
:mod:`zicato.proposer.tool_context` (see its docstring for the full
rationale, including why it is a separate module).

Never write to the snapshot
---------------------------
Both functions here READ the parent generation's snapshot; neither writes.
A proposer tool that mutated the snapshot would corrupt the very tree the
round is about to patch, and would break the content-hash guard the applier
relies on, so that prohibition is absolute. The other host tool an episode
is served does not relax it either: ``validate_patches`` writes only into a
disposable scratch copy in the OS temp root, never into the snapshot, and it
consumes no board data and produces no score (see
:mod:`zicato.proposer.validate`).

The readable surface is the whole snapshot — the generation root, rather
than the declared mutable subtrees inside it. That width is what lets the
search reach the non-mutable code consuming a mutable string, which is what
makes a candidate rewrite groundable. What the proposer may *change* stays
narrow and operator-owned: patches are addressed by mutation id, and the
applier writes only what an id covers.
"""

from __future__ import annotations

import re
from pathlib import Path

from zicato.proposer.tool_context import _active_context

#: Hard cap on the number of ``grep_mutable`` matches returned, so a
#: pathological pattern cannot flood the agent's context window. The cap
#: is annotated in the returned text when it bites.
_GREP_MATCH_LIMIT = 200


def grep_mutable(pattern: str) -> str:
    """Regex-search the parent snapshot, returning ``path:line: text``.

    Walks every file under the generation root — the whole snapshot, so a
    mutable value's non-mutable consumers are found too — and returns each
    matching line as ``<snapshot-relative path>:<line_no>: <line text>``.
    The match count is capped at :data:`_GREP_MATCH_LIMIT`; truncation is
    annotated in the returned text. Read-only. A pattern that matches
    nowhere returns the literal ``"(no matches)"`` so the caller sees an
    explicit empty signal rather than a blank string.
    """
    ctx = _active_context()
    try:
        compiled = re.compile(pattern)
    except re.error as exc:
        raise ValueError(f"grep_mutable: invalid regex {pattern!r}: {exc}") from exc

    matches: list[str] = []
    truncated = False
    root = ctx.generation_root.resolve()
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        try:
            rel = str(path.relative_to(root))
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

    if not matches:
        return "(no matches)"
    body = "\n".join(matches)
    if truncated:
        body += f"\n[... truncated: more than {_GREP_MATCH_LIMIT} matches ...]"
    return body


def mutation_usage(mutation_id: str) -> str:
    """Find where a mutation point's current value/symbol is referenced.

    Grounds a candidate edit in how the point is actually USED: greps the
    parent snapshot for (a) the point's symbol — the trailing segment of
    its id, which by the marker convention names the variable/kwarg
    holding the span — and (b) its current content, when that content is a
    short single-line literal (a numeric/enum value, a one-line prompt), so
    the agent sees every consumer of the value it is about to change.

    Bounded and sandboxed by construction: each search is delegated to
    :func:`grep_mutable` (regex-escaped), so the snapshot containment and
    the :data:`_GREP_MATCH_LIMIT` cap both apply unchanged. Read-only.
    ``mutation_id`` must name a point in the current round's manifest; an
    unknown id raises :class:`ValueError` so the agent gets an actionable
    retry signal.
    """
    ctx = _active_context()
    point = next((mp for mp in ctx.mutations if mp.id == mutation_id), None)
    if point is None:
        raise ValueError(
            f"mutation_usage: unknown mutation id {mutation_id!r}; only ids "
            "in the round's manifest are valid"
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


__all__ = [
    "grep_mutable",
    "mutation_usage",
]
