"""Hypothesis / experiment types: predictions, outcomes, the journal unit.

Split out of :mod:`zicato.core.types`; re-exported from there and from
:mod:`zicato.core` so existing import paths keep working.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from zicato.core.mutation import Patch
from zicato.core.tournament import MatchOutcome, TournamentDecision

# ---------------------------------------------------------------------------
# Hypothesis / experiment
# ---------------------------------------------------------------------------


#: Predicted direction of movement for a drift kind under a hypothesis.
#:
#: * ``"decrease"`` / ``"increase"`` — strict directional predictions.
#: * ``"neutral"`` — expected to stay roughly flat.
#: * ``"decrease_or_neutral"`` / ``"increase_or_neutral"`` — directional
#:   prediction with the neutral case acceptable (the proposer is
#:   confident about one side but agnostic about the magnitude).
DriftDirection = Literal[
    "decrease",
    "increase",
    "neutral",
    "decrease_or_neutral",
    "increase_or_neutral",
]

#: Predicted magnitude of movement. Coarse buckets keep proposer
#: schemas compact; the journal records the actual delta separately so
#: this is only a qualitative hint.
DriftMagnitude = Literal["small", "medium", "large"]


@dataclass(frozen=True, slots=True)
class ExpectedDriftMovement:
    """A proposer's prediction about how one drift kind will move.

    Fields
    ------
    kind:
        The drift-kind string the prediction is about.
    direction:
        Predicted direction (see :data:`DriftDirection`).
    magnitude:
        Predicted magnitude bucket (see :data:`DriftMagnitude`).
    """

    kind: str
    direction: DriftDirection
    magnitude: DriftMagnitude


@dataclass(frozen=True, slots=True)
class ExpectedMetricMovement:
    """A proposer's prediction about how one namespaced metric will move.

    Generalises :class:`ExpectedDriftMovement` to any namespace. The
    proposer can now make claims about non-drift objectives — cost,
    latency, rubric scores, schema failures — using the same shape.

    Fields
    ------
    metric_name:
        The :class:`MetricCount.name` the prediction is about. Carries
        the namespace prefix (``"drift:off_topic"``, ``"cost:tokens_spent"``,
        ``"rubric:slide_structure"``, ``"latency:p95_turn_ms"``, ...).
    direction:
        Predicted direction (see :data:`DriftDirection` — reused
        verbatim; the direction lattice is namespace-agnostic).
    magnitude:
        Predicted magnitude bucket (see :data:`DriftMagnitude`).
    """

    metric_name: str
    direction: DriftDirection
    magnitude: DriftMagnitude


@dataclass(frozen=True, slots=True)
class HypothesisSpec:
    """Structured hypothesis written by the proposer BEFORE the run.

    Hypotheses are mandatory and structured so the journal captures what
    the proposer was thinking and whether it was right rather than just what
    changed. Schema-invalid proposer responses are rejected and the
    proposer is asked to fix.

    Fields
    ------
    core_idea:
        One sentence describing what is being modulated. Must be terse
        enough to render in a one-line journal entry.
    modulating:
        The :class:`MutationPoint.id` values this hypothesis is touching.
        The proposer's :class:`Patch` set MUST address only these ids; the
        applier verifies.
    why:
        Pattern-driven rationale — why the proposer believes this edit
        will move the loss in the expected direction. Free-form prose.
    expected_drift_movements:
        Per-drift-kind directional predictions (see
        :class:`ExpectedDriftMovement`). Only kinds the proposer is
        making claims about need appear — silence implies "no claim".
    expected_pass_rate_delta:
        Predicted change in board-wide pass rate as free-text
        (e.g. ``"+0.10 to +0.20"``). Free text rather than a typed range
        because the proposer expresses uncertainty differently per
        hypothesis and a typed range would force false precision.
    risks:
        Optional one-paragraph description of failure modes the proposer
        anticipates and any mitigations baked into the patches.
    """

    core_idea: str
    modulating: tuple[str, ...]
    why: str
    expected_drift_movements: tuple[ExpectedDriftMovement, ...]
    expected_pass_rate_delta: str
    risks: str = ""
    # Generalised: predictions over any namespaced metric. Back-compat
    # default: empty. The proposer prefers this field when emitting new
    # hypotheses; the orchestrator round-trips the older
    # `expected_drift_movements` shape transparently.
    expected_metric_movements: tuple[ExpectedMetricMovement, ...] = ()


@dataclass(frozen=True, slots=True)
class DriftMovementActual:
    """Realized movement of one drift kind from parent to child generation.

    Joined with :class:`ExpectedDriftMovement` at outcome-write time to
    decide whether the proposer's prediction was correct.

    Fields
    ------
    kind:
        The drift-kind string.
    from_rate:
        Per-run mean count of this kind in the parent generation.
    to_rate:
        Per-run mean count of this kind in the child generation.
    hypothesis_match:
        ``True`` iff the realized movement matches the proposer's
        directional prediction within the magnitude bucket. ``False`` if
        the proposer predicted a movement that did not occur or occurred
        in the wrong direction.
    note:
        Optional human-readable detail (e.g. "predicted decrease,
        observed flat — within neutral band").
    """

    kind: str
    from_rate: float
    to_rate: float
    hypothesis_match: bool
    note: str = ""


@dataclass(frozen=True, slots=True)
class MetricMovementActual:
    """Realised movement of one namespaced metric across two generations.

    Generalises :class:`DriftMovementActual` for the metric-namespace
    surface. Joined with :class:`ExpectedMetricMovement` at outcome-
    write time to decide whether the proposer's prediction was correct.

    Fields
    ------
    metric_name:
        The :class:`MetricCount.name` whose movement is recorded.
    from_value:
        Per-run mean (or aggregate) value of this metric in the parent
        generation.
    to_value:
        Per-run mean (or aggregate) value of this metric in the child
        generation.
    hypothesis_match:
        ``True`` iff the realised movement matches the proposer's
        directional prediction within the magnitude bucket.
    note:
        Optional human-readable detail.
    """

    metric_name: str
    from_value: float
    to_value: float
    hypothesis_match: bool
    note: str = ""


@dataclass(frozen=True, slots=True)
class OutcomeRecord:
    """The post-run record appended to an :class:`Experiment` after evaluation.

    Written atomically by the tournament runner once the decision is
    made. The pairing with :class:`HypothesisSpec` is the journal's
    core unit — what was predicted, what happened, what was decided.

    Fields
    ------
    ran_at:
        ISO-8601 UTC timestamp when the experiment finished evaluating.
    drift_movements:
        Per-kind realized movements, one entry per kind the hypothesis
        made a claim about plus any kind whose realized movement was
        large enough for the tournament to flag.
    pass_rate_delta:
        Change in board-wide pass rate from parent to child generation.
        Range ``[-1.0, 1.0]``.
    drift_loss_delta:
        Change in mean drift loss across the board. Negative = improvement.
    scalar_score_delta:
        Change in the combined tournament scalar (see
        :class:`ScoringWeights`). The sign of this field gates the
        :attr:`tournament_decision`.
    tournament_decision:
        The decision (see :data:`TournamentDecision`).
    rejection_reason:
        Symbolic reason when :attr:`tournament_decision` is
        ``"rejected"``. Empty string for the other two outcomes.
    """

    ran_at: str
    drift_movements: tuple[DriftMovementActual, ...]
    pass_rate_delta: float
    drift_loss_delta: float
    scalar_score_delta: float
    tournament_decision: TournamentDecision
    rejection_reason: str = ""
    # Generalised: realised movements over any namespaced metric. The
    # original `drift_movements` field is kept verbatim so existing
    # journal JSON keeps deserialising; `metric_movements` is the
    # superset surface for new namespaces.
    metric_movements: tuple[MetricMovementActual, ...] = ()
    # Generalised tournament-structure surface (additive; every field
    # defaults to the gauntlet reading so old journals deserialize and
    # score unchanged). ``structure`` mirrors the epoch's resolved
    # ``tournament.structure``; ``final_rank`` / ``eliminated_in_round``
    # / ``match_record`` describe this generation's path through a
    # non-gauntlet bracket. A gauntlet leaves them at the defaults below.
    structure: str = "gauntlet"
    final_rank: int | None = None
    eliminated_in_round: int | None = None
    match_record: tuple[MatchOutcome, ...] = ()
    # RUNTIME champion-eval provenance (NOT a contract input): how the
    # champion side was evaluated this round under the ``--mode`` knob.
    # ``"full"`` = the champion was run live; ``"fast"`` = its cached
    # per-board scalars were reused and the champion was NOT executed;
    # ``"fast-degraded"`` = fast was requested but no cache covered the
    # needed boards, so the champion ran once to seed it. Defaults to
    # ``"full"``, which a journal omitting the key also reads back as.
    # Provenance only: flipping fast↔full does not roll the epoch.
    champion_eval_mode: str = "full"
    # Holdout + Ladder evidence for THIS round (OVERFITTING.md §4 / §12 #2).
    # ``None`` (the default) when no holdout slice exists — a small board, the
    # split disabled, or no tagged entry. An exhausted query budget carries a
    # populated block with ``holdout_consulted=False`` so the record explains
    # why no comparison ran. This is a plain JSON-shaped dict with the stable
    # shape documented at
    # :func:`zicato.tournament.ladder.holdout_record`):
    # ``{"confirmed": bool|None, "train_scalar": float|None,
    #    "holdout_scalar": float|None, "holdout_consulted": bool,
    #    "ladder_released": bool, "ladder_budget_total": int,
    #    "ladder_budget_before_query": int|None,
    #    "ladder_budget_remaining": int, "ladder_query_reserved": bool,
    #    "threshold": float}``. Runtime evidence, no part of the contract.
    holdout: dict[str, Any] | None = None
    # Per-generation train/holdout loss + the generalization gap
    # (OVERFITTING.md §6 / §12 #5). Runtime evidence, no part of the contract.
    # ``train_loss`` is THIS generation's (the child's) TRAIN-slice scalar —
    # the score that gated it. ``holdout_loss`` is its HOLDOUT-slice scalar,
    # or ``None`` when there was no holdout (small board / split disabled /
    # older journals). ``generalization_gap`` is ``holdout_loss - train_loss``
    # (positive = the holdout is worse than train, the memorization signature),
    # or ``None`` when there is no holdout. A parallel dashboard agent reads
    # these three keys verbatim; the ``generalization_gap`` health detector
    # reads them off the champion lineage.
    train_loss: float | None = None
    holdout_loss: float | None = None
    generalization_gap: float | None = None
    # Operator override, driven by the control protocol's promote/reject
    # commands (``docs/design/RUNTIME-V2.md``). When an operator
    # force-promotes or force-rejects the in-flight generation through the
    # dashboard, the gate's own verdict is overridden — but NEVER silently:
    # this flag is set and the reason recorded, so the journal and index
    # carry that the decision was an explicit operator override rather than
    # the gate's. ``False`` on every gate-decided round, and on any journal
    # that omits the key. This is runtime evidence about how a round was
    # decided; it is no part of the evaluation contract.
    operator_override: bool = False
    # The freeform reason the operator attached to the override (the
    # dashboard's ``reason`` field), or a synthesised note. Empty unless
    # :attr:`operator_override` is ``True``.
    operator_override_reason: str = ""
    # Evidence-gate (Bradley--Terry pre-gate) resolution for THIS round's
    # crowning duel: the ``gate.rating`` block (both CIs, ``p_stronger``,
    # ``threshold``, ``ci_overlap``, ``replicates_spent``, ``n_duels``,
    # the terminal ``decision``) plus the per-refit ``ci_history`` trace the
    # defer→replicate loop produced. ``None`` when the pre-gate never reached
    # a credible terminal — the gate is off, the decision was a plain reject,
    # or the fit never cleared the credibility floor — so older journals and
    # gate-off rounds deserialize unchanged. RUNTIME evidence rather than a contract
    # input.
    evidence: dict[str, Any] | None = None


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
#: (``overfitting.random_baseline_every_n``). The marker is the STABLE
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
        (``expected_drift_movements`` / ``expected_metric_movements`` /
        ``expected_pass_rate_delta``) that the realised movements bore out,
        in ``[0.0, 1.0]``. ``None`` when the experiment is unsettled /
        in-flight or made no predictions to grade. This is a DIAGNOSTIC,
        ADVISORY calibration signal folded into the experiment-memory
        section (banded, like the rest of the restricted memory); it NEVER
        gates promotion. See ``docs/design/FUNCTIONALITY-RECOMMENDATIONS.md``
        §4.2 and :func:`zicato.tournament.detail.hypothesis_ledger`.
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
