"""The lens render model — a pure, printable description of a screen.

A lens is a function from a served payload to a :class:`View`. It touches no
terminal, no widget and no clock, which is what makes the whole surface
testable as text: a golden snapshot, an ASCII snapshot and a narrow-terminal
snapshot are three calls to :func:`render_text` on the same value.

The model is deliberately small:

* :class:`Span` — a run of text plus a SEMANTIC style token. Style tokens name
  meaning (``accent``, ``good``, ``bad``, ``warn``, ``faint``), never a colour;
  the Textual layer maps tokens to colours and ``NO_COLOR`` maps them to weight
  alone. Colour is always redundant encoding.
* :class:`Row` — one line, its stable ``key``, and the five-slot ``evidence``
  the drawer shows when the row is under the cursor.
* :class:`Block` — a titled group of rows. No box drawing: whitespace and
  typographic hierarchy do the separating, the way the browser Console does it
  with type rather than borders.
* :class:`View` — the screen, plus the ``digest`` that gates repainting.

The digest is the whole repaint discipline in one field: it folds every value
the view RENDERS and nothing else (no timestamps, no sequence numbers), so a
no-op SSE heartbeat produces an identical digest and therefore zero cell
patches. See :mod:`zicato.tui.app`.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from zicato.tui.present import NULL

#: Semantic style tokens. A renderer may map these to colour, weight, or
#: nothing at all — the text must read correctly with all three.
STYLES = ("plain", "bold", "faint", "accent", "good", "bad", "warn")


@dataclass(frozen=True)
class Span:
    """A run of text carrying a semantic style token."""

    text: str
    style: str = "plain"


@dataclass(frozen=True)
class Row:
    """One rendered line.

    ``key`` is stable across refreshes so the cursor stays on the same logical
    row when the data underneath it changes. ``evidence`` is the five-slot
    hovercard equivalent: ``(label, value)`` pairs shown in the drawer.
    ``action`` is a route or command the row drills into on ``enter``.
    """

    key: str
    spans: tuple[Span, ...] = ()
    evidence: tuple[tuple[str, str], ...] = ()
    action: str | None = None
    indent: int = 0
    selectable: bool = False

    @property
    def text(self) -> str:
        return " " * (self.indent * 2) + "".join(s.text for s in self.spans)


@dataclass(frozen=True)
class Block:
    """A titled group of rows. The title may be ``None`` for a bare group."""

    title: str | None = None
    rows: tuple[Row, ...] = ()
    note: str | None = None


@dataclass(frozen=True)
class View:
    """A whole lens screen."""

    title: str
    blocks: tuple[Block, ...] = ()
    digest: str = ""
    subtitle: str | None = None
    #: Set when the lens could not render for a reason the OPERATOR must see —
    #: a degraded payload, an absent index, a service that is not answering.
    #: The lens still returns a View; it never raises at the UI.
    degraded: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    def rows(self) -> list[Row]:
        return [row for block in self.blocks for row in block.rows]

    def selectable_rows(self) -> list[Row]:
        return [row for row in self.rows() if row.selectable]

    def lines(self) -> list[tuple[str, Row]]:
        """EVERY printed line, including block titles, notes and spacers.

        The one enumeration both renderers walk: the text snapshot and the
        Textual widget reconciliation. Keeping them on one function is what
        stops the two from disagreeing about whether a heading exists — a bug
        that shows up as "the golden has a Gate heading and the app does not".
        Synthesised lines take ``@``-prefixed keys, which no lens can collide
        with because a lens key is its own row's name.
        """
        out: list[tuple[str, Row]] = []
        if self.subtitle:
            out.append(("@subtitle", Row(key="@subtitle", spans=(Span(self.subtitle, "faint"),))))
        if self.degraded:
            out.append(
                ("@degraded", Row(key="@degraded", spans=(Span(f"! {self.degraded}", "warn"),)))
            )
        for i, block in enumerate(self.blocks):
            out.append((f"{i}:@gap", Row(key="@gap")))
            if block.title:
                out.append((f"{i}:@title", Row(key="@title", spans=(Span(block.title, "bold"),))))
            out.extend((f"{i}:{row.key}", row) for row in block.rows)
            if block.note:
                out.append((f"{i}:@note", Row(key="@note", spans=(Span(block.note, "faint"),))))
        return out


def row(
    key: str,
    *parts: str | Span | tuple[str, str] | None,
    evidence: Sequence[tuple[str, str]] = (),
    action: str | None = None,
    indent: int = 0,
    selectable: bool = False,
) -> Row:
    """Build a :class:`Row` from loose parts.

    A part is a plain string (``plain`` style), a ``(text, style)`` pair, a
    :class:`Span`, or ``None`` (dropped) — so a conditional cell reads as
    ``x if cond else None`` at the call site rather than a list-append dance.
    """
    spans: list[Span] = []
    for part in parts:
        if part is None:
            continue
        if isinstance(part, Span):
            spans.append(part)
        elif isinstance(part, tuple):
            spans.append(Span(part[0], part[1]))
        else:
            spans.append(Span(part))
    return Row(
        key=key,
        spans=tuple(spans),
        evidence=tuple(evidence),
        action=action,
        indent=indent,
        selectable=selectable,
    )


def digest_of(*parts: Any) -> str:
    """Fold rendered values into a short, stable content digest.

    JSON with sorted keys, hashed. Only pass values the view DISPLAYS: folding
    a timestamp or a sequence number in would make every heartbeat look like a
    change and reintroduce the flicker this discipline exists to prevent.
    """
    blob = json.dumps(parts, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


#: The gutter between columns. Whitespace is the only separator this surface
#: uses — no rules, no box drawing.
GAP = "  "


def columns(rows_of_cells: Iterable[Sequence[str]]) -> list[int]:
    """CONTENT widths that align a table by its data, not by guess.

    The inter-column gutter is NOT included: :func:`pad` and :func:`rpad` add
    it. Folding it into the width instead would put the gutter on the wrong
    side of a right-aligned number, which is how a numeric column ends up
    touching the one after it.
    """
    widths: list[int] = []
    for cells in rows_of_cells:
        for i, cell in enumerate(cells):
            w = _display_width(cell)
            if i >= len(widths):
                widths.append(w)
            elif w > widths[i]:
                widths[i] = w
    return widths


def _display_width(text: str) -> int:
    """Character count — every glyph this surface emits is single-width.

    The braille, block and box-drawing glyphs in :mod:`zicato.tui.glyphs` are
    all narrow, and the em-dash null is narrow too, so ``len`` is exact. A
    lens that renders operator-supplied text (a note, a rationale) truncates
    it via ``present.truncate`` before it reaches a column.
    """
    return len(text)


def pad(text: str, width: int) -> str:
    """Left-align ``text`` in ``width`` columns, plus the gutter."""
    return text + " " * max(0, width - _display_width(text)) + GAP


def rpad(text: str, width: int) -> str:
    """Right-align ``text`` in ``width`` columns, plus the gutter.

    The numeric register: digits line up on their last significant place, which
    is the only way a column of measurements can be compared by eye.
    """
    return " " * max(0, width - _display_width(text)) + text + GAP


def render_text(view: View, *, width: int = 100, ascii_only: bool = False) -> str:
    """Render a :class:`View` to plain text — the snapshot-test surface.

    Styles are dropped: what remains must still be completely readable, which
    is the ``NO_COLOR`` contract stated as a test. ``ascii_only`` additionally
    transliterates the few non-ASCII characters the model itself owns (the
    null glyph); glyph primitives are asked for their ASCII form by the lens.
    """
    out = [view.title, *(row.text for _, row in view.lines())]
    # Clip EVERY line, not just table rows: a terminal clips prose too, and a
    # snapshot that let notes run past the edge would hide exactly the overflow
    # the narrow-mode test exists to catch.
    # Clip FIRST, then strip: clipping a padded cell can leave the trailing
    # space that stripping was supposed to remove.
    text = "\n".join(line[:width].rstrip() for line in out)
    if ascii_only:
        text = to_ascii(text)
    return text


#: The transliteration table for an ASCII-only terminal. Every entry is
#: SAME-WIDTH — one display column in, one out — so an ASCII render reflows
#: nothing and every aligned column stays aligned. The combining circumflex
#: maps to the empty string precisely because it occupies no column of its own.
_ASCII_MAP = {
    NULL: "-",
    "·": "*",
    "±": "+",
    "…": ".",
    "▸": ">",
    "✓": "y",
    "✕": "x",
    "✂": "x",
    "⟳": "@",
    "→": ">",
    "↑": "^",
    "↓": "v",
    "Δ": "D",
    "θ": "t",
    "̂": "",  # combining circumflex (θ̂) — zero-width
    "≤": "<",
    "≥": ">",
    "~": "~",
    "┆": ":",
    "─": "-",
    "╟": "|",
    "╣": "|",
    "┼": "+",
    "›": ">",
    "░": ":",
    "▒": "=",
    "▓": "#",
    "█": "#",
}


def to_ascii(text: str) -> str:
    """Transliterate the model's own non-ASCII glyphs, same width for same width."""
    for src, dst in _ASCII_MAP.items():
        text = text.replace(src, dst)
    return "".join(ch if ord(ch) < 128 else "?" for ch in text)


__all__ = [
    "GAP",
    "STYLES",
    "Block",
    "Row",
    "Span",
    "View",
    "columns",
    "digest_of",
    "pad",
    "render_text",
    "row",
    "rpad",
    "to_ascii",
]
