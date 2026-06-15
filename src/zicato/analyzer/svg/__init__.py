"""Inline-SVG figure builders for the epoch analysis report.

This package holds the per-figure-family renderers split out of the
former monolithic :mod:`zicato.analyzer.report_figures`. The shared
visual language lives in :mod:`~zicato.analyzer.svg.palette` (the one
decision palette + its CSS-variable tokens) and
:mod:`~zicato.analyzer.svg.primitives` (escaping / number formatting /
placeholder helpers). Each remaining module owns one cohesive figure
family and emits a self-contained SVG fragment.

The public entry points (``render_svg_*`` builders, the
``FIGURE_RENDERERS`` map, ``render_figure`` / ``iter_figure_names``)
are re-exported from :mod:`zicato.analyzer.report_figures` so existing
import paths stay stable.
"""
