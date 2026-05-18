"""Minimal LlmAgent used as the agent-under-test for target 2 normal entries.

Target 2 optimizes goldfive's own steering layer; the agent goldfive is
steering is a workload, not the optimization target. This file ships a
deliberately small LlmAgent so the "normal" board entries have something
concrete to wrap without the agent's own quality dominating the signal.

Design notes:

* Two simple tools (``compute_word_count``, ``echo_summary``) give the
  steerer something to observe — tool calls, reasoning traces, an output
  shape — without introducing failure modes of their own. They are
  side-effect-free pure functions wrapped as ADK tools.
* The instruction string is intentionally bland. Improvements to it are
  out of scope for target 2; the proposer is directed at goldfive's
  judge prompts and threshold knobs, not at this agent's prompt (see
  ``rubric.md``).
* Importing this module requires ``google.adk`` to be installed. If the
  user is scaffolding the example before installing the ADK, the file
  still parses (the imports execute lazily inside ``build_agent``) so
  ``zicato_examples.target_2_goldfive_steering`` remains importable for tests
  that do not actually run the agent.

Exports
-------
``agent``
    Module-level :class:`google.adk.agents.LlmAgent` instance, built
    lazily on first attribute access. The runner imports
    ``zicato_examples.target_2_goldfive_steering.agent_under_test:agent`` as
    its ADK entrypoint when executing this directory's normal board
    entries.
"""

from __future__ import annotations

from typing import Any


def compute_word_count(text: str) -> int:
    """Return the number of whitespace-separated tokens in ``text``.

    A trivial tool the agent can call to demonstrate tool use without
    introducing any failure modes. The implementation is a one-liner
    on purpose — there is nothing here to optimize, which is the
    point: the steering layer is what we are tuning, not the workload.
    """

    return len(text.split())


def echo_summary(summary: str) -> str:
    """Return ``summary`` unchanged, prefixed with ``"Summary: "``.

    Useful for the normal correctness predicate
    (:func:`predicates.output_mentions_target_token`) which checks
    that the agent's final output mentions the word "summary". The tool
    nudges the agent toward producing a recognizable summary shape
    without prescribing content.
    """

    return f"Summary: {summary}"


_INSTRUCTION = (
    "You are a small research-assistant agent. When the user gives you "
    "a topic or a short text, call `compute_word_count` if word-count "
    "is relevant, then call `echo_summary` with a one-sentence summary "
    "of the topic or text. Return only the summary; do not add commentary."
)


def build_agent() -> Any:
    """Construct the module-level LlmAgent lazily.

    Kept as a function so the file imports cleanly even when
    ``google.adk`` is not installed (the import error surfaces at
    agent-build time, not at module-import time). The runner calls
    this exactly once per process via the ``agent`` module attribute.
    """

    from google.adk.agents import LlmAgent  # noqa: PLC0415

    return LlmAgent(
        name="target_2_normal_agent_under_test",
        instruction=_INSTRUCTION,
        tools=[compute_word_count, echo_summary],
    )


def __getattr__(name: str) -> Any:
    """Lazy module-level ``agent`` attribute.

    Resolves ``zicato_examples.target_2_goldfive_steering.agent_under_test.agent``
    by calling :func:`build_agent` on first access. Subsequent accesses
    return the cached instance. Lets the scaffolding live in a tree
    where ``google.adk`` may or may not be importable at write time
    without breaking module discovery (e.g. test collection).
    """

    if name == "agent":
        global _AGENT_CACHE
        try:
            return _AGENT_CACHE
        except NameError:
            _AGENT_CACHE = build_agent()
            return _AGENT_CACHE
    raise AttributeError(name)
