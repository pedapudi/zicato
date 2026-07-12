# ruff: noqa: PLC0415
# ADK symbols are imported INSIDE methods on purpose — see the module
# docstring. Hoisting them to module scope would force the optional
# ``google-adk`` extra on every importer of ``zicato.proposer.agent``,
# which the default (text-shim) proposer path must not require.
"""Tool-using proposer agent that runs on ADK's own ``Runner`` (Design A).

The default proposer is a single-shot text exchange: zicato hands the
auxiliary callable a ``(system, user, model) -> str`` prompt and parses
the returned string. That shim cannot express the *function calls* a
tool-using agent needs — the auxiliary callable is text-in / text-out by
contract — so a proposer that wants to read the parent snapshot, grep the
mutable surface, or consult the journal while it reasons cannot run on it.

Design A resolves this by running a tool-using proposer as a **native ADK
agent that declares its own ``model=``**, driven on ADK's own
:class:`~google.adk.runners.InMemoryRunner` — NOT through ``goldfive.run``
and NOT through zicato's auxiliary text shim. The agent author owns the
model; ``--auxiliary-call-llm`` does not govern it. The per-round task
(brief + skills + mutation manifest + patterns + loss + prior experiments
+ the JSON-schema demand) is delivered as the agent's run INPUT — the
custom agent owns its own static instruction — and the agent's read-only
tools (:data:`zicato.proposer.tools.DEFAULT_PROPOSER_TOOLS`) are bound to
the round's :class:`~zicato.proposer.tools.ProposerToolContext` for the
duration of the run.

The final assistant message is run through the SAME post-response loop the
default proposer uses (:func:`zicato.proposer.structured.parse_experiment_json`
→ forbidden-id enforcement → the caller's post-apply validation hook), and
a retryable failure feeds its feedback into the next run's input, within
the same bounded budget. The agent NEVER touches ``ctx.aux_call_llm`` —
that callable belongs to the default path.

Collusion guard
---------------
The ``is``-identity callable guard
(:func:`zicato.core.workspace.assert_distinct_callables`) does not apply
here: the proposer runs on its *own* model, not the auxiliary callable. The
proposer's model merely needs to differ from the harness model — a
documented author responsibility, not a hard gate. When both model strings
are trivially discoverable we emit a soft WARNING on a match; we do not
build a hard guard.

Lazy ADK imports
----------------
Every ``google.adk`` / ``google.genai`` import in this module is local to
the method that needs it, so importing :mod:`zicato.proposer.adk_agent`
(and, transitively, :mod:`zicato.proposer.agent`) never requires the
optional ``google-adk`` extra. Only :meth:`ADKProposerAgent.propose` and
:meth:`ADKProposerAgent._load_agent` pull ADK in.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from zicato.core.types import Experiment, ProposerSpec
from zicato.proposer.brief import enforce_forbidden
from zicato.proposer.prompts import render_system_prompt, render_user_prompt
from zicato.proposer.proposer import ProposerError
from zicato.proposer.structured import (
    ExperimentParseError,
    PostApplyValidationError,
    parse_experiment_json,
)
from zicato.proposer.tools import (
    DEFAULT_PROPOSER_TOOLS,
    ProposerToolContext,
    bind_proposer_tool_context,
)

if TYPE_CHECKING:  # pragma: no cover - typing-only import
    from zicato.proposer.agent import ProposerContext

log = logging.getLogger("zicato.proposer.adk_agent")

#: The ADK ``agent`` symbol a custom proposer dir exposes — mirrors the
#: harness adapter's ``module:agent`` convention. ``proposers/<name>/agent.py``
#: must define a module-level ``agent`` that is a built ``LlmAgent``.
_AGENT_SYMBOL = "agent"

#: Stable ADK app / session coordinates for a proposer run. The proposer
#: is single-session and single-user per run — we mint a fresh session id
#: per ``propose`` invocation but the app / user labels are constant.
_APP_NAME = "zicato-proposer"
_USER_ID = "zicato"

#: Static instruction for the built-in default tool-using proposer. It tells
#: the agent HOW to work — ground with the read-only tools, then emit the
#: structured JSON — while the per-round WHAT (brief, skills, mutation
#: manifest, patterns, loss, prior experiments, the JSON schema) arrives as
#: the run's input (:func:`_render_task_text`). A custom ``agent.py`` may
#: override this entirely; the built-in default uses it verbatim.
_DEFAULT_PROPOSER_INSTRUCTION = (
    "You are an improvement-proposer for a multi-agent system. The user "
    "message you receive carries the proposer brief, the available mutation "
    "points, the observed patterns, the current loss summary, the prior "
    "experiments, and the exact JSON schema your answer must follow.\n\n"
    "Before you answer, USE YOUR READ-ONLY TOOLS to ground your proposal:\n"
    "- call `list_mutation_points` to confirm the exact ids you may target;\n"
    "- call `read_mutable_file` / `grep_mutable` to inspect how a candidate "
    "target is used in the current generation's source;\n"
    "- call `read_journal` / `read_insights` to recall what prior rounds "
    "already tried and what the analyzer observed;\n"
    "- call `mutation_track_record` to see how experiments touching a "
    "candidate target have fared this epoch (advisory aggregates — "
    "experiments touching the point, not causal effects);\n"
    "- call `read_parent_diff` to see exactly what the LAST promotion "
    "changed, so you can build on it rather than blindly re-roll it;\n"
    "- call `mutation_usage` to find where a candidate target's current "
    "value/symbol is referenced across the snapshot before you change it.\n\n"
    "Then emit a SINGLE JSON object matching the schema in the user message "
    "— no prose, no markdown fences. The first character of your final "
    "response MUST be '{' and the last MUST be '}'."
)

#: Name of the built-in default proposer's ADK ``LlmAgent``.
_DEFAULT_PROPOSER_AGENT_NAME = "zicato_default_proposer"


def build_default_adk_agent(model: Any) -> Any:
    """Build the built-in default tool-using proposer ``LlmAgent``.

    This is the agent zicato runs when a contract does NOT configure a
    proposer dir — the DEFAULT proposer. It is a native ADK
    :class:`~google.adk.agents.LlmAgent` that declares ``model=`` (the model
    string the orchestrator threads from the workspace's auxiliary model,
    via :attr:`ProposerContext.model`) and opts into the full read-only
    proposer tool registry
    (:data:`zicato.proposer.tools.DEFAULT_PROPOSER_TOOLS`), so the default
    proposer can ground its proposal in the parent snapshot, the journal,
    and the analyzer insights while it reasons — capabilities the
    single-shot text shim cannot express.

    ``model`` is the agent's own model (a model string ADK understands or a
    built :class:`~google.adk.models.BaseLlm`). ADK is imported lazily here
    so this module stays importable without the optional ``google-adk``
    extra; the import error, if any, surfaces only when the default agent is
    actually built (i.e. when an ``evolve`` round runs).
    """
    from google.adk.agents import LlmAgent  # noqa: PLC0415

    return LlmAgent(
        name=_DEFAULT_PROPOSER_AGENT_NAME,
        model=model,
        instruction=_DEFAULT_PROPOSER_INSTRUCTION,
        tools=list(DEFAULT_PROPOSER_TOOLS),
    )


def _render_task_text(spec: ProposerSpec, ctx: ProposerContext, feedback: str) -> str:
    """Build the per-round agent INPUT from the same parts the default uses.

    A custom ADK agent owns its own static *instruction*; the per-round
    payload is delivered as the run's user message. We assemble it from the
    same two halves the default proposer composes — the skills-aware system
    scaffolding (:func:`render_system_prompt`) and the round's user payload
    (:func:`render_user_prompt`) — concatenated into one input string so the
    agent sees identical guidance, schema demand, and round context. On a
    retry, ``feedback`` is threaded into :func:`render_user_prompt` exactly
    as the default path threads it, so a rejected attempt's concrete error
    reaches the next run.
    """
    system_text = render_system_prompt(ctx.brief_text, spec.skills)
    user_text = render_user_prompt(
        current_loss_summary=ctx.current_loss_summary,
        patterns=ctx.patterns,
        mutations=ctx.mutations,
        feedback=feedback,
        prior_experiments=ctx.prior_experiments,
        restrict_visibility=ctx.restrict_visibility,
        custom_judge_names=ctx.custom_judge_names or frozenset(),
        failure_profile=ctx.failure_profile,
        process_exemplars=ctx.process_exemplars,
        genealogy=ctx.genealogy,
        calibration=ctx.calibration,
        sample_hint=ctx.sample_hint,
        mutation_track_records=ctx.mutation_track_records,
    )
    return f"{system_text}\n\n{user_text}"


def _resolve_generation_root(ctx: ProposerContext) -> Path:
    """Resolve the parent generation's snapshot dir the tools read.

    The read-only tools (``read_mutable_file`` / ``grep_mutable``) read the
    PARENT generation snapshot — the tree this round is about to patch. We
    resolve it the same way the orchestrator does, via the generation
    store's pure path math
    (:meth:`zicato.epoch.genstore.FileGenerationStore.snapshot_root`),
    from the lineage coordinates already on the context. When no
    ``workspace_root`` is set (a standalone propose with no on-disk
    workspace), we fall back to the workspace root itself if present, else
    the current directory — the read/grep tools then simply find no files,
    which is the correct degenerate behaviour for a contextless call.
    """
    if ctx.workspace_root is None:
        return Path.cwd()
    from zicato.epoch.genstore import default_generation_store

    store = default_generation_store(ctx.workspace_root)
    return store.snapshot_root(ctx.epoch_id, ctx.parent_generation_id)


@dataclass
class ADKProposerAgent:
    """A tool-using proposer run on ADK's own ``Runner`` (Design A).

    Wraps a native ADK ``LlmAgent`` — the built-in default agent, an agent
    loaded from ``proposers/<name>/agent.py``'s ``agent`` symbol, or an
    agent injected directly for tests — and drives it on an
    :class:`~google.adk.runners.InMemoryRunner`. The agent declares its own
    ``model=``; this class never calls the auxiliary text shim
    (``ctx.aux_call_llm``). Each run is wrapped in a
    :func:`~zicato.proposer.tools.bind_proposer_tool_context` block so the
    read-only proposer tools resolve the round's snapshot / manifest /
    journal, and the final assistant message is run through the same
    parse → forbidden-id → post-apply-validation loop as the default
    proposer, retrying within the bounded budget.

    Construct it one of three ways:

    * as the BUILT-IN DEFAULT proposer —
      ``ADKProposerAgent(spec, builtin_default=True)``. The agent is built
      lazily on first :meth:`propose` from :func:`build_default_adk_agent`,
      bound to ``ctx.model`` (the workspace's auxiliary model string). This
      is what ``build_proposer_agent`` returns when a contract configures no
      proposer dir — the default proposer is a tool-using ADK agent; or
    * from a proposer dir — ``ADKProposerAgent(spec, proposer_path=<dir>)``;
      the ``agent`` symbol is loaded lazily on first
      :meth:`propose` (mirroring the harness adapter's module load); or
    * with an already-built agent injected —
      ``ADKProposerAgent(spec, agent=<LlmAgent>)`` — the test seam, so a
      test can wire an ``LlmAgent`` to a
      :class:`~zicato.testing.adk_fake.FakeADKModel` and bypass disk
      loading entirely.
    """

    spec: ProposerSpec
    proposer_path: Path | None = None
    #: Injected pre-built ADK ``LlmAgent`` (the test seam). When set,
    #: ``proposer_path`` / ``builtin_default`` are ignored and no disk load
    #: or default-agent build happens.
    agent: Any | None = None
    #: When ``True`` this is the BUILT-IN DEFAULT proposer: the agent is
    #: built from :func:`build_default_adk_agent` bound to ``ctx.model`` on
    #: first :meth:`propose` (no ``agent.py`` on disk, no injected agent).
    #: ``build_proposer_agent`` sets this for the builtin-default spec. The
    #: per-run model is the auxiliary model the operator already configured,
    #: so the model-collusion smell test is intentionally skipped for the
    #: default (it is the documented, expected posture, not an author error).
    builtin_default: bool = False

    def _load_agent(self, ctx: ProposerContext | None = None) -> Any:
        """Return the ADK ``LlmAgent``, building / loading it if needed.

        Resolution order:

        * an ``agent`` injected at construction (the test seam) wins and is
          returned as-is;
        * when :attr:`builtin_default` is set, the agent is built from
          :func:`build_default_adk_agent` bound to ``ctx.model`` (the
          auxiliary model string the orchestrator threads on the context),
          and cached on :attr:`agent`. ``ctx`` MUST be supplied in this
          mode (``propose`` always supplies it);
        * otherwise the ``proposers/<name>/agent.py`` module is imported
          with ``proposer_path`` at the front of ``sys.path`` and its
          module-level ``agent`` symbol is fetched — mirroring
          :meth:`zicato.adapters.adk.ADKHarnessAdapter.load`.

        ADK is imported lazily (inside :func:`build_default_adk_agent` or
        further down) so this class stays importable without the
        ``google-adk`` extra.
        """
        if self.agent is not None:
            return self.agent
        if self.builtin_default:
            if ctx is None:  # pragma: no cover — propose always supplies ctx
                raise ProposerError(
                    [
                        "ADKProposerAgent built-in default needs a "
                        "ProposerContext to resolve its model from"
                    ]
                )
            agent = build_default_adk_agent(ctx.model)
            self.agent = agent
            return agent
        if self.proposer_path is None:
            raise ProposerError(
                [
                    "ADKProposerAgent has neither an injected agent, a "
                    "builtin_default flag, nor a proposer_path to load "
                    "proposers/<name>/agent.py from"
                ]
            )

        import importlib.util
        import sys

        agent_py = Path(self.proposer_path).resolve() / "agent.py"
        if not agent_py.is_file():
            raise ProposerError([f"proposer agent module not found at {agent_py}"])

        # Load the module file directly by path under a synthetic module
        # name so a proposer dir that is not on the import path still
        # resolves. The proposer dir is prepended to ``sys.path`` first so
        # the agent module can ``from zicato.proposer.tools import ...`` AND
        # import any sibling helpers it ships next to ``agent.py``.
        root_str = str(Path(self.proposer_path).resolve())
        if root_str not in sys.path:
            sys.path.insert(0, root_str)
        mod_name = f"_zicato_proposer_{Path(self.proposer_path).name}"
        spec = importlib.util.spec_from_file_location(mod_name, agent_py)
        if spec is None or spec.loader is None:
            raise ProposerError([f"could not build an import spec for {agent_py}"])
        module = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = module
        spec.loader.exec_module(module)
        try:
            agent = getattr(module, _AGENT_SYMBOL)
        except AttributeError as exc:
            raise ProposerError(
                [f"proposer module {agent_py} has no {_AGENT_SYMBOL!r} symbol"]
            ) from exc
        self.agent = agent
        return agent

    def _warn_on_model_collusion(self, agent: Any, ctx: ProposerContext) -> None:
        """Soft-WARN when the proposer model equals the harness model string.

        The proposer runs on its OWN model, so the hard ``is``-identity
        callable guard does not apply — but an author who points the
        proposer at the very model the harness runs is asking for
        collusion. We can only check this when both strings are trivially
        discoverable: the agent's ``model`` may be a string or a built
        ``BaseLlm`` (in which case its ``model`` attribute is the string),
        and the harness model string is not on the proposer context, so we
        compare against ``ctx.model`` (the AUXILIARY model) only as a
        best-effort smell test. This is advisory: it logs a WARNING and
        returns; it never raises and never blocks the run.
        """
        proposer_model = getattr(agent, "model", None)
        if not isinstance(proposer_model, str):
            proposer_model = getattr(proposer_model, "model", None)
        if (
            isinstance(proposer_model, str)
            and proposer_model
            and ctx.model
            and proposer_model == ctx.model
        ):
            log.warning(
                "proposer agent model %r equals the auxiliary model string; "
                "a tool-using proposer should run on a model distinct from "
                "the harness to avoid collusion (author responsibility)",
                proposer_model,
            )

    async def _run_agent_once(self, agent: Any, task_text: str) -> str:
        """Run the agent once on ADK's ``Runner`` and return its final text.

        Builds an :class:`~google.adk.runners.InMemoryRunner` over the
        agent (ADK-native — the agent's own ``model`` drives generation, no
        ``goldfive.run``, no auxiliary callable), creates a fresh session,
        and sends ``task_text`` as a single user :class:`Content`. Iterates
        the event stream and returns the concatenated text of the LAST
        final-response event with text content. ADK / genai are imported
        lazily here so the module stays importable without the extra.
        """
        import uuid

        from google.adk.runners import InMemoryRunner
        from google.genai import types as genai_types

        runner = InMemoryRunner(agent, app_name=_APP_NAME)
        session_id = uuid.uuid4().hex
        await runner.session_service.create_session(
            app_name=_APP_NAME,
            user_id=_USER_ID,
            session_id=session_id,
        )
        new_message = genai_types.Content(
            role="user",
            parts=[genai_types.Part(text=task_text)],
        )
        final_text = ""
        async for event in runner.run_async(
            user_id=_USER_ID,
            session_id=session_id,
            new_message=new_message,
        ):
            if not event.is_final_response():
                continue
            content = getattr(event, "content", None)
            if content is None or not getattr(content, "parts", None):
                continue
            text = "".join(part.text for part in content.parts if getattr(part, "text", None))
            if text:
                final_text = text
        return final_text

    async def propose(self, ctx: ProposerContext) -> Experiment:
        """Drive the custom ADK agent to a schema-valid :class:`Experiment`.

        Runs the agent on ADK's own ``Runner`` (its own ``model``), binding
        the read-only proposer tools to this round's
        :class:`~zicato.proposer.tools.ProposerToolContext` for the duration
        of each run, and lifts the final assistant message through the same
        post-response loop as the default proposer:

        1. :func:`~zicato.proposer.structured.parse_experiment_json`;
        2. forbidden-id enforcement against the emitted patches
           (:func:`~zicato.proposer.brief.enforce_forbidden`);
        3. the caller's optional post-apply validation hook
           (``ctx.validate_experiment``).

        A failure at any step is *retryable*: its message is appended to the
        next run's input as feedback and the agent re-runs, within the same
        ``ctx.max_retries + 1`` budget. After the budget is exhausted this
        raises :class:`~zicato.proposer.proposer.ProposerError` carrying the
        per-attempt errors — identical contract to the default path. The
        auxiliary callable (``ctx.aux_call_llm``) is NEVER invoked.
        """
        agent = self._load_agent(ctx)
        # The model-collusion smell test is a check on AUTHOR-supplied custom
        # agents. The built-in default deliberately runs on the auxiliary
        # model string the operator configured, so a match there is the
        # expected posture rather than a misconfiguration — skip the warning.
        if not self.builtin_default:
            self._warn_on_model_collusion(agent, ctx)

        generation_root = _resolve_generation_root(ctx)
        workspace_root = ctx.workspace_root if ctx.workspace_root is not None else Path.cwd()
        tool_ctx = ProposerToolContext(
            workspace_root=workspace_root,
            generation_root=generation_root,
            epoch_id=ctx.epoch_id,
            mutations=ctx.mutations,
            generation_id=ctx.parent_generation_id,
        )
        mutations_by_id = {mp.id: mp for mp in ctx.mutations}

        # The revise channel seeds the FIRST attempt's feedback (empty for
        # every non-revise call — byte-identical input); retries then
        # overwrite it with their own concrete errors exactly as before.
        feedback = ctx.revise_feedback
        attempt_errors: list[str] = []
        total_attempts = ctx.max_retries + 1
        for _attempt in range(total_attempts):
            task_text = _render_task_text(self.spec, ctx, feedback)
            try:
                with bind_proposer_tool_context(tool_ctx):
                    response_text = await self._run_agent_once(agent, task_text)
            except Exception as exc:  # noqa: BLE001 — opaque agent/model errors are common
                err = f"proposer agent run raised {type(exc).__name__}: {exc}"
                attempt_errors.append(err)
                feedback = err
                continue

            try:
                experiment = parse_experiment_json(
                    response_text,
                    epoch_id=ctx.epoch_id,
                    parent_gen=ctx.parent_generation_id,
                    new_gen=ctx.new_generation_id,
                    mutations_by_id=mutations_by_id,
                    custom_judge_names=ctx.custom_judge_names,
                )
            except ExperimentParseError as exc:
                err = str(exc)
                attempt_errors.append(err)
                feedback = err
                continue

            if ctx.forbidden_ids:
                violations = enforce_forbidden(list(experiment.patches), ctx.forbidden_ids)
                if violations:
                    err = "patches violate proposer-brief forbidden-edits list: " + "; ".join(
                        violations
                    )
                    attempt_errors.append(err)
                    feedback = err
                    continue

            if ctx.validate_experiment is not None:
                try:
                    post_apply_errors = await ctx.validate_experiment(experiment)
                except PostApplyValidationError as exc:
                    post_apply_errors = exc.errors
                if post_apply_errors:
                    err = "patches failed post-apply validation: " + "; ".join(post_apply_errors)
                    attempt_errors.append(err)
                    feedback = err
                    continue

            return experiment

        raise ProposerError(attempt_errors)


__all__ = ["ADKProposerAgent", "build_default_adk_agent"]
