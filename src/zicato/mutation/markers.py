"""Marker-comment parsing primitives.

The annotation surface uses comment lines of the form::

    # zicato:mutable id="researcher_instructions"
    # zicato:mutable id="refine_prompt" required_placeholders="{drift_kind},{plan_summary}"
    # zicato:mutable:file id="researcher_prompts"
    # zicato:mutable:code id="write_slug_logic"
    ...code region...
    # zicato:mutable:end

This module exposes the minimal regex + tail-parser used by the enumerator.
It is intentionally I/O-free so unit tests can exercise edge cases in
isolation.

Marker variants
---------------

* ``# zicato:mutable id="..."`` — span marker; binds to the nearest
  string literal beneath it. The historical default.
* ``# zicato:mutable:file id="..."`` — file marker; declares the whole
  file mutable as one unit.
* ``# zicato:mutable:code id="..."`` — code marker; opens a pointed
  code region that runs until the matching ``# zicato:mutable:end``
  sentinel. The region body is the verbatim source lines BETWEEN
  (exclusive of) the opening and closing marker lines — this is how a
  control-flow block (e.g. the slugify / path-resolution logic inside a
  tool function) is exposed as mutable surface without exposing the
  whole file and without wrapping the body in a string literal.
* ``# zicato:mutable:end`` — closes the most recently-opened ``:code``
  region. It carries no id and no metadata; use :func:`is_end_marker`
  to detect it (``parse_marker_line`` returns ``None`` for it).

The syntax table
----------------

The marker *token* (``zicato:mutable...``) is the same everywhere; only
the host language's comment lead-in differs. That lead-in — and the file
suffixes it applies to — is DATA rather than code: :class:`MarkerSyntax` pairs a
suffix with the comment leaders that open a marker on it and the block
closers tolerated at end of line, and the table of those entries decides
what is enumerable at all. Every parsing function takes the entry to
parse under, which callers resolve from the file's suffix with
:func:`marker_syntax_for`.

:data:`BUILTIN_SYNTAXES` carries the proven set — ``.py`` under the
historical ``#``-only grammar, and the six prompt/config suffixes under
the leaders those formats use. An operator DECLARES further suffixes in
the contract's ``mutation_surface`` table (MUTATION-SURFACE.md §2.5);
:func:`syntax_table_from_config` folds them over the built-ins and
:func:`install_syntax_table` makes the result this process's active
table. Widening the surface is then a contract edit that rolls the
epoch rather than a zicato release.

``.py`` is RESERVED: the table governs the text pass only, so the Python
surface keeps a grammar no config can reach, and its behaviour stays a
property of the built-in entry rather than something a test has to keep
rediscovering.

The declared syntax is load-bearing for CONTAINMENT rather than just for
discovery: the applier strips echoed marker lines out of a region body
under the file's own leaders (see
:func:`zicato.mutation.applier._reindent_code_region`), so a file type
whose comment syntax zicato cannot parse is one where a proposer could
smuggle a live ``:end`` marker into a region. Declared syntax is
enforceable containment.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

#: Span-level marker comment prefix.
MARKER_SPAN_PREFIX = "# zicato:mutable"

#: File-level marker comment prefix.
MARKER_FILE_PREFIX = "# zicato:mutable:file"

#: Code-region opening marker comment prefix.
MARKER_CODE_PREFIX = "# zicato:mutable:code"

#: Code-region closing sentinel.
MARKER_END_PREFIX = "# zicato:mutable:end"

#: Operator-grading sentinel. A module carrying ``# zicato:grading`` anywhere
#: declares itself OPERATOR-OWNED grading code — predicates, judges, or the
#: scoring ``scalar_fn`` / ``drift_reducer`` plugins (issue #19). The
#: enumerator skips the WHOLE file: "the proposer does not get to rewrite the
#: operator's grading." Scoring plugins / predicates / judges are never
#: enumerated as mutation points, exactly like the documented contract.
MARKER_GRADING_PREFIX = "# zicato:grading"

#: The suffix parsed under the historical Python grammar. Reserved: an
#: operator table entry for it is rejected.
PYTHON_SUFFIX = ".py"


@dataclass(frozen=True, slots=True)
class MarkerSyntax:
    """One suffix's comment grammar — the unit of the syntax table.

    Fields
    ------
    suffix:
        The file suffix this entry governs, including the leading dot.
    leaders:
        Comment lead-ins that may open a marker line. Matched
        longest-first, so a short leader never shadows a longer one that
        starts with it (``//`` vs ``/``).
    trailers:
        Block-comment closers tolerated at end of a marker line, so a
        ``<!-- zicato:mutable:end -->`` or ``/* zicato:mutable:end */``
        parses. On an OPENING marker a closer simply lands in the
        metadata tail and yields no ``key="value"`` pair, so only the
        id-less ``:end`` sentinel — which is anchored — needs them.
    """

    suffix: str
    leaders: tuple[str, ...]
    trailers: tuple[str, ...] = ()

    @property
    def is_python(self) -> bool:
        """``True`` for the reserved ``.py`` entry — the AST-bound pass."""

        return self.suffix == PYTHON_SUFFIX


#: The reserved ``.py`` entry: the historical ``#``-only grammar, and the
#: default the parsing functions fall back to.
PYTHON_SYNTAX = MarkerSyntax(suffix=PYTHON_SUFFIX, leaders=("#",))

#: The proven set, expressed as the table it now is: prompts and config,
#: the two shapes this surface exists for. Every entry hosts a comment,
#: which is what excludes strict JSON and CSV — a marker cannot be written
#: in them without invalidating the document. An operator extends this
#: through the contract's ``mutation_surface`` table, never by editing it.
#:
#: The text entries carry ``#`` and ``<!--`` as one UNION rather than
#: narrowed per suffix, because that union is what the text pass has
#: always accepted — and over-accepting a leader is safe for containment
#: (it strips more) where under-accepting is not.
BUILTIN_SYNTAXES: Mapping[str, MarkerSyntax] = {
    PYTHON_SUFFIX: PYTHON_SYNTAX,
    **{
        suffix: MarkerSyntax(suffix=suffix, leaders=("<!--", "#"), trailers=("-->",))
        for suffix in (".md", ".markdown", ".txt", ".yaml", ".yml", ".toml")
    },
}

_TAIL_KV_RE = re.compile(r'([A-Za-z_][A-Za-z0-9_]*)="([^"]*)"')


def _alternation(tokens: tuple[str, ...]) -> str:
    """Regex alternation over ``tokens``, longest-first."""

    return "(?:" + "|".join(re.escape(t) for t in sorted(tokens, key=len, reverse=True)) + ")"


@dataclass(frozen=True, slots=True)
class _Patterns:
    marker: re.Pattern[str]
    end: re.Pattern[str]
    grading: re.Pattern[str]


@cache
def _patterns(syntax: MarkerSyntax) -> _Patterns:
    """Compile ``syntax``'s three marker patterns.

    Cached on the (frozen) entry, so the per-file resolution in the
    enumerator's walk costs one dict lookup.

    With ``leaders=("#",)`` and no trailers this reproduces the historical
    Python patterns exactly — the ``.py`` byte-identity pin holds because
    the built-in entry is that grammar rather than because a branch preserves it.
    """

    leader = _alternation(syntax.leaders)
    closer = rf"(?:\s*{_alternation(syntax.trailers)})?" if syntax.trailers else ""
    return _Patterns(
        marker=re.compile(
            rf"""^\s*{leader}\s*zicato:mutable(?P<variant>:file|:code)?"""
            r"""\s+id="(?P<id>[^"]+)"(?:\s+(?P<tail>.+))?\s*$"""
        ),
        end=re.compile(rf"""^\s*{leader}\s*zicato:mutable:end{closer}\s*$"""),
        grading=re.compile(rf"""^\s*{leader}\s*zicato:grading\b.*$"""),
    )


