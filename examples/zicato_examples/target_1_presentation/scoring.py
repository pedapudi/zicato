"""Operator-owned SCORING PLUGINS for the target_1_presentation board.

# zicato:grading — operator-owned scoring; never a proposer mutation point.

These are the issue-#19 phase-3 dotted-spec scoring plugins: pure, deterministic,
NO-LLM functions the operator references from ``scoring.json`` via::

    "drift_reducer": "zicato_examples.target_1_presentation.scoring:harmonic_looping_reducer",
    "scalar_fn":     "zicato_examples.target_1_presentation.scoring:f_beta_scalar"

Each takes the matching frozen context
(:class:`zicato.scoring.api.DriftContext` / :class:`~zicato.scoring.api.ScalarContext`)
which carries the post-transform value as ``ctx.builtin_*`` — so the plugin WRAPS
the declarative shape rather than reimplementing scoring. zicato invokes them
fail-open: a raise / non-finite return falls back to ``ctx.builtin_*`` (logged +
recorded in provenance), never crashing the run.

Like predicates / judges, this module is operator grading and is NEVER a proposer
mutation point — hence the ``# zicato:grading`` sentinel above, which the
mutation enumerator honours by skipping the whole file.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover — typing-only import
    from zicato.scoring.api import DriftContext, ScalarContext


def harmonic_looping_reducer(ctx: DriftContext) -> float:
    """Reproduce the retired harmonic-looping special-case as a ~10-line plugin.

    The built-in scores each drift kind LINEARLY (``severity × kind_weight ×
    count``). For ``looping_reasoning`` this operator wants DIMINISHING RETURNS:
    the 1st loop hurts fully, the 2nd half as much, the 3rd a third, … (the
    harmonic series ``1 + 1/2 + … + 1/n``). Rather than reimplement the whole
    drift formula, it starts from ``ctx.builtin_loss`` (which already counts the
    looping term linearly), SUBTRACTS that linear looping contribution, and ADDS
    the harmonic one — wrapping the built-in shape. Pure, deterministic, no I/O.
    """
    loop_count = sum(c.count for c in ctx.drift_counts if c.kind == "looping_reasoning")
    if loop_count <= 0:
        return ctx.builtin_loss
    # The built-in counted ``severity_weight × kind_weight × count`` linearly for
    # looping_reasoning; recover that per-unit weight to subtract it cleanly.
    sev_w = ctx.weights.severity_weights
    kind_w = ctx.weights.per_kind_weights.get("looping_reasoning", 1.0)
    linear_term = sum(
        sev_w.get(c.severity, 0.0) * kind_w * c.count
        for c in ctx.drift_counts
        if c.kind == "looping_reasoning"
    )
    harmonic_term = sum(1.0 / k for k in range(1, int(loop_count) + 1))
    return max(0.0, ctx.builtin_loss - linear_term + harmonic_term)


def f_beta_scalar(ctx: ScalarContext, *, beta: float = 1.0) -> float:
    """An F-beta recall/precision blend the declarative registry can't express.

    The built-in scalar runs its pass component on ``mean_score`` (recall-shaped
    on a continuous board). This operator instead wants the F-beta blend of the
    precision/recall the search board emits into ``namespace_aggregates`` (under
    a ``"recall:"`` / ``"precision:"`` namespace). It WRAPS the built-in: it
    keeps the built-in's drift + namespace contribution by starting from
    ``ctx.builtin_scalar`` and only RESHAPES the pass term — replacing the
    recall-only miss with ``1 - F_beta`` so over-retrieval (low precision) is
    penalised too.

    Falls back to ``ctx.builtin_scalar`` when the precision/recall namespaces are
    absent, so a board that does not emit them scores exactly as the built-in.
    Pure, deterministic, no LLM. ``beta`` defaults to ``1.0`` (plain F1); the
    operator references the bare ``f_beta_scalar`` name (zicato calls it with the
    single ``ctx`` positional), so the default applies unless wrapped.
    """
    ns = ctx.namespace_aggregates
    if "recall:" not in ns or "precision:" not in ns:
        return ctx.builtin_scalar
    recall = float(ns["recall:"])
    precision = float(ns["precision:"])
    denom = (beta * beta * precision) + recall
    if denom <= 0.0:
        f_beta = 0.0
    else:
        f_beta = (1.0 + beta * beta) * precision * recall / denom
    # Reshape ONLY the pass term: built-in used pass_weight*(1 - mean_score);
    # swap in pass_weight*(1 - F_beta), leaving drift + namespaces intact.
    old_pass = ctx.weights.pass_weight * (1.0 - ctx.mean_score)
    new_pass = ctx.weights.pass_weight * (1.0 - f_beta)
    return ctx.builtin_scalar - old_pass + new_pass


__all__ = ["harmonic_looping_reducer", "f_beta_scalar"]
