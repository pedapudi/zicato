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

from dataclasses import replace

from zicato.scoring.api import (
    PROVENANCE_BUILTIN,
    DriftContext,
    ScalarContext,
    ScoringProvenance,
)
from zicato.scoring.builtins import _TASK_FAILURE_RATIO_MULTIPLIER, _kind_multiplier
from zicato.scoring.plugins import apply_drift_reducer, apply_scalar_fn
from zicato.scoring.transforms import apply_transform, is_neutral


def _spec_provenance(spec: object) -> str:
    """Render a transform spec as a compact provenance token.

    ``{"op":"pow","exponent":2.0}`` → ``"pow(2.0)"`` (params in spec order),
    ``{"op":"harmonic"}`` → ``"harmonic"``. Structured enough for a later
    phase to parse/render, and stable for tests to assert against.
    """
    if not isinstance(spec, dict):
        return "?"
    op = spec.get("op", "?")
    params = [f"{v}" for k, v in spec.items() if k != "op"]
    return f"{op}({', '.join(params)})" if params else f"{op}"


def resolve_drift_loss(ctx: DriftContext) -> tuple[float, ScoringProvenance]:
    """Resolve **Seam 1** (per-run drift loss) for one run.

    Returns ``(loss, provenance)``.

    * No ``drift_kind_aggregation`` configured (or every entry neutral) →
      ``(ctx.builtin_loss, "builtin")`` — byte-identical to today.
    * Otherwise the dispatcher re-derives the per-drift-count terms, applying
      the named transform to the COUNT of each kind that carries a non-neutral
      spec (``severity × kind_weight × transform(count)`` in place of
      ``severity × kind_weight × count``), and re-adds the unchanged
      plan-revision / task-failure / runtime terms — so a default-linear kind
      contributes exactly as the built-in does, and only the configured kinds
      reshape. The provenance records each transformed kind, e.g.
      ``"transform:drift{looping_reasoning=harmonic}"``.

    If the contract names a ``drift_reducer`` dotted spec the dispatcher then
    composes a Phase-3 plugin ON TOP of the transformed value: the plugin sees
    the transformed loss as ``ctx.builtin_loss`` (so it WRAPS the declarative
    shape) and, fail-open, a raise / non-finite return falls back to the
    transformed value with a ``"<pre-plugin token> (fallback: ...)"``
    provenance. Success yields ``"plugin:drift_reducer=<spec>"``.

    Runs inside the killable worker subprocess (both the transform AND the
    plugin resolution + invocation). Must stay pure / deterministic / no-LLM /
    no-I/O / no-wall-clock. The transform specs were already validated at
    contract load (``ScoringWeights.__post_init__``), so :func:`apply_transform`
    is total here and never yields a ``NaN`` mid-run.
    """
    # PHASE 2 — declarative drift_kind_aggregation. Compute the
    # transformed-or-builtin loss + its provenance; this becomes the PRE-PLUGIN
    # value the Phase-3 plugin (if any) composes on top of.
    pre_value, pre_token = _drift_transform(ctx)

    # PHASE 3 HOOK — dotted-spec drift_reducer plugin (Seam 1, runs in the
    # worker). Composes ON TOP of the Phase-2 transformed loss: the plugin sees
    # `pre_value` as `ctx.builtin_loss`, so it WRAPS the declarative shape rather
    # than the raw built-in. Fail-open (raise / non-finite -> WARNING + fall back
    # to `pre_value` with a "<pre_token> (fallback: ...)" provenance). NO
    # timeout — these are declared pure CPU functions (see plugins.py).
    spec = ctx.weights.drift_reducer
    if not spec:
        return pre_value, pre_token
    plugin_ctx = replace(ctx, builtin_loss=pre_value)
    return apply_drift_reducer(spec, plugin_ctx, pre_value=pre_value, pre_token=pre_token)


