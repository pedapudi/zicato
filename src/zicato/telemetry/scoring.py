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
A custom judge contributes through ``drift_loss`` like any other
drift source — it does not get a separate scalar axis. The reducer
(:func:`zicato.telemetry.reducer.compute_drift_loss`) attributes each
``custom``-kind drift to its authoring judge by the stable
``judge_name`` and weights it by
:attr:`ScoringWeights.per_judge_weights` (falling back to
:attr:`ScoringWeights.default_judge_weight` for an unconfigured
judge). By the time a :class:`LossProfile` reaches this module its
``drift_loss`` already carries the per-judge-weighted custom-judge
contribution, so :func:`aggregate_generation_score` and
:func:`combined_scalar` need no custom-judge-specific arithmetic —
they mean / combine ``drift_loss`` exactly as before.
``per_judge_weights`` enters the pipeline upstream, in the reducer,
not here. ``ScoringWeights`` is still threaded through both functions
unchanged so the call-site surface stays uniform.
"""

from __future__ import annotations

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
    _ = weights  # see docstring — symmetry, not computation
    if not losses:
        return 0.0, 1.0

    drift_loss_mean = sum(p.drift_loss for p in losses) / len(losses)

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

        score = weights.drift_weight * drift_loss_mean
              + weights.pass_weight * (1.0 - pass_rate)

    Lower is better. The two terms are additive rather than
    multiplicative so an epoch can zero out one axis (set
    ``pass_weight=0`` to ignore expectations entirely) without
    obliterating the other. Operators tune
    :attr:`ScoringWeights.drift_weight` and
    :attr:`ScoringWeights.pass_weight` per epoch; defaults are equal
    (1.0 / 1.0) which keeps the two axes commensurate during early
    dogfood.
    """
    return weights.drift_weight * drift_loss_mean + weights.pass_weight * (1.0 - pass_rate)


__all__ = ["aggregate_generation_score", "combined_scalar"]
