"""Deterministic mock ``call_llm`` callables for the target_1 smoke test.

Two callables are exported:

* :func:`harness_llm` — stands in for the inner harness's LLM. The
  multi-agent presentation tree under :mod:`zicato_examples.target_1_presentation.agent`
  drives this via :mod:`goldfive`; ``harness_llm`` returns canned
  multi-line replies shaped to look like coordinator / researcher /
  writer turns.
* :func:`aux_llm` — stands in for every auxiliary call site (proposer,
  emulator, judge, epoch analysis). The function dispatches on stable
  fragments of the system prompt the call sites use today.

Both callables are byte-deterministic for the same ``(system, user,
model)`` triple. The test harness (``zicato evolve --rounds 2``)
relies on that determinism for reproducible artifacts; CI can re-run
this without any external service.

The signatures accept ``**kwargs`` so they tolerate callers that
forward stray keyword arguments (the goldfive surface and a few
zicato sites add their own).

The mocks NEVER reference any specific model vendor. The ``model``
positional argument is accepted and ignored; the caller is free to
pass anything.
"""

from __future__ import annotations

import json
from typing import Any

# ---------------------------------------------------------------------------
# harness_llm — the inner harness's LLM surface
# ---------------------------------------------------------------------------


_HARNESS_RESPONSES: dict[str, str] = {
    "waffle": (
        "Slide 1: Waffles — A Brief Introduction.\n"
        "Slide 2: A short history of waffles, from medieval Europe to the modern brunch.\n"
        "Slide 3: Belgian vs American waffles.\n"
        "Slide 4: Why waffles still matter.\n"
        "(revised v2 deck per your feedback — incorporated the new structure as requested.)"
    ),
    "transformer": (
        "Slide 1: Transformers — What they are.\n"
        "Slide 2: Attention is the key idea (we'll explain it without ML jargon).\n"
        "Slide 3: Encoder, decoder — what they do for the lay reader.\n"
        "Slide 4: Real-world applications: search, translation, summarization.\n"
        "Slide 5: Closing thoughts on neural language models.\n"
        "(updated deck per your feedback.)"
    ),
    "q3 metrics": (
        "Slide 1: Q3 metrics outline.\n"
        "- Revenue overview for the quarter.\n"
        "- Operating margin movements.\n"
        "- Headcount and hiring funnel.\n"
        "- Customer growth and churn.\n"
        "- Forward look for Q4.\n"
        "Slide 2: Q3 details.\n"
        "(revised per your feedback, with concrete numbers as requested.)"
    ),
    "quarterly metrics": (
        "Slide 1: Quarterly metrics outline for Q3.\n"
        "1. Revenue.\n"
        "2. Operating margin.\n"
        "3. Headcount.\n"
        "4. Customer growth.\n"
        "5. Forward look.\n"
        "(updated v2, incorporating the latest feedback.)"
    ),
    "metrics": (
        "Slide 1: Metrics overview.\n"
        "1. Headline numbers.\n"
        "2. Trend lines.\n"
        "3. Risks and call-outs.\n"
        "(revised per your feedback.)"
    ),
}


_HARNESS_DEFAULT = (
    "Slide 1: Acknowledged. I'll work on that and produce a structured\n"
    "deck with at least three slides.\n"
    "Slide 2: Outline.\n"
    "Slide 3: Closing.\n"
    "(updated as requested.)"
)


#: The mutation surface changes the OUTPUT (issue #84). A baseline researcher
#: instruction lets the writer slip in an uncited, fabricated figure; the
#: proposer's improved instruction (which demands a source citation per claim)
#: replaces it with a cited figure that only reuses numbers the user provided.
#: The ``no_fabricated_numbers`` process judge keys on the ``unverified
#: estimate`` marker, so the two outputs score DIFFERENTLY — the whole point
#: of a contract that can discriminate a challenger from its champion.
_FABRICATED_METRIC = "Also: churn improved to 2.1% this quarter (unverified estimate)."
_CITED_METRIC = "Net revenue retention held at 118% (source: the Q3 figures you provided)."

