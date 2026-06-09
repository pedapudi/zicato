"""Scoring dispatchers — the single seam the live paths call.

``reducer.py`` (Seam 1) and ``tournament/scoring.py`` (Seam 2) no longer
inline their formulas; they build the typed context from
:mod:`zicato.scoring.api`, hand it to the matching dispatcher here, and use
the returned value + provenance marker.

PHASE 1 behaviour
-----------------
Each dispatcher computes the BUILT-IN result (already carried on the context
as ``ctx.builtin_loss`` / ``ctx.builtin_scalar``) and returns it with the
``"builtin"`` provenance marker. There is no transform, no plugin, no config
crossing any boundary yet — this is a pure refactor whose only observable
change is the additive provenance string.

Where the later phases plug in (INERT hook points)
-------------------------------------------------
Both dispatchers are structured so Phases 2-3 are PURELY ADDITIVE — they fill
the marked branches, never rewrite the dispatch:

* **Phase 2 — declarative transforms.** A future ``pass_transform`` /
  ``drift_kind_aggregation`` block on ``ScoringWeights`` (neutral by default)
  would be applied here, BEFORE the built-in fallback, by reading the spec
  off ``ctx.weights`` and composing the named transforms from a
  ``zicato.scoring.transforms`` registry. The weights already cross the
  worker boundary via ``_weights_spec`` / ``_weights_from_args``, so threading
  the transform config is additive plumbing on that existing channel.

* **Phase 3 — dotted-spec plugins.** An optional ``drift_reducer`` /
  ``scalar_fn`` dotted spec on the contract would be resolved (via the shared
  importer) and invoked here, wrapped in the fail-open try/except the issue
  specifies: on raise / NaN / inf / timeout, log at WARNING and fall back to
  ``ctx.builtin_*`` with a provenance marker recording the fallback. The
  context already carries the built-in result precisely so a plugin can wrap
  rather than reimplement.

Each hook point below is marked ``# PHASE 2 HOOK`` / ``# PHASE 3 HOOK`` so the
next agent can find them by grep.
"""

from __future__ import annotations

from zicato.scoring.api import (
    PROVENANCE_BUILTIN,
    DriftContext,
    ScalarContext,
    ScoringProvenance,
)


def resolve_drift_loss(ctx: DriftContext) -> tuple[float, ScoringProvenance]:
    """Resolve **Seam 1** (per-run drift loss) for one run.

    Returns ``(loss, provenance)``. PHASE 1: always
    ``(ctx.builtin_loss, "builtin")``.

    Runs inside the killable worker subprocess. Must stay pure /
    deterministic / no-LLM / no-I/O / no-wall-clock.
    """
    # PHASE 2 HOOK: apply declarative drift_kind_aggregation transforms read
    # off ctx.weights here, producing a transformed loss + a "transform:..."
    # provenance, before falling through to the built-in.
    #
    # PHASE 3 HOOK: if ctx.weights / contract names a `drift_reducer` dotted
    # spec, resolve + invoke it on ctx here, wrapped fail-open (raise / NaN /
    # inf / timeout -> log WARNING + fall back to ctx.builtin_loss with a
    # "builtin (fallback: ...)" provenance). The plugin reads ctx.builtin_loss
    # to wrap rather than reimplement.
    return ctx.builtin_loss, PROVENANCE_BUILTIN


def resolve_scalar(ctx: ScalarContext) -> tuple[float, ScoringProvenance]:
    """Resolve **Seam 2** (per-generation scalar) for one generation.

    Returns ``(scalar, provenance)``. PHASE 1: always
    ``(ctx.builtin_scalar, "builtin")``.

    Runs in the orchestrator. Must stay pure / deterministic / no-LLM /
    no-I/O / no-wall-clock.
    """
    # PHASE 2 HOOK: apply the declarative pass_transform read off ctx.weights
    # here (e.g. {"op":"pow","exponent":2.0} replacing the old pass_exponent),
    # producing a transformed scalar + a "transform:..." provenance, before
    # falling through to the built-in.
    #
    # PHASE 3 HOOK: if the contract names a `scalar_fn` dotted spec, resolve +
    # invoke it on ctx here, wrapped fail-open (raise / NaN / inf / timeout ->
    # log WARNING + fall back to ctx.builtin_scalar with a "builtin
    # (fallback: ...)" provenance). The plugin reads ctx.builtin_scalar to
    # wrap rather than reimplement.
    return ctx.builtin_scalar, PROVENANCE_BUILTIN


__all__ = [
    "resolve_drift_loss",
    "resolve_scalar",
]
