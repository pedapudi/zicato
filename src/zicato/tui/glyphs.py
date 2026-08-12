"""Glyph microtypography — density without cramping.

The terminal's answer to the dashboard's SVG figures. Four primitives, each
with a Unicode form and an ASCII fallback of the SAME character width, so a
``NO_COLOR``/ASCII terminal reflows nothing:

* :func:`sparkline` — braille 2x4 trend line (2 samples per cell: the densest
  honest trend a terminal renders).
* :func:`whisker` — an inline confidence interval ``╟──┼──╢`` so the OVERLAP
  between two intervals is visible rather than inferred.
* :func:`lifeline` — the live round's stage strip, current stage lit.

Honesty rules shared by all four:

* A null (``None`` / non-finite) sample is a HOLE, never a zero. Sparkline
  holes render as blank cells; an all-null series renders as the null glyph.
* Nothing is drawn at a precision the data does not carry: a single sample has
  no trend, so it renders as one cell, not a flat line across the field.
* Every primitive returns a plain ``str`` of exactly the requested width (or
  shorter when the data is shorter) — the caller owns colour.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

#: The null render. Never ``0`` — zero is a legal measurement.
NULL = "—"  # em dash

_BRAILLE_BASE = 0x2800
#: Braille dot bits by (column, height), BOTTOM-UP. Column 0 is the left dot
#: column, column 1 the right; index 0 is the bottom row, so a bar grows
#: upwards from the cell's baseline. (Braille's own dot numbering runs
#: top-down — dot 1 is the TOP-left — so these tuples are that order reversed.
#: Getting it backwards renders every trend upside down, which reads as a
#: plausible chart and is the reason this note exists.)
_DOTS = ((0x40, 0x04, 0x02, 0x01), (0x80, 0x20, 0x10, 0x08))

#: ASCII sparkline ramp, one char per sample (5 levels, matching braille's
#: 0-4 dot heights).
_ASCII_RAMP = "_.-=#"


def _finite(value: object) -> float | None:
    """Return ``value`` as a float when it is a finite real number, else None."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    f = float(value)
    return f if math.isfinite(f) else None


def _levels(values: Sequence[object], steps: int) -> tuple[list[int | None], bool]:
    """Quantise ``values`` onto ``0..steps`` levels; None for holes.

    Returns ``(levels, any_data)``. A constant series sits at the midpoint —
    a flat line is the honest render of "no variation", not a full bar.
    """
    nums = [_finite(v) for v in values]
    present = [v for v in nums if v is not None]
    if not present:
        return [None] * len(nums), False
    lo, hi = min(present), max(present)
    if hi == lo:
        mid = steps // 2
        return [None if v is None else mid for v in nums], True
    span = hi - lo
    return (
        [None if v is None else round((v - lo) / span * steps) for v in nums],
        True,
    )


def sparkline(values: Sequence[object], *, ascii_only: bool = False) -> str:
    """Render ``values`` as a trend line.

    Unicode: braille cells packing TWO samples each, every sample a 0-4 dot
    column measured from the cell's baseline. ASCII: one ramp character per
    sample. Both scale to the series' own min/max — a sparkline reports SHAPE,
    never level, and carries no axis, so it must never be read as absolute.

    Holes (``None``, ``NaN``, non-numeric) render blank: the trend is drawn
    around the gap rather than through a fabricated zero.
    """
    if not values:
        return NULL
    if ascii_only:
        levels, any_data = _levels(values, len(_ASCII_RAMP) - 1)
        if not any_data:
            return NULL
        return "".join(" " if lv is None else _ASCII_RAMP[lv] for lv in levels)

    levels, any_data = _levels(values, 4)
    if not any_data:
        return NULL
    out: list[str] = []
    for i in range(0, len(levels), 2):
        pair = levels[i : i + 2]
        bits = 0
        for col, level in enumerate(pair):
            if level is None:
                continue
            # level 0 still draws the baseline dot: the sample EXISTS and a
            # blank cell already means "missing".
            for row in range(max(level, 1)):
                bits |= _DOTS[col][row]
        out.append(chr(_BRAILLE_BASE + bits))
    return "".join(out)


