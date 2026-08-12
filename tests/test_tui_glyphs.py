"""Glyph microtypography — density that never overstates the data.

Each primitive gets the same three questions: does it say the right thing, does
it stay honest when the data is absent, and does its ASCII form occupy the same
columns as its Unicode form (so a ``NO_COLOR`` / non-UTF-8 terminal reflows
nothing).
"""

from __future__ import annotations

import pytest

from zicato.tui import glyphs
from zicato.tui.glyphs import NULL

# ---------------------------------------------------------------------------
# sparkline
# ---------------------------------------------------------------------------


def test_sparkline_direction_is_not_upside_down() -> None:
    """A rising series must render rising — braille dot bits run top-down.

    Braille numbers dot 1 at the TOP, so the naive bit order draws every trend
    inverted; an inverted chart is still a plausible-looking chart, which is
    why this is pinned rather than eyeballed.
    """
    rising = glyphs.sparkline([1, 2, 3, 4, 5, 6, 7, 8])
    falling = glyphs.sparkline([8, 7, 6, 5, 4, 3, 2, 1])
    ink = [(ord(c) - 0x2800).bit_count() for c in rising]
    assert ink == sorted(ink) and ink[0] < ink[-1]
    assert [(ord(c) - 0x2800).bit_count() for c in falling] == ink[::-1]
    # The fullest cell is where the series peaks, at each end respectively.
    assert rising[-1] == "⣾"
    assert falling[0] == "⣷"


def test_sparkline_packs_two_samples_per_cell() -> None:
    assert len(glyphs.sparkline([1, 2, 3, 4])) == 2
    assert len(glyphs.sparkline([1, 2, 3])) == 2
    assert len(glyphs.sparkline([1, 2, 3, 4], ascii_only=True)) == 4


def test_sparkline_hole_is_blank_never_zero() -> None:
    """A missing sample leaves a gap; it never becomes a floor-level bar."""
    with_hole = glyphs.sparkline([5, None, 5, 9])
    assert with_hole != glyphs.sparkline([5, 0, 5, 9])
    ascii_hole = glyphs.sparkline([5, None, 5, 9], ascii_only=True)
    assert ascii_hole[1] == " "


def test_sparkline_all_null_renders_the_null_glyph() -> None:
    assert glyphs.sparkline([None, None]) == NULL
    assert glyphs.sparkline([]) == NULL
    assert glyphs.sparkline(["x", True]) == NULL


def test_sparkline_constant_series_sits_at_the_midpoint() -> None:
    """No variation renders flat and mid — not a full bar, not an empty one."""
    flat = glyphs.sparkline([5, 5, 5, 5])
    assert len(set(flat)) == 1
    assert flat[0] not in ("⣿", "⠀")


# ---------------------------------------------------------------------------
# whisker
# ---------------------------------------------------------------------------


def test_whisker_overlap_is_visible_on_a_shared_scale() -> None:
    """Two intervals on ONE axis overlap in the same columns they overlap in."""
    scale = (0.0, 10.0)
    a = glyphs.whisker(1.0, 2.0, 3.0, scale=scale, width=11)
    b = glyphs.whisker(2.0, 4.0, 6.0, scale=scale, width=11)
    a_cells = {i for i, c in enumerate(a) if c != " "}
    b_cells = {i for i, c in enumerate(b) if c != " "}
    assert a_cells & b_cells  # they genuinely overlap
    disjoint = glyphs.whisker(8.0, 9.0, 10.0, scale=scale, width=11)
    assert not a_cells & {i for i, c in enumerate(disjoint) if c != " "}


def test_whisker_never_overwrites_a_bound() -> None:
    """The point estimate must not eat a cap — that would understate the CI."""
    drawn = glyphs.whisker(0.0, 0.0, 1.0, scale=(0.0, 1.0), width=9)
    assert drawn[0] == "╟"
    assert drawn[-1] == "╣"


def test_whisker_absent_interval_is_the_null_glyph() -> None:
    assert glyphs.whisker(None, 1.0, 2.0, width=9).strip() == NULL
    assert glyphs.whisker(3.0, 2.0, 1.0, width=9).strip() == NULL


def test_whisker_ascii_is_the_same_width() -> None:
    unicode_form = glyphs.whisker(1.0, 2.0, 3.0, scale=(0.0, 4.0), width=13)
    ascii_form = glyphs.whisker(1.0, 2.0, 3.0, scale=(0.0, 4.0), width=13, ascii_only=True)
    assert len(unicode_form) == len(ascii_form) == 13
    assert ascii_form.strip("| -+") == ""


# ---------------------------------------------------------------------------
# margin bar
# ---------------------------------------------------------------------------


def test_margin_bar_direction_carries_the_sign() -> None:
    positive = glyphs.margin_bar(0.05, scale=0.1, width=8)
    negative = glyphs.margin_bar(-0.05, scale=0.1, width=8)
    half = len(positive) // 2
    assert positive[half:].strip() and not positive[:half].strip()
    assert negative[:half].strip() and not negative[half:].strip()


def test_margin_bar_marks_the_threshold_and_flags_a_clamp() -> None:
    marked = glyphs.margin_bar(0.01, scale=0.1, threshold=0.05, width=8)
    assert "┆" in marked
    clamped = glyphs.margin_bar(0.5, scale=0.1, width=8)
    assert clamped.endswith("›")


def test_margin_bar_null_is_not_an_empty_bar() -> None:
    """An absent margin must not render as "zero margin"."""
    assert NULL in glyphs.margin_bar(None, scale=0.1, width=8)
    assert NULL not in glyphs.margin_bar(0.0, scale=0.1, width=8)


def test_margin_bar_ascii_keeps_the_width() -> None:
    a = glyphs.margin_bar(0.05, scale=0.1, threshold=0.02, width=8)
    b = glyphs.margin_bar(0.05, scale=0.1, threshold=0.02, width=8, ascii_only=True)
    assert len(a) == len(b)


# ---------------------------------------------------------------------------
# lifeline
# ---------------------------------------------------------------------------


def test_lifeline_lights_exactly_one_stage() -> None:
    spans = glyphs.lifeline("tournament")
    accents = [text for text, style in spans if style == "accent"]
    assert accents == ["tournament"]
    earlier = [text for text, style in spans if style == "plain"]
    assert earlier == ["propose", "screen"]


@pytest.mark.parametrize("current", [None, "", "not-a-stage"])
def test_lifeline_claims_no_stage_it_was_not_told(current: str | None) -> None:
    """An unknown stage lights NOTHING rather than guessing the first one."""
    spans = glyphs.lifeline(current)
    assert not [text for text, style in spans if style == "accent"]
    assert [text for text, _ in spans if text in glyphs.STAGES] == list(glyphs.STAGES)


def test_lifeline_ascii_arrow() -> None:
    text = "".join(t for t, _ in glyphs.lifeline("gate", ascii_only=True))
    assert "▸" not in text
    assert " > " in text
