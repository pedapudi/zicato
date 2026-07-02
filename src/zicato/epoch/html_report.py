"""Self-contained HTML analysis report rendering.

Produces ``analysis.html`` alongside the markdown ``analysis.md`` at
epoch close. The rendered document is a single file with NO external
CSS, NO external JS, NO external images — every visual is inline SVG
and every style is inline CSS. The result renders identically over
``file://`` as it does over ``https://``.

The module is deterministic: identical inputs yield identical output.
No LLM lives here — the optional :attr:`HtmlReportContext.narrative_html`
field accepts pre-rendered narrative HTML from the upstream LLM pass.

Design pillars (mirrored in tests):

* **Zero external resources** — search-and-replace audit guarantees no
  ``href="http``, ``src="http``, ``<link rel=``, or ``<script src=`` in
  the output.
* **Dark-mode aware** — every color rule has a ``@media
  (prefers-color-scheme: dark)`` override.
* **Small** — output for a 20-generation epoch sits well under the
  100 KB envelope the test suite enforces.
* **Useful with JS disabled** — the optional inline ``<script>`` only
  enhances keyboard nav across cards; the page is fully readable
  without it.
"""

from __future__ import annotations

import html as html_lib
import math
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from zicato.core.types import (
    Experiment,
    Generation,
)

# ---------------------------------------------------------------------------
# Palette (kept as module constants so tests can assert on the colors)
# ---------------------------------------------------------------------------

PROMOTED_COLOR = "#2ea043"
REJECTED_COLOR = "#d73a49"
BASELINE_COLOR = "#6e7681"
DEFERRED_COLOR = "#bf8700"
GRID_COLOR = "#d0d7de"


# ---------------------------------------------------------------------------
# Context dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HtmlReportContext:
    """Inputs to html report rendering.

    All deterministic — no LLM here. ``narrative_html`` is the optional
    pre-rendered narrative HTML supplied by the markdown-analysis pass
    (or empty if the operator wants a structure-only report).
    """

    epoch_id: str
    epoch_name: str
    duration: str
    generations: list[Generation] = field(default_factory=list)
    experiments: list[Experiment] = field(default_factory=list)
    final_scalar: float = 0.0
    promoted_count: int = 0
    rejected_count: int = 0
    narrative_html: str = ""


# ---------------------------------------------------------------------------
# Small formatting helpers
# ---------------------------------------------------------------------------


def _esc(s: str) -> str:
    """HTML-escape a string for safe inclusion in element bodies / attrs."""
    return html_lib.escape(s, quote=True)


def _fmt_delta(value: float) -> str:
    """Format a signed delta like ``+0.080`` / ``-0.012``."""
    return f"{value:+.3f}"


def _fmt_rate(value: float) -> str:
    """Format a rate value to 2 decimal places."""
    return f"{value:.2f}"


def _truncate(text: str, limit: int = 24) -> str:
    """Truncate text with a trailing ellipsis when it exceeds ``limit``."""
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _decision_of(exp: Experiment) -> str:
    """Return the experiment's tournament decision or ``"pending"``."""
    if exp.outcome is None:
        return "pending"
    return exp.outcome.tournament_decision


def _decision_marker(decision: str) -> str:
    """Return a small ASCII marker for a decision (no emoji per house style)."""
    if decision == "promoted":
        return "[+]"
    if decision == "rejected":
        return "[x]"
    if decision == "deferred":
        return "[=]"
    return "[?]"


# ---------------------------------------------------------------------------
# Inline CSS
# ---------------------------------------------------------------------------

