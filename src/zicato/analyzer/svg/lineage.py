"""Figure: compact lineage — boxes-and-edges over the proposed generations."""

from __future__ import annotations

from zicato.analyzer.report_data import EpochReportData, GenerationView
from zicato.analyzer.svg.palette import (
    _VAR_BASELINE,
    _VAR_DEFERRED,
    _VAR_NEUTRAL,
    _VAR_PROMOTED,
    _VAR_REJECTED,
    _generation_decision_var,
)
from zicato.analyzer.svg.primitives import _empty_svg, _esc

#: Minimum clear space between wrapped nodes, horizontally and vertically.
_GRID_GAP_X, _GRID_GAP_Y = 14, 14


def _wrapped_positions(
    gens: list[GenerationView],
    *,
    width: int,
    margin_x: int,
    margin_y: int,
    node_w: int,
    node_h: int,
) -> tuple[dict[str, tuple[float, float]], int]:
    """Grid positions for a lineage too wide to sit on one row.

    Fills the spine (baseline + promoted) first, then the rejected /
    deferred siblings, at a fixed step of ``node_w + gap`` so boxes
    cannot overlap at any generation count. The spine finishes its last
    row before the siblings begin, which keeps the centerline reading of
    the unwrapped layout: the champion chain leads, the discarded
    attempts follow beneath it.

    Returns the positions and the canvas height they require. A proper
    star / tree layout would read better still and is registered
    separately; this only guarantees legibility.
    """
    cols = max(1, int((width - 2 * margin_x + _GRID_GAP_X) // (node_w + _GRID_GAP_X)))
    spine = [g for g in gens if g.is_baseline or g.decision == "promoted"]
    siblings = [g for g in gens if not (g.is_baseline or g.decision == "promoted")]
    spine_rows = -(-len(spine) // cols)
    row_h = node_h + _GRID_GAP_Y

    positions: dict[str, tuple[float, float]] = {}
    for i, g in enumerate(spine + siblings):
        slot = i if i < len(spine) else i - len(spine) + spine_rows * cols
        row, col = divmod(slot, cols)
        positions[g.generation_id] = (
            float(margin_x + col * (node_w + _GRID_GAP_X)),
            float(margin_y + row * row_h),
        )

    total_rows = max(1, spine_rows + -(-len(siblings) // cols))
    return positions, 2 * margin_y + total_rows * row_h - _GRID_GAP_Y


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

    The single row divides the canvas by generation count, so past
    roughly nine generations the step falls below a node's width and the
    boxes pile up illegibly. At that point the layout wraps into a grid
    (see :func:`_wrapped_positions`) and the canvas grows taller to hold
    it; the caller's ``height`` is a minimum rather than a cap.
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
    if n > 1 and x_step < node_w:
        positions, wrapped_h = _wrapped_positions(
            gens,
            width=width,
            margin_x=margin_x,
            margin_y=margin_y,
            node_w=node_w,
            node_h=node_h,
        )
        # A minimum rather than a cap: the first wrap (two rows) needs LESS canvas
        # than the caller asked for, and letting it shrink would make the
        # figure jump shorter at the very generation count where it grows
        # an extra row — the opposite of what the reader expects.
        height = max(height, wrapped_h)
    else:
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
