"""The one decision palette for the analysis-report SVG figures.

Both the standalone epoch HTML and the dashboard-embedded analysis
paper card draw from this single palette so the figures carry one
visual language across surfaces. The bare hex constants are the
canonical decision colours (retained for tests and callers that need a
literal hex); the ``_VAR_*`` tokens are the CSS-variable references the
figures actually emit so a host palette can re-tint a figure without
re-rendering it.
"""

from __future__ import annotations

from zicato.analyzer.report_data import GenerationView

# The canonical decision colours. This module owns them: the standalone
# epoch HTML and the copy the Publication view embeds render through the
# same figures, so one definition here carries the visual language across
# both surfaces.
#
# Figures emit these as CSS-variable references in a ``style=""``
# attribute on each SVG element, so a host that overrode the tokens would
# re-tint the figures without re-rendering the SVG. No host overrides them
# — see the note in :mod:`zicato.analyzer.report` and issue #367. The bare
# hex constants are retained for tests and for callers that want the
# canonical decision colour directly.
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


def _generation_decision_var(g: GenerationView) -> str:
    """Map a generation's decision to its CSS-variable reference.

    The figures emit this in a ``style=""`` attribute so the host
    palette (light paper-tone standalone, or dark dashboard inline)
    controls the actual rendered colour.
    """
    return _decision_var(g.decision, is_baseline=g.is_baseline)