def syntax_table_from_config(raw: Mapping[str, Any] | None) -> dict[str, MarkerSyntax]:
    """Fold the operator's declared ``mutation_surface`` over the built-ins.

    ``raw`` is the contract's table as it is spelled in ``scoring.json``:
    ``{".ts": {"leaders": ["//", "/*"], "trailers": ["*/"]}}``. An entry
    for a built-in suffix OVERRIDES it (that is how an operator narrows a
    format's leaders), except for ``.py``, which is reserved.

    Raises ``ValueError`` on a malformed entry — a surface declaration that
    does not parse must fail at contract load rather than silently enumerate less
    than the operator declared.
    """

    table = dict(BUILTIN_SYNTAXES)
    if not raw:
        return table
    for suffix, entry in raw.items():
        if not isinstance(suffix, str) or not suffix.startswith(".") or len(suffix) < 2:
            raise ValueError(
                f"mutation_surface: {suffix!r} is not a file suffix; "
                'entries are keyed by suffix including the dot (e.g. ".ts")'
            )
        if suffix == PYTHON_SUFFIX:
            raise ValueError(
                "mutation_surface: .py is reserved — the table governs the text "
                "pass only, so the Python grammar cannot be redeclared"
            )
        if not isinstance(entry, Mapping):
            raise ValueError(
                f"mutation_surface[{suffix!r}] must be a table of "
                '"leaders" / "trailers", got '
                f"{type(entry).__name__}"
            )
        leaders = _tokens(entry.get("leaders"), suffix, "leaders")
        if not leaders:
            raise ValueError(
                f"mutation_surface[{suffix!r}]: at least one comment leader is "
                "required — zicato must know the comment syntax to strip echoed "
                "marker lines out of a region body"
            )
        table[suffix] = MarkerSyntax(
            suffix=suffix,
            leaders=leaders,
            trailers=_tokens(entry.get("trailers"), suffix, "trailers"),
        )
    return table


