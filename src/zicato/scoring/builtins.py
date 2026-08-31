"""The built-in (default) scoring formulas, as pure functions.

These two functions ARE zicato's scoring formulas, held here rather than
inline in ``telemetry/reducer.py`` (Seam 1) and ``tournament/scoring.py``
(Seam 2) so that:

* both the orchestrator AND the killable worker subprocess import the SAME
  implementation (no divergence between the two sites), and
* a plugin can compute the default and adjust it (``ctx.builtin_loss`` /
  ``ctx.builtin_scalar``) rather than reimplement it.

``tests/test_scoring_seams.py`` holds a deliberately independent second
implementation of both formulas and pins the two against each other across a
representative corpus; a change here that is not mirrored there is a test
failure by design. Transforms and plugins ride ON TOP via the dispatcher
(:mod:`zicato.scoring.dispatch`), never by editing these.

Pure / deterministic / no-LLM / no-I/O / no-wall-clock by contract.
"""

from __future__ import annotations

import math
from collections.abc import Mapping

from zicato.core import DriftCount, ScoringWeights


def is_judge_attributed_kind(kind: str) -> bool:
    """Return ``True`` for a custom-judge drift kind (``custom`` / ``custom:x``).

    Judge-attributed drift is scored in the ``judge:`` channel, off the
    profile's ``per_judge_loss`` split, so it is EXCLUDED from the drift
    channel here — the two must never both charge for the same event.

    The test is done locally (rather than importing the reducer's
    ``split_judge_attributed_kind``) so this module has NO dependency on the
    reducer — the reducer depends on it, keeping the seam one-directional and
    importable from the worker.
    """
    return kind == "custom" or kind.startswith("custom:")


def _kind_multiplier(kind: str, weights: ScoringWeights) -> float:
    """Resolve the per-kind multiplier for one drift kind.

    ``per_kind_weights.get(kind, 1.0)`` — an unknown kind falls back to
    ``1.0``. Custom-judge kinds never reach here (their callers skip them via
    :func:`is_judge_attributed_kind`); ``per_kind_weights["custom"]`` is
    rejected at contract load for the same reason.
    """
    return weights.per_kind_weights.get(kind, 1.0)


def builtin_drift_loss(
    drift_counts: tuple[DriftCount, ...],
    plan_revisions: int,
    weights: ScoringWeights,
) -> float:
    """The built-in per-run drift-loss formula (Seam 1) — the ``drift:`` channel.

    Byte-identical to ``zicato.telemetry.reducer.compute_drift_loss``::

        loss = fsum(severity_weights[c.severity] * per_kind_weights(c.kind) * c.count
                    for c in drift_counts if not judge-attributed)
             + plan_revision_weight * plan_revisions

    clamped to ``max(0.0, loss)``. Drift EVENTS only: the run-outcome facts
    (task failures, a run that did not complete) are the ``failure:`` channel
    and wall-clock is the ``runtime:`` channel, both derived from the profile
    by :meth:`zicato.core.LossProfile.unified_metrics`. Plan revisions stay
    here because they are the same telemetry stream — an adapter that emits no
    plan-revision events contributes exactly zero.

    The ``task_failure_ratio`` floor for a not-completed run is applied by the
    reducer (it is reducer policy, not part of this formula), and the
    not-completed magnitude is charged in the failure channel.
    """
    sev_w = weights.severity_weights
    # ``math.fsum`` over collected terms, never a running float: this term
    # reaches every served scalar and every parity golden, and both the
    # builtin ``sum`` (whose float behaviour changed in Python 3.12) and a
    # running accumulator make the result depend on something other than the
    # inputs — the interpreter version, or the order the counts arrive in.
    terms = [
        sev_w.get(c.severity, 0.0) * _kind_multiplier(c.kind, weights) * c.count
        for c in drift_counts
        if not is_judge_attributed_kind(c.kind)
    ]
    terms.append(weights.plan_revision_weight * plan_revisions)
    return max(0.0, math.fsum(terms))