_CSS = """
:root {
    --bg: #ffffff;
    --bg-elev: #f6f8fa;
    --text: #24292f;
    --text-muted: #57606a;
    --border: #d0d7de;
    --accent-promoted: #2ea043;
    --accent-rejected: #d73a49;
    --accent-baseline: #6e7681;
    --accent-deferred: #bf8700;
    --code-bg: #f6f8fa;
}

@media (prefers-color-scheme: dark) {
    :root {
        --bg: #1a1a1a;
        --bg-elev: #252525;
        --text: #e0e0e0;
        --text-muted: #9d9d9d;
        --border: #444;
        --code-bg: #2d2d2d;
    }
}

* { box-sizing: border-box; }

html, body {
    margin: 0;
    padding: 0;
    background: var(--bg);
    color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Oxygen, Ubuntu, sans-serif;
    line-height: 1.5;
    font-size: 15px;
}

main {
    max-width: 1200px;
    margin: 0 auto;
    padding: 32px 28px 64px 28px;
}

header.report-header {
    border-bottom: 1px solid var(--border);
    padding-bottom: 18px;
    margin-bottom: 28px;
}

header.report-header h1 {
    margin: 0 0 6px 0;
    font-size: 26px;
    font-weight: 600;
}

header.report-header .meta {
    margin: 0;
    color: var(--text-muted);
    font-size: 14px;
}

code, .mono {
    font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;
    font-size: 0.92em;
    background: var(--code-bg);
    padding: 1px 5px;
    border-radius: 4px;
}

h2 {
    font-size: 19px;
    font-weight: 600;
    margin: 36px 0 14px 0;
    padding-bottom: 6px;
    border-bottom: 1px solid var(--border);
}

h3 {
    font-size: 15px;
    font-weight: 600;
    margin: 0 0 10px 0;
    color: var(--text-muted);
    text-transform: uppercase;
    letter-spacing: 0.04em;
}

section { margin-bottom: 16px; }

/* Metadata grid */
.stat-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
    gap: 12px;
}

.stat {
    background: var(--bg-elev);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 14px 16px;
}

.stat-label {
    font-size: 11px;
    color: var(--text-muted);
    text-transform: uppercase;
    letter-spacing: 0.05em;
    margin-bottom: 6px;
}

.stat-value {
    font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;
    font-size: 20px;
    font-weight: 600;
}

.stat-value.promoted { color: var(--accent-promoted); }
.stat-value.rejected { color: var(--accent-rejected); }

/* Diagram panels */
.diagrams {
    display: grid;
    grid-template-columns: 1fr;
    gap: 18px;
}

@media (min-width: 900px) {
    .diagrams { grid-template-columns: minmax(0, 1.4fr) minmax(0, 1fr); }
}

.diagram {
    background: var(--bg-elev);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 14px 16px;
    overflow-x: auto;
}

.diagram svg { display: block; max-width: 100%; height: auto; }

.empty {
    color: var(--text-muted);
    font-style: italic;
    padding: 12px 0;
}

/* Experiment cards */
.experiment-card {
    background: var(--bg-elev);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 0;
    margin-bottom: 10px;
}

.experiment-card summary {
    cursor: pointer;
    padding: 12px 16px;
    font-weight: 500;
    list-style: none;
    user-select: none;
}

.experiment-card summary::-webkit-details-marker { display: none; }

.experiment-card summary:hover { background: rgba(127, 127, 127, 0.06); }

.experiment-card[open] summary {
    border-bottom: 1px solid var(--border);
}

.badge {
    display: inline-block;
    padding: 2px 8px;
    border-radius: 10px;
    font-size: 11px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    margin-right: 8px;
    color: #ffffff;
}
.badge.promoted { background: var(--accent-promoted); }
.badge.rejected { background: var(--accent-rejected); }
.badge.deferred { background: var(--accent-deferred); }
.badge.pending  { background: var(--accent-baseline); }

.card-body {
    padding: 14px 16px 16px 16px;
}

.card-body h4 {
    margin: 12px 0 6px 0;
    font-size: 13px;
    color: var(--text-muted);
    text-transform: uppercase;
    letter-spacing: 0.05em;
    font-weight: 600;
}

.card-body p { margin: 4px 0; }

.card-body table {
    border-collapse: collapse;
    width: 100%;
    font-size: 13px;
    margin: 6px 0;
}

.card-body th, .card-body td {
    text-align: left;
    padding: 5px 8px;
    border-bottom: 1px solid var(--border);
}

.card-body th {
    font-weight: 600;
    color: var(--text-muted);
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.04em;
}

.deltas {
    display: flex;
    flex-wrap: wrap;
    gap: 16px;
    margin: 10px 0;
}

.delta {
    font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;
    font-size: 13px;
}
.delta-label { color: var(--text-muted); margin-right: 4px; }

.narrative {
    background: var(--bg-elev);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 16px 20px;
}

footer.report-footer {
    margin-top: 40px;
    padding-top: 14px;
    border-top: 1px solid var(--border);
    color: var(--text-muted);
    font-size: 12px;
    text-align: center;
}

/* SVG type baseline */
.svg-label {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    font-size: 11px;
    fill: var(--text);
}
.svg-axis {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    font-size: 10px;
    fill: var(--text-muted);
}
""".strip()


_JS = """
(function () {
    // Small enhancement: arrow keys cycle focus across experiment cards.
    var cards = document.querySelectorAll('details.experiment-card > summary');
    if (!cards.length) return;
    cards.forEach(function (c, i) {
        c.setAttribute('tabindex', '0');
        c.addEventListener('keydown', function (e) {
            if (e.key === 'ArrowDown' && cards[i + 1]) { cards[i + 1].focus(); e.preventDefault(); }
            if (e.key === 'ArrowUp' && cards[i - 1]) { cards[i - 1].focus(); e.preventDefault(); }
        });
    });
})();
""".strip()


# ---------------------------------------------------------------------------
# SVG: lineage DAG
# ---------------------------------------------------------------------------


