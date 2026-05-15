"""zicato.tournament — generation scoring, runner, and the promote gate.

This subpackage owns the post-run side of the loop:

* :mod:`zicato.tournament.scoring` aggregates per-run
  :class:`~zicato.core.LossProfile` instances into a per-generation
  summary (drift-loss mean, pass rate, combined scalar) under a frozen
  :class:`~zicato.core.ScoringWeights`.
* :mod:`zicato.tournament.gate` applies the promote gate: a minimum
  scalar-margin requirement combined with strict pass-rate
  monotonicity on pre-existing entries.
* :mod:`zicato.tournament.runner` orchestrates the actual A/B (full
  mode) or A-against-historical-aggregate (fast mode) tournament, by
  driving the inner harness via a :class:`HarnessAdapter` and pulling
  reduced loss profiles through the lazily-imported
  :mod:`zicato.telemetry` layer.

The runner LAZY-imports :mod:`zicato.telemetry` so this package keeps
loading cheaply (and is testable) even before the telemetry layer is
wired up — tests in this subpackage stub the adapter and inject canned
loss profiles directly.
"""

from __future__ import annotations

from zicato.tournament.gate import GateOutcome, evaluate_gate
from zicato.tournament.runner import TournamentResult, run_fast_mode, run_tournament
from zicato.tournament.scoring import (
    aggregate_generation_score,
    per_run_drift_loss,
)

__all__ = [
    "GateOutcome",
    "TournamentResult",
    "aggregate_generation_score",
    "evaluate_gate",
    "per_run_drift_loss",
    "run_fast_mode",
    "run_tournament",
]
