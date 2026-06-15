"""Figure: per-board-entry Δ-scalar heatmap across generations."""

from __future__ import annotations

from zicato.analyzer.report_data import EpochReportData, GenerationView
from zicato.analyzer.svg.palette import (
    _VAR_GRID,
    _VAR_NEAR_ZERO,
    _VAR_PROMOTED,
    _VAR_REJECTED,
    _VAR_STRIPE_BG,
    GRID_COLOR,
)
from zicato.analyzer.svg.primitives import (
    _coerce_float,
    _empty_svg,
    _esc,
    _fmt_delta,
    _truncate,
)


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
