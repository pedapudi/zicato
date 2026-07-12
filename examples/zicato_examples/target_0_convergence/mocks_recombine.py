"""Scripted ``call_llm`` for the WS-REC two-marker recombination OC — NO LLM.

The recombination known-answer (``tests/test_recombination_known_answer.py``)
drives a TWO-MARKER variant of the convergence target: the test writes a
policy carrying two separate mutation points —

    # zicato:mutable id="style_rules" ...
    STYLE_RULES = "omit-summary"
    # zicato:mutable id="style_rules_extra" ...
    STYLE_RULES_EXTRA = "skip-citations"

so the two single-fix challengers touch DISJOINT mutation ids (the
recombination selector's hard disjointness predicate). Each fix alone is a
Δ = 1.2 improvement (one drift frame + one predicate flip); the union is
Δ = 2.4; a contract pinning ``promote_margin`` strictly between (1.5 in
the promote arm, 3.0 in the dedup arm) makes each single fix REJECT while
only the mechanically-minted union can clear the gate.

Module-level callables, like :mod:`.mocks` (LOAD-BEARING: the tournament
runner serialises each role callable as a re-importable dotted path for
the subprocess worker). The proposer script serves, in call order:

    FIX_A, FIX_A, FIX_B, FIX_B, FIX_A, FIX_A, FIX_A, ...

Under ``best_of_n=2`` (critique off) that is: round 1 both slots sample
fix A (reject), round 2 both slots sample fix B (reject), round 3+ every
LLM-sampled slot re-serves fix A — so with recombination ON round 3's
last slot MINTS the union with exactly n−1 = 1 auxiliary propose call
(the cost-neutrality counter reads :func:`proposer_calls`), and with
recombination OFF the same script can only ever re-propose the single
fixes and the champion stalls at v0 (the stall control).

Everything is byte-deterministic for the same call sequence. The mocks
never reference any specific model vendor; ``model`` is ignored.
"""

from __future__ import annotations

import json
from typing import Any

from zicato_examples.target_0_convergence.mocks import _dispatch


def _experiment(
    core_idea: str, why: str, mutation_id: str, new_content: str, rationale: str
) -> str:
    """One schema-valid Experiment payload patching a SINGLE mutation point."""
    return json.dumps(
        {
            "hypothesis": {
                "core_idea": core_idea,
                "modulating": [mutation_id],
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
                    "mutation_id": mutation_id,
                    "op": "replace",
                    "new_content": new_content,
                    "rationale": rationale,
                }
            ],
        }
    )


#: Fix A — empty ``style_rules`` (removes ``omit-summary``; the
#: ``skip-citations`` defect in ``style_rules_extra`` remains).
_FIX_A = _experiment(
    core_idea="Drop the omit-summary rule so every note ends with a SUMMARY line.",
    why=(
        "The summary predicate fails on every run and the token costs one "
        "drift frame per run; removing it fixes both."
    ),
    mutation_id="style_rules",
    new_content=" ",
    rationale="Removing omit-summary restores the SUMMARY line.",
)

#: Fix B — empty ``style_rules_extra`` (removes ``skip-citations``; the
#: ``omit-summary`` defect in ``style_rules`` remains). DISJOINT from A.
_FIX_B = _experiment(
    core_idea="Drop the skip-citations rule so every claim carries a source tag.",
    why=(
        "The citation predicate fails on every run and the token costs one "
        "drift frame per run; removing it fixes both."
    ),
    mutation_id="style_rules_extra",
    new_content=" ",
    rationale="Removing skip-citations restores the [source: ...] tag.",
)

#: The call-ordered script. Index 4 onward wraps to FIX_A (benign
#: schema-valid repeats — the recombination rounds' LLM-sampled slots).
_SCRIPT: tuple[str, ...] = (_FIX_A, _FIX_A, _FIX_B, _FIX_B, _FIX_A)

_STATE: dict[str, int] = {"calls": 0}


def reset() -> None:
    """Rewind the proposer script and the call counter."""
    _STATE["calls"] = 0


def proposer_calls() -> int:
    """How many PROPOSER calls the script has served — the cost counter.

    Only proposer-fingerprint calls count; analysis / other auxiliary call
    sites are dispatched before the counter (a recombination mint makes NO
    call here, which is exactly what the cost-neutrality test measures).
    """
    return _STATE["calls"]


async def aux_llm(system: str, user: str, model: str, **_kwargs: Any) -> str:
    """The scripted two-marker proposer plus every other auxiliary site."""
    del user, model
    canned = _dispatch(system, "")
    if canned is not None:
        return canned
    idx = _STATE["calls"] % len(_SCRIPT)
    _STATE["calls"] += 1
    return _SCRIPT[idx]


__all__ = ["aux_llm", "proposer_calls", "reset"]
