"""Inline-SVG figure generators for the epoch analysis report.

The comprehensive report is a hybrid artifact: the data-bearing sections
are templated deterministically from the workspace
(:mod:`zicato.analyzer.report_sections`); the *interpretive* prose is
written by the auxiliary LLM. The figures live in the same deterministic
half as the tables — every figure here is drawn directly from a
:class:`zicato.analyzer.report_data.EpochReportData` view so the chart
and the table next to it can never disagree.

Figures emitted:

* :func:`render_svg_score_trajectory` — scalar (loss) across generations,
  with promoted vs. rejected styling per point. The y axis is the
  cumulative scalar (lower is better); the x axis is the generation
  index in proposed order.
* :func:`render_svg_drift_movements` — a small-multiples block: one mini
  panel per challenger generation, each carrying paired bars for every
  drift kind moved that round (from-rate vs. to-rate) plus the signed
  delta.
* :func:`render_svg_per_board_heatmap` — rows = board entries, columns
  = generations; cell colour encodes per-entry Δ scalar (red = worse,
  grey = ~flat, green = better). Cached-champion columns are annotated.
* :func:`render_svg_lineage_compact` — boxes-and-edges diagram of the
  lineage (baseline + every proposed generation, decision-coloured).
* :func:`render_svg_mutation_surface` — a compact, single-figure list of
  ``id · kind · file`` rows for the most recent mutation enumeration.

All outputs are self-contained SVG fragments: no external resources, no
external fonts, every visual property inlined as an attribute or in a
scoped ``<style>``. Drop-in for the analysis HTML and the inline
paper-card view alike.
"""

from __future__ import annotations

import html as _html
import math
from collections.abc import Iterable

from zicato.analyzer.report_data import EpochReportData, GenerationView

# Palette mirrored from :mod:`zicato.epoch.html_report` so the analysis
# report and the standalone epoch HTML carry one visual language across
# the dashboard surfaces. Hard-coded (not imported) so the analyzer does
# not depend on the epoch HTML module.
PROMOTED_COLOR = "#2ea043"
REJECTED_COLOR = "#d73a49"
BASELINE_COLOR = "#6e7681"
DEFERRED_COLOR = "#bf8700"
GRID_COLOR = "#d0d7de"
NEUTRAL_COLOR = "#8a8d91"


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
        f'fill="none" stroke="{GRID_COLOR}" stroke-dasharray="4 3"/>'
        f'<text class="svg-axis" x="{width / 2}" y="{height / 2}" '
        f'text-anchor="middle" dominant-baseline="middle">{_esc(label)}</text>'
        f"</svg>"
    )


def _generation_decision_color(g: GenerationView) -> str:
    """Map a generation's decision to its palette colour."""
    if g.is_baseline:
        return BASELINE_COLOR
    if g.decision == "promoted":
        return PROMOTED_COLOR
    if g.decision == "rejected":
        return REJECTED_COLOR
    if g.decision == "deferred":
        return DEFERRED_COLOR
    return NEUTRAL_COLOR


# ---------------------------------------------------------------------------
# Figure: score trajectory
# ---------------------------------------------------------------------------


