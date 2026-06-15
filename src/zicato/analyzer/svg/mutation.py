"""Figures: mutation surface (table-as-figure) and mutation-impact matrix."""

from __future__ import annotations

from zicato.analyzer.report_data import EpochReportData, GenerationView
from zicato.analyzer.svg.palette import (
    _VAR_GRID,
    _VAR_INCOMPLETE,
    _VAR_PROMOTED,
    _VAR_REJECTED,
)
from zicato.analyzer.svg.primitives import _empty_svg, _esc, _truncate


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