def render_svg_lineage(
    generations: list[Generation],
    experiments: list[Experiment],
    *,
    width: int = 900,
    height: int = 360,
) -> str:
    """Inline SVG of the lineage DAG.

    Nodes are rounded rectangles laid out left-to-right in lineage order.
    Promoted nodes use the green palette with a solid border; rejected
    nodes are red with a dashed border. Edges from a parent to a
    promoted child are thick solid arrows; rejected edges are thinner
    and dashed. Edge labels truncate the rejection reason / promotion
    note to ~24 characters.
    """
    if not generations:
        return _empty_svg(width, height, "No generations recorded yet.")

    # Index experiments by their child generation id so we can pick up
    # tournament outcomes per node.
    exp_by_gen: dict[str, Experiment] = {e.generation_id: e for e in experiments}

    # Layout: place promoted generations on the centerline; rejected /
    # deferred children dip below their parent. We walk generations in
    # given order — which is the lineage order callers persist.
    n = len(generations)
    margin_x, margin_y = 36, 50
    usable_w = width - 2 * margin_x
    usable_h = height - 2 * margin_y
    node_w, node_h = 130, 56
    if n == 1:
        x_step = 0.0
    else:
        x_step = (usable_w - node_w) / (n - 1)

    center_y = margin_y + usable_h / 2 - node_h / 2
    branch_offset = min(usable_h / 3, 90)

    positions: dict[str, tuple[float, float]] = {}
    for i, gen in enumerate(generations):
        x = margin_x + i * x_step
        # Baseline (no parent) sits on the centerline. Promoted children
        # also stay on the centerline. Rejected / deferred children sit
        # below the line so the promoted spine reads cleanly.
        exp = exp_by_gen.get(gen.id)
        if exp is None or exp.outcome is None or exp.outcome.tournament_decision == "promoted":
            y = center_y
        else:
            y = center_y + branch_offset
        positions[gen.id] = (x, y)

    parts: list[str] = []
    parts.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
        f'role="img" aria-label="Lineage graph">'
    )
    parts.append(
        "<defs>"
        '<marker id="arr-promoted" viewBox="0 0 10 10" refX="9" refY="5" '
        'markerWidth="6" markerHeight="6" orient="auto-start-reverse">'
        f'<path d="M 0 0 L 10 5 L 0 10 z" fill="{PROMOTED_COLOR}"/>'
        "</marker>"
        '<marker id="arr-rejected" viewBox="0 0 10 10" refX="9" refY="5" '
        'markerWidth="5" markerHeight="5" orient="auto-start-reverse">'
        f'<path d="M 0 0 L 10 5 L 0 10 z" fill="{REJECTED_COLOR}"/>'
        "</marker>"
        '<marker id="arr-deferred" viewBox="0 0 10 10" refX="9" refY="5" '
        'markerWidth="5" markerHeight="5" orient="auto-start-reverse">'
        f'<path d="M 0 0 L 10 5 L 0 10 z" fill="{DEFERRED_COLOR}"/>'
        "</marker>"
        "</defs>"
    )

    # --- Edges first so nodes paint on top ---
    for gen in generations:
        if gen.parent_id is None or gen.parent_id not in positions:
            continue
        parent_pos = positions[gen.parent_id]
        child_pos = positions[gen.id]
        exp = exp_by_gen.get(gen.id)
        decision: str = (
            exp.outcome.tournament_decision
            if exp is not None and exp.outcome is not None
            else "pending"
        )
        x1 = parent_pos[0] + node_w
        y1 = parent_pos[1] + node_h / 2
        x2 = child_pos[0]
        y2 = child_pos[1] + node_h / 2
        # Use a gentle cubic for the curve.
        cx1 = x1 + (x2 - x1) * 0.45
        cx2 = x1 + (x2 - x1) * 0.55
        if decision == "promoted":
            stroke = PROMOTED_COLOR
            stroke_w = 2.6
            dash = ""
            marker = "url(#arr-promoted)"
            label = "promoted"
        elif decision == "rejected":
            stroke = REJECTED_COLOR
            stroke_w = 1.4
            dash = ' stroke-dasharray="5 4"'
            marker = "url(#arr-rejected)"
            label = (
                exp.outcome.rejection_reason
                if exp is not None and exp.outcome is not None and exp.outcome.rejection_reason
                else "rejected"
            )
        elif decision == "deferred":
            stroke = DEFERRED_COLOR
            stroke_w = 1.6
            dash = ' stroke-dasharray="2 3"'
            marker = "url(#arr-deferred)"
            label = "deferred"
        else:
            stroke = BASELINE_COLOR
            stroke_w = 1.4
            dash = ' stroke-dasharray="3 4"'
            marker = ""
            label = "pending"
        parts.append(
            f'<path d="M {x1:.1f} {y1:.1f} C {cx1:.1f} {y1:.1f}, '
            f'{cx2:.1f} {y2:.1f}, {x2:.1f} {y2:.1f}" '
            f'fill="none" stroke="{stroke}" stroke-width="{stroke_w}"{dash} '
            f'marker-end="{marker}"/>'
        )
        # Edge label at midpoint.
        mid_x = (x1 + x2) / 2
        mid_y = (y1 + y2) / 2 - 6
        parts.append(
            f'<text class="svg-axis" x="{mid_x:.1f}" y="{mid_y:.1f}" '
            f'text-anchor="middle">{_esc(_truncate(label, 24))}</text>'
        )

    # --- Nodes ---
    for gen in generations:
        x, y = positions[gen.id]
        exp = exp_by_gen.get(gen.id)
        decision = _decision_of(exp) if exp is not None else "baseline"
        if gen.parent_id is None:
            # Baseline node — neutral.
            fill = "rgba(110, 118, 129, 0.12)"
            stroke = BASELINE_COLOR
            dash_attr = ""
            marker = "(seed)"
        elif decision == "promoted":
            fill = "rgba(46, 160, 67, 0.18)"
            stroke = PROMOTED_COLOR
            dash_attr = ""
            marker = _decision_marker(decision)
        elif decision == "rejected":
            fill = "rgba(215, 58, 73, 0.16)"
            stroke = REJECTED_COLOR
            dash_attr = ' stroke-dasharray="5 4"'
            marker = _decision_marker(decision)
        elif decision == "deferred":
            fill = "rgba(191, 135, 0, 0.18)"
            stroke = DEFERRED_COLOR
            dash_attr = ' stroke-dasharray="2 3"'
            marker = _decision_marker(decision)
        else:
            fill = "rgba(110, 118, 129, 0.12)"
            stroke = BASELINE_COLOR
            dash_attr = ""
            marker = "(pending)"

        parts.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{node_w}" height="{node_h}" '
            f'rx="8" ry="8" fill="{fill}" stroke="{stroke}" '
            f'stroke-width="1.8"{dash_attr}/>'
        )

        # Node text: id on top, marker in the middle row, deltas at bottom
        title = f"{gen.id} {marker}"
        parts.append(
            f'<text class="svg-label" x="{x + node_w / 2:.1f}" y="{y + 18:.1f}" '
            f'text-anchor="middle" font-weight="600">{_esc(title)}</text>'
        )
        if exp is not None and exp.outcome is not None:
            dscalar = exp.outcome.scalar_score_delta
            parts.append(
                f'<text class="svg-axis" x="{x + node_w / 2:.1f}" y="{y + 34:.1f}" '
                f'text-anchor="middle">'
                f"Δ scalar {_fmt_delta(dscalar)}</text>"
            )
            parts.append(
                f'<text class="svg-axis" x="{x + node_w / 2:.1f}" y="{y + 48:.1f}" '
                f'text-anchor="middle">'
                f"Δ drift {_fmt_delta(exp.outcome.drift_loss_delta)}</text>"
            )
        else:
            parts.append(
                f'<text class="svg-axis" x="{x + node_w / 2:.1f}" y="{y + 40:.1f}" '
                f'text-anchor="middle">baseline</text>'
            )

    parts.append("</svg>")
    return "".join(parts)