def render_svg_score_trajectory(
    data: EpochReportData,
    *,
    width: int = 720,
    height: int = 240,
) -> str:
    """Inline SVG of the cumulative scalar across generations.

    The y axis is ``cumulative_scalar`` (lower is better — the scoring
    model expresses loss); the x axis is the generation index in lineage
    order. Promoted points are filled green circles connected by a solid
    line along the promoted spine. Rejected points are hollow red
    squares — they are not connected to the line because they are not
    on the cumulative trajectory the next round would build on. Per-
    point value labels sit above the marker.
    """
    gens = list(data.generations)
    if not gens:
        return _empty_svg(width, height, "No generations to plot.")

    margin_l, margin_r, margin_t, margin_b = 56, 22, 28, 42
    plot_w = width - margin_l - margin_r
    plot_h = height - margin_t - margin_b

    values = [g.cumulative_scalar for g in gens]
    vmin, vmax = min(values), max(values)
    if math.isclose(vmin, vmax):
        pad = max(0.1, abs(vmin) * 0.2 if vmin else 0.1)
        vmin -= pad
        vmax += pad
    else:
        pad = (vmax - vmin) * 0.1
        vmin -= pad
        vmax += pad

    n = len(gens)
    x_step = plot_w / max(1, n - 1) if n > 1 else 0.0

    def to_x(i: int) -> float:
        if n == 1:
            return margin_l + plot_w / 2
        return margin_l + i * x_step

    def to_y(v: float) -> float:
        if vmax == vmin:
            return margin_t + plot_h / 2
        return margin_t + (vmax - v) / (vmax - vmin) * plot_h

    parts: list[str] = []
    parts.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
        f'role="img" aria-label="Score trajectory across generations">'
    )

    # Horizontal grid + tick labels.
    for k in range(5):
        gy = margin_t + plot_h * k / 4
        parts.append(
            f'<line x1="{margin_l}" y1="{gy:.1f}" x2="{margin_l + plot_w}" '
            f'y2="{gy:.1f}" stroke="{GRID_COLOR}" stroke-width="0.5" '
            f'stroke-opacity="0.7"/>'
        )
        tick_val = vmax - (vmax - vmin) * k / 4
        parts.append(
            f'<text class="svg-axis" x="{margin_l - 8}" y="{gy + 3:.1f}" '
            f'text-anchor="end">{tick_val:+.2f}</text>'
        )

    # Emphasised zero line if it crosses the range.
    if vmin < 0 < vmax:
        zy = to_y(0.0)
        parts.append(
            f'<line x1="{margin_l}" y1="{zy:.1f}" x2="{margin_l + plot_w}" '
            f'y2="{zy:.1f}" stroke="{BASELINE_COLOR}" stroke-width="1" '
            f'stroke-dasharray="3 3" stroke-opacity="0.85"/>'
        )

    # x-axis baseline.
    parts.append(
        f'<line x1="{margin_l}" y1="{margin_t + plot_h:.1f}" '
        f'x2="{margin_l + plot_w}" y2="{margin_t + plot_h:.1f}" '
        f'stroke="{GRID_COLOR}" stroke-width="1"/>'
    )

    # Promoted spine — connect baseline + every promoted point in order.
    spine_pts: list[tuple[float, float]] = []
    for i, g in enumerate(gens):
        if g.is_baseline or g.decision == "promoted":
            spine_pts.append((to_x(i), to_y(g.cumulative_scalar)))
    if len(spine_pts) >= 2:
        path = "M " + " L ".join(f"{x:.1f} {y:.1f}" for x, y in spine_pts)
        parts.append(
            f'<path d="{path}" fill="none" stroke="{PROMOTED_COLOR}" ' f'stroke-width="2"/>'
        )

    # Per-point markers + labels.
    for i, g in enumerate(gens):
        cx = to_x(i)
        cy = to_y(g.cumulative_scalar)
        if g.is_baseline:
            parts.append(
                f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="4.5" '
                f'fill="{BASELINE_COLOR}" stroke="{BASELINE_COLOR}" '
                f'stroke-width="1.2"/>'
            )
        elif g.decision == "promoted":
            parts.append(
                f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="5" '
                f'fill="{PROMOTED_COLOR}" stroke="{PROMOTED_COLOR}" '
                f'stroke-width="1.4"/>'
            )
        elif g.decision == "rejected":
            s = 4.5
            parts.append(
                f'<rect x="{cx - s:.1f}" y="{cy - s:.1f}" width="{2 * s}" '
                f'height="{2 * s}" fill="none" stroke="{REJECTED_COLOR}" '
                f'stroke-width="1.6"/>'
            )
        elif g.decision == "deferred":
            parts.append(
                f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="4.5" fill="none" '
                f'stroke="{DEFERRED_COLOR}" stroke-width="1.6" '
                f'stroke-dasharray="2 2"/>'
            )
        else:
            parts.append(
                f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="4" fill="none" '
                f'stroke="{NEUTRAL_COLOR}" stroke-width="1.3"/>'
            )
        # x-axis label — generation id.
        parts.append(
            f'<text class="svg-axis" x="{cx:.1f}" '
            f'y="{margin_t + plot_h + 16:.1f}" text-anchor="middle">'
            f"{_esc(g.generation_id)}</text>"
        )
        # Value label above the marker.
        parts.append(
            f'<text class="svg-axis svg-value" x="{cx:.1f}" '
            f'y="{cy - 9:.1f}" text-anchor="middle">'
            f"{_fmt_delta(g.cumulative_scalar)}</text>"
        )

    # y-axis label.
    parts.append(
        f'<text class="svg-axis svg-axislabel" x="{margin_l - 42}" '
        f'y="{margin_t + plot_h / 2:.1f}" text-anchor="middle" '
        f'transform="rotate(-90 {margin_l - 42} {margin_t + plot_h / 2:.1f})">'
        f"scalar (loss — lower is better)</text>"
    )
    parts.append("</svg>")
    return "".join(parts)


