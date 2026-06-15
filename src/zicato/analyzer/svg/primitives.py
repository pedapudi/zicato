"""Shared low-level builders for the analysis-report SVG figures.

HTML/attribute escaping, signed/plain number formatting, label
truncation, the inline placeholder SVG used when there is nothing to
plot, and best-effort float coercion. Every figure family draws on
these so the formatting and the empty-state rendering stay identical
across figures.
"""

from __future__ import annotations

import html as _html

from zicato.analyzer.svg.palette import _VAR_GRID


def _esc(text: str) -> str:
    """HTML-escape a string for safe SVG attribute/text inclusion."""
    return _html.escape(text, quote=True)


def _fmt_delta(value: float) -> str:
    """Format a signed delta like ``+0.080`` / ``-0.012``."""
    return f"{value:+.3f}"


def _fmt_num(value: float, places: int = 2) -> str:
    """Format a non-signed numeric value with fixed decimal places."""
    return f"{value:.{places}f}"


def _truncate(text: str, limit: int) -> str:
    """Truncate text with an ellipsis when it exceeds ``limit`` characters."""
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 1)] + "…"


def _empty_svg(width: int, height: int, label: str) -> str:
    """Inline SVG placeholder used when there is nothing to plot."""
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'viewBox="0 0 {width} {height}" role="img" '
        f'aria-label="{_esc(label)}">'
        f'<rect x="0.5" y="0.5" width="{width - 1}" height="{height - 1}" '
        f'fill="none" style="stroke: {_VAR_GRID}" stroke-dasharray="4 3"/>'
        f'<text class="svg-axis" x="{width / 2}" y="{height / 2}" '
        f'text-anchor="middle" dominant-baseline="middle">{_esc(label)}</text>'
        f"</svg>"
    )


def _coerce_float(value: object) -> float | None:
    """Coerce to ``float`` if possible, else ``None`` (preserve absence)."""
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
