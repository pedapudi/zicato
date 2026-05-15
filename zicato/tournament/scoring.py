"""Scoring helpers: per-run drift loss and per-generation aggregation.

The aggregator collapses a list of :class:`~zicato.core.LossProfile`
instances (one per board entry executed under one generation) into a
single dict that is comparable across generations under the same
epoch's :class:`~zicato.core.ScoringWeights`. The dict carries:

* ``drift_loss_mean`` — mean ``drift_loss`` across the list.
* ``pass_rate`` — fraction of entries with an attached expectation that
  passed. Entries whose :attr:`LossProfile.pass_fail` is ``None`` (no
  expectation, or expectation could not be evaluated — e.g. budget
  exceeded before the matcher fired) are EXCLUDED from both numerator
  and denominator. When no entries had pass/fail at all, pass rate is
  reported as ``1.0`` so the ``(1 - pass_rate)`` term does not penalize
  a board that simply lacks expectations.
* ``expectation_count`` — number of entries that contributed to pass
  rate (denominator).
* ``entry_count`` — number of entries that contributed to drift loss
  (denominator for the drift term).
* ``scalar`` — the multi-objective combined score. Lower = better.
* ``per_entry`` — ``{entry_id: {"drift_loss": float, "pass_fail":
  bool|None}}`` for entry-level deltas the gate needs.
* ``namespace_aggregates`` — ``{namespace: weighted_aggregate}`` for
  every namespace observed in the inputs or named in
  :attr:`ScoringWeights.namespace_weights`. Each value is the
  namespace's per-run mean already multiplied by its weight, so it is
  ready to add directly into the scalar.
* ``scalar_components`` — ``{component_name: contribution}`` whose
  values sum exactly to ``scalar``. Includes ``"drift"`` (the
  drift-weight × drift_loss_mean term, kept for back-compat with
  callers that only know the drift term), ``"pass"`` (the
  ``(1 - pass_rate)`` term), and one entry per namespace whose weight
  is non-zero — minus the ``"drift:"`` namespace, which the drift
  component already covers to avoid double-counting.

The aggregation is intentionally cheap and deterministic; it does NOT
re-derive ``drift_loss`` from raw drift counts (that derivation lives
in the telemetry reducer). :func:`per_run_drift_loss` is exposed as
the canonical hook for callers who want to re-derive — today it just
returns ``loss.drift_loss``; if the telemetry reducer's formula ever
needs to diverge from the tournament's view, the divergence is bounded
to this single function.
"""

from __future__ import annotations

from typing import Any

from zicato.core import LossProfile, ScoringWeights


def per_run_drift_loss(loss: LossProfile, weights: ScoringWeights) -> float:
    """Return the scalar drift-loss for one run.

    The :class:`LossProfile` already carries a reducer-computed
    ``drift_loss`` field; this function exists so the tournament has a
    single canonical re-derivation hook. Today it simply returns
    ``loss.drift_loss``.

    The *weights* argument is accepted for API symmetry with the
    reducer's ``compute_drift_loss`` shape — callers can pass it
    confidently knowing the function will not silently ignore changes
    if the formula ever has to diverge between the two sites.
    """
    # The weights are intentionally unused right now; LossProfile.drift_loss
    # is the reducer's canonical output and the tournament trusts it.
    del weights
    return loss.drift_loss


def _namespace_of(metric_name: str) -> str:
    """Return the ``"<prefix>:"`` namespace of a metric name.

    Returns ``""`` (the empty string) for unnamespaced names so callers
    can skip them uniformly. The namespace is everything up to and
    including the first colon, matching the convention documented on
    :class:`zicato.core.MetricCount`.
    """
    idx = metric_name.find(":")
    if idx < 0:
        return ""
    return metric_name[: idx + 1]


