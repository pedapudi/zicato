"""Scripted ``call_llm`` for the WS-MERGE LLM-guided recombination OC — NO LLM.

The counterpart to :mod:`.mocks_recombine`: where that fixture drives the
MECHANICAL slot over a TWO-marker policy (two DISJOINT single fixes), this one
drives the ``recombine_merge = "llm"`` slot over a SINGLE-marker policy whose
two single fixes touch the SAME mutation id —

    # zicato:mutable id="style_rules" ...
    STYLE_RULES = "omit-summary;skip-citations"

so the two challengers' patch sets OVERLAP. The mechanical mint REQUIRES a
disjoint pair (selector predicate #7), so on this fixture mechanical mode
selects NOTHING (the control). Only an LLM merge can compose the union — a
single edit that removes BOTH defect tokens, which a blind last-wins
concatenation could never produce.

The arithmetic mirrors the mechanical OC exactly (σ = 0, ``info = 1.0``,
the ``drift:`` channel and ``pass_weight`` both at ``1.0``, 5-entry board):

    scalar(k tokens, p passes) = k + (1 - p/5)

    v0  ("omit-summary;skip-citations", 2 tok, 3/5) = 2.4
    fix A ("skip-citations",            1 tok, 4/5) = 1.2   — Δ 1.2 < 1.5 REJECT
    fix B ("omit-summary",              1 tok, 4/5) = 1.2   — Δ 1.2 < 1.5 REJECT
    the LLM merge ("" , 0 tok, 5/5)                 = 0.0   — Δ 2.4 > 1.5 PROMOTE

Module-level callables, like :mod:`.mocks_recombine` (LOAD-BEARING: the
tournament runner serialises each role callable as a re-importable dotted
path for the subprocess worker). The proposer script serves single fixes in
call order; the MERGE call is recognised by its distinctive user-prompt marker
(the merge prompt's opening sentence) and returns the true union WITHOUT
consuming the sample script — but it DOES advance the total call counter, since
the merge call substitutes the slot's own sample call (the ``n``-call cost
story the OC pins).

Everything is byte-deterministic for the same call sequence. The mocks never
reference any specific model vendor; ``model`` is ignored.
"""

from __future__ import annotations

import json
from typing import Any

from zicato_examples.target_0_convergence.mocks import _dispatch

#: The opening sentence of the LLM merge user prompt
#: (:func:`zicato.proposer.prompts.render_recombine_merge_prompt`). The merge
#: call is the ONLY call whose user prompt carries it, so the mock keys on it
#: to distinguish a merge from an ordinary sample.
MERGE_MARKER = "MERGING two rejected complementary"


def _single_patch_experiment(core_idea: str, why: str, new_content: str, rationale: str) -> str:
    """One schema-valid Experiment payload patching the single ``style_rules`` point."""
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
                "expected_pass_rate_delta": "+0.20",
                "risks": "None — a pure token-list change on one mutation point.",
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


#: Fix A — removes ``omit-summary`` (leaves ``skip-citations``). Touches
#: ``style_rules``.
_FIX_A = _single_patch_experiment(
    core_idea="Drop the omit-summary rule so every note ends with a SUMMARY line.",
    why="The summary predicate fails every run and the token costs one drift frame.",
    new_content="skip-citations",
    rationale="Removing omit-summary restores the SUMMARY line.",
)

#: Fix B — removes ``skip-citations`` (leaves ``omit-summary``). Touches the
#: SAME ``style_rules`` point (OVERLAP with fix A).
_FIX_B = _single_patch_experiment(
    core_idea="Drop the skip-citations rule so every claim carries a source tag.",
    why="The citation predicate fails every run and the token costs one drift frame.",
    new_content="omit-summary",
    rationale="Removing skip-citations restores the [source: ...] tag.",
)

#: The TRUE union the LLM merge composes: a SINGLE edit on ``style_rules`` that
#: removes BOTH defect tokens — exactly what a last-wins concatenation of the
#: two overlapping patches could never produce.
_MERGE_UNION = _single_patch_experiment(
    core_idea="Drop both style rules so every note carries a SUMMARY line and a source tag.",
    why=(
        "Parent A fixed the summary slice and parent B the citation slice; both "
        "targeted the same policy token, so the merged edit clears both defects at once."
    ),
    new_content=" ",
    rationale="Removing both omit-summary and skip-citations restores every feature.",
)

#: The call-ordered sample script (indexing is over SAMPLE calls only). Index 4
#: onward wraps to FIX_A (benign schema-valid repeats).
_SCRIPT: tuple[str, ...] = (_FIX_A, _FIX_A, _FIX_B, _FIX_B, _FIX_A)

_STATE: dict[str, int] = {"calls": 0, "samples": 0}


def reset() -> None:
    """Rewind the proposer script and the call counters."""
    _STATE["calls"] = 0
    _STATE["samples"] = 0


def proposer_calls() -> int:
    """How many PROPOSER calls the script has served — the cost counter.

    Counts BOTH ordinary sample calls AND the LLM merge call (the merge
    substitutes the slot's own sample call, so it costs one proposer call —
    exactly the ``n``-call cost story the OC pins). Non-proposer auxiliary
    sites are dispatched before the counter.
    """
    return _STATE["calls"]


async def aux_llm(system: str, user: str, model: str, **_kwargs: Any) -> str:
    """The scripted single-marker proposer plus the LLM merge, plus other sites."""
    del model
    canned = _dispatch(system, "")
    if canned is not None:
        return canned
    _STATE["calls"] += 1
    if MERGE_MARKER in user:
        # The LLM merge call — recognised by the merge prompt's marker; returns
        # the true union without consuming the sample script.
        return _MERGE_UNION
    idx = _STATE["samples"] % len(_SCRIPT)
    _STATE["samples"] += 1
    return _SCRIPT[idx]


__all__ = ["MERGE_MARKER", "aux_llm", "proposer_calls", "reset"]
