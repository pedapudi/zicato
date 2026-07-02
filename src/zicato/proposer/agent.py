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

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from zicato.core.types import (
    Experiment,
    MutationPoint,
    Pattern,
    PriorExperiment,
    ProposerSpec,
)
from zicato.proposer.proposer import ExperimentValidator, propose_experiment

if TYPE_CHECKING:  # pragma: no cover - typing-only import
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
    #: Optional per-sample edit-class steering line — the best-of-N slate
    #: diversifier (:data:`zicato.proposer.best_of_n.EDIT_CLASS_HINTS`). The
    #: wrapper stamps a DISTINCT hint on each slate slot's context via
    #: ``dataclasses.replace`` so the N samples explore different edit
    #: strategies. A static instruction string carrying no board identity —
    #: it composes with the restricted-visibility envelope untouched. Empty
    #: (the default — every single-sample propose) renders no section.
    sample_hint: str = ""


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
        return await propose_experiment(
            epoch_id=ctx.epoch_id,
            parent_generation_id=ctx.parent_generation_id,
            new_generation_id=ctx.new_generation_id,
            patterns=ctx.patterns,
            mutations=ctx.mutations,
            brief_text=ctx.brief_text,
            current_loss_summary=ctx.current_loss_summary,
            aux_call_llm=ctx.aux_call_llm,
            model=ctx.model,
            max_retries=ctx.max_retries,
            forbidden_ids=ctx.forbidden_ids,
            workspace_root=ctx.workspace_root,
            validate_experiment=ctx.validate_experiment,
            meta_loop_emitter=ctx.meta_loop_emitter,
            custom_judge_names=ctx.custom_judge_names,
            prior_experiments=ctx.prior_experiments,
            skills=self.spec.skills,
            restrict_visibility=ctx.restrict_visibility,
            failure_profile=ctx.failure_profile,
            sample_hint=ctx.sample_hint,
        )


def build_proposer_agent(
    spec: ProposerSpec,
    proposer_path: Path | None = None,
) -> ProposerAgent:
    """Build the :class:`ProposerAgent` for a resolved proposer spec.

    Three outcomes, in resolution order:

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
        ``proposer_path`` was supplied to load it from — a misconfiguration
        the caller must fix rather than silently fall back to the default.
    """
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
]
