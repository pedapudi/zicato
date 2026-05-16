"""Loop-health diagnostics for the zicato evolve loop.

The evolve loop can run for a long time and still produce *no useful
optimization signal*: two generations score identically, no drift fires,
no expectation differentiates the candidates. When that happens the
operator has burned wall-clock on a toothless evaluation — the loss
surface is flat and the proposer has nothing to climb.

This subsystem turns that failure mode into a first-class, machine-
detectable finding. :func:`zicato.health.diagnostics.assess_loop_health`
runs a battery of detectors over an epoch's losses, experiments, and
board, and returns a :class:`~zicato.health.diagnostics.LoopHealth`
report. The ``zicato health`` CLI prints that report and exits non-zero
when a critical finding is present so a CI / supervisor wrapper notices.

The module does no I/O of its own — every detector is a pure function
over already-loaded data. The CLI is the only piece that touches the
workspace, and it leans on :mod:`zicato.workspace_loader` and the
telemetry reducer's :func:`read_loss_profile` for that.
"""

from __future__ import annotations

from zicato.health.diagnostics import (
    HealthFinding,
    LoopHealth,
    assess_loop_health,
    detect_degenerate_scoring,
    detect_flat_drift_signal,
    detect_no_expectations,
    detect_non_differentiating_entry,
    detect_stalled_loop,
)

__all__ = [
    "HealthFinding",
    "LoopHealth",
    "assess_loop_health",
    "detect_degenerate_scoring",
    "detect_flat_drift_signal",
    "detect_no_expectations",
    "detect_non_differentiating_entry",
    "detect_stalled_loop",
]