#: Substrings that mark a researcher instruction as carrying the proposer's
#: quality directive (a citation / compact-bullets demand). The v0 baseline
#: instruction carries none of them; the improved challenger instructions do,
#: so ``system`` — the mutated instruction — now changes the produced text.
_QUALITY_DIRECTIVE_MARKERS: tuple[str, ...] = (
    "citation",
    "one factual claim",
    "compact bullet",
    "under twelve bullets",
    "do not assert a metric",
)


def instruction_demands_citations(system: str) -> bool:
    """Whether the (mutated) instruction carries the proposer's quality directive.

    Public so the deterministic verification test can drive the same
    baseline-vs-improved discrimination the harness applies.
    """
    lowered = system.lower()
    return any(marker in lowered for marker in _QUALITY_DIRECTIVE_MARKERS)


async def harness_llm(system: str, user: str, model: str, **_kwargs: Any) -> str:
    """Return a canned response whose QUALITY depends on the instruction.

    Dispatches on lowercase-substring matches in ``user`` for the base deck,
    then — crucially — reads ``system`` (the agent's instruction, the
    mutation surface) to decide whether the writer slips in a fabricated
    metric or cites a provided figure. Before issue #84 this mock discarded
    ``system`` entirely, so no instruction mutation could change a single byte
    of output and every challenger tied its champion; reading it here is what
    makes a mutation to the surface OBSERVABLE (and therefore scorable).

    Parameters
    ----------
    system, user, model:
        Forwarded by :func:`goldfive.run` and the in-process inner agents.
        ``system`` is the (possibly mutated) agent instruction; ``model`` is
        accepted to satisfy the ``Callable[[str, str, str], Awaitable[str]]``
        contract and ignored.
    _kwargs:
        Swallowed. Callers occasionally pass extras (e.g. a
        ``response_format`` hint); the mock ignores them so the smoke
        test does not break when a new kwarg is added upstream.
    """
    _ = model
    lowered = user.lower()
    base = _HARNESS_DEFAULT
    for needle, response in _HARNESS_RESPONSES.items():
        if needle in lowered:
            base = response
            break
    # The mutation surface decides the factual grounding of the deck.
    tail = _CITED_METRIC if instruction_demands_citations(system) else _FABRICATED_METRIC
    return f"{base}\n{tail}"


# ---------------------------------------------------------------------------
# aux_llm — proposer / judge / emulator / analysis surface
# ---------------------------------------------------------------------------
#
# Each call site is identified by a stable fragment of its system
# prompt. We keep a small round counter in module state so successive
# proposer calls return *different* experiments — the smoke test
# expects two distinct mutations across the two-round run.


_PROPOSER_FINGERPRINT = "improvement-proposer"
_PROPOSER_FINGERPRINT_FALLBACK = "JSON object describing one experiment"
_JUDGE_FINGERPRINT_PASS = "{'pass': bool"
_JUDGE_FINGERPRINT_REASON = "pass"  # broad — judges vary; we narrow below
#: The marker a firing process judge keys on — the ``_FABRICATED_METRIC``
#: tail the baseline (non-citation-demanding) researcher instruction produces.
#: Its absence (the challenger's cited output) is what makes the judge pass.
_JUDGE_VIOLATION_MARKER = "unverified estimate"
_EMULATOR_FINGERPRINT = "You are a simulated user"
_ANALYSIS_FINGERPRINT_HEADLINE = "Headline movements"
_ANALYSIS_FINGERPRINT_REVIEWER = "expert reviewer summarizing one epoch"