# ---------------------------------------------------------------------------
# SVG: score trajectory
# ---------------------------------------------------------------------------


def render_svg_score_trajectory(
    generations: list[Generation],
    experiments: list[Experiment],
    *,
    width: int = 720,
    height: int = 220,
) -> str:
    """Line + scatter of scalar score delta over generations in proposed order.

    Promoted points are filled green circles connected by a solid line;
    rejected points are hollow red squares not connected. The y-axis
    shows ``scalar_score_delta`` so the chart reads naturally even
    without an absolute scalar reference.
    """
    if not generations:
        return _empty_svg(width, height, "No generations to plot.")

    exp_by_gen: dict[str, Experiment] = {e.generation_id: e for e in experiments}
    margin_l, margin_r, margin_t, margin_b = 50, 18, 22, 36
    plot_w = width - margin_l - margin_r
    plot_h = height - margin_t - margin_b

    # Series: (idx, gen_id, decision, delta)
    series: list[tuple[int, str, str, float]] = []
    for i, gen in enumerate(generations):
        exp = exp_by_gen.get(gen.id)
        if exp is None or exp.outcome is None:
            series.append((i, gen.id, "baseline", 0.0))
        else:
            series.append(
                (i, gen.id, exp.outcome.tournament_decision, exp.outcome.scalar_score_delta)
            )

    values = [v for _, _, _, v in series]
    vmin, vmax = (min(values), max(values)) if values else (0.0, 0.0)
    # Add headroom; ensure non-zero range.
    if math.isclose(vmin, vmax):
        pad = max(0.1, abs(vmin) * 0.2 if vmin else 0.1)
        vmin -= pad
        vmax += pad
    else:
        pad = (vmax - vmin) * 0.1
        vmin -= pad
        vmax += pad

    n = len(series)
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
        f'role="img" aria-label="Scalar score trajectory">'
    )

    # Grid (5 horizontal lines).
    for k in range(5):
        gy = margin_t + plot_h * k / 4
        parts.append(
            f'<line x1="{margin_l}" y1="{gy:.1f}" x2="{margin_l + plot_w}" '
            f'y2="{gy:.1f}" stroke="{GRID_COLOR}" stroke-width="0.5" '
            f'stroke-opacity="0.6"/>'
        )
        tick_val = vmax - (vmax - vmin) * k / 4
        parts.append(
            f'<text class="svg-axis" x="{margin_l - 6}" y="{gy + 3:.1f}" '
            f'text-anchor="end">{tick_val:+.2f}</text>'
        )

    # Zero line — emphasised.
    if vmin < 0 < vmax:
        zy = to_y(0.0)
        parts.append(
            f'<line x1="{margin_l}" y1="{zy:.1f}" x2="{margin_l + plot_w}" '
            f'y2="{zy:.1f}" stroke="{BASELINE_COLOR}" stroke-width="1" '
            f'stroke-dasharray="3 3" stroke-opacity="0.8"/>'
        )

    # Axis line (x).
    parts.append(
        f'<line x1="{margin_l}" y1="{margin_t + plot_h:.1f}" '
        f'x2="{margin_l + plot_w}" y2="{margin_t + plot_h:.1f}" '
        f'stroke="{GRID_COLOR}" stroke-width="1"/>'
    )

    # Promoted line connecting promoted points in order.
    promoted_pts = [(to_x(i), to_y(v)) for i, _, d, v in series if d == "promoted"]
    if len(promoted_pts) >= 2:
        path = "M " + " L ".join(f"{x:.1f} {y:.1f}" for x, y in promoted_pts)
        parts.append(f'<path d="{path}" fill="none" stroke="{PROMOTED_COLOR}" stroke-width="2"/>')

    # Plot each point.
    for i, gen_id, decision, v in series:
        cx = to_x(i)
        cy = to_y(v)
        if decision == "promoted":
            parts.append(
                f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="5" '
                f'fill="{PROMOTED_COLOR}" stroke="{PROMOTED_COLOR}" '
                f'stroke-width="1.5"/>'
            )
        elif decision == "rejected":
            s = 4.5
            parts.append(
                f'<rect x="{cx - s:.1f}" y="{cy - s:.1f}" width="{2 * s}" '
                f'height="{2 * s}" fill="none" stroke="{REJECTED_COLOR}" '
                f'stroke-width="1.6"/>'
            )
        elif decision == "deferred":
            parts.append(
                f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="4.5" fill="none" '
                f'stroke="{DEFERRED_COLOR}" stroke-width="1.6" '
                f'stroke-dasharray="2 2"/>'
            )
        else:
            parts.append(
                f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="4" fill="none" '
                f'stroke="{BASELINE_COLOR}" stroke-width="1.4"/>'
            )
        # x-axis label
        parts.append(
            f'<text class="svg-axis" x="{cx:.1f}" '
            f'y="{margin_t + plot_h + 14:.1f}" text-anchor="middle">'
            f"{_esc(gen_id)}</text>"
        )
        # Value label above the point.
        parts.append(
            f'<text class="svg-axis" x="{cx:.1f}" y="{cy - 8:.1f}" '
            f'text-anchor="middle">{_fmt_delta(v)}</text>'
        )

    # Y-axis label (vertical).
    parts.append(
        f'<text class="svg-axis" x="{margin_l - 36}" '
        f'y="{margin_t + plot_h / 2:.1f}" text-anchor="middle" '
        f'transform="rotate(-90 {margin_l - 36} {margin_t + plot_h / 2:.1f})">'
        f"Δ scalar</text>"
    )
    # Min / max annotations.
    parts.append(
        f'<text class="svg-axis" x="{margin_l + plot_w - 4}" '
        f'y="{margin_t + 12:.1f}" text-anchor="end">max {vmax:+.2f}</text>'
    )
    parts.append(
        f'<text class="svg-axis" x="{margin_l + plot_w - 4}" '
        f'y="{margin_t + plot_h - 2:.1f}" text-anchor="end">'
        f"min {vmin:+.2f}</text>"
    )

    parts.append("</svg>")
    return "".join(parts)


