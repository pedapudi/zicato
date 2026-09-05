"""Scoring-config types: :class:`ScoringWeights` and its nested config blocks.

Split out of :mod:`zicato.core.types`; re-exported from there and from
:mod:`zicato.core` so existing import paths keep working.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import MISSING, dataclass, field, fields
from types import MappingProxyType
from typing import Any, get_args

from zicato.core.constraints import (
    KnobConstraint,
    require_finite_mapping,
    require_finite_number,
    validate_knobs,
)
from zicato.core.tournament import (
    EXPERIMENTAL_TOURNAMENT_STRUCTURES,
    PassRateMonotonicityScope,
    TournamentStructure,
    _default_tournament_structure,
    experimental_structure_refusal,
)

# ---------------------------------------------------------------------------
# Telemetry dialects (TELEMETRY-DIALECTS.md)
# ---------------------------------------------------------------------------

#: The default (and most powerful) telemetry dialect: the full
#: drift-instrumented event stream the reducer has always consumed. Kept
#: as a bare string so it serialises through the field-enumerating scoring
#: serde with no custom codec.
DIALECT_GOLDFIVE: str = "goldfive"
#: A generic ADK-style agent event-log JSONL (tool-call / tool-response /
#: agent-transfer / error / model-usage events). Weaker than goldfive — no
#: in-process drift instruments, no custom process-judge drift — but
#: recovers the failure / cost / loop envelope. See TELEMETRY-DIALECTS.md §3.
DIALECT_ADK_EVENTS: str = "adk_events"
#: The floor tier: no telemetry at all — predicates + optional in-run judges
#: only, the drift term structurally zero. See TELEMETRY-DIALECTS.md §4.
DIALECT_TRANSCRIPT: str = "transcript"

#: The closed set of dialect names a contract may pin. An unknown name is a
#: genuine config error rejected fail-fast at contract load (the "refuse"
#: half of the warn-or-refuse story; the capability-mismatch "warn" half
#: lives in :func:`zicato.telemetry.dialects.dialect_capability_warnings`).
KNOWN_TELEMETRY_DIALECTS: frozenset[str] = frozenset(
    {DIALECT_GOLDFIVE, DIALECT_ADK_EVENTS, DIALECT_TRANSCRIPT}
)

#: How the recombination slot composes the patch union of two rejected
#: challengers: ``"mechanical"`` concatenates two disjoint patches with no
#: model call, ``"llm"`` issues one merge call that can also resolve an
#: overlap. See :attr:`ProposerQualityConfig.recombine_merge`.
RECOMBINE_MERGE_MODES: tuple[str, ...] = ("mechanical", "llm")


# ---------------------------------------------------------------------------
# Declarative knob metadata (REIMPLEMENTATION.md — Finding 3)
# ---------------------------------------------------------------------------


def _knob(
    *,
    omit_at_default: bool = False,
    builder_op: str | None = None,
    builder_arg: str | None = None,
    constraint: KnobConstraint | None = None,
) -> dict[str, Any]:
    """Per-field knob metadata — the declarative source of truth.

    Without it a scoring or proposer knob fans out across seven hand-kept
    registries. This makes the field declaration the source those registries
    DERIVE from, with the guard tables as the enforcement net.

    ``omit_at_default`` — the field is dropped from the contract canonical
    form while it holds its default (an additive, default-off knob that must
    not retroactively roll existing epochs). The canonicalizer's omit set
    (:data:`zicato.epoch.contract._SCORING_OMIT_AT_DEFAULT_FIELDS`) is
    DERIVED from this flag across the contract dataclasses; a frozen-literal
    guard test pins the derived set so a metadata typo can never silently
    move the contract hash.

    ``builder_op`` — the builder operation that exposes this knob (e.g.
    ``"set_proposer_quality"``), or ``None`` for a field with no GUI knob.
    ``builder_arg`` — the argument NAME the op / API dispatch / copilot tool
    / GUI row use for this field WHEN it differs from the field name (e.g.
    ``screen_entries`` is the ``entries`` arg of ``set_screening``); ``None``
    means "same as the field name". A DOTTED value (``"ladder.threshold"``)
    names a field the op takes as a SUBKEY of a partial-mapping argument.
    The op, dispatch and copilot touchpoints are then checked against the
    mapping argument (``ladder``), while the GUI row and node test must
    additionally name the subkey. Without that, one row for one subkey
    would vacuously cover every sibling — which is how ``ladder.threshold``
    came to ship with no GUI row at all.

    Three guard tests keep the registry honest. A completeness guard asserts
    every ``builder_op`` knob is wired through all five touchpoints (op
    signature, API dispatch, copilot tool, GUI row, node test), naming which
    one is missing for which knob. A companion guard asserts every contract
    knob field either CARRIES a ``builder_op`` or sits in an explicitly
    justified exemption set, so a knob cannot skip the builder by omitting
    this metadata. A third guard feeds every declared ``constraint`` an
    inadmissible value and requires the loader and the builder to refuse it
    with the same message.

    ``constraint`` — the values the knob admits
    (:class:`zicato.core.constraints.KnobConstraint`). ``__post_init__``
    applies it through :func:`~zicato.core.constraints.validate_knobs`, and
    the builder consults the SAME declaration through
    :func:`~zicato.core.constraints.require_knob`, so an out-of-range value
    is refused with one wording whichever surface catches it. ``None`` means
    the field carries no machine-checkable domain (a bool, a mapping, a
    knob whose rule needs prose).

    Defaults stay on the field declaration; a rule too rich for a
    ``KnobConstraint`` stays in ``__post_init__``.
    """
    return {
        "omit_at_default": omit_at_default,
        "builder_op": builder_op,
        "builder_arg": builder_arg,
        "constraint": constraint,
    }


# ---------------------------------------------------------------------------
# Scoring config (overfitting / proposer-quality sub-configs)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LadderConfig:
    """The Ladder governor over holdout queries (OVERFITTING.md §4).

    The train/holdout split and the holdout confirmation step live in
    :class:`OverfittingConfig`. This block governs how that holdout is
    queried across an epoch's rounds, after Blum and Hardt's Ladder: a
    reused holdout stays valid under a proposer that adapts to its answers
    only when every query passes through a mechanism that limits the
    information leaked back. Two rules do that:

    * Release rule. A holdout-based signal is released only when the
      train-measured improvement clears a threshold beyond the noise band.
      Within the band the previous best is reported again, so the proposer
      cannot chase board fluctuations.
    * Budget. Each holdout query charges a finite per-epoch budget. Once it
      is spent, the runner schedules no further holdout comparison and the
      training verdict decides the round.

    Part of the contract hash through :class:`OverfittingConfig`, so a
    change to any field rolls the epoch. An empty holdout (a small board, or
    the split switched off) leaves nothing to govern: every holdout query
    is then answered directly.

    Each field entry below is served to the tournament builder as the
    knob's help text.

    Fields
    ------
    enabled:
        Switches the Ladder governor on. On by default. When off, the
        holdout confirmation runs unmediated: every query is answered, with
        no budget and no release rule. The governor is what keeps a reused
        holdout valid under a proposer that adapts to its answers.
    threshold:
        The train-side improvement a round must show before a holdout
        signal is released at all. Unset by default, which derives the bar
        from ``promote_margin`` so the Ladder reuses the gate's noise
        threshold; a number pins it. Raising it withholds the holdout query
        from a round that clears the gate on train, and that train promote
        then stands unconfirmed. To widen the tolerance of the confirmation
        that runs after release, set ``holdout_margin`` instead. Must be
        ``>= 0``.
    budget:
        Per-epoch holdout-query budget. Each round that consults the
        holdout charges one. When the budget is spent the runner stops
        consulting the holdout and the training verdict decides the round.
        The finite budget is what keeps a reused holdout statistically
        valid under an adaptive proposer. Must be ``>= 0``; ``0`` schedules
        no holdout comparison.
    noise_scale:
        Width of the noise band added to the release threshold. ``0.0``
        (default) is the parameter-free Ladder, which needs no calibration.
        Reserved for differential-privacy-grade noise calibration; must be
        ``>= 0``.
    """

    enabled: bool = field(
        default=True, metadata=_knob(builder_op="set_holdout", builder_arg="ladder.enabled")
    )
    threshold: float | None = field(
        default=None,
        metadata=_knob(
            builder_op="set_holdout",
            builder_arg="ladder.threshold",
            constraint=KnobConstraint(minimum=0.0, allow_none=True, label="ladder.threshold"),
        ),
    )
    budget: int = field(
        default=16,
        metadata=_knob(
            builder_op="set_holdout",
            builder_arg="ladder.budget",
            constraint=KnobConstraint(minimum=0, label="ladder.budget"),
        ),
    )
    noise_scale: float = field(
        default=0.0,
        metadata=_knob(
            builder_op="set_holdout",
            builder_arg="ladder.noise_scale",
            constraint=KnobConstraint(minimum=0.0, label="ladder.noise_scale"),
        ),
    )

    def __post_init__(self) -> None:
        validate_knobs(self)

    @classmethod
    def defaults(cls) -> LadderConfig:
        """The fully-defaulted (default-on) config an absent block resolves to."""
        return cls()


def _default_ladder_config() -> LadderConfig:
    """Default-factory for :attr:`OverfittingConfig.ladder`."""
    return LadderConfig.defaults()


@dataclass(frozen=True, slots=True)
class OverfittingConfig:
    """Anti-overfitting controls: the train/holdout board split and the leakage gate.

    A field of :class:`ScoringWeights`, so it folds into the contract hash
    through the scoring canonicalizer and a change to any field rolls the
    epoch. A run that holds a slice of the board out and confirms
    promotions against it selects champions under a different rule from one
    that does not.

    Every field is on by default with a safe degrade: a board too small to
    split (fewer than :attr:`min_board_size_for_split` entries and no
    explicit ``holdout`` tag) yields an empty holdout, and the loop then
    behaves as if no split were configured.

    Each field entry below is served to the tournament builder as the
    knob's help text.

    Fields
    ------
    enabled:
        Switches the train/holdout split on. On by default. When off, no
        holdout is derived (an explicit ``holdout`` tag on a board entry
        still holds that entry out; see
        :func:`zicato.board.split.split_board`) and the loop behaves as if
        the guard did not exist.
    holdout_fraction:
        Target share of the board to hold out when the split is derived by
        hash (no explicit ``holdout`` tag). A deterministic, id-stable
        threshold selects about this share. A larger holdout guards harder
        against overfitting, costs more confirmation runs, and shrinks the
        train field. Must lie strictly between 0 and 1.
    min_board_size_for_split:
        Smallest board at which a hash-derived split is attempted. Below it
        the holdout is empty, so a small board is never starved of train
        entries. An explicit ``holdout`` tag overrides this floor.
    restrict_proposer_visibility:
        When on (default), the proposer prompt is sanitised where it is
        rendered: per-entry identities in the detector patterns are
        aggregated to counts and rates, and experiment-memory score deltas
        are coarsened to ``improved``, ``flat`` and ``regressed`` bands, so
        the proposer cannot memorise individual board entries. Off restores
        the verbatim rendering.
    ladder:
        The Ladder governor over holdout queries (:class:`LadderConfig`).
        On by default; inert while the holdout is empty.
    rotate_holdout:
        When on (default), the hash-derived holdout rotates across epochs:
        the epoch id seeds the split, so a different slice of about
        ``holdout_fraction`` is held out each epoch and no fixed slice is
        mined forever. The slice is stable within an epoch. Off uses the
        unseeded split, the same slice every epoch. The rotation is derived
        per epoch and does not change the contract hash for an unchanged
        board; only this flag itself is hashed. An explicit ``holdout`` tag
        is never rotated.
    max_generations_per_contract:
        Optional cadence ceiling (OVERFITTING.md §9). When set, the loop
        raises a board-refresh recommendation (a health finding and a
        logged signal) once a contract has been mined for this many
        generations, as a cue for the operator to roll the contract. Unset
        (default) sets no ceiling. The ceiling never forces an epoch roll;
        it only recommends one. Must be ``>= 1`` when set.
    random_baseline_every_n:
        Opt-in placebo arm (OVERFITTING.md §12). When ``> 0``, every Nth
        round the orchestrator fields one extra challenger whose patch
        changes nothing (the mutation point's current value re-emitted),
        with a hypothesis marked as the baseline arm. The gate must reject
        it, since identical trees leave no improvement to clear the margin;
        a promoted baseline is the alarm that gate discrimination is broken
        and recent wins are suspect, and the loop then raises a critical
        ``placebo_promoted`` health finding. Costs one extra challenger
        every Nth round. ``0`` (default) fields no baseline. Omitted from
        the contract canonical form at its default, so a contract that
        never sets it keeps its hash. Must be ``>= 0``.
    """

    enabled: bool = field(default=True, metadata=_knob(builder_op="set_holdout"))
    holdout_fraction: float = field(
        default=0.3, metadata=_knob(builder_op="set_holdout", builder_arg="fraction")
    )
    min_board_size_for_split: int = field(
        default=6,
        metadata=_knob(builder_op="set_holdout", constraint=KnobConstraint(minimum=0)),
    )
    restrict_proposer_visibility: bool = field(
        default=True, metadata=_knob(builder_op="set_holdout")
    )
    ladder: LadderConfig = field(
        default_factory=_default_ladder_config, metadata=_knob(builder_op="set_holdout")
    )
    rotate_holdout: bool = field(default=True, metadata=_knob(builder_op="set_holdout"))
    max_generations_per_contract: int | None = field(
        default=None,
        metadata=_knob(
            builder_op="set_holdout",
            constraint=KnobConstraint(minimum=1, allow_none=True),
        ),
    )
    random_baseline_every_n: int = field(
        default=0,
        metadata=_knob(
            omit_at_default=True,
            builder_op="set_holdout",
            constraint=KnobConstraint(minimum=0),
        ),
    )

    def __post_init__(self) -> None:
        validate_knobs(self)
        # An open interval rather than a floor, so it stays here: a fraction of
        # 0 would hold nothing out and a fraction of 1 would leave nothing to
        # train on, and neither end is admissible.
        require_finite_number("holdout_fraction", self.holdout_fraction)
        if not 0.0 < self.holdout_fraction < 1.0:
            raise ValueError(f"holdout_fraction must be in (0, 1), got {self.holdout_fraction!r}")

    @classmethod
    def defaults(cls) -> OverfittingConfig:
        """The fully-defaulted (default-on) config an absent block resolves to."""
        return cls()


def _default_overfitting_config() -> OverfittingConfig:
    """Default-factory for :attr:`ScoringWeights.overfitting`."""
    return OverfittingConfig.defaults()


@dataclass(frozen=True, slots=True)
class ProposerQualityConfig:
    """Proposer-quality levers: best-of-N sampling, self-critique, and the opt-in channels.

    A field of :class:`ScoringWeights`, so it folds into the contract hash
    and a change to any field rolls the epoch: a proposer that samples N
    candidates and self-critiques proposes differently from one that
    samples once.

    The default samples a slate of three (:attr:`best_of_n`) and lets the
    self-critique pass select the best. Pin ``"proposer_quality":
    {"best_of_n": 1}`` for a single-sample proposer with no critique
    (scripted and mock proposers do). See
    ``docs/design/FUNCTIONALITY-RECOMMENDATIONS.md`` §4.1.

    Overfitting discipline: the self-critique pass sees only the restricted
    prompt context the proposer itself sees (the train-slice patterns, the
    banded experiment memory, the bucketed failure-mode profile) and sees
    neither the holdout nor any per-entry identity. The critic sits inside the same
    visibility envelope as the proposer (OVERFITTING.md §11) and cannot
    widen what the proposer may learn about the board.

    Each field entry below is served to the tournament builder as the
    knob's help text.

    Fields
    ------
    best_of_n:
        How many candidate experiments each propose step samples before
        the critique pass picks one. ``3`` (default) samples a slate; ``1``
        is a single sample with no critique. Each sample is one propose
        call to the proposer, so the cost meter prices the slate. Each
        slate slot carries a distinct edit-class hint
        (:data:`zicato.proposer.best_of_n.EDIT_CLASS_HINTS`); a candidate
        the inner proposer cannot produce narrows the slate, and an empty
        slate falls back to one final propose call so the step never
        yields nothing. Must be ``>= 1``.
    critique_enabled:
        When on (default) and ``best_of_n > 1``, one cheap evaluation-model
        pass scores the sampled candidates against a quality bar (grounded
        in a tool call, targets a real failure mode, minimal diff) and
        selects the best. When off, best-of-N still samples ``best_of_n``
        candidates and the selection falls back to the built-in heuristic
        (the smallest diff that targets an observed failure mode), with no
        extra model call. Inert at ``best_of_n == 1``.
    screen_entries:
        Opt-in pre-tournament screening of the slate. When ``> 0`` and
        ``best_of_n > 1``, each slate candidate runs on a small rotating
        panel of this many train board entries before selection. The
        screen only vetoes: a confirmed catastrophic regression (a
        pass-flip on an entry the champion passes, or a budget abort)
        disqualifies the candidate, and the critic or heuristic still
        chooses among the survivors. It costs ``best_of_n × screen_entries``
        extra runs per propose step. ``0`` (default) runs no screen; new
        workspaces scaffold ``2``. Inert at ``best_of_n == 1``. Omitted
        from the contract canonical form at its default; a non-zero value
        rolls the epoch, since a proposer whose slate is screened selects
        differently. Must be ``>= 0``. See :mod:`zicato.epoch.screen`.

        A screen-informed revise pass rides this knob. When every slate
        candidate is vetoed, the proposer takes one feedback-informed
        re-sample before degrading to the critic over the whole slate, so
        the propose step is not spent on a known-vetoed candidate. See
        :class:`zicato.proposer.best_of_n.BestOfNProposerAgent`.
    screen_veto_only:
        When on, the screen's measurements feed nothing but the veto: the
        critic prompt carries no screen-measurement block and the heuristic
        ignores the panel-scalar tiebreak. Selection then stays blind to
        the tryout measurements, which are biased by the selection they
        inform, while catastrophic regressions are still caught. Off
        (default) lets the survivors' banded panel counts advise the
        selection as a late tiebreak. Inert while ``screen_entries == 0``.
        Omitted from the contract canonical form at its default.
    process_exemplars:
        Opt-in process-exemplar channel
        (``docs/design/PROCESS-EXEMPLARS.md``). When ``> 0``, each round
        the orchestrator extracts up to this many redacted event windows
        from the champion's train-slice event logs, one per detected
        pattern and three events either side of an anchor drift, and
        splices them into the proposer prompt after the failure-mode
        profile. The proposer then sees how a detected failure unfolds (a
        wandering plan step, a looping tool call) without learning which
        board entry it unfolded on. A window carries no entry ids, no task
        text and no model outputs, and the redaction rules of that
        document's §3 are enforced in code. Read-side only, so free on the
        cost meter. The channel widens what the proposer can see, so the
        scaffold does not set it; enable it under the harm-detection
        runbook of that document's §5: watch the ``generalization_gap``
        finding and set the cap back to ``0`` if the gap widens while
        train improves. ``0`` (default) extracts nothing. Omitted from the
        contract canonical form at its default; a non-zero cap rolls the
        epoch. Must be ``>= 0``.
    recombine:
        Opt-in recombination slot. When on and ``best_of_n > 1``, the
        orchestrator builds one recombination pair per round from the
        current reign's rejected challengers whose patches are
        complementary and disjoint. When a pair is found, the last slate
        slot mints the union of the two patch sets instead of sampling the
        proposer, and a non-vetoed mint is chosen with
        ``selection_mode="recombined"``, so one winner can capture two
        fixes a parsimony-biased selector would each discount.
        Cost-neutral: the mint replaces the slot's propose call. Inert at
        ``best_of_n == 1``. Off (default) builds no pair. Omitted from the
        contract canonical form at its default; on rolls the epoch. See
        :mod:`zicato.epoch.recombine` and :mod:`zicato.proposer.recombine`.
    genealogy:
        Opt-in genealogy channel (``docs/design/PROPOSER.md`` §2.7). When
        ``> 0``, each round the orchestrator samples up to this many
        candidate-lineage items from the current reign's records: parents
        (the champion's own promoted patch history) and inspirations
        (rejected reign candidates chosen for dissimilar mutation-id
        sets). Each carries the proposer's own core idea, a capped diff
        excerpt, and a banded whole-candidate outcome (improved, flat or
        regressed). Spliced into the prompt, they let the proposer extend a
        winning line or re-frame a rejected one: the in-context
        counterpart of the recombination slot, reaching the pairs that
        slot cannot see. The channel carries candidate genealogy and never
        board data: no entry ids, no per-entry results, no exact deltas,
        nothing derived from the holdout. The sampler is deterministic.
        Read-side only, so free on the cost meter. Like
        ``process_exemplars`` it widens what the proposer can see, so the
        scaffold does not set it. ``0`` (default) samples nothing. Omitted
        from the contract canonical form at its default; a non-zero count
        rolls the epoch. Must be ``>= 0``. See
        :mod:`zicato.proposer.genealogy`.
    calibration_feedback:
        Opt-in critic-calibration channel (``docs/design/PROPOSER.md``
        §2.8). When ``> 0``, each round the orchestrator summarises how
        the proposer's own falsifiable movement predictions landed against
        realised outcomes, from the durable records and the
        prediction-accuracy grader
        (:func:`zicato.tournament.detail.hypothesis_ledger`). The summary
        spliced into the prompt carries hit, miss and unresolved counts per
        claim type, the overall calibration fraction, and up to this many
        recent graded claims (claim text, banded realised outcome, hit or
        miss). A proposer shown its own miss pattern hypothesises more
        honestly. The channel carries the proposer's own claim text and
        aggregate counts and never board data: outcomes are banded, grades
        come from whole-candidate aggregates, and nothing is derived from
        the holdout. The sampler is deterministic. Read-side only, so free
        on the cost meter; like ``genealogy`` it widens what the proposer
        can see, so the scaffold does not set it. ``0`` (default) samples
        nothing. Omitted from the contract canonical form at its default;
        a non-zero count rolls the epoch. Must be ``>= 0``. See
        :mod:`zicato.proposer.calibration`.
    recombine_merge:
        How the recombination slot composes the union once a pair is
        picked (``docs/design/PROPOSER.md`` §2.6.1). ``"mechanical"``
        (default) mints the concatenation of two disjoint patch sets with
        no model call, and requires a disjoint pair because the applier
        keeps the last write to a duplicated target. ``"llm"`` issues one
        merge call to the evaluation model, whose response flows through
        the normal proposal parse and validation. It also relaxes the
        disjointness rule for pair selection, so two rejected fixes that
        overlap on a mutation target can be merged; the overlap becomes a
        ranking penalty rather than a filter. Meaningful only when
        ``recombine`` is on and ``best_of_n > 1``; ``"llm"`` with
        ``recombine`` off is accepted and inert. Cost: ``"mechanical"``
        spends ``best_of_n − 1`` propose calls, since the mint is free;
        ``"llm"`` spends ``best_of_n``, the merge call taking the slot's
        own sample call, plus one fallback sample in the rare round where
        the merge fails to parse or validate. Omitted from the contract
        canonical form at its default; ``"llm"`` rolls the epoch. Must be
        ``"mechanical"`` or ``"llm"``. See :mod:`zicato.epoch.recombine`
        and :mod:`zicato.proposer.recombine`.
    """

    best_of_n: int = field(
        default=3,
        metadata=_knob(builder_op="set_proposer_quality", constraint=KnobConstraint(minimum=1)),
    )
    critique_enabled: bool = field(
        default=True,
        metadata=_knob(builder_op="set_proposer_quality"),
    )
    screen_entries: int = field(
        default=0,
        metadata=_knob(
            omit_at_default=True,
            builder_op="set_screening",
            builder_arg="entries",
            constraint=KnobConstraint(minimum=0),
        ),
    )
    screen_veto_only: bool = field(
        default=False,
        metadata=_knob(omit_at_default=True, builder_op="set_screening", builder_arg="veto_only"),
    )
    process_exemplars: int = field(
        default=0,
        metadata=_knob(
            omit_at_default=True,
            builder_op="set_proposer_quality",
            constraint=KnobConstraint(minimum=0),
        ),
    )
    recombine: bool = field(
        default=False,
        metadata=_knob(omit_at_default=True, builder_op="set_proposer_quality"),
    )
    genealogy: int = field(
        default=0,
        metadata=_knob(
            omit_at_default=True,
            builder_op="set_proposer_quality",
            constraint=KnobConstraint(minimum=0),
        ),
    )
    calibration_feedback: int = field(
        default=0,
        metadata=_knob(
            omit_at_default=True,
            builder_op="set_proposer_quality",
            constraint=KnobConstraint(minimum=0),
        ),
    )
    recombine_merge: str = field(
        default="mechanical",
        metadata=_knob(
            omit_at_default=True,
            builder_op="set_proposer_quality",
            constraint=KnobConstraint(choices=RECOMBINE_MERGE_MODES),
        ),
    )

    def __post_init__(self) -> None:
        validate_knobs(self)

    @classmethod
    def defaults(cls) -> ProposerQualityConfig:
        """The fully-defaulted (best-of-3 + self-critique) config."""
        return cls()


def _default_proposer_quality_config() -> ProposerQualityConfig:
    """Default-factory for :attr:`ScoringWeights.proposer_quality`."""
    return ProposerQualityConfig.defaults()


@dataclass(frozen=True, slots=True)
class ExperimentMemoryConfig:
    """Experiment-memory scoping: which settled history the proposer sees.

    A field of :class:`ScoringWeights`, like :class:`OverfittingConfig`,
    because a change to what history the proposer reads selects champions
    under a different rule (EXPERIMENT-MEMORY.md §3.4). Omitted from the
    contract canonical form at its default, so a contract that never sets
    it keeps its hash; a non-default value rolls the epoch.

    Each field entry below is served to the tournament builder as the
    knob's help text.

    Fields
    ------
    cross_epoch:
        Opt-in cross-epoch transfer (EXPERIMENT-MEMORY.md §3.4 and §5.2).
        Off (default) keeps the experiment-memory digest to the current
        epoch. On appends settled experiments from earlier epochs of the
        same workspace that share the current epoch's contract hash,
        marked ``same_contract=False`` and with their score delta omitted,
        since the number does not transfer. They render in a separate
        block and are admitted only into the budget left after every
        same-epoch entry, so same-epoch history keeps priority in the cap. Experiments under
        a different contract hash are never surfaced.
    """

    cross_epoch: bool = field(
        default=False,
        metadata=_knob(builder_op="set_experiment_memory"),
    )

    @classmethod
    def defaults(cls) -> ExperimentMemoryConfig:
        """The fully-defaulted (same-epoch-only) config."""
        return cls()


def _default_experiment_memory_config() -> ExperimentMemoryConfig:
    """Default-factory for :attr:`ScoringWeights.experiment_memory`."""
    return ExperimentMemoryConfig.defaults()


@dataclass(frozen=True, slots=True)
class ExperimentalConfig:
    """The contract's opt-ins for features without a measured case.

    A feature stays in this block until a measurement sweep graduates it
    (``docs/design/CAMPAIGN.md``); graduation moves the knob out of the
    block, which rolls the epoch. A field of :class:`ScoringWeights`,
    omitted from the contract canonical form while every flag holds its
    default, so a contract that names none of them keeps its hash and one
    that enables a flag rolls the epoch.

    Each field entry below is served to the tournament builder as the
    knob's help text.

    Fields
    ------
    tournament_structures:
        Admits single elimination, double elimination and Swiss pairing
        (:data:`zicato.core.tournament.EXPERIMENTAL_TOURNAMENT_STRUCTURES`)
        as the contract's ``tournament.structure``. Each pairs challengers
        against each other, so a candidate's fate depends on its draw; the
        second life a losers' bracket buys is what ``replicates`` already
        buys, and Swiss pairing is racing without the escalating board
        slice. None has a measured case at a field of two to four
        candidates under an expensive, noisy evaluator. Off (default): a
        contract naming one of the three is refused at load, by the
        builder, and by the strategy registry, each with the message
        :func:`zicato.core.tournament.experimental_structure_refusal`
        renders, and turning the flag off while the structure is one of
        the three is refused the same way. On: the three resolve like
        ``gauntlet`` and ``racing``.
    """

    tournament_structures: bool = field(
        default=False,
        metadata=_knob(builder_op="set_experimental"),
    )

    @classmethod
    def defaults(cls) -> ExperimentalConfig:
        """The config with every opt-in off."""
        return cls()


def _default_experimental_config() -> ExperimentalConfig:
    """Default-factory for :attr:`ScoringWeights.experimental`."""
    return ExperimentalConfig.defaults()


# ---------------------------------------------------------------------------
# Scoring weights
# ---------------------------------------------------------------------------


def _default_severity_weights() -> Mapping[str, float]:
    """Default severity multipliers for drift-loss scoring.

    INFO is the baseline (1.0), WARNING is materially worse (3.0), and
    CRITICAL is qualitatively different (10.0) — a single CRITICAL drift
    swamps a handful of INFOs. Operators tune these per epoch.
    """
    return {"info": 1.0, "warning": 3.0, "critical": 10.0}


def _default_namespace_weights() -> Mapping[str, float]:
    """Default per-namespace weights for the multi-objective scalar.

    The mapping keys are namespace prefixes (with the trailing colon
    preserved so callers never have to remember to add or strip it).
    Values are signed coefficients that turn a namespace's per-run mean
    metric value into a scalar-component contribution:

    * Positive weight → "higher value is worse". The component is added
      to the scalar as ``weight * mean``. Drift, cost, latency, and
      schema-failure namespaces have positive weights.
    * Negative weight → "higher value is better". Rubric scores grow with
      quality, so a negative weight turns the scalar into a loss.
    * Zero → namespace excluded from the scalar entirely. Useful for
      observability-only namespaces (``output:`` length stats) the
      operator wants to track but not optimise.

    Defaults intentionally span several orders of magnitude — cost is
    often counted in tokens (thousands) while drift loss is a small
    weighted sum, so the cost coefficient is small to keep both terms
    in a comparable scale.

    Every measured channel rides this map; the scalar has no privileged
    term besides the bounded pass/miss one (see
    :func:`zicato.scoring.builtins.builtin_scalar`). ``runtime:`` is
    separate from ``latency:`` on purpose: ``latency:`` coefficients are
    calibrated for adapter-supplied millisecond percentiles, and summing
    those together with a whole-run duration in seconds would produce a
    meaningless within-namespace total.
    """
    return {
        "drift:": 1.0,
        "judge:": 1.0,
        "failure:": 1.0,
        "runtime:": 0.0,
        "cost:": 0.001,
        "latency:": 0.0001,
        "rubric:": -1.0,
        "output:": 0.0,
        "schema:": 5.0,
    }


def _default_namespace_monotonicity() -> Mapping[str, bool]:
    """Default per-namespace monotonicity flags for the promote gate.

    When a namespace's flag is ``True``, the gate rejects any child
    whose per-namespace aggregate has regressed against the parent (in
    the namespace's own "worse" direction, as encoded by the sign of
    the corresponding :func:`_default_namespace_weights` entry).

    The defaults guard the namespaces whose regression is qualitatively
    bad even when the overall scalar improves: rubric (quality drop)
    and schema (introducing failures). Drift is left unguarded so
    proposers can trade some drift movement for gains elsewhere.
    """
    return {
        "drift:": False,
        "rubric:": True,
        "schema:": True,
    }


@dataclass(frozen=True, slots=True)
class ScoringWeights:
    """Tunable weights that turn a :class:`LossProfile` into a scalar.

    A single :class:`ScoringWeights` instance is frozen for the lifetime
    of an epoch. Changing weights starts a new epoch; generations in
    different epochs are not directly comparable.

    Each field entry below is served to the tournament builder as the
    knob's help text (:func:`zicato.builder.knob_help.knob_help` reads
    this section), so an entry is written for the operator who reads it
    there.

    Fields
    ------
    pass_weight:
        Coefficient on the ``(1 - pass_rate)`` miss term. The pass/miss
        term is the scalar's one term outside the namespace map: it has
        its own denominator (the entries that carry expectations rather
        than every entry), its own monotonicity rule
        (:attr:`pass_rate_monotonicity_scope`) and its own transform
        (:attr:`pass_transform`). Every measured channel (drift, judges,
        failures, runtime, cost, latency, rubric, output, schema) rides
        :attr:`namespace_weights` instead.
    severity_weights:
        Per-severity multipliers applied inside the drift-loss
        aggregation: how much a drift observation of each severity costs a
        challenger. Keys are lowercase severity names; a severity with no
        entry scores at ``0.0`` rather than failing. The contract holds
        the whole mapping, so a change to one severity writes the mapping
        with the other severities as they are.
    per_kind_weights:
        Optional per-drift-kind multipliers, keyed by drift kind. They
        stack multiplicatively with :attr:`severity_weights`; a kind with
        no entry weighs ``1.0``, and an empty mapping weighs every kind
        alike. Custom-judge drift is scored in the ``judge:`` channel
        through :attr:`per_judge_weights`, so an entry for ``custom`` is
        refused.
    per_judge_weights:
        Optional per-judge multipliers, keyed on the stable ``judge_name``
        a judge implementation sets on its ``name`` attribute. Every
        custom judge emits drift under the single ``custom`` drift kind,
        so :attr:`per_kind_weights` cannot tell two custom judges apart;
        this mapping is the per-judge counterpart and stacks
        multiplicatively with :attr:`severity_weights` the same way. A
        judge with no entry scores at :attr:`default_judge_weight`. Each
        judge's weighted loss becomes a ``judge:<name>`` metric inside the
        ``judge:`` namespace, so retiring one judge is
        ``per_judge_weights: {name: 0.0}`` and retiring the whole channel
        is ``namespace_weights: {"judge:": 0.0}``.
    default_judge_weight:
        The multiplier for a judge whose ``judge_name`` is absent from
        :attr:`per_judge_weights`. ``1.0`` by default, so an unconfigured
        judge contributes on the same footing as a drift kind with no
        :attr:`per_kind_weights` entry.
    plan_revision_weight:
        Coefficient on :attr:`LossProfile.plan_revisions`, the count of
        times the system under test rewrote its own plan during a run.
        ``0.5`` by default: plan revisions are signal, but weaker signal
        than drift.
    task_failure_weight:
        Multiplier on :attr:`LossProfile.task_failure_ratio`, the share of
        a run's started tasks that failed, charged in the ``failure:``
        channel as ``failure:tasks``. ``10.0`` by default, large against a
        single drift observation, because an outright failure matters
        more.
    not_completed_weight:
        Fixed magnitude charged in the ``failure:`` channel (as
        ``failure:not_completed``) for a run that did not complete:
        killed, crashed, a harness exception, an emulator-leak abort, or an
        exhausted wall-clock budget. ``50.0`` by default. An absolute
        magnitude rather than a multiple of :attr:`severity_weights`, so
        retuning severities cannot silently rescale what a crash costs.
        Without it a run that crashed at once (an empty events file, zero
        drift) would earn the best possible score and a challenger could
        win by failing fast.
    diff_complexity_weight:
        Opt-in parsimony term (OVERFITTING.md §5). When ``> 0`` the scalar
        gains a ``diff_complexity`` component equal to this weight times
        ``added + removed + patches``, the diff size read from the
        challenger's patch records
        (:func:`zicato.scoring.diff_complexity.diff_size`). A shorter edit
        overfits the board less, so penalising diff size biases selection
        toward the smaller, more general edit. ``0.0`` (default) leaves
        the term absent: it is not added to the scalar, and the scalar,
        the contract hash and every recorded outcome are byte-identical to
        a contract without the field. The contract canonical form omits
        the field at the default, so setting it ``> 0`` rolls the epoch
        like any other weight change. Applies on the full
        champion-versus-challenger promotion path only; fast-mode and
        multi-challenger matchup scoring carry no diff term. Calibration:
        the diff size counts changed lines against the parent's content
        rather than the size of the whole replacement. An edit to a
        whole-file mutation point therefore scores about an order of
        magnitude lower than a whole-file charge would, and a re-emit that
        changes nothing scores ``0``; tune the weight against a measured
        round.
    diff_complexity_ceiling:
        The parsimony ceiling paired with :attr:`diff_complexity_weight`
        (OVERFITTING.md §5). Where the weight dampens an oversized diff
        with a loss term, the ceiling is a gate rule: a challenger whose
        diff complexity (``added + removed + patches``, the same measure
        the loss term reads) exceeds it is rejected outright, however much
        it improved. The rejection reason names both numbers
        (``diff_complexity_ceiling: diff complexity 14 exceeds ceiling
        10``) in the experiment record and the round log. ``0.0``
        (default) turns the ceiling off: it is never consulted, and the
        contract canonical form omits the field. Any value ``<= 0`` is
        off. Applies on the full promotion path only, like the weight, and
        reads the same changed-line measure, so a ceiling tuned against
        whole-file re-emits admits far larger edits than intended.
    promote_margin:
        Minimum scalar improvement (champion loss minus challenger loss) a
        challenger must show to be promoted. A larger margin demands a
        more decisive win and resists noise; ``0`` promotes on any
        improvement. Without the evidence gate the margin must clear the
        measured same-versus-same noise floor, which the builder's
        preflight measures. Calibrated against the train slice; see
        :attr:`holdout_margin` for why the holdout needs its own bound.
        Must be ``>= 0``.
    holdout_margin:
        The scalar tolerance the holdout confirmation applies, or unset
        (the default) to reuse :attr:`promote_margin`.

        One margin serving both uses is pulled in opposite directions. The
        train rule wants it small enough that a real train-measured win
        clears it; the holdout confirmation wants it large enough to
        absorb the holdout slice's own quantisation. A slice of N entries
        moves its scalar in steps of ``1/N``, and the holdout is the
        smaller slice (``holdout_fraction`` defaults to 0.3), so its steps
        are the coarser ones. On the default 12-train, 6-holdout split, a
        two-entry train win needs ``margin <= 2/12`` while tolerating one
        regressed holdout entry needs ``margin >= 1/6``: the same number,
        so the feasible window is a single point that float rounding
        closes. Separate bounds make such a board promotable.

        For bounds that mean the same on both slices, set
        ``holdout_margin ≈ promote_margin × N_train / N_holdout`` (about
        twice ``promote_margin`` on the default split). Unset keeps the
        single-knob behaviour and the contract canonical form omits the
        field, so the hash is unmoved.

        Scoped to the holdout confirmation alone. It does not move the
        Ladder's release threshold
        (:func:`zicato.tournament.ladder.effective_threshold`), which
        gates a train-measured improvement and where a raised bar would
        withhold the query and leave the train promote unconfirmed; widen
        that band with :attr:`LadderConfig.threshold`.
    holdout_entry_regression_budget:
        How many holdout entries may regress before the holdout
        confirmation rejects. ``0`` (default) is zero tolerance: any
        regressing holdout entry blocks confirmation.

        The holdout confirms rather than re-decides: a train-measured win
        must merely not regress there. The pass-rate monotonicity rule the
        confirmation reuses carries only a float-noise tolerance (``1e-9``
        aggregate, ``0.02`` per entry). On a six-entry noisy slice a single
        entry flipping from pass to fail therefore rejects at every margin,
        and no ``holdout_margin`` can rescue it, because the rejection
        never came from the scalar bound. This budget is the tolerance
        that rule lacks. It applies under both
        :attr:`pass_rate_monotonicity_scope` values: per entry it allows
        up to N regressed entries; in aggregate it widens the mean-score
        tolerance by ``N / (scored holdout entries)``, the movement N flips
        would produce on that slice. Holdout-only: the train side keeps
        zero tolerance, so this cannot loosen the gate's primary decision.
        Must be ``>= 0``.
    pass_rate_monotonicity:
        When on (default), a pass-rate regression rejects the challenger
        whatever the drift-side improvement: every expectation the
        champion passed must still pass. This guards against trading a
        hard pass away for an average-loss gain, and is the stricter half
        of the tournament gate. Off admits non-monotone exploration in an
        experimental epoch. The on/off switch only;
        :attr:`pass_rate_monotonicity_scope` selects which movement counts
        as a regression.
    pass_rate_monotonicity_scope:
        Granularity of the pass-rate check while
        :attr:`pass_rate_monotonicity` is on
        (:data:`PassRateMonotonicityScope`). ``"per_entry"`` (default)
        rejects when any entry the champion passed flips to fail, the
        right policy for invariant and regression-suite boards.
        ``"aggregate"`` rejects only when the overall pass rate drops below
        the champion's beyond a small float-noise tolerance, the right
        policy for sampled boards where one noisy flip should not veto a
        better challenger. There is no ``"off"`` value; disable the check
        with ``pass_rate_monotonicity=False``.
    regression_gate_enabled:
        When on, the tournament runner runs the snapshot's own test suite
        before evaluating the scoring gate, and a failing or timed-out
        suite rejects the candidate whatever its scalar movement. Off by
        default; turn it on only for a system under test whose snapshot
        ships a suite.
    regression_test_command:
        The command line that invokes the regression suite, as an
        argument list. ``pytest tests/ -q`` by default; a non-pytest suite
        names its own command, such as ``python -m unittest discover``.
    regression_timeout_s:
        Wall-clock seconds the regression subprocess may take before the
        runner kills it. A timeout counts as a regression failure. Must be
        ``>= 1``.
    namespace_weights:
        Per-namespace coefficients of the multi-objective scalar, the one
        map every measured channel rides. Keys are namespace prefixes with
        the trailing colon (``"drift:"``). The sign of each coefficient
        states the namespace's worse direction:

        * Positive: a higher value is worse (drift, judges, failures,
          runtime, cost, latency, schema). Added to the scalar as
          ``weight × mean``.
        * Negative: a higher value is better (rubric). The negation turns
          the metric into a loss so the scalar stays lower-is-better.
        * Zero: the namespace is tracked and left out of the scalar (the
          default for ``"output:"`` and ``"runtime:"``).

        An explicit mapping replaces the defaults as a whole rather than
        merging with them, and a namespace it omits scores at ``0.0``.
        ``"failure:"`` must be present and strictly positive, because that
        channel carries the task-failure and not-completed terms and a
        contract must not be able to make crashing free. See
        :func:`_default_namespace_weights` for the shipped values.
    namespace_monotonicity:
        Per-namespace strict-monotonicity flags. When a namespace's flag
        is on, the promote gate rejects a challenger whose per-namespace
        aggregate moved in that namespace's worse direction (the sign in
        :attr:`namespace_weights`) by more than the namespace's tolerance,
        even when the combined scalar improved. A namespace whose flag is
        missing or off is not gated this way. The shipped defaults gate
        ``rubric:`` and ``schema:``.
    tournament_structure:
        The per-epoch tournament structure and its parameters
        (:class:`TournamentStructure`): ``gauntlet`` runs one challenger
        against the champion, ``racing`` a field of challengers over an
        escalating board slice; the structures behind
        :attr:`experimental` need that block's opt-in. Changing the
        structure or any parameter rolls the epoch.
    telemetry_dialect:
        Which producer reduces a run's raw telemetry into the loss profile
        the scalar scores (TELEMETRY-DIALECTS.md). ``"goldfive"``
        (default) reads the full drift-instrument event stream;
        ``"adk_events"`` reduces a generic agent event-log JSONL, with no
        in-process drift instruments and no custom process-judge drift;
        ``"transcript"`` is the floor, predicates and in-run judges only,
        with a drift term of zero. A contract input: changing it selects
        champions under a different measurement rule and rolls the epoch.
        Omitted from the contract canonical form at its default. An
        unknown name is refused at load.
    block_on_containment_violation:
        When on, the orchestrator re-checks diff containment before it
        finalises a gate-decided promotion: every file outside the
        registered mutable trees must be byte-identical between parent and
        child, the rule the supervisor attests out of band. A violating
        child is rejected with a ``containment_violation`` reason instead
        of being promoted with an alarm. Off (default) keeps the alarm-only
        posture. An unreadable snapshot skips the check rather than
        quarantining a candidate, and an explicit operator force-promote
        is never blocked; the override is recorded. Omitted from the
        contract canonical form at its default.
    block_on_gate_contradiction:
        When on, the orchestrator re-derives the gate's scalar rule
        (``delta_scalar <= -promote_margin``) immediately before it
        finalises a gate-decided promotion and refuses the promotion on a
        contradiction. Off (default) persists the promotion and leaves the
        supervisor's out-of-band scan to raise the alarm. A promotion with
        no usable scalar evidence is skipped rather than refused, and an
        explicit operator force-promote is not re-checked. Omitted from
        the contract canonical form at its default.
    goldfive:
        The optional goldfive integration block: its detector, judge,
        steering, endpoint and wrapped-call settings, as one JSON object.
        Absent (the default) unless the selected adapter declares the
        integration; an explicit block binds all of that behaviour to the
        epoch, and any change to it rolls the epoch.
    mutation_surface:
        The mutation-site file types declared beyond the built-in syntax
        table (MUTATION-SURFACE.md §2.5): ``{suffix: {"leaders": [...],
        "trailers": [...]}}``, where the leaders are the comment lead-ins
        a marker may be written under and the trailers the comment
        closers. The built-ins are ``.md``, ``.markdown``, ``.txt``,
        ``.yaml``, ``.yml`` and ``.toml``; ``.py`` is reserved, and the
        table governs the text pass only. The table decides which files
        are enumerable at all, hence what the proposer may rewrite, so it
        is a contract input: declaring or removing a type rolls the epoch,
        and the empty default is omitted from the canonical form.
        Validated by ``markers.syntax_table_from_config`` when installed or
        set through the builder.
    pass_transform:
        Optional declarative transform (one
        :data:`zicato.scoring.transforms.TransformSpec`,
        ``{"op": ..., ...params}``) reshaping the scalar's pass/miss term
        ``(1 - mean_score)`` where that term is formed. Unset (default)
        keeps the plain linear miss term. A ``pass_exponent`` key is
        rejected at load; write ``{"op": "pow", "exponent": 2.0}`` for
        that curve. Validated at construction.
    drift_kind_aggregation:
        Optional per-drift-kind declarative transforms (``{kind:
        TransformSpec}``) reshaping how each kind's count aggregates into
        the per-run drift loss. A diminishing-returns rule for
        ``looping_reasoning`` is opted into here, per contract
        (``{"looping_reasoning": {"op": "harmonic"}}``). A kind with no
        entry aggregates linearly as ``severity × kind_weight × count``.
        Validated at construction.
    """

    pass_weight: float = field(
        default=1.0, metadata=_knob(builder_op="set_weights", constraint=KnobConstraint())
    )
    severity_weights: Mapping[str, float] = field(
        default_factory=_default_severity_weights,
        metadata=_knob(builder_op="set_weights"),
    )
    per_kind_weights: Mapping[str, float] = field(
        default_factory=dict, metadata=_knob(builder_op="set_weights")
    )
    per_judge_weights: Mapping[str, float] = field(
        default_factory=dict, metadata=_knob(builder_op="set_weights")
    )
    default_judge_weight: float = field(
        default=1.0, metadata=_knob(builder_op="set_weights", constraint=KnobConstraint())
    )
    plan_revision_weight: float = field(
        default=0.5, metadata=_knob(builder_op="set_weights", constraint=KnobConstraint())
    )
    # The two ``failure:`` channel magnitudes. They live on the contract (not
    # as module constants) so retuning them rolls the epoch through the normal
    # hash mechanism — a mid-epoch retune would otherwise let the unit cache
    # fold old- and new-formula losses together undetectably.
    task_failure_weight: float = field(
        default=10.0, metadata=_knob(builder_op="set_weights", constraint=KnobConstraint())
    )
    not_completed_weight: float = field(
        default=50.0, metadata=_knob(builder_op="set_weights", constraint=KnobConstraint())
    )
    # Omitted at the default so the parity goldens and every existing contract
    # hash hold (``epoch/contract.py::scoring_to_canon``).
    diff_complexity_weight: float = field(
        default=0.0,
        metadata=_knob(
            omit_at_default=True,
            builder_op="set_namespace_weights",
            constraint=KnobConstraint(minimum=0),
        ),
    )
    diff_complexity_ceiling: float = field(
        default=0.0,
        metadata=_knob(
            omit_at_default=True,
            builder_op="set_namespace_weights",
            constraint=KnobConstraint(minimum=0),
        ),
    )
    # A TOLERANCE the challenger must clear, so a negative value is not an
    # aggressive setting but an inverted gate: the scalar rule
    # ``delta_scalar <= -promote_margin`` would then promote a challenger that
    # scored WORSE than the champion by up to the margin. Refused at contract
    # load, like every other out-of-domain knob.
    promote_margin: float = field(
        default=0.01,
        metadata=_knob(builder_op="set_gate", constraint=KnobConstraint(minimum=0)),
    )
    # The holdout confirmation's own bounds. Both are inert at their default
    # and omitted from the canonical form there, so no contract hash moves.
    holdout_margin: float | None = field(
        default=None,
        metadata=_knob(omit_at_default=True, builder_op="set_gate"),
    )
    holdout_entry_regression_budget: int = field(
        default=0,
        metadata=_knob(
            omit_at_default=True, builder_op="set_gate", constraint=KnobConstraint(minimum=0)
        ),
    )
    pass_rate_monotonicity: bool = field(
        default=True,
        metadata=_knob(builder_op="set_gate", builder_arg="monotonicity"),
    )
    pass_rate_monotonicity_scope: PassRateMonotonicityScope = field(
        default="per_entry",
        metadata=_knob(
            builder_op="set_gate",
            builder_arg="monotonicity_scope",
            # The accepted tokens come from the annotation itself, so the
            # closed set is stated once.
            constraint=KnobConstraint(choices=get_args(PassRateMonotonicityScope)),
        ),
    )
    regression_gate_enabled: bool = field(default=False, metadata=_knob(builder_op="set_gate"))
    regression_test_command: tuple[str, ...] = field(
        default=("pytest", "tests/", "-q"),
        metadata=_knob(builder_op="set_gate"),
    )
    regression_timeout_s: int = field(
        default=600,
        metadata=_knob(builder_op="set_gate", constraint=KnobConstraint(minimum=1)),
    )
    # Multi-objective surface — see the helpers above for the rationale
    # behind the default coefficient choices.
    namespace_weights: Mapping[str, float] = field(
        default_factory=_default_namespace_weights,
        metadata=_knob(builder_op="set_namespace_weights"),
    )
    namespace_monotonicity: Mapping[str, bool] = field(
        default_factory=_default_namespace_monotonicity,
        metadata=_knob(builder_op="set_gate"),
    )
    tournament_structure: TournamentStructure = field(
        default_factory=_default_tournament_structure,
        metadata=_knob(builder_op="set_structure", builder_arg="structure"),
    )
    # Anti-overfitting controls (train/holdout split + proposer leakage
    # restriction). Modelled here so it factors into the contract hash
    # through the existing scoring canonicalizer with zero new plumbing:
    # changing any knob — or the one-time default-on rollout — rolls the
    # epoch. Default-on with a safe auto-degrade on small boards. See
    # :class:`OverfittingConfig` and ``docs/design/OVERFITTING.md``.
    overfitting: OverfittingConfig = field(default_factory=_default_overfitting_config)
    # Proposer-quality levers: best-of-N sampling + a self-critique pass
    # (FUNCTIONALITY-RECOMMENDATIONS.md §4.1). Modelled here so it factors
    # into the contract hash through the existing scoring canonicalizer with
    # zero new plumbing (the canonicalizer recurses into nested frozen
    # dataclasses): changing the best-of-N count or the critique flag rolls
    # the epoch. The DEFAULT (``best_of_n == 3``) samples a slate + critiques;
    # pin ``best_of_n: 1`` for the historical single-sample proposer. See
    # :class:`ProposerQualityConfig`.
    proposer_quality: ProposerQualityConfig = field(
        default_factory=_default_proposer_quality_config
    )
    # Experiment-memory scoping (EXPERIMENT-MEMORY.md §3.4): opt-in
    # cross-epoch transfer of settled history under the SAME contract
    # hash. Default-off ⇒ same-epoch-only, byte-identical digest; the
    # contract canonicalizer omits the field at its default (see
    # ``_SCORING_OMIT_AT_DEFAULT_FIELDS``) so existing epochs never roll
    # retroactively, while opting in rolls the epoch like any other
    # contract change. See :class:`ExperimentMemoryConfig`.
    experiment_memory: ExperimentMemoryConfig = field(
        default_factory=_default_experiment_memory_config,
        metadata=_knob(omit_at_default=True),
    )
    # Opt-ins for features without a measured case (issue #394's
    # graduation namespace). Omitted from the canonical form while every
    # flag is off, so a contract naming none of them keeps its hash; a
    # flag turned on rolls the epoch. See :class:`ExperimentalConfig`.
    experimental: ExperimentalConfig = field(
        default_factory=_default_experimental_config,
        metadata=_knob(omit_at_default=True),
    )
    goldfive: Mapping[str, Any] | None = field(
        default=None,
        metadata=_knob(
            omit_at_default=True,
            builder_op="set_goldfive",
            builder_arg="config",
        ),
    )
    # Optional operator outcome-summarizer hook (Capability 2 of issue #18,
    # item 8). A dotted spec (``pkg.mod:fn`` / ``pkg.mod.fn``) resolved like
    # predicates / judges. The resolved callable receives the TRAIN-SLICE
    # per-entry results and returns a STRUCTURED aggregate — a
    # ``{marginal_name: numeric_rate}`` mapping, NOT prose — so zicato can
    # ENFORCE bucketing + anonymity on its output (it sanitizes + bands the
    # returned values before they reach the proposer; see
    # :func:`zicato.analyzer.outcome_marginals.run_operator_summarizer`). The
    # empty string (the default) configures NO summarizer, so the proposer
    # prompt is byte-identical to the default path. Because it is a plain
    # ``ScoringWeights`` field, it folds into the field-enumerating contract
    # serde + canonicalizer automatically: configuring (or changing) the spec
    # rolls the epoch, exactly like every other contract field.
    outcome_summarizer_spec: str = ""
    # Declarative scoring transforms (issue #19). Each is a single
    # ``{"op": "<name>", ...params}`` spec from the
    # :mod:`zicato.scoring.transforms` registry (``linear`` / ``pow`` /
    # ``harmonic`` / ``cap`` / ``clip`` / ``log1p``). Single op per slot — NO
    # pipelines (arbitrary multi-step logic belongs to a ``scalar_fn`` /
    # ``drift_reducer`` plugin). Specs are validated fail-fast in
    # ``__post_init__`` so a malformed transform is rejected at contract load,
    # never producing a NaN mid-scoring. Both fold into the field-enumerating
    # contract serde + canonicalizer automatically (plain dict / mapping
    # fields), so configuring or changing a transform rolls the epoch and
    # omitting one provokes no spurious roll.
    #
    # ``pass_transform`` reshapes the scalar's pass/miss term (the
    # ``(1 - mean_score)`` recall miss) at Seam 2 — the declarative replacement
    # for the retired ``pass_exponent`` field (express ``pass_exponent=2`` as
    # ``{"op":"pow","exponent":2.0}``; a stray ``pass_exponent`` key is now
    # rejected at load rather than lowered). ``None`` (the default) is NEUTRAL =
    # ``linear`` = the plain linear miss term.
    pass_transform: Mapping[str, Any] | None = None
    # ``drift_kind_aggregation`` reshapes, per drift KIND, how that kind's
    # count aggregates into the drift loss at Seam 1. A diminishing-returns
    # rule for ``looping_reasoning`` is opted into here
    # (``{"looping_reasoning": {"op": "harmonic"}}``) for THIS contract only,
    # rather than applied unconditionally. An absent kind entry is NEUTRAL =
    # ``linear`` = ``severity × kind_weight × count``, the built-in rule.
    drift_kind_aggregation: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    # Dotted-spec scoring PLUGINS (issue #19) — the escape hatch for
    # arbitrary operator scoring logic the declarative registry cannot express
    # (F-beta, cost-aware penalties, the retired harmonic-looping curve as a
    # ~10-line operator plugin). Each is a dotted spec (``pkg.mod:fn`` /
    # ``pkg.mod.fn``) resolved by the SAME importer predicates / judges use, and
    # invoked as a PURE, deterministic, NO-LLM function over the matching frozen
    # context (which carries the post-transform value as ``builtin_*`` so the
    # plugin WRAPS the declarative shape rather than reimplementing it). The
    # empty string (the default) configures NO plugin = the transform-or-builtin path
    # exactly. Both fold into the contract hash via the field-enumerating
    # canonicalizer — and the canonicalizer additionally hashes the resolved
    # plugin MODULE's SOURCE (``spec_with_source_hash``), so editing a plugin
    # body rolls the epoch. A plugin that raises / returns a non-finite value
    # fails OPEN to the pre-plugin value (logged + recorded in provenance), never
    # crashing the run. Validated at construction only as strings; resolution
    # happens at scoring time.
    #
    # ``drift_reducer`` is Seam 1 — it runs INSIDE the killable worker
    # subprocess, so it (like ``drift_kind_aggregation``) MUST cross the
    # ``_weights_spec`` boundary or the worker would score drift with no plugin
    # while the orchestrator believed otherwise (the per_judge_weights desync
    # class). ``scalar_fn`` is Seam 2 — it runs in the orchestrator.
    drift_reducer: str = ""
    scalar_fn: str = ""
    # Threaded to both the orchestrator and the killable worker through the
    # same field-enumerating serde that carries ``drift_reducer`` across the
    # worker boundary, so the two never score under different dialects.
    telemetry_dialect: str = field(
        default=DIALECT_GOLDFIVE,
        metadata=_knob(
            omit_at_default=True,
            builder_op="set_telemetry_dialect",
            builder_arg="dialect",
            constraint=KnobConstraint(choices=tuple(sorted(KNOWN_TELEMETRY_DIALECTS))),
        ),
    )
    # The two integrity blocking modes share the containment rule with the
    # supervisor (``crates/supervisor/src/diff_containment.rs``) and the gate
    # rule with its ``promotion_gate.rs check_row``; both stay alarm-only at
    # their default.
    block_on_containment_violation: bool = field(
        default=False,
        metadata=_knob(omit_at_default=True, builder_op="set_gate"),
    )
    block_on_gate_contradiction: bool = field(
        default=False,
        metadata=_knob(omit_at_default=True, builder_op="set_gate"),
    )
    # Folded over ``zicato.mutation.markers.BUILTIN_SYNTAXES`` and validated
    # by ``markers.syntax_table_from_config`` alone: core must not import
    # mutation, so no second validator lives here.
    mutation_surface: Mapping[str, Mapping[str, Any]] = field(
        default_factory=dict,
        metadata=_knob(omit_at_default=True, builder_op="set_mutation_surface"),
    )

    def __post_init__(self) -> None:
        """Validate the contract fail-fast at construction.

        Runs at contract load (the loader builds a :class:`ScoringWeights`
        from ``scoring.json``), so an out-of-range knob, a non-finite weight
        and a malformed transform — unknown op, non-finite / missing / typo'd
        param — are all rejected here with a clear error rather than silently
        defaulting or surfacing as a ``NaN`` scalar partway through a run. By
        the time the scoring dispatchers call
        :func:`zicato.scoring.transforms.apply_transform`, every spec on this
        instance is already known-good.

        Each knob's admissible range or closed vocabulary is declared on the
        field itself and applied by
        :func:`~zicato.core.constraints.validate_knobs`; the checks written
        out below are the rules a declaration cannot carry — cross-field
        invariants, mapping contents, and bounds whose message must say more
        than the bound.

        The dotted-spec scoring PLUGINS (``drift_reducer`` / ``scalar_fn``,
        issue #19) are validated HERE only as strings — resolution +
        invocation happen at scoring time (the worker resolves ``drift_reducer``
        itself), and a not-yet-written plugin must still construct so the
        contract can be hashed with the spec string + a degraded source hash.
        """
        from zicato.scoring.transforms import validate_transform_spec  # noqa: PLC0415

        if self.goldfive is not None:
            if not isinstance(self.goldfive, Mapping):
                raise ValueError("goldfive must be an object or null")
            object.__setattr__(self, "goldfive", _freeze_json(self.goldfive))
        validate_knobs(self)
        if self.holdout_margin is not None:
            require_finite_number("holdout_margin", self.holdout_margin)
        require_finite_mapping("severity_weights", self.severity_weights)
        require_finite_mapping("per_kind_weights", self.per_kind_weights)
        require_finite_mapping("per_judge_weights", self.per_judge_weights)
        require_finite_mapping("namespace_weights", self.namespace_weights)

        # A run that did not complete is charged in the ``failure:`` channel,
        # so a contract that zeroes (or omits) that channel makes crashing
        # free — and a challenger can then win by failing fast. The invariant
        # is enforced at load rather than left to operator discipline because
        # the failure mode is silent: the scalar simply stops seeing aborts.
        # An explicit namespace_weights mapping replaces the defaults, so
        # omission is the same statement as 0.0 and is rejected the same way.
        failure_weight = float(self.namespace_weights.get("failure:", 0.0))
        if failure_weight <= 0.0:
            raise ValueError(
                'namespace_weights["failure:"] must be present and > 0 (got '
                f"{failure_weight!r}): the failure: channel carries the "
                "task-failure and not-completed terms, and a contract must "
                "not be able to make crashing free. Dampen it with a small "
                "positive coefficient instead of zeroing it."
            )
        # ``custom`` / ``custom:<judge>`` drift is scored in the ``judge:``
        # channel via per_judge_weights, never through per_kind_weights, so a
        # per_kind_weights entry for it would be silently inert. Reject it
        # rather than let an operator believe they have retuned their judges.
        if "custom" in self.per_kind_weights:
            raise ValueError(
                'per_kind_weights["custom"] is inert: custom-judge drift is '
                "scored in the judge: channel. Use per_judge_weights "
                "{judge_name: weight} to retune one judge, or "
                'namespace_weights {"judge:": w} to retune the channel.'
            )

        if self.pass_transform is not None:
            validate_transform_spec(self.pass_transform)
        for kind, spec in self.drift_kind_aggregation.items():
            try:
                validate_transform_spec(spec)
            except ValueError as exc:
                raise ValueError(f"drift_kind_aggregation[{kind!r}]: {exc}") from exc
        for plugin_field in ("drift_reducer", "scalar_fn"):
            value = getattr(self, plugin_field)
            if not isinstance(value, str):
                raise ValueError(
                    f"{plugin_field} must be a dotted-spec string (got "
                    f"{type(value).__name__}); resolution happens at scoring time"
                )
        # The holdout confirmation's own margin (issue #118). A tolerance, so a
        # negative value is meaningless rather than merely aggressive — it would
        # invert the confirmation into a bar the holdout must clear. Stated here
        # rather than as a declared bound so the message can name the fallback
        # the ``None`` token selects.
        if self.holdout_margin is not None and self.holdout_margin < 0.0:
            raise ValueError(
                f"holdout_margin must be >= 0 (or None to reuse promote_margin), "
                f"got {self.holdout_margin!r}"
            )
        # An experimental structure is admitted by the contract's own opt-in,
        # checked here so a hand-edited scoring.json is refused at load
        # rather than at round start, after the epoch has already rolled.
        structure = self.tournament_structure.structure
        if (
            structure in EXPERIMENTAL_TOURNAMENT_STRUCTURES
            and not self.experimental.tournament_structures
        ):
            raise ValueError(experimental_structure_refusal(structure))

    def to_json(self) -> dict[str, Any]:
        """Serialise to a JSON-shaped dict via the field-enumerating serde.

        The single source of truth for putting a :class:`ScoringWeights` on
        the wire — used by BOTH the tournament runner (to hand weights to the
        subprocess worker) and the frozen-contract snapshot. Because it walks
        ``dataclasses.fields()`` (see
        :mod:`zicato.epoch.contract_serde`) it covers every applicable field,
        recursing into nested config dataclasses. An inactive optional
        integration is omitted. Adding a field can therefore never silently
        desync the worker into scoring under defaults — the historical
        ``per_judge_weights`` /
        ``pass_rate_monotonicity_scope`` / ``drift_kind_aggregation`` desync
        class that two hand-aligned field lists kept re-introducing.

        :meth:`from_json` is the exact inverse:
        ``ScoringWeights.from_json(w.to_json()) == w`` for every field.
        """
        from zicato.epoch.contract_serde import dataclass_to_jsonable  # noqa: PLC0415

        serialized = dataclass_to_jsonable(self)
        if self.goldfive is None:
            serialized.pop("goldfive")
        return serialized

    @classmethod
    def from_json(cls, data: Mapping[str, Any] | None) -> ScoringWeights:
        """Reconstruct from a :meth:`to_json` dict, the inverse of it.

        Tolerant of a partial / absent payload — a key absent from ``data``
        falls back to the field's dataclass default (so a caller that does
        not care about scoring weights, e.g. a stub-adapter test, can pass
        ``None`` / ``{}`` and still get a usable default-weighted instance).
        ``__post_init__`` re-validates the reconstructed transform specs, so a
        corrupt payload fails fast here. Used by the subprocess worker to
        rebuild the weights the runner serialised with :meth:`to_json`.

        Defensive coercion: ``pass_rate_monotonicity_scope`` is a closed
        ``Literal``; a token outside ``{"per_entry", "aggregate"}`` (a
        corrupt / future args file) is coerced back to the default rather
        than letting an out-of-domain string desync the worker's gate-view
        from the parent's (issue #17). The field-enumerating serde itself
        passes a bare ``Literal`` token through unchanged, so this guard
        lives here at the deserialise seam.
        """
        from zicato.epoch.contract_serde import jsonable_to_dataclass  # noqa: PLC0415

        if not isinstance(data, Mapping):
            return cls()
        raw_scope = data.get("pass_rate_monotonicity_scope")
        if raw_scope is not None and raw_scope not in ("per_entry", "aggregate"):
            data = {**data, "pass_rate_monotonicity_scope": cls().pass_rate_monotonicity_scope}
        return jsonable_to_dataclass(cls, data)


def _freeze_json(value: Any) -> Any:
    """Copy a JSON-shaped value into immutable mappings and tuples."""
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("goldfive object keys must be strings")
        return MappingProxyType({key: _freeze_json(item) for key, item in value.items()})
    if isinstance(value, list | tuple):
        return tuple(_freeze_json(item) for item in value)
    if value is not None and type(value) not in {bool, int, float, str}:
        raise ValueError("goldfive must contain only JSON-compatible values")
    return value


CONTRACT_KNOB_TYPES: tuple[type, ...] = (
    ScoringWeights,
    OverfittingConfig,
    LadderConfig,
    ProposerQualityConfig,
    ExperimentMemoryConfig,
    ExperimentalConfig,
)


@dataclass(frozen=True, slots=True)
class ContractKnob:
    """One runtime-derived scoring-contract field declaration."""

    owner: type
    name: str
    default: object
    omit_at_default: bool
    builder_op: str | None
    builder_arg: str

    @property
    def key(self) -> str:
        return f"{self.owner.__name__}.{self.name}"


def contract_knobs() -> tuple[ContractKnob, ...]:
    """Return the scoring contract's field registry in declaration order."""

    knobs: list[ContractKnob] = []
    for owner in CONTRACT_KNOB_TYPES:
        for declared in fields(owner):
            if declared.default is not MISSING:
                default = declared.default
            elif declared.default_factory is not MISSING:
                default = declared.default_factory()
            else:
                default = MISSING
            knobs.append(
                ContractKnob(
                    owner=owner,
                    name=declared.name,
                    default=default,
                    omit_at_default=bool(declared.metadata.get("omit_at_default")),
                    builder_op=declared.metadata.get("builder_op"),
                    builder_arg=declared.metadata.get("builder_arg") or declared.name,
                )
            )
    return tuple(knobs)


