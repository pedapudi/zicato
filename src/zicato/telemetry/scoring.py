"""Per-generation aggregation of :class:`LossProfile` instances.

The reducer (in :mod:`zicato.telemetry.reducer`) emits one
:class:`LossProfile` per run. A generation runs every entry on the
board once, so a generation produces N profiles where N == board size.
This module rolls those profiles up into the two scalars the
tournament gate reads:

* ``drift_loss_mean`` — arithmetic mean of per-run drift loss across
  the generation.
* ``pass_rate`` — fraction of runs with a recorded pass/fail outcome
  that passed. Runs without an expectation are skipped (they cannot
  pass or fail in a ground-truth sense); a generation with zero
  expectations reports pass-rate ``1.0`` so the ``(1 - pass_rate)``
  term in :func:`combined_scalar` contributes zero rather than
  bottoming out.

The combined scalar is the tournament-side quantity the gate compares
across generations: lower is better, by construction.

Custom-judge signal
-------------------
Custom judges are NOT in ``drift_loss``: the reducer
(:func:`zicato.telemetry.reducer.compute_per_judge_loss`) attributes each
``custom``-kind drift to its authoring judge by the stable ``judge_name``,
weights it by :attr:`ScoringWeights.per_judge_weights` (falling back to
:attr:`ScoringWeights.default_judge_weight` for an unconfigured judge), and
records the split on the profile, from which it reaches the scalar as the
``judge:`` channel. The two-axis :func:`combined_scalar` here therefore does
not see judge signal at all — one of the reasons it is a projection rather
than the scalar. ``ScoringWeights`` is threaded through both functions so
the call-site surface stays uniform.
"""

from __future__ import annotations

import math

from zicato.core import LossProfile, ScoringWeights


def aggregate_generation_score(
    losses: list[LossProfile],
    weights: ScoringWeights,
) -> tuple[float, float]:
    """Return ``(drift_loss_mean, pass_rate)`` across a generation's profiles.

    ``drift_loss_mean`` is the arithmetic mean of
    :attr:`LossProfile.drift_loss` over every profile in ``losses``.
    When ``losses`` is empty the mean is ``0.0`` — there is nothing to
    score, and a zero contribution to the combined scalar is the right
    neutral.

    ``pass_rate`` is ``passed / observed`` where ``observed`` counts
    only profiles whose :attr:`pass_fail` is not ``None``. When no
    profile has an expectation, the pass-rate axis cannot be evaluated;
    we return ``1.0`` so the ``(1 - pass_rate)`` term in
    :func:`combined_scalar` is zero — i.e. boards without expectations
    score on drift loss alone, which matches the scoring contract:
    "a generation can only beat its parent on pass-rate if predicates
    exist on the board."

    The ``weights`` argument is intentionally accepted but unused at
    aggregation time — weights apply at the combine step. Threading
    them in here keeps the call-site symmetry between
    ``aggregate_generation_score`` and :func:`combined_scalar` even
    when the implementation has nothing to do with them.
    """
    _ = weights  # see docstring — symmetry rather than computation
    if not losses:
        return 0.0, 1.0

    # ``math.fsum``: this mean is served and captured in the parity goldens,
    # so it must not depend on the interpreter's float-summation strategy.
    drift_loss_mean = math.fsum(p.drift_loss for p in losses) / len(losses)

    passes = 0
    observed = 0
    for p in losses:
        if p.pass_fail is None:
            continue
        observed += 1
        if p.pass_fail:
            passes += 1
    if observed == 0:
        pass_rate = 1.0
    else:
        pass_rate = passes / observed
    return drift_loss_mean, pass_rate


def combined_scalar(
    drift_loss_mean: float,
    pass_rate: float,
    weights: ScoringWeights,
) -> float:
    """Combine drift-loss mean and pass-rate into the tournament scalar.

    Formula::

        score = namespace_weights["drift:"] * drift_loss_mean
              + weights.pass_weight * (1.0 - pass_rate)

    Lower is better. The two terms are additive rather than
    multiplicative so an epoch can zero out one axis (set
    ``pass_weight=0`` to ignore expectations entirely) without
    obliterating the other.

    This is the TWO-AXIS view: drift and pass only. The full composition
    (:func:`zicato.scoring.builtins.builtin_scalar`) sums every measured
    channel — judges, failures, runtime, cost, latency, rubric, schema —
    and needs the per-run metric view this signature does not carry. Use it
    where only the two aggregate axes are available, and read the two
    numbers as a projection of the scalar rather than the scalar itself.
    """
    drift_weight = weights.namespace_weights.get("drift:", 0.0)
    return drift_weight * drift_loss_mean + weights.pass_weight * (1.0 - pass_rate)


__all__ = ["aggregate_generation_score", "combined_scalar"]