# Deterministic per-call proposer responses. The gauntlet smoke test
# executes two rounds (researcher_instruction, then coordinator_instruction);
# a multi-challenger structure (e.g. racing, field_size=4) draws four in one
# round. The four payloads are GENUINELY DISTINCT ideas (two on the
# researcher marker, two on the coordinator marker) so a wide field stays
# diverse under the field-diversity constraint
# (FUNCTIONALITY-RECOMMENDATIONS.md §4.3) rather than collapsing on repeats.
# The list still wraps around so a caller that loops further gets something
# valid (a repeat past the fourth is a benign duplicate).
_PROPOSER_ROUNDS: list[dict[str, Any]] = [
    {
        "hypothesis": {
            "core_idea": (
                "Tighten the researcher's instruction so it produces a "
                "compact bullet-point synthesis instead of long prose."
            ),
            "modulating": ["researcher_instruction"],
            "why": (
                "The current researcher prompt encourages a verbose, "
                "step-by-step synthesis; compact bullets give the writer "
                "a cleaner input and should reduce off-topic drift."
            ),
            "expected_drift_movements": [
                {
                    "kind": "context_pressure",
                    "direction": "decrease",
                    "magnitude": "medium",
                },
                {
                    "kind": "stopped_early",
                    "direction": "neutral",
                    "magnitude": "small",
                },
            ],
            "expected_pass_rate_delta": "+0.05 to +0.10",
            "risks": (
                "Compact bullets may drop nuance the writer relied on; "
                "the writer's slide quality may regress if so."
            ),
        },
        "patches": [
            {
                "mutation_id": "researcher_instruction",
                "op": "replace",
                "new_content": (
                    "You are a researcher. Produce a compact bulleted "
                    "synthesis of the topic the user provides. Each "
                    "bullet is one factual claim suitable for a single "
                    "slide. Keep it under twelve bullets."
                ),
                "rationale": (
                    "Compact bullets reduce context pressure on the "
                    "writer and tighten the topical signal."
                ),
            }
        ],
    },
    {
        "hypothesis": {
            "core_idea": (
                "Sharpen the coordinator's routing instruction so it "
                "stops re-dispatching the reviewer in a loop on "
                "files_not_found cases."
            ),
            "modulating": ["coordinator_instruction"],
            "why": (
                "The current coordinator prompt is long and conflates "
                "two failure modes; a sharper routing flow reduces "
                "agent_transfer churn on the picky-stakeholder entry."
            ),
            "expected_drift_movements": [
                {
                    "kind": "agent_transfer",
                    "direction": "decrease",
                    "magnitude": "medium",
                },
                {
                    "kind": "looping_reasoning",
                    "direction": "decrease_or_neutral",
                    "magnitude": "small",
                },
            ],
            "expected_pass_rate_delta": "+0.02 to +0.08",
            "risks": (
                "An overly terse routing flow may skip the debugger when "
                "it was actually needed; watch the multi-turn entries."
            ),
        },
        "patches": [
            {
                "mutation_id": "coordinator_instruction",
                "op": "replace",
                "new_content": (
                    "You are the Coordinator. Flow: get a topic, route "
                    "to research_agent, then web_developer_agent, then "
                    "reviewer_agent. On critical issues route to "
                    "debugger_agent once and only once. On "
                    "files_not_found, route to debugger_agent for "
                    "find_presentation_files; on found=False re-dispatch "
                    "web_developer_agent with the bare topic. Report to "
                    "the user when done."
                ),
                "rationale": (
                    "Tightening the routing flow reduces redundant "
                    "agent_transfer events and breaks reviewer loops."
                ),
            }
        ],
    },
    # A third, GENUINELY DISTINCT idea on the researcher marker — a different
    # lever (citations) on the same target. Distinct core idea, so the field-
    # diversity constraint (FUNCTIONALITY-RECOMMENDATIONS.md §4.3) keeps it
    # alongside the first researcher idea in a wide (field_size >= 3) field.
    {
        "hypothesis": {
            "core_idea": (
                "Require the researcher to attach a source citation to "
                "every claim so the writer stops inventing unsupported "
                "metrics."
            ),
            "modulating": ["researcher_instruction"],
            "why": (
                "Uncited claims drive the writer to fabricate numbers; "
                "demanding a citation per bullet tightens factual grounding."
            ),
            "expected_drift_movements": [
                {
                    "kind": "context_pressure",
                    "direction": "decrease_or_neutral",
                    "magnitude": "small",
                },
            ],
            "expected_pass_rate_delta": "+0.02 to +0.06",
            "risks": "Strict citation demands may slow the researcher down.",
        },
        "patches": [
            {
                "mutation_id": "researcher_instruction",
                "op": "replace",
                "new_content": (
                    "You are a researcher. Produce a bulleted synthesis "
                    "where EACH bullet is one factual claim followed by a "
                    "short source citation in parentheses. Do not assert a "
                    "metric without a citation."
                ),
                "rationale": (
                    "Per-bullet citations ground the writer's numbers and "
                    "cut fabricated metrics."
                ),
            }
        ],
    },
    # A fourth, GENUINELY DISTINCT idea on the coordinator marker — a budget
    # hint rather than a routing rewrite. Distinct core idea + content, so a
    # field_size==4 round mints four distinct challengers.
    {
        "hypothesis": {
            "core_idea": (
                "Give the coordinator an explicit turn budget so it stops "
                "re-routing on revision turns once the budget is spent."
            ),
            "modulating": ["coordinator_instruction"],
            "why": (
                "Without a budget the coordinator re-dispatches the "
                "reviewer indefinitely; a hard turn cap halts the loop."
            ),
            "expected_drift_movements": [
                {
                    "kind": "looping_reasoning",
                    "direction": "decrease",
                    "magnitude": "medium",
                },
            ],
            "expected_pass_rate_delta": "+0.01 to +0.05",
            "risks": "Too tight a budget may cut a revision the user wanted.",
        },
        "patches": [
            {
                "mutation_id": "coordinator_instruction",
                "op": "replace",
                "new_content": (
                    "You are the Coordinator. You have a budget of six "
                    "routing turns. Flow: topic -> research_agent -> "
                    "web_developer_agent -> reviewer_agent. Route to "
                    "debugger_agent at most once. When the budget is spent, "
                    "report the best result to the user instead of "
                    "re-dispatching."
                ),
                "rationale": (
                    "A hard turn budget halts the reviewer re-dispatch loop " "deterministically."
                ),
            }
        ],
    },
]