# ---------------------------------------------------------------------------
# SVG: drift heatmap
# ---------------------------------------------------------------------------


def render_svg_drift_heatmap(
    generations: list[Generation],
    experiments: list[Experiment],
    *,
    cell_size: int = 28,
) -> str:
    """Heatmap of drift-kind ``to_rate`` across promoted generations.

    Rows are drift kinds (top-12 by absolute movement magnitude across
    the epoch). Columns are promoted generations in proposed order.
    Cell color uses a diverging palette: deep red for high rates,
    neutral mid, blue for low. Each cell labels the per-run rate.
    """
    exp_by_gen: dict[str, Experiment] = {e.generation_id: e for e in experiments}
    # Filter to promoted generations only (those with a promoted outcome).
    promoted_gens: list[Generation] = []
    for g in generations:
        exp = exp_by_gen.get(g.id)
        if (
            exp is not None
            and exp.outcome is not None
            and exp.outcome.tournament_decision == "promoted"
        ):
            promoted_gens.append(g)

    # Collect movements; aggregate by kind for ranking.
    kind_max_abs: dict[str, float] = {}
    # cell_value[(kind, gen_id)] = to_rate
    cell_value: dict[tuple[str, str], float] = {}
    for gen in promoted_gens:
        exp = exp_by_gen.get(gen.id)
        if exp is None or exp.outcome is None:
            continue
        for mv in exp.outcome.drift_movements:
            cell_value[(mv.kind, gen.id)] = mv.to_rate
            delta = abs(mv.to_rate - mv.from_rate)
            cur = kind_max_abs.get(mv.kind, 0.0)
            if delta > cur:
                kind_max_abs[mv.kind] = delta

    if not promoted_gens or not kind_max_abs:
        # Compute a reasonable empty size that at least frames the slot.
        return _empty_svg(
            cell_size * 12 + 180,
            cell_size * 2 + 60,
            "No drift movements recorded yet.",
        )

    # Top 12 kinds by largest absolute delta.
    ranked = sorted(kind_max_abs.items(), key=lambda kv: kv[1], reverse=True)[:12]
    kinds = [k for k, _ in ranked]

    n_cols = len(promoted_gens)
    n_rows = len(kinds)
    label_w = 150
    legend_h = 28
    inner_pad = 4
    width = label_w + n_cols * (cell_size + inner_pad) + 24
    height = 28 + n_rows * (cell_size + inner_pad) + legend_h + 8

    # Determine value range for color scaling — use the rate values themselves.
    all_rates = [v for v in cell_value.values()]
    if all_rates:
        rate_min = min(0.0, min(all_rates))
        rate_max = max(all_rates)
    else:
        rate_min, rate_max = 0.0, 1.0
    if math.isclose(rate_min, rate_max):
        rate_max = rate_min + 1.0

    def rate_to_color(v: float) -> str:
        # Normalize to [0, 1].
        t = (v - rate_min) / (rate_max - rate_min)
        t = max(0.0, min(1.0, t))
        # Diverging: blue (low) -> light -> red (high).
        # Interpolate via two segments.
        if t < 0.5:
            # blue to neutral
            u = t / 0.5
            r = int(33 + (240 - 33) * u)
            g = int(102 + (240 - 102) * u)
            b = int(172 + (240 - 172) * u)
        else:
            u = (t - 0.5) / 0.5
            r = int(240 + (178 - 240) * u)
            g = int(240 + (24 - 240) * u)
            b = int(240 + (43 - 240) * u)
        return f"rgb({r}, {g}, {b})"

    parts: list[str] = []
    parts.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
        f'role="img" aria-label="Drift heatmap">'
    )

    # Header row — generation ids.
    for i, gen in enumerate(promoted_gens):
        cx = label_w + i * (cell_size + inner_pad) + cell_size / 2
        parts.append(
            f'<text class="svg-axis" x="{cx:.1f}" y="20" text-anchor="middle">{_esc(gen.id)}</text>'
        )

    # Cells + row labels.
    for r, kind in enumerate(kinds):
        ry = 28 + r * (cell_size + inner_pad)
        parts.append(
            f'<text class="svg-label" x="{label_w - 8}" '
            f'y="{ry + cell_size / 2 + 4:.1f}" text-anchor="end">'
            f"{_esc(kind)}</text>"
        )
        for c, gen in enumerate(promoted_gens):
            cx = label_w + c * (cell_size + inner_pad)
            v = cell_value.get((kind, gen.id))
            if v is None:
                parts.append(
                    f'<rect x="{cx:.1f}" y="{ry:.1f}" '
                    f'width="{cell_size}" height="{cell_size}" '
                    f'rx="3" ry="3" fill="none" stroke="{GRID_COLOR}" '
                    f'stroke-width="0.8" stroke-dasharray="2 2"/>'
                )
                continue
            color = rate_to_color(v)
            parts.append(
                f'<rect x="{cx:.1f}" y="{ry:.1f}" '
                f'width="{cell_size}" height="{cell_size}" '
                f'rx="3" ry="3" fill="{color}" stroke="rgba(0,0,0,0.06)" '
                f'stroke-width="0.5"/>'
            )
            parts.append(
                f'<text class="svg-axis" x="{cx + cell_size / 2:.1f}" '
                f'y="{ry + cell_size / 2 + 3:.1f}" text-anchor="middle" '
                f'fill="#111">{_fmt_rate(v)}</text>'
            )

    # Legend at the bottom.
    legend_y = 28 + n_rows * (cell_size + inner_pad) + 8
    legend_x = label_w
    legend_w = min(220, n_cols * (cell_size + inner_pad))
    # Gradient bar via segmented rects.
    seg_count = 20
    seg_w = legend_w / seg_count
    for s in range(seg_count):
        t = s / (seg_count - 1)
        v = rate_min + t * (rate_max - rate_min)
        parts.append(
            f'<rect x="{legend_x + s * seg_w:.1f}" y="{legend_y:.1f}" '
            f'width="{seg_w + 0.5:.1f}" height="10" '
            f'fill="{rate_to_color(v)}" stroke="none"/>'
        )
    parts.append(
        f'<text class="svg-axis" x="{legend_x:.1f}" y="{legend_y + 22:.1f}">{rate_min:.2f}</text>'
    )
    parts.append(
        f'<text class="svg-axis" x="{legend_x + legend_w:.1f}" '
        f'y="{legend_y + 22:.1f}" text-anchor="end">{rate_max:.2f}</text>'
    )

    parts.append("</svg>")
    return "".join(parts)


