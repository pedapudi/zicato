"""Operator-edited proposer brief: parsing, enforcement, embedding.

A ``brief.md`` lives at the epoch root alongside ``board.jsonl`` and
``scoring.json``. It is the operator's running guidance to the proposer
— the place where you encode "don't touch the router system prompt this
epoch" or "prefer to edit the planner's tool descriptions over the
planner's persona" without rewriting code.

The proposer brief is an *epoch-level* concept: one brief governs every
proposer call within an epoch. It is distinct from the per-board-entry
``Rubric`` (see :mod:`zicato.board.rubric`), which is an LLM-as-judge scorer
for a single board entry.

Two specially-named sections carry structured signal that the proposer
enforces at validation time:

* ``# Forbidden edits`` — any mutation-point id mentioned in a bullet
  (in backticks or quoted) is hard-forbidden. The proposer will refuse
  to emit a patch targeting it; the runner re-checks before applying.
* ``# Preferred edits`` — soft hint. The proposer is encouraged to look
  there first but is not constrained.

The full brief text passes through verbatim to the system prompt so
free-form guidance the operator wrote outside the two structured
sections still reaches the model.

Section headings are matched case-insensitively and either ``#`` or
``##`` is accepted; everything until the next heading at the same or
shallower level is treated as the section body. Bullet markers are
``-``, ``*``, or ``+``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from zicato.core.types import Patch

#: Heading prefixes considered "section starts" by the parser.
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*$")

#: Bullet line prefix. Captures the body so id-extraction can run on it.
_BULLET_RE = re.compile(r"^\s*[-*+]\s+(.*)$")

#: Id-extraction patterns. Backticked tokens win; otherwise single- or
#: double-quoted tokens are accepted. A bullet may mention multiple ids.
_BACKTICK_RE = re.compile(r"`([^`]+)`")
_SINGLE_QUOTE_RE = re.compile(r"'([^']+)'")
_DOUBLE_QUOTE_RE = re.compile(r'"([^"]+)"')

_FORBIDDEN_HEADING = "forbidden edits"
_PREFERRED_HEADING = "preferred edits"


@dataclass(frozen=True, slots=True)
class ProposerBrief:
    """Parsed view of an operator-edited ``brief.md``.

    The epoch-level brief the operator hands the proposer. Not to be
    confused with :class:`zicato.board.rubric`'s per-entry judge — this
    is improvement-loop steering, that is single-entry scoring.

    Fields
    ------
    text:
        The original markdown source, byte-for-byte. Embedded into the
        proposer's system prompt so the model sees the operator's prose
        guidance in addition to the structured forbidden/preferred lists.
    forbidden_ids:
        Mutation-point ids the proposer MUST NOT touch this epoch.
        Order is the order of appearance in the brief.
    preferred_ids:
        Mutation-point ids the proposer is encouraged to consider first.
        Soft hint; not enforced.
    """

    text: str
    forbidden_ids: tuple[str, ...]
    preferred_ids: tuple[str, ...]


def _extract_ids_from_bullet(body: str) -> list[str]:
    """Return ids mentioned in a single bullet body.

    Backticked tokens take priority — they are the unambiguous form and
    line up with how the CLI renders mutation ids. If no backticked
    tokens appear, quoted tokens are accepted as a convenience for
    operators editing the brief in editors that auto-format backticks
    away.
    """

    ids: list[str] = []
    seen: set[str] = set()
    for tok in _BACKTICK_RE.findall(body):
        tok = tok.strip()
        if tok and tok not in seen:
            ids.append(tok)
            seen.add(tok)
    if ids:
        return ids
    for pattern in (_SINGLE_QUOTE_RE, _DOUBLE_QUOTE_RE):
        for tok in pattern.findall(body):
            tok = tok.strip()
            if tok and tok not in seen:
                ids.append(tok)
                seen.add(tok)
    return ids


def _split_into_sections(text: str) -> dict[str, list[str]]:
    """Split markdown text by heading.

    Returns a mapping from lowercased heading text to a list of body
    lines. Sections at any heading depth are recorded; the caller picks
    the ones it cares about by lowercased title.

    Headings outside any recognized form become a fall-through section
    named ``""`` (the empty string) holding any text before the first
    heading.
    """

    sections: dict[str, list[str]] = {"": []}
    current = ""
    for line in text.splitlines():
        m = _HEADING_RE.match(line)
        if m is not None:
            heading_text = m.group(2).strip().lower()
            current = heading_text
            sections.setdefault(current, [])
            continue
        sections.setdefault(current, []).append(line)
    return sections


def _ids_from_section(body_lines: list[str]) -> tuple[str, ...]:
    """Collect ids from every bullet line in a section body."""

    out: list[str] = []
    seen: set[str] = set()
    for line in body_lines:
        m = _BULLET_RE.match(line)
        if m is None:
            continue
        for ident in _extract_ids_from_bullet(m.group(1)):
            if ident not in seen:
                out.append(ident)
                seen.add(ident)
    return tuple(out)


def load_brief(path: Path) -> ProposerBrief:
    """Parse a ``brief.md`` file from disk.

    The file is read as UTF-8. A missing file is a hard error — the
    proposer cannot operate without operator guidance, even if that
    guidance is an empty brief. Operators who want a permissive default
    should commit a brief that says so explicitly rather than relying
    on a defaulting behavior here.

    Raises
    ------
    FileNotFoundError
        If *path* does not exist.
    OSError
        If the file cannot be read (forwarded from :func:`pathlib.Path.read_text`).
    """

    text = path.read_text(encoding="utf-8")
    sections = _split_into_sections(text)

    forbidden_ids: tuple[str, ...] = ()
    preferred_ids: tuple[str, ...] = ()
    for title, body in sections.items():
        normalized = title.strip().lower()
        if normalized == _FORBIDDEN_HEADING:
            forbidden_ids = _ids_from_section(body)
        elif normalized == _PREFERRED_HEADING:
            preferred_ids = _ids_from_section(body)

    return ProposerBrief(text=text, forbidden_ids=forbidden_ids, preferred_ids=preferred_ids)


def enforce_forbidden(
    patches: list[Patch] | tuple[Patch, ...], forbidden_ids: tuple[str, ...]
) -> list[str]:
    """Check a list of patches against the brief's forbidden-id set.

    Returns
    -------
    list[str]
        A list of human-readable error messages, one per offending
        patch. An empty list means the patch set is clean.

    The check is strict equality on :attr:`Patch.mutation_id`; globbing
    is intentionally NOT supported here. Operators who want to forbid a
    family of ids should enumerate them (the CLI's ``zicato inspect mutations``
    listing is the obvious source).
    """

    if not forbidden_ids:
        return []
    forbidden_set = set(forbidden_ids)
    errors: list[str] = []
    for patch in patches:
        if patch.mutation_id in forbidden_set:
            errors.append(f"patch {patch.id!r} targets forbidden mutation id {patch.mutation_id!r}")
    return errors


__all__ = [
    "ProposerBrief",
    "load_brief",
    "enforce_forbidden",
]
