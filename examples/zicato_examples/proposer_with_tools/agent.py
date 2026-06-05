"""A copy-me tool-using proposer agent (Design A).

This is the shape a real ``proposers/<name>/agent.py`` takes: a native ADK
``LlmAgent`` that declares its OWN ``model=`` and opts into one or two of
zicato's read-only proposer tools
(:data:`zicato.proposer.tools.DEFAULT_PROPOSER_TOOLS`). zicato runs it on
ADK's own ``Runner`` — NOT through the auxiliary text shim — so the agent
can call its tools to read the parent snapshot and the epoch journal while
it reasons, then emit the structured ``{hypothesis, patches}`` JSON the
proposer contract demands.

The agent's INSTRUCTION is static (it is built once and reused across every
round); the per-round task — the brief, skills, mutation manifest,
patterns, loss summary, prior experiments, and the JSON-schema demand — is
delivered by zicato as the agent's run INPUT. So the instruction here only
needs to tell the agent HOW to work (use the tools, then emit JSON), not
WHAT this round's context is.

Two entry points, mirroring the example harness agents:

* :func:`build_agent` — the factory. Pass ``model=`` your proposer model
  (a model string ADK understands, or a built ``BaseLlm``). Tests pass a
  fake model here and bypass disk loading entirely.
* ``agent`` — a module-level instance built lazily on first access with a
  DEFAULT model string. SET ``model=`` to your proposer model — it MUST
  differ from the harness model — before relying on this in production;
  the default string is a placeholder, not a real endpoint.

``google.adk`` is imported lazily inside :func:`build_agent`, so this
module imports cleanly even when the optional ADK extra is not installed.
"""

from __future__ import annotations

from typing import Any

from zicato.proposer.tools import grep_mutable, list_mutation_points, read_journal

#: Placeholder default model string for the module-level ``agent``. SET
#: this to your proposer model (which MUST differ from the harness model)
#: when lifting this file into a real proposer dir. It is deliberately not
#: a real endpoint so an unconfigured copy fails loudly rather than
#: silently colluding with the harness model.
DEFAULT_PROPOSER_MODEL = "set-me-to-your-proposer-model"

_INSTRUCTION = (
    "You are an improvement-proposer for a multi-agent system. The user "
    "message you receive carries the proposer brief, the available "
    "mutation points, the observed patterns, the current loss summary, and "
    "the exact JSON schema your answer must follow.\n\n"
    "Before you answer, USE YOUR TOOLS to ground your proposal:\n"
    "- call `list_mutation_points` to confirm the exact ids you may target;\n"
    "- call `grep_mutable` to inspect how a candidate target is used in the "
    "current generation's source;\n"
    "- call `read_journal` to recall what prior rounds already tried.\n\n"
    "Then emit a SINGLE JSON object matching the schema in the user "
    "message — no prose, no markdown fences. The first character of your "
    "final response MUST be '{' and the last MUST be '}'."
)


def build_agent(*, model: Any = DEFAULT_PROPOSER_MODEL) -> Any:
    """Construct the proposer ``LlmAgent`` bound to ``model``.

    ``model`` is the agent's OWN model — a model string ADK understands or
    a built :class:`~google.adk.models.BaseLlm`. It MUST differ from the
    harness model; zicato runs this agent on ADK's own ``Runner`` with this
    model, so ``--auxiliary-call-llm`` does not govern it. The agent opts
    into a read-only subset of
    :data:`zicato.proposer.tools.DEFAULT_PROPOSER_TOOLS`
    (``list_mutation_points``, ``grep_mutable``, ``read_journal``); zicato
    binds those tools to the round's context for the duration of each run.

    ADK is imported lazily here so the module stays importable without the
    optional ``google-adk`` extra (the import error, if any, surfaces at
    build time, not at module-import time).
    """
    from google.adk.agents import LlmAgent  # noqa: PLC0415

    return LlmAgent(
        name="example_proposer_with_tools",
        model=model,
        instruction=_INSTRUCTION,
        tools=[list_mutation_points, grep_mutable, read_journal],
    )


def __getattr__(name: str) -> Any:
    """Lazily build the module-level ``agent`` on first access.

    Resolves ``...proposer_with_tools.agent.agent`` by calling
    :func:`build_agent` with the placeholder :data:`DEFAULT_PROPOSER_MODEL`.
    Lets the module live in a tree where ``google.adk`` may not be
    importable at write time without breaking module discovery.
    """
    if name == "agent":
        global _AGENT_CACHE
        try:
            return _AGENT_CACHE
        except NameError:
            _AGENT_CACHE = build_agent()
            return _AGENT_CACHE
    raise AttributeError(name)