# ---------------------------------------------------------------------------
# Figure: drift-kind movements (small multiples, one panel per generation)
# ---------------------------------------------------------------------------


def _coerce_float(value: object) -> float | None:
    """Coerce to ``float`` if possible, else ``None`` (preserve absence)."""
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _challenger_drift_panels(
    data: EpochReportData,
) -> list[tuple[GenerationView, list[tuple[str, float | None, float | None]]]]:
    """Per-challenger panel rows: ``(generation, [(kind, from, to), ...])``."""
    panels: list[tuple[GenerationView, list[tuple[str, float | None, float | None]]]] = []
    for g in data.generations:
        if g.is_baseline:
            continue
        kinds: list[tuple[str, float | None, float | None]] = []
        for mv in g.drift_movements:
            kind = str(mv.get("kind", "")).strip()
            if not kind:
                continue
            kinds.append(
                (kind, _coerce_float(mv.get("from_rate")), _coerce_float(mv.get("to_rate")))
            )
        if kinds:
            panels.append((g, kinds))
    return panels


def render_svg_drift_movements(
    data: EpochReportData,
    *,
    panel_w: int = 220,
    panel_h: int = 150,
    cols: int = 3,
) -> str:
    """Inline SVG small-multiples of per-generation drift-kind movements.

    Each completed challenger generation that recorded drift-movement
    rows is laid out as one mini panel. Inside a panel, every drift kind
    is one row of two paired bars (from-rate light, to-rate solid) plus
    the signed Δ rate label. Panels are colour-coded by the generation's
    decision (promoted = green border, rejected = red border).

    Returns an inline placeholder SVG when no generation has recorded
    drift movements yet.
    """
    panels = _challenger_drift_panels(data)
    if not panels:
        return _empty_svg(panel_w * cols, panel_h, "No drift movements recorded yet.")

    n = len(panels)
    actual_cols = min(cols, n)
    rows = (n + actual_cols - 1) // actual_cols
    width = panel_w * actual_cols
    height = panel_h * rows

    # Determine a shared x scale across panels (0..max from-or-to rate)
    # so visual magnitudes are comparable round-to-round.
    all_rates: list[float] = []
    for _, kinds in panels:
        for _, fr, to in kinds:
            if fr is not None:
                all_rates.append(fr)
            if to is not None:
                all_rates.append(to)
    rmax = max(all_rates) if all_rates else 1.0
    if rmax <= 0:
        rmax = 1.0
    rmax = max(rmax, 0.1)

    parts: list[str] = []
    parts.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
        f'role="img" aria-label="Drift-kind rate movements per generation">'
    )

    for idx, (g, kinds) in enumerate(panels):
        col = idx % actual_cols
        row = idx // actual_cols
        ox = col * panel_w
        oy = row * panel_h

        border = _generation_decision_color(g)
        parts.append(
            f'<rect x="{ox + 4:.1f}" y="{oy + 4:.1f}" '
            f'width="{panel_w - 8}" height="{panel_h - 8}" '
            f'rx="6" ry="6" fill="none" stroke="{border}" '
            f'stroke-width="1.2" stroke-opacity="0.85"/>'
        )

        # Title row: generation id + decision.
        parts.append(
            f'<text class="svg-label" x="{ox + 12:.1f}" y="{oy + 20:.1f}" '
            f'font-weight="600">{_esc(g.generation_id)} '
            f"· {_esc(g.decision)}</text>"
        )

        # Layout the bars.
        inner_l = ox + 90  # leave room for the kind label
        inner_r = ox + panel_w - 70  # leave room for the Δ value
        bar_w = inner_r - inner_l
        max_bars = min(len(kinds), 4)
        band = (panel_h - 36) / max(1, max_bars)

        for j, (kind, fr, to) in enumerate(kinds[:max_bars]):
            band_y = oy + 30 + j * band
            label_y = band_y + band / 2 + 3
            # Kind label.
            parts.append(
                f'<text class="svg-axis" x="{ox + 12:.1f}" '
                f'y="{label_y:.1f}">{_esc(_truncate(kind, 12))}</text>'
            )
            # Paired bars: from (light) above, to (solid) below.
            half_h = max(3.0, band / 2 - 2)
            if fr is not None:
                w_from = bar_w * min(1.0, fr / rmax)
                parts.append(
                    f'<rect x="{inner_l:.1f}" y="{band_y + 2:.1f}" '
                    f'width="{w_from:.1f}" height="{half_h:.1f}" '
                    f'fill="{GRID_COLOR}" fill-opacity="0.85"/>'
                )
            if to is not None:
                w_to = bar_w * min(1.0, to / rmax)
                fill = border
                parts.append(
                    f'<rect x="{inner_l:.1f}" y="{band_y + 2 + half_h:.1f}" '
                    f'width="{w_to:.1f}" height="{half_h:.1f}" '
                    f'fill="{fill}" fill-opacity="0.85"/>'
                )
            # Δ value.
            delta_txt = ""
            if fr is not None and to is not None:
                delta_txt = _fmt_delta(to - fr)
            elif to is not None:
                delta_txt = f"->{_fmt_num(to)}"
            elif fr is not None:
                delta_txt = f"{_fmt_num(fr)}->"
            parts.append(
                f'<text class="svg-axis svg-value" '
                f'x="{ox + panel_w - 10:.1f}" '
                f'y="{label_y:.1f}" text-anchor="end">{_esc(delta_txt)}</text>'
            )

        # Footer: "from -> to" legend.
        parts.append(
            f'<text class="svg-axis" x="{ox + 12:.1f}" '
            f'y="{oy + panel_h - 10:.1f}" fill-opacity="0.7">'
            f"top: from  ·  bottom: to</text>"
        )

    parts.append("</svg>")
    return "".join(parts)


