"""The built-in (default) scoring formulas, extracted as pure functions.

These two functions ARE the current scoring formulas, lifted verbatim out of
``telemetry/reducer.py`` (Seam 1) and ``tournament/scoring.py`` (Seam 2) so
that:

* both the orchestrator AND the killable worker subprocess import the SAME
  implementation (no drift between the two sites), and
* a later plugin can compute the default and adjust it
  (``ctx.builtin_loss`` / ``ctx.builtin_scalar``) rather than reimplement it.

PHASE 1 invariant: these produce results **byte-identical** to the inline
formulas they replaced. The golden test in ``tests/test_scoring_seams.py``
pins that across a representative corpus. Any later phase that changes a
*default* (Phase 2's neutral-by-default transforms) leaves these untouched —
the change rides on top via the dispatcher, never by editing these.

Pure / deterministic / no-LLM / no-I/O / no-wall-clock by contract.
"""

from __future__ import annotations

from collections.abc import Mapping

from zicato.core import DriftCount, ScoringWeights

# ---------------------------------------------------------------------------
# Shared constants (single source of truth, mirrored by the reducer's
# module-level constants so the extraction stayed byte-identical).
# ---------------------------------------------------------------------------

#: Multiplier on ``task_failure_ratio`` inside the drift-loss formula. The
#: contract pinned it constant ("pure failures matter"); see the reducer's
#: ``_TASK_FAILURE_RATIO_MULTIPLIER`` (kept equal — single value).
_TASK_FAILURE_RATIO_MULTIPLIER: float = 10.0


def _kind_multiplier(kind: str, weights: ScoringWeights) -> float:
    """Resolve the kind-/judge-level multiplier for one drift kind.

    Mirrors the reducer's ``_kind_multiplier`` exactly:

    * **first-class kinds** → ``per_kind_weights.get(kind, 1.0)``;
    * **custom-judge kinds** (``custom`` / ``custom:<judge_name>``) →
      ``per_judge_weights.get(judge_name, default_judge_weight)``.

    The split is done locally (rather than importing the reducer) so this
    module has NO dependency on the reducer — the reducer depends on it,
    keeping the seam one-directional and importable from the worker.
    """
    # Inline of reducer.split_judge_attributed_kind to avoid a reducer import.
    if kind == "custom":
        judge_name = ""
        is_custom = True
    elif kind.startswith("custom:"):
        judge_name = kind[len("custom:") :]
        is_custom = True
    else:
        judge_name = ""
        is_custom = False
    if is_custom:
        return weights.per_judge_weights.get(judge_name, weights.default_judge_weight)
    return weights.per_kind_weights.get(kind, 1.0)


def builtin_drift_loss(
    drift_counts: tuple[DriftCount, ...],
    plan_revisions: int,
    task_failure_ratio: float,
    runtime_ms: int,
    weights: ScoringWeights,
) -> float:
    """The built-in per-run drift-loss formula (Seam 1).

    Byte-identical to ``zicato.telemetry.reducer.compute_drift_loss``::

        loss = sum(severity_weights[c.severity] * kind_mult(c.kind) * c.count
                   for c in drift_counts)
             + plan_revision_weight * plan_revisions
             + 10.0 * task_failure_ratio
             + runtime_weight * (runtime_ms / 1000.0)

    clamped to ``max(0.0, loss)``. The not-completed heavy term and the
    ``task_failure_ratio`` floor are applied by the reducer AROUND this call
    (they are reducer policy, not part of the per-run drift formula), so they
    stay where they are — this function is the inner formula only.
    """
    sev_w = weights.severity_weights
    loss = 0.0
    for c in drift_counts:
        sev_mult = sev_w.get(c.severity, 0.0)
        kind_mult = _kind_multiplier(c.kind, weights)
        loss += sev_mult * kind_mult * c.count
    loss += weights.plan_revision_weight * plan_revisions
    loss += _TASK_FAILURE_RATIO_MULTIPLIER * task_failure_ratio
    loss += weights.runtime_weight * (runtime_ms / 1000.0)
    return max(0.0, float(loss))


def builtin_scalar(
    mean_score: float,
    drift_loss_mean: float,
    namespace_aggregates: Mapping[str, float],
    weights: ScoringWeights,
) -> float:
    """The built-in per-generation scalar formula (Seam 2).

    Byte-identical to the scalar composition in
    ``zicato.tournament.scoring.aggregate_generation_score``: builds the
    ``scalar_components`` dict (``"drift"`` + ``"pass"`` + one entry per
    non-``"drift:"`` namespace, keyed by the colon-stripped namespace name)
    and returns ``sum(scalar_components.values())``.

    The summation reproduces the ORIGINAL term order EXACTLY — drift, pass,
    then each namespace in ``namespace_aggregates`` iteration order — because
    float addition is not associative: accumulating the namespaces in a
    different order can flip the last bit of the result. The dict-then-``sum``
    shape is therefore load-bearing for the byte-identical guarantee (the
    golden test pins it), not a stylistic choice. It also mirrors the original
    key-collision behaviour: two namespaces that strip to the same component
    name collapse to the last one written, exactly as before.

    ``namespace_aggregates`` are ALREADY weight-multiplied (see
    :func:`zicato.tournament.scoring.aggregate_namespaced_metrics`), so each
    non-drift namespace value slots straight in. The ``"drift:"`` namespace is
    excluded because the ``drift`` component already owns the drift
    contribution (avoids double-counting), exactly as the inline formula does.

    The pass component runs on the UNIFORM ``mean_score`` (issue #18), which
    equals ``pass_rate`` byte-for-byte on an all-bool board — so this stays
    back-compat-neutral on binary boards while tracking graded quality on
    scored boards.
    """
    drift_component = weights.drift_weight * drift_loss_mean
    pass_component = weights.pass_weight * (1.0 - mean_score)
    scalar_components: dict[str, float] = {
        "drift": drift_component,
        "pass": pass_component,
    }
    for ns, value in namespace_aggregates.items():
        if ns == "drift:":
            continue
        component_name = ns[:-1] if ns.endswith(":") else ns
        scalar_components[component_name] = value
    return sum(scalar_components.values())


__all__ = [
    "builtin_drift_loss",
    "builtin_scalar",
]
