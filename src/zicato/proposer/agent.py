"""The :class:`ProposerAgent` abstraction over the structured proposer.

Phase 1 turned a proposer dir (or ``None``) into a hash-ready
:class:`~zicato.core.types.ProposerSpec` (agent identity + skills). This
module is the Phase 2a core: it wraps the single-shot
:func:`zicato.proposer.proposer.propose_experiment` engine behind a
uniform :class:`ProposerAgent` protocol, threads a proposer's *skills*
into the prompt the engine sends, and exposes a builder that the
orchestrator drives at each propose site.

Two halves:

* :class:`ProposerContext` — a frozen bundle of everything
  :func:`propose_experiment` needs as call-time inputs. The orchestrator
  assembles one per challenger and hands it to the agent; the agent
  decides how to turn it into an :class:`~zicato.core.types.Experiment`.
* :class:`ProposerAgent` / :class:`DefaultProposerAgent` /
  :func:`build_proposer_agent` — the protocol, the skills-aware built-in
  implementation, and the spec→agent builder.

Two agent implementations ship here. The
:class:`~zicato.proposer.adk_agent.ADKProposerAgent` is a native ADK agent
that declares its own ``model=`` and runs on ADK's own ``Runner`` (NOT the
auxiliary text shim, which cannot express the function calls a tool-using
agent needs); :class:`DefaultProposerAgent` is the single-shot text-shim
path that drives :func:`propose_experiment` over the auxiliary callable.

:func:`build_proposer_agent` selects between them:

* **no proposer dir configured (the DEFAULT)** ⇒ the tool-using
  ``ADKProposerAgent`` in ``builtin_default`` mode, bound to the auxiliary
  model and the full read-only proposer tool registry. The default proposer
  reads the world while it reasons;
* **a proposer dir with a custom ``agent.py``**
  (``spec.agent_source_sha256`` is set) ⇒ ``ADKProposerAgent`` loading that
  author-owned agent from disk;
* **a proposer dir with skills but no ``agent.py``** ⇒ the skill-composed
  ``DefaultProposerAgent`` — the EXPLICIT opt-in into the single-shot
  text-shim engine.

The ADK module is imported lazily so importing this module never forces the
optional ``google-adk`` extra.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from zicato.core.types import (
    Experiment,
    MutationPoint,
    Pattern,
    PriorExperiment,
    ProposerQualityConfig,
    ProposerSpec,
)
from zicato.proposer.proposer import ExperimentValidator, propose_experiment

if TYPE_CHECKING:  # pragma: no cover - typing-only import
    from zicato.index.query import MutationTrackRecord
    from zicato.proposer.best_of_n import ScreenRunner
    from zicato.proposer.calibration import CalibrationSummary
    from zicato.proposer.external import ExternalProposerConfig
    from zicato.proposer.genealogy import GenealogyItem
    from zicato.proposer.recombine import RecombinationPair
    from zicato.telemetry.meta_loop import MetaLoopEmitter


@dataclass(frozen=True)
class ProposerContext:
    """Call-time inputs for one proposer invocation.

    Bundles exactly what :func:`zicato.proposer.proposer.propose_experiment`
    needs as keyword arguments — the lineage coordinates, the advisory
    inputs (patterns, mutation manifest, loss summary, prior experiments),
    the auxiliary-LLM seam, and the bounded-retry / validation knobs. The
    orchestrator builds one per challenger and hands it to a
    :class:`ProposerAgent`; the agent (not the orchestrator) owns the
    decision of how to turn the context into an
    :class:`~zicato.core.types.Experiment`.

    The iterable inputs are stored as tuples so a context is a stable,
    re-readable value — an agent may consult them across retries without
    exhausting a generator. The field set and types mirror the
    :func:`propose_experiment` signature one-for-one; see that function's
    docstring for the per-field semantics.
    """

    epoch_id: str
    parent_generation_id: str
    new_generation_id: str
    patterns: tuple[Pattern, ...]
    mutations: tuple[MutationPoint, ...]
    brief_text: str
    current_loss_summary: str
    aux_call_llm: Callable[[str, str, str], Awaitable[str]]
    model: str = ""
    max_retries: int = 2
    forbidden_ids: tuple[str, ...] = ()
    workspace_root: Path | None = None
    #: The PARENT generation's materialised snapshot — the tree this round is
    #: about to patch, resolved by the orchestrator through the generation
    #: store's path convention and threaded here so a tool-using proposer
    #: does not re-derive it. :class:`~zicato.proposer.adk_agent.ADKProposerAgent`
    #: binds it onto the tool context, and the external-proposer launch
    #: forwards it into the MCP server's per-round context file.
    #:
    #: ``None`` (the default) means "no orchestrator populated this" — the
    #: ADK path then falls back to deriving it, which is exactly the
    #: duplication this field removes, so the fallback is a compatibility
    #: shim for contexts built by hand (tests, a standalone propose) and NOT
    #: a supported production shape. ``_propose_child`` takes it as a
    #: REQUIRED argument so the real path cannot reach the fallback.
    generation_root: Path | None = None
    validate_experiment: ExperimentValidator | None = None
    meta_loop_emitter: MetaLoopEmitter | None = None
    custom_judge_names: frozenset[str] | None = None
    prior_experiments: tuple[PriorExperiment, ...] = ()
    #: When ``True`` (the default-on
    #: :attr:`~zicato.core.types.OverfittingConfig.restrict_proposer_visibility`
    #: posture, set by the orchestrator from the epoch's scoring config),
    #: the assembled prompt aggregates per-entry pattern identities to
    #: counts/rates and coarsens experiment-memory Δscalar to buckets
    #: (OVERFITTING.md §11). ``False`` renders the verbatim prompt.
    restrict_visibility: bool = False
    #: Pre-rendered, train-slice-only, BUCKETED outcome-marginal block
    #: (Capability 2 of issue #18 — built by
    #: :func:`~zicato.proposer.prompts.render_failure_mode_profile` from an
    #: :class:`~zicato.analyzer.outcome_marginals.OutcomeMarginalSummary` the
    #: orchestrator aggregates over the SAME train slice it passes to the
    #: patterns + loss summary). When non-empty, a ``## Failure-mode profile``
    #: section is spliced into the user prompt so the proposer can target
    #: *why* answers are wrong. The string is already board-anonymized +
    #: banded by its renderer; the agents only forward it. Empty (the
    #: default) omits the section, byte-identical to before this surface.
    failure_profile: str = ""
    #: Pre-rendered, BANDED statement of what the frozen contract scores —
    #: built by the orchestrator from the epoch's
    #: :class:`~zicato.core.scoring_config.ScoringWeights` via
    #: :func:`~zicato.evolve.decision_support.build_metric_priorities` and
    #: :func:`~zicato.proposer.prompts.render_metric_priorities_block`. When
    #: non-empty it REPLACES the flat membership list in
    #: ``## Valid expectation targets``: targets are ordered by weight within
    #: each channel and anything the contract weights at zero is absent, so a
    #: round is not spent improving a metric that cannot move the score.
    #: BANDED, never the raw coefficients — the weights stay orchestrator-side,
    #: because handing an agent the objective function invites optimising the
    #: shape of the score instead of the behaviour the board measures. The
    #: validator's accept-list is untouched, so a target dropped from the
    #: prompt is still parsed without a burned retry. Empty (the default —
    #: every caller that holds no weights) renders the membership form
    #: byte-identically.
    metric_priorities: str = ""
    #: Pre-rendered, train-slice-only, REDACTED process-exemplar block —
    #: the opt-in ``proposer_quality.process_exemplars`` channel
    #: (``docs/design/PROCESS-EXEMPLARS.md``). Built by the orchestrator,
    #: best-effort, from
    #: :func:`~zicato.analyzer.process_exemplars.extract_process_exemplars`
    #: (mechanical redaction: no entry ids, no task text, no model outputs)
    #: rendered through
    #: :func:`~zicato.proposer.prompts.render_process_exemplars`. When
    #: non-empty, a ``## Process exemplars`` section is spliced into the
    #: user prompt directly after the failure-mode profile so the proposer
    #: sees HOW a detected failure unfolds — never WHICH entry it unfolded
    #: on. Empty (the default — every knob-off round) omits the section,
    #: byte-identical to before this surface.
    process_exemplars: str = ""
    #: Sampled genealogy items — the opt-in ``proposer_quality.genealogy``
    #: channel (``docs/design/PROPOSER.md`` §2.7). Built by the orchestrator
    #: from :func:`~zicato.proposer.genealogy.sample_genealogy` (parents = the
    #: champion's promoted spine; inspirations = diverse rejected reign
    #: candidates by mutation-id-set dissimilarity) and rendered by
    #: :func:`~zicato.proposer.prompts.render_genealogy_block` inside
    #: ``render_user_prompt``; when the render is non-empty a
    #: ``## Candidate genealogy`` section is spliced directly above the
    #: experiment-memory block so the proposer can evolve in context — extend a
    #: promoted line or re-frame a rejected one. Each item is already BANDED
    #: (whole-candidate outcomes through the ``improved``/``flat``/``regressed``
    #: vocabulary) + CAPPED (proposer's own diff excerpts) by the sampler;
    #: NEVER an entry id, a per-entry result, an exact delta, or anything
    #: holdout-derived. Empty ``()`` (the default — every knob-off round) omits
    #: the section, byte-identical to before this surface.
    genealogy: tuple[GenealogyItem, ...] = ()
    #: Optional per-reign prediction-calibration summary — the opt-in
    #: ``proposer_quality.calibration_feedback`` channel
    #: (``docs/design/PROPOSER.md`` §2.8). Built by the orchestrator from
    #: :func:`~zicato.proposer.calibration.sample_calibration` (the reign's
    #: settled hypotheses graded by the prediction-accuracy grader —
    #: :func:`~zicato.tournament.detail.hypothesis_ledger`) and rendered by
    #: :func:`~zicato.proposer.prompts.render_calibration_block` inside
    #: ``render_user_prompt``; when non-empty a ``## Prediction calibration``
    #: section is spliced above the experiment-memory block so the proposer
    #: sees its OWN miss pattern and hypothesizes more honestly. Already banded
    #: (whole-candidate outcomes through ``improved``/``flat``/``regressed``) +
    #: reduced to hit/miss verdicts + aggregate counts by the sampler; NEVER an
    #: entry id, a per-entry result, an exact delta, or anything holdout-derived
    #: (the grader scores whole-candidate movement aggregates). ``None`` (the
    #: default — every knob-off round, and any round with no graded history)
    #: omits the section, byte-identical to before this surface.
    calibration: CalibrationSummary | None = None
    #: Optional per-sample edit-class steering line — the best-of-N slate
    #: diversifier (:data:`zicato.proposer.best_of_n.EDIT_CLASS_HINTS`). The
    #: wrapper stamps a DISTINCT hint on each slate slot's context via
    #: ``dataclasses.replace`` so the N samples explore different edit
    #: strategies. A static instruction string carrying no board identity —
    #: it composes with the restricted-visibility envelope untouched. Empty
    #: (the default — every single-sample propose) renders no section.
    sample_hint: str = ""
    #: Optional best-of-N slate coordinate, stamped by the wrapper on each
    #: slot's context alongside ``sample_hint``. It reaches no renderer: its
    #: only consumer is the durable input capture
    #: (:mod:`zicato.proposer.input_capture`), which needs it to tell one
    #: slot's records from a sibling's in the epoch's shared capture file.
    #: ``None`` (the default — every single-sample propose) records no slot.
    slot_index: int | None = None
    #: Optional seed for the repair-feedback loop's FIRST attempt — the
    #: screen-informed revise channel (WS-R). The best-of-N wrapper stamps
    #: the all-vetoed slate's COUNTS-ONLY veto summary here (never an entry
    #: id — the restricted-visibility envelope) so the ONE bounded revise
    #: re-sample starts as a genuine repair turn: both engines thread it
    #: into the same ``feedback`` slot a validation failure would populate
    #: on retry. Empty (the default — every non-revise propose) seeds
    #: nothing and every prompt renders byte-identically.
    revise_feedback: str = ""
    #: Optional per-mutation-point track records (the fertility map —
    #: :func:`zicato.index.query.mutation_point_track_record`), read
    #: best-effort from the analytical index by the orchestrator, exactly
    #: like ``prior_experiments``. The prompt renderer annotates each
    #: manifest entry that has a record with one compact, BANDED advisory
    #: line — aggregate counts + bucketed Δscalar only, labelled
    #: "experiments touching this point" (multi-patch experiments confound
    #: credit; never causal) — inside the restricted-visibility envelope.
    #: ``None`` (the default) renders a byte-identical manifest.
    mutation_track_records: Mapping[str, MutationTrackRecord] | None = None
    #: Optional best-effort ROUND-LOG event emitter (WS8), threaded by the
    #: orchestrator so the proposer stack can trace its sampling decisions
    #: into the round's durable event log WITHOUT importing the log module
    #: (the proposer stays decoupled from :mod:`zicato.epoch.round_log`).
    #: Called as ``emitter(type_token, fields, scope)`` — e.g.
    #: ``("candidate_sampled", {"i": 0, "n": 3}, {"generation_id": "v1"})``
    #: from the best-of-N wrapper. ``scope`` carries the event's PLAN
    #: coordinates and is a SEPARATE argument, never a payload key, so no
    #: emitter can forward it into the typed event's constructor. Emission is
    #: best-effort by contract: callers guard every invocation so a raising
    #: emitter can never fail a propose step.
    #: ``None`` (the default) emits nothing.
    round_event_emitter: Callable[[str, dict[str, Any], Mapping[str, Any] | None], None] | None = (
        None
    )
    #: Optional pre-tournament candidate-screen runner (tryouts; WS-S).
    #: The orchestrator builds ONE closure per round — via
    #: ``_build_candidate_screen_runner``, only when the contract opts in
    #: (``proposer_quality.screen_entries > 0`` AND ``best_of_n > 1``) —
    #: binding the rotating train panel, the parent baseline and the
    #: frozen weights. The best-of-N wrapper calls it GUARDED once the
    #: slate settles: veto-first (a catastrophic regression is
    #: disqualified before the critic chooses), and any screen failure
    #: degrades to an unscreened selection — screening can never fail a
    #: propose. ``None`` (the default) screens nothing and the propose
    #: path is byte-identical.
    screen_candidates: ScreenRunner | None = None
    #: Optional recombination pair (WS-REC) — plain DATA, not a callable:
    #: the orchestrator's ``_build_recombination_pair`` selects it once per
    #: round from round-start state (rejected complementary challengers of
    #: the current reign) and threads the envelope-clean value here (counts
    #: + patches + hypothesis text ONLY — never a board-entry id). When
    #: set, the best-of-N wrapper's LAST slot MINTS the union experiment
    #: (:func:`zicato.proposer.recombine.mint_recombined_experiment`)
    #: instead of sampling the LLM. On the multi-challenger field path the
    #: orchestrator threads the pair to SLOT 0 ONLY (identical mints across
    #: the field would collapse into diversity soft-rejects). ``None`` (the
    #: default — every knob-off round, and every round with no eligible
    #: pair) mints nothing and the propose path is byte-identical.
    recombine_pair: RecombinationPair | None = None
    #: Optional per-slot scratch-validator factory (WS-CONC) — the seam that
    #: lets the best-of-N slate GATHER. Built once per round by the
    #: orchestrator beside the shared post-apply validator
    #: (:func:`zicato.evolve.round.build_scratch_validator_factory`); each
    #: call mints a FRESH, disjoint scratch child tree + a ``(validate,
    #: cleanup)`` lease. The best-of-N wrapper calls it once per slate slot so
    #: N slots validate concurrently into disjoint trees instead of all
    #: deriving the shared ``next_id`` tree (the write that used to serialise
    #: the slate). ``None`` (the default — single-sample proposers, and every
    #: unit-test context that threads no genstore) ⇒ the wrapper falls back to
    #: the shared ``validate_experiment`` hook and runs the slate serially,
    #: byte-identically to the pre-concurrency behaviour. The chosen candidate
    #: is still mounted into the real ``next_id`` once, after selection, via
    #: ``validate_experiment`` — this factory never writes the canonical tree.
    scratch_validator_factory: (
        Callable[[], tuple[ExperimentValidator, Callable[[], None]]] | None
    ) = None


class ProposerAgent(Protocol):
    """A proposer that turns a :class:`ProposerContext` into an experiment.

    The protocol is the single seam the orchestrator drives, regardless of
    whether the proposer is the built-in single-shot agent or — in a later
    phase — a custom agent that calls tools. An implementation MUST raise
    :class:`zicato.proposer.proposer.ProposerError` when it cannot produce
    a schema-valid experiment within its budget, matching the contract the
    orchestrator already handles at each propose site.
    """

    async def propose(self, ctx: ProposerContext) -> Experiment: ...


class NativeSlateProposer(ProposerAgent, Protocol):
    """A proposer that can keep best-of-N work inside its own session."""

    async def propose_slate(
        self, ctx: ProposerContext, config: ProposerQualityConfig
    ) -> Experiment: ...


@dataclass(frozen=True)
class DefaultProposerAgent:
    """The built-in single-shot proposer, made skills-aware.

    Wraps :func:`zicato.proposer.proposer.propose_experiment` — the
    compose-prompts → call-aux-LLM → parse → bounded-retry engine — and
    threads the proposer spec's :attr:`~zicato.core.types.ProposerSpec.skills`
    into the system prompt. With no skills (the built-in default spec) the
    behaviour is byte-identical to a bare :func:`propose_experiment` call,
    so configuring no proposer changes nothing.
    """

    spec: ProposerSpec

    async def propose(self, ctx: ProposerContext) -> Experiment:
        return await propose_via_engine(spec=self.spec, ctx=ctx, aux_call_llm=ctx.aux_call_llm)


async def propose_via_engine(
    *,
    spec: ProposerSpec,
    ctx: ProposerContext,
    aux_call_llm: Callable[[str, str, str], Awaitable[str]],
) -> Experiment:
    """Run the single-shot engine over ``ctx``, against a chosen callable.

    The one place a :class:`ProposerContext` is unpacked into
    :func:`~zicato.proposer.proposer.propose_experiment`'s keyword
    arguments. ``aux_call_llm`` is a parameter rather than being read off
    the context because the transport is the only thing that varies:
    :class:`DefaultProposerAgent` passes ``ctx.aux_call_llm`` (the text
    shim), while :class:`~zicato.proposer.pi_agent.PiProposerAgent` passes
    a live RPC session's ``call``. Everything downstream of that choice —
    the bounded retry, the repair turns, the forbidden-id enforcement, the
    post-apply validation hook, the meta-loop bookends — is identical for
    both by construction rather than by two implementations agreeing.
    """
    return await propose_experiment(
        epoch_id=ctx.epoch_id,
        parent_generation_id=ctx.parent_generation_id,
        new_generation_id=ctx.new_generation_id,
        patterns=ctx.patterns,
        mutations=ctx.mutations,
        brief_text=ctx.brief_text,
        current_loss_summary=ctx.current_loss_summary,
        aux_call_llm=aux_call_llm,
        model=ctx.model,
        max_retries=ctx.max_retries,
        forbidden_ids=ctx.forbidden_ids,
        workspace_root=ctx.workspace_root,
        validate_experiment=ctx.validate_experiment,
        meta_loop_emitter=ctx.meta_loop_emitter,
        custom_judge_names=ctx.custom_judge_names,
        prior_experiments=ctx.prior_experiments,
        skills=spec.skills,
        restrict_visibility=ctx.restrict_visibility,
        failure_profile=ctx.failure_profile,
        metric_priorities=ctx.metric_priorities,
        process_exemplars=ctx.process_exemplars,
        genealogy=ctx.genealogy,
        calibration=ctx.calibration,
        sample_hint=ctx.sample_hint,
        mutation_track_records=ctx.mutation_track_records,
        revise_feedback=ctx.revise_feedback,
        slot_index=ctx.slot_index,
    )


def build_proposer_agent(
    spec: ProposerSpec,
    proposer_path: Path | None = None,
    external_config: ExternalProposerConfig | None = None,
) -> ProposerAgent:
    """Build the :class:`ProposerAgent` for a resolved proposer spec.

    Four outcomes, in resolution order:

    0. **External agent** — when ``runtime.proposer_agent`` named a class
       (``spec.external_path`` is set), this imports it and constructs it
       with ``(spec=..., config=...)``. It resolves FIRST because it is
       the one tier that is not an ADK agent at all: the class owns its
       own process, transport and tool surface, and zicato drives it
       through the same one-method :class:`ProposerAgent` protocol. See
       :mod:`zicato.proposer.external`.

    1. **Custom ADK agent** — when the proposer dir ships a
       ``proposers/<name>/agent.py`` (``spec.agent_source_sha256`` is set),
       this returns an :class:`~zicato.proposer.adk_agent.ADKProposerAgent`
       that loads that author-owned agent from disk. ``proposer_path`` is
       the dir the module is loaded from; the orchestrator threads the same
       frozen ``proposer_path`` it resolved the spec from.

    2. **Built-in default (the DEFAULT)** — when a contract configures NO
       proposer dir, ``spec`` is :meth:`ProposerSpec.default` (agent id
       ``"builtin:default"``). This returns an
       :class:`~zicato.proposer.adk_agent.ADKProposerAgent` in
       ``builtin_default`` mode: a native ADK tool-using agent
       (:func:`~zicato.proposer.adk_agent.build_default_adk_agent`) bound to
       the workspace's auxiliary model and the full read-only proposer tool
       registry. The DEFAULT proposer therefore reads the world (the parent
       snapshot, the journal, the analyzer insights) while it reasons, on
       ADK's own ``Runner`` — NOT the single-shot text shim.

    3. **Skill-composed default (EXPLICIT opt-in)** — a ``dir:*`` proposer
       that carries *no* custom agent module (``spec.agent_source_sha256``
       is ``None``) but DOES configure a proposer dir (e.g. to drop
       ``skills/*.md``). This returns a :class:`DefaultProposerAgent`, the
       single-shot text-shim engine, steered purely through its skills over
       the auxiliary callable. Configuring a proposer dir is the explicit
       opt-in into this path; the bare default (#2) is the tool-using agent.

    Every ``google.adk`` import is lazy, so importing this module never
    forces the optional ``google-adk`` extra — only constructing an
    ``ADKProposerAgent``'s agent (at first ``propose``) pulls it in.

    Raises
    ------
    ValueError
        When the spec carries a custom agent module but no
        ``proposer_path`` was supplied to load it from, or when it carries
        an ``external_path`` but no ``external_config`` to resolve it
        against — misconfigurations the caller must fix rather than
        silently fall back to the default.
    """
    if spec.external_path is not None:
        from zicato.proposer.external import load_external_proposer_class  # noqa: PLC0415

        if external_config is None:
            raise ValueError(
                "spec declares an external proposer agent (external_path is "
                f"{spec.external_path!r}) but no external_config was supplied "
                "to construct it with"
            )
        cls = load_external_proposer_class(spec.external_path)
        return cls(spec=spec, config=external_config)  # type: ignore[call-arg]

    # Lazy import: ADKProposerAgent pulls in the optional google-adk extra
    # only when its agent is actually built (at first propose), so importing
    # this module stays dependency-light.
    from zicato.proposer.adk_agent import ADKProposerAgent  # noqa: PLC0415

    if spec.agent_source_sha256 is not None:
        if proposer_path is None:
            raise ValueError(
                "spec declares a custom proposer agent (agent_source_sha256 is "
                "set) but no proposer_path was supplied to load "
                "proposers/<name>/agent.py from"
            )
        return ADKProposerAgent(spec=spec, proposer_path=proposer_path)

    if spec == ProposerSpec.default():
        # The DEFAULT proposer: a tool-using ADK agent bound to the
        # auxiliary model at propose time. No proposer dir was configured.
        return ADKProposerAgent(spec=spec, builtin_default=True)

    # A configured proposer dir with skills but no custom agent.py — the
    # skill-composed single-shot engine, the explicit opt-in.
    return DefaultProposerAgent(spec)


__all__ = [
    "DefaultProposerAgent",
    "ProposerAgent",
    "ProposerContext",
    "build_proposer_agent",
    "propose_via_engine",
]
