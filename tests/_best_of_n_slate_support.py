"""Module-level scripted best-of-N slate proposers for the e2e tests.

The best-of-N tree-integrity e2e tests drive REAL evolve rounds (subprocess
tournament workers) with ``proposer_quality.best_of_n = 3``, so the auxiliary
callable must be a real, importable, module-level object: the tournament
runner serialises each role callable as a re-importable dotted path for the
worker subprocess, and a closure-local callable is rejected at spawn time
(the same worker-boundary rule ``zicato_examples.target_0_convergence.mocks``
documents).

:func:`slate_aux_llm` answers the best-of-N CRITIC call (fingerprint:
"strict reviewer selecting") with candidate index ``0`` — the
FIRST-sampled slate slot, so the chosen candidate is never the
last-validated one, which is the exact tree/selection-mismatch coordinate
under test. Every other auxiliary site gets a short acknowledgement.

What each slate slot PROPOSES is scripted separately, as the policy that
slot writes (:data:`GAUNTLET_POLICIES`, :data:`FIELD_POLICIES`), because
a proposal is an episode rather than an auxiliary call.

Slate design (target_0 planted-defect arithmetic — see
``tests/test_convergence_known_answer.py::_expected_scalar``): within every
slate, slot 0 is the BEST candidate (fewest defect tokens) and slot 2 — the
LAST-validated one, whose tree the pre-fix bug left mounted — is a strictly
WORSE token set including ``fabricate-metrics``. A run that mounts slot 2's
tree while persisting slot 0's experiment is therefore detectable both by
tree content and by the known-answer scalar.
"""

from __future__ import annotations

from typing import Any

#: A stable fragment of the best-of-N critic's system prompt, the one
#: auxiliary call site this module answers with anything but "ok".
_CRITIC_FINGERPRINT = "strict reviewer selecting"


#: Content of the best (slot-0, critic-chosen) gauntlet candidate — leaves
#: only ``verbose-prose`` ⇒ the known floor scalar 1.2 ⇒ MUST promote.
GAUNTLET_CHOSEN_CONTENT = "verbose-prose"
#: Content of the last-sampled (slot-2) candidate — ADDS
#: ``fabricate-metrics`` on top of the seeded tokens ⇒ scalar 4.8 ⇒ would be
#: rejected if its tree were the one mounted (the pre-fix mismatch).
LAST_SLOT_CONTENT = "verbose-prose; omit-summary; skip-citations; fabricate-metrics"

#: Per-challenger chosen (slot-0) content for the two-arm racing field.
FIELD_CHOSEN_CONTENTS = ("verbose-prose", "verbose-prose; skip-citations")

#: The gauntlet slate, as the policy each slot writes. One candidate, three
#: slots: slot 0 is the best, slot 2 the fabricate-metrics decoy.
GAUNTLET_POLICIES: dict[str, dict[str, str]] = {
    "v1#0": {"style_rules": GAUNTLET_CHOSEN_CONTENT},
    "v1#1": {"style_rules": "verbose-prose; skip-citations"},
    "v1#2": {"style_rules": LAST_SLOT_CONTENT},
}

#: The racing-field slates: two challengers x three slots. Slot 0 of each
#: slate is the chosen candidate, and the two chosen policies carry DISTINCT
#: token sets so the field-diversity check keeps both. Slot 2 of each slate
#: is the same fabricate-metrics decoy, so a run that mounts the last
#: validated tree instead of the chosen one mounts identical trees for both
#: arms — which is exactly what the tree-integrity test detects.
FIELD_POLICIES: dict[str, dict[str, str]] = {
    "v1#0": {"style_rules": FIELD_CHOSEN_CONTENTS[0]},
    "v1#1": {"style_rules": "verbose-prose; skip-citations"},
    "v1#2": {"style_rules": LAST_SLOT_CONTENT},
    "v2#0": {"style_rules": FIELD_CHOSEN_CONTENTS[1]},
    "v2#1": {"style_rules": "verbose-prose; omit-summary"},
    "v2#2": {"style_rules": LAST_SLOT_CONTENT},
}


def _dispatch(system: str) -> str:
    """The canned response for one auxiliary call site.

    The scripted critic ALWAYS picks candidate index 0 — the first-sampled
    slot — so the selection never lands on the last-validated candidate,
    which is the coordinate the tree-integrity tests are about.
    """
    if _CRITIC_FINGERPRINT in system.lower():
        return "0"
    return "ok"


async def harness_llm(system: str, user: str, model: str, **_kwargs: Any) -> str:
    """Harness-role placeholder (the deterministic adapter never calls it).

    Exists because the runtime requires two distinct role callables and the
    worker spec serialises each as a module-level dotted path.
    """
    del system, user, model
    return "unused-deterministic-harness"


async def slate_aux_llm(system: str, user: str, model: str, **_kwargs: Any) -> str:
    """The scripted slate critic, plus every other auxiliary call site."""
    del user, model
    return _dispatch(system)


__all__ = [
    "FIELD_CHOSEN_CONTENTS",
    "FIELD_POLICIES",
    "GAUNTLET_CHOSEN_CONTENT",
    "GAUNTLET_POLICIES",
    "LAST_SLOT_CONTENT",
    "harness_llm",
    "slate_aux_llm",
]
