"""Deterministic mock LLM callables for target 2 (goldfive steering).

Target 2's evolve loop drives a real goldfive runtime against a board
that mixes adversarial agents (LoopingAgent, HallucinatingAgent, ...),
clean negative-control agents (CleanAgent), and a tiny ADK
``agent_under_test`` for the normal entries. The loop needs two
``call_llm`` callables, threaded through
:class:`zicato.core.types.RuntimeConfig`:

* :data:`target_llm` — handed to ``goldfive.run`` / ``goldfive.wrap``.
  Goldfive's planner, goal-deriver, and reasoning judges all route
  through it. For NORMAL board entries the small ADK
  :data:`zicato_examples.target_2_goldfive_steering.agent_under_test.agent`
  also calls it via the ADK plugin layer.

* :data:`aux_llm` — used by zicato's evaluation path (judges, the user
  emulator, the closing analysis). Proposals come from the proposal
  runtime, never from this callable.

Both are async ``(system, user, model) -> str`` shaped — the contract
fixed by :data:`zicato.core.types.CallLLM`. Both are deterministic;
the same call sequence always produces the same outputs, so the
smoke-test invocation always lands on the same lineage.

Why deterministic mocks
-----------------------
The point of the smoke test is to exercise wiring end-to-end — that
the manifest bridge, applier, runner, tournament, and analysis layer
all hand each other the right shapes. A real LLM in this slot would
introduce nondeterministic failure modes orthogonal to the wiring
under test. The mocks ship just enough verisimilitude that goldfive's
planner / goal-deriver / judges can do their happy-path thing without
the smoke run depending on unbounded model output.

Where to swap a real LLM in
---------------------------
Point the workspace's ``models`` block at a real engine. The two roles
a round needs are named in ``config.json``::

    "models": {"engines": {
        "target": {"model": "<model id>"},
        "evaluation": {"model": "<model id>"}}, "roles": {}}

An engine naming a ``call_llm`` dotted path instead of a ``model`` is
how these mocks reach the same roles.

The mocks here have no special status — they live under
``examples/`` precisely so they can be lifted into a project tree
verbatim and edited in place.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

# ---------------------------------------------------------------------------
# Harness LLM
# ---------------------------------------------------------------------------


async def target_llm(system: str, user: str, model: str, **_kwargs: Any) -> str:
    """Best-effort canned responses for goldfive's target-role LLM calls.

    Goldfive routes a number of distinct call shapes through this one
    callable. The dispatch order below mirrors the most-specific
    matchers first so a planner/goal-deriver call wins over the
    generic "produce some plausible text" fallback. The exact response
    strings are chosen to satisfy goldfive's structural expectations
    (a planner expects JSON; a goal-deriver expects either JSON or a
    short list of strings; the reasoning judge expects a small JSON
    verdict object).

    Synthesis: every branch ends with a ``return`` so the contract
    "every target call resolves" holds. An unknown call shape falls
    through to a tiny string the ADK agent or the planner can usefully
    consume.

    The ``**_kwargs`` swallow keeps the callable tolerant of forward-
    compatible kwargs goldfive may add upstream.
    """

    _ = model, _kwargs  # not switched on

    sys_lower = system.lower()
    user_lower = user.lower()

    # Goldfive's LLMGoalDeriver asks for an extracted goal list. The
    # canonical shape is ``{"goals": [{"id": "g1", "summary": "..."}]}``.
    if "extract" in sys_lower and "goal" in sys_lower:
        first_line = user.splitlines()[0] if user else ""
        return json.dumps(
            {
                "goals": [
                    {
                        "id": "g1",
                        "summary": (first_line[:160] or "Complete the requested task."),
                    }
                ]
            }
        )

    # Goldfive's LLMPlanner.generate asks for an initial plan. The
    # canonical shape is ``{"summary": "...", "tasks": [...], "edges":
    # [...]}``. One short task is enough to make the executor produce
    # an InvocationResult per board entry.
    if "task-planning" in sys_lower and (
        "initial plan" in sys_lower
        or "complete end-to-end execution plan" in sys_lower
        or "comprehensiveness" in sys_lower
    ):
        return json.dumps(
            {
                "summary": "Mock plan: produce a single-task continuation.",
                "tasks": [
                    {
                        "id": "t1",
                        "title": "complete_request",
                        "description": ("Produce a one-sentence summary that answers the user."),
                    }
                ],
                "edges": [],
            }
        )

    # LLMPlanner.refine — fired when a drift event triggers replanning.
    # Same shape as generate; we hand back a minimal "no-op refine" so
    # the executor proceeds.
    if "refine" in sys_lower or "drift event" in sys_lower:
        return json.dumps(
            {
                "summary": "Mock refine: stay the course.",
                "tasks": [
                    {
                        "id": "t1",
                        "title": "complete_request",
                        "description": "Continue with the original task.",
                    }
                ],
                "edges": [],
            }
        )

    # Reasoning-judge style call. Goldfive accepts a small JSON verdict
    # — verdict in {on_topic, off_topic, justified_deviation} plus a
    # confidence number. We default to on_topic with low confidence so
    # the steerer does not trigger a CRITICAL on normal entries.
    if "reasoning" in sys_lower and "judge" in sys_lower:
        verdict = "on_topic"
        # Trigger off_topic for the wandering-agent reasoning shape.
        if "switch tasks" in user_lower or "real goal should be" in user_lower:
            verdict = "off_topic"
        return json.dumps(
            {
                "verdict": verdict,
                "confidence": 0.35,
                "rationale": "Mock judge: " + verdict,
            }
        )

    # Goal-drift judge. Same JSON shape, with a binary verdict.
    if "goal" in sys_lower and "drift" in sys_lower and "judge" in sys_lower:
        return json.dumps(
            {
                "verdict": "on_goal",
                "confidence": 0.3,
                "rationale": "Mock goal-drift judge: on_goal",
            }
        )

    # Default fallback. The ADK agent's instruction asks for a
    # one-sentence summary; we hand one back so the
    # ``output_mentions_target_token`` predicate ("summary" in output)
    # passes on the normal entries.
    return "Summary: this is a deterministic mock harness response."


# ---------------------------------------------------------------------------
# Evaluation LLM (judge / emulator / analysis)
# ---------------------------------------------------------------------------


def _build_emulator_json() -> str:
    """Canned single-turn emulator response.

    The emulator-side path is not exercised by the smoke board, but
    aux_llm may still be called from a path that probes the emulator.
    We return a tiny JSON envelope that satisfies the most common
    emulator-response shape used downstream.
    """
    return json.dumps(
        {
            "next_user_message": "thanks, that's enough.",
            "should_stop": True,
            "rationale": "Mock emulator: terminating early.",
        }
    )


async def aux_llm(system: str, user: str, model: str, **_kwargs: Any) -> str:
    """Evaluation-LLM mock — emulator first, judge second, fallback last.

    Three dispatch branches:

    * Emulator calls — identified by "next_user_message" /
      "should_stop" hints. Returns a one-shot terminating envelope.
    * JSON judge calls — a passing ``{"pass": true}`` verdict.
    * Analysis / fallback — short JSON-ish placeholder.
      Analysis-pass consumers treat the response as commentary, so a
      stable placeholder is enough to keep the evaluation path moving.

    The ``**_kwargs`` swallow keeps the callable tolerant of forward-
    compatible kwargs.
    """

    _ = model, _kwargs

    sys_lower = system.lower()
    user_lower = user.lower()

    if "next_user_message" in user_lower or "should_stop" in user_lower:
        return _build_emulator_json()

    if "pass" in sys_lower and "reason" in sys_lower and "json" in sys_lower:
        return json.dumps({"pass": True, "reason": "ok (mock)"})

    # Analysis / journal / pattern-summary fallback — short JSON-ish
    # string. None of the analysis-side consumers parse this strictly;
    # they treat the response as commentary.
    return json.dumps(
        {
            "summary": "Mock aux response — analysis path placeholder.",
            "call_id": uuid.uuid4().hex,
        }
    )


__all__ = ["aux_llm", "target_llm"]
