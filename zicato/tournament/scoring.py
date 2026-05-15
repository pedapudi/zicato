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
* ``scalar`` — ``drift_weight * drift_loss_mean + pass_weight * (1 -
  pass_rate)``. Lower = better.
* ``per_entry`` — ``{entry_id: {"drift_loss": float, "pass_fail":
  bool|None}}`` for entry-level deltas the gate needs.

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
    scalar = (
        weights.drift_weight * drift_loss_mean
        + weights.pass_weight * (1.0 - pass_rate)
    )

    return {
        "drift_loss_mean": drift_loss_mean,
        "pass_rate": pass_rate,
        "expectation_count": expectation_count,
        "entry_count": entry_count,
        "scalar": scalar,
        "per_entry": per_entry,
    }


__all__ = [
    "aggregate_generation_score",
    "per_run_drift_loss",
]
