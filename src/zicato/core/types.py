"""Foundational dataclasses for zicato.

These types are the contract every other zicato module imports. They are
frozen dataclasses with explicit field types so that downstream code —
adapters, the runner, the proposer, the tournament, the persistence
layer, pattern detectors, the CLI — can rely on a stable surface while
their internals evolve independently.

Design rules encoded here:

* **Frozen** — every dataclass uses ``frozen=True, slots=True``. State
  transitions construct new instances via :func:`dataclasses.replace`;
  callers who captured a reference keep operating on their snapshot.
* **JSON-friendly enums** — discriminant fields are either
  :class:`typing.Literal` strings or string-valued :class:`enum.Enum`
  members. The enums in this module (:class:`OutputScope`,
  :class:`ExpectationKind`, :class:`JudgeMode`) subclass ``str``, so a
  member compares equal to its wire string and ``json.dumps`` emits the
  bare string with no converter. The board-authoring API
  (:mod:`zicato.board`) hands operators those enum members directly so
  there are no magic strings at any call site; the on-disk JSONL stays a
  plain string token. Goldfive's own :class:`~goldfive.DriftKind` /
  :class:`~goldfive.DriftSeverity` follow the same string-enum shape and
  are reused verbatim where a board entry needs a drift coordinate.
* **Discriminated unions for board entries** — :class:`BoardEntry` carries
  every kind's discriminant fields as optional attributes; the
  :meth:`BoardEntry.validate` method (and the free function
  :func:`validate_board_entry`) enforce that the right combination is
  present for the declared ``kind``.
* **Model-agnostic LLM surface** — the only callable shape this module
  references is ``Callable[[str, str, str], Awaitable[str]]``
  (``(system, user, model) -> response``). No vendor SDK is named.
* **Open-ended kind strings where forward-compat matters** —
  :class:`BoardEntry`'s ``kind`` field is a closed :class:`Literal` for
  the v0 surface, but :class:`Pattern.kind` and the drift-kind strings
  on :class:`DriftCount` / :class:`ExpectedDriftMovement` /
  :class:`DriftMovementActual` are bare ``str`` validated against a
  registered set (see :mod:`zicato.core.drift_kinds`). This is the
  forward-compatible posture required by the dogfood-target plan.

Internal layout
---------------
The dataclasses are defined in cohesive submodules
(:mod:`zicato.core.mutation`, :mod:`zicato.core.board`,
:mod:`zicato.core.loss`, :mod:`zicato.core.lineage`,
:mod:`zicato.core.tournament`, :mod:`zicato.core.experiment`,
:mod:`zicato.core.scoring_config`, :mod:`zicato.core.proposer`,
:mod:`zicato.core.epoch`, :mod:`zicato.core.patterns`,
:mod:`zicato.core.runtime`). This module re-exports the entire public
surface so ``from zicato.core.types import X`` keeps working unchanged for
every name; the contract-serde annotation resolver also resolves field
types against this module's namespace, so every type referenced by a
contract dataclass field stays importable here. New code should prefer
``from zicato.core import X``.
"""

from __future__ import annotations

# Re-export the typing primitives the contract dataclasses' field
# annotations reference, so the contract-serde resolver (which evaluates
# stringised annotations against this module's namespace) keeps resolving
# them — exactly as it did when every dataclass lived in this file.
from collections.abc import Awaitable, Callable, Mapping  # noqa: F401
from pathlib import Path  # noqa: F401
from typing import Any, Literal  # noqa: F401

from zicato.core.board import (
    BoardEntry,
    BoardEntryKind,
    Expectation,
    ExpectationFiresOn,
    ExpectationKind,
    JudgeMode,
    JudgeSpec,
    OutputScope,
    ScriptedTurn,
    UserPersona,
    validate_board_entry,
)
from zicato.core.epoch import (
    EpochConfig,
    Generation,
)
from zicato.core.experiment import (
    EXPERIMENT_MEMORY_MAX_ENTRIES,
    DriftDirection,
    DriftMagnitude,
    DriftMovementActual,
    ExpectedDriftMovement,
    ExpectedMetricMovement,
    Experiment,
    HypothesisSpec,
    MetricMovementActual,
    OutcomeRecord,
    PriorExperiment,
)
from zicato.core.lineage import (
    RunRecord,
    RunResult,
)

# ``BUDGET_ABORT_CAUSE`` / ``is_infra_abort_cause`` are re-exported but kept
# out of ``__all__`` (matching the pre-split surface). The redundant ``as``
# aliases mark them as explicit re-exports for the type checker.
from zicato.core.loss import BUDGET_ABORT_CAUSE as BUDGET_ABORT_CAUSE
from zicato.core.loss import (
    DriftCount,
    ExpectationResult,
    JudgeLoss,
    LossProfile,
    MetricCount,
    MetricSeverity,
)
from zicato.core.loss import is_infra_abort_cause as is_infra_abort_cause
from zicato.core.mutation import (
    MutationKind,
    MutationPoint,
    Patch,
    PatchOpKind,
)
from zicato.core.patterns import (
    Pattern,
)
from zicato.core.proposer import (
    ProposerSkill,
    ProposerSpec,
)
from zicato.core.runtime import (
    CallLLM,
    RuntimeConfig,
)
from zicato.core.scoring_config import (
    ExperimentMemoryConfig,
    LadderConfig,
    OverfittingConfig,
    ProposerQualityConfig,
    ScoringWeights,
)
from zicato.core.tournament import (
    VALID_TOURNAMENT_STRUCTURES,
    MatchOutcome,
    PassRateMonotonicityScope,
    Side,
    TournamentDecision,
    TournamentStructure,
)

__all__ = [
    # Mutation surface
    "MutationKind",
    "MutationPoint",
    "PatchOpKind",
    "Patch",
    # Board
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
    # Telemetry / loss
    "DriftCount",
    "MetricCount",
    "MetricSeverity",
    "JudgeLoss",
    "ExpectationResult",
    "LossProfile",
    # Run record / lineage
    "RunRecord",
    "RunResult",
    # Hypothesis / experiment
    "DriftDirection",
    "DriftMagnitude",
    "ExpectedDriftMovement",
    "ExpectedMetricMovement",
    "HypothesisSpec",
    "DriftMovementActual",
    "MetricMovementActual",
    "TournamentDecision",
    "Side",
    "PassRateMonotonicityScope",
    "VALID_TOURNAMENT_STRUCTURES",
    "MatchOutcome",
    "TournamentStructure",
    "OutcomeRecord",
    "Experiment",
    "PriorExperiment",
    "EXPERIMENT_MEMORY_MAX_ENTRIES",
    # Proposer
    "ProposerSkill",
    "ProposerSpec",
    # Epoch / generation
    "ScoringWeights",
    "OverfittingConfig",
    "LadderConfig",
    "ProposerQualityConfig",
    "ExperimentMemoryConfig",
    "EpochConfig",
    "Generation",
    # Patterns
    "Pattern",
    # Runtime config
    "CallLLM",
    "RuntimeConfig",
]
