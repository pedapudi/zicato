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
  a board that simply lacks expectations. Kept for display / the gate's
  per-entry scope alongside the continuous ``mean_score``.
* ``mean_score`` — the UNIFORM continuous outcome axis: the arithmetic
  mean of each entry's :func:`entry_score` (a bool maps to ``1.0`` /
  ``0.0``; a continuous score is clamped to ``[0, 1]``) over the same
  expectation denominator as ``pass_rate``. This is what the scalar's
  pass component runs on. Because a bool maps to exactly
  ``float(pass_fail)``, ``mean_score`` equals ``pass_rate`` byte-for-byte
  on an all-bool board, so the substitution is back-compat-neutral.
* ``expectation_count`` — number of entries that contributed to pass
  rate / mean_score (denominator).
* ``entry_count`` — number of entries that contributed to drift loss
  (denominator for the drift term).
* ``scalar`` — the multi-objective combined score. Lower = better.
* ``per_entry`` — ``{entry_id: {"drift_loss": float, "pass_fail":
  bool|None, "score": float|None}}`` for entry-level deltas the gate
  needs. ``score`` is the per-entry :func:`entry_score` the gate's
  per-entry continuous-monotonicity scope reads.
* ``namespace_aggregates`` — ``{namespace: weighted_aggregate}`` for
  every namespace observed in the inputs or named in
  :attr:`ScoringWeights.namespace_weights`. Each value is the
  namespace's per-run mean already multiplied by its weight, so it is
  ready to add directly into the scalar.
* ``scalar_components`` — ``{component_name: contribution}`` whose
  values sum exactly to ``scalar``. Includes ``"drift"`` (the
  drift-weight × drift_loss_mean term, kept for back-compat with
  callers that only know the drift term), ``"pass"`` (the
  ``(1 - mean_score)`` term — equal to the historical
  ``(1 - pass_rate)`` term on an all-bool board), and one entry per namespace whose weight
  is non-zero — minus the ``"drift:"`` namespace, which the drift
  component already covers to avoid double-counting. When the opt-in
  diff-complexity term is active (``diff_complexity_weight > 0`` AND a
  ``diff_size`` was threaded for the candidate) a final ``"diff_complexity"``
  entry is appended; at the default weight ``0.0`` the key is absent and the
  scalar is byte-identical.