# ---------------------------------------------------------------------------
# Figure: per-board outcomes heatmap
# ---------------------------------------------------------------------------


def _per_entry_delta(g: GenerationView, entry_id: str) -> float | None:
    """Best-effort extract of the per-entry Δ scalar for one generation.

    The tournament runner caches per-entry results under varying shapes
    across versions; this resolver walks the common ones — ``entries``
    keyed by id, ``per_entry`` list of dicts, ``board`` mapping. Returns
    ``None`` when no per-entry information is recorded so the renderer
    can mark the cell as 'no data' rather than a fake zero.
    """
    score = g.gen_score
    if not score:
        return None
    # Common shape 1: {"entries": {id: {"scalar_delta": x, ...}}}.
    entries = score.get("entries")
    if isinstance(entries, dict) and entry_id in entries:
        ent = entries[entry_id]
        if isinstance(ent, dict):
            for k in ("scalar_delta", "delta", "scalar_score_delta"):
                if k in ent:
                    v = _coerce_float(ent.get(k))
                    if v is not None:
                        return v
    # Common shape 2: {"per_entry": [{"entry_id": id, "delta": x}, ...]}.
    per_entry = score.get("per_entry")
    if isinstance(per_entry, list):
        for row in per_entry:
            if isinstance(row, dict) and str(row.get("entry_id", "")) == entry_id:
                for k in ("scalar_delta", "delta", "scalar_score_delta"):
                    if k in row:
                        v = _coerce_float(row.get(k))
                        if v is not None:
                            return v
    return None