_AUX_STATE: dict[str, int] = {"proposer_round": 0}


def _next_proposer_payload() -> dict[str, Any]:
    """Return the next proposer payload and advance the round counter.

    Wraps around when the smoke test executes more rounds than we have
    distinct responses for — the resulting patch is still schema-valid,
    just a repeat of an earlier idea.
    """
    idx = _AUX_STATE["proposer_round"] % len(_PROPOSER_ROUNDS)
    _AUX_STATE["proposer_round"] += 1
    return _PROPOSER_ROUNDS[idx]


_EMULATOR_REPLIES: tuple[str, ...] = (
    "Could you sharpen slide 2 with concrete Q3 numbers?",
    "Please revise the framing of the headline metric and add a " "Q4 outlook bullet.",
    "Looks closer — can you produce a final v2 that addresses my " "previous notes end-to-end?",
)


async def aux_llm(system: str, user: str, model: str, **_kwargs: Any) -> str:
    """Return canned auxiliary responses keyed off the system prompt.

    Dispatch order:

    1. Proposer (system prompt mentions ``improvement-proposer`` or
       the schema preamble) — returns a valid Experiment JSON. The
       returned mutation_id is rotated across rounds so two-round
       smoke tests exercise two different mutation points.
    2. Epoch-analysis reviewer — returns a markdown narrative with the
       required level-2 sections.
    3. Emulator (system prompt mentions ``simulated user``) — returns
       a plausible next-turn user message. Never leaks expected
       answer shape.
    4. Judge (system prompt is short and asks for ``pass`` /
       ``reason``) — returns ``{"pass": true, "reason": "ok"}``.
    5. Default — a short acknowledgement string. Some call sites may
       not match the explicit fingerprints; the default keeps the
       smoke test from failing on an unrecognised prompt shape.

    Parameters
    ----------
    system, user, model:
        Forwarded by the proposer, emulator, judge, and analysis call
        sites. The model is opaque to the mock.
    _kwargs:
        Swallowed for forward-compat (same rationale as
        :func:`harness_llm`).
    """
    _ = model
    sys_lower = system.lower()

    # 1. Proposer.
    if _PROPOSER_FINGERPRINT in sys_lower or _PROPOSER_FINGERPRINT_FALLBACK.lower() in sys_lower:
        payload = _next_proposer_payload()
        return json.dumps(payload)

    # 2. Epoch analysis.
    if (
        _ANALYSIS_FINGERPRINT_REVIEWER in sys_lower
        or _ANALYSIS_FINGERPRINT_HEADLINE.lower() in sys_lower
    ):
        return (
            "## Headline movements\n"
            "Two rounds ran. The first tightened the researcher's "
            "instruction; the second sharpened the coordinator's "
            "routing flow. The picky-stakeholder entry remained the "
            "dominant scalar contributor.\n\n"
            "## Hypotheses that held\n"
            "- Tightening the researcher reduced context pressure on "
            "the writer as predicted.\n\n"
            "## Hypotheses that didn't\n"
            "- The coordinator-routing change is too new to call; "
            "movement on agent_transfer was inside the noise band.\n\n"
            "## Surface still open at epoch close\n"
            "Tool-description spans are untouched; revisiting them is "
            "the natural next focus area.\n\n"
            "## Recommended focus for next epoch\n"
            "Promote the researcher edit, hold the coordinator edit "
            "for another round of data, and queue tool descriptions "
            "for the next experiment batch.\n"
        )

    # 3. Emulator.
    if _EMULATOR_FINGERPRINT in system:
        # Pick a reply based on how many turns are already in the user
        # prompt. The runner sends transcript content under
        # "Conversation so far:"; count AGENT: occurrences to advance.
        agent_turns = user.count("AGENT:")
        idx = min(agent_turns, len(_EMULATOR_REPLIES) - 1)
        return _EMULATOR_REPLIES[idx]

    # 4. Judge (the canonical shape is a short system prompt asking for
    # a JSON {"pass": bool, "reason": str}). We match generously on the
    # judge response shape mentioned in the prompt.
    if _JUDGE_FINGERPRINT_PASS in system or (
        "pass" in sys_lower and "reason" in sys_lower and "json" in sys_lower
    ):
        # A judge with TEETH (issue #84). The declared inline judges
        # (no_fabricated_numbers, incorporates_feedback, audience_appropriate)
        # receive the run's reasoning/output in ``user``. Before the fix this
        # branch unconditionally returned pass=True (a rubber stamp), so a
        # declared judge NEVER emitted a custom:<name> drift and loop-health
        # flagged it "never fired". Now it VIOLATES (pass=False) when the
        # output carries the uncited/fabricated-metric marker the baseline
        # instruction produces — so the judge fires on the champion and passes
        # on the citation-demanding challenger, moving the scalar.
        if _JUDGE_VIOLATION_MARKER in user.lower():
            return json.dumps(
                {"pass": False, "reason": "output asserts an uncited/fabricated metric"}
            )
        return json.dumps({"pass": True, "reason": "ok (mock)"})

    # 5. Fallback. Some call sites might not match — return a short
    # neutral acknowledgement so they at least see a parseable string.
    return "ok"


__all__ = ["aux_llm", "harness_llm"]
