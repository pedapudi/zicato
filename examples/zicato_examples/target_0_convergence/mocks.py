"""Scripted ``call_llm`` callables for the convergence harness — NO LLM.

Two module-level callables (module-level is LOAD-BEARING: the tournament
runner serialises each role callable as a re-importable dotted path for
the subprocess worker, and a closure-local callable is rejected at spawn
time):

* :func:`target_llm` — the target-role placeholder. The deterministic
  policy adapter never calls an LLM, so this is never invoked; it exists
  because the runtime requires two distinct role callables and the
  worker spec must serialise.
* :func:`aux_llm` — every evaluation call site: the epoch-analysis
  narrative, and a short acknowledgement for anything else.

The proposer is not an evaluation call. It runs as its own episode, and
what it writes is scripted here as a policy per candidate —
:data:`GAUNTLET_POLICIES` for the three-round gauntlet and
:data:`RACING_POLICIES` for the four-candidate racing field — which a
harness hands to the stand-in that drives those episodes. Each policy is
the ABSOLUTE token list, so a candidate's tree is decided by its own
entry and not by the order the rounds ran in.

Everything is byte-deterministic. The mocks never reference any specific
model vendor; the ``model`` argument is accepted and ignored.
"""

from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# Call-site fingerprint (a stable fragment of the analysis system prompt)
# ---------------------------------------------------------------------------

_ANALYSIS_FINGERPRINT = "expert reviewer summarizing one epoch"


#: The gauntlet script, as the policy each candidate writes — one
#: candidate per round, in order.
#:
#: 1. ``v1`` removes ``omit-summary``: fixes the SUMMARY: feature (one
#:    predicate flips to pass) and drops one drift frame. MUST promote.
#: 2. ``v2`` is the NEGATIVE CONTROL, adding ``fabricate-metrics``: a new
#:    failing predicate and a new drift frame. MUST reject.
#: 3. ``v3`` removes ``skip-citations``: fixes the citation feature and
#:    drops one more drift frame. MUST promote to the known floor, with
#:    only ``verbose-prose`` left.
#:
#: The policy is ABSOLUTE rather than a delta, so each candidate's tree is
#: the same whatever its parent held.
GAUNTLET_POLICIES: dict[str, dict[str, str]] = {
    "v1": {"style_rules": "verbose-prose; skip-citations"},
    "v2": {"style_rules": "verbose-prose; skip-citations; fabricate-metrics"},
    "v3": {"style_rules": "verbose-prose"},
}

#: The racing field — four DISTINCT policies drawn within one round
#: (``field_size=4``). The token sets form a strict superset chain
#: (v2 ⊂ v1 ⊂ v4 ⊂ v3), so the per-slice scalars are strictly ordered on
#: EVERY board slice and the rung cuts are fully deterministic:
#:   v2 {verbose-prose}                                        → best
#:   v1 {verbose-prose, skip-citations}
#:   v4 {verbose-prose, skip-citations, fabricate-metrics}
#:   v3 {verbose-prose, omit-summary, skip-citations, fabricate-metrics} → worst
RACING_POLICIES: dict[str, dict[str, str]] = {
    "v1": {"style_rules": "verbose-prose; skip-citations"},
    "v2": {"style_rules": "verbose-prose"},
    "v3": {"style_rules": "verbose-prose; omit-summary; skip-citations; fabricate-metrics"},
    "v4": {"style_rules": "verbose-prose; skip-citations; fabricate-metrics"},
}


_ANALYSIS_NARRATIVE = (
    "## Headline movements\n"
    "The scripted rounds removed two planted defect tokens and rejected "
    "the negative-control token; the scalar dropped to the known floor.\n\n"
    "## Hypotheses that held\n"
    "- Removing a defect token fixed exactly one predicate and one drift "
    "frame per run, as designed.\n\n"
    "## Hypotheses that didn't\n"
    "- The fabricate-metrics idea regressed the board and was rejected, "
    "as designed.\n\n"
    "## Surface still open at epoch close\n"
    "The verbose-prose token remains — the known floor is not zero.\n\n"
    "## Recommended focus for next epoch\n"
    "Remove verbose-prose to converge the remaining conciseness entry.\n"
)


def _dispatch(system: str) -> str:
    """The canned response for one evaluation call site."""
    if _ANALYSIS_FINGERPRINT in system.lower():
        return _ANALYSIS_NARRATIVE
    # No emulator / judge call sites exist on this board; anything else
    # gets a short parseable acknowledgement.
    return "ok"


async def target_llm(system: str, user: str, model: str, **_kwargs: Any) -> str:
    """The target-role placeholder. Deterministic; never actually invoked.

    The deterministic policy adapter synthesises its output without any
    LLM call — this callable exists only because the runtime contract
    requires two distinct role callables and the subprocess worker spec
    serialises each one as a module-level dotted path.
    """
    del system, user, model
    return "unused-deterministic-harness"


async def aux_llm(system: str, user: str, model: str, **_kwargs: Any) -> str:
    """Every evaluation call site this harness has.

    Epoch-analysis calls get a canned narrative; anything else gets a
    short acknowledgement. The proposer is not among them: it runs as its
    own episode, writing the policies :data:`GAUNTLET_POLICIES` and
    :data:`RACING_POLICIES` name.
    """
    del user, model
    return _dispatch(system)


__all__ = ["GAUNTLET_POLICIES", "RACING_POLICIES", "aux_llm", "target_llm"]
