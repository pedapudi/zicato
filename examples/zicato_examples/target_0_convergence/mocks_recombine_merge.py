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

The single fixes are scripted per slate slot (:data:`SLATE_POLICIES`) and
written by the proposal episodes themselves. The MERGE is the one
proposal that is still a single auxiliary call — it composes two patch
sets that already exist rather than investigating a tree — and
:func:`aux_llm` recognises it by the merge prompt's opening sentence and
answers with the true union. The merge SUBSTITUTES the last slot's own
episode, so a merging round runs ``n - 1`` episodes plus one merge call:
the ``n``-call cost story the OC pins.

``aux_llm`` is module-level, which is LOAD-BEARING: the tournament runner
serialises each role callable as a re-importable dotted path for the
subprocess worker. Everything is byte-deterministic; the mocks never
reference any specific model vendor and ``model`` is ignored.
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


#: The two overlapping single fixes, as the policy each slate slot writes.
#: Both touch the SAME mutation point, which is what makes the mechanical
#: selector — whose predicate is disjointness — decline the pair.
#:
#: Under ``best_of_n=2`` (critique off) both of round 1's slots write fix
#: A (removing ``omit-summary``) and both of round 2's write fix B
#: (removing ``skip-citations``). Each single fix is a Δ 1.2 improvement,
#: strictly under the 1.5 margin, so each of those rounds REJECTS; round
#: 3's last slot is where the merge lands.
SLATE_POLICIES: dict[str, dict[str, str]] = {
    "v1#0": {"style_rules": "skip-citations"},
    "v1#1": {"style_rules": "skip-citations"},
    "v2#0": {"style_rules": "omit-summary"},
    "v2#1": {"style_rules": "omit-summary"},
    "v3#0": {"style_rules": "skip-citations"},
    "v3#1": {"style_rules": "skip-citations"},
}


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

_STATE: dict[str, int] = {"merges": 0}


def merge_calls() -> int:
    """How many LLM merge calls this interpreter has answered so far.

    The merge is the one proposal this fixture still makes as a single
    auxiliary call — it composes two patch sets that already exist rather
    than investigating a tree — so it is the one counted here. Proposal
    EPISODES are counted from the workspace's own durable record.

    The count is cumulative for the process, which is what a module-level
    callable can be: the tournament runner reimports this module in each
    worker, so there is no per-run state to key it on. A caller reads it
    either side of the run it is measuring.
    """
    return _STATE["merges"]


async def aux_llm(system: str, user: str, model: str, **_kwargs: Any) -> str:
    """The LLM merge, plus every other auxiliary call site."""
    del model
    if MERGE_MARKER in user:
        _STATE["merges"] += 1
        return _MERGE_UNION
    return _dispatch(system)


__all__ = ["MERGE_MARKER", "SLATE_POLICIES", "aux_llm", "merge_calls"]
