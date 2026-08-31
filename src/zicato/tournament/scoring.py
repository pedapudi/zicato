"""Scoring helpers: per-run drift loss and per-generation aggregation.

Exact aggregation
-----------------
Every float total on this path is computed with :func:`math.fsum`, never
with the builtin ``sum`` and never with a running ``+=`` accumulator.
``math.fsum`` returns the correctly-rounded exact sum, which makes an
aggregate a function of its inputs alone: independent of the order the
terms arrive in, and — the reason this is an invariant rather than a
preference — independent of the interpreter. CPython 3.12 changed the
builtin ``sum`` over floats to compensated summation, so the same board
scored under 3.11 and 3.12 produced ``0.39999999999999997`` and ``0.4``
for the same inputs. These values are served, compared against contract
margins, and frozen byte-for-byte in the parity goldens; none of that may
depend on which interpreter ran the round.

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
* ``per_entry`` — ``{entry_id: {"drift_loss": float, "failure": float,
  "pass_fail": bool|None, "score": float|None}}`` for entry-level deltas the
  gate needs. ``score`` is the per-entry :func:`entry_score` the gate's
  per-entry continuous-monotonicity scope reads; ``failure`` is the entry's
  ``failure:`` channel total, which is what explains an aborted unit whose
  ``drift_loss`` is legitimately ``0.0``.
* ``namespace_aggregates`` — ``{namespace: weighted_aggregate}`` for
  every namespace observed in the inputs or named in
  :attr:`ScoringWeights.namespace_weights`. Each value is the
  namespace's per-run mean already multiplied by its weight, so it is
  ready to add directly into the scalar.
* ``scalar_components`` — ``{component_name: contribution}`` whose
  values sum exactly to ``scalar``: ``"pass"`` (the ``(1 - mean_score)``
  term, which equals ``(1 - pass_rate)`` on an all-bool board) plus one
  entry per namespace, keyed by the colon-stripped namespace
  name and written in sorted namespace order. When the opt-in
  diff-complexity term is active (``diff_complexity_weight > 0`` AND a
  ``diff_size`` was threaded for the candidate) a final ``"diff_complexity"``
  entry is appended; at the default weight ``0.0`` the key is absent.

The aggregation is intentionally cheap and deterministic; it does NOT
re-derive ``drift_loss`` from raw drift counts (that derivation lives
in the telemetry reducer). :func:`per_run_drift_loss` is exposed as
the canonical hook for callers who want to re-derive; it returns
``loss.drift_loss``. Should the telemetry reducer's formula ever need to
diverge from the tournament's view, the divergence is bounded to this
single function.
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


def _within_channel_weight(metric_name: str, weights: ScoringWeights) -> float:
    """Return the within-channel multiplier for one metric, ``1.0`` by default.

    The ``failure:`` channel's two members carry contract magnitudes the way
    ``drift:`` carries ``severity_weights × per_kind_weights`` (applied inside
    ``drift_loss``) and ``judge:`` carries ``per_judge_weights`` (applied
    inside ``per_judge_loss.weighted_loss``). Both live on the contract, so
    retuning either rolls the epoch.

    Every other metric enters its channel at its measured value; the channel's
    ``namespace_weights`` coefficient is what scales it.
    """
    if metric_name == "failure:tasks":
        return weights.task_failure_weight
    if metric_name == "failure:not_completed":
        return weights.not_completed_weight
    return 1.0


def _failure_channel_total(loss: LossProfile, weights: ScoringWeights) -> float:
    """Return one run's ``failure:`` channel total, before its coefficient.

    ``task_failure_weight × task_failure_ratio + not_completed_weight`` (the
    latter only for a run that did not complete) — the same two members
    :meth:`LossProfile.unified_metrics` derives and
    :func:`aggregate_namespaced_metrics` sums, computed per entry so the
    evidence surface can explain an aborted unit's contribution.

    Reads both fields via ``getattr`` so a duck-typed loss stand-in — the
    projected-standings placeholder, or a profile materialised before the
    fields existed — resolves to "completed, nothing failed", mirroring
    :func:`entry_score`'s defensive read.
    """
    total = weights.task_failure_weight * float(getattr(loss, "task_failure_ratio", 0.0) or 0.0)
    if getattr(loss, "not_completed", False):
        total += weights.not_completed_weight
    return total


def aggregate_namespaced_metrics(
    losses: list[LossProfile],
    weights: ScoringWeights,
) -> dict[str, float]:
    """Compute the weighted per-namespace aggregate across ``losses``.

    For the ``"drift:"`` namespace the aggregate is
    ``namespace_weights["drift:"] * mean(LossProfile.drift_loss)``: drift is
    reduced per run into ``drift_loss`` (Seam 1) and enters the scalar as one
    channel, not as its per-``(kind, severity)`` buckets.

    For every other namespace the aggregate is
    ``namespace_weights[namespace] * mean(Σ within-channel-weighted
    MetricCount.count)`` over the namespace's entries across all losses'
    unified metric view (see :meth:`LossProfile.unified_metrics`, which
    derives the ``judge:`` / ``failure:`` / ``runtime:`` members).
    Namespaces named in :attr:`ScoringWeights.namespace_weights` but absent
    from the loss data appear with an aggregate of ``0.0`` so the keys are
    predictable for downstream consumers.

    Unnamespaced metric names (no colon prefix) are silently ignored.
    Namespaces present in the data but with no weight configured are
    aggregated at weight ``0.0`` (so they show up as ``0.0`` in the
    return value and contribute nothing to the scalar).

    Returns ``{namespace: weighted_aggregate}``. The mapping keys
    preserve the trailing colon (``"drift:"``, ``"cost:"``, ...).
    """
    namespace_means: dict[str, float] = {}

    if losses:
        # ``math.fsum``, never the builtin: see the module docstring on
        # exact aggregation. This mean reaches every served scalar, so a
        # summation whose result depends on the interpreter version would
        # make the parity goldens depend on it too.
        drift_total = math.fsum(per_run_drift_loss(loss, weights) for loss in losses)
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
    # Terms are collected per namespace and summed ONCE with ``math.fsum``
    # rather than accumulated in a running float, so the aggregate does not
    # depend on the order the losses arrive in.
    terms: dict[str, list[float]] = {}
    n_losses = len(losses)
    for loss in losses:
        # Sum within one loss first so a loss with multiple entries in
        # the same namespace counts each entry, and a loss with none
        # contributes zero.
        per_loss: dict[str, list[float]] = {}
        for mc in loss.unified_metrics():
            ns = _namespace_of(mc.name)
            if not ns or ns == "drift:":
                # Drift is already reduced into ``drift_loss`` above; its
                # MetricCount mirrors — including the ``drift:custom``
                # judge-attributed ones the judge: channel scores — are
                # skipped here so nothing is counted twice.
                continue
            per_loss.setdefault(ns, []).append(mc.count * _within_channel_weight(mc.name, weights))
        for ns, values in per_loss.items():
            terms.setdefault(ns, []).append(math.fsum(values))
    sums: dict[str, float] = {ns: math.fsum(values) for ns, values in terms.items()}

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
    (the same values reach it as the ``judge:<name>`` metrics of the
    ``judge:`` channel). Summing each judge's per-run
    ``weighted_loss`` and dividing by the run count mirrors the per-run mean
    model the rest of the aggregation uses; a judge absent from a run
    contributes zero to its sum. Returns ``{}`` for empty input.
    """
    if not losses:
        return {}
    terms: dict[str, list[float]] = {}
    for loss in losses:
        # ``getattr`` (not attribute access) so a duck-typed loss stand-in —
        # the projected-standings ``_FakeLoss``, or a profile materialised
        # before the field existed — that carries no ``per_judge_loss`` is
        # tolerated, mirroring :func:`entry_score`'s defensive ``getattr``.
        for jl in getattr(loss, "per_judge_loss", ()) or ():
            terms.setdefault(jl.judge_name, []).append(jl.weighted_loss)
    n = len(losses)
    return {name: math.fsum(values) / n for name, values in terms.items()}


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
    # Collected, then summed once with ``math.fsum`` — see the module
    # docstring on exact aggregation. Counts stay integer accumulators;
    # only the float terms need the exact sum.
    drift_terms: list[float] = []
    score_terms: list[float] = []
    pass_count = 0
    expectation_count = 0

    for loss in losses:
        drift = per_run_drift_loss(loss, weights)
        entry_outcome = entry_score(loss)
        per_entry[loss.entry_id] = {
            "drift_loss": drift,
            # The entry's within-channel ``failure:`` total, the analogue of
            # ``drift_loss`` for the failure channel (both are pre-namespace-
            # coefficient). Without it an aborted unit shows an empty
            # ``drift_loss`` of 0.0 and nothing explaining the loss it
            # contributed.
            "failure": _failure_channel_total(loss, weights),
            "pass_fail": loss.pass_fail,
            # The continuous per-entry outcome the gate's per_entry scope
            # reads. ``None`` for an entry with no expectation; a bool
            # entry carries 1.0/0.0 so the gate can treat bool and float
            # entries uniformly.
            "score": entry_outcome,
        }
        drift_terms.append(drift)
        if loss.pass_fail is not None:
            expectation_count += 1
            if loss.pass_fail:
                pass_count += 1
        if entry_outcome is not None:
            score_terms.append(entry_outcome)

    entry_count = len(losses)
    score_count = len(score_terms)
    drift_loss_mean = math.fsum(drift_terms) / entry_count if entry_count > 0 else 0.0
    pass_rate = pass_count / expectation_count if expectation_count > 0 else 1.0
    # ``mean_score`` is the UNIFORM outcome axis: the arithmetic mean of each
    # entry's continuous :func:`entry_score` over every entry that produced
    # one (``score_count``). On an all-bool board every entry with a
    # pass/fail also produces a score and every entry without one produces
    # neither, so ``score_count == expectation_count`` AND
    # the score terms sum to ``pass_count`` — hence ``mean_score == pass_rate``
    # byte-for-byte. That is the back-compat proof: substituting mean_score
    # for pass_rate in the pass component below is a no-op on all-bool
    # boards. A board with no scored entries reports 1.0, exactly as
    # pass_rate does, so the (1 - mean_score) term contributes zero.
    mean_score = math.fsum(score_terms) / score_count if score_count > 0 else 1.0

    # Namespace aggregates are already weight-multiplied per
    # :func:`aggregate_namespaced_metrics` — they slot straight into
    # the scalar.
    namespace_aggregates = aggregate_namespaced_metrics(losses, weights)

    # The pass component runs on the UNIFORM mean_score rather than the binary
    # pass_rate. On an all-bool board mean_score == pass_rate (see above), so
    # this equals ``pass_weight * (1 - pass_rate)``; on a board with continuous
    # scores it tracks the graded quality with no threshold cliff. It is the
    # scalar's only non-namespace term — see :func:`builtin_scalar` for why
    # pass is not a channel.
    pass_component = weights.pass_weight * (1.0 - mean_score)

    scalar_components: dict[str, float] = {"pass": pass_component}
    # SORTED, mirroring :func:`builtin_scalar`: float addition is not
    # associative, so summing the channels in mapping-iteration order would
    # make the scalar's last bit depend on hash seeding.
    for ns in sorted(namespace_aggregates):
        # Strip the trailing colon for human-readable component names.
        component_name = ns[:-1] if ns.endswith(":") else ns
        scalar_components[component_name] = namespace_aggregates[ns]

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
    # is byte-identical to ``fsum(scalar_components.values())`` — the golden
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
    # Echo the candidate diff size onto the aggregate when EITHER half of the
    # diff-complexity regularizer is active — the loss-term weight (``> 0`` ⇒
    # ``diff_component is not None``) OR the opt-in parsimony CEILING
    # (``diff_complexity_ceiling > 0``, which the gate's Rule 0 reads off this
    # key). This lets the gate / dashboard surface
    # ``diff_size:challenger:{added,removed,patches}`` evidence and lets the
    # ceiling see the diff size even when the loss weight is off. At BOTH
    # defaults (weight 0.0 and ceiling 0.0), or with no diff size, the key is
    # ABSENT, so the returned dict — and therefore the serialised
    # ``gen_score.json`` golden — is byte-identical to the pre-feature
    # aggregate.
    parsimony_active = diff_component is not None or weights.diff_complexity_ceiling > 0.0
    if parsimony_active and diff_size is not None:
        agg["diff_size"] = dict(diff_size)
    return agg


__all__ = [
    "aggregate_generation_score",
    "aggregate_namespaced_metrics",
    "entry_score",
    "per_run_drift_loss",
]
