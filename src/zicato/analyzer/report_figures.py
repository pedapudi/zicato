"""Inline-SVG figure generators for the epoch analysis report.

The comprehensive report is a hybrid artifact: the data-bearing sections
are templated deterministically from the workspace
(:mod:`zicato.analyzer.report_sections`); the *interpretive* prose is
written by the auxiliary LLM. The figures live in the same deterministic
half as the tables — every figure here is drawn directly from a
:class:`zicato.analyzer.report_data.EpochReportData` view so the chart
and the table next to it can never disagree.

The figure builders now live in the :mod:`zicato.analyzer.svg` package,
split one module per figure family over the shared
:mod:`~zicato.analyzer.svg.palette` (the one decision palette + its
CSS-variable tokens) and :mod:`~zicato.analyzer.svg.primitives`
(escaping / number formatting / placeholder helpers). This module is the
stable public surface: it re-exports every figure builder and the
dispatch helpers (:data:`FIGURE_RENDERERS`, :func:`render_figure`,
:func:`iter_figure_names`) so existing import paths keep working.

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

from collections.abc import Iterable

from zicato.analyzer.report_data import EpochReportData

# Figure families — every public ``render_svg_*`` builder, plus the
# handful of private helpers the figure tests reach for by name.
from zicato.analyzer.svg.drift import render_svg_drift_movements
from zicato.analyzer.svg.heatmap import render_svg_per_board_heatmap
from zicato.analyzer.svg.hypothesis import (
    _hvo_hit_glyph,  # noqa: F401 — re-exported for the figure tests
    _parse_expected_pass_rate_delta,  # noqa: F401 — re-exported for the figure tests
    _predicted_drift_delta_sum,  # noqa: F401 — re-exported for the figure tests
    render_svg_hypothesis_vs_outcome,
)
from zicato.analyzer.svg.lineage import render_svg_lineage_compact
from zicato.analyzer.svg.mutation import (
    render_svg_mutation_impact_matrix,
    render_svg_mutation_surface,
)

# Palette — the canonical decision hex constants, re-exported so the
# legacy ``from zicato.analyzer.report_figures import PROMOTED_COLOR``
# import paths keep resolving.
from zicato.analyzer.svg.palette import (
    BASELINE_COLOR,
    DEFERRED_COLOR,
    GRID_COLOR,
    PROMOTED_COLOR,
    REJECTED_COLOR,
)
from zicato.analyzer.svg.trajectory import render_svg_score_trajectory

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
