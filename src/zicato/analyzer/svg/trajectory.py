"""Figure: score trajectory — cumulative scalar across generations."""

from __future__ import annotations

import math

from zicato.analyzer.report_data import EpochReportData
from zicato.analyzer.svg.palette import (
    _VAR_BASELINE,
    _VAR_DEFERRED,
    _VAR_GRID,
    _VAR_NEUTRAL,
    _VAR_PROMOTED,
    _VAR_REJECTED,
)
from zicato.analyzer.svg.primitives import _empty_svg, _esc, _fmt_delta


def render_svg_score_trajectory(
    data: EpochReportData,
    *,
    width: int = 720,
    height: int = 260,
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

    margin_l, margin_r, margin_t, margin_b = 56, 22, 36, 58
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
            f'y2="{gy:.1f}" style="stroke: {_VAR_GRID}" stroke-width="0.5" '
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
            f'y2="{zy:.1f}" style="stroke: {_VAR_BASELINE}" stroke-width="1" '
            f'stroke-dasharray="3 3" stroke-opacity="0.85"/>'
        )

    # x-axis baseline.
    parts.append(
        f'<line x1="{margin_l}" y1="{margin_t + plot_h:.1f}" '
        f'x2="{margin_l + plot_w}" y2="{margin_t + plot_h:.1f}" '
        f'style="stroke: {_VAR_GRID}" stroke-width="1"/>'
    )

    # Promoted spine — connect baseline + every promoted point in order.
    spine_pts: list[tuple[float, float]] = []
    for i, g in enumerate(gens):
        if g.is_baseline or g.decision == "promoted":
            spine_pts.append((to_x(i), to_y(g.cumulative_scalar)))
    if len(spine_pts) >= 2:
        path = "M " + " L ".join(f"{x:.1f} {y:.1f}" for x, y in spine_pts)
        parts.append(
            f'<path d="{path}" fill="none" style="stroke: {_VAR_PROMOTED}" ' f'stroke-width="2"/>'
        )

    # Per-point markers + labels.
    for i, g in enumerate(gens):
        cx = to_x(i)
        cy = to_y(g.cumulative_scalar)
        if g.is_baseline:
            parts.append(
                f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="4.5" '
                f'style="fill: {_VAR_BASELINE}; stroke: {_VAR_BASELINE}" '
                f'stroke-width="1.2"/>'
            )
        elif g.decision == "promoted":
            parts.append(
                f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="5" '
                f'style="fill: {_VAR_PROMOTED}; stroke: {_VAR_PROMOTED}" '
                f'stroke-width="1.4"/>'
            )
        elif g.decision == "rejected":
            s = 4.5
            parts.append(
                f'<rect x="{cx - s:.1f}" y="{cy - s:.1f}" width="{2 * s}" '
                f'height="{2 * s}" fill="none" '
                f'style="stroke: {_VAR_REJECTED}" stroke-width="1.6"/>'
            )
        elif g.decision == "deferred":
            parts.append(
                f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="4.5" fill="none" '
                f'style="stroke: {_VAR_DEFERRED}" stroke-width="1.6" '
                f'stroke-dasharray="2 2"/>'
            )
        else:
            parts.append(
                f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="4" fill="none" '
                f'style="stroke: {_VAR_NEUTRAL}" stroke-width="1.3"/>'
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
        f'<text class="svg-axislabel" x="{margin_l - 42}" '
        f'y="{margin_t + plot_h / 2:.1f}" text-anchor="middle" '
        f'transform="rotate(-90 {margin_l - 42} {margin_t + plot_h / 2:.1f})">'
        f"scalar (loss — lower is better)</text>"
    )
    # x-axis label.
    parts.append(
        f'<text class="svg-axislabel" x="{margin_l + plot_w / 2:.1f}" '
        f'y="{height - 4:.1f}" text-anchor="middle">'
        f"generation</text>"
    )
    # Legend strip at top-right: promoted ⏺  rejected ▢  baseline ⏺.
    legend_y = margin_t - 12
    legend_x = margin_l + plot_w - 200
    parts.append(
        f'<circle cx="{legend_x:.1f}" cy="{legend_y:.1f}" r="3.5" '
        f'style="fill: {_VAR_PROMOTED}"/>'
        f'<text class="svg-legend" x="{legend_x + 7:.1f}" '
        f'y="{legend_y + 3.5:.1f}">promoted</text>'
        f'<rect x="{legend_x + 64:.1f}" y="{legend_y - 3.5:.1f}" '
        f'width="7" height="7" fill="none" '
        f'style="stroke: {_VAR_REJECTED}" stroke-width="1.4"/>'
        f'<text class="svg-legend" x="{legend_x + 75:.1f}" '
        f'y="{legend_y + 3.5:.1f}">rejected</text>'
        f'<circle cx="{legend_x + 130:.1f}" cy="{legend_y:.1f}" r="3" '
        f'style="fill: {_VAR_BASELINE}"/>'
        f'<text class="svg-legend" x="{legend_x + 137:.1f}" '
        f'y="{legend_y + 3.5:.1f}">baseline</text>'
    )
    parts.append("</svg>")
    return "".join(parts)
