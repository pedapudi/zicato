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
#
# Figures emit these as CSS-variable references in a ``style=""``
# attribute on each SVG element so a host palette (the dashboard's
# dark ``.analysis-paper-card``, say) can flip the figure rendering
# without re-rendering the SVG. The bare hex constants are retained
# for tests and for callers that want the canonical decision colour
# directly.
PROMOTED_COLOR = "#2ea043"
REJECTED_COLOR = "#d73a49"
BASELINE_COLOR = "#6e7681"
DEFERRED_COLOR = "#bf8700"
GRID_COLOR = "#d0d7de"
NEUTRAL_COLOR = "#8a8d91"

# CSS-variable references that match the palette tokens declared by
# :mod:`zicato.analyzer.report` (``--paper-promoted`` &c). Emitting
# ``var(--paper-promoted)`` in the SVG ``style=""`` attribute means a
# downstream host can override the token and the figure re-tints.
_VAR_PROMOTED = "var(--paper-promoted)"
_VAR_REJECTED = "var(--paper-rejected)"
_VAR_BASELINE = "var(--paper-baseline)"
_VAR_DEFERRED = "var(--paper-deferred)"
_VAR_INCOMPLETE = "var(--paper-incomplete, var(--paper-deferred))"
_VAR_NEUTRAL = "var(--paper-neutral)"
_VAR_PREDICTED = "var(--paper-predicted, var(--paper-baseline))"
_VAR_GRID = "var(--paper-figure-grid)"
_VAR_STRIPE_BG = "var(--paper-figure-stripe-bg)"
_VAR_NEAR_ZERO = "var(--paper-figure-near-zero, #dde2e7)"


def _decision_var(decision: str, *, is_baseline: bool = False) -> str:
    """Return the CSS-variable reference for a decision colour."""
    if is_baseline:
        return _VAR_BASELINE
    if decision == "promoted":
        return _VAR_PROMOTED
    if decision == "rejected":
        return _VAR_REJECTED
    if decision == "deferred":
        return _VAR_DEFERRED
    return _VAR_NEUTRAL


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


def _generation_decision_var(g: GenerationView) -> str:
    """Map a generation's decision to its CSS-variable reference.

    The figures emit this in a ``style=""`` attribute so the host
    palette (light paper-tone standalone, or dark dashboard inline)
    controls the actual rendered colour.
    """
    return _decision_var(g.decision, is_baseline=g.is_baseline)


def _generation_decision_color(g: GenerationView) -> str:
    """Map a generation's decision to its palette hex colour.

    Retained for tests / callers that need a literal hex; figures
    themselves emit :func:`_generation_decision_var` so the host
    palette can re-tint them.
    """
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

    For the on-disk shape the orchestrator currently writes
    (``{"per_entry": {entry_id: {"drift_loss": x, ...}}}``) this only
    yields a delta if the cached object explicitly carries one of the
    delta keys; otherwise the per-entry-against-parent resolver
    (:func:`_per_entry_delta_against`) must be used so a drift_loss
    absolute value can be diffed against the parent champion.
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
    # Common shape 2a: {"per_entry": [{"entry_id": id, "delta": x}, ...]}.
    per_entry = score.get("per_entry")
    if isinstance(per_entry, list):
        for row in per_entry:
            if isinstance(row, dict) and str(row.get("entry_id", "")) == entry_id:
                for k in ("scalar_delta", "delta", "scalar_score_delta"):
                    if k in row:
                        v = _coerce_float(row.get(k))
                        if v is not None:
                            return v
    # Common shape 2b: {"per_entry": {entry_id: {"scalar_delta": x, ...}}}.
    if isinstance(per_entry, dict) and entry_id in per_entry:
        ent = per_entry[entry_id]
        if isinstance(ent, dict):
            for k in ("scalar_delta", "delta", "scalar_score_delta"):
                if k in ent:
                    v = _coerce_float(ent.get(k))
                    if v is not None:
                        return v
    return None


