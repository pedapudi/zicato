"""Deterministic mock ``call_llm`` callables for the target_1 smoke test.

Two callables are exported:

* :func:`target_llm` — stands in for the system under test's LLM. The
  multi-agent presentation tree under :mod:`zicato_examples.target_1_presentation.agent`
  drives this via :mod:`goldfive`; ``target_llm`` returns canned
  multi-line replies shaped to look like coordinator / researcher /
  writer turns.
* :func:`aux_llm` — stands in for every evaluation call site (user
  emulator, judge, epoch analysis). The function dispatches on stable
  fragments of the system prompt the call sites use today. Proposals come
  from the proposal runtime, never from this callable.

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
# target_llm — the system under test's LLM surface
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
#: instruction lets the writer slip in an uncited, fabricated figure; an
#: improved instruction that demands a source citation per claim replaces it
#: with a cited figure that only reuses numbers the user provided.
#: The ``no_fabricated_numbers`` process judge keys on the ``unverified
#: estimate`` marker, so the two outputs score DIFFERENTLY — the whole point
#: of a contract that can discriminate a challenger from its champion.
_FABRICATED_METRIC = "Also: churn improved to 2.1% this quarter (unverified estimate)."
_CITED_METRIC = "Net revenue retention held at 118% (source: the Q3 figures you provided)."

#: Substrings that mark a researcher instruction as carrying a quality
#: directive (a citation / compact-bullets demand). The v0 baseline
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
    """Whether the (mutated) instruction carries a quality directive.

    Public so the deterministic verification test can drive the same
    baseline-vs-improved discrimination the harness applies.
    """
    lowered = system.lower()
    return any(marker in lowered for marker in _QUALITY_DIRECTIVE_MARKERS)


#: The research agent's stable self-identification. Every agent instruction in
#: ``agent/agent.py`` opens with a distinct ``You are a …`` clause; only the
#: research agent says ``You are a researcher``. The harness gates the
#: factual-grounding tail on THIS marker so the ``researcher_instruction``
#: mutation is the SOLE lever over the judged fabricated-metric marker
#: (issue #84 A-3): the web_developer / reviewer / coordinator / debugger
#: transcripts never carry the tail, so they cannot mask a researcher-only
#: mutation by emitting the marker themselves. An improved researcher
#: instruction keeps this opener too.
_RESEARCHER_INSTRUCTION_MARKER = "you are a researcher"


def _is_researcher_instruction(system: str) -> bool:
    """Whether ``system`` is the research agent's (mutable) instruction."""
    return _RESEARCHER_INSTRUCTION_MARKER in system.lower()


