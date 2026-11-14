"""Typed predictions, outcomes, and experiments retained in the journal."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal

from zicato.core.mutation import Patch
from zicato.core.tournament import MatchOutcome, TournamentDecision

MovementDirection = Literal[
    "decrease", "increase", "neutral", "decrease_or_neutral", "increase_or_neutral"
]
MovementMagnitude = Literal["small", "medium", "large"]


@dataclass(frozen=True, slots=True)
class ExpectedMetricMovement:
    """Predicted direction and magnitude for one measured metric name."""

    metric_name: str
    direction: MovementDirection
    magnitude: MovementMagnitude


@dataclass(frozen=True, slots=True)
class HypothesisSpec:
    """A proposer's explanation and predictions, written before execution.

    ``modulating`` names the mutation points the patches address. Predictions
    use the measured metric's name, including its namespace. The pass-rate
    forecast remains free text so the proposer can state its uncertainty.
    The response parser requires at least one named prediction; synthetic
    seed and failure records may carry none.
    """

    core_idea: str
    modulating: tuple[str, ...]
    why: str
    expected_pass_rate_delta: str
    risks: str = ""
    expected_metric_movements: tuple[ExpectedMetricMovement, ...] = ()


@dataclass(frozen=True, slots=True)
class MetricMovementActual:
    """Parent and child values of one metric, with the recorded prediction verdict."""

    metric_name: str
    from_value: float
    to_value: float
    hypothesis_match: bool
    note: str = ""


@dataclass(frozen=True, slots=True)
class OutcomeRecord:
    """Evaluation results joined to the hypothesis that preceded execution.

    Deltas are child minus parent. Lower loss is better; pass-rate delta is
    higher when more tasks pass. A null decision means no verdict was recorded.
    Named movements retain their own units and must not be summed across
    unrelated metrics. Holdout and confirmation evidence are optional when
    those measurements did not run.

    ``scalar_score_delta_se``, ``train_fixed``, ``train_broke`` and
    ``train_entries`` describe the training comparison against the champion:
    the standard error of the paired per-task scalar difference, how many
    training tasks went from failing to passing and the reverse, and how many
    training tasks were compared. Settlement records them only under
    ``experimental.outcome_feedback``; a record without them omits the keys.
    """

    ran_at: str
    pass_rate_delta: float
    drift_loss_delta: float
    scalar_score_delta: float
    tournament_decision: TournamentDecision | None
    rejection_reason: str = ""
    metric_movements: tuple[MetricMovementActual, ...] = ()
    structure: str = "gauntlet"
    final_rank: int | None = None
    eliminated_in_round: int | None = None
    match_record: tuple[MatchOutcome, ...] = ()
    # Evaluation provenance does not alter the sealed contract.
    champion_eval_mode: str = "full"
    holdout: dict[str, Any] | None = None
    train_loss: float | None = None
    holdout_loss: float | None = None
    generalization_gap: float | None = None
    # An operator override records why the gate's decision was replaced.
    operator_override: bool = False
    operator_override_reason: str = ""
    evidence: dict[str, Any] | None = None
    scalar_score_delta_se: float | None = None
    train_fixed: int | None = None
    train_broke: int | None = None
    train_entries: int | None = None


#: Outcome fields a stored record may omit; the encoder drops them when unset.
OUTCOME_OPTIONAL_FIELDS = (
    "scalar_score_delta_se",
    "train_fixed",
    "train_broke",
    "train_entries",
)

#: The ``note`` settlement stamps on a movement measured on fewer than two
#: training entries. Such a movement is one entry's result, so the
#: experiment-memory reader withholds it from the proposer.
SINGLE_ENTRY_NOTE = "measured on fewer than two training entries"


def outcome_to_dict(outcome: OutcomeRecord) -> dict[str, Any]:
    """Encode an outcome, omitting the optional training fields that are unset."""
    body = asdict(outcome)
    for name in OUTCOME_OPTIONAL_FIELDS:
        if body[name] is None:
            del body[name]
    return body


#: Reason codes a restricted proposer or critic may see, keyed by the leading
#: text of the stored rejection reason. The stored reason keeps its detail,
#: which can name board entries and exact scalars, for the operator.
_REASON_CODES: tuple[tuple[str, str], ...] = (
    ("holdout_not_confirmed", "holdout_not_confirmed"),
    ("holdout confirmation incomplete", "confirmation_incomplete"),
    ("confirmation incomplete", "confirmation_incomplete"),
    ("deferred:", "confirmation_incomplete"),
    ("inconclusive:", "confirmation_incomplete"),
    ("insufficient improvement", "insufficient_improvement"),
    ("challenger regressed", "challenger_regressed"),
    ("pass-rate regression", "pass_rate_regression"),
    ("monotonicity_regression", "monotonicity_regression"),
    ("regression suite failed", "regression_suite_failed"),
    ("diff_complexity_ceiling", "diff_complexity_ceiling"),
    ("invalid evidence", "invalid_evidence"),
    ("incomplete execution", "incomplete_execution"),
    ("containment_violation", "containment_violation"),
    ("gate_contradiction", "gate_contradiction"),
    ("field_diversity_duplicate", "field_diversity_duplicate"),
    ("field_diversity_overlap", "field_diversity_overlap"),
    ("validation_failed", "validation_failed"),
    ("proposer_retries_exhausted", "proposer_retries_exhausted"),
    ("deferred_infra", "deferred_infra"),
    ("skip_round", "skip_round"),
    ("wall_clock_budget", "wall_clock_budget"),
    ("operator override", "operator_override"),
    ("no challenger duel ran", "not_evaluated"),
    ("multi-challenger field: no challenger applied", "not_evaluated"),
    ("no racing survivor cleared", "champion_gate_not_cleared"),
    ("no finalist cleared", "champion_gate_not_cleared"),
    ("no grand finalist cleared", "champion_gate_not_cleared"),
    ("swiss leader did not clear", "champion_gate_not_cleared"),
)

#: The closed vocabulary :func:`rejection_reason_code` can return.
REJECTION_REASON_CODES = frozenset(code for _prefix, code in _REASON_CODES) | {"rejected"}


def rejection_reason_code(reason: str) -> str:
    """Reduce a stored rejection reason to its symbolic code.

    An unrecognised reason maps to ``rejected``, so no free text reaches a
    restricted prompt.
    """
    for prefix, code in _REASON_CODES:
        if reason.startswith(prefix):
            return code
    return "rejected"


#: Hard cap on the number of settled prior experiments surfaced to the
#: proposer's experiment-memory section (the ``## What's already been
#: tried`` block). A long epoch can accumulate dozens of experiments; the
#: digest is curated and capped to this many so the prompt stays small
#: and the mutation manifest the proposer must read in full is not
#: crowded out. Wins are never dropped by the cap; the sharpest recent
#: rejections fill the remainder. See ``docs/design/EXPERIMENT-MEMORY.md``
#: §3.3.
EXPERIMENT_MEMORY_MAX_ENTRIES = 12

#: Prefix stamped onto the ``hypothesis.core_idea`` of a random-baseline
#: (placebo) challenger — the opt-in calibration arm of OVERFITTING.md #7
#: (``experimental.random_baseline_every_n``). The marker is the STABLE
#: contract between the minting side (:mod:`zicato.evolve.placebo`) and
#: every consumer that must recognise the arm: the health detector
#: (:func:`zicato.health.diagnostics.detect_placebo_promoted` — a PROMOTED
#: placebo is the alarm) and the loop-health input filter (placebo
#: experiments are calibration probes, excluded from the optimization-
#: stream detectors like stalled-loop / degenerate-scoring). Lives here in
#: :mod:`zicato.core` so both sides import one dependency-light constant.
PLACEBO_HYPOTHESIS_MARKER = "[placebo:random-baseline]"


@dataclass(frozen=True, slots=True)
class PriorExperiment:
    """One prior experiment as surfaced to the proposer's memory section.

    A compact digest entry — what was tried, where, and how it fared —
    assembled by the orchestrator (the index reader for settled history,
    the field loop for in-flight siblings) and rendered into the
    ``## What's already been tried`` user-prompt section. The proposer
    reads it to avoid re-proposing known failures and to build on known
    wins. It is advisory context only — never part of the hard schema or
    the system prompt. See ``docs/design/EXPERIMENT-MEMORY.md`` §3.2.

    Fields
    ------
    generation_id, epoch_id:
        Lineage coordinates of the prior experiment's child generation.
    core_idea:
        One-sentence hypothesis core (the ``HypothesisSpec.core_idea`` the
        proposer wrote for that experiment).
    modulating:
        The targeted mutation-point ids — the experiment's *declared*
        ``HypothesisSpec.modulating`` set, lifted from the recorded
        hypothesis.
    decision:
        The verdict: ``"promoted"`` / ``"rejected"`` / ``"deferred"`` for
        a settled experiment, or ``"in_flight"`` for a sibling minted
        this round but not yet run.
    rejection_reason:
        The symbolic reason when ``decision == "rejected"``; ``""``
        otherwise.
    scalar_score_delta:
        The signed Δscalar (negative = the child scored the lower /
        better loss). ``None`` when the experiment is unsettled /
        in-flight or when the delta does not transfer (a cross-contract
        entry — see :attr:`same_contract`).
    same_contract:
        ``True`` for a same-epoch (same-contract) entry whose Δscalar is
        directly comparable; ``False`` for a cross-contract entry from a
        different epoch under the same ``contract_hash``, which renders
        without its Δscalar because the number does not transfer.
    prediction_accuracy:
        The proposer's **hypothesis prediction-accuracy** for this settled
        experiment — the fraction of its falsifiable predictions
        (``expected_metric_movements`` /
        ``expected_pass_rate_delta``) that the realised movements bore out,
        in ``[0.0, 1.0]``. ``None`` when the experiment is unsettled /
        in-flight or made no predictions to grade. This is a DIAGNOSTIC,
        ADVISORY calibration signal folded into the experiment-memory
        section (banded, like the rest of the restricted memory); it NEVER
        gates promotion. See ``docs/design/FUNCTIONALITY-RECOMMENDATIONS.md``
        §4.2 and :func:`zicato.tournament.detail.hypothesis_ledger`.
    scalar_delta_se, train_fixed, train_broke, train_entries, predicted_movements,
    realised_movements:
        Outcome feedback, present only under ``experimental.outcome_feedback``
        (``docs/design/EXPERIMENT-MEMORY.md`` §3.6). The standard error, the
        two training-task counts and the number of training tasks compared
        are the recorded outcome fields. The
        predictions are the hypothesis's own; each realised movement carries
        a direction and a magnitude bucket for one metric, never its values,
        and covers the measured predictions plus at most three unpredicted
        metrics that moved by a medium or large amount.
    """

    generation_id: str
    epoch_id: str
    core_idea: str
    modulating: tuple[str, ...]
    decision: str
    rejection_reason: str
    scalar_score_delta: float | None
    same_contract: bool = True
    prediction_accuracy: float | None = None
    scalar_delta_se: float | None = None
    train_fixed: int | None = None
    train_broke: int | None = None
    train_entries: int | None = None
    predicted_movements: tuple[ExpectedMetricMovement, ...] = ()
    realised_movements: tuple[ExpectedMetricMovement, ...] = ()


@dataclass(frozen=True, slots=True)
class Experiment:
    """One generation's proposer output joined with its tournament outcome.

    An :class:`Experiment` is the unit of journaling. It is constructed
    when the proposer emits a hypothesis+patches; the :attr:`outcome`
    starts as ``None`` and is filled in by the tournament runner once
    the run completes and the decision is made.

    Fields
    ------
    id:
        Experiment identifier (convention: ``"exp_{epoch}_{generation}"``).
    epoch_id, generation_id:
        The lineage coordinates of THIS experiment's child generation.
    parent_generation_id:
        The lineage head this experiment is challenging, or ``None`` when
        there is no in-epoch parent (the ``v0`` seed marker — cross-epoch
        lineage lives in ``lineage.json``). An on-disk ``""`` is normalised
        to ``None`` on read.
    proposed_at:
        ISO-8601 UTC timestamp when the proposer emitted the hypothesis.
    hypothesis:
        The proposer's structured ahead-of-time prediction.
    patches:
        The concrete edits the proposer wants applied to the parent
        snapshot to produce the child snapshot.
    outcome:
        The tournament's verdict, or ``None`` until the experiment runs.
    round_index:
        The 0-based EVOLVE round that minted this generation. Persisted into
        ``experiment.json`` so the dashboard can attribute each generation to
        its birth round (the round-timeline / champion-spine view reads it);
        the canonical value the orchestrator already threads as
        ``Generation.round_index``. Defaults to 0 for the seed and for
        pre-feature records that predate the stamp.
    """

    id: str
    epoch_id: str
    generation_id: str
    parent_generation_id: str | None
    proposed_at: str
    hypothesis: HypothesisSpec
    patches: tuple[Patch, ...]
    outcome: OutcomeRecord | None
    round_index: int = 0
    #: Machine provenance for a mechanically-recombined experiment: the
    #: generation ids of the two rejected complementary parents whose patch
    #: sets were merged to mint this challenger, in ascending-gid order.
    #: Empty ``()`` for every ordinary (non-recombined) experiment — the
    #: vast majority — and the journal writer OMITS the key entirely at
    #: that default, so a non-recombined ``experiment.json`` carries no
    #: recombination key at all.
    #: Consumers read THIS field for recombination provenance; they never
    #: parse the ``[recombined]`` display prefix on ``hypothesis.core_idea``.
    #: An on-disk record without the key reads back as ``()``.
    recombined_from: tuple[str, ...] = ()