def _per_entry_drift_loss(g: GenerationView, entry_id: str) -> float | None:
    """Best-effort extract of the per-entry absolute ``drift_loss``.

    The current orchestrator caches per-entry results as a dict keyed
    by entry id, where each value carries the absolute ``drift_loss``
    (not a delta against the round's champion). This resolver pulls
    that absolute number so the heatmap renderer can subtract the
    parent champion's value and form a delta — the cell semantics the
    figure encodes (challenger − champion, red = worse, green = better).

    Returns ``None`` when no per-entry drift_loss is recorded so the
    renderer can hatch the cell rather than fake a zero.
    """
    score = g.gen_score
    if not score:
        return None
    # Most common on-disk shape: per_entry as a dict of {id: {...}}.
    per_entry = score.get("per_entry")
    if isinstance(per_entry, dict) and entry_id in per_entry:
        ent = per_entry[entry_id]
        if isinstance(ent, dict):
            for k in ("drift_loss", "loss", "loss_mean"):
                if k in ent:
                    v = _coerce_float(ent.get(k))
                    if v is not None:
                        return v
    # List shape: a row keyed by entry_id carries the absolute number.
    if isinstance(per_entry, list):
        for row in per_entry:
            if isinstance(row, dict) and str(row.get("entry_id", "")) == entry_id:
                for k in ("drift_loss", "loss", "loss_mean"):
                    if k in row:
                        v = _coerce_float(row.get(k))
                        if v is not None:
                            return v
    # `entries` shape: same idea under a different top-level key.
    entries = score.get("entries")
    if isinstance(entries, dict) and entry_id in entries:
        ent = entries[entry_id]
        if isinstance(ent, dict):
            for k in ("drift_loss", "loss", "loss_mean"):
                if k in ent:
                    v = _coerce_float(ent.get(k))
                    if v is not None:
                        return v
    return None


def _per_entry_delta_against(
    g: GenerationView,
    parent: GenerationView | None,
    entry_id: str,
) -> float | None:
    """Resolve one challenger × board-entry cell value for the heatmap.

    First tries the cached delta (``_per_entry_delta``); when that
    yields ``None``, falls back to ``challenger.drift_loss −
    parent.drift_loss`` so the cell still surfaces a movement on
    workspaces whose cached ``gen_score.json`` only carries absolute
    per-entry losses. Returns ``None`` only when neither the cached
    delta nor a both-sides drift_loss reading is available — the
    renderer hatches that cell as 'no data'.
    """
    cached = _per_entry_delta(g, entry_id)
    if cached is not None:
        return cached
    if parent is None:
        return None
    chal = _per_entry_drift_loss(g, entry_id)
    champ = _per_entry_drift_loss(parent, entry_id)
    if chal is None or champ is None:
        return None
    return chal - champ


