"""zicato.patterns — cross-run aggregator detectors.

The patterns layer reads windows of :class:`zicato.core.LossProfile`
instances (one per ``(entry, generation)`` over some history) and emits
:class:`zicato.core.Pattern` records that the proposer consumes to decide
which mutation points to address next.

Detectors are deliberately small, statistical, and explainable: each one
answers a single question ("which drift kind dominates?", "which task is
disproportionately failing?", "which entries show plan-revision
flapping?") and emits one Pattern per finding with a stable id derived
from the kind + summary + affected ids.

Patterns flow proposer-ward only — they never carry executable code, and
they leave ``affected_mutation_ids`` empty (the proposer resolves a
pattern into specific mutation points; see :mod:`zicato.core.types`).
"""

from __future__ import annotations

from zicato.patterns.detectors import (
    ALL_DETECTORS,
    DetectorFn,
    DetectorInput,
    detect_drift_kind_frequency,
    detect_hot_agents,
    detect_hot_tasks,
    detect_multi_turn_context_loss,
    detect_multi_turn_memory_failure,
    detect_patterns,
    detect_plan_revision_instability,
)
from zicato.patterns.registry import get_all_detectors, register_detector

__all__ = [
    "ALL_DETECTORS",
    "DetectorFn",
    "DetectorInput",
    "detect_drift_kind_frequency",
    "detect_hot_agents",
    "detect_hot_tasks",
    "detect_multi_turn_context_loss",
    "detect_multi_turn_memory_failure",
    "detect_patterns",
    "detect_plan_revision_instability",
    "get_all_detectors",
    "register_detector",
]
