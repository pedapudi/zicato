"""Scoring-config types: :class:`ScoringWeights` and its nested config blocks.

Split out of :mod:`zicato.core.types`; re-exported from there and from
:mod:`zicato.core` so existing import paths keep working.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Literal, get_args

from zicato.core.constraints import (
    KnobConstraint,
    require_finite_mapping,
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
#: overlap. See :attr:`ExperimentalConfig.recombine_merge`.
RECOMBINE_MERGE_MODES: tuple[str, ...] = ("mechanical", "llm")


# ---------------------------------------------------------------------------
# Declarative knob metadata (REIMPLEMENTATION.md — Finding 3)
# ---------------------------------------------------------------------------


def _knob(
    *,
    persisted_name: str | None = None,
    description: str | None = None,
    constraint: KnobConstraint | None = None,
) -> dict[str, Any]:
    """Declare a field's stored name, description, and accepted values."""
    return {
        "persisted_name": persisted_name,
        "description": description,
        "constraint": constraint,
    }


# ---------------------------------------------------------------------------
# Scoring config (overfitting / proposer-quality sub-configs)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LadderConfig:
    """Budgeted release of holdout confirmations within an epoch.

    Each query charges one unit of the configured budget. A holdout signal
    is released when train-measured improvement reaches the release threshold;
    otherwise the previous released result is retained. Exhausting the budget
    leaves subsequent training decisions unconfirmed by the holdout.
    Required confirmation therefore defers promotion.

    These controls limit feedback. Their configuration does not state a
    calibrated privacy guarantee or a measured generalization bound.

    Fields
    ------
    enabled:
        Switches the Ladder governor on. On by default. When off, the
        holdout confirmation runs unmediated: every query is answered, with
        no budget and no release rule. Disabling this governor leaves the
        holdout confirmation requirement enabled.
    threshold:
        The train-side improvement a round must show before a holdout
        signal is released at all. Unset by default, which derives the bar
        from ``promote_margin`` so the Ladder reuses the gate's noise
        threshold; a number pins it. Raising it withholds the holdout query
        from a round that clears the gate on train, and that train promote
        then defers. To widen the tolerance of the confirmation
        that runs after release, set ``holdout_margin`` instead. Must be
        ``>= 0``.
    budget:
        Per-epoch holdout-query budget. Each round that consults the
        holdout charges one. When the budget is spent the runner stops
        consulting the holdout and required confirmation defers promotion.
        The finite budget limits adaptive feedback; it does not establish a
        statistical validity guarantee by itself. Must be ``>= 0``; ``0``
        permits no holdout-confirmed promotion.
    """

    enabled: bool = field(default=True, metadata=_knob())
    threshold: float | None = field(
        default=None,
        metadata=_knob(
            constraint=KnobConstraint(minimum=0.0, allow_none=True, label="ladder.threshold"),
        ),
    )
    budget: int = field(
        default=16,
        metadata=_knob(
            constraint=KnobConstraint(minimum=0, label="ladder.budget"),
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

    Each field entry below describes the accepted value and its effect.

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
    """

    enabled: bool = field(default=True, metadata=_knob())
    holdout_fraction: float = field(
        default=0.3,
        metadata=_knob(
            constraint=KnobConstraint(
                minimum=0, maximum=1, exclusive_minimum=True, exclusive_maximum=True
            ),
        ),
    )
    min_board_size_for_split: int = field(
        default=6,
        metadata=_knob(constraint=KnobConstraint(minimum=0)),
    )
    restrict_proposer_visibility: bool = field(default=True, metadata=_knob())
    ladder: LadderConfig = field(default_factory=_default_ladder_config, metadata=_knob())
    rotate_holdout: bool = field(default=True, metadata=_knob())

    def __post_init__(self) -> None:
        validate_knobs(self)

    @classmethod
    def defaults(cls) -> OverfittingConfig:
        """The fully-defaulted (default-on) config an absent block resolves to."""
        return cls()


def _default_overfitting_config() -> OverfittingConfig:
    """Default-factory for :attr:`ScoringWeights.overfitting`."""
    return OverfittingConfig.defaults()


@dataclass(frozen=True, slots=True)
class ProposerQualityConfig:
    """Candidate sampling, critique, and pre-tournament screening.

    Sampling and critique select among a slate of proposals. Screening vetoes
    catastrophic regressions using a rotating training panel. The critic and
    proposer share the restricted visibility policy.

    Fields
    ------
    best_of_n:
        Candidates sampled per proposal. One bypasses slate selection.
    critique_enabled:
        Use a critique call to select the slate winner; otherwise use the
        configured heuristic. Inert when best_of_n is one.
    screen_entries:
        Training entries sampled per slate candidate before selection. The
        default panel has two entries; zero disables screening.
    screen_veto_only:
        Use screening only to veto candidates. When false, banded screening
        counts may also advise the final selection.
    """

    best_of_n: int = field(
        default=3,
        metadata=_knob(constraint=KnobConstraint(minimum=1)),
    )
    critique_enabled: bool = field(
        default=True,
        metadata=_knob(),
    )
    screen_entries: int = field(
        default=2,
        metadata=_knob(
            constraint=KnobConstraint(minimum=0),
        ),
    )
    screen_veto_only: bool = field(
        default=False,
        metadata=_knob(),
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
class ExperimentalConfig:
    """Optional optimization features awaiting complete-loop qualification.

    Every feature is inactive by default. Recommended settings leave this
    block at its defaults. Safety enforcement remains ordinary policy.
    Enabling a feature changes the evaluation contract.

    Fields
    ------
    tournament_structures:
        Admit experimental elimination and Swiss tournament structures.
    process_exemplars:
        Maximum redacted training-event windows added to each proposal.
    recombine:
        Replace a slate slot with a combination of rejected candidates.
    recombine_merge:
        Compose a disjoint patch union mechanically, or request a merge.
        The merge mode is inactive while recombine is false.
    genealogy:
        Maximum candidate ancestry examples added to each proposal.
    calibration_feedback:
        Maximum graded prediction examples added to each proposal.
    random_baseline_every_n:
        Run an additional unchanged candidate every N rounds. Zero disables it.
    max_generations_per_contract:
        Recommend contract refresh after this many generations; null disables it.
    diff_complexity_weight:
        Coefficient for the edit-complexity penalty. Zero disables it.
    diff_complexity_ceiling:
        Reject edits above this complexity. Zero disables it.
    cross_epoch_memory:
        Include prior-epoch experiment history under the same contract identity.
    standing_rating:
        Fit candidate standings with Bradley–Terry, or retain ordinary standings.
    resolver:
        Nominate an internal leader with Copeland or Ranked Pairs. The final
        champion comparison still uses the configured promotion gate.
    """

    tournament_structures: bool = field(
        default=False,
        metadata=_knob(),
    )

    max_generations_per_contract: int | None = field(
        default=None,
        metadata=_knob(
            constraint=KnobConstraint(minimum=1, allow_none=True),
        ),
    )
    random_baseline_every_n: int = field(
        default=0,
        metadata=_knob(
            constraint=KnobConstraint(minimum=0),
        ),
    )
    process_exemplars: int = field(
        default=0,
        metadata=_knob(
            constraint=KnobConstraint(minimum=0),
        ),
    )
    recombine: bool = field(
        default=False,
        metadata=_knob(),
    )
    genealogy: int = field(
        default=0,
        metadata=_knob(
            constraint=KnobConstraint(minimum=0),
        ),
    )
    calibration_feedback: int = field(
        default=0,
        metadata=_knob(
            constraint=KnobConstraint(minimum=0),
        ),
    )
    recombine_merge: str = field(
        default="mechanical",
        metadata=_knob(
            constraint=KnobConstraint(choices=RECOMBINE_MERGE_MODES),
        ),
    )
    diff_complexity_weight: float = field(
        default=0.0,
        metadata=_knob(
            constraint=KnobConstraint(minimum=0),
        ),
    )
    diff_complexity_ceiling: float = field(
        default=0.0,
        metadata=_knob(
            constraint=KnobConstraint(minimum=0),
        ),
    )
    cross_epoch_memory: bool = field(
        default=False,
        metadata=_knob(),
    )
    standing_rating: Literal["none", "bradley_terry"] = field(
        default="none",
        metadata=_knob(),
    )
    resolver: Literal["none", "copeland", "ranked_pairs"] = field(
        default="none",
        metadata=_knob(),
    )

    def __post_init__(self) -> None:
        validate_knobs(self)
        if self.standing_rating not in ("none", "bradley_terry"):
            raise ValueError("experimental.standing_rating must be none or bradley_terry")
        if self.resolver not in ("none", "copeland", "ranked_pairs"):
            raise ValueError("experimental.resolver must be none, copeland or ranked_pairs")

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

    Each field entry below describes the accepted value and its effect.

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
    promote_margin:
        Minimum scalar improvement (champion loss minus challenger loss) a
        challenger must show to be promoted. A larger margin demands a
        more decisive win and resists noise; ``0`` promotes on any
        improvement. Without the evidence gate the margin must clear the
        measured same-versus-same noise floor. Calibrated against the train slice; see
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
        Validated by ``markers.syntax_table_from_config``.
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

    pass_weight: float = field(default=1.0, metadata=_knob(constraint=KnobConstraint()))
    severity_weights: Mapping[str, float] = field(
        default_factory=_default_severity_weights,
        metadata=_knob(),
    )
    per_kind_weights: Mapping[str, float] = field(default_factory=dict, metadata=_knob())
    per_judge_weights: Mapping[str, float] = field(default_factory=dict, metadata=_knob())
    default_judge_weight: float = field(default=1.0, metadata=_knob(constraint=KnobConstraint()))
    plan_revision_weight: float = field(default=0.5, metadata=_knob(constraint=KnobConstraint()))
    # The two ``failure:`` channel magnitudes. They live on the contract (not
    # as module constants) so retuning them rolls the epoch through the normal
    # hash mechanism — a mid-epoch retune would otherwise let the unit cache
    # fold old- and new-formula losses together undetectably.
    task_failure_weight: float = field(default=10.0, metadata=_knob(constraint=KnobConstraint()))
    not_completed_weight: float = field(default=50.0, metadata=_knob(constraint=KnobConstraint()))
    # Omitted at the default so the parity goldens and every existing contract
    # hash hold (``epoch/contract.py::scoring_to_canon``).
    # A TOLERANCE the challenger must clear, so a negative value is not an
    # aggressive setting but an inverted gate: the scalar rule
    # ``delta_scalar <= -promote_margin`` would then promote a challenger that
    # scored WORSE than the champion by up to the margin. Refused at contract
    # load, like every other out-of-domain knob.
    promote_margin: float = field(
        default=0.01,
        metadata=_knob(constraint=KnobConstraint(minimum=0)),
    )
    # The holdout confirmation's own bounds. Both are inert at their default
    # and omitted from the canonical form there, so no contract hash moves.
    holdout_margin: float | None = field(
        default=None,
        metadata=_knob(
            constraint=KnobConstraint(minimum=0, allow_none=True),
        ),
    )
    holdout_entry_regression_budget: int = field(
        default=0,
        metadata=_knob(constraint=KnobConstraint(minimum=0)),
    )
    pass_rate_monotonicity: bool = field(
        default=True,
        metadata=_knob(),
    )
    pass_rate_monotonicity_scope: PassRateMonotonicityScope = field(
        default="per_entry",
        metadata=_knob(
            # The accepted tokens come from the annotation itself, so the
            # closed set is stated once.
            constraint=KnobConstraint(choices=get_args(PassRateMonotonicityScope)),
        ),
    )
    regression_gate_enabled: bool = field(default=False, metadata=_knob())
    regression_test_command: tuple[str, ...] = field(
        default=("pytest", "tests/", "-q"),
        metadata=_knob(),
    )
    regression_timeout_s: int = field(
        default=600,
        metadata=_knob(constraint=KnobConstraint(minimum=1)),
    )
    # Multi-objective surface — see the helpers above for the rationale
    # behind the default coefficient choices.
    namespace_weights: Mapping[str, float] = field(
        default_factory=_default_namespace_weights,
        metadata=_knob(),
    )
    namespace_monotonicity: Mapping[str, bool] = field(
        default_factory=_default_namespace_monotonicity,
        metadata=_knob(),
    )
    tournament_structure: TournamentStructure = field(
        default_factory=_default_tournament_structure,
        metadata=_knob(persisted_name="tournament"),
    )
    # Anti-overfitting controls (train/holdout split + proposer leakage
    # restriction). Modelled here so it factors into the contract hash
    # through the existing scoring canonicalizer with zero new plumbing:
    # changing any knob — or the one-time default-on rollout — rolls the
    # epoch. Default-on with a safe auto-degrade on small boards. See
    # :class:`OverfittingConfig` and ``docs/design/OVERFITTING.md``.
    overfitting: OverfittingConfig = field(
        default_factory=_default_overfitting_config,
        metadata=_knob(
            description="Training and holdout partitions, confirmation, and information limits."
        ),
    )
    # Proposer-quality levers: best-of-N sampling + a self-critique pass
    # (FUNCTIONALITY-RECOMMENDATIONS.md §4.1). Modelled here so it factors
    # into the contract hash through the existing scoring canonicalizer with
    # zero new plumbing (the canonicalizer recurses into nested frozen
    # dataclasses): changing the best-of-N count or the critique flag rolls
    # the epoch. The DEFAULT (``best_of_n == 3``) samples a slate + critiques;
    # pin ``best_of_n: 1`` for the historical single-sample proposer. See
    # :class:`ProposerQualityConfig`.
    proposer_quality: ProposerQualityConfig = field(
        default_factory=_default_proposer_quality_config,
        metadata=_knob(
            description="Candidate sampling, critique, screening, and field composition."
        ),
    )
    # Optional features remain explicit in stored settings and contract identity.
    experimental: ExperimentalConfig = field(
        default_factory=_default_experimental_config,
        metadata=_knob(
            description="Explicit experimental evaluation and proposal features.",
        ),
    )
    goldfive: Mapping[str, Any] | None = field(
        default=None,
        metadata=_knob(),
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
    outcome_summarizer_spec: str = field(
        default="",
        metadata=_knob(
            description=(
                "Importable function reducing training run results to numeric outcome "
                "marginals before proposer feedback is bucketed."
            )
        ),
    )
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
    drift_reducer: str = field(
        default="",
        metadata=_knob(
            description=(
                "Pure scoring function executed inside the killable worker. It receives the "
                "transformed built-in drift loss and must return a finite value."
            )
        ),
    )
    scalar_fn: str = field(
        default="",
        metadata=_knob(
            description=(
                "Pure aggregate scoring function executed in the coordinator. It receives "
                "the built-in aggregate scalar and must return a finite value."
            )
        ),
    )
    # Threaded to both the orchestrator and the killable worker through the
    # same field-enumerating serde that carries ``drift_reducer`` across the
    # worker boundary, so the two never score under different dialects.
    telemetry_dialect: str = field(
        default=DIALECT_GOLDFIVE,
        metadata=_knob(
            constraint=KnobConstraint(choices=tuple(sorted(KNOWN_TELEMETRY_DIALECTS))),
        ),
    )
    # The two integrity blocking modes share the containment rule with the
    # supervisor (``crates/supervisor/src/diff_containment.rs``) and the gate
    # rule with its ``promotion_gate.rs check_row``; both stay alarm-only at
    # their default.
    block_on_containment_violation: bool = field(
        default=False,
        metadata=_knob(),
    )
    block_on_gate_contradiction: bool = field(
        default=False,
        metadata=_knob(),
    )
    # Folded over ``zicato.mutation.markers.BUILTIN_SYNTAXES`` and validated
    # by ``markers.syntax_table_from_config`` alone: core must not import
    # mutation, so no second validator lives here.
    mutation_surface: Mapping[str, Mapping[str, Any]] = field(
        default_factory=dict,
        metadata=_knob(),
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
        # An experimental structure is admitted by the contract's own opt-in,
        # checked here so a hand-edited scoring.json is refused at load
        # rather than at round start, after the epoch has already rolled.
        for key in ("rating", "resolver"):
            if key in self.tournament_structure.params:
                field = "standing_rating" if key == "rating" else "resolver"
                raise ValueError(f"move tournament.params.{key} to experimental.{field}")
        structure = self.tournament_structure.structure
        if (
            structure in EXPERIMENTAL_TOURNAMENT_STRUCTURES
            and not self.experimental.tournament_structures
        ):
            raise ValueError(experimental_structure_refusal(structure))

    def to_json(self) -> dict[str, Any]:
        """Write every effective scoring setting for storage and worker execution."""
        from zicato.core.configuration import dataclass_to_jsonable  # noqa: PLC0415

        return dataclass_to_jsonable(self)

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> ScoringWeights:
        """Read scoring settings through the shared configuration validator."""
        return scoring_weights_from_dict(data)


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


def scoring_weights_from_dict(d: Mapping[str, Any]) -> ScoringWeights:
    """Validate authored scoring values before constructing the contract.

    Unknown fields, malformed nested blocks, and values outside their
    declared types or ranges raise with the persisted field path. Omitted
    fields use their declared defaults. Arbitrary keys remain valid in
    fields declared as mappings; their values follow the declared type.
    """
    from zicato.core.configuration import (  # noqa: PLC0415
        authored_dataclass_from_json,
    )

    if isinstance(d, Mapping):
        tournament = d.get("tournament")
        if isinstance(tournament, Mapping):
            default = ScoringWeights().tournament_structure
            resolved = dict(tournament)
            resolved.setdefault("structure", default.structure)
            resolved.setdefault(
                "params", dict(default.params) if resolved["structure"] == default.structure else {}
            )
            d = {**d, "tournament": resolved}
    return authored_dataclass_from_json(ScoringWeights, d, path="scoring")
