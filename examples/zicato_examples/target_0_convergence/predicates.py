"""Predicate functions for the target_0_convergence board.

Referenced from ``board.jsonl`` entries via ``expectation`` blocks of
kind ``"predicate"``, e.g.::

    zicato_examples.target_0_convergence.predicates:has_summary

Every predicate accepts a single :class:`zicato.core.types.RunResult`
positional argument and returns ``bool``. They are deliberately
defensive — a run may abort partway and hand the predicate an empty
``final_output``; predicates must never raise.

Each predicate checks exactly ONE feature the deterministic harness
(:mod:`zicato_examples.target_0_convergence.harness`) either produces
or suppresses depending on the defect tokens remaining in
``agent/policy.py``, so each token's removal flips exactly one entry
from fail to pass and the board differentiates every partial fix.

The predicates module is itself NOT a zicato mutation point — the
proposer does not get to rewrite the operator's pass/fail contract.
"""

from __future__ import annotations

# zicato:grading — operator-owned pass/fail contract; never a proposer mutation point.
from typing import Any

#: Character budget for the conciseness predicate. The harness's base
#: output (all features present, no filler) stays well under this; the
#: ``verbose-prose`` filler paragraph alone blows well past it.
CONCISE_MAX_CHARS = 400


def _final_output(result: Any) -> str:
    """Return ``result.final_output`` as a lowercase string, or empty.

    Tolerates a missing attribute so predicates never raise on whatever
    shape the runner hands them.
    """
    out = getattr(result, "final_output", "") or ""
    return str(out).lower()


def has_body(result: Any) -> bool:
    """The output carries the harness's note marker at all.

    The stable control entry: the deterministic harness always opens
    its output with ``NOTE:``, so this passes for every generation —
    proving the agent runs at all, independent of the defect tokens.
    """
    return "note:" in _final_output(result)


def has_summary(result: Any) -> bool:
    """The output ends with a ``SUMMARY:`` line.

    Fails exactly while the ``omit-summary`` defect token remains in
    the policy.
    """
    return "summary:" in _final_output(result)


def has_citations(result: Any) -> bool:
    """The output carries a ``[source: ...]`` citation.

    Fails exactly while the ``skip-citations`` defect token remains in
    the policy.
    """
    return "[source:" in _final_output(result)


def is_concise(result: Any) -> bool:
    """The output stays under the conciseness character budget.

    Fails exactly while the ``verbose-prose`` defect token remains in
    the policy (the filler paragraph alone exceeds the budget). An
    empty output is NOT concise — it is a failed run, and the predicate
    must not reward it.
    """
    out = _final_output(result)
    return 0 < len(out) <= CONCISE_MAX_CHARS


def no_fabricated_metrics(result: Any) -> bool:
    """The output never asserts the harness's unverified metric claim.

    Fails exactly while the ``fabricate-metrics`` defect token is
    present — the token the scripted proposer's negative-control round
    tries to introduce. An empty output is a failed run, not an honest
    one, so it fails too.
    """
    out = _final_output(result)
    if not out:
        return False
    return "metric-claim:" not in out


__all__ = [
    "CONCISE_MAX_CHARS",
    "has_body",
    "has_summary",
    "has_citations",
    "is_concise",
    "no_fabricated_metrics",
]
