"""Figure: hypothesis-vs-outcome — per-generation predicted Δ vs actual Δ."""

from __future__ import annotations

from collections.abc import Iterable

from zicato.analyzer.report_data import EpochReportData, GenerationView
from zicato.analyzer.svg.palette import (
    _VAR_BASELINE,
    _VAR_GRID,
    _VAR_PREDICTED,
    _VAR_PROMOTED,
    _VAR_REJECTED,
    _generation_decision_var,
)
from zicato.analyzer.svg.primitives import _empty_svg, _esc, _fmt_delta, _truncate

# Magnitude tokens the proposer uses when writing an `expected_drift_movements`
# direction/magnitude pair. Used to project a categorical prediction onto a
# unit-scale rate axis [-1, +1]. The bucket values are coarse
# since they're a categorical estimate rather than a measurement; the renderer
# normalises them per-lane so the visual reading is direction + relative
# magnitude rather than an absolute number.
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
