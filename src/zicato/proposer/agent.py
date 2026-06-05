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

Two agent implementations ship here. The built-in
:class:`DefaultProposerAgent` is the single-shot text-shim path: it drives
:func:`propose_experiment` over the auxiliary callable. A proposer dir that
carries a custom ``agent.py`` (``spec.agent_source_sha256`` is set) selects
the Design-A path instead — :func:`build_proposer_agent` returns an
:class:`~zicato.proposer.adk_agent.ADKProposerAgent`, a native ADK agent
that declares its own ``model=`` and runs on ADK's own ``Runner`` (NOT the
auxiliary text shim, which cannot express the function calls a tool-using
agent needs). The ADK module is imported lazily so the default path stays
free of the optional ``google-adk`` extra.
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
        )


def build_proposer_agent(
    spec: ProposerSpec,
    proposer_path: Path | None = None,
) -> ProposerAgent:
    """Build the :class:`ProposerAgent` for a resolved proposer spec.

    Returns a :class:`DefaultProposerAgent` — the skills-aware single-shot
    built-in — for the built-in default spec and for any ``dir:*`` proposer
    that carries *no* custom agent module (``spec.agent_source_sha256`` is
    ``None``). Such a proposer steers the default engine purely through its
    skills, over the auxiliary text shim.

    When the proposer dir ships a ``proposers/<name>/agent.py``
    (``spec.agent_source_sha256`` is set), this returns an
    :class:`~zicato.proposer.adk_agent.ADKProposerAgent` — the Design-A
    tool-using path: a native ADK agent that declares its own ``model=``
    and runs on ADK's own ``Runner`` (the auxiliary callable does NOT
    govern it). ``proposer_path`` is the proposer dir the
    ``agent.py`` module is loaded from; the orchestrator threads the same
    frozen ``proposer_path`` it resolved the spec from. The ADK module is
    imported lazily so the default path never requires the ``google-adk``
    extra.

    Raises
    ------
    ValueError
        When the spec carries a custom agent module but no
        ``proposer_path`` was supplied to load it from — a misconfiguration
        the caller must fix rather than silently fall back to the default.
    """
    if spec.agent_source_sha256 is not None:
        if proposer_path is None:
            raise ValueError(
                "spec declares a custom proposer agent (agent_source_sha256 is "
                "set) but no proposer_path was supplied to load "
                "proposers/<name>/agent.py from"
            )
        # Lazy import: ADKProposerAgent pulls in the optional google-adk
        # extra only when actually constructed, so the default path stays
        # dependency-light.
        from zicato.proposer.adk_agent import ADKProposerAgent  # noqa: PLC0415

        return ADKProposerAgent(spec=spec, proposer_path=proposer_path)
    return DefaultProposerAgent(spec)


__all__ = [
    "DefaultProposerAgent",
    "ProposerAgent",
    "ProposerContext",
    "build_proposer_agent",
]