def _empty_svg(width: int, height: int, message: str) -> str:
    """Render a small placeholder SVG with a single text label."""
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
        f'role="img" aria-label="{_esc(message)}">'
        f'<rect x="1" y="1" width="{width - 2}" height="{height - 2}" '
        f'fill="none" stroke="{GRID_COLOR}" stroke-width="1" '
        f'stroke-dasharray="4 3" rx="6" ry="6"/>'
        f'<text class="svg-axis" x="{width / 2:.1f}" y="{height / 2:.1f}" '
        f'text-anchor="middle">{_esc(message)}</text>'
        f"</svg>"
    )


# ---------------------------------------------------------------------------
# Experiment cards
# ---------------------------------------------------------------------------


def _render_patches_list(experiment: Experiment) -> str:
    if not experiment.patches:
        return '<p class="empty">No patches.</p>'
    items = []
    for p in experiment.patches:
        items.append(
            "<tr>"
            f"<td><code>{_esc(p.mutation_id)}</code></td>"
            f"<td><code>{_esc(p.op)}</code></td>"
            f"<td>{_esc(p.rationale)}</td>"
            "</tr>"
        )
    return (
        "<table><thead><tr><th>mutation</th><th>op</th><th>rationale</th>"
        "</tr></thead><tbody>" + "".join(items) + "</tbody></table>"
    )