def _heatmap_color(value: float, vmax: float) -> str:
    """Map a signed delta to a red/grey/green cell colour.

    ``vmax`` is the symmetric magnitude used to normalise; a delta of
    ``+vmax`` saturates to red (worse), ``-vmax`` to green (better),
    zero is neutral grey. Lower scalar is better, hence the sign mapping.
    """
    if vmax <= 0:
        return GRID_COLOR
    t = max(-1.0, min(1.0, value / vmax))
    if abs(t) < 0.04:
        return "#dde2e7"  # near-zero grey
    if t > 0:
        # red — worse
        a = 0.25 + 0.55 * t
        return f"rgba(215, 58, 73, {a:.3f})"
    # green — better
    a = 0.25 + 0.55 * (-t)
    return f"rgba(46, 160, 67, {a:.3f})"


def render_svg_per_board_heatmap(
    data: EpochReportData,
    *,
    cell_w: int = 64,
    cell_h: int = 26,
) -> str:
    """Inline SVG heatmap of per-board-entry Δ scalar across generations.

    Rows are board entries (in board order), columns are challenger
    generations. Each cell colours the per-entry Δ scalar (challenger −
    champion) on a red (worse) / grey (flat) / green (better) gradient.
    Cells with no per-entry data render as a hatched placeholder so the
    figure surfaces the gap rather than a fake value. Columns where the
    tournament reused a cached champion get a "cached" header
    annotation.
    """
    entries = list(data.board_entries)
    challengers = [g for g in data.generations if not g.is_baseline]
    if not entries or not challengers:
        return _empty_svg(
            max(420, cell_w * 6),
            max(120, cell_h * 3),
            "No per-board outcomes recorded yet.",
        )

    label_w = 130
    header_h = 40
    width = label_w + cell_w * len(challengers) + 18
    height = header_h + cell_h * len(entries) + 16

    # Collect all per-entry deltas to pick a symmetric vmax.
    values: list[float] = []
    cell_grid: dict[tuple[int, int], float | None] = {}
    for ci, g in enumerate(challengers):
        for ri, e in enumerate(entries):
            v = _per_entry_delta(g, e.id)
            cell_grid[(ri, ci)] = v
            if v is not None:
                values.append(v)
    if values:
        vmax = max(abs(v) for v in values)
    else:
        vmax = 0.0

    parts: list[str] = []
    parts.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
        f'role="img" aria-label="Per-board entry outcomes heatmap">'
    )

    # Defs: stripe pattern for "no data" cells.
    parts.append(
        '<defs><pattern id="nodata-stripes" patternUnits="userSpaceOnUse" '
        'width="6" height="6">'
        '<rect width="6" height="6" fill="#eef0f3"/>'
        f'<path d="M -1 7 L 7 -1" stroke="{GRID_COLOR}" stroke-width="1"/>'
        "</pattern></defs>"
    )

    # Column headers — generation ids; mark cached-champion rounds.
    for ci, g in enumerate(challengers):
        x = label_w + ci * cell_w + cell_w / 2
        parts.append(
            f'<text class="svg-label" x="{x:.1f}" y="{header_h - 22:.1f}" '
            f'text-anchor="middle" font-weight="600">'
            f"{_esc(g.generation_id)}</text>"
        )
        # Cache annotation — gen_score may carry a `champion_cached` flag
        # or a `cached` flag set by the fast-mode runner.
        cached = bool(
            g.gen_score.get("champion_cached")
            or g.gen_score.get("cached")
            or g.gen_score.get("champion_reused")
        )
        if cached:
            parts.append(
                f'<text class="svg-axis" x="{x:.1f}" y="{header_h - 8:.1f}" '
                f'text-anchor="middle" fill-opacity="0.7">cached</text>'
            )

    # Row labels.
    for ri, e in enumerate(entries):
        y = header_h + ri * cell_h + cell_h / 2 + 4
        parts.append(
            f'<text class="svg-axis" x="{label_w - 8:.1f}" y="{y:.1f}" '
            f'text-anchor="end">{_esc(_truncate(e.id, 18))}</text>'
        )

    # Cells.
    for ri in range(len(entries)):
        for ci in range(len(challengers)):
            x = label_w + ci * cell_w + 2
            y = header_h + ri * cell_h + 2
            w = cell_w - 4
            h = cell_h - 4
            v = cell_grid[(ri, ci)]
            if v is None:
                parts.append(
                    f'<rect x="{x:.1f}" y="{y:.1f}" width="{w}" '
                    f'height="{h}" fill="url(#nodata-stripes)" '
                    f'stroke="{GRID_COLOR}" stroke-width="0.5"/>'
                )
                continue
            colour = _heatmap_color(v, vmax)
            parts.append(
                f'<rect x="{x:.1f}" y="{y:.1f}" width="{w}" '
                f'height="{h}" fill="{colour}" stroke="{GRID_COLOR}" '
                f'stroke-width="0.5"/>'
            )
            parts.append(
                f'<text class="svg-axis svg-value" '
                f'x="{x + w / 2:.1f}" y="{y + h / 2 + 4:.1f}" '
                f'text-anchor="middle">{_fmt_delta(v)}</text>'
            )

    # Legend (bottom).
    legend_y = height - 6
    parts.append(
        f'<text class="svg-axis" x="{label_w:.1f}" y="{legend_y:.1f}" '
        f'fill-opacity="0.7">'
        f"red = worse  ·  grey = flat  ·  green = better"
        f"</text>"
    )
    parts.append("</svg>")
    return "".join(parts)


