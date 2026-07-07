"""Scripted ``call_llm`` callables for the convergence harness — NO LLM.

Two module-level callables (module-level is LOAD-BEARING: the tournament
runner serialises each role callable as a re-importable dotted path for
the subprocess worker, and a closure-local callable is rejected at spawn
time):

* :func:`harness_llm` — the harness-role placeholder. The deterministic
  policy adapter never calls an LLM, so this is never invoked; it exists
  because the runtime requires two distinct role callables and the
  worker spec must serialise.
* :func:`aux_llm` — the scripted proposer (and every other auxiliary
  call site). Proposer calls are dispatched off a module-level round
  counter; call :func:`reset` before each test / demo run so the script
  starts from round 1 regardless of import-order side effects.

The gauntlet script (one challenger per round, 3 rounds):

1. Remove ``omit-summary``            → strictly better  → MUST promote.
2. ADD ``fabricate-metrics``          → strictly worse   → MUST reject
   (the negative control).
3. Remove ``skip-citations``          → strictly better  → MUST promote
   to the known floor (only ``verbose-prose`` remains).

The racing script (:func:`racing_aux_llm`, ``field_size=4``): four
GENUINELY DISTINCT payloads forming a strict superset chain of defect
token sets, so every board slice orders the arms identically and the
best-known arm (challenger 2, tokens ``{verbose-prose}``) deterministically
survives every rung and clears the champion gate.

Everything is byte-deterministic for the same call sequence. The mocks
never reference any specific model vendor; the ``model`` argument is
accepted and ignored.
"""

from __future__ import annotations

import json
from typing import Any

# ---------------------------------------------------------------------------
# Call-site fingerprints (stable fragments of each system prompt)
# ---------------------------------------------------------------------------

_PROPOSER_FINGERPRINT = "improvement-proposer"
_PROPOSER_FINGERPRINT_FALLBACK = "json object describing one experiment"
_ANALYSIS_FINGERPRINT = "expert reviewer summarizing one epoch"


def _experiment(core_idea: str, why: str, delta: str, new_content: str, rationale: str) -> str:
    """Render one schema-valid Experiment JSON payload for ``style_rules``.

    Every patch targets the single ``style_rules`` mutation point with a
    full ``replace`` — the scripted proposer sets the ABSOLUTE token
    list, so the payload is deterministic regardless of what the parent
    snapshot currently holds.
    """
    return json.dumps(
        {
            "hypothesis": {
                "core_idea": core_idea,
                "modulating": ["style_rules"],
                "why": why,
                "expected_drift_movements": [
                    {
                        "kind": "unexpected_output",
                        "direction": "decrease",
                        "magnitude": "medium",
                    }
                ],
                "expected_pass_rate_delta": delta,
                "risks": "None — the policy edit is a pure token-list change.",
            },
            "patches": [
                {
                    "mutation_id": "style_rules",
                    "op": "replace",
                    "new_content": new_content,
                    "rationale": rationale,
                }
            ],
        }
    )


#: Gauntlet rounds — one payload per evolve round, in order.
_GAUNTLET_ROUNDS: tuple[str, ...] = (
    # Round 1 — remove `omit-summary`. Fixes the SUMMARY: feature (one
    # predicate flips to pass) and drops one drift frame. MUST promote.
    _experiment(
        core_idea="Drop the omit-summary rule so every note ends with a SUMMARY line.",
        why=(
            "The summary predicate fails on every run and the omitted "
            "feature costs one drift frame per run; removing the token "
            "fixes both."
        ),
        delta="+0.20",
        new_content="verbose-prose; skip-citations",
        rationale="Removing omit-summary restores the SUMMARY line and one drift frame.",
    ),
    # Round 2 — the NEGATIVE CONTROL: add `fabricate-metrics`. Introduces
    # a new failing predicate and a new drift frame. MUST reject.
    _experiment(
        core_idea="Add a fabricate-metrics rule so notes carry a headline growth number.",
        why=(
            "A concrete metric claim might read as more authoritative "
            "even without a source for it."
        ),
        delta="+0.05",
        new_content="verbose-prose; skip-citations; fabricate-metrics",
        rationale="A headline metric could make the note feel more concrete.",
    ),
    # Round 3 — remove `skip-citations`. Fixes the citation feature and
    # drops one more drift frame. MUST promote to the known floor
    # (only `verbose-prose` remains).
    _experiment(
        core_idea="Drop the skip-citations rule so every claim carries a source tag.",
        why=(
            "The citation predicate fails on every run and the token "
            "costs one drift frame per run; removing it fixes both."
        ),
        delta="+0.20",
        new_content="verbose-prose",
        rationale="Removing skip-citations restores the [source: ...] tag and one drift frame.",
    ),
)


