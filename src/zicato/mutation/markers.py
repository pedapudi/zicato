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
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: Span-level marker comment prefix.
MARKER_SPAN_PREFIX = "# zicato:mutable"

#: File-level marker comment prefix.
MARKER_FILE_PREFIX = "# zicato:mutable:file"

#: Code-region opening marker comment prefix.
MARKER_CODE_PREFIX = "# zicato:mutable:code"

#: Code-region closing sentinel.
MARKER_END_PREFIX = "# zicato:mutable:end"


_MARKER_RE = re.compile(
    r"""^\s*\#\s*zicato:mutable(?P<variant>:file|:code)?\s+id="(?P<id>[^"]+)"(?:\s+(?P<tail>.+))?\s*$"""
)
_END_RE = re.compile(r"""^\s*\#\s*zicato:mutable:end\s*$""")
_TAIL_KV_RE = re.compile(r'([A-Za-z_][A-Za-z0-9_]*)="([^"]*)"')


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


def is_end_marker(line: str) -> bool:
    """Return ``True`` iff ``line`` is a ``# zicato:mutable:end`` sentinel."""

    return _END_RE.match(line) is not None


def parse_marker_line(line: str) -> ParsedMarker | None:
    """Parse one source line.

    Returns ``None`` when ``line`` is not a recognised *opening* marker.
    The ``# zicato:mutable:end`` sentinel is NOT an opening marker and
    returns ``None`` here — detect it with :func:`is_end_marker`. Returns
    a :class:`ParsedMarker` when the line matches an opening marker; the
    ``tail`` portion is split on ``key="value"`` pairs and stored under
    :attr:`ParsedMarker.metadata`.
    """

    match = _MARKER_RE.match(line)
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
    "MARKER_SPAN_PREFIX",
    "ParsedMarker",
    "is_end_marker",
    "parse_marker_line",
]