def _tokens(raw: Any, suffix: str, key: str) -> tuple[str, ...]:
    """Coerce a ``leaders`` / ``trailers`` value to a tuple of tokens."""

    if raw is None:
        return ()
    if isinstance(raw, str) or not isinstance(raw, list | tuple):
        raise ValueError(f"mutation_surface[{suffix!r}].{key} must be a list of strings")
    tokens = tuple(str(token).strip() for token in raw)
    if any(not token for token in tokens):
        raise ValueError(f"mutation_surface[{suffix!r}].{key} carries an empty token")
    return tokens


#: The process's active table. Written only by :func:`install_syntax_table`
#: — the run path installs the epoch's declared table once, at contract
#: load, so every enumeration and every apply inside that process agrees on
#: what is surface. Unset ⇒ the built-ins alone.
_ACTIVE_TABLE: dict[str, MarkerSyntax] = dict(BUILTIN_SYNTAXES)


def install_syntax_table(raw: Mapping[str, Any] | None) -> tuple[str, ...]:
    """Install the operator-declared table for this process; return its suffixes.

    Enumeration re-runs deep inside the apply loop from call sites that
    hold nothing but paths, and propose-time and apply-time enumeration
    MUST see the same surface, so the declared table is process-level
    rather than an argument threaded through every walk. Installed once
    per invocation from the contract (:func:`zicato.workspace_loader.
    activate_mutation_surface`); passing ``None`` restores the built-ins.
    """

    table = syntax_table_from_config(raw)
    _ACTIVE_TABLE.clear()
    _ACTIVE_TABLE.update(table)
    return tuple(sorted(set(table) - set(BUILTIN_SYNTAXES)))


def active_syntax_table() -> Mapping[str, MarkerSyntax]:
    """Return the table this process enumerates under."""

    return _ACTIVE_TABLE


@contextmanager
def swap_syntax_table(raw: Mapping[str, Any] | None) -> Iterator[tuple[str, ...]]:
    """Install ``raw`` for the block, then restore the previous table.

    The sanctioned entry for tests and tooling that need a declared table
    for a bounded scope. :func:`install_syntax_table` is the RUN path's
    one-way install — a caller that uses it without restoring leaves every
    later enumeration in the process running under a table it never asked
    for, which is an order-dependent failure rather than an obvious one.
    Restoring on the way out (including on exception) is what keeps that
    from being a footgun, so reach for this rather than the module state.
    """

    previous = dict(_ACTIVE_TABLE)
    try:
        yield install_syntax_table(raw)
    finally:
        _ACTIVE_TABLE.clear()
        _ACTIVE_TABLE.update(previous)