# ---------------------------------------------------------------------------
# Figure: compact lineage
# ---------------------------------------------------------------------------


def render_svg_lineage_compact(
    data: EpochReportData,
    *,
    width: int = 720,
    height: int = 160,
) -> str:
    """A compact boxes-and-edges lineage figure.

    Generations are laid out left-to-right in lineage order. Promoted
    children sit on the centerline; rejected / deferred children dip
    below. Decisions are encoded by box stroke and fill — promoted
    green, rejected red dashed, deferred amber dotted, baseline /
    pending neutral grey.
    """
    gens = list(data.generations)
    if not gens:
        return _empty_svg(width, height, "No generations recorded yet.")

    margin_x, margin_y = 28, 28
    node_w, node_h = 84, 36
    usable_w = width - 2 * margin_x
    usable_h = height - 2 * margin_y
    n = len(gens)
    x_step = (usable_w - node_w) / (n - 1) if n > 1 else 0.0
    center_y = margin_y + usable_h / 2 - node_h / 2
    branch_offset = min(usable_h / 3, 52)

    positions: dict[str, tuple[float, float]] = {}
    for i, g in enumerate(gens):
        x = margin_x + i * x_step
        if g.is_baseline or g.decision == "promoted":
            y = center_y
        else:
            y = center_y + branch_offset
        positions[g.generation_id] = (x, y)

    parts: list[str] = []
    parts.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
        f'role="img" aria-label="Lineage diagram">'
    )

    # Edges.
    for g in gens:
        if g.is_baseline or g.parent_generation_id not in positions:
            continue
        px, py = positions[g.parent_generation_id]
        cx, cy = positions[g.generation_id]
        x1 = px + node_w
        y1 = py + node_h / 2
        x2 = cx
        y2 = cy + node_h / 2
        mid_x1 = x1 + (x2 - x1) * 0.45
        mid_x2 = x1 + (x2 - x1) * 0.55
        if g.decision == "promoted":
            stroke, sw, dash = PROMOTED_COLOR, 2.0, ""
        elif g.decision == "rejected":
            stroke, sw, dash = REJECTED_COLOR, 1.3, ' stroke-dasharray="5 4"'
        elif g.decision == "deferred":
            stroke, sw, dash = DEFERRED_COLOR, 1.4, ' stroke-dasharray="2 3"'
        else:
            stroke, sw, dash = BASELINE_COLOR, 1.2, ' stroke-dasharray="3 4"'
        parts.append(
            f'<path d="M {x1:.1f} {y1:.1f} C {mid_x1:.1f} {y1:.1f}, '
            f'{mid_x2:.1f} {y2:.1f}, {x2:.1f} {y2:.1f}" '
            f'fill="none" stroke="{stroke}" stroke-width="{sw}"{dash}/>'
        )

    # Nodes.
    for g in gens:
        x, y = positions[g.generation_id]
        colour = _generation_decision_color(g)
        if g.decision == "rejected":
            dash_attr = ' stroke-dasharray="5 4"'
        elif g.decision == "deferred":
            dash_attr = ' stroke-dasharray="2 3"'
        else:
            dash_attr = ""
        fill_attr = "rgba(255,255,255,0.0)"
        if g.is_baseline:
            fill_attr = "rgba(110, 118, 129, 0.10)"
        elif g.decision == "promoted":
            fill_attr = "rgba(46, 160, 67, 0.14)"
        elif g.decision == "rejected":
            fill_attr = "rgba(215, 58, 73, 0.10)"
        elif g.decision == "deferred":
            fill_attr = "rgba(191, 135, 0, 0.12)"
        parts.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{node_w}" '
            f'height="{node_h}" rx="6" ry="6" fill="{fill_attr}" '
            f'stroke="{colour}" stroke-width="1.4"{dash_attr}/>'
        )
        parts.append(
            f'<text class="svg-label" x="{x + node_w / 2:.1f}" '
            f'y="{y + 15:.1f}" text-anchor="middle" font-weight="600">'
            f"{_esc(g.generation_id)}</text>"
        )
        tag = "baseline" if g.is_baseline else g.decision
        parts.append(
            f'<text class="svg-axis" x="{x + node_w / 2:.1f}" '
            f'y="{y + 28:.1f}" text-anchor="middle">{_esc(tag)}</text>'
        )

    parts.append("</svg>")
    return "".join(parts)


