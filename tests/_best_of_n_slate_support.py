"""Module-level scripted best-of-N slate proposers for the e2e tests.

The best-of-N tree-integrity e2e tests drive REAL evolve rounds (subprocess
tournament workers) with ``proposer_quality.best_of_n = 3``, so the auxiliary
callable must be a real, importable, module-level object: the tournament
runner serialises each role callable as a re-importable dotted path for the
worker subprocess, and a closure-local callable is rejected at spawn time
(the same worker-boundary rule ``zicato_examples.target_0_convergence.mocks``
documents).

Each callable dispatches on the SYSTEM prompt:

* the best-of-N CRITIC call (fingerprint: "strict reviewer selecting") is
  scripted to pick candidate index ``0`` — the FIRST-sampled slate slot, so
  the chosen candidate is never the last-validated one (the exact tree/
  selection-mismatch coordinate under test);
* PROPOSER calls (the target_0 fingerprints) serve the slate payloads in
  sample order off a module-level counter;
* anything else gets a short parseable acknowledgement.

Slate design (target_0 planted-defect arithmetic — see
``tests/test_convergence_known_answer.py::_expected_scalar``): within every
slate, slot 0 is the BEST candidate (fewest defect tokens) and slot 2 — the
LAST-validated one, whose tree the pre-fix bug left mounted — is a strictly
WORSE token set including ``fabricate-metrics``. A run that mounts slot 2's
tree while persisting slot 0's experiment is therefore detectable both by
tree content and by the known-answer scalar.

Call :func:`reset` before each test so the module-level counters never leak
across runs in one interpreter.
"""

from __future__ import annotations

import json
from typing import Any

#: Fingerprints of the two call sites this mock must tell apart. The critic
#: check runs FIRST: the critic's system prompt talks about "improvement
#: proposal(s)" too, so the proposer fingerprints alone cannot separate them.
_CRITIC_FINGERPRINT = "strict reviewer selecting"
_PROPOSER_FINGERPRINT = "improvement-proposer"
_PROPOSER_FINGERPRINT_FALLBACK = "json object describing one experiment"


def _experiment(core_idea: str, new_content: str) -> str:
    """One schema-valid Experiment JSON payload targeting ``style_rules``."""
    return json.dumps(
        {
            "hypothesis": {
                "core_idea": core_idea,
                "modulating": ["style_rules"],
                "why": "Each remaining defect token costs one predicate and one drift frame.",
                "expected_drift_movements": [
                    {
                        "kind": "unexpected_output",
                        "direction": "decrease",
                        "magnitude": "medium",
                    }
                ],
                "expected_pass_rate_delta": "+0.20",
                "risks": "None — the policy edit is a pure token-list change.",
            },
            "patches": [
                {
                    "mutation_id": "style_rules",
                    "op": "replace",
                    "new_content": new_content,
                    "rationale": f"Set the token list to: {new_content or '(empty)'}",
                }
            ],
        }
    )


#: Content of the best (slot-0, critic-chosen) gauntlet candidate — leaves
#: only ``verbose-prose`` ⇒ the known floor scalar 1.2 ⇒ MUST promote.
GAUNTLET_CHOSEN_CONTENT = "verbose-prose"
#: Content of the last-sampled (slot-2) gauntlet candidate — ADDS
#: ``fabricate-metrics`` on top of the seeded tokens ⇒ scalar 4.8 ⇒ would be
#: rejected if its tree were the one mounted (the pre-fix mismatch).
GAUNTLET_LAST_CONTENT = "verbose-prose; omit-summary; skip-citations; fabricate-metrics"

#: The gauntlet slate, in sample order (slot 0 first).
GAUNTLET_SLATE: tuple[str, ...] = (
    _experiment(
        "Drop both omit-summary and skip-citations in one policy cleanup.",
        GAUNTLET_CHOSEN_CONTENT,
    ),
    _experiment(
        "Drop the omit-summary rule so every note ends with a SUMMARY line.",
        "verbose-prose; skip-citations",
    ),
    _experiment(
        "Add a fabricate-metrics rule on top of the seeded policy.",
        GAUNTLET_LAST_CONTENT,
    ),
)

