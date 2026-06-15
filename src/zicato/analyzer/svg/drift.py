"""Figure: drift-kind movements (small multiples, one panel per generation)."""

from __future__ import annotations

from zicato.analyzer.report_data import EpochReportData, GenerationView
from zicato.analyzer.svg.palette import _VAR_GRID, _generation_decision_var
from zicato.analyzer.svg.primitives import (
    _coerce_float,
    _empty_svg,
    _esc,
    _fmt_delta,
    _fmt_num,
    _truncate,
)


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

        border_var = _generation_decision_var(g)
        parts.append(
            f'<rect x="{ox + 4:.1f}" y="{oy + 4:.1f}" '
            f'width="{panel_w - 8}" height="{panel_h - 8}" '
            f'rx="6" ry="6" fill="none" '
            f'style="stroke: {border_var}" '
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
                    f'style="fill: {_VAR_GRID}" fill-opacity="0.85"/>'
                )
            if to is not None:
                w_to = bar_w * min(1.0, to / rmax)
                parts.append(
                    f'<rect x="{inner_l:.1f}" y="{band_y + 2 + half_h:.1f}" '
                    f'width="{w_to:.1f}" height="{half_h:.1f}" '
                    f'style="fill: {border_var}" fill-opacity="0.85"/>'
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
            f'<text class="svg-legend" x="{ox + 12:.1f}" '
            f'y="{oy + panel_h - 10:.1f}">'
            f"top: from-rate  ·  bottom: to-rate</text>"
        )

    parts.append("</svg>")
    return "".join(parts)