def _drift_transform(ctx: DriftContext) -> tuple[float, ScoringProvenance]:
    """PHASE 2 — apply the declarative ``drift_kind_aggregation`` (if any).

    Returns the transformed-or-builtin loss + provenance. Extracted from
    :func:`resolve_drift_loss` so the dispatcher can use the result as the
    pre-plugin value the Phase-3 ``drift_reducer`` wraps.
    """
    agg = ctx.weights.drift_kind_aggregation
    active = {k: s for k, s in agg.items() if not is_neutral(s)}
    if not active:
        # Neutral: no kind reshaped → exact built-in path + provenance.
        return ctx.builtin_loss, PROVENANCE_BUILTIN

    weights = ctx.weights
    sev_w = weights.severity_weights
    loss = 0.0
    transformed: dict[str, str] = {}
    for c in ctx.drift_counts:
        sev_mult = sev_w.get(c.severity, 0.0)
        kind_mult = _kind_multiplier(c.kind, weights)
        spec = active.get(c.kind)
        if spec is None:
            # Default kind: linear count, byte-identical to the built-in term.
            shaped_count: float = c.count
        else:
            shaped_count = apply_transform(spec, c.count)
            transformed[c.kind] = _spec_provenance(spec)
        loss += sev_mult * kind_mult * shaped_count
    # The non-drift-count terms are NOT per-kind and ride through unchanged.
    loss += weights.plan_revision_weight * ctx.plan_revisions
    loss += _TASK_FAILURE_RATIO_MULTIPLIER * ctx.task_failure_ratio
    loss += weights.runtime_weight * (ctx.runtime_ms / 1000.0)
    loss = max(0.0, float(loss))

    if not transformed:
        # Every active spec matched no observed drift-count of that kind: the
        # value is byte-identical to the built-in, so report it as built-in.
        return loss, PROVENANCE_BUILTIN
    body = ", ".join(f"{k}={v}" for k, v in sorted(transformed.items()))
    return loss, f"transform:drift{{{body}}}"


def resolve_scalar(ctx: ScalarContext) -> tuple[float, ScoringProvenance]:
    """Resolve **Seam 2** (per-generation scalar) for one generation.

    Returns ``(scalar, provenance)``.

    * No ``pass_transform`` (or neutral ``linear``) → ``(ctx.builtin_scalar,
      "builtin")`` — byte-identical to today.
    * Otherwise the dispatcher reshapes ONLY the pass/miss component: it
      transforms the ``(1 - mean_score)`` miss with ``pass_transform`` and
      rebuilds the scalar as ``builtin_scalar - old_pass + new_pass``, leaving
      the drift + namespace components untouched. ``pass_transform =
      {"op":"pow","exponent":2.0}`` reproduces the retired
      ``pass_exponent=2`` quadratic-recall behaviour. Provenance records the
      transform, e.g. ``"transform:pass=pow(2.0)"``.

    If the contract names a ``scalar_fn`` dotted spec the dispatcher then
    composes a Phase-3 plugin ON TOP of the transformed value: the plugin sees
    the transformed scalar as ``ctx.builtin_scalar`` (so it WRAPS the
    declarative shape) and, fail-open, a raise / non-finite return falls back to
    the transformed value with a ``"<pre-plugin token> (fallback: ...)"``
    provenance. Success yields ``"plugin:scalar_fn=<spec>"``.

    Runs in the orchestrator. Must stay pure / deterministic / no-LLM /
    no-I/O / no-wall-clock. The spec was validated at contract load, so
    :func:`apply_transform` is total here.
    """
    # PHASE 2 — declarative pass_transform. Compute the transformed-or-builtin
    # scalar + its provenance; this becomes the PRE-PLUGIN value.
    pre_value, pre_token = _scalar_transform(ctx)

    # PHASE 3 HOOK — dotted-spec scalar_fn plugin (Seam 2, orchestrator).
    # Composes ON TOP of the Phase-2 transformed scalar: the plugin sees
    # `pre_value` as `ctx.builtin_scalar`, so it WRAPS the declarative shape.
    # Fail-open (raise / non-finite -> WARNING + fall back to `pre_value` with a
    # "<pre_token> (fallback: ...)" provenance). NO timeout (see plugins.py).
    spec = ctx.weights.scalar_fn
    if not spec:
        return pre_value, pre_token
    plugin_ctx = replace(ctx, builtin_scalar=pre_value)
    return apply_scalar_fn(spec, plugin_ctx, pre_value=pre_value, pre_token=pre_token)


def _scalar_transform(ctx: ScalarContext) -> tuple[float, ScoringProvenance]:
    """PHASE 2 — apply the declarative ``pass_transform`` (if any).

    Returns the transformed-or-builtin scalar + provenance. Extracted from
    :func:`resolve_scalar` so the dispatcher can use the result as the
    pre-plugin value the Phase-3 ``scalar_fn`` wraps.
    """
    spec = ctx.weights.pass_transform
    if spec is None or is_neutral(spec):
        return ctx.builtin_scalar, PROVENANCE_BUILTIN

    miss = 1.0 - ctx.mean_score
    old_pass = ctx.weights.pass_weight * miss
    new_pass = ctx.weights.pass_weight * apply_transform(spec, miss)
    scalar = ctx.builtin_scalar - old_pass + new_pass
    return scalar, f"transform:pass={_spec_provenance(spec)}"


__all__ = [
    "resolve_drift_loss",
    "resolve_scalar",
]