def aggregate_namespaced_metrics(
    losses: list[LossProfile],
    weights: ScoringWeights,
) -> dict[str, float]:
    """Compute the weighted per-namespace aggregate across ``losses``.

    For the ``"drift:"`` namespace the aggregate is
    ``weights.drift_weight * mean(LossProfile.drift_loss)`` — keeping
    parity with the existing drift-loss-mean term so callers that
    consume only the namespace surface get the same drift contribution
    they used to derive from ``drift_loss_mean``.

    For every other namespace the aggregate is
    ``weights.namespace_weights[namespace] * mean(MetricCount.count)``
    over the namespace's entries across all losses' unified metric view
    (see :meth:`LossProfile.unified_metrics`). Namespaces named in
    :attr:`ScoringWeights.namespace_weights` but absent from the loss
    data appear with an aggregate of ``0.0`` so the keys are predictable
    for downstream consumers.

    Unnamespaced metric names (no colon prefix) are silently ignored.
    Namespaces present in the data but with no weight configured are
    aggregated at weight ``0.0`` (so they show up as ``0.0`` in the
    return value and contribute nothing to the scalar).

    Returns ``{namespace: weighted_aggregate}``. The mapping keys
    preserve the trailing colon (``"drift:"``, ``"cost:"``, ...).
    """
    namespace_means: dict[str, float] = {}

    # Drift gets the existing weighted-mean semantics so a multi-
    # objective scalar built on top of namespace aggregates collapses to
    # the same number a drift-only scorer would produce.
    if losses:
        drift_total = sum(per_run_drift_loss(loss, weights) for loss in losses)
        drift_mean = drift_total / len(losses)
    else:
        drift_mean = 0.0
    drift_weight = weights.namespace_weights.get("drift:", 0.0)
    namespace_means["drift:"] = drift_weight * drift_mean

    # Collect the per-namespace running sums across every loss's
    # unified metric view. We track counts per namespace independently
    # so that the per-run mean is well-defined even when one loss
    # contributes no entries for a given namespace (its contribution to
    # the sum is zero by convention — same model as
    # drift_loss_mean / entry_count above).
    sums: dict[str, float] = {}
    n_losses = len(losses)
    for loss in losses:
        # Sum within one loss first so a loss with multiple entries in
        # the same namespace counts each entry, and a loss with none
        # contributes zero.
        per_loss: dict[str, float] = {}
        for mc in loss.unified_metrics():
            ns = _namespace_of(mc.name)
            if not ns or ns == "drift:":
                # Drift already handled via drift_loss above; skip its
                # MetricCount mirror entries here to avoid double-
                # counting.
                continue
            per_loss[ns] = per_loss.get(ns, 0.0) + mc.count
        for ns, val in per_loss.items():
            sums[ns] = sums.get(ns, 0.0) + val

    # Promote known-but-absent namespaces to zero aggregates so
    # downstream consumers iterate a stable key set.
    observed_namespaces = set(sums.keys()) | set(weights.namespace_weights.keys())
    for ns in observed_namespaces:
        if ns == "drift:":
            continue
        mean_val = sums.get(ns, 0.0) / n_losses if n_losses > 0 else 0.0
        ns_weight = weights.namespace_weights.get(ns, 0.0)
        namespace_means[ns] = ns_weight * mean_val

    return namespace_means


def aggregate_generation_score(
    losses: list[LossProfile],
    weights: ScoringWeights,
) -> dict[str, Any]:
    """Aggregate per-entry losses into a per-generation summary dict.

    See module docstring for the dict shape. Empty input returns
    ``drift_loss_mean=0.0``, ``pass_rate=1.0``, both counts zero, and
    ``scalar=0.0`` — equivalent to "nothing to compare", which the gate
    treats as a tie (no improvement).
    """
    per_entry: dict[str, dict[str, Any]] = {}
    drift_total = 0.0
    pass_count = 0
    expectation_count = 0

    for loss in losses:
        drift = per_run_drift_loss(loss, weights)
        per_entry[loss.entry_id] = {
            "drift_loss": drift,
            "pass_fail": loss.pass_fail,
        }
        drift_total += drift
        if loss.pass_fail is not None:
            expectation_count += 1
            if loss.pass_fail:
                pass_count += 1

    entry_count = len(losses)
    drift_loss_mean = drift_total / entry_count if entry_count > 0 else 0.0
    pass_rate = (
        pass_count / expectation_count if expectation_count > 0 else 1.0
    )

    # Namespace aggregates are already weight-multiplied per
    # :func:`aggregate_namespaced_metrics` — they slot straight into
    # the scalar.
    namespace_aggregates = aggregate_namespaced_metrics(losses, weights)

    # Drift and pass components keep their existing meaning so callers
    # that only inspect "drift" / "pass" in scalar_components remain
    # well-defined under the back-compat default weights. The
    # ``"drift:"`` namespace entry of namespace_aggregates would
    # otherwise duplicate the drift contribution; we route it through
    # the named ``"drift"`` component instead.
    drift_component = weights.drift_weight * drift_loss_mean
    pass_component = weights.pass_weight * (1.0 - pass_rate)

    scalar_components: dict[str, float] = {
        "drift": drift_component,
        "pass": pass_component,
    }
    for ns, value in namespace_aggregates.items():
        if ns == "drift:":
            # Drift is owned by the "drift" component above. Including
            # it here would double-count when drift_weight equals the
            # namespace_weights["drift:"] coefficient (the common
            # back-compat case).
            continue
        # Strip the trailing colon for human-readable component names.
        component_name = ns[:-1] if ns.endswith(":") else ns
        scalar_components[component_name] = value

    scalar = sum(scalar_components.values())

    return {
        "drift_loss_mean": drift_loss_mean,
        "pass_rate": pass_rate,
        "expectation_count": expectation_count,
        "entry_count": entry_count,
        "scalar": scalar,
        "per_entry": per_entry,
        "namespace_aggregates": namespace_aggregates,
        "scalar_components": scalar_components,
    }


__all__ = [
    "aggregate_generation_score",
    "aggregate_namespaced_metrics",
    "per_run_drift_loss",
]
