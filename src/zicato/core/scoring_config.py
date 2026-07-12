"""Scoring-config types: :class:`ScoringWeights` and its nested config blocks.

Split out of :mod:`zicato.core.types`; re-exported from there and from
:mod:`zicato.core` so existing import paths keep working.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from zicato.core.tournament import (
    PassRateMonotonicityScope,
    TournamentStructure,
    _default_tournament_structure,
)

# ---------------------------------------------------------------------------
# Scoring config (overfitting / proposer-quality sub-configs)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LadderConfig:
    """The Ladder/Thresholdout governor over the holdout query (OVERFITTING.md §4, §12 #2).

    Phase A built the train/holdout split and a holdout-*confirmation* step
    (:class:`OverfittingConfig`). This sub-config governs *how* that holdout
    is queried across an epoch's rounds, after Blum & Hardt's Ladder: a
    reused holdout stays valid under an adaptively-querying proposer only if
    every interaction with it is mediated by a mechanism that limits the
    information leaked back. The two rules:

    * **Release rule.** A new holdout-based signal is released only when the
      *train-measured* improvement clears the threshold beyond the noise
      band. Within the band the previous best is re-reported, so the
      proposer cannot chase board fluctuations.
    * **Budget.** Each holdout query charges a finite per-epoch budget; once
      exhausted, no further holdout signals are released (the loop degrades
      to "champion stands" — no holdout-gated promotion).

    Folded into the contract hash through :class:`OverfittingConfig` →
    :class:`ScoringWeights` (the canonicalizer recurses into nested frozen
    dataclasses), so changing any knob — or the one-time default-on rollout —
    rolls the epoch, exactly as retuning ``promote_margin`` does.

    Default-on with a safe auto-degrade: an empty holdout (small board, split
    disabled) means there is nothing to govern, and the Ladder is a no-op —
    behaviour stays byte-identical to Phase A.

    Fields
    ------
    enabled:
        Master switch for the Ladder governor. ``True`` by default. When
        ``False`` the holdout confirmation runs in its raw Phase-A form
        (every holdout query counts, no budget, no release rule).
    threshold:
        The train-improvement bar the release rule applies. ``None``
        (default) derives it from :attr:`ScoringWeights.promote_margin` so
        the Ladder reuses the gate's existing noise threshold; a float pins
        it explicitly.
    budget:
        Per-epoch holdout-query budget. Each round that consults the holdout
        charges one. When the budget is exhausted the Ladder stops releasing
        holdout signals. Must be ``>= 0`` (``0`` releases nothing).
    noise_scale:
        Width of the noise band added to the threshold. ``0.0`` (default) is
        the parameter-free Ladder — no calibration needed. Reserved for
        DP-grade noise calibration later; must be ``>= 0``.
    """

    enabled: bool = True
    threshold: float | None = None
    budget: int = 16
    noise_scale: float = 0.0

    def __post_init__(self) -> None:
        if self.threshold is not None and self.threshold < 0.0:
            raise ValueError(f"ladder.threshold must be >= 0 or None, got {self.threshold!r}")
        if self.budget < 0:
            raise ValueError(f"ladder.budget must be >= 0, got {self.budget!r}")
        if self.noise_scale < 0.0:
            raise ValueError(f"ladder.noise_scale must be >= 0, got {self.noise_scale!r}")

    @classmethod
    def defaults(cls) -> LadderConfig:
        """The fully-defaulted (default-on) config an absent block resolves to."""
        return cls()


def _default_ladder_config() -> LadderConfig:
    """Default-factory for :attr:`OverfittingConfig.ladder`."""
    return LadderConfig.defaults()


@dataclass(frozen=True, slots=True)
class OverfittingConfig:
    """Anti-overfitting controls: the train/holdout board split + leakage gate.

    Part of the frozen evaluation contract: it is modelled as a field of
    :class:`ScoringWeights` (and therefore folds into the contract hash
    automatically through the existing scoring canonicalizer), so changing
    any knob — or the one-time default-on rollout — rolls the epoch,
    exactly as retuning ``promote_margin`` does. A run that splits a holdout
    out of the board, and confirms promotions against it, selects champions
    under a different rule than one that does not, which is precisely the
    contract-roll rationale.

    Every field is default-on with a safe auto-degrade: a board too small
    to split (fewer than :attr:`min_board_size_for_split` entries, and no
    explicit ``holdout`` tag) yields an *empty* holdout, and the whole
    machine collapses to the pre-split behaviour byte-for-byte.

    Fields
    ------
    enabled:
        Master switch for the train/holdout split. ``True`` by default.
        When ``False``, no holdout is ever derived (an explicit
        ``holdout`` tag still wins — see :func:`zicato.board.split.split_board`)
        and the loop behaves exactly as it did before this phase.
    holdout_fraction:
        Target fraction of the board to hold out when the split is derived
        by hash (no explicit ``holdout`` tag). A deterministic, id-stable
        threshold selects approximately this fraction. Range ``(0, 1)``.
    min_board_size_for_split:
        Smallest board size at which a hash-derived split is attempted.
        Below this the holdout is empty (degrade to today's behaviour) so a
        small board is never starved of train entries. An explicit
        ``holdout`` tag overrides this floor.
    restrict_proposer_visibility:
        When ``True`` (default), the proposer prompt is sanitised at the
        render boundary: per-entry identities in the detector patterns are
        aggregated to counts/rates, and experiment-memory ``Δscalar`` is
        coarsened to ``improved``/``flat``/``regressed`` buckets. Turning
        it off restores the verbatim rendering byte-for-byte.
    ladder:
        The Ladder/Thresholdout governor over the holdout query
        (:class:`LadderConfig`; OVERFITTING.md §4 / §12 #2). Default-on;
        a no-op when the holdout is empty.
    rotate_holdout:
        When ``True`` (default), the hash-derived holdout *rotates* across
        epochs (OVERFITTING.md §7 / §12 #6): the epoch id is folded into the
        id-hash at the split call sites so a different ~``holdout_fraction``
        slice is held out each epoch — no fixed slice is mined forever.
        Stable *within* an epoch (the seed is the epoch id). When ``False``
        the unseeded split is used (the same slice every epoch). The
        rotation is an epoch-local derivation: it does NOT change the
        contract hash for an unchanged board — only this flag itself
        participates in the hash. An explicit ``holdout`` tag is never
        rotated.
    max_generations_per_contract:
        Optional cadence ceiling (OVERFITTING.md §9 / §12 #6, cross-ref
        SELECTION-THEORY.md §5 optimal-stopping horizon). When set, the loop
        surfaces a board-refresh *recommendation* (a health finding / logged
        signal) once a contract has been mined for this many generations —
        a cue that the contract should be refreshed (the operator rolls).
        ``None`` (default) imposes no ceiling. This never forces a surprising
        auto epoch-roll; it only recommends. Must be ``>= 1`` when set.
    random_baseline_every_n:
        Opt-in random-baseline challenger cadence (OVERFITTING.md §12 #7 —
        the placebo arm). When ``> 0``, every Nth round the orchestrator
        fields ONE additional challenger whose patch is a
        semantics-preserving no-op (the mutation point's current value
        re-emitted unchanged), hypothesis clearly marked as the baseline
        arm. The gate MUST reject it — identical trees leave no
        improvement to clear the margin — so a PROMOTED baseline is the
        alarm: the loop emits a CRITICAL ``placebo_promoted`` health
        finding (gate discrimination is broken; recent "wins" are
        suspect). ``0`` (default) fields no baseline and the loop is
        byte-identical. Like ``diff_complexity_weight``, the field is
        omitted from the contract canonical form at its default so
        existing epochs never roll retroactively. Must be ``>= 0``.
    """

    enabled: bool = True
    holdout_fraction: float = 0.3
    min_board_size_for_split: int = 6
    restrict_proposer_visibility: bool = True
    ladder: LadderConfig = field(default_factory=_default_ladder_config)
    rotate_holdout: bool = True
    max_generations_per_contract: int | None = None
    random_baseline_every_n: int = 0

    def __post_init__(self) -> None:
        if not 0.0 < self.holdout_fraction < 1.0:
            raise ValueError(f"holdout_fraction must be in (0, 1), got {self.holdout_fraction!r}")
        if self.min_board_size_for_split < 0:
            raise ValueError(
                f"min_board_size_for_split must be >= 0, got " f"{self.min_board_size_for_split!r}"
            )
        if self.max_generations_per_contract is not None and self.max_generations_per_contract < 1:
            raise ValueError(
                f"max_generations_per_contract must be >= 1 or None, got "
                f"{self.max_generations_per_contract!r}"
            )
        if self.random_baseline_every_n < 0:
            raise ValueError(
                f"random_baseline_every_n must be >= 0, got {self.random_baseline_every_n!r}"
            )

    @classmethod
    def defaults(cls) -> OverfittingConfig:
        """The fully-defaulted (default-on) config an absent block resolves to."""
        return cls()


def _default_overfitting_config() -> OverfittingConfig:
    """Default-factory for :attr:`ScoringWeights.overfitting`."""
    return OverfittingConfig.defaults()


@dataclass(frozen=True, slots=True)
class ProposerQualityConfig:
    """Proposer-quality levers: best-of-N sampling + a self-critique pass.

    Part of the frozen evaluation contract — modelled as a field of
    :class:`ScoringWeights` so it folds into the contract hash through the
    existing scoring canonicalizer (it recurses into nested frozen
    dataclasses), exactly like :class:`OverfittingConfig` and
    :class:`TournamentStructure`. Changing any knob rolls the epoch, which is
    correct: a proposer that samples N candidates and self-critiques proposes
    *differently* than one that samples once.

    The DEFAULT samples a slate: :attr:`best_of_n` ``= 3`` — three candidate
    experiments per propose-step with the self-critique pass selecting the
    best (the top proposal-quality lever; a valid-but-mediocre single sample
    was never reconsidered). Pin ``"proposer_quality": {"best_of_n": 1}`` for
    the historical single-sample, no-critique proposer (scripted / mock
    proposers do). Changing the value rolls the epoch, like every contract
    field. See ``docs/design/FUNCTIONALITY-RECOMMENDATIONS.md`` §4.1.

    Overfitting discipline (LOAD-BEARING): the self-critique pass sees ONLY
    the SAME restricted prompt context the proposer itself sees (the
    train-slice patterns, the banded experiment memory, the bucketed
    failure-mode profile) — NEVER the holdout, never a per-entry identity.
    The critic is inside the same overfitting-visibility envelope as the
    proposer (OVERFITTING.md §11); it cannot widen what the proposer is
    allowed to learn about the board.

    Fields
    ------
    best_of_n:
        How many candidate experiments to sample per propose-step before
        the critique pass picks the best. ``3`` (default) samples a slate;
        ``1`` is the historical single sample with no critique. Must be
        ``>= 1``. Each sample is an independent inner ``propose`` (the LLM's
        own sampling supplies the variety, and each slate slot carries a
        distinct edit-class hint — see
        :data:`zicato.proposer.best_of_n.EDIT_CLASS_HINTS`); a candidate that
        the inner proposer cannot produce simply narrows the slate, and an
        empty slate falls back to a final inner ``propose`` so the step never
        silently yields nothing.
    critique_enabled:
        When ``True`` (default) and ``best_of_n > 1``, a single cheap
        auxiliary-LLM self-critique pass scores the sampled candidates
        against a quality bar (grounded in a tool call? targets a real
        failure mode? minimal diff?) and selects the best. When ``False``,
        best-of-N still samples ``best_of_n`` candidates but the selection
        falls back to the deterministic built-in heuristic (smallest diff
        that targets an observed failure mode) — no extra LLM call. With
        ``best_of_n == 1`` this flag is inert (no critique ever runs).
    screen_entries:
        Opt-in pre-tournament candidate screening (tryouts). When ``> 0``
        AND ``best_of_n > 1``, each best-of-N slate candidate is RUN on a
        small rotating panel of this many TRAIN board entries before the
        selection pass — VETO-FIRST semantics: the screen only
        disqualifies catastrophic regressions (a confirmed pass-flip on a
        champion-passing entry, or a budget abort); it never ranks, and
        the critic/heuristic still chooses among the survivors. ``0``
        (default) is OFF — the orchestrator does not even construct a
        screen callable, so the propose path is byte-identical. With
        ``best_of_n == 1`` the knob is inert (there is no slate to
        screen). Like ``random_baseline_every_n``, the field is omitted
        from the contract canonical form at its default so existing
        epochs never roll retroactively; a non-zero value rolls the
        epoch, which is correct — a proposer whose slate is screened
        selects differently. Must be ``>= 0``. See
        :mod:`zicato.epoch.screen`.

        The screen-informed revise pass RIDES this knob (no separate
        lever, deliberately): when a screened slate ends all-vetoed, the
        wrapper takes exactly one feedback-informed re-sample before
        degrading to critic-over-all. An all-vetoed slate with no revise
        wastes the whole propose step on a known-vetoed candidate, so
        the single re-sample is the cheapest possible recovery — and a
        contract that opted into paying for the screen has already
        accepted that propose-step cost class. See
        :meth:`zicato.proposer.best_of_n.BestOfNProposerAgent`.
    screen_veto_only:
        When ``True``, the screen's measurements feed NOTHING but the
        veto: the critic prompt carries no ``## Screen measurements``
        block and the deterministic heuristic ignores the panel scalar
        tiebreak — the screen can only disqualify, never nudge the
        ordering. ``False`` (default) lets the survivors' banded panel
        counts advise the selection as a late tiebreak. Inert while
        ``screen_entries == 0``. Omitted-at-default from the contract
        canonical form, exactly like ``screen_entries``.
    process_exemplars:
        Opt-in drift-anchored process-exemplar channel
        (``docs/design/PROCESS-EXEMPLARS.md``). When ``> 0``, each round
        the orchestrator extracts (best-effort) up to this many
        mechanically-REDACTED event windows from the champion's
        TRAIN-slice ``events.jsonl`` files — one per detected pattern,
        ±3 events around an anchor drift — and splices them into the
        proposer prompt after the failure-mode profile, so the proposer
        can see HOW a detected failure unfolds (the wandering plan step,
        the looping tool call) without ever learning WHICH board entry
        it unfolded on (no entry ids, no task text, no model outputs —
        the doc's §3 redaction rules are enforced in code, never by an
        LLM). ``0`` (default) is OFF — no extraction runs and the
        proposer prompt is byte-identical. Unlike ``screen_entries``
        this knob is NOT set by the scaffold: the screen is
        evaluation-side, while exemplars widen the proposer-visibility
        channel (OVERFITTING.md §11), so the operator opts in
        deliberately under the doc's §5 harm-detection runbook. Omitted
        from the contract canonical form at its 0 default so existing
        epochs never roll retroactively; a non-zero cap rolls the epoch,
        which is correct — a proposer shown process windows proposes
        under a different rule. Must be ``>= 0``.
    recombine:
        Opt-in mechanical recombination slot (WS-REC). When ``True`` AND
        ``best_of_n > 1``, the orchestrator builds one recombination pair
        per round (from the current reign's REJECTED, complementary,
        disjoint-patch challengers) and, when a pair is found, the last
        best-of-N slot MINTS the union of the two patch sets instead of
        sampling the LLM — a non-vetoed mint is CHOSEN with
        ``selection_mode="recombined"`` (a single winner can capture two
        complementary fixes a parsimony-biased selector would each
        discount). INERT unless ``best_of_n > 1`` (a single-sample proposer
        has no slate slot to mint into) — and cost-neutral: the mint
        REPLACES the slot's auxiliary propose call, never adds one.
        ``False`` (default) is OFF — no pair is ever built and the propose
        path is byte-identical. Omitted from the contract canonical form at
        its ``False`` default so existing epochs never roll retroactively;
        ``True`` rolls the epoch, which is correct — a proposer whose slate
        can recombine rejected fixes proposes under a different rule. See
        :mod:`zicato.epoch.recombine` / :mod:`zicato.proposer.recombine`.
    genealogy:
        Opt-in genealogy channel (WS-GENE; ``docs/design/PROPOSER.md`` §2.7).
        When ``> 0``, each round the orchestrator samples up to this many
        candidate-LINEAGE items from the current reign's durable records —
        PARENTS (the champion's own promoted patch history) + INSPIRATIONS
        (diverse rejected reign candidates chosen by mutation-id-set
        dissimilarity) — each carrying the proposer-authored core idea + a
        capped diff excerpt + a BANDED whole-candidate outcome, and splices
        them into the proposer prompt so the LLM can evolve IN CONTEXT
        (extend a winning line or re-frame a rejected one — the in-context
        analogue of the mechanical recombination slot, reaching even the
        pure-drift-side pairs that slot cannot see). Envelope-safe by
        construction: it carries candidate genealogy, never board data — no
        entry ids, no per-entry results, no exact deltas (banded through the
        same ``_bucket_scalar_delta`` vocabulary as the experiment memory),
        nothing holdout-derived. The sampler is a pure, DETERMINISTIC
        function (no RNG). ``0`` (default) is OFF — no sampling runs and the
        proposer prompt is byte-identical. Like ``process_exemplars`` this
        widens the proposer-visibility channel, so it is NOT set by the
        scaffold; the operator opts in deliberately. Omitted from the
        contract canonical form at its 0 default so existing epochs never
        roll retroactively; a non-zero count rolls the epoch, which is
        correct — a proposer shown candidate genealogy proposes under a
        different rule. Read-side only (the cost meter is untouched). Must be
        ``>= 0``. See :mod:`zicato.proposer.genealogy`.
    recombine_merge:
        How the recombination slot COMPOSES the union once the selector has
        picked a pair (``docs/design/PROPOSER.md`` §2.6.1). ``"mechanical"``
        (default) MINTS the concatenation of the two disjoint patch sets with
        NO LLM call — REQUIRES a disjoint pair (the applier is last-wins on a
        duplicate target). ``"llm"`` instead issues ONE auxiliary merge call
        (the depth refinement role) whose response flows through the normal
        proposal parse/validate path, and RELAXES the disjointness predicate
        for pair SELECTION so two REJECTED fixes that OVERLAP on a mutation
        target can be merged (the model resolves the overlap — overlap then
        becomes a ranking penalty, never a filter). Meaningful only when
        ``recombine`` is ``True`` AND ``best_of_n > 1``; a ``"llm"`` value set
        with ``recombine`` off is accepted-and-inert (the dependent-knob house
        style, like ``screen_veto_only`` without ``screen_entries``). Cost:
        ``"mechanical"`` spends ``best_of_n − 1`` propose calls (the mint is
        free); ``"llm"`` spends ``best_of_n`` on the happy path (the merge
        call SUBSTITUTES the slot's own sample call) — a rare parse/validate
        degrade adds the one fallback sample on top (``best_of_n + 1`` for
        that round). Omitted from the contract canonical form at its
        ``"mechanical"`` default (no retroactive roll); ``"llm"`` rolls the
        epoch — a slate that can compose an LLM merge proposes under a
        different rule. Must be ``"mechanical"`` or ``"llm"``. See
        :mod:`zicato.epoch.recombine` / :mod:`zicato.proposer.recombine`.
    """

    best_of_n: int = 3
    critique_enabled: bool = True
    screen_entries: int = 0
    screen_veto_only: bool = False
    process_exemplars: int = 0
    recombine: bool = False
    genealogy: int = 0
    recombine_merge: str = "mechanical"

    def __post_init__(self) -> None:
        if self.best_of_n < 1:
            raise ValueError(f"best_of_n must be >= 1, got {self.best_of_n!r}")
        if self.screen_entries < 0:
            raise ValueError(f"screen_entries must be >= 0, got {self.screen_entries!r}")
        if self.process_exemplars < 0:
            raise ValueError(f"process_exemplars must be >= 0, got {self.process_exemplars!r}")
        if self.genealogy < 0:
            raise ValueError(f"genealogy must be >= 0, got {self.genealogy!r}")
        if self.recombine_merge not in ("mechanical", "llm"):
            raise ValueError(
                f"recombine_merge must be 'mechanical' or 'llm', got {self.recombine_merge!r}"
            )

    @classmethod
    def defaults(cls) -> ProposerQualityConfig:
        """The fully-defaulted (best-of-3 + self-critique) config."""
        return cls()


def _default_proposer_quality_config() -> ProposerQualityConfig:
    """Default-factory for :attr:`ScoringWeights.proposer_quality`."""
    return ProposerQualityConfig.defaults()


@dataclass(frozen=True, slots=True)
class ExperimentMemoryConfig:
    """Experiment-memory scoping: what settled history the proposer sees.

    Part of the frozen evaluation contract — a field of
    :class:`ScoringWeights`, like :class:`OverfittingConfig` — because
    changing what history the proposer reads selects champions under a
    different rule (EXPERIMENT-MEMORY.md §3.4). The field is
    omitted-at-default from the contract canonicalizer
    (``epoch/contract.py::_SCORING_OMIT_AT_DEFAULT_FIELDS``), so a
    contract that never sets it hashes byte-identically to one that
    predates it; a non-default value rolls the epoch normally.

    Fields
    ------
    cross_epoch:
        Opt-in cross-epoch transfer (EXPERIMENT-MEMORY.md §3.4 / §5.2).
        ``False`` (default): the experiment-memory digest is same-epoch
        only — byte-identical to the shipped behaviour. ``True``: settled
        experiments from PRIOR epochs of the same workspace that share
        the current epoch's ``contract_hash`` are appended to the digest,
        marked ``same_contract=False``, with their ``scalar_score_delta``
        omitted (the number does not transfer), rendered in a clearly
        separated block, and admitted only into the budget left after
        every same-epoch entry — same-epoch history always keeps priority
        in the cap. Experiments under a DIFFERENT contract hash are never
        surfaced regardless of this knob.
    """

    cross_epoch: bool = False

    @classmethod
    def defaults(cls) -> ExperimentMemoryConfig:
        """The fully-defaulted (same-epoch-only) config."""
        return cls()


def _default_experiment_memory_config() -> ExperimentMemoryConfig:
    """Default-factory for :attr:`ScoringWeights.experiment_memory`."""
    return ExperimentMemoryConfig.defaults()


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
    """
    return {
        "drift:": 1.0,
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
    of an epoch. Changing weights starts a new epoch — generations across
    different epochs are not directly comparable.

    Fields
    ------
    drift_weight:
        Coefficient on the aggregated drift-loss term.
    pass_weight:
        Coefficient on the ``(1 - pass_rate)`` term.
    severity_weights:
        Per-severity multipliers applied inside the drift-loss
        aggregation. Keys are lowercase severity strings; missing keys
        default to ``0.0`` (the aggregator treats unknown severities as
        non-scoring rather than panicking).
    per_kind_weights:
        Optional per-drift-kind multipliers. Stacks multiplicatively
        with :attr:`severity_weights`. Empty mapping = uniform weighting
        across kinds.
    per_judge_weights:
        Optional per-custom-judge multipliers, keyed on the stable
        ``judge_name`` (the value a judge implementation sets on its
        ``name`` attribute). A custom judge emits drift under the
        single ``"custom"`` drift kind, so :attr:`per_kind_weights`
        cannot tell two custom judges apart — ``per_judge_weights``
        is the per-judge analogue. It stacks multiplicatively with
        :attr:`severity_weights` exactly the way :attr:`per_kind_weights`
        does for first-class kinds. A custom judge with no entry here
        scores at :attr:`default_judge_weight` rather than crashing —
        mirroring how an unknown kind falls back to ``1.0`` under
        :attr:`per_kind_weights`. Empty mapping = every custom judge
        scores at the default.
    default_judge_weight:
        Fallback multiplier for a custom judge whose ``judge_name`` is
        absent from :attr:`per_judge_weights`. Defaults to ``1.0`` so an
        unconfigured custom judge contributes on the same footing as a
        first-class drift kind with no ``per_kind_weights`` entry.
    plan_revision_weight:
        Coefficient on :attr:`LossProfile.plan_revisions`. Defaults to
        ``0.5`` — plan revisions are signal but less so than drift.
    runtime_weight:
        Coefficient on per-second runtime. Defaults to ``0.0`` — operators
        usually rely on the wall-clock budget as a hard ceiling rather
        than scoring runtime continuously, but the knob is here for
        cases where runtime matters intrinsically.
    diff_complexity_weight:
        Coefficient on the challenger's diff complexity — an opt-in
        parsimony / MDL term (OVERFITTING.md §5 / §12 #4). When ``> 0`` the
        scalar gains a ``diff_complexity`` component equal to
        ``diff_complexity_weight * (added + removed + patches)``, where the
        diff size comes from the challenger experiment's patch records (see
        :func:`zicato.scoring.diff_complexity.diff_size`). A shorter-
        description edit provably overfits the board less, so penalising diff
        size biases the optimiser toward the smaller, more general edit.
        Defaults to ``0.0`` — when ``0.0`` (or when no diff size is available,
        e.g. the champion side has no challenger experiment) the term is
        EXACTLY absent: it is not added to the scalar, not surfaced as a
        component, and the scalar / contract hash / every golden are
        byte-identical to a contract without this field. The contract
        canonicalizer omits the field from the scoring hash at the default so
        an unset contract does not roll its epoch; setting it ``> 0`` adds the
        key and rolls the epoch like any other weight change.
    promote_margin:
        Minimum scalar-score improvement the child generation must show
        over the parent to be promoted. Acts as a regression-noise
        threshold.
    pass_rate_monotonicity:
        When ``True`` (default), a pass-rate regression rejects the child
        regardless of drift-side improvement. The stricter half of the
        tournament gate; operators can flip to ``False`` for experimental
        epochs where they expect non-monotone exploration. The on/off
        switch only — :attr:`pass_rate_monotonicity_scope` selects WHICH
        movement counts as a regression.
    pass_rate_monotonicity_scope:
        Granularity of the pass-rate monotonicity check when
        :attr:`pass_rate_monotonicity` is on (see
        :data:`PassRateMonotonicityScope`). ``"per_entry"`` (default,
        back-compatible) rejects when ANY champion-passed entry flips to
        fail — the right policy for invariant / regression-suite boards.
        ``"aggregate"`` rejects only when the OVERALL pass-rate drops below
        the champion's (modulo a small float-noise tolerance) — the right
        policy for sampled evaluation boards where individual pass/fail is
        noisy and a strictly-better challenger should not be vetoed by a
        single entry flip. There is no ``"off"`` value: disable the check
        with ``pass_rate_monotonicity=False``.
    regression_gate_enabled:
        When ``True``, the tournament runner shells out to the
        snapshot's own test suite BEFORE evaluating the scoring gate.
        A non-passing suite hard-rejects the candidate regardless of
        drift_loss / pass_rate movement. Defaults to ``False`` for
        backwards compatibility with epochs whose snapshots do not
        ship a regression suite.
    regression_test_command:
        The argv used to invoke the regression suite. Defaults to a
        plain pytest invocation; operators with non-pytest suites can
        override (e.g. ``("python", "-m", "unittest", "discover")``).
    regression_timeout_s:
        Wall-clock seconds the regression subprocess is allowed before
        the runner kills it. A timeout counts as a regression failure.
    namespace_weights:
        Per-namespace coefficients used by the multi-objective scalar.
        Keys are namespace prefixes (with the trailing colon, e.g.
        ``"drift:"``). The sign of each coefficient codifies the
        namespace's "worse" direction:

        * Positive → higher value is worse (drift, cost, latency,
          schema). Added to the scalar as ``weight * mean``.
        * Negative → higher value is better (rubric). The negation
          flips the metric into a loss so the scalar stays
          lower-is-better.
        * Zero → namespace excluded from the scalar; tracked but not
          optimised (default for ``"output:"``).

        See :func:`_default_namespace_weights` for the shipped values.
    namespace_monotonicity:
        Per-namespace strict-monotonicity flags. When a namespace's
        flag is ``True``, the promote gate rejects any child whose
        per-namespace aggregate has moved in the namespace's "worse"
        direction (as defined by the sign in
        :attr:`namespace_weights`) by more than the namespace's
        tolerance — even when the combined scalar improves. Namespaces
        whose flag is missing or ``False`` are not gated this way.
    pass_transform:
        Optional declarative transform (a single
        :data:`zicato.scoring.transforms.TransformSpec`,
        ``{"op": ..., ...params}``) reshaping the scalar's pass/miss
        term ``(1 - mean_score)`` at Seam 2 — the declarative replacement
        for the retired ``pass_exponent`` field (a stray ``pass_exponent``
        config key is now rejected at load, not silently dropped). ``None``
        (default) is NEUTRAL = ``linear`` = today's plain linear miss term.
        Validated fail-fast in :meth:`__post_init__`.
    drift_kind_aggregation:
        Optional per-drift-kind declarative transforms
        (``{kind: TransformSpec}``) reshaping how each kind's count
        aggregates into the per-run drift loss at Seam 1 — the opt-in
        replacement for the old unconditional harmonic
        ``looping_reasoning`` special-case. An absent kind entry is
        NEUTRAL = ``linear`` = ``severity × kind_weight × count``.
        Validated fail-fast in :meth:`__post_init__`.
    """

    drift_weight: float = 1.0
    pass_weight: float = 1.0
    severity_weights: Mapping[str, float] = field(default_factory=_default_severity_weights)
    per_kind_weights: Mapping[str, float] = field(default_factory=dict)
    per_judge_weights: Mapping[str, float] = field(default_factory=dict)
    default_judge_weight: float = 1.0
    plan_revision_weight: float = 0.5
    runtime_weight: float = 0.0
    # Opt-in parsimony / MDL term (OVERFITTING.md §5 / §12 #4). DEFAULT 0.0 ⇒
    # the diff-complexity term is exactly absent and the scalar / contract hash
    # / every golden are byte-identical to a contract without this field (the
    # contract canonicalizer omits it at the default — see
    # ``epoch/contract.py::scoring_to_canon``). Set ``> 0`` to fold a
    # ``diff_complexity_weight * (added + removed + patches)`` component into the
    # challenger's scalar so a shorter-description edit is preferred.
    diff_complexity_weight: float = 0.0
    promote_margin: float = 0.01
    pass_rate_monotonicity: bool = True
    pass_rate_monotonicity_scope: PassRateMonotonicityScope = "per_entry"
    regression_gate_enabled: bool = False
    regression_test_command: tuple[str, ...] = ("pytest", "tests/", "-q")
    regression_timeout_s: int = 600
    # Multi-objective surface — see the helpers above for the rationale
    # behind the default coefficient choices.
    namespace_weights: Mapping[str, float] = field(default_factory=_default_namespace_weights)
    namespace_monotonicity: Mapping[str, bool] = field(
        default_factory=_default_namespace_monotonicity
    )
    # Per-epoch tournament structure (gauntlet by default). Modelled here
    # so it factors into the contract hash through the existing scoring
    # canonicalizer with zero new plumbing: changing the structure or any
    # param rolls the epoch. See :class:`TournamentStructure`.
    tournament_structure: TournamentStructure = field(default_factory=_default_tournament_structure)
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
        default_factory=_default_experiment_memory_config
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
    # prompt is byte-identical to today. Because it is a plain
    # ``ScoringWeights`` field, it folds into the field-enumerating contract
    # serde + canonicalizer automatically: configuring (or changing) the spec
    # rolls the epoch, exactly like every other contract field.
    outcome_summarizer_spec: str = ""
    # Declarative scoring transforms (issue #19 phase 2). Each is a single
    # ``{"op": "<name>", ...params}`` spec from the
    # :mod:`zicato.scoring.transforms` registry (``linear`` / ``pow`` /
    # ``harmonic`` / ``cap`` / ``clip`` / ``log1p``). Single op per slot — NO
    # pipelines (arbitrary multi-step logic is Phase 3's ``scalar_fn`` /
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
    # rejected at load, not lowered). ``None`` (the default) is NEUTRAL =
    # ``linear`` = today's plain linear miss term.
    pass_transform: Mapping[str, Any] | None = None
    # ``drift_kind_aggregation`` reshapes, per drift KIND, how that kind's
    # count aggregates into the drift loss at Seam 1 — the opt-in replacement
    # for the old unconditional harmonic ``looping_reasoning`` special-case
    # (``{"looping_reasoning": {"op": "harmonic"}}`` reproduces it for THIS
    # contract only). An absent kind entry is NEUTRAL = ``linear`` =
    # ``severity × kind_weight × count`` (today's built-in).
    drift_kind_aggregation: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    # Dotted-spec scoring PLUGINS (issue #19 phase 3) — the escape hatch for
    # arbitrary operator scoring logic the declarative registry cannot express
    # (F-beta, cost-aware penalties, the retired harmonic-looping curve as a
    # ~10-line operator plugin). Each is a dotted spec (``pkg.mod:fn`` /
    # ``pkg.mod.fn``) resolved by the SAME importer predicates / judges use, and
    # invoked as a PURE, deterministic, NO-LLM function over the matching frozen
    # context (which carries the post-transform value as ``builtin_*`` so the
    # plugin WRAPS the declarative shape rather than reimplementing it). The
    # empty string (the default) configures NO plugin = the Phase-2/builtin path
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
    # Opt-in INTEGRITY BLOCKING modes (both default OFF — the alarm-only
    # posture of the supervisor's integrity notary is the shipped baseline).
    # Both are omitted from the contract canonical form at their default
    # (``epoch/contract.py::_SCORING_OMIT_AT_DEFAULT_FIELDS``) so existing
    # epochs never roll retroactively; opting in rolls the epoch normally.
    #
    # ``block_on_containment_violation``: before finalizing a GATE-DECIDED
    # promotion, the orchestrator re-checks diff containment on the same
    # rule surface the Rust supervisor attests out-of-band
    # (``crates/supervisor/src/diff_containment.rs``): every file OUTSIDE
    # the registered mutable trees must be byte-identical parent↔child.
    # When ON, a violating child is REJECTED with a clear
    # ``containment_violation`` reason instead of promoted-with-alarm.
    # Fail-open like the supervisor: an unreadable snapshot skips the
    # check (never a false quarantine). An explicit operator
    # force-promote is NOT blocked — the override is recorded provenance,
    # never silent.
    block_on_containment_violation: bool = False
    # ``block_on_gate_contradiction``: immediately before finalizing a
    # GATE-DECIDED promotion, re-derive the gate's scalar rule
    # (``delta_scalar <= -promote_margin`` — the supervisor's
    # ``promotion_gate.rs check_row`` semantics, applied pre-persist) and
    # REFUSE the promotion on contradiction. When OFF (default) a
    # contradiction persists exactly as today and the supervisor's
    # out-of-band scan raises the alarm. An explicit operator
    # force-promote is NOT re-checked (same rationale as above); a
    # promotion with no usable scalar evidence is skipped (fail-open,
    # mirroring ``check_row``'s SkippedNoEvidence).
    block_on_gate_contradiction: bool = False

    def __post_init__(self) -> None:
        """Validate the declarative transform specs fail-fast at construction.

        Runs at contract load (the loader builds a :class:`ScoringWeights`
        from ``scoring.json``), so a malformed transform — unknown op,
        non-finite / missing / typo'd param — is rejected here with a clear
        error rather than silently defaulting or surfacing as a ``NaN`` scalar
        partway through a run. By the time the scoring dispatchers call
        :func:`zicato.scoring.transforms.apply_transform`, every spec on this
        instance is already known-good.

        The dotted-spec scoring PLUGINS (``drift_reducer`` / ``scalar_fn``,
        issue #19 phase 3) are validated HERE only as strings — resolution +
        invocation happen at scoring time (the worker resolves ``drift_reducer``
        itself), and a not-yet-written plugin must still construct so the
        contract can be hashed with the spec string + a degraded source hash.
        """
        from zicato.scoring.transforms import validate_transform_spec  # noqa: PLC0415

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

    def to_json(self) -> dict[str, Any]:
        """Serialise to a JSON-shaped dict via the field-enumerating serde.

        The single source of truth for putting a :class:`ScoringWeights` on
        the wire — used by BOTH the tournament runner (to hand weights to the
        subprocess worker) and the frozen-contract snapshot. Because it walks
        ``dataclasses.fields()`` (see
        :mod:`zicato.epoch.contract_serde`) it covers EVERY field
        automatically, recursing into nested config dataclasses. Adding a
        field can therefore never silently desync the worker into scoring
        under defaults — the historical ``per_judge_weights`` /
        ``pass_rate_monotonicity_scope`` / ``drift_kind_aggregation`` desync
        class that two hand-aligned field lists kept re-introducing.

        :meth:`from_json` is the exact inverse:
        ``ScoringWeights.from_json(w.to_json()) == w`` for every field.
        """
        from zicato.epoch.contract_serde import dataclass_to_jsonable  # noqa: PLC0415

        return dataclass_to_jsonable(self)

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


def recommended_scaffold_weights() -> ScoringWeights:
    """The FULL effective contract new-workspace scaffolds write out.

    Shared by ``zicato init`` (which writes it to the operator's live
    ``scoring.json``) and the tournament builder's blank draft, so both
    scaffolds spell the SAME recommended contract explicitly instead of
    leaning on invisible defaults: the racing structure (field 4, eta 2,
    board_fraction 0.4), two averaged replicates per duel, and the
    Bradley--Terry evidence gate ENABLED EXPLICITLY (threshold 0.8 with a
    32-replicate budget). The gate is deliberately NOT a silent in-code
    default — its CIs separate only after a long unbroken win streak (~37
    duels on a two-contestant pair), so it needs an honest budget the
    operator can see and price: under racing the crowning-pair replicates
    amortize through the per-unit cache, and the builder's cost meter
    reflects the ``replicates`` knob. Everything else is the dataclass
    default; the field-enumerating serializer then writes every field, so
    the generated ``scoring.json`` IS the effective contract.

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