def omit_at_default_fields() -> frozenset[str]:
    """Names omitted from canonical scoring while equal to their defaults."""

    return frozenset(knob.name for knob in contract_knobs() if knob.omit_at_default)


def recommended_scaffold_weights() -> ScoringWeights:
    """The FULL effective contract new-workspace scaffolds write out.

    Shared by ``zicato init`` (which writes it to the operator's live
    ``scoring.json``) and the tournament builder's blank draft, so both
    scaffolds spell the SAME recommended contract explicitly instead of
    leaning on invisible defaults: the racing structure (field 4, eta 2,
    board_fraction 0.4), two averaged replicates per duel, and the
    Bradley--Terry evidence gate ENABLED EXPLICITLY (threshold 0.8 with a
    32-replicate budget). The gate is NOT a silent in-code
    default — its CIs separate only after a long unbroken win streak (~37
    duels on a two-contestant pair), so it needs an honest budget the
    operator can see and price: under racing the crowning-pair replicates
    amortize through the per-unit cache, and the builder's cost meter
    reflects the ``replicates`` knob. Everything else is the dataclass
    default; the field-enumerating serializer then writes every applicable
    field. Optional integration blocks remain absent until selected.

    A pure recommendation for NEW workspaces — the in-code default
    structure when a contract says nothing remains the gauntlet, with no
    pre-gate.
    """
    # Function-local import: core is the base layer; the selection package
    # (which imports core) owns the recommended evidence-gate bar.
    from zicato.selection.evidence_gate import (  # noqa: PLC0415
        DEFAULT_PROMOTE_CONFIDENCE_THRESHOLD,
    )

    return ScoringWeights(
        tournament_structure=TournamentStructure(
            structure="racing",
            params={
                "field_size": 4,
                "eta": 2,
                "board_fraction": 0.4,
                "replicates": 2,
                "promote_confidence_threshold": DEFAULT_PROMOTE_CONFIDENCE_THRESHOLD,
                # An honest defer→replicate budget: CI separation on a
                # two-contestant crowning pair needs ~32+ decisive duels,
                # each a cheap cache-amortized re-read under racing.
                "promote_confidence_replicates": 32,
            },
        ),
        # Pre-tournament candidate screening (tryouts), enabled EXPLICITLY
        # like the evidence gate: each best-of-N slate candidate runs on a
        # 2-entry rotating train panel before selection, and a candidate
        # with a confirmed catastrophic regression (a pass-flip on a
        # champion-passing entry, or a budget abort) is vetoed before it
        # can reach the tournament. Veto-first: the screen never ranks —
        # the critic still chooses among the survivors. The in-code
        # default stays OFF (``screen_entries=0``); the scaffold is where
        # an operator sees and prices the extra
        # proposes × best_of_n × screen_entries panel runs.
        proposer_quality=ProposerQualityConfig(screen_entries=2),
    )