#: Racing field — four DISTINCT payloads drawn within one round
#: (field_size=4). The token sets form a strict superset chain
#: (c2 ⊂ c1 ⊂ c4 ⊂ c3), so the per-slice scalars are strictly ordered on
#: EVERY board slice and the rung cuts are fully deterministic:
#:   c2 {verbose-prose}                                        → best
#:   c1 {verbose-prose, skip-citations}
#:   c4 {verbose-prose, skip-citations, fabricate-metrics}
#:   c3 {verbose-prose, omit-summary, skip-citations, fabricate-metrics} → worst
_RACING_FIELD: tuple[str, ...] = (
    _experiment(
        core_idea="Drop the omit-summary rule so every note ends with a SUMMARY line.",
        why="The summary predicate fails on every run; removing the token fixes it.",
        delta="+0.20",
        new_content="verbose-prose; skip-citations",
        rationale="Restore the SUMMARY line.",
    ),
    _experiment(
        core_idea="Drop both omit-summary and skip-citations in one policy cleanup.",
        why=(
            "Both features fail their predicates today and each token "
            "costs one drift frame; one cleanup fixes all four signals."
        ),
        delta="+0.40",
        new_content="verbose-prose",
        rationale="Restore the SUMMARY line and the [source: ...] tag together.",
    ),
    _experiment(
        core_idea="Add a fabricate-metrics rule on top of the current policy.",
        why="A concrete headline number might read as more authoritative.",
        delta="+0.05",
        new_content="verbose-prose; omit-summary; skip-citations; fabricate-metrics",
        rationale="Add a headline metric claim to every note.",
    ),
    _experiment(
        core_idea="Drop omit-summary but add fabricate-metrics in the same edit.",
        why="Trade the summary fix against a punchier headline claim.",
        delta="+0.10",
        new_content="verbose-prose; skip-citations; fabricate-metrics",
        rationale="Mixed edit: restore SUMMARY, add a metric claim.",
    ),
)


_AUX_STATE: dict[str, int] = {"gauntlet_round": 0, "racing_round": 0}


def reset() -> None:
    """Rewind both proposer scripts to round 1 / challenger 1.

    Tests (and the RUN.md demo) call this before driving evolve so the
    module-level counters never leak across runs in one interpreter.
    """
    _AUX_STATE["gauntlet_round"] = 0
    _AUX_STATE["racing_round"] = 0


def _next_payload(key: str, script: tuple[str, ...]) -> str:
    """Return the next scripted payload and advance the named counter.

    Wraps around past the end of the script — a benign repeat of an
    earlier (schema-valid) idea, mirroring the target_1 mocks.
    """
    idx = _AUX_STATE[key] % len(script)
    _AUX_STATE[key] += 1
    return script[idx]


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


def _dispatch(system: str, payload: str) -> str | None:
    """Shared non-proposer dispatch for both aux callables.

    Returns the canned response for a recognised non-proposer call site,
    or ``None`` when the system prompt is the proposer's (the caller
    then serves its own script).
    """
    sys_lower = system.lower()
    if _PROPOSER_FINGERPRINT in sys_lower or _PROPOSER_FINGERPRINT_FALLBACK in sys_lower:
        return None
    if _ANALYSIS_FINGERPRINT in sys_lower:
        return _ANALYSIS_NARRATIVE
    # No emulator / judge call sites exist on this board; anything else
    # gets a short parseable acknowledgement.
    del payload
    return "ok"


async def harness_llm(system: str, user: str, model: str, **_kwargs: Any) -> str:
    """The harness-role placeholder. Deterministic; never actually invoked.

    The deterministic policy adapter synthesises its output without any
    LLM call — this callable exists only because the runtime contract
    requires two distinct role callables and the subprocess worker spec
    serialises each one as a module-level dotted path.
    """
    del system, user, model
    return "unused-deterministic-harness"


async def aux_llm(system: str, user: str, model: str, **_kwargs: Any) -> str:
    """The scripted GAUNTLET proposer plus every other auxiliary site.

    Proposer calls serve :data:`_GAUNTLET_ROUNDS` in order (module-level
    counter; see :func:`reset`). Epoch-analysis calls get a canned
    narrative; anything else gets a short acknowledgement.
    """
    del user, model
    canned = _dispatch(system, "")
    if canned is not None:
        return canned
    return _next_payload("gauntlet_round", _GAUNTLET_ROUNDS)


async def racing_aux_llm(system: str, user: str, model: str, **_kwargs: Any) -> str:
    """The scripted RACING proposer (field_size=4) plus other aux sites.

    Proposer calls serve the four distinct :data:`_RACING_FIELD` payloads
    in order — one round's field. Further calls wrap around (benign
    schema-valid repeats).
    """
    del user, model
    canned = _dispatch(system, "")
    if canned is not None:
        return canned
    return _next_payload("racing_round", _RACING_FIELD)


__all__ = ["aux_llm", "harness_llm", "racing_aux_llm", "reset"]
