"""Deterministic mock LLM callables for target 2 (goldfive steering).

Target 2's evolve loop drives a real goldfive runtime against a board
that mixes adversarial agents (LoopingAgent, HallucinatingAgent, ...),
clean negative-control agents (CleanAgent), and a tiny ADK
``agent_under_test`` for the normal entries. The loop needs two
``call_llm`` callables, threaded through
:class:`zicato.core.types.RuntimeConfig`:

* :data:`harness_llm` — handed to ``goldfive.run`` / ``goldfive.wrap``.
  Goldfive's planner, goal-deriver, and reasoning judges all route
  through it. For NORMAL board entries the small ADK
  :data:`zicato_examples.target_2_goldfive_steering.agent_under_test.agent`
  also calls it via the ADK plugin layer.

* :data:`aux_llm` — used by zicato's auxiliary path (the proposer,
  pattern-summary judge, emulator). The proposer call is what
  produces the structured ``{hypothesis, patches}`` payload that
  drives a round.

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
Replace the import path on the command line::

    --harness-call-llm my_project.llms:harness_call_llm
    --auxiliary-call-llm my_project.llms:aux_call_llm

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


async def harness_llm(system: str, user: str, model: str, **_kwargs: Any) -> str:
    """Best-effort canned responses for goldfive's harness-LLM calls.

    Goldfive routes a number of distinct call shapes through this one
    callable. The dispatch order below mirrors the most-specific
    matchers first so a planner/goal-deriver call wins over the
    generic "produce some plausible text" fallback. The exact response
    strings are chosen to satisfy goldfive's structural expectations
    (a planner expects JSON; a goal-deriver expects either JSON or a
    short list of strings; the reasoning judge expects a small JSON
    verdict object).

    Synthesis: every branch ends with a ``return`` so the contract
    "every harness call resolves" holds. An unknown call shape falls
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
# Auxiliary LLM (proposer / judge / emulator / analysis)
# ---------------------------------------------------------------------------


# Preferred goldfive mutation ids the proposer rotates through across
# rounds. Each is a real id declared in
# ``goldfive/optimization/manifest.toml``; the bridge in
# :mod:`zicato.synthetic.manifest_bridge` exposes them as
# :class:`MutationPoint` records so the proposer's hypothesis + patches
# land cleanly. Two prompt mutations are listed so the round-rotation
# produces distinguishable v1 + v2 patches without resorting to
# numeric ops (which the smoke applier does not wire end-to-end against
# manifest-bridged points yet).
_PROPOSER_TARGETS: tuple[tuple[str, str, str, str], ...] = (
    (
        "refine_system_prompt",
        "Tighten the refine system prompt to push the planner toward "
        "single-task continuations on adversarial entries.",
        "refine_validation_failed",
        "decrease_or_neutral",
    ),
    (
        "reasoning_judge_system_prompt",
        "Sharpen the reasoning-judge system prompt so the off-topic / "
        "justified-deviation boundary moves toward higher recall.",
        "off_topic",
        "increase_or_neutral",
    ),
)


# Canned replacement bodies. Real-world these would be thoughtful
# rewrites; for the smoke test they are short variants that still parse
# as system prompts and are distinguishable from the baseline.
_REFINE_NEW_CONTENT = (
    "You are a task-planning assistant maintaining an ACTIVE plan for a "
    "multi-agent system. Keep refinements minimal: prefer a single-task "
    "continuation when the drift event is recoverable. Emit the same "
    "JSON plan shape the upstream prompt declared (tasks array with id "
    "/ title / description / agent / depends_on)."
)

_REASONING_JUDGE_NEW_CONTENT = (
    "You are assessing whether an autonomous agent's chain-of-thought "
    "is still aligned with the bound task. Return one of {on_topic, "
    "off_topic, justified_deviation} with a confidence in [0, 1]. "
    "Default to on_topic when the reasoning continues to reference the "
    "task title or its sub-goals; flip to off_topic only when the "
    "reasoning explicitly proposes switching tasks."
)


# Track call count so we can rotate targets across rounds without
# threading state through the orchestrator. Module-level state is fine
# here — the mocks are intentionally not re-entrant; tests construct a
# fresh process per smoke run.
_PROPOSER_CALL_INDEX = 0


def _build_experiment_json(round_index: int) -> str:
    """Build a structured proposer response targeting goldfive mutation ids.

    The hypothesis JSON shape matches
    :data:`zicato.proposer.structured.EXPERIMENT_JSON_SCHEMA`. The
    ``mutation_id`` always names a real entry from goldfive's
    optimization manifest so the orchestrator's cross-check against the
    live mutation manifest (`orchestrator.evolve_once` step 7) passes.
    """

    target_id, core_idea, drift_kind, direction = _PROPOSER_TARGETS[
        round_index % len(_PROPOSER_TARGETS)
    ]
    new_content = (
        _REFINE_NEW_CONTENT if target_id == "refine_system_prompt" else _REASONING_JUDGE_NEW_CONTENT
    )
    payload: dict[str, Any] = {
        "hypothesis": {
            "core_idea": core_idea,
            "modulating": [target_id],
            "why": (
                "Round-rotation smoke proposer: targeting "
                f"{target_id} so the applier exercises a manifest-bridged "
                "prompt rewrite end-to-end. Real proposer rounds will read "
                "pattern detector output and choose a substantive edit."
            ),
            "expected_drift_movements": [
                {
                    "kind": drift_kind,
                    "direction": direction,
                    "magnitude": "small",
                }
            ],
            "expected_pass_rate_delta": "+0.00 to +0.05",
            "risks": (
                "Mock-driven; a real round may regress recall on the "
                "adversarial board if the rewrite weakens the steerer."
            ),
        },
        "patches": [
            {
                "mutation_id": target_id,
                "op": "replace",
                "new_content": new_content,
                "rationale": (
                    "Smoke-test rewrite. Body is bland on purpose — the "
                    "applier's job here is to land the diff, not to "
                    "produce a substantively better prompt."
                ),
            }
        ],
    }
    return json.dumps(payload)


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
    """Auxiliary-LLM mock — proposer first, emulator second, fallback last.

    Three dispatch branches:

    * Proposer calls — identified by the structured-proposer system
      prompt's "hypothesis" / "patches" fingerprints. Returns a
      schema-valid ``{hypothesis, patches}`` payload with a real
      goldfive mutation id.
    * Emulator calls — identified by "next_user_message" /
      "should_stop" hints. Returns a one-shot terminating envelope.
    * Analysis / judge / fallback — short JSON-ish placeholder.
      Analysis-pass consumers treat the response as commentary, so a
      stable placeholder is enough to keep the auxiliary path moving.

    The ``**_kwargs`` swallow keeps the callable tolerant of forward-
    compatible kwargs.
    """

    global _PROPOSER_CALL_INDEX
    _ = model, _kwargs

    sys_lower = system.lower()
    user_lower = user.lower()

    if "hypothesis" in sys_lower and "patches" in sys_lower:
        payload = _build_experiment_json(_PROPOSER_CALL_INDEX)
        _PROPOSER_CALL_INDEX += 1
        return payload

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


__all__ = ["aux_llm", "harness_llm"]