def _heatmap_color(value: float, vmax: float) -> str:
    """Map a signed delta to a red/grey/green cell colour.

    ``vmax`` is the symmetric magnitude used to normalise; a delta of
    ``+vmax`` saturates to red (worse), ``-vmax`` to green (better),
    zero is neutral grey. Lower scalar is better, hence the sign mapping.

    Returns a literal CSS colour string — retained for tests that still
    exercise the canonical hex/rgba palette directly. The figure
    renderer itself uses :func:`_heatmap_cell_style` so the cell picks
    up the host's decision palette via CSS variables.
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


def _heatmap_cell_style(value: float, vmax: float) -> str:
    """Theme-aware fill style for one heatmap cell.

    The fill colour binds to a host CSS variable (``--paper-rejected``
    for the worse half, ``--paper-promoted`` for the better half,
    ``--paper-figure-grid`` for zero / unset). The signed magnitude
    drives ``fill-opacity`` independently so a dark host can re-tint
    the red/green hues without rewriting the SVG.
    """
    if vmax <= 0:
        return f"fill: {_VAR_GRID}"
    t = max(-1.0, min(1.0, value / vmax))
    if abs(t) < 0.04:
        # near-zero — render as the near-zero token (falls back to a
        # neutral grey when the host does not define it).
        return f"fill: {_VAR_NEAR_ZERO}"
    if t > 0:
        # red — worse
        a = 0.25 + 0.55 * t
        return f"fill: {_VAR_REJECTED}; fill-opacity: {a:.3f}"
    # green — better
    a = 0.25 + 0.55 * (-t)
    return f"fill: {_VAR_PROMOTED}; fill-opacity: {a:.3f}"


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

    # Build a lineage index so per-challenger cells can be diffed against
    # the round's champion (parent) when only absolute per-entry losses
    # are cached.
    by_id: dict[str, GenerationView] = {g.generation_id: g for g in data.generations}

    # Collect all per-entry deltas to pick a symmetric vmax.
    values: list[float] = []
    cell_grid: dict[tuple[int, int], float | None] = {}
    for ci, g in enumerate(challengers):
        parent = by_id.get(g.parent_generation_id)
        for ri, e in enumerate(entries):
            v = _per_entry_delta_against(g, parent, e.id)
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

    # Defs: stripe pattern for "no data" cells. The background tile and
    # the diagonal stroke both bind to host palette variables so the
    # pattern reads correctly in both light and dark surrounds.
    parts.append(
        '<defs><pattern id="nodata-stripes" patternUnits="userSpaceOnUse" '
        'width="6" height="6">'
        f'<rect width="6" height="6" style="fill: {_VAR_STRIPE_BG}"/>'
        f'<path d="M -1 7 L 7 -1" style="stroke: {_VAR_GRID}" '
        'stroke-width="1"/>'
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
                    f'style="stroke: {_VAR_GRID}" stroke-width="0.5"/>'
                )
                continue
            cell_style = _heatmap_cell_style(v, vmax)
            parts.append(
                f'<rect x="{x:.1f}" y="{y:.1f}" width="{w}" '
                f'height="{h}" style="{cell_style}; stroke: {_VAR_GRID}" '
                f'stroke-width="0.5"/>'
            )
            parts.append(
                f'<text class="svg-axis svg-value" '
                f'x="{x + w / 2:.1f}" y="{y + h / 2 + 4:.1f}" '
                f'text-anchor="middle">{_fmt_delta(v)}</text>'
            )

    # Legend (bottom): three small chips paired with their meaning,
    # painted with the same palette tokens the cells use so the legend
    # cannot drift from the figure.
    legend_y = height - 8
    legend_x = label_w
    parts.append(
        f'<rect x="{legend_x:.1f}" y="{legend_y - 8:.1f}" width="10" '
        f'height="10" style="fill: {_VAR_REJECTED}; fill-opacity: 0.6"/>'
        f'<text class="svg-legend" x="{legend_x + 14:.1f}" '
        f'y="{legend_y:.1f}">worse</text>'
        f'<rect x="{legend_x + 56:.1f}" y="{legend_y - 8:.1f}" width="10" '
        f'height="10" style="fill: {_VAR_NEAR_ZERO}"/>'
        f'<text class="svg-legend" x="{legend_x + 70:.1f}" '
        f'y="{legend_y:.1f}">flat</text>'
        f'<rect x="{legend_x + 100:.1f}" y="{legend_y - 8:.1f}" width="10" '
        f'height="10" style="fill: {_VAR_PROMOTED}; fill-opacity: 0.6"/>'
        f'<text class="svg-legend" x="{legend_x + 114:.1f}" '
        f'y="{legend_y:.1f}">better</text>'
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
            stroke_var, sw, dash = _VAR_PROMOTED, 2.0, ""
        elif g.decision == "rejected":
            stroke_var, sw, dash = _VAR_REJECTED, 1.3, ' stroke-dasharray="5 4"'
        elif g.decision == "deferred":
            stroke_var, sw, dash = _VAR_DEFERRED, 1.4, ' stroke-dasharray="2 3"'
        else:
            stroke_var, sw, dash = _VAR_BASELINE, 1.2, ' stroke-dasharray="3 4"'
        parts.append(
            f'<path d="M {x1:.1f} {y1:.1f} C {mid_x1:.1f} {y1:.1f}, '
            f'{mid_x2:.1f} {y2:.1f}, {x2:.1f} {y2:.1f}" '
            f'fill="none" style="stroke: {stroke_var}" '
            f'stroke-width="{sw}"{dash}/>'
        )

    # Nodes — fill is the decision-coloured palette token at low alpha;
    # stroke is the same token at full strength. The host palette flips
    # both via the ``--paper-*`` variables.
    for g in gens:
        x, y = positions[g.generation_id]
        stroke_var = _generation_decision_var(g)
        if g.decision == "rejected":
            dash_attr = ' stroke-dasharray="5 4"'
        elif g.decision == "deferred":
            dash_attr = ' stroke-dasharray="2 3"'
        else:
            dash_attr = ""
        if g.is_baseline:
            fill_var, fill_alpha = _VAR_BASELINE, 0.10
        elif g.decision == "promoted":
            fill_var, fill_alpha = _VAR_PROMOTED, 0.14
        elif g.decision == "rejected":
            fill_var, fill_alpha = _VAR_REJECTED, 0.10
        elif g.decision == "deferred":
            fill_var, fill_alpha = _VAR_DEFERRED, 0.12
        else:
            fill_var, fill_alpha = _VAR_NEUTRAL, 0.0
        parts.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{node_w}" '
            f'height="{node_h}" rx="6" ry="6" '
            f'style="fill: {fill_var}; fill-opacity: {fill_alpha}; '
            f'stroke: {stroke_var}" stroke-width="1.4"{dash_attr}/>'
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
        f'y2="{header_h - 4}" style="stroke: {_VAR_GRID}" '
        f'stroke-width="0.8"/>'
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
# Figure: hypothesis-vs-outcome — per-generation predicted Δ vs actual Δ
# ---------------------------------------------------------------------------


# Magnitude tokens the proposer uses when writing an `expected_drift_movements`
# direction/magnitude pair. Used to project a categorical prediction onto a
# unit-scale rate axis [-1, +1]. The bucket values are deliberately coarse
# since they're a categorical estimate, not a measurement; the renderer
# normalises them per-lane so the visual reading is direction + relative
# magnitude, not an absolute number.
_MAGNITUDE_MAP: dict[str, float] = {
    "tiny": 0.02,
    "small": 0.05,
    "minor": 0.05,
    "modest": 0.08,
    "medium": 0.10,
    "moderate": 0.10,
    "large": 0.20,
    "major": 0.20,
    "big": 0.20,
}


def _parse_expected_pass_rate_delta(text: str) -> float | None:
    """Coerce a textual ``expected_pass_rate_delta`` to a midpoint float.

    The proposer writes free-form predictions like ``"+0.05 to +0.15"`` or
    ``"-0.10"`` or ``"+0.0"``. This reducer accepts a single signed number,
    a range ``a to b`` (returns the midpoint), or returns ``None`` when
    nothing parseable is found. The numeric magnitude side is the same axis
    the realised Δ pass rate uses, so the predicted/actual pair plot
    directly against one another.
    """
    if not text or not isinstance(text, str):
        return None
    import re

    # Match each signed-decimal token.
    nums = re.findall(r"[+-]?\d+\.\d+|[+-]?\d+", text)
    if not nums:
        return None
    try:
        values = [float(n) for n in nums]
    except ValueError:
        return None
    if len(values) >= 2:
        return (values[0] + values[1]) / 2.0
    return values[0]


def _direction_sign(direction: str) -> int:
    """Map a textual direction to a sign for projecting magnitude onto Δ."""
    d = (direction or "").strip().lower()
    if d in ("decrease", "down", "lower", "drop", "fall"):
        return -1
    if d in ("increase", "up", "higher", "rise"):
        return +1
    return 0


def _predicted_drift_delta_sum(g: GenerationView) -> float | None:
    """Project the proposer's expected drift movements to a single Δ scalar.

    Sums signed magnitude predictions across drift kinds: a "decrease /
    moderate" movement contributes -0.10, an "increase / small" +0.05, and
    so on, mirroring the units the realised ``drift_loss_delta`` reads in.
    Returns ``None`` when the proposer recorded no movements at all (so the
    figure can mark the bar as "no prediction").
    """
    if not g.expected_drift_movements:
        return None
    total = 0.0
    seen = False
    for mv in g.expected_drift_movements:
        sign = _direction_sign(str(mv.get("direction", "")))
        mag = str(mv.get("magnitude", "")).strip().lower()
        if not mag:
            continue
        # Accept a free-form ``magnitude`` like "small" / "moderate" /
        # "large"; default to the moderate bucket when unrecognised.
        amount = _MAGNITUDE_MAP.get(mag)
        if amount is None:
            # tolerate a raw numeric in the magnitude slot.
            try:
                amount = abs(float(mag))
            except ValueError:
                amount = 0.0
        if sign == 0 or amount == 0.0:
            continue
        total += sign * amount
        seen = True
    return total if seen else None


def _hvo_lane_extent(values: Iterable[float | None], floor: float) -> float:
    """Symmetric magnitude for one metric lane.

    Pass-rate Δ (≈ ±0.3) and drift-loss Δ (≈ ±30) live on completely
    different scales, so the figure draws each metric in its own lane
    with an independent symmetric extent. A small floor keeps a single
    near-zero round from collapsing to an empty lane.
    """
    mags = [abs(v) for v in values if v is not None]
    extent = max(mags) if mags else floor
    return max(extent, floor)


def _render_hvo_predicted_marker(
    value: float | None,
    *,
    zero_x: float,
    bar_w_max: float,
    extent: float,
    pred_y: float,
    marker_h: float,
) -> str:
    """Render the predicted marker for one lane.

    Predicted shows as an outlined, dashed bar floating above the
    actual bar — the dashed stroke makes it visually distinct from
    the solid actual fill even at small magnitudes. Returns a short
    ``(no prediction)`` label when the proposer didn't record a
    forecast for this metric.
    """
    if value is None:
        return (
            f'<text class="svg-legend" x="{zero_x + 4:.1f}" '
            f'y="{pred_y + marker_h / 2 + 3:.1f}" '
            f'fill-opacity="0.7">(no prediction)</text>'
        )
    w = bar_w_max * min(1.0, abs(value) / extent) if extent > 0 else 0.0
    # Always render at least a hairline so the prediction is visible
    # when the magnitude is tiny but non-zero.
    w = max(w, 2.0)
    x_start = zero_x if value >= 0 else zero_x - w
    return (
        f'<rect x="{x_start:.1f}" y="{pred_y:.1f}" '
        f'width="{w:.1f}" height="{marker_h:.1f}" '
        f'fill="none" style="stroke: {_VAR_PREDICTED}" '
        f'stroke-width="1.4" stroke-dasharray="4 2"/>'
    )


def _render_hvo_actual_bar(
    value: float | None,
    *,
    zero_x: float,
    bar_w_max: float,
    extent: float,
    act_y: float,
    marker_h: float,
    decision_var: str,
) -> str:
    """Render the actual bar for one lane.

    The actual bar is decision-coloured (promoted green, rejected red)
    and filled solid to read as the "ground truth" against the
    predicted marker. ``None`` (no outcome recorded) yields nothing,
    which the lane footer reports as ``act —``.
    """
    if value is None:
        return ""
    w = bar_w_max * min(1.0, abs(value) / extent) if extent > 0 else 0.0
    w = max(w, 2.0) if value != 0.0 else w
    x_start = zero_x if value >= 0 else zero_x - w
    return (
        f'<rect x="{x_start:.1f}" y="{act_y:.1f}" '
        f'width="{w:.1f}" height="{marker_h:.1f}" '
        f'style="fill: {decision_var}; fill-opacity: 0.88"/>'
    )


def _hvo_hit_glyph(pred: float | None, act: float | None) -> str:
    """A short hit/miss glyph for one lane.

    Returns:
    * ``hit`` when prediction and outcome point the same direction (or
      both are ~zero);
    * ``miss`` when they point opposite ways;
    * empty string when there's no prediction to score against.
    """
    if pred is None or act is None:
        return ""
    if abs(pred) < 1e-6 and abs(act) < 1e-6:
        return "hit"
    if (pred >= 0) == (act >= 0):
        return "hit"
    return "miss"


def render_svg_hypothesis_vs_outcome(
    data: EpochReportData,
    *,
    width: int = 760,
    row_h: int = 78,
) -> str:
    """Inline SVG: per-generation predicted Δ vs actual Δ — one row per gen.

    Each scored generation occupies one full-width row stacked
    top-to-bottom in lineage order. The row carries:

    * a left **gutter** with generation id, decision tag, and a brief
      idea snippet;
    * a **pass rate Δ** lane with its own symmetric extent so small
      absolute values stay readable;
    * a **drift loss Δ** lane with its own symmetric extent (drift loss
      lives on a wildly different scale than pass rate).

    Within each lane the **predicted** Δ renders as an outlined, dashed
    bar in the upper strip; the **actual** Δ renders as a solid,
    decision-coloured bar in the lower strip. They share the zero
    midline so the visual reading is "did the prediction point the same
    way as the outcome, and was the magnitude comparable?". A small
    ``hit / miss`` glyph at the right of each lane summarises the pair.

    v0 (the baseline) is shown if scored — it carries no prediction so
    the lane reads ``(no prediction)`` and only the actual bar paints,
    keeping its position in the lineage visible. Rejected and deferred
    rounds are NOT silently dropped — the prediction was still made, so
    the figure renders the pair.
    """
    rounds: list[GenerationView] = []
    for g in data.generations:
        # Skip pending generations (no outcome yet). Baseline AND rejected
        # AND deferred AND promoted are all included — the prediction +
        # actual pair is meaningful for every scored generation.
        if g.decision in ("pending", ""):
            continue
        rounds.append(g)

    if not rounds:
        return _empty_svg(width, row_h * 2, "No scored generations to compare against predictions.")

    # Per-lane symmetric extents (each metric has its own scale).
    pass_extent = _hvo_lane_extent(
        [
            v
            for g in rounds
            for v in (
                _parse_expected_pass_rate_delta(g.expected_pass_rate_delta),
                g.pass_rate_delta,
            )
        ],
        floor=0.05,
    )
    drift_extent = _hvo_lane_extent(
        [v for g in rounds for v in (_predicted_drift_delta_sum(g), g.drift_loss_delta)],
        floor=0.10,
    )

    # Layout: header strip + one row per generation. Each row has a
    # left gutter (id + decision + idea snippet), then two lanes.
    # The header stacks three rows so the title, legend, and lane
    # headers never crash into one another: title on top, legend
    # below it, lane labels just above the first generation row.
    title_y = 16
    legend_y = 34
    lane_label_y = 58
    header_h = 68
    gutter_w = 168
    lane_pad = 18
    lane_w = (width - gutter_w - lane_pad * 3) // 2
    height = header_h + row_h * len(rounds) + 24

    parts: list[str] = []
    parts.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
        f'role="img" aria-label="Predicted vs actual Δ per generation">'
    )

    # Title row.
    parts.append(
        f'<text class="svg-title" x="12" y="{title_y}" font-weight="600">'
        "PREDICTED vs ACTUAL  ·  pass-rate Δ and drift-loss Δ per generation"
        "</text>"
    )

    # Predicted/actual legend in its own row so it can't collide with
    # the lane headers — the previous layout floated the legend on the
    # title line, which overlapped on wider lane extents.
    parts.append(
        f'<rect x="12" y="{legend_y - 10}" width="16" height="9" '
        f'fill="none" style="stroke: {_VAR_PREDICTED}" stroke-width="1.4" '
        f'stroke-dasharray="3 2"/>'
        f'<text class="svg-legend" x="34" y="{legend_y - 2}">'
        f"predicted (outlined, dashed)</text>"
        f'<rect x="206" y="{legend_y - 10}" width="16" height="9" '
        f'style="fill: {_VAR_PROMOTED}; fill-opacity: 0.88"/>'
        f'<text class="svg-legend" x="228" y="{legend_y - 2}">'
        f"actual (filled, decision-coloured)</text>"
    )

    # Lane header row — lane name + per-lane symmetric extent so the
    # reader can read the axis range without having to mentally
    # normalise. The "lane ±X" suffix calls out that each lane has
    # its own scale, since drift loss and pass rate live on very
    # different axes.
    pass_lane_x = gutter_w + lane_pad
    drift_lane_x = pass_lane_x + lane_w + lane_pad
    parts.append(
        f'<text class="svg-label" x="{pass_lane_x:.1f}" y="{lane_label_y}" '
        f'font-weight="600">pass rate Δ '
        f'<tspan fill-opacity="0.65">  (lane ±{pass_extent:.2f})</tspan></text>'
    )
    parts.append(
        f'<text class="svg-label" x="{drift_lane_x:.1f}" y="{lane_label_y}" '
        f'font-weight="600">drift loss Δ '
        f'<tspan fill-opacity="0.65">  (lane ±{drift_extent:.2f})</tspan></text>'
    )
    # Thin separator under the header strip.
    parts.append(
        f'<line x1="0" y1="{header_h - 2:.1f}" x2="{width}" '
        f'y2="{header_h - 2:.1f}" style="stroke: {_VAR_GRID}" '
        f'stroke-width="0.6"/>'
    )

    # One row per generation.
    marker_h = 11.0
    for ri, g in enumerate(rounds):
        oy = header_h + ri * row_h

        # Row separator (top of each row except the first).
        if ri > 0:
            parts.append(
                f'<line x1="0" y1="{oy:.1f}" x2="{width}" y2="{oy:.1f}" '
                f'style="stroke: {_VAR_GRID}" stroke-width="0.5" '
                f'stroke-opacity="0.6"/>'
            )

        decision_var = _generation_decision_var(g)
        decision_text = "baseline" if g.is_baseline else g.decision

        # Gutter: generation id (large), decision pill, one-line idea snippet.
        parts.append(
            f'<text class="svg-label" x="12" y="{oy + 22:.1f}" '
            f'font-weight="700" font-size="14">'
            f"{_esc(g.generation_id)}</text>"
        )
        # Decision pill — small filled rect with the decision word inside.
        pill_x = 50
        pill_y = oy + 10
        pill_w = 78
        pill_h = 16
        parts.append(
            f'<rect x="{pill_x:.1f}" y="{pill_y:.1f}" '
            f'width="{pill_w}" height="{pill_h}" rx="8" ry="8" '
            f'style="fill: {decision_var}; fill-opacity: 0.16; '
            f'stroke: {decision_var}" stroke-width="1"/>'
        )
        parts.append(
            f'<text class="svg-axis" x="{pill_x + pill_w / 2:.1f}" '
            f'y="{pill_y + 11.5:.1f}" text-anchor="middle" '
            f'font-weight="600">{_esc(decision_text)}</text>'
        )
        # Idea snippet (truncated; provides scannable context per row).
        idea = _truncate(g.core_idea, 36) if g.core_idea else ""
        if idea:
            parts.append(
                f'<text class="svg-axis" x="12" y="{oy + 46:.1f}" '
                f'fill-opacity="0.78">{_esc(idea)}</text>'
            )

        # Compute per-row values once.
        pred_pass = _parse_expected_pass_rate_delta(g.expected_pass_rate_delta)
        pred_drift = _predicted_drift_delta_sum(g)
        # The baseline has no outcome.delta in the lineage sense — its
        # pass_rate_delta / drift_loss_delta are zero from the reducer.
        # Suppress the lane bars on the baseline row so it reads as
        # "baseline anchor", not "predicted zero".
        act_pass: float | None = g.pass_rate_delta
        act_drift: float | None = g.drift_loss_delta
        if g.is_baseline:
            pred_pass = None
            pred_drift = None
            act_pass = None
            act_drift = None

        # Draw each lane. Each lane has:
        #   - a horizontal axis spanning [zero - extent .. zero + extent]
        #   - a centered zero tick
        #   - predicted marker above, actual bar below
        #   - numeric pred/act/hit at the right
        lanes: tuple[tuple[str, float, float, float | None, float | None], ...] = (
            ("pass", pass_lane_x, pass_extent, pred_pass, act_pass),
            ("drift", drift_lane_x, drift_extent, pred_drift, act_drift),
        )
        for _, lane_x, extent, pred_v, act_v in lanes:
            bar_w_max = (lane_w - 80) / 2.0  # half-width per side
            zero_x = lane_x + (lane_w - 80) / 2.0  # leave room for the right-side label
            axis_y = oy + 36
            # Horizontal axis line (the zero tick spans the full lane).
            parts.append(
                f'<line x1="{lane_x:.1f}" y1="{axis_y:.1f}" '
                f'x2="{lane_x + lane_w - 80:.1f}" y2="{axis_y:.1f}" '
                f'style="stroke: {_VAR_GRID}" stroke-width="0.5" '
                f'stroke-opacity="0.6"/>'
            )
            # Zero tick.
            parts.append(
                f'<line x1="{zero_x:.1f}" y1="{axis_y - 18:.1f}" '
                f'x2="{zero_x:.1f}" y2="{axis_y + 18:.1f}" '
                f'style="stroke: {_VAR_BASELINE}" stroke-width="0.8" '
                f'stroke-opacity="0.8"/>'
            )
            pred_y = axis_y - 17.0  # predicted bar floats above the axis
            act_y = axis_y + 6.0  # actual bar sits below the axis
            parts.append(
                _render_hvo_predicted_marker(
                    pred_v,
                    zero_x=zero_x,
                    bar_w_max=bar_w_max,
                    extent=extent,
                    pred_y=pred_y,
                    marker_h=marker_h,
                )
            )
            parts.append(
                _render_hvo_actual_bar(
                    act_v,
                    zero_x=zero_x,
                    bar_w_max=bar_w_max,
                    extent=extent,
                    act_y=act_y,
                    marker_h=marker_h,
                    decision_var=decision_var,
                )
            )
            # Right-side numeric labels: pred / act / hit-glyph.
            label_x = lane_x + lane_w - 76
            pred_text = f"pred {_fmt_delta(pred_v)}" if pred_v is not None else "pred —"
            act_text = f"act {_fmt_delta(act_v)}" if act_v is not None else "act —"
            parts.append(
                f'<text class="svg-legend" x="{label_x:.1f}" '
                f'y="{axis_y - 8:.1f}" fill-opacity="0.85">'
                f"{_esc(pred_text)}</text>"
            )
            parts.append(
                f'<text class="svg-axis svg-value" x="{label_x:.1f}" '
                f'y="{axis_y + 14:.1f}">{_esc(act_text)}</text>'
            )
            glyph = _hvo_hit_glyph(pred_v, act_v)
            if glyph:
                glyph_var = _VAR_PROMOTED if glyph == "hit" else _VAR_REJECTED
                parts.append(
                    f'<text class="svg-legend" x="{label_x:.1f}" '
                    f'y="{axis_y + 26:.1f}" '
                    f'style="fill: {glyph_var}" font-weight="600">'
                    f"{_esc(glyph)}</text>"
                )

    # Footer caption explaining the scoring of hit vs miss.
    parts.append(
        f'<text class="svg-legend" x="12" y="{height - 6:.1f}" '
        f'fill-opacity="0.7">'
        f"hit = predicted and actual point the same direction (or both ≈ 0); "
        f"miss = they disagree. Each lane is normalised independently."
        f"</text>"
    )

    parts.append("</svg>")
    return "".join(parts)


# ---------------------------------------------------------------------------
# Figure: mutation-impact matrix — rows = mutation sites, cols = generations
# ---------------------------------------------------------------------------


def _generation_outcome_var(g: GenerationView) -> str:
    """Map a generation to its outcome palette token for impact-matrix cells.

    Promoted → promoted, rejected → rejected, deferred / pending →
    incomplete. The baseline never appears in this matrix (it isn't a
    challenger), and its column is dropped upstream.
    """
    if g.decision == "promoted":
        return _VAR_PROMOTED
    if g.decision == "rejected":
        return _VAR_REJECTED
    return _VAR_INCOMPLETE


def _touched_mutation_ids(g: GenerationView) -> set[str]:
    """Set of mutation ids the generation's patches addressed."""
    return {str(p.get("mutation_id", "")) for p in g.patches if p.get("mutation_id")}


