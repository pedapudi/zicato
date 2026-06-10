"""zicato.core — foundational types and workspace helpers.

This package re-exports the foundational dataclasses and helpers that
every other zicato module imports. Downstream code should import from
``zicato.core`` rather than the individual submodules so the surface
stays stable even if internal layout changes::

    from zicato.core import BoardEntry, MutationPoint, LossProfile

The contract is pinned by ``zicato/core/types.py``; the workspace
path-math helpers are in ``zicato/core/workspace.py``; the registered
goldfive drift-kind strings are in ``zicato/core/drift_kinds.py``.
"""

from __future__ import annotations

from zicato.core.drift_kinds import (
    GOLDFIVE_DRIFT_KINDS,
    normalize_wire_drift_kind,
    normalize_wire_severity,
    validate_drift_kind,
)
from zicato.core.types import (
    BUDGET_ABORT_CAUSE,
    BoardEntry,
    BoardEntryKind,
    CallLLM,
    DriftCount,
    DriftDirection,
    DriftMagnitude,
    DriftMovementActual,
    EpochConfig,
    Expectation,
    ExpectationFiresOn,
    ExpectationKind,
    ExpectationResult,
    ExpectedDriftMovement,
    ExpectedMetricMovement,
    Experiment,
    Generation,
    HypothesisSpec,
    JudgeLoss,
    JudgeMode,
    JudgeSpec,
    LossProfile,
    MetricCount,
    MetricMovementActual,
    MetricSeverity,
    MutationKind,
    MutationPoint,
    OutcomeRecord,
    OutputScope,
    PassRateMonotonicityScope,
    Patch,
    PatchOpKind,
    Pattern,
    RunRecord,
    RunResult,
    RuntimeConfig,
    ScoringWeights,
    ScriptedTurn,
    Side,
    TournamentDecision,
    UserPersona,
    is_infra_abort_cause,
    validate_board_entry,
)
from zicato.core.workspace import (
    analysis_path,
    assert_distinct_callables,
    board_path,
    epoch_dir,
    events_jsonl_path,
    experiment_json_path,
    generation_dir,
    journal_path,
    lineage_path,
    loss_profile_path,
    rubric_path,
    run_dir,
    scoring_path,
)

__all__ = [
    # drift-kind registry
    "GOLDFIVE_DRIFT_KINDS",
    "normalize_wire_drift_kind",
    "normalize_wire_severity",
    "validate_drift_kind",
    # mutation surface
    "MutationKind",
    "MutationPoint",
    "PatchOpKind",
    "Patch",
    # board
    "BoardEntryKind",
    "ExpectationKind",
    "OutputScope",
    "ExpectationFiresOn",
    "Expectation",
    "JudgeMode",
    "JudgeSpec",
    "UserPersona",
    "ScriptedTurn",
    "BoardEntry",
    "validate_board_entry",
    # telemetry / loss
    "DriftCount",
    "MetricCount",
    "MetricSeverity",
    "JudgeLoss",
    "ExpectationResult",
    "LossProfile",
    "BUDGET_ABORT_CAUSE",
    "is_infra_abort_cause",
    # run record / lineage
    "RunRecord",
    "RunResult",
    # hypothesis / experiment
    "DriftDirection",
    "DriftMagnitude",
    "ExpectedDriftMovement",
    "ExpectedMetricMovement",
    "HypothesisSpec",
    "DriftMovementActual",
    "MetricMovementActual",
    "TournamentDecision",
    "Side",
    "OutcomeRecord",
    "Experiment",
    # epoch / generation
    "ScoringWeights",
    "PassRateMonotonicityScope",
    "EpochConfig",
    "Generation",
    # patterns
    "Pattern",
    # runtime config
    "CallLLM",
    "RuntimeConfig",
    # workspace paths
    "epoch_dir",
    "generation_dir",
    "run_dir",
    "events_jsonl_path",
    "loss_profile_path",
    "experiment_json_path",
    "journal_path",
    "analysis_path",
    "lineage_path",
    "rubric_path",
    "board_path",
    "scoring_path",
    "assert_distinct_callables",
]