def _render_experiment_card(experiment: Experiment, *, open_card: bool) -> str:
    """Render one ``<details>`` card for an experiment."""
    decision = _decision_of(experiment)
    badge_class = decision if decision in {"promoted", "rejected", "deferred"} else "pending"
    summary_bits: list[str] = []
    summary_bits.append(f'<span class="badge {badge_class}">{decision}</span>')
    summary_bits.append(f'<span class="mono">{_esc(experiment.generation_id)}</span>')
    summary_bits.append(" " + _esc(_truncate(experiment.hypothesis.core_idea, 80)))
    if experiment.outcome is not None:
        summary_bits.append(
            f' <span class="delta-label">Δ scalar</span>'
            f'<span class="mono">{_fmt_delta(experiment.outcome.scalar_score_delta)}</span>'
        )

    parts: list[str] = []
    open_attr = " open" if open_card else ""
    parts.append(f'<details class="experiment-card"{open_attr}>')
    parts.append("<summary>" + "".join(summary_bits) + "</summary>")
    parts.append('<div class="card-body">')

    # Hypothesis body
    parts.append("<h4>Hypothesis</h4>")
    parts.append(f"<p><strong>core idea.</strong> {_esc(experiment.hypothesis.core_idea)}</p>")
    if experiment.hypothesis.why:
        parts.append(f"<p><strong>why.</strong> {_esc(experiment.hypothesis.why)}</p>")
    if experiment.hypothesis.risks:
        parts.append(f"<p><strong>risks.</strong> {_esc(experiment.hypothesis.risks)}</p>")

    if experiment.hypothesis.modulating:
        mods = ", ".join(f"<code>{_esc(m)}</code>" for m in experiment.hypothesis.modulating)
        parts.append(f"<p><strong>modulating.</strong> {mods}</p>")

    # Expected vs actual drift movements (side-by-side table)
    parts.append("<h4>Expected vs actual drift</h4>")
    parts.append(_render_expected_vs_actual(experiment))

    # Deltas
    if experiment.outcome is not None:
        out = experiment.outcome
        parts.append('<div class="deltas">')
        parts.append(
            f'<span class="delta"><span class="delta-label">Δ pass_rate</span>'
            f"{_fmt_delta(out.pass_rate_delta)}</span>"
        )
        parts.append(
            f'<span class="delta"><span class="delta-label">Δ drift_loss</span>'
            f"{_fmt_delta(out.drift_loss_delta)}</span>"
        )
        parts.append(
            f'<span class="delta"><span class="delta-label">Δ scalar</span>'
            f"{_fmt_delta(out.scalar_score_delta)}</span>"
        )
        parts.append("</div>")
        if out.tournament_decision == "rejected" and out.rejection_reason:
            parts.append(f"<p><strong>rejection reason.</strong> {_esc(out.rejection_reason)}</p>")

    # Patches
    parts.append("<h4>Patches</h4>")
    parts.append(_render_patches_list(experiment))

    parts.append("</div>")
    parts.append("</details>")
    return "".join(parts)


def _render_expected_vs_actual(experiment: Experiment) -> str:
    """Side-by-side table of expected and actual drift movements."""
    expected = {m.kind: m for m in experiment.hypothesis.expected_drift_movements}
    actual = {}
    if experiment.outcome is not None:
        actual = {m.kind: m for m in experiment.outcome.drift_movements}
    kinds = sorted(set(expected.keys()) | set(actual.keys()))
    if not kinds:
        return '<p class="empty">No drift claims or movements.</p>'
    rows = []
    for k in kinds:
        exp = expected.get(k)
        act = actual.get(k)
        exp_cell = (
            f"{_esc(exp.direction)} ({_esc(exp.magnitude)})" if exp is not None else "&mdash;"
        )
        if act is not None:
            act_cell = f"{_fmt_rate(act.from_rate)} → {_fmt_rate(act.to_rate)}"
            match_cell = "match" if act.hypothesis_match else "miss"
        else:
            act_cell = "&mdash;"
            match_cell = "&mdash;"
        rows.append(
            "<tr>"
            f"<td><code>{_esc(k)}</code></td>"
            f"<td>{exp_cell}</td>"
            f"<td>{act_cell}</td>"
            f"<td>{match_cell}</td>"
            "</tr>"
        )
    return (
        "<table><thead><tr>"
        "<th>kind</th><th>expected</th><th>actual</th><th>verdict</th>"
        "</tr></thead><tbody>" + "".join(rows) + "</tbody></table>"
    )


