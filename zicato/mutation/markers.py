"""Marker-comment parsing primitives.

The annotation surface uses comment lines of the form::

    # zicato:mutable id="researcher_instructions"
    # zicato:mutable id="refine_prompt" required_placeholders="{drift_kind},{plan_summary}"
    # zicato:mutable:file id="researcher_prompts"

This module exposes the minimal regex + tail-parser used by the enumerator.
It is intentionally I/O-free so unit tests can exercise edge cases in
isolation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: Span-level marker comment prefix.
MARKER_SPAN_PREFIX = "# zicato:mutable"

#: File-level marker comment prefix.
MARKER_FILE_PREFIX = "# zicato:mutable:file"


_MARKER_RE = re.compile(
    r"""^\s*\#\s*zicato:mutable(?P<file_suffix>:file)?\s+id="(?P<id>[^"]+)"(?:\s+(?P<tail>.+))?\s*$"""
)
_TAIL_KV_RE = re.compile(r'([A-Za-z_][A-Za-z0-9_]*)="([^"]*)"')


@dataclass(frozen=True, slots=True)
class ParsedMarker:
    """A successfully-parsed marker comment line.

    Fields
    ------
    id:
        The mutation id this marker introduces.
    is_file:
        ``True`` iff this is a file-level marker (``# zicato:mutable:file``);
        ``False`` for the span-level form.
    metadata:
        Additional ``key="value"`` pairs found on the marker line. Returned
        as plain ``dict[str, str]`` so the enumerator can copy it straight
        into :attr:`MutationPoint.metadata`.
    """

    id: str
    is_file: bool
    metadata: dict[str, str]


def parse_marker_line(line: str) -> ParsedMarker | None:
    """Parse one source line.

    Returns ``None`` when ``line`` is not a recognised marker. Returns a
    :class:`ParsedMarker` when the line matches; the ``tail`` portion is
    split on ``key="value"`` pairs and stored under
    :attr:`ParsedMarker.metadata`.
    """

    match = _MARKER_RE.match(line)
    if match is None:
        return None
    tail = match.group("tail") or ""
    metadata: dict[str, str] = {}
    for kv in _TAIL_KV_RE.finditer(tail):
        metadata[kv.group(1)] = kv.group(2)
    return ParsedMarker(
        id=match.group("id"),
        is_file=match.group("file_suffix") is not None,
        metadata=metadata,
    )


__all__ = [
    "MARKER_FILE_PREFIX",
    "MARKER_SPAN_PREFIX",
    "ParsedMarker",
    "parse_marker_line",
]