#: Per-challenger chosen (slot-0) content for the two-arm racing field.
FIELD_CHOSEN_CONTENTS = ("verbose-prose", "verbose-prose; skip-citations")

#: The racing-field slates: field_size=2 challengers x best_of_n=3 samples,
#: served strictly in propose order (challenger 1's slate, then challenger
#: 2's). Slot 0 of each slate is the chosen candidate; the two chosen
#: hypotheses carry DISTINCT core ideas + token sets so the field-diversity
#: check keeps both. Slot 2 of each slate is the same fabricate-metrics
#: decoy, so a pre-fix run mounts the WRONG (identical) trees for both arms.
FIELD_SLATES: tuple[str, ...] = (
    # Challenger 1 — chosen: only verbose-prose left (the known floor, 1.2).
    _experiment(
        "Drop both omit-summary and skip-citations in one policy cleanup.",
        FIELD_CHOSEN_CONTENTS[0],
    ),
    _experiment(
        "Drop the omit-summary rule so every note ends with a SUMMARY line.",
        "verbose-prose; skip-citations",
    ),
    _experiment(
        "Add a fabricate-metrics rule on top of the seeded policy.",
        GAUNTLET_LAST_CONTENT,
    ),
    # Challenger 2 — chosen: remove omit-summary only (scalar 2.4).
    _experiment(
        "Drop only the omit-summary rule and keep the citation behaviour.",
        FIELD_CHOSEN_CONTENTS[1],
    ),
    _experiment(
        "Drop the skip-citations rule so every claim carries a source tag.",
        "verbose-prose; omit-summary",
    ),
    _experiment(
        "Add a fabricate-metrics rule while dropping nothing.",
        GAUNTLET_LAST_CONTENT,
    ),
)

_STATE: dict[str, int] = {"gauntlet": 0, "field": 0}


def reset() -> None:
    """Rewind both slate scripts to the first payload."""
    _STATE["gauntlet"] = 0
    _STATE["field"] = 0


def _next_payload(key: str, script: tuple[str, ...]) -> str:
    idx = _STATE[key] % len(script)
    _STATE[key] += 1
    return script[idx]


def _dispatch(system: str) -> str | None:
    """Return the canned non-proposer response, or ``None`` for a proposer call.

    The critic check runs first (its prompt mentions "improvement proposal"
    which would otherwise shadow into the proposer fallback fingerprint):
    the scripted critic ALWAYS picks candidate index 0 — the first-sampled
    slot — so the selection never lands on the last-validated candidate.
    """
    sys_lower = system.lower()
    if _CRITIC_FINGERPRINT in sys_lower:
        return "0"
    if _PROPOSER_FINGERPRINT in sys_lower or _PROPOSER_FINGERPRINT_FALLBACK in sys_lower:
        return None
    return "ok"


async def harness_llm(system: str, user: str, model: str, **_kwargs: Any) -> str:
    """Harness-role placeholder (the deterministic adapter never calls it).

    Exists because the runtime requires two distinct role callables and the
    worker spec serialises each as a module-level dotted path.
    """
    del system, user, model
    return "unused-deterministic-harness"


async def gauntlet_slate_aux_llm(system: str, user: str, model: str, **_kwargs: Any) -> str:
    """Scripted GAUNTLET slate proposer + critic (+ other aux sites)."""
    del user, model
    canned = _dispatch(system)
    if canned is not None:
        return canned
    return _next_payload("gauntlet", GAUNTLET_SLATE)


async def field_slate_aux_llm(system: str, user: str, model: str, **_kwargs: Any) -> str:
    """Scripted RACING-FIELD slate proposer + critic (+ other aux sites)."""
    del user, model
    canned = _dispatch(system)
    if canned is not None:
        return canned
    return _next_payload("field", FIELD_SLATES)


__all__ = [
    "FIELD_CHOSEN_CONTENTS",
    "FIELD_SLATES",
    "GAUNTLET_CHOSEN_CONTENT",
    "GAUNTLET_LAST_CONTENT",
    "GAUNTLET_SLATE",
    "field_slate_aux_llm",
    "gauntlet_slate_aux_llm",
    "harness_llm",
    "reset",
]
