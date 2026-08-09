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

Two comment syntaxes
--------------------

The marker *token* (``zicato:mutable...``) is the same everywhere; only
the host language's comment lead-in differs. Every parsing function
therefore takes a ``syntax`` argument:

* ``"python"`` (the default, and the ONLY syntax used for ``*.py``) —
  the lead-in must be ``#``. This is the historical grammar, unchanged
  to the character.
* ``"text"`` — the lead-in may be any of :data:`TEXT_COMMENT_LEADERS`,
  and the line may carry a trailing ``-->`` closer, so a marker can live
  inside a markdown ``<!-- ... -->`` comment as well as a YAML/TOML ``#``
  one.

The split is deliberate rather than "one permissive regex everywhere":
``.py`` enumeration is the load-bearing legacy surface, and gating it to
the historical ``#``-only pattern makes byte-identical behaviour a
property of the grammar instead of something a test has to keep
discovering. Callers pick the syntax from the file's suffix with
:func:`marker_syntax_for`.

Adding a leader for another comment style is one entry in
:data:`TEXT_COMMENT_LEADERS`; the set stays at what the supported file
types actually use, so no leader is carried speculatively.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

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
#: scoring ``scalar_fn`` / ``drift_reducer`` plugins (issue #19 phase 3). The
#: enumerator skips the WHOLE file: "the proposer does not get to rewrite the
#: operator's grading." Scoring plugins / predicates / judges are never
#: enumerated as mutation points, exactly like the documented contract.
MARKER_GRADING_PREFIX = "# zicato:grading"


#: Which comment grammar a line is parsed under. ``"python"`` is the
#: historical ``#``-only form; ``"text"`` widens the lead-in to every
#: leader in :data:`TEXT_COMMENT_LEADERS`.
MarkerSyntax = Literal["python", "text"]

#: Comment lead-ins recognised under the ``"text"`` syntax — one per
#: comment style the supported file types use: ``#`` for YAML / TOML /
#: plain text, ``<!--`` for markdown. Ordered longest-first in the
#: compiled alternation.
TEXT_COMMENT_LEADERS: tuple[str, ...] = ("<!--", "#")

#: Trailing block-comment closer tolerated at end of a marker line, so a
#: markdown ``<!-- zicato:mutable:end -->`` parses. On an OPENING marker
#: the closer simply lands in the metadata tail and yields no
#: ``key="value"`` pairs, so only the id-less ``:end`` sentinel — which is
#: anchored — needs it spelled out.
_TEXT_CLOSER = r"(?:\s*-->)?"

_LEADER_PY = r"\#"
_LEADER_TEXT = "(?:" + "|".join(re.escape(lead) for lead in TEXT_COMMENT_LEADERS) + ")"


def _marker_re(leader: str) -> re.Pattern[str]:
    return re.compile(
        rf"""^\s*{leader}\s*zicato:mutable(?P<variant>:file|:code)?"""
        r"""\s+id="(?P<id>[^"]+)"(?:\s+(?P<tail>.+))?\s*$"""
    )


# The ``"python"`` patterns are the historical ones, character-for-character.
_MARKER_RE = _marker_re(_LEADER_PY)
_END_RE = re.compile(r"""^\s*\#\s*zicato:mutable:end\s*$""")
_GRADING_RE = re.compile(r"""^\s*\#\s*zicato:grading\b.*$""")

_MARKER_RE_TEXT = _marker_re(_LEADER_TEXT)
_END_RE_TEXT = re.compile(rf"""^\s*{_LEADER_TEXT}\s*zicato:mutable:end{_TEXT_CLOSER}\s*$""")
_GRADING_RE_TEXT = re.compile(rf"""^\s*{_LEADER_TEXT}\s*zicato:grading\b.*$""")

_TAIL_KV_RE = re.compile(r'([A-Za-z_][A-Za-z0-9_]*)="([^"]*)"')

#: Files with this suffix are parsed under the ``"python"`` syntax; every
#: other suffix uses ``"text"``.
_PYTHON_SUFFIX = ".py"


def marker_syntax_for(path: Path) -> MarkerSyntax:
    """Return the marker syntax to parse ``path`` under.

    ``.py`` resolves to ``"python"`` (the historical ``#``-only grammar);
    everything else to ``"text"``. Callers that hold a
    :class:`~zicato.core.types.MutationPoint` should route through this
    rather than testing the suffix themselves, so the mapping lives in
    exactly one place.
    """

    return "python" if Path(path).suffix == _PYTHON_SUFFIX else "text"


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


def is_end_marker(line: str, *, syntax: MarkerSyntax = "python") -> bool:
    """Return ``True`` iff ``line`` is a ``zicato:mutable:end`` sentinel.

    Under ``syntax="text"`` any leader in :data:`TEXT_COMMENT_LEADERS` is
    accepted and a trailing ``-->`` / ``*/`` closer is tolerated, so
    ``<!-- zicato:mutable:end -->`` closes a markdown region.
    """

    pattern = _END_RE if syntax == "python" else _END_RE_TEXT
    return pattern.match(line) is not None


def is_grading_marker(line: str, *, syntax: MarkerSyntax = "python") -> bool:
    """Return ``True`` iff ``line`` is a ``# zicato:grading`` sentinel.

    A file carrying this marker is operator-owned grading code (predicates,
    judges, or scoring ``scalar_fn`` / ``drift_reducer`` plugins) and is skipped
    wholesale by the enumerator — the proposer never mutates the operator's
    grading. The guard is syntax-agnostic: a YAML scoring config or a
    markdown rubric can declare itself operator-owned exactly like a
    predicates module can.
    """

    pattern = _GRADING_RE if syntax == "python" else _GRADING_RE_TEXT
    return pattern.match(line) is not None


def parse_marker_line(line: str, *, syntax: MarkerSyntax = "python") -> ParsedMarker | None:
    """Parse one source line.

    Returns ``None`` when ``line`` is not a recognised *opening* marker.
    The ``# zicato:mutable:end`` sentinel is NOT an opening marker and
    returns ``None`` here — detect it with :func:`is_end_marker`. Returns
    a :class:`ParsedMarker` when the line matches an opening marker; the
    ``tail`` portion is split on ``key="value"`` pairs and stored under
    :attr:`ParsedMarker.metadata`.

    Under ``syntax="text"`` the lead-in may be any of
    :data:`TEXT_COMMENT_LEADERS`. A trailing block-comment closer needs no
    special handling: it lands in ``tail``, where it contributes no
    ``key="value"`` pair and is dropped.
    """

    pattern = _MARKER_RE if syntax == "python" else _MARKER_RE_TEXT
    match = pattern.match(line)
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
    "MARKER_CODE_PREFIX",
    "MARKER_END_PREFIX",
    "MARKER_FILE_PREFIX",
    "MARKER_GRADING_PREFIX",
    "MARKER_SPAN_PREFIX",
    "TEXT_COMMENT_LEADERS",
    "MarkerSyntax",
    "ParsedMarker",
    "is_end_marker",
    "is_grading_marker",
    "marker_syntax_for",
    "parse_marker_line",
]