# ---------------------------------------------------------------------------
# Figure: mutation surface compact table-as-figure
# ---------------------------------------------------------------------------


def render_svg_mutation_surface(
    data: EpochReportData,
    *,
    width: int = 720,
    row_h: int = 22,
    max_rows: int = 14,
) -> str:
    """Compact figure listing the most recent mutation enumeration.

    Each row carries ``id  ·  kind  ·  file:lines`` — the canonical
    summary the proposer sees. The figure is capped at ``max_rows`` to
    keep the inline figure tight; an overflow row reports the remainder.
    Drawn as inline SVG so it remains self-contained and themable.
    """
    surface = list(data.mutation_surface)
    if not surface:
        return _empty_svg(width, row_h * 2, "No mutation surface recorded.")

    visible = surface[:max_rows]
    overflow = len(surface) - len(visible)
    rows = len(visible) + (1 if overflow > 0 else 0)
    header_h = 22
    height = header_h + row_h * rows + 8

    col_id = 14
    col_kind = 170
    col_file = 320

    parts: list[str] = []
    parts.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
        f'role="img" aria-label="Mutation surface">'
    )
    parts.append(
        f'<line x1="0" y1="{header_h - 4}" x2="{width}" '
        f'y2="{header_h - 4}" stroke="{GRID_COLOR}" stroke-width="0.8"/>'
    )
    parts.append(
        f'<text class="svg-label" x="{col_id}" y="{header_h - 8}" '
        f'font-weight="600">id</text>'
        f'<text class="svg-label" x="{col_kind}" y="{header_h - 8}" '
        f'font-weight="600">kind</text>'
        f'<text class="svg-label" x="{col_file}" y="{header_h - 8}" '
        f'font-weight="600">file</text>'
    )
    for i, m in enumerate(visible):
        y = header_h + (i + 1) * row_h - 6
        mid = _truncate(str(m.get("id", "")), 22)
        kind = _truncate(str(m.get("kind", "")), 18)
        mfile = str(m.get("file", ""))
        # Append :lines if present (start/end).
        line_start = m.get("line_start") or m.get("lineno")
        line_end = m.get("line_end")
        if line_start and line_end:
            mfile = f"{mfile}:{line_start}-{line_end}"
        elif line_start:
            mfile = f"{mfile}:{line_start}"
        mfile = _truncate(mfile, 50)
        parts.append(
            f'<text class="svg-axis svg-mono" x="{col_id}" y="{y}">'
            f"{_esc(mid)}</text>"
            f'<text class="svg-axis" x="{col_kind}" y="{y}">'
            f"{_esc(kind)}</text>"
            f'<text class="svg-axis svg-mono" x="{col_file}" y="{y}">'
            f"{_esc(mfile)}</text>"
        )
    if overflow > 0:
        y = header_h + (len(visible) + 1) * row_h - 6
        parts.append(
            f'<text class="svg-axis" x="{col_id}" y="{y}" '
            f'fill-opacity="0.7">+{overflow} more mutation point'
            f"{'' if overflow == 1 else 's'} not shown</text>"
        )

    parts.append("</svg>")
    return "".join(parts)


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