def render_experiment_cards(experiments: list[Experiment]) -> str:
    """Render the list of experiment cards.

    The latest three experiments (by list order) are rendered open;
    older ones are collapsed.
    """
    if not experiments:
        return '<p class="empty">No experiments recorded.</p>'
    n = len(experiments)
    pieces = []
    # Latest first: reverse the list so the most recent appears at top.
    reversed_exps = list(reversed(experiments))
    open_threshold = min(3, n)
    for i, exp in enumerate(reversed_exps):
        pieces.append(_render_experiment_card(exp, open_card=(i < open_threshold)))
    return "".join(pieces)


# ---------------------------------------------------------------------------
# Metadata panel
# ---------------------------------------------------------------------------


def render_metadata_panel(ctx: HtmlReportContext) -> str:
    """Top panel with the key numbers for the epoch."""
    total_attempts = len(ctx.experiments)
    total_drift_delta = sum(
        e.outcome.drift_loss_delta for e in ctx.experiments if e.outcome is not None
    )
    cells = [
        ("Epoch id", _esc(ctx.epoch_id), ""),
        ("Duration", _esc(ctx.duration), ""),
        ("Generations", str(len(ctx.generations)), ""),
        ("Experiments", str(total_attempts), ""),
        ("Promoted", str(ctx.promoted_count), "promoted"),
        ("Rejected", str(ctx.rejected_count), "rejected"),
        ("Final scalar", _fmt_rate(ctx.final_scalar), ""),
        ("Total Δ drift_loss", _fmt_delta(total_drift_delta), ""),
    ]
    items = []
    for label, value, klass in cells:
        klass_attr = f" {klass}" if klass else ""
        items.append(
            '<div class="stat">'
            f'<div class="stat-label">{label}</div>'
            f'<div class="stat-value{klass_attr}">{value}</div>'
            "</div>"
        )
    return '<div class="stat-grid">' + "".join(items) + "</div>"


# ---------------------------------------------------------------------------
# Top-level renderer
# ---------------------------------------------------------------------------


def render_html_report(ctx: HtmlReportContext) -> str:
    """Render the full self-contained HTML document."""
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    title = f"zicato — epoch {ctx.epoch_id} analysis"

    lineage_svg = render_svg_lineage(ctx.generations, ctx.experiments)
    trajectory_svg = render_svg_score_trajectory(ctx.generations, ctx.experiments)
    heatmap_svg = render_svg_drift_heatmap(ctx.generations, ctx.experiments)
    metadata = render_metadata_panel(ctx)
    cards = render_experiment_cards(ctx.experiments)

    narrative_block = ""
    if ctx.narrative_html.strip():
        # Trust the upstream narrative HTML — it is generated by a pass
        # the caller owns. The wrapper supplies the section heading.
        narrative_block = (
            '<section class="narrative-section">'
            "<h2>Narrative</h2>"
            f'<div class="narrative">{ctx.narrative_html}</div>'
            "</section>"
        )

    epoch_name = _esc(ctx.epoch_name) if ctx.epoch_name else _esc(ctx.epoch_id)

    head = (
        "<!DOCTYPE html>"
        '<html lang="en">'
        "<head>"
        '<meta charset="utf-8" />'
        '<meta name="viewport" content="width=device-width, initial-scale=1" />'
        f"<title>{_esc(title)}</title>"
        f"<style>{_CSS}</style>"
        "</head>"
    )

    body = (
        "<body><main>"
        '<header class="report-header">'
        f"<h1>{epoch_name}</h1>"
        f'<p class="meta">epoch <code>{_esc(ctx.epoch_id)}</code> &middot; '
        f"{_esc(ctx.duration)} &middot; "
        f"final scalar <code>{_fmt_rate(ctx.final_scalar)}</code></p>"
        "</header>"
        '<section class="metadata-section">'
        f"<h2>Overview</h2>{metadata}"
        "</section>"
        '<section class="tournament-outcomes">'
        "<h2>Tournament outcomes</h2>"
        '<div class="diagrams">'
        f'<div class="diagram"><h3>Lineage</h3>{lineage_svg}</div>'
        f'<div class="diagram"><h3>Score trajectory</h3>{trajectory_svg}</div>'
        "</div>"
        "</section>"
        '<section class="drift-heatmap-section">'
        "<h2>Drift-kind movements</h2>"
        f'<div class="diagram">{heatmap_svg}</div>'
        "</section>"
        f"{narrative_block}"
        '<section class="experiments-section">'
        f"<h2>Experiments</h2>{cards}"
        "</section>"
        '<footer class="report-footer">'
        f"Generated by zicato at <code>{now}</code>."
        "</footer>"
        "</main>"
        f"<script>{_JS}</script>"
        "</body></html>"
    )
    return head + body


def write_html_report(path: Path, ctx: HtmlReportContext) -> None:
    """Render ``ctx`` and write the resulting HTML document to ``path``.

    Parent directories are NOT created — the caller is expected to have
    arranged the epoch directory already (the markdown analysis writer
    does the same).
    """
    text = render_html_report(ctx)
    path.write_text(text, encoding="utf-8")


__all__ = [
    "HtmlReportContext",
    "render_html_report",
    "write_html_report",
    "render_svg_lineage",
    "render_svg_score_trajectory",
    "render_svg_drift_heatmap",
    "render_experiment_cards",
    "render_metadata_panel",
]