def render_svg_mutation_impact_matrix(
    data: EpochReportData,
    *,
    cell_w: int = 50,
    cell_h: int = 22,
    label_w: int = 160,
) -> str:
    """Inline SVG: rows = mutation sites, cols = generations.

    Each cell is filled with the outcome colour of the generation when
    that site was touched in that generation, otherwise empty (a thin
    grid stroke marks the empty position). The matrix shows the
    campaign's exploration pattern at a glance: which sites were tried
    in which generations and with what result.

    Sites that were never touched by any challenger are dropped; the
    matrix focuses on the active region of the surface. The baseline is
    excluded from the columns since it never carries a patch.
    """
    challengers = [g for g in data.generations if not g.is_baseline]
    if not challengers:
        return _empty_svg(
            max(360, cell_w * 4 + label_w),
            max(120, cell_h * 4),
            "No challengers yet — mutation-impact matrix is empty.",
        )

    # Collect every site referenced by a patch across the campaign,
    # plus the canonical mutation-surface label / file when available.
    surface_label: dict[str, str] = {}
    for m in data.mutation_surface:
        mid = str(m.get("id", ""))
        if mid:
            kind = str(m.get("kind", ""))
            surface_label[mid] = f"{mid}  ·  {kind}" if kind else mid

    touched_ids: list[str] = []
    seen: set[str] = set()
    for g in challengers:
        for mid in _touched_mutation_ids(g):
            if mid and mid not in seen:
                seen.add(mid)
                touched_ids.append(mid)

    if not touched_ids:
        return _empty_svg(
            max(360, cell_w * len(challengers) + label_w),
            max(120, cell_h * 4),
            "No patches touched the mutation surface yet.",
        )

    header_h = 40
    width = label_w + cell_w * len(challengers) + 18
    height = header_h + cell_h * len(touched_ids) + 36

    parts: list[str] = []
    parts.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
        f'role="img" aria-label="Mutation impact matrix">'
    )

    # Column headers — generation ids.
    for ci, g in enumerate(challengers):
        x = label_w + ci * cell_w + cell_w / 2
        parts.append(
            f'<text class="svg-label" x="{x:.1f}" y="{header_h - 22:.1f}" '
            f'text-anchor="middle" font-weight="600">{_esc(g.generation_id)}</text>'
        )
        # Tag the column with the outcome word so colour-blind viewers can
        # still parse the matrix.
        decision_text = (
            "promoted"
            if g.decision == "promoted"
            else ("rejected" if g.decision == "rejected" else g.decision or "pending")
        )
        parts.append(
            f'<text class="svg-legend" x="{x:.1f}" y="{header_h - 8:.1f}" '
            f'text-anchor="middle">{_esc(decision_text)}</text>'
        )

    # Row labels.
    for ri, mid in enumerate(touched_ids):
        y = header_h + ri * cell_h + cell_h / 2 + 4
        text = surface_label.get(mid, mid)
        parts.append(
            f'<text class="svg-axis svg-mono" x="{label_w - 8:.1f}" '
            f'y="{y:.1f}" text-anchor="end">{_esc(_truncate(text, 22))}</text>'
        )

    # Cells.
    for ri, mid in enumerate(touched_ids):
        for ci, g in enumerate(challengers):
            x = label_w + ci * cell_w + 2
            y = header_h + ri * cell_h + 2
            w = cell_w - 4
            h = cell_h - 4
            touched = mid in _touched_mutation_ids(g)
            if not touched:
                # Empty cell: a faint grid square so the matrix reads as
                # a grid even where no patch landed.
                parts.append(
                    f'<rect x="{x:.1f}" y="{y:.1f}" width="{w}" height="{h}" '
                    f'fill="none" style="stroke: {_VAR_GRID}" stroke-width="0.5"/>'
                )
                continue
            fill_var = _generation_outcome_var(g)
            parts.append(
                f'<rect x="{x:.1f}" y="{y:.1f}" width="{w}" height="{h}" '
                f'style="fill: {fill_var}; fill-opacity: 0.7; '
                f'stroke: {fill_var}" stroke-width="0.6"/>'
            )

    # Legend (bottom): three chips with palette + word, painted with the
    # same tokens the matrix cells use.
    legend_y = height - 8
    legend_x = label_w
    parts.append(
        f'<rect x="{legend_x:.1f}" y="{legend_y - 8:.1f}" width="10" '
        f'height="10" style="fill: {_VAR_PROMOTED}; fill-opacity: 0.7"/>'
        f'<text class="svg-legend" x="{legend_x + 14:.1f}" y="{legend_y:.1f}">'
        f"promoted</text>"
        f'<rect x="{legend_x + 76:.1f}" y="{legend_y - 8:.1f}" width="10" '
        f'height="10" style="fill: {_VAR_REJECTED}; fill-opacity: 0.7"/>'
        f'<text class="svg-legend" x="{legend_x + 90:.1f}" y="{legend_y:.1f}">'
        f"rejected</text>"
        f'<rect x="{legend_x + 146:.1f}" y="{legend_y - 8:.1f}" width="10" '
        f'height="10" style="fill: {_VAR_INCOMPLETE}; fill-opacity: 0.7"/>'
        f'<text class="svg-legend" x="{legend_x + 160:.1f}" y="{legend_y:.1f}">'
        f"incomplete</text>"
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
    "hypothesis-vs-outcome": "render_svg_hypothesis_vs_outcome",
    "mutation-impact-matrix": "render_svg_mutation_impact_matrix",
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
    if name == "hypothesis-vs-outcome":
        return render_svg_hypothesis_vs_outcome(data)
    if name == "mutation-impact-matrix":
        return render_svg_mutation_impact_matrix(data)
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
    "render_svg_hypothesis_vs_outcome",
    "render_svg_mutation_impact_matrix",
    "render_figure",
    "iter_figure_names",
]