The aggregation is intentionally cheap and deterministic; it does NOT
re-derive ``drift_loss`` from raw drift counts (that derivation lives
in the telemetry reducer). :func:`per_run_drift_loss` is exposed as
the canonical hook for callers who want to re-derive — today it just
returns ``loss.drift_loss``; if the telemetry reducer's formula ever
needs to diverge from the tournament's view, the divergence is bounded
to this single function.
"""

from __future__ import annotations

import math
from typing import Any

from zicato.core import LossProfile, ScoringWeights
from zicato.scoring import ScalarContext, builtin_scalar, resolve_scalar
from zicato.scoring.builtins import diff_complexity_component


def entry_score(loss: LossProfile) -> float | None:
    """Return one loss's continuous outcome score in ``[0, 1]``, or ``None``.

    The single, UNIFORM mapping the scalar and gate read:

    * a profile with an explicit continuous ``score`` returns that value,
      clamped to ``[0.0, 1.0]`` (a non-finite value is treated as a miss,
      ``0.0``, so a rogue scorer can never poison the mean);
    * otherwise the binary ``pass_fail`` bit maps to ``1.0`` / ``0.0``;
    * an entry with neither a score NOR a pass/fail (``pass_fail is None``
      and ``score is None`` — no expectation, or one that could not fire)
      returns ``None`` and is EXCLUDED from the mean, exactly as the binary
      ``pass_rate`` already excludes ``pass_fail is None``.

    The bool path is exactly ``float(pass_fail)``, so a board whose entries
    are all bool produces a per-entry score sequence identical to its
    pass/fail sequence. ``mean_score`` over that sequence therefore equals
    the binary ``pass_rate`` byte-for-byte — the property the all-bool
    back-compat proof test pins.

    Reads ``score`` via ``getattr`` so a duck-typed loss stand-in (or a
    profile materialised before the field existed) that carries only
    ``pass_fail`` still resolves to the binary outcome.
    """
    raw_score = getattr(loss, "score", None)
    if raw_score is not None:
        value = float(raw_score)
        if not math.isfinite(value):
            return 0.0
        if value < 0.0:
            return 0.0
        if value > 1.0:
            return 1.0
        return value
    if loss.pass_fail is None:
        return None
    return 1.0 if loss.pass_fail else 0.0


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


def _per_judge_loss_aggregate(losses: list[LossProfile]) -> dict[str, float]:
    """Mean per-judge ``weighted_loss`` across ``losses``, keyed by judge_name.

    Carried onto :class:`~zicato.scoring.api.ScalarContext` purely for plugin
    / provenance visibility — the built-in scalar does NOT add it separately
    (each judge's contribution is already folded into ``drift_loss`` by the
    reducer, hence into ``drift_loss_mean``). Summing each judge's per-run
    ``weighted_loss`` and dividing by the run count mirrors the per-run mean
    model the rest of the aggregation uses; a judge absent from a run
    contributes zero to its sum. Returns ``{}`` for empty input.
    """
    if not losses:
        return {}
    sums: dict[str, float] = {}
    for loss in losses:
        # ``getattr`` (not attribute access) so a duck-typed loss stand-in —
        # the projected-standings ``_FakeLoss``, or a profile materialised
        # before the field existed — that carries no ``per_judge_loss`` is
        # tolerated, mirroring :func:`entry_score`'s defensive ``getattr``.
        for jl in getattr(loss, "per_judge_loss", ()) or ():
            sums[jl.judge_name] = sums.get(jl.judge_name, 0.0) + jl.weighted_loss
    n = len(losses)
    return {name: total / n for name, total in sums.items()}


def aggregate_generation_score(
    losses: list[LossProfile],
    weights: ScoringWeights,
    diff_size: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Aggregate per-entry losses into a per-generation summary dict.

    See module docstring for the dict shape. Empty input returns
    ``drift_loss_mean=0.0``, ``pass_rate=1.0``, both counts zero, and
    ``scalar=0.0`` — equivalent to "nothing to compare", which the gate
    treats as a tie (no improvement).

    ``diff_size`` is the OPT-IN parsimony / MDL input: the candidate
    generation's ``{added, removed, patches}`` diff size (see
    :func:`zicato.scoring.diff_complexity.diff_size`). ``None`` (the default)
    or a contract whose :attr:`~zicato.core.types.ScoringWeights.diff_complexity_weight`
    is ``0.0`` leaves the result BYTE-IDENTICAL — the ``diff_complexity``
    component is never written and the scalar is unchanged. The runner threads
    it only for the CHALLENGER side; the champion side passes ``None`` so the
    term measures the challenger's diff, exactly as the gate compares it against
    a champion baseline that pays no parsimony cost.
    """
    per_entry: dict[str, dict[str, Any]] = {}
    drift_total = 0.0
    pass_count = 0
    expectation_count = 0
    score_total = 0.0
    score_count = 0

    for loss in losses:
        drift = per_run_drift_loss(loss, weights)
        entry_outcome = entry_score(loss)
        per_entry[loss.entry_id] = {
            "drift_loss": drift,
            "pass_fail": loss.pass_fail,
            # The continuous per-entry outcome the gate's per_entry scope
            # reads. ``None`` for an entry with no expectation; a bool
            # entry carries 1.0/0.0 so the gate can treat bool and float
            # entries uniformly.
            "score": entry_outcome,
        }
        drift_total += drift
        if loss.pass_fail is not None:
            expectation_count += 1
            if loss.pass_fail:
                pass_count += 1
        if entry_outcome is not None:
            score_total += entry_outcome
            score_count += 1

    entry_count = len(losses)
    drift_loss_mean = drift_total / entry_count if entry_count > 0 else 0.0
    pass_rate = pass_count / expectation_count if expectation_count > 0 else 1.0
    # ``mean_score`` is the UNIFORM outcome axis: the arithmetic mean of each
    # entry's continuous :func:`entry_score` over every entry that produced
    # one (``score_count``). On an all-bool board every entry with a
    # pass/fail also produces a score and every entry without one produces
    # neither, so ``score_count == expectation_count`` AND
    # ``score_total == pass_count`` — hence ``mean_score == pass_rate``
    # byte-for-byte. That is the back-compat proof: substituting mean_score
    # for pass_rate in the pass component below is a no-op on all-bool
    # boards. A board with no scored entries reports 1.0, exactly as
    # pass_rate does, so the (1 - mean_score) term contributes zero.
    mean_score = score_total / score_count if score_count > 0 else 1.0

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
    # The pass component runs on the UNIFORM mean_score, not the binary
    # pass_rate. On an all-bool board mean_score == pass_rate (see above),
    # so this is byte-identical to the historical
    # ``pass_weight * (1 - pass_rate)`` term; on a board with continuous
    # scores it tracks the graded quality with no threshold cliff.
    pass_component = weights.pass_weight * (1.0 - mean_score)

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

    # Parsimony / MDL term (OVERFITTING.md §5 / §12 #4), appended LAST and only
    # when opted in. The component value comes from the SAME seam
    # :func:`builtin_scalar` uses, so the surfaced component and the appended
    # scalar term can never disagree. ``None`` (default weight 0.0 / no diff
    # size) ⇒ the key is never written, so ``scalar_components`` and the scalar
    # are byte-identical to the pre-feature path.
    diff_component = diff_complexity_component(weights, diff_size)
    if diff_component is not None:
        scalar_components["diff_complexity"] = diff_component

    # Seam 2 (issue #19 phase 1): synthesise the scalar through the scoring
    # dispatcher. The built-in formula (drift + pass + non-drift namespaces)
    # is byte-identical to ``sum(scalar_components.values())`` — the golden
    # test pins that — and the dispatcher returns it with a ``"builtin"``
    # provenance in this phase. ``scalar_components`` is still computed above
    # for the display / gate breakdown; the dispatcher owns the scalar value
    # the later phases (transforms / plugins) hook into.
    scalar, scalar_provenance = resolve_scalar(
        ScalarContext(
            pass_rate=pass_rate,
            mean_score=mean_score,
            drift_loss_mean=drift_loss_mean,
            namespace_aggregates=namespace_aggregates,
            per_judge_loss=_per_judge_loss_aggregate(losses),
            weights=weights,
            builtin_scalar=builtin_scalar(
                mean_score=mean_score,
                drift_loss_mean=drift_loss_mean,
                namespace_aggregates=namespace_aggregates,
                weights=weights,
                diff_size=diff_size,
            ),
            diff_size=diff_size,
        )
    )

    agg: dict[str, Any] = {
        "drift_loss_mean": drift_loss_mean,
        "pass_rate": pass_rate,
        # The uniform continuous outcome the scalar's pass component and
        # the gate's aggregate scope read. Equals pass_rate on an all-bool
        # board; kept alongside pass_rate so display and the per_entry gate
        # scope still have the binary view.
        "mean_score": mean_score,
        "expectation_count": expectation_count,
        "entry_count": entry_count,
        "scalar": scalar,
        "per_entry": per_entry,
        "namespace_aggregates": namespace_aggregates,
        "scalar_components": scalar_components,
        # Which scoring path produced ``scalar`` (issue #19). PHASE 1:
        # always ``"builtin"``; later phases enrich it. Additive — callers
        # that don't read it are unaffected.
        "scalar_provenance": scalar_provenance,
    }
    # Echo the candidate diff size onto the aggregate ONLY when the
    # diff-complexity term is actually active, so the gate / dashboard can
    # surface ``diff_size:challenger:{added,removed,patches}`` evidence. At the
    # default weight (or with no diff size) the key is ABSENT, so the returned
    # dict — and therefore the serialised ``gen_score.json`` golden — is
    # byte-identical to the pre-feature aggregate.
    if diff_component is not None and diff_size is not None:
        agg["diff_size"] = dict(diff_size)
    return agg


__all__ = [
    "aggregate_generation_score",
    "aggregate_namespaced_metrics",
    "entry_score",
    "per_run_drift_loss",
]