#: Marker -> renderer lookup used by the markdown renderer to substitute
#: figures at their anchor points. The marker text is the deterministic
#: section's HTML comment (``<!-- FIGURE:NAME -->``) — see
#: :mod:`zicato.analyzer.report_sections`.
FIGURE_RENDERERS: dict[str, str] = {
    "score-trajectory": "render_svg_score_trajectory",
    "drift-movements": "render_svg_drift_movements",
    "per-board-heatmap": "render_svg_per_board_heatmap",
    "lineage": "render_svg_lineage_compact",
    "mutation-surface": "render_svg_mutation_surface",
}


def render_figure(name: str, data: EpochReportData) -> str:
    """Render one figure by its marker name. Returns ``""`` on unknown name."""
    if name == "score-trajectory":
        return render_svg_score_trajectory(data)
    if name == "drift-movements":
        return render_svg_drift_movements(data)
    if name == "per-board-heatmap":
        return render_svg_per_board_heatmap(data)
    if name == "lineage":
        return render_svg_lineage_compact(data)
    if name == "mutation-surface":
        return render_svg_mutation_surface(data)
    return ""


def iter_figure_names() -> Iterable[str]:
    """Yield the canonical figure marker names in canonical order."""
    return tuple(FIGURE_RENDERERS.keys())


__all__ = [
    "PROMOTED_COLOR",
    "REJECTED_COLOR",
    "BASELINE_COLOR",
    "DEFERRED_COLOR",
    "GRID_COLOR",
    "FIGURE_RENDERERS",
    "render_svg_score_trajectory",
    "render_svg_drift_movements",
    "render_svg_per_board_heatmap",
    "render_svg_lineage_compact",
    "render_svg_mutation_surface",
    "render_figure",
    "iter_figure_names",
]
