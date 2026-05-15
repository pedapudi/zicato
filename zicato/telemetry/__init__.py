"""zicato.telemetry — per-run sink wiring + post-run loss reducer + scoring.

This package owns the *capture* and *reduction* half of zicato's telemetry
path:

* :mod:`zicato.telemetry.sink` constructs a per-run goldfive
  ``JSONLPersistenceSink`` pointed at the canonical workspace path. The
  goldfive import is deferred to the factory so the wider zicato install
  stays usable when the optional ``goldfive`` extra is not present at
  module load.
* :mod:`zicato.telemetry.reducer` walks one run's ``events.jsonl`` after
  the run completes and emits a :class:`zicato.core.LossProfile` to
  ``loss.json``. The reducer is a pure function over a JSONL path; it
  produces the same output regardless of who wrote the JSONL, which
  keeps it testable in isolation against fixture transcripts.
* :mod:`zicato.telemetry.scoring` aggregates a list of
  :class:`LossProfile` instances into the per-generation tournament
  scalar.

The split keeps the *contract surface* (LossProfile, ScoringWeights)
exposed via :mod:`zicato.core` and the *behavior* (how to write events,
how to read them back, how to weight them) here. Detectors, the
tournament runner, and the CLI consume the contract, not the behavior.

Nothing in this package depends on a particular vendor SDK or
foundational model — the only third-party surface it touches is the
goldfive sink, and that import is deferred so non-telemetry callers
never pay for it.
"""

from __future__ import annotations

from zicato.telemetry.reducer import (
    compute_drift_loss,
    read_loss_profile,
    reduce_loss,
    write_loss_profile,
)
from zicato.telemetry.scoring import (
    aggregate_generation_score,
    combined_scalar,
)
from zicato.telemetry.sink import make_run_sink, make_run_sink_path

__all__ = [
    "make_run_sink",
    "make_run_sink_path",
    "reduce_loss",
    "compute_drift_loss",
    "read_loss_profile",
    "write_loss_profile",
    "aggregate_generation_score",
    "combined_scalar",
]