@dataclass(frozen=True, slots=True)
class ParsedMarker:
    """A successfully-parsed opening marker comment line.

    Fields
    ------
    id:
        The mutation id this marker introduces.
    is_file:
        ``True`` iff this is a file-level marker (``# zicato:mutable:file``).
    is_code:
        ``True`` iff this is a code-region opening marker
        (``# zicato:mutable:code``). At most one of ``is_file`` /
        ``is_code`` is ``True``; both ``False`` means the historical
        span marker.
    metadata:
        Additional ``key="value"`` pairs found on the marker line. Returned
        as plain ``dict[str, str]`` so the enumerator can copy it straight
        into :attr:`MutationPoint.metadata`.
    """

    id: str
    is_file: bool
    is_code: bool
    metadata: dict[str, str]


def marker_syntax_for(
    path: Path, *, table: Mapping[str, MarkerSyntax] | None = None
) -> MarkerSyntax | None:
    """Return the syntax ``path`` is parsed under, or ``None`` if it is not surface.

    ``table`` defaults to this process's active table
    (:func:`active_syntax_table`). Callers that hold a
    :class:`~zicato.core.types.MutationPoint` should route through this
    rather than testing the suffix themselves, so the mapping lives in
    exactly one place.
    """

    return (active_syntax_table() if table is None else table).get(Path(path).suffix)


def is_end_marker(line: str, *, syntax: MarkerSyntax = PYTHON_SYNTAX) -> bool:
    """Return ``True`` iff ``line`` is a ``zicato:mutable:end`` sentinel.

    Any of ``syntax``'s leaders opens the sentinel and any of its trailers
    may close the line, so ``<!-- zicato:mutable:end -->`` closes a
    markdown region and ``/* zicato:mutable:end */`` a TypeScript one.
    """

    return _patterns(syntax).end.match(line) is not None


def is_grading_marker(line: str, *, syntax: MarkerSyntax = PYTHON_SYNTAX) -> bool:
    """Return ``True`` iff ``line`` is a ``# zicato:grading`` sentinel.

    A file carrying this marker is operator-owned grading code (predicates,
    judges, or scoring ``scalar_fn`` / ``drift_reducer`` plugins) and is skipped
    wholesale by the enumerator — the proposer never mutates the operator's
    grading. The guard is syntax-agnostic: a YAML scoring config or a
    markdown rubric can declare itself operator-owned exactly like a
    predicates module can.
    """

    return _patterns(syntax).grading.match(line) is not None


def parse_marker_line(line: str, *, syntax: MarkerSyntax = PYTHON_SYNTAX) -> ParsedMarker | None:
    """Parse one source line under ``syntax``.

    Returns ``None`` when ``line`` is not a recognised *opening* marker.
    The ``# zicato:mutable:end`` sentinel is NOT an opening marker and
    returns ``None`` here — detect it with :func:`is_end_marker`. Returns
    a :class:`ParsedMarker` when the line matches an opening marker; the
    ``tail`` portion is split on ``key="value"`` pairs and stored under
    :attr:`ParsedMarker.metadata`.

    A trailing block-comment closer needs no special handling: it lands in
    ``tail``, where it contributes no ``key="value"`` pair and is dropped.
    """

    match = _patterns(syntax).marker.match(line)
    if match is None:
        return None
    tail = match.group("tail") or ""
    metadata: dict[str, str] = {}
    for kv in _TAIL_KV_RE.finditer(tail):
        metadata[kv.group(1)] = kv.group(2)
    variant = match.group("variant")
    return ParsedMarker(
        id=match.group("id"),
        is_file=variant == ":file",
        is_code=variant == ":code",
        metadata=metadata,
    )


__all__ = [
    "BUILTIN_SYNTAXES",
    "MARKER_CODE_PREFIX",
    "MARKER_END_PREFIX",
    "MARKER_FILE_PREFIX",
    "MARKER_GRADING_PREFIX",
    "MARKER_SPAN_PREFIX",
    "PYTHON_SUFFIX",
    "PYTHON_SYNTAX",
    "MarkerSyntax",
    "ParsedMarker",
    "active_syntax_table",
    "install_syntax_table",
    "is_end_marker",
    "is_grading_marker",
    "marker_syntax_for",
    "parse_marker_line",
    "swap_syntax_table",
    "syntax_table_from_config",
]