def builtin_scalar(
    mean_score: float,
    namespace_aggregates: Mapping[str, float],
    weights: ScoringWeights,
    diff_size: Mapping[str, int] | None = None,
) -> float:
    """The built-in per-generation scalar formula (Seam 2).

    Byte-identical to the scalar composition in
    ``zicato.tournament.scoring.aggregate_generation_score``: builds the
    ``scalar_components`` dict (``"pass"`` + one entry per namespace, keyed by
    the colon-stripped namespace name) and returns
    ``fsum(scalar_components.values())``. The composition has exactly two kinds
    of term::

        scalar = pass_weight * (1 - mean_score)
               + Σ over SORTED namespaces of namespace_aggregates[ns]
               + diff_complexity term (when configured)

    ``pass`` is deliberately NOT a namespace: it runs on a different
    denominator (expectation-bearing entries, not every entry), it has its own
    monotonicity mechanism (``pass_rate_monotonicity_scope``), and the
    transform seam reads it as a bounded coefficient. Every MEASURED channel —
    drift, judges, failures, runtime, cost, latency, rubric, output, schema —
    is a namespace, with no privileged term among them.

    The namespace sum is SORTED because float addition is not associative:
    accumulating in ``dict``/``set`` iteration order would make the last bit of
    the scalar depend on hash seeding. Sorting makes the result reproducible
    across processes, which the goldens and the two statistical oracles pin.
    The dict-then-``sum`` shape also keeps the original key-collision
    behaviour: two namespaces that strip to the same component name collapse to
    the last one written.

    ``namespace_aggregates`` are ALREADY weight-multiplied (see
    :func:`zicato.tournament.scoring.aggregate_namespaced_metrics`), so each
    value slots straight in.

    The pass component runs on the UNIFORM ``mean_score`` (issue #18), which
    equals ``pass_rate`` byte-for-byte on an all-bool board — so this stays
    back-compat-neutral on binary boards while tracking graded quality on
    scored boards.

    ``diff_size`` is the OPT-IN parsimony / MDL term (OVERFITTING.md §5 / §12
    #4). It is the challenger's ``{added, removed, patches}`` diff size, or
    ``None`` for a side with no challenger experiment. The
    ``diff_complexity`` component is appended LAST — in a fixed position after
    every namespace — and ONLY when ``weights.diff_complexity_weight > 0.0``
    AND ``diff_size`` is not ``None``; otherwise the term is EXACTLY absent (no
    key, no addition), so the default ``diff_complexity_weight == 0.0`` leaves
    the scalar byte-identical to a contract without the field.
    """
    pass_component = weights.pass_weight * (1.0 - mean_score)
    scalar_components: dict[str, float] = {"pass": pass_component}
    for ns in sorted(namespace_aggregates):
        component_name = ns[:-1] if ns.endswith(":") else ns
        scalar_components[component_name] = namespace_aggregates[ns]
    # Parsimony / MDL term, appended LAST and only when opted in. The guard is
    # the byte-identical-when-off contract: at the 0.0 default (or with no diff
    # size) the key is never written, so `fsum(...)` is unchanged.
    diff_component = diff_complexity_component(weights, diff_size)
    if diff_component is not None:
        scalar_components["diff_complexity"] = diff_component
    return math.fsum(scalar_components.values())


def diff_complexity_component(
    weights: ScoringWeights,
    diff_size: Mapping[str, int] | None,
) -> float | None:
    """Return the diff-complexity scalar contribution, or ``None`` when absent.

    The single seam both :func:`builtin_scalar` and
    :func:`zicato.tournament.scoring.aggregate_generation_score` read so the
    appended scalar term and the surfaced ``scalar_components`` entry can NEVER
    disagree:

    * ``weights.diff_complexity_weight <= 0.0`` (the default ``0.0``) OR
      ``diff_size is None`` ⇒ ``None`` — the term is exactly absent (no
      component key, nothing added to the scalar), the byte-identical-when-off
      contract.
    * otherwise ⇒ ``diff_complexity_weight * complexity(diff_size)``, where
      ``complexity = added + removed + patches`` (see
      :func:`zicato.scoring.diff_complexity.diff_complexity`).
    """
    if weights.diff_complexity_weight <= 0.0 or diff_size is None:
        return None
    from zicato.scoring.diff_complexity import diff_complexity  # noqa: PLC0415

    return weights.diff_complexity_weight * diff_complexity(diff_size)


__all__ = [
    "builtin_drift_loss",
    "builtin_scalar",
    "diff_complexity_component",
    "is_judge_attributed_kind",
]