async def target_llm(system: str, user: str, model: str, **_kwargs: Any) -> str:
    """Return a canned response whose QUALITY depends on the instruction.

    Dispatches on lowercase-substring matches in ``user`` for the base deck,
    then — crucially — reads ``system`` (the agent's instruction, the
    mutation surface). Only the RESEARCHER's output carries the
    factual-grounding tail (a fabricated metric under the baseline
    instruction, a cited figure under a citation-demanding one); every other
    agent returns the base deck untailed. That makes the
    ``researcher_instruction`` mutation the SOLE lever over the judged
    ``no_fabricated_numbers`` marker (issue #84 A-3): a mutation to that one
    surface is OBSERVABLE and scorable, and the web_developer / reviewer /
    coordinator / debugger transcripts can never mask it by emitting the
    marker themselves. Before issue #84 this mock discarded ``system``
    entirely, so no instruction mutation could change a single byte of output
    and every challenger tied its champion.

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
    # Only the researcher's output carries the factual-grounding tail, so the
    # researcher_instruction mutation is the SOLE lever over the judged
    # fabricated-metric marker (issue #84 A-3). Other agents (web_developer /
    # reviewer / coordinator / debugger) return the base deck UNTAILED —
    # otherwise they would emit the fabricated marker regardless of a
    # researcher-only mutation and the discrimination would collapse (the
    # judged transcript would trip ``no_fabricated_numbers`` on the champion
    # AND the citation-demanding challenger alike).
    if not _is_researcher_instruction(system):
        return base
    # The (mutated) researcher instruction decides the factual grounding: a
    # citation-demanding instruction cites a provided figure; the baseline
    # slips in an uncited, fabricated metric.
    tail = _CITED_METRIC if instruction_demands_citations(system) else _FABRICATED_METRIC
    return f"{base}\n{tail}"


# ---------------------------------------------------------------------------
# aux_llm — judge / emulator / analysis surface
# ---------------------------------------------------------------------------
#
# Each call site is identified by a stable fragment of its system prompt.


_JUDGE_FINGERPRINT_PASS = "{'pass': bool"
_JUDGE_FINGERPRINT_REASON = "pass"  # broad — judges vary; we narrow below
#: The marker a firing process judge keys on — the ``_FABRICATED_METRIC``
#: tail the baseline (non-citation-demanding) researcher instruction produces.
#: Its absence (the challenger's cited output) is what makes the judge pass.
_JUDGE_VIOLATION_MARKER = "unverified estimate"
#: Markers that fingerprint the REAL inline-criterion judge system prompt
#: (``zicato.judge_runtime.builder._INLINE_SYSTEM_PROMPT``): it asks a strict
#: reviewer to audit an agent's chain-of-thought against a single quality
#: CRITERION and to answer starting with VIOLATION or OK. Requiring BOTH
#: markers keeps this from colliding with the emulator / analysis /
#: JSON-judge prompts (none of which ask to audit a "criterion" for a
#: "violation"). Recognising THIS protocol — not just the JSON one — is the
#: issue #84 fix that makes the declared inline judges fire through the real
#: judge runtime instead of falling through to the neutral default reply.
_INLINE_JUDGE_MARKERS: tuple[str, ...] = ("violation", "criterion")
_EMULATOR_FINGERPRINT = "You are a simulated user"
_ANALYSIS_FINGERPRINT_HEADLINE = "Headline movements"
_ANALYSIS_FINGERPRINT_REVIEWER = "expert reviewer summarizing one epoch"


_EMULATOR_REPLIES: tuple[str, ...] = (
    "Could you sharpen slide 2 with concrete Q3 numbers?",
    "Please revise the framing of the headline metric and add a " "Q4 outlook bullet.",
    "Looks closer — can you produce a final v2 that addresses my " "previous notes end-to-end?",
)


def _is_inline_judge_prompt(system_lower: str) -> bool:
    """Whether ``system_lower`` is the real inline-criterion judge prompt.

    ``system_lower`` is the already-lowercased judge system prompt. See
    :data:`_INLINE_JUDGE_MARKERS` for why both markers are required.
    """
    return all(marker in system_lower for marker in _INLINE_JUDGE_MARKERS)


async def aux_llm(system: str, user: str, model: str, **_kwargs: Any) -> str:
    """Return canned evaluation responses keyed off the system prompt.

    Dispatch order:

    1. Epoch-analysis reviewer — returns a markdown narrative with the
       required level-2 sections.
    2. Emulator (system prompt mentions ``simulated user``) — returns
       a plausible next-turn user message. Never leaks expected
       answer shape.
    3. Judge with TEETH — answers BOTH judge protocols that reach this
       mock: (a) the REAL inline-criterion judge runtime
       (``_INLINE_SYSTEM_PROMPT``: a one-line ``VIOLATION`` / ``OK``
       reply) that a real ``zicato evolve`` drives, and (b) a JSON
       ``{"pass": bool, "reason": str}`` prompt. It FIRES when the judged
       text in ``user`` carries the fabricated-metric marker and passes
       otherwise (issue #84).
    4. Default — a short acknowledgement string. Some call sites may
       not match the explicit fingerprints; the default keeps the
       smoke test from failing on an unrecognised prompt shape.

    Parameters
    ----------
    system, user, model:
        Forwarded by the emulator, judge, and analysis call sites. The model is opaque to the mock.
    _kwargs:
        Swallowed for forward-compat (same rationale as
        :func:`target_llm`).
    """
    _ = model
    sys_lower = system.lower()

    # 1. Epoch analysis.
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

    # 2. Emulator.
    if _EMULATOR_FINGERPRINT in system:
        # Pick a reply based on how many turns are already in the user
        # prompt. The runner sends transcript content under
        # "Conversation so far:"; count AGENT: occurrences to advance.
        agent_turns = user.count("AGENT:")
        idx = min(agent_turns, len(_EMULATOR_REPLIES) - 1)
        return _EMULATOR_REPLIES[idx]

    # 3. Judge with TEETH (issue #84). The declared process judges
    # (no_fabricated_numbers, incorporates_feedback, audience_appropriate)
    # receive the run's reasoning/output in ``user``; the judge must FIRE when
    # that text carries the uncited/fabricated-metric marker the baseline
    # researcher instruction produces, and pass otherwise. Before the fix this
    # branch was a rubber stamp (always pass) AND — worse — only recognised the
    # JSON prompt, so the REAL inline-criterion judge runtime (which sends
    # ``_INLINE_SYSTEM_PROMPT``, not JSON) fell through to the neutral default
    # below: the declared inline judges NEVER fired on a real run and
    # loop-health flagged them "never fired".
    #
    # TWO judge protocols reach this mock and BOTH must be answered:
    #   (a) the REAL inline-criterion judge runtime
    #       (``zicato.judge_runtime.builder._InlineCriterionJudge``) sends
    #       ``_INLINE_SYSTEM_PROMPT`` — a one-line VIOLATION/OK contract — with
    #       the audited reasoning embedded in ``user``. This is the protocol a
    #       real ``zicato evolve`` drives, so answering it is what makes the
    #       judge fire through the real runtime + reducer + scoring.
    #   (b) a JSON judge prompt asking for ``{"pass": bool, "reason": str}``
    #       (kept for any call site still on that shape — detect BOTH).
    judged_text_violates = _JUDGE_VIOLATION_MARKER in user.lower()

    # (a) Real inline-criterion judge: reply on the VIOLATION/OK contract the
    #     runtime's ``_parse_inline_response`` parser expects (leading token).
    #     Mirrors the ``VIOLATION`` / ``OK`` scripted endpoints the PR's own
    #     ``tests/test_target_0_declared_judges.py`` already drives correctly.
    if _is_inline_judge_prompt(sys_lower):
        if judged_text_violates:
            return "VIOLATION: output asserts an uncited/fabricated metric"
        return "OK the reasoning only uses figures the stakeholder provided"

    # (b) JSON judge protocol.
    if _JUDGE_FINGERPRINT_PASS in system or (
        "pass" in sys_lower and "reason" in sys_lower and "json" in sys_lower
    ):
        if judged_text_violates:
            return json.dumps(
                {"pass": False, "reason": "output asserts an uncited/fabricated metric"}
            )
        return json.dumps({"pass": True, "reason": "ok (mock)"})

    # 4. Fallback. Some call sites might not match — return a short
    # neutral acknowledgement so they at least see a parseable string.
    return "ok"


__all__ = ["aux_llm", "target_llm"]
