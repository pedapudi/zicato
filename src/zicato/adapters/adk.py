"""ADK :class:`~zicato.adapters.base.HarnessAdapter` for goldfive-wrapped trees.

Drives Google ADK agent trees (``LlmAgent`` and ``BaseAgent`` subclasses)
through :mod:`goldfive` so the inner harness gets the same goal / plan
/ drift overlay every other zicato target uses. The shape mirrors
goldfive's own one-line ``goldfive.run`` surface: the adapter takes
care of *which* agent symbol to import from *which* generation
snapshot, then hands the live agent to ``goldfive.run`` for the
actual turn.

Lazy imports
------------

This module imports :mod:`goldfive` and :mod:`google.adk` only inside
:meth:`ADKHarnessAdapter.load` (and the runnable's :meth:`run`), so
``from zicato.adapters import HarnessAdapter`` does not force the
ADK extra on consumers who only need the Protocol surface.

:mod:`zicato.mutation.enumerator` is owned by a sibling module and is
imported lazily inside :meth:`ADKHarnessAdapter.mutation_points` for
the same reason — the adapter does not transitively pull in the
enumerator's parser machinery at import time.

Generation-snapshot loading
---------------------------

:meth:`ADKHarnessAdapter.load` puts ``generation_root`` at the front
of ``sys.path`` and re-imports the entrypoint module from that root.
We deliberately do NOT restore ``sys.modules`` after the load — the
tournament-runner contract in v0+1 is "fresh process per generation",
so a single-process pass-through here is enough. Multi-generation
processes can wrap calls themselves if they need stricter isolation.

Transcript extraction
---------------------

goldfive's :class:`~goldfive.results.ExecutionOutcome` carries the
session as ``outcome.session``; the user-facing assistant outputs
land on ``session.completed_results`` keyed by task id. We treat the
ordered values of that dict as the run's transcript and the last
entry as :attr:`RunResult.final_output`. For trees that produce no
``completed_results`` (e.g. when the planner short-circuited
PassthroughPlanner with no LLM available), the transcript is empty
and :attr:`final_output` is ``""``.

Judges
------

goldfive#437 lets a caller pass a custom :class:`~goldfive.judges.Judge`
list into ``goldfive.run`` / ``goldfive.wrap`` via ``judges=[...]``. The
adapter assembles that list per board entry through
:func:`zicato.judge_runtime.assemble_judges`: goldfive's default
built-in judges (minus any the board's ``disable_drift`` suppressed)
plus the entry's declared :class:`~zicato.core.JudgeSpec` judges, each
turned into a live goldfive ``Judge``. Inline judges run on zicato's
*auxiliary* callable (the two-callable rule); python judges bring their
own dependencies. When the entry declares no custom judges and the
board suppresses nothing, the assembled list equals goldfive's default
set, so behaviour is byte-identical to a plain ``goldfive.run`` call.

Judge-only mode
---------------

A board may opt into *judge-only* evaluation via its board-level
``judge_only`` flag (``Board.judge_only`` → ``board_meta`` header →
stamped onto each entry's ``context`` by the tournament runner →
:func:`_entry_judge_only`). In judge-only mode goldfive still JUDGES the
wrapped agent — the drift / process judges stay armed exactly as above —
but does ZERO steering: no goal-derivation LLM call, no planner
replanning, no drift-triggered refine. This is implemented by spreading
:func:`_judge_only_overrides` (a one-task ``StaticPlanner`` so the native
agent tree still runs on goldfive's overlay path and produces a
transcript, a ``LiteralGoalDeriver`` so the entry input becomes the goal
verbatim, AND a :class:`_JudgeOnlySteerer` that suppresses the refine
loop) into every ``goldfive.run`` call for the entry. The default
(``judge_only`` False) leaves the steering path untouched and
byte-identical. The flag folds into the epoch contract hash, so flipping
it opens a new epoch.

Why the steerer override is required
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The ``StaticPlanner`` alone does NOT yield zero refine attempts. goldfive
dispatches EVERY non-INFO drift through ``DriftObserver.handle_drift`` →
the cancel + refine ladder, regardless of which planner is installed. A
``StaticPlanner`` makes ``planner.refine`` return ``None``, but goldfive
treats a ``None`` refine as *handler exhaustion* and escalates to
``DRIFT_KIND_HUMAN_INTERVENTION_REQUIRED`` — emitting ``refine_attempted``
/ ``refine_failed`` and (under the ``multi_turn_emulated`` persona, where
a CRITICAL ``no_fabricated_numbers`` judge keeps re-firing) spinning to
the wall-clock budget and aborting the run. ``SteeringConfig.observation_only``
does not help either: it gates only the three steer *injection* points
while ``planner.refine`` still runs.

The custom-judge SCORING signal that zicato's reducer reads is the
``custom``-kind ``DriftDetected`` event (attributed to its ``judge_name``
via the paired ``JudgementEmitted``) — and ``handle_drift`` emits that
``DriftDetected`` *before* it enters the ladder. So judge-only suppresses
the refine loop WITHOUT losing the scalar by overriding ``handle_drift``
to emit the ``DriftDetected`` and return, skipping the cancel / promote /
refine machinery entirely. ``JudgementEmitted`` is published upstream in
``DefaultSteerer.evaluate_judges`` and is unaffected. Net effect under
judge-only on EVERY board-run path — gauntlet, multi-challenger, and
``multi_turn_emulated`` — is pure observe + judge: ZERO ``steering``
decisions and ZERO ``refine_attempted`` events.
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import logging
import sys
import time
import uuid
from collections.abc import AsyncGenerator, Iterable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from zicato.core import BoardEntry, MutationPoint, RunResult, RuntimeConfig

if TYPE_CHECKING:
    from zicato.adapters.base import RunnableHarness

log = logging.getLogger("zicato.adapters.adk")


# ---------------------------------------------------------------------------
# Entrypoint resolution
# ---------------------------------------------------------------------------


def _split_entrypoint(entrypoint: str) -> tuple[str, str]:
    """Split a ``"module.path:agent_symbol"`` spec into its two halves.

    Raises :class:`ValueError` with an actionable message on a malformed
    spec — empty, missing the colon, multiple colons, empty module, or
    empty symbol. The colon convention mirrors Python's ``entry_points``
    syntax so operators authoring zicato configs feel at home.
    """
    if not entrypoint or ":" not in entrypoint:
        raise ValueError(
            f"ADKHarnessAdapter: entrypoint must be 'module.path:agent_symbol', got {entrypoint!r}"
        )
    parts = entrypoint.split(":")
    if len(parts) != 2:
        raise ValueError(
            f"ADKHarnessAdapter: entrypoint must contain exactly one ':' "
            f"separator, got {entrypoint!r}"
        )
    module_path, symbol = parts
    module_path = module_path.strip()
    symbol = symbol.strip()
    if not module_path or not symbol:
        raise ValueError(
            f"ADKHarnessAdapter: entrypoint module and symbol must be non-empty, got {entrypoint!r}"
        )
    return module_path, symbol


def _default_mutable_trees(module_path: str) -> list[Path]:
    """Best-effort default for :attr:`ADKHarnessAdapter.mutable_trees`.

    Resolves the entrypoint module via :func:`importlib.util.find_spec`
    and returns the directory containing the module file as the single
    mutable tree. Returns an empty list when the module cannot be
    resolved at construction time — the adapter does not fail
    construction on a missing module because tests construct adapters
    against modules that will only exist after a later patch applier
    pass.
    """
    try:
        spec = importlib.util.find_spec(module_path)
    except (ImportError, ValueError):
        return []
    if spec is None or spec.origin is None:
        return []
    origin = Path(spec.origin)
    if not origin.is_file():
        return []
    return [origin.parent.resolve()]


# ---------------------------------------------------------------------------
# Transcript extraction from a goldfive ExecutionOutcome
# ---------------------------------------------------------------------------


def _outcome_transcript(outcome: Any) -> tuple[str, ...]:
    """Return the ordered user-facing assistant outputs from ``outcome``.

    goldfive 0.x represents per-task assistant text on
    ``outcome.session.completed_results`` — a ``dict[str, str]`` keyed
    by task id and ordered by completion. We treat those values, in
    insertion order, as the run's transcript. For runs that produced
    no ``completed_results`` (PassthroughPlanner with no LLM, or a
    failed run that aborted before any task completed), the transcript
    is empty.
    """
    session = getattr(outcome, "session", None)
    if session is None:
        return ()
    completed = getattr(session, "completed_results", None)
    if not completed:
        return ()
    return tuple(str(v) for v in completed.values())


# ---------------------------------------------------------------------------
# goldfive RuntimeConfig — per-call LLM timeout
# ---------------------------------------------------------------------------


def _goldfive_runtime() -> Any:
    """Build the goldfive ``RuntimeConfig`` for a ``goldfive.run`` call.

    goldfive's :class:`~goldfive.config.AgentConfig.call_timeout_ms`
    defaults to 120 000 ms. A real reasoning model under concurrency
    legitimately exceeds 120 s on a single long-prompt LLM call, so the
    raw default aborts healthy calls and fires a spurious
    ``LLM_CALL_TIMEOUT`` drift. zicato raises the per-call budget to
    :attr:`zicato.config.RuntimeTuningConfig.harness_call_timeout_ms`
    (operator-tunable via ``ZICATO_HARNESS_CALL_TIMEOUT_MS``).

    We start from :meth:`goldfive.config.RuntimeConfig.from_env` so
    every other goldfive subsystem (embedding, judge endpoint,
    drift thresholds, ...) keeps its env-driven configuration, and only
    replace the ``agent`` sub-config's ``call_timeout_ms``. An explicit
    ``GOLDFIVE_AGENT_CALL_TIMEOUT_MS`` env var still wins — when set, we
    leave goldfive's env-resolved value untouched so an operator who
    tunes goldfive directly is not overridden.
    """
    import dataclasses  # noqa: PLC0415
    import os  # noqa: PLC0415

    from goldfive.config import RuntimeConfig as GoldfiveRuntimeConfig  # noqa: PLC0415

    from zicato.config import load_config  # noqa: PLC0415

    runtime = GoldfiveRuntimeConfig.from_env()
    if os.environ.get("GOLDFIVE_AGENT_CALL_TIMEOUT_MS"):
        # Operator tuned goldfive directly — defer to their value.
        return runtime
    timeout_ms = load_config().runtime.harness_call_timeout_ms
    agent = dataclasses.replace(runtime.agent, call_timeout_ms=timeout_ms)
    return dataclasses.replace(runtime, agent=agent)


# ---------------------------------------------------------------------------
# call_llm-backed ADK model — the LAST-RESORT text-only shim.
#
# This whole section builds the TEXT-ONLY fallback for the inner ADK agents'
# own task turns. It is the LAST of three parallel ADK-model paths and the
# wrong one for any function-calling target:
#
#   1. ``models_config.build_adk_model``      — the canonical builder: a real
#      (typically function-calling ``LiteLlm``) ``BaseLlm`` from a configured
#      ``{model, endpoint, api_key_env}`` spec. PREFERRED. Injected by
#      :func:`rebind_tree_models_to_adk_model` when ``models.harness`` is set.
#   2. ADK's own ``LLMRegistry`` native resolution — a bare ``"openai/<model>"``
#      / ``LiteLlm``-resolvable string an author hardcoded keeps native
#      tool/function calling; :func:`_resolves_to_native_function_calling`
#      detects it and the shim LEAVES IT ALONE.
#   3. THIS shim (:func:`rebind_tree_models_to_call_llm`) — fires ONLY on a
#      bare string that resolves to a ``google.genai``-backed client
#      (``gemini-*`` / ``gemma-*``) or is wholly unresolvable. It exists to
#      stop the genai-client GC flood (below) for an UNCONFIGURED / misconfigured
#      target — and it is TEXT-ONLY: it carries NO ``function_declarations``,
#      so any agent rebound to it loses native tool/function calling and a
#      tool-driven tree degenerates to a single text turn. Hence last-resort.
# ---------------------------------------------------------------------------


def _build_call_llm_adk_model_class() -> type:
    """Build the LAST-RESORT, TEXT-ONLY ``BaseLlm`` shim driven by a call_llm.

    LIMITATION (load-bearing): this shim is **text-only**. Its
    :meth:`generate_content_async` flattens the request to a ``(system, user)``
    text pair and never reads ``llm_request.config.tools`` — so the model it
    yields carries NO ``function_declarations`` and CANNOT make tool/function
    calls. Rebinding a function-calling agent to it silently strips its tools
    and reduces a tool-driven tree to a single text turn. It is therefore the
    LAST resort, used only when neither a configured inner model (path 1) nor a
    native ``LiteLlm``-resolvable string (path 2) is available; see the section
    banner above and :func:`rebind_tree_models_to_call_llm`.

    The harness threads its ``(system, user, model) -> str`` callable into
    every ``goldfive.run`` invocation, and goldfive's *steering* (goal
    derive / planner refine / judges) runs on it. But the wrapped ADK
    ``LlmAgent``s' OWN task turns still run on each agent's declared
    ``model`` field — a bare model string in live runs. ADK resolves that
    string through ``LLMRegistry.new_llm`` and constructs a
    :class:`google.genai.Client`'s ``BaseApiClient`` for it. When no Google
    API key is present (the vLLM / call_llm path never needs one), that
    constructor raises ``ValueError('No API key')`` *before* it sets
    ``_async_httpx_client`` — leaving a partial client whose ``__del__``
    schedules ``aclose()``, which then raises
    ``AttributeError('_async_httpx_client')`` on GC. With one such client
    per turn this floods the log with hundreds of "Task exception was never
    retrieved" tracebacks. The client is never used for real generation.

    The fix is to give each ``LlmAgent`` a real ``BaseLlm`` backed by the
    harness ``call_llm`` so ADK's ``canonical_model`` returns it directly
    (it short-circuits on ``isinstance(self.model, BaseLlm)``), never
    touching ``LLMRegistry.new_llm`` / the genai client. This is the inverse
    of goldfive's :func:`goldfive._llm_detect.make_default_adk_call_llm`
    (which wraps a ``BaseLlm`` *into* a call_llm); here we wrap a call_llm
    *into* a ``BaseLlm``.

    Defined as a factory so the ADK / genai symbols are imported only when a
    rebind actually happens — keeping this module importable without the
    optional ``google-adk`` extra.
    """
    from google.adk.models import BaseLlm  # noqa: PLC0415
    from google.adk.models.llm_request import LlmRequest  # noqa: PLC0415
    from google.adk.models.llm_response import LlmResponse  # noqa: PLC0415
    from google.genai import types as genai_types  # noqa: PLC0415

    class _CallLlmADKModel(BaseLlm):
        """A :class:`BaseLlm` whose generation routes through a call_llm.

        Implements the single abstract method
        :meth:`generate_content_async` by flattening the ADK
        :class:`LlmRequest` into the ``(system, user)`` text pair the
        harness ``call_llm`` expects, awaiting it, and yielding a single
        text :class:`LlmResponse`. ``model`` preserves the original
        model-string label purely for observability; the actual generation
        is the call_llm's, which is already model-bound.

        The system instruction is read from
        ``llm_request.config.system_instruction`` and the user text is the
        concatenation of every text part across the request's ``contents``
        (mirroring how ADK assembles a turn). This is the symmetric reverse
        of goldfive's forward adapter, which puts ``system`` on
        ``config.system_instruction`` and ``user`` on a single user
        ``Content`` — so a round-trip through both is lossless for the
        text-only turns the harness drives.
        """

        # ``call_llm`` is the harness callable; declared as a pydantic
        # field (BaseLlm is a pydantic BaseModel) so assignment validates.
        call_llm: Any = None

        async def generate_content_async(
            self,
            llm_request: LlmRequest,
            stream: bool = False,
        ) -> AsyncGenerator[Any, None]:
            del stream  # the harness call_llm is non-streaming text-in/out
            # TEXT-ONLY by construction: we read only the system + user TEXT and
            # deliberately ignore ``llm_request.config.tools`` (the agent's
            # ``function_declarations``). The reply is a single text part with no
            # ``function_call`` — so an agent on this shim CANNOT call its tools.
            # That is the no-tools limitation that makes this a last resort; the
            # configured-inner-model path keeps tools (see the section banner).
            config = getattr(llm_request, "config", None)
            system = ""
            if config is not None:
                raw_system = getattr(config, "system_instruction", None)
                if isinstance(raw_system, str):
                    system = raw_system
                elif raw_system is not None:
                    # Some ADK shapes carry a Content/list here; flatten its
                    # text parts defensively rather than str()-ing the object.
                    system = _flatten_request_text([raw_system])
            user = _flatten_request_text(getattr(llm_request, "contents", None) or ())
            reply = await self.call_llm(system, user, self.model)
            yield LlmResponse(
                content=genai_types.Content(
                    role="model",
                    parts=[genai_types.Part(text=str(reply))],
                )
            )

    return _CallLlmADKModel


def _flatten_request_text(contents: Iterable[Any]) -> str:
    """Concatenate every text part across an ADK request's ``contents``.

    Walks each ``Content``-shaped item's ``parts`` and joins the non-empty
    ``part.text`` values with newlines, in order. Tolerant of bare strings
    and shapes missing ``parts`` so a malformed request degrades to an empty
    (or best-effort) user string rather than raising inside the model.
    """
    chunks: list[str] = []
    for content in contents:
        if isinstance(content, str):
            if content:
                chunks.append(content)
            continue
        parts = getattr(content, "parts", None) or ()
        for part in parts:
            text = getattr(part, "text", None)
            if text:
                chunks.append(str(text))
    return "\n".join(chunks)


def _iter_agent_tree(root: Any) -> Iterable[Any]:
    """Yield ``root`` and every agent reachable through the ADK tree edges.

    Follows the same edges goldfive's own ADK adapter walks —
    ``sub_agents`` (the native agent tree), ``inner_agent`` (overlay
    wrappers), and ``AgentTool.agent`` (tool-wrapped agents reachable via an
    agent's ``tools``) — so every ``LlmAgent`` that ADK could drive a turn
    on is visited exactly once. Cycle-safe via an id-based visited set.
    """
    seen: set[int] = set()
    stack: list[Any] = [root]
    while stack:
        node = stack.pop()
        if node is None or id(node) in seen:
            continue
        seen.add(id(node))
        yield node
        for sub in getattr(node, "sub_agents", None) or ():
            stack.append(sub)
        inner = getattr(node, "inner_agent", None)
        if inner is not None:
            stack.append(inner)
        for tool in getattr(node, "tools", None) or ():
            tool_agent = getattr(tool, "agent", None)
            if tool_agent is not None:
                stack.append(tool_agent)


def _resolves_to_native_function_calling(model_str: str) -> bool:
    """True if ``model_str`` resolves to a real function-calling ``BaseLlm``.

    Uses :meth:`LLMRegistry.resolve`, which returns the model *class* without
    instantiating it — so this classifier never constructs (and therefore
    never floods on the garbage-collection of) a ``google.genai`` client.

    A provider-style identifier such as ``"openai/<model>"`` resolves to ADK's
    :class:`LiteLlm`, which carries native tool/function calling against an
    OpenAI-compatible endpoint (e.g. a local vLLM). Those agents must NOT be
    rebound to the text-only ``call_llm`` shim — doing so silently strips
    function calls and reduces a tool-calling tree to a single text turn.

    Bare ``gemini-*`` / ``gemma-*`` strings resolve to the ``google.genai``
    backed clients (``Gemini`` / ``Gemma``) — the unused-client flood source
    the shim exists to avoid — and unresolvable strings raise; both return
    ``False`` so the caller routes them through the shim as a last resort.
    Returns ``False`` if ADK / litellm is unavailable (no native path exists).
    """
    try:
        from google.adk.models.lite_llm import LiteLlm  # noqa: PLC0415
        from google.adk.models.registry import LLMRegistry  # noqa: PLC0415
    except ImportError:
        return False
    try:
        cls = LLMRegistry.resolve(model_str)
    except Exception:  # noqa: BLE001 — unresolvable string → shim fallback
        return False
    return issubclass(cls, LiteLlm)


def rebind_tree_models_to_call_llm(root: Any, call_llm: Any) -> int:
    """LAST-RESORT: rebind only *unresolvable* string models to the text shim.

    The third and last ADK-model path (see the section banner): used only when
    no configured inner model (:func:`rebind_tree_models_to_adk_model`) and no
    natively-resolvable ``LiteLlm`` string is available. Walks the agent tree
    (root + ``sub_agents`` / ``inner_agent`` / ``AgentTool.agent`` edges). For
    each agent whose ``model`` is a bare string that does NOT resolve to a real
    function-calling model, replaces it with the TEXT-ONLY :class:`BaseLlm`
    shim backed by ``call_llm`` so ADK's ``canonical_model`` returns it
    directly and NEVER resolves the string through ``LLMRegistry.new_llm``
    (which would build the unused, flood-causing google.genai client). Because
    the shim carries no tools, every rebound agent loses native tool/function
    calling — which is why this only fires when no tool-preserving path exists.

    Two kinds of agent are deliberately LEFT UNTOUCHED:

    * an agent whose ``model`` is already a :class:`BaseLlm` — an author who
      wired a real model object owns it; and
    * an agent whose ``model`` string resolves to a function-calling
      :class:`LiteLlm` (e.g. ``"openai/<model>"`` against a local endpoint) —
      rebinding it to the text-only ``call_llm`` shim would strip native
      tool/function calling and reduce a tool-calling tree to a single text
      turn (see :func:`_resolves_to_native_function_calling`).

    Any agent that IS rebound has its native tool-calling disabled (the shim is
    text-only); a single ``warning`` names them so a degraded target is loud
    rather than silently inert.

    Returns the number of agents rebound. A no-op (returns 0) when ``call_llm``
    is falsy. Idempotent — a second pass finds every model already a
    ``BaseLlm`` (or LiteLlm-resolvable) and rebinds none.
    """
    if not call_llm:
        return 0
    from google.adk.models import BaseLlm  # noqa: PLC0415

    model_cls = _build_call_llm_adk_model_class()
    rebound_names: list[str] = []
    for agent in _iter_agent_tree(root):
        # Only ``LlmAgent``-shaped nodes carry a ``model``; a plain
        # ``BaseAgent`` (or an overlay wrapper) simply has no such field.
        if not hasattr(agent, "model"):
            continue
        current = agent.model
        if isinstance(current, BaseLlm):
            continue  # author-supplied model object — leave it.
        if isinstance(current, str) and current and _resolves_to_native_function_calling(current):
            continue  # real LiteLlm endpoint model — keep native tool-calling.
        label = current if isinstance(current, str) and current else "call-llm"
        agent.model = model_cls(model=label, call_llm=call_llm)
        rebound_names.append(getattr(agent, "name", "?"))
    if rebound_names:
        log.warning(
            "ADK rebind: %d agent(s) %s had no resolvable function-calling "
            "model; routing their turns through the harness call_llm "
            "(TEXT-ONLY — native tool/function calling is DISABLED for them). "
            "Configure a LiteLlm endpoint model (e.g. 'openai/<model>' with an "
            "endpoint + the 'adk' extra) to restore tool-calling.",
            len(rebound_names),
            rebound_names,
        )
    return len(rebound_names)


def rebind_tree_models_to_adk_model(root: Any, model: Any) -> int:
    """Rebind every string-model ``LlmAgent`` in ``root``'s tree to ``model``.

    The config-driven counterpart to :func:`rebind_tree_models_to_call_llm`:
    when the workspace configures an inner agent model (a ``models.harness``
    model spec built into a :class:`BaseLlm` — typically a function-calling
    :class:`LiteLlm` pointed at the live endpoint), the adapter injects that
    object into every string-model agent. The configured model is the source
    of truth for the inner harness, so it overrides whatever bare model string
    the target hardcoded (e.g. the example target's ``"openai/gpt-4o-mini"``
    default) — unlike the shim path, this keeps native tool/function calling.

    Agents whose ``model`` is already a :class:`BaseLlm` are left untouched —
    an author who wired a real model object owns it. The same ``model``
    instance is shared across the tree (a ``BaseLlm`` is stateless per call,
    exactly as a normal multi-agent ADK app shares one model). Returns the
    number of agents rebound; a no-op (returns 0) when ``model`` is falsy.
    """
    if model is None:
        return 0
    from google.adk.models import BaseLlm  # noqa: PLC0415

    rebound = 0
    for agent in _iter_agent_tree(root):
        if not hasattr(agent, "model"):
            continue
        if isinstance(agent.model, BaseLlm):
            continue  # author-supplied model object — leave it.
        agent.model = model
        rebound += 1
    return rebound


# ---------------------------------------------------------------------------
# Judge assembly inputs from a board entry
# ---------------------------------------------------------------------------


def _entry_judge_specs(entry: BoardEntry) -> tuple[Any, ...]:
    """Return the entry's declared :class:`~zicato.core.JudgeSpec` tuple.

    Reads ``BoardEntry.judges`` defensively via :func:`getattr` so the
    adapter keeps working against a :class:`BoardEntry` revision that
    predates the ``judges`` field (the field is owned by
    ``zicato/core/types.py``; this adapter must not assume a particular
    landing order). An absent / ``None`` field yields an empty tuple —
    the entry simply contributes no custom judges.
    """
    judges = getattr(entry, "judges", None)
    if not judges:
        return ()
    return tuple(judges)


#: ``BoardEntry.context`` key the tournament runner stamps the
#: board-level ``disable_drift`` suppression set under. Kept in sync with
#: ``zicato.tournament.runner._DISABLE_DRIFT_CONTEXT_KEY`` — the two ends
#: meet on this single string.
_DISABLE_DRIFT_CONTEXT_KEY = "disable_drift"


def _entry_disable_drift(entry: BoardEntry) -> tuple[Any, ...]:
    """Return the drift kinds the board wants suppressed for ``entry``.

    ``disable_drift`` is a board-LEVEL setting (``Board.disable_drift``),
    but the :class:`~zicato.adapters.base.RunnableHarness` Protocol hands
    the adapter a :class:`BoardEntry`, not the owning ``Board``. The
    tournament runner therefore stamps the board-level suppression set
    onto every entry's :attr:`~zicato.core.BoardEntry.context` mapping
    under :data:`_DISABLE_DRIFT_CONTEXT_KEY` (see
    ``zicato.tournament.runner._stamp_disable_drift``) — ``context`` is
    the one per-entry channel that survives the runner -> subprocess
    worker -> :func:`zicato.core.validate_board_entry` round-trip.

    The value is a comma / whitespace separated list of
    :class:`goldfive.DriftKind` wire strings. Returns an empty tuple when
    the entry carries no such key, in which case goldfive's built-in
    judges all stay default-on.
    """
    raw = (getattr(entry, "context", {}) or {}).get(_DISABLE_DRIFT_CONTEXT_KEY)
    if not raw:
        return ()
    # ``context`` is a string-valued mapping; split on commas /
    # whitespace into individual drift-kind wire strings.
    return tuple(token for token in raw.replace(",", " ").split() if token)


#: ``BoardEntry.context`` key the tournament runner stamps the
#: board-level ``judge_only`` flag under. Kept in sync with
#: ``zicato.tournament.runner._JUDGE_ONLY_CONTEXT_KEY`` — the two ends
#: meet on this single string.
_JUDGE_ONLY_CONTEXT_KEY = "judge_only"


def _entry_judge_only(entry: BoardEntry) -> bool:
    """Return whether ``entry`` should run in judge-only (no-steering) mode.

    ``judge_only`` is a board-LEVEL setting (``Board.judge_only``), but
    the :class:`~zicato.adapters.base.RunnableHarness` Protocol hands the
    adapter a :class:`BoardEntry`, not the owning ``Board``. The
    tournament runner therefore stamps the flag onto every entry's
    :attr:`~zicato.core.BoardEntry.context` mapping under
    :data:`_JUDGE_ONLY_CONTEXT_KEY` (see
    ``zicato.tournament.runner._stamp_judge_only``) — ``context`` is the
    one per-entry channel that survives the runner -> subprocess worker
    -> :func:`zicato.core.validate_board_entry` round-trip.

    The value is the lowercase wire string ``"true"`` / ``"false"``.
    Returns ``False`` when the entry carries no such key (the default,
    steering-on path).
    """
    raw = (getattr(entry, "context", {}) or {}).get(_JUDGE_ONLY_CONTEXT_KEY)
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() == "true"


def _build_judge_only_steerer(call_llm: Any, runtime: Any) -> Any:
    """Build the :class:`DefaultSteerer` subclass that suppresses refine.

    Under judge-only the goal is ZERO steering AND ZERO refine attempts
    while the scoring signal is preserved. The ``StaticPlanner`` alone
    does not achieve that: goldfive dispatches every non-INFO drift
    through ``DriftObserver.handle_drift`` → the cancel + refine ladder,
    and a ``StaticPlanner.refine`` returning ``None`` is treated as
    handler exhaustion → ``HUMAN_INTERVENTION_REQUIRED`` escalation (which
    emits ``refine_attempted`` / ``refine_failed`` and, on the persona
    path, spins to the wall-clock budget).

    The fix is a steerer whose drift observer's ``handle_drift`` emits the
    ``DriftDetected`` event — the SAME signal zicato's reducer reads to
    attribute custom-judge loss by ``judge_name`` — and then returns,
    skipping the entire cancel / promote / refine machinery. This mirrors
    goldfive's own "emit for observability, skip dispatch" early-return
    pattern (used on its redundant-verdict and concurrent-refine guards).
    ``JudgementEmitted`` is published earlier in
    ``DefaultSteerer.evaluate_judges`` and is therefore unaffected, so the
    paired judgement + custom drift the reducer needs both still land.

    Detection stays fully wired. ``goldfive.wrap`` builds its default
    steerer with the judge call_llm + the runtime's drift configs (see
    its ``resolved_steerer = DefaultSteerer(...)`` branch); passing an
    explicit ``steerer=`` SKIPS that construction, so this factory must
    reproduce the same wiring or the built-in goal-/reasoning-drift
    detectors silently go inert and the scalar changes. We therefore
    thread the adapter's harness ``call_llm`` (which is exactly what
    ``goldfive.wrap`` resolves the judge callable to when the caller
    passes ``call_llm=`` explicitly, as the adapter does) and the
    resolved ``runtime`` into the same constructor kwargs. The ONLY
    behavioural delta from goldfive's default steerer is the neutered
    ``handle_drift`` — detectors fire and emit ``DriftDetected`` exactly
    as before; only the refine ladder is skipped.

    Imported lazily so the optional goldfive dependency stays out of this
    module's import time. The subclass is defined inside the factory so
    the ``DefaultSteerer`` base symbol is resolved at call time.
    """
    from goldfive.steerer import DefaultSteerer  # noqa: PLC0415

    class _JudgeOnlySteerer(DefaultSteerer):
        """``DefaultSteerer`` that observes + judges but never refines.

        Rebinds the bound :class:`~goldfive.drift_observer.DriftObserver`'s
        ``handle_drift`` to a variant that emits ``DriftDetected`` (so the
        scalar reducer still sees the custom-judge-attributed drift) and
        returns before the refine ladder. Everything else — judge
        invocation, ``JudgementEmitted`` emission, sink fan-out, the
        built-in detector wiring — is inherited unchanged.
        """

        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            observer = self.drift

            # ``DriftObserver.handle_drift`` is the single chokepoint every
            # drift (built-in detector OR custom-judge verdict) flows
            # through before the cancel + refine ladder. Replace it on the
            # instance with an observe-only variant. ``_emit_drift_detected``
            # is the canonical ``DriftDetected`` emit the reducer keys on.
            async def _observe_only_handle_drift(drift: Any, session: Any) -> None:
                await observer._emit_drift_detected(session, drift)

            observer.handle_drift = _observe_only_handle_drift  # type: ignore[method-assign]

    # Mirror ``goldfive.wrap``'s default-steerer construction so the
    # built-in goal-/reasoning-drift detectors stay wired identically.
    # Each config field is read defensively: a goldfive RuntimeConfig
    # revision that renames a sub-config must not crash judge-only — a
    # missing field falls back to the steerer's own default.
    kwargs: dict[str, Any] = {}
    if call_llm is not None:
        kwargs["goal_drift_call_llm"] = call_llm
        kwargs["reasoning_drift_call_llm"] = call_llm
    goal_drift = getattr(runtime, "goal_drift", None)
    if goal_drift is not None:
        kwargs["goal_drift_config"] = goal_drift
    tool_loops = getattr(runtime, "tool_loops", None)
    if tool_loops is not None:
        kwargs["tool_loop_config"] = tool_loops
    reasoning_drift = getattr(runtime, "reasoning_drift", None)
    if reasoning_drift is not None:
        kwargs["reasoning_drift_config"] = reasoning_drift
        mode = getattr(reasoning_drift, "mode", None)
        if mode is not None:
            kwargs["reasoning_drift_mode"] = mode
    steering = getattr(runtime, "steering", None)
    if steering is not None:
        kwargs["steering_config"] = steering
    return _JudgeOnlySteerer(**kwargs)


def _judge_only_overrides(agent: Any, call_llm: Any, runtime: Any) -> dict[str, Any]:
    """Build the ``goldfive.run`` kwargs that turn STEERING off, JUDGING on.

    Judge-only evaluation keeps goldfive's drift / process judges armed
    (``reasoning_drift_mode="judge"`` is goldfive's default and is left
    untouched) while removing every steering LLM call AND every refine
    attempt:

    * ``goal_deriver=LiteralGoalDeriver()`` — the entry input becomes the
      goal verbatim, so no ``goal_derive`` LLM call fires (goldfive's
      default :class:`LLMGoalDeriver` would call the LLM here).
    * ``planner=StaticPlanner(<one task>)`` — installs a single static
      task assigned to the root agent so the native agent tree runs on
      goldfive's overlay path (``invoke_passthrough``) and produces a
      transcript to judge. :class:`StaticPlanner.generate` returns that
      fixed plan; its ``refine`` / ``handle_turn`` return ``None``, so no
      replanning LLM call ever fires. (Goldfive's default
      :class:`LLMPlanner` would replan/refine via the LLM.)
      ``PassthroughPlanner`` is deliberately NOT used: its ``generate``
      returns ``None`` and aborts the run with an empty transcript,
      leaving nothing to judge.
    * ``steerer=_build_judge_only_steerer()`` — a :class:`DefaultSteerer`
      subclass whose ``handle_drift`` emits ``DriftDetected`` (preserving
      the custom-judge scoring signal) but skips the cancel + refine
      ladder. WITHOUT this, the ``StaticPlanner``'s ``None`` refine is
      mis-read by goldfive as handler exhaustion → ``refine_attempted`` /
      ``refine_failed`` → ``HUMAN_INTERVENTION_REQUIRED`` escalation,
      which is exactly the ``multi_turn_emulated`` abort spiral this mode
      must avoid. See :func:`_build_judge_only_steerer`.

    Symbols are imported lazily so the optional goldfive dependency stays
    out of this module's import time (matching the lazy-import discipline
    used at the call sites).

    Empirically (goldfive installed in zicato's ``.venv``): with this set
    the only ``goldfive_llm_call_start`` events carry judge names
    (e.g. ``judge_goal_drift``); the steering ``goal_derive`` /
    ``refine`` / ``refine_steer`` call names never appear, and NO
    ``refine_attempted`` event is emitted on any path. The
    ``goal_derived`` / ``plan_revised`` *bookkeeping* events still fire
    once (literal goal recorded; the static plan's "initial plan install"
    revision) but neither is LLM-driven nor a refine.
    """
    from goldfive import StaticPlanner  # noqa: PLC0415
    from goldfive.goal_deriver import LiteralGoalDeriver  # noqa: PLC0415
    from goldfive.types import Plan, Task  # noqa: PLC0415

    one_task_plan = Plan(
        id="zicato-judge-only",
        run_id="",
        goal_ids=("g1",),
        tasks=(
            Task(
                id="t1",
                title="accomplish the user's request",
                description="accomplish the user's request",
                # The overlay's passthrough dispatch routes to the agent
                # whose name matches the task assignee — the root agent.
                assignee_agent_id=agent.name,
            ),
        ),
        edges=(),
        summary="judge-only: run the native agent tree once, no steering",
    )
    return {
        "planner": StaticPlanner(one_task_plan),
        "goal_deriver": LiteralGoalDeriver(),
        "steerer": _build_judge_only_steerer(call_llm, runtime),
    }


# ---------------------------------------------------------------------------
# Concrete RunnableHarness
# ---------------------------------------------------------------------------


class ADKRunnableHarness:
    """An ADK agent loaded under one generation snapshot.

    Constructed by :meth:`ADKHarnessAdapter.load`; not intended for
    direct instantiation. Stateless across :meth:`run` calls — the
    runner constructs a new instance per generation and discards it
    when the generation's board has been executed.

    Conforms to the :class:`~zicato.adapters.base.RunnableHarness`
    Protocol structurally (no inheritance), so the Protocol's
    ``runtime_checkable`` check passes.
    """

    __slots__ = ("_agent", "_mutable_trees")

    def __init__(self, agent: Any, mutable_trees: list[Path]) -> None:
        """Bind a loaded ADK ``agent`` and remember the mutable-tree set.

        ``mutable_trees`` is kept on the runnable purely for diagnostics;
        the runner does not consult it on the runnable, only on the
        adapter that produced this instance.
        """
        self._agent = agent
        self._mutable_trees = list(mutable_trees)

    async def run(
        self,
        entry: BoardEntry,
        sinks: list[Any],
        config: RuntimeConfig,
    ) -> RunResult:
        """Execute ``entry`` against the loaded ADK agent via :mod:`goldfive`.

        Dispatches on ``entry.kind``:

        * ``single_turn`` → :func:`goldfive.run` with ``entry.input`` as
          the user message.
        * ``multi_turn_scripted`` → lazy import :mod:`zicato.board.scripted`
          and delegate (sibling module owned by R2-E).
        * ``multi_turn_emulated`` → lazy import :mod:`zicato.emulator`
          and delegate (sibling module owned by R2-I).

        For ``synthetic_*`` kinds (forward-compat reserved slots) we
        return an aborted :class:`RunResult` with ``abort_reason=
        'unsupported_kind'`` rather than raising — they're not yet
        wired and the runner should report a clean failure for them.

        The entry's :attr:`wall_clock_budget_seconds` is enforced via
        :func:`asyncio.wait_for`; on timeout we return ``RunResult(
        aborted=True, abort_reason='wall_clock_budget')`` rather than
        propagating :class:`asyncio.TimeoutError`.

        Any other exception is caught and surfaced as ``RunResult(
        aborted=True, abort_reason='harness_exception')`` with the
        exception message on :attr:`RunResult.abort_reason` after a
        colon. The runner's outer scope still sees no exception — the
        invariant the runner relies on is "one RunResult per entry,
        always".
        """
        run_id = uuid.uuid4().hex
        budget_s = float(entry.wall_clock_budget_seconds)
        started_at = time.monotonic()

        # Bind the loaded tree's string-model agents to a working model BEFORE
        # any goldfive.run dispatch. Two paths, config-driven first:
        #
        # 1. A configured inner model (config.inner_model — a BaseLlm/LiteLlm
        #    built from a models.harness spec) is injected into every
        #    string-model agent. This is the preferred, idiomatic path: the
        #    agents reach the live endpoint with native tool/function calling
        #    intact, overriding the target's hardcoded default model string.
        # 2. Otherwise, fall back to the guarded call_llm shim rebind: only the
        #    UNRESOLVABLE bare strings (a "gemma-*"/"gemini-*" id that would
        #    resolve to an unused google.genai client and flood the log with
        #    AttributeError('_async_httpx_client') tracebacks on GC) are routed
        #    through the harness call_llm. A string that resolves to a real
        #    LiteLlm is left for ADK so native tool-calling survives; routing it
        #    through the text-only shim would reduce a tool-calling tree to a
        #    single text turn (the presentation target writes no files then).
        #
        # See rebind_tree_models_to_adk_model / rebind_tree_models_to_call_llm /
        # _resolves_to_native_function_calling. Both are idempotent.
        if getattr(config, "inner_model", None) is not None:
            rebind_tree_models_to_adk_model(self._agent, config.inner_model)
        else:
            rebind_tree_models_to_call_llm(self._agent, config.harness_call_llm)

        async def _drive() -> RunResult:
            if entry.kind == "single_turn":
                return await self._run_single_turn(run_id, entry, sinks, config)
            if entry.kind == "multi_turn_scripted":
                return await self._run_multi_turn_scripted(run_id, entry, sinks, config)
            if entry.kind == "multi_turn_emulated":
                return await self._run_multi_turn_emulated(run_id, entry, sinks, config)
            # Reserved forward-compat slots — not wired in v0.
            elapsed_ms = int((time.monotonic() - started_at) * 1000)
            return RunResult(
                run_id=run_id,
                entry_id=entry.id,
                final_output="",
                transcript=(),
                runtime_ms=elapsed_ms,
                aborted=True,
                abort_reason="unsupported_kind",
            )

        try:
            return await asyncio.wait_for(_drive(), timeout=budget_s)
        except TimeoutError:
            elapsed_ms = int((time.monotonic() - started_at) * 1000)
            return RunResult(
                run_id=run_id,
                entry_id=entry.id,
                final_output="",
                transcript=(),
                runtime_ms=elapsed_ms,
                aborted=True,
                abort_reason="wall_clock_budget",
            )
        except Exception as exc:  # noqa: BLE001 — see RunResult invariant in docstring
            elapsed_ms = int((time.monotonic() - started_at) * 1000)
            log.warning(
                "ADKRunnableHarness.run: harness raised %s on entry %r",
                type(exc).__name__,
                entry.id,
            )
            return RunResult(
                run_id=run_id,
                entry_id=entry.id,
                final_output="",
                transcript=(),
                runtime_ms=elapsed_ms,
                aborted=True,
                abort_reason=f"harness_exception:{type(exc).__name__}",
            )

    # ------------------------------------------------------------------
    # Per-kind drivers
    # ------------------------------------------------------------------

    async def _run_single_turn(
        self,
        run_id: str,
        entry: BoardEntry,
        sinks: list[Any],
        config: RuntimeConfig,
    ) -> RunResult:
        """Drive a single ``goldfive.run`` invocation against the agent.

        Forwards :attr:`RuntimeConfig.harness_call_llm` (not the
        auxiliary callable — see the two-callable rule on
        :class:`RuntimeConfig`) and the entry's input. Returns a
        :class:`RunResult` constructed from the outcome's session's
        ``completed_results`` values.

        Judges (goldfive#437) are assembled per entry and passed into
        ``goldfive.run`` via its ``judges=`` parameter: goldfive's
        default built-ins minus any the board's ``disable_drift``
        suppressed, plus the entry's declared
        :class:`~zicato.core.JudgeSpec` judges. Inline judges run on the
        *auxiliary* callable — distinct from the harness callable the
        agent runs on — so a judge cannot trivially collude with the
        tree it grades.
        """
        import goldfive  # lazy: keep the optional dep out of import time

        from zicato.judge_runtime import assemble_judges

        assert entry.input is not None, "single_turn entry must have 'input' (validated upstream)"
        started_at = time.monotonic()
        judges = assemble_judges(
            entry_judges=_entry_judge_specs(entry),
            disable_drift=_entry_disable_drift(entry),
            aux_call_llm=config.effective_judge_call_llm(),
        )
        # Judge-only mode: spread in the no-steering overrides
        # (StaticPlanner + LiteralGoalDeriver) so goldfive judges without
        # deriving goals, replanning, or refining. Judges stay armed in
        # both paths. When off (the default), the call is byte-identical
        # to the legacy steering path.
        gf_runtime = _goldfive_runtime()
        overrides = (
            _judge_only_overrides(self._agent, config.harness_call_llm, gf_runtime)
            if _entry_judge_only(entry)
            else {}
        )
        outcome = await goldfive.run(
            self._agent,
            entry.input,
            sinks=sinks,
            call_llm=config.harness_call_llm,
            judges=judges,
            runtime=gf_runtime,
            **overrides,
        )
        elapsed_ms = int((time.monotonic() - started_at) * 1000)
        transcript = _outcome_transcript(outcome)
        final_output = transcript[-1] if transcript else ""
        return RunResult(
            run_id=run_id,
            entry_id=entry.id,
            final_output=final_output,
            transcript=transcript,
            runtime_ms=elapsed_ms,
        )

    async def _run_multi_turn_scripted(
        self,
        run_id: str,
        entry: BoardEntry,
        sinks: list[Any],
        config: RuntimeConfig,
    ) -> RunResult:
        """Delegate to :mod:`zicato.board.scripted` (lazy import).

        The scripted driver is owned by R2-E and may not exist at the
        time this adapter is built. We import it lazily and surface a
        clean abort if it is missing so the adapter degrades gracefully
        when other modules land out of order.

        The scripted driver expects a ``harness`` object with an
        ``async run(user_message: str)`` interface (see
        :func:`zicato.board.scripted._resolve_invoker`). Passing the
        raw ADK agent would cause :class:`TypeError` because the ADK
        agent's ``.run()`` method does not accept a bare string
        positional argument — it expects ADK-specific invocation
        arguments. We therefore wrap the agent in a thin per-turn
        caller that calls :func:`goldfive.run` with the correct
        signature on each scripted turn.
        """
        import goldfive  # lazy: keep the optional dep out of import time

        from zicato.judge_runtime import assemble_judges

        try:
            from zicato.board import scripted as scripted_driver
        except ImportError:
            return RunResult(
                run_id=run_id,
                entry_id=entry.id,
                final_output="",
                transcript=(),
                runtime_ms=0,
                aborted=True,
                abort_reason="scripted_driver_unavailable",
            )

        judges = assemble_judges(
            entry_judges=_entry_judge_specs(entry),
            disable_drift=_entry_disable_drift(entry),
            aux_call_llm=config.effective_judge_call_llm(),
        )
        agent = self._agent
        gf_runtime = _goldfive_runtime()
        # Judge-only overrides (no-steering, no-refine) applied to every
        # per-turn goldfive.run call; empty dict on the default steering
        # path. Built ONCE (the judge-only steerer is reused across turns,
        # which goldfive explicitly supports — it unwires per-run state at
        # each run boundary).
        overrides = (
            _judge_only_overrides(agent, config.harness_call_llm, gf_runtime)
            if _entry_judge_only(entry)
            else {}
        )

        class _PerTurnCaller:
            """Thin wrapper that calls ``goldfive.run`` per scripted turn.

            The scripted driver calls ``harness.run(user_message)``; this
            wrapper satisfies that interface and dispatches each call to
            ``goldfive.run(agent, user_message, ...)`` with the correct
            ADK-level arguments. The return value is the last user-facing
            assistant reply extracted from the outcome's
            ``completed_results``, matching the same extraction path used
            by :meth:`_run_single_turn`. Returning a plain string lets the
            scripted driver's :func:`~zicato.board.scripted._coerce_reply`
            pass it through without any further unwrapping.
            """

            async def run(self, user_message: str) -> str:
                outcome = await goldfive.run(
                    agent,
                    user_message,
                    sinks=sinks,
                    call_llm=config.harness_call_llm,
                    judges=judges,
                    runtime=gf_runtime,
                    **overrides,
                )
                transcript = _outcome_transcript(outcome)
                return transcript[-1] if transcript else ""

        return await scripted_driver.run_scripted(
            agent=_PerTurnCaller(),
            entry=entry,
            sinks=sinks,
            config=config,
            run_id=run_id,
        )

    async def _run_multi_turn_emulated(
        self,
        run_id: str,
        entry: BoardEntry,
        sinks: list[Any],
        config: RuntimeConfig,
    ) -> RunResult:
        """Delegate to :mod:`zicato.emulator` (lazy import).

        The emulator is owned by R2-I and may not exist at the time
        this adapter is built; same degradation contract as the
        scripted driver above.

        The emulator driver (:func:`zicato.emulator.run_emulated`)
        invokes its ``agent`` argument once per emulated turn as
        ``agent.run(user_message)`` with a bare string. Passing the raw
        ADK agent would raise :class:`TypeError` because the ADK
        agent's ``.run()`` does not accept a bare string positional —
        it expects ADK-specific invocation arguments. This is the same
        bug class #105 fixed for the scripted path; the emulated path
        was explicitly scoped out there and is fixed here by the
        analogous wrapper. We therefore wrap the agent in a thin
        per-turn caller that calls :func:`goldfive.run` with the
        correct signature on each emulated turn — mirroring
        :class:`_PerTurnCaller` in :meth:`_run_multi_turn_scripted`.
        """
        import goldfive  # lazy: keep the optional dep out of import time

        from zicato.judge_runtime import assemble_judges

        try:
            from zicato import emulator
        except ImportError:
            return RunResult(
                run_id=run_id,
                entry_id=entry.id,
                final_output="",
                transcript=(),
                runtime_ms=0,
                aborted=True,
                abort_reason="emulator_unavailable",
            )

        judges = assemble_judges(
            entry_judges=_entry_judge_specs(entry),
            disable_drift=_entry_disable_drift(entry),
            aux_call_llm=config.effective_judge_call_llm(),
        )
        agent = self._agent
        gf_runtime = _goldfive_runtime()
        # Judge-only overrides (no-steering, no-refine) applied to every
        # per-turn goldfive.run call; empty dict on the default steering
        # path. This is the path the picky-stakeholder persona runs on —
        # without the refine-suppressing steerer in these overrides the
        # CRITICAL no_fabricated_numbers judge's verdict was promoted to a
        # drift, the StaticPlanner's None-refine was mis-read as handler
        # exhaustion, and the run escalated to HUMAN_INTERVENTION_REQUIRED
        # and spun to the 900s wall-clock. Built ONCE; the judge-only
        # steerer is reused across turns (goldfive supports a shared
        # steerer — it unwires per-run state at each run boundary).
        overrides = (
            _judge_only_overrides(agent, config.harness_call_llm, gf_runtime)
            if _entry_judge_only(entry)
            else {}
        )

        class _PerTurnCaller:
            """Thin wrapper that calls ``goldfive.run`` per emulated turn.

            The emulator driver calls ``agent.run(user_message)``; this
            wrapper satisfies that interface and dispatches each call to
            ``goldfive.run(agent, user_message, ...)`` with the correct
            ADK-level arguments. The return value is the last
            user-facing assistant reply extracted from the outcome's
            ``completed_results``, matching the extraction path used by
            :meth:`_run_single_turn`. Identical in shape to the
            scripted path's wrapper — the emulator driver and the
            scripted driver both expect an ``async run(str) -> str``.
            """

            async def run(self, user_message: str) -> str:
                outcome = await goldfive.run(
                    agent,
                    user_message,
                    sinks=sinks,
                    call_llm=config.harness_call_llm,
                    judges=judges,
                    runtime=gf_runtime,
                    **overrides,
                )
                transcript = _outcome_transcript(outcome)
                return transcript[-1] if transcript else ""

        return await emulator.run_emulated(
            agent=_PerTurnCaller(),
            entry=entry,
            sinks=sinks,
            config=config,
            run_id=run_id,
        )


# ---------------------------------------------------------------------------
# Concrete HarnessAdapter
# ---------------------------------------------------------------------------


class ADKHarnessAdapter:
    """A :class:`HarnessAdapter` for Google ADK trees driven through goldfive.

    Constructed once per zicato instance and re-used across all
    generations of one epoch. The expensive work (importing
    :mod:`goldfive`, :mod:`google.adk`, the entrypoint module) is
    deferred to :meth:`load`.

    Parameters
    ----------
    entrypoint:
        ``"module.path:agent_symbol"`` string identifying a
        module-level ADK agent. The module is re-imported under each
        generation root; the symbol is fetched via :func:`getattr` and
        passed to :func:`goldfive.run`.
    mutable_trees:
        Optional list of source-tree roots the mutation enumerator
        should walk. When ``None``, defaults to ``[Path(<directory
        containing the entrypoint module>)]`` — the natural single-tree
        case. The default is resolved at construction time on a
        best-effort basis (we tolerate a missing module so adapters
        can be built against modules that only exist post-patch);
        callers wanting a strict construction-time check should pass
        ``mutable_trees`` explicitly.

    The ``mutable_trees`` paths are *absolute* (resolved at construction
    against the registered source). The mutable surface inside a given
    generation snapshot is obtained by :meth:`mutable_subpaths`, which
    re-bases each ``mutable_trees`` entry onto the snapshot root.

    Conforms to :class:`~zicato.adapters.base.HarnessAdapter`
    structurally; the Protocol's ``runtime_checkable`` check passes
    without inheritance.
    """

    name: str = "adk"

    #: Names the inner harness writes run output under, excluded from
    #: every generation copy by :mod:`zicato.epoch.snapshot_scope`.
    #: ``"output"`` is already in the standing artifact set; declaring it
    #: here keeps the adapter contract explicit and lets the directory /
    #: git stores honour an adapter-specific name without a code change.
    run_output_names: tuple[str, ...] = ("output",)

    def __init__(
        self,
        entrypoint: str,
        mutable_trees: list[Path] | None = None,
    ) -> None:
        module_path, symbol = _split_entrypoint(entrypoint)
        self._entrypoint = entrypoint
        self._module_path = module_path
        self._symbol = symbol
        if mutable_trees is None:
            self.mutable_trees = _default_mutable_trees(module_path)
        else:
            self.mutable_trees = [Path(p).resolve() for p in mutable_trees]

    def mutable_subpaths(self, generation_root: Path) -> list[Path]:
        """Re-base the adapter's mutable trees onto a concrete snapshot root.

        Each construction-time ``mutable_trees`` entry is an absolute
        path under the *registered* source. A generation snapshot copies
        every registered tree under its basename, so the mutable surface
        inside ``generation_root`` is ``generation_root / <basename>``
        for each registered tree.

        Returns only the sub-paths that exist under ``generation_root``.
        Falls back to ``[generation_root]`` when the adapter has no
        ``mutable_trees`` declaration at all — the whole snapshot is then
        the surface, matching the pre-narrowing default behaviour.
        """
        root = Path(generation_root).resolve()
        if not self.mutable_trees:
            return [root]
        subpaths: list[Path] = []
        for tree in self.mutable_trees:
            candidate = root / Path(tree).name
            if candidate.exists():
                subpaths.append(candidate)
        # Every registered tree missing from the snapshot is a degenerate
        # case (e.g. an empty snapshot); fall back to the whole root so
        # enumeration still has something to walk.
        return subpaths or [root]

    # ------------------------------------------------------------------
    # HarnessAdapter surface
    # ------------------------------------------------------------------

    def load(self, generation_root: Path) -> RunnableHarness:
        """Load the entrypoint agent from ``generation_root``.

        Puts ``generation_root`` at the front of :data:`sys.path`,
        re-imports the entrypoint module (so a cached parent-generation
        version is replaced with the snapshot version), fetches the
        named symbol, and returns an :class:`ADKRunnableHarness`
        wrapping it.

        We do not restore ``sys.path`` or ``sys.modules`` — see this
        module's docstring on the fresh-process-per-generation
        contract.
        """
        # Lazy import: keep these optional at zicato.adapters import time.
        import goldfive  # noqa: F401 — surface the dep here so missing extra fails clean
        import google.adk  # noqa: F401 — same; google-adk is the ADK extra

        root_str = str(Path(generation_root).resolve())
        if root_str not in sys.path:
            sys.path.insert(0, root_str)

        # Reload semantics: if the module was previously imported from a
        # different root, force a fresh import so the snapshot's version
        # wins. ``importlib.reload`` requires an existing module object;
        # for the first-import case we fall back to ``import_module``.
        if self._module_path in sys.modules:
            module = importlib.reload(sys.modules[self._module_path])
        else:
            module = importlib.import_module(self._module_path)

        try:
            agent = getattr(module, self._symbol)
        except AttributeError as exc:
            raise AttributeError(
                f"ADKHarnessAdapter: entrypoint module {self._module_path!r} "
                f"has no symbol {self._symbol!r} (loaded from "
                f"{getattr(module, '__file__', '<unknown>')!r})"
            ) from exc

        return ADKRunnableHarness(agent=agent, mutable_trees=list(self.mutable_trees))

    def mutation_points(self, source_roots: list[Path] | None = None) -> list[MutationPoint]:
        """Enumerate mutation points across ``source_roots``.

        When ``source_roots`` is ``None``, falls back to
        :attr:`mutable_trees`. Delegates to
        :func:`zicato.mutation.enumerator.enumerate_mutations` — owned
        by a sibling module and imported lazily so this adapter does
        not pull the enumerator's parser at import time.
        """
        roots = source_roots if source_roots is not None else self.mutable_trees
        if not roots:
            return []

        # Lazy import — the enumerator module is owned elsewhere and may
        # not exist yet at adapter import time. Surface a clean error
        # instead of an opaque ImportError so operators know which
        # module is missing.
        #
        # Use importlib.import_module explicitly (rather than a
        # from-import) so test-side monkeypatches of
        # importlib.import_module can intercept this call to simulate
        # the module being absent.
        try:
            enumerator_mod = importlib.import_module("zicato.mutation.enumerator")
            enumerate_mutations = enumerator_mod.enumerate_mutations
        except ImportError as exc:
            raise ImportError(
                "ADKHarnessAdapter.mutation_points requires "
                "zicato.mutation.enumerator.enumerate_mutations; the "
                "mutation enumerator module is not yet available."
            ) from exc

        return _coerce_to_list(enumerate_mutations(roots))


def _coerce_to_list(points: Iterable[MutationPoint]) -> list[MutationPoint]:
    """Normalize the enumerator's return shape to a concrete ``list``.

    The enumerator's signature is "returns an iterable of
    :class:`MutationPoint`"; we materialize once so callers downstream
    can safely iterate twice (e.g. one pass to count, one pass to
    render an audit table).
    """
    if isinstance(points, list):
        return points
    return list(points)


__all__ = [
    "ADKHarnessAdapter",
    "ADKRunnableHarness",
    "rebind_tree_models_to_adk_model",
    "rebind_tree_models_to_call_llm",
]