def whisker(
    lo: object,
    mid: object,
    hi: object,
    *,
    scale: tuple[float, float] | None = None,
    width: int = 9,
    ascii_only: bool = False,
) -> str:
    """Render a confidence interval as an inline whisker.

    ``╟──┼──╢`` (ASCII ``|--+--|``): the two caps are the interval bounds, the
    cross is the point estimate. When ``scale`` is given the whisker is drawn
    on that shared axis, which is the whole point — two rows drawn on one
    scale make interval OVERLAP visible. Without a scale the interval fills
    the field, which shows width but not position; callers comparing rows must
    pass a scale.

    Returns :data:`NULL` padded to ``width`` when the interval is unavailable.
    """
    caps = ("╟", "╣") if not ascii_only else ("|", "|")
    dash = "─" if not ascii_only else "-"
    cross = "┼" if not ascii_only else "+"
    n_lo, n_mid, n_hi = _finite(lo), _finite(mid), _finite(hi)
    if width < 3 or n_lo is None or n_hi is None or n_hi < n_lo:
        return NULL.ljust(max(width, 1))

    if scale is None:
        left, right = 0, width - 1
    else:
        s_lo, s_hi = scale
        if not (math.isfinite(s_lo) and math.isfinite(s_hi)) or s_hi <= s_lo:
            return NULL.ljust(width)
        span = s_hi - s_lo

        def pos(v: float) -> int:
            return min(width - 1, max(0, round((v - s_lo) / span * (width - 1))))

        left, right = pos(n_lo), pos(n_hi)
        if right < left:
            left, right = right, left

    cells = [" "] * width
    for i in range(left, right + 1):
        cells[i] = dash
    cells[left] = caps[0]
    cells[right] = caps[1]
    if n_mid is not None:
        if scale is None:
            # No shared axis: place the estimate proportionally inside the bar.
            frac = 0.5 if n_hi == n_lo else (n_mid - n_lo) / (n_hi - n_lo)
            at = left + round(min(1.0, max(0.0, frac)) * (right - left))
        else:
            s_lo, s_hi = scale
            at = min(width - 1, max(0, round((n_mid - s_lo) / (s_hi - s_lo) * (width - 1))))
        # The estimate never overwrites a cap: losing a bound would understate
        # the interval, and understated uncertainty is the one lie forbidden.
        if left < at < right:
            cells[at] = cross
    return "".join(cells)


#: The round's stages, in order. Mirrors the browser's pipeline stepper.
STAGES: tuple[str, ...] = ("propose", "screen", "tournament", "gate")


def lifeline(
    current: str | None,
    *,
    stages: Sequence[str] = STAGES,
    ascii_only: bool = False,
) -> list[tuple[str, str]]:
    """Return the round lifeline as ``(text, style)`` spans.

    ``propose ▸ screen ▸ tournament ▸ gate`` with the current stage lit, every
    earlier stage in normal weight and every later stage dim. An unknown or
    absent ``current`` lights NOTHING — the strip still shows the shape of a
    round without claiming a stage the service did not report.
    """
    arrow = " > " if ascii_only else " ▸ "
    names = list(stages)
    idx = names.index(current) if current in names else -1
    spans: list[tuple[str, str]] = []
    for i, name in enumerate(names):
        if i:
            spans.append((arrow, "faint"))
        if idx < 0:
            spans.append((name, "faint"))
        elif i < idx:
            spans.append((name, "plain"))
        elif i == idx:
            spans.append((name, "accent"))
        else:
            spans.append((name, "faint"))
    return spans


__all__ = ["NULL", "STAGES", "lifeline", "sparkline", "whisker"]
