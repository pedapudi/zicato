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

What the proposer writes is scripted per slate slot: with recombination
ON, round 3's last slot MINTS the union instead of running an episode, so
the round spends n−1 = 1 proposal episode rather than n (the
cost-neutrality measurement counts the episodes the workspace recorded);
with recombination OFF the same script can only ever re-propose the
single fixes and the champion stalls at v0 (the stall control).

Everything is byte-deterministic. The mocks never reference any specific
model vendor.
"""

from __future__ import annotations

#: The two disjoint single fixes, as the policy each slate slot writes.
#:
#: Under ``best_of_n=2`` (critique off) both of round 1's slots write fix
#: A and both of round 2's write fix B, so each round fields one single
#: fix and each single fix REJECTS against the pinned margin. From round 3
#: every sampled slot writes fix A again, which is what leaves the
#: mechanically-minted union as the only thing that can clear the gate —
#: and, with recombination off, leaves the champion at v0.
#:
#: A policy of ``" "`` empties the point, which removes the defect token
#: it holds. ``FIX_A`` empties ``style_rules`` (dropping ``omit-summary``)
#: and ``FIX_B`` empties ``style_rules_extra`` (dropping
#: ``skip-citations``); the two touch DISJOINT mutation ids, which is the
#: recombination selector's hard predicate.
FIX_A: dict[str, str] = {"style_rules": " "}
FIX_B: dict[str, str] = {"style_rules_extra": " "}

#: Which fix each slate slot of each round writes. Rounds past the third
#: are not reached by the harness; a candidate with no entry writes the
#: mechanical tag, which no arm of these tests depends on.
SLATE_POLICIES: dict[str, dict[str, str]] = {
    "v1#0": FIX_A,
    "v1#1": FIX_A,
    "v2#0": FIX_B,
    "v2#1": FIX_B,
    "v3#0": FIX_A,
    "v3#1": FIX_A,
}


__all__ = ["FIX_A", "FIX_B", "SLATE_POLICIES"]
