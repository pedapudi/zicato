"""Dotted-path predicate callables for trajectory-bootstrap entries.

The bootstrap synthesis tier (TRAJECTORY-BOOTSTRAP.md §5.2) drafts board
entries from foreign agent traces. A drift-signal property (a tool-error
cascade, a retry loop, a transfer churn) is a *process* property invisible to a
``RunResult`` matcher, so those episodes NEVER get a fabricated output predicate
— they bind to drift-loss-only scoring and/or an inline judge. The ONE honest
output predicate a bootstrap entry can pin is **structural**: a budget-blowout
entry sets a tightened ``wall_clock_budget_seconds`` and pins that the re-run
must complete without aborting (BOARD-FORMAT.md §1.2: over-budget ⇒ abort ⇒
worst-case). That is a genuine property of a finished :class:`RunResult`.

This module ships the small, resolvable dotted-path callable that check
references — a ``predicate``-kind :class:`~zicato.core.Expectation` stores the
callable's import path (bodies are never serialised, BOARD-FORMAT.md §3.4), so
the path must resolve at run time. The callable takes the finished
:class:`~zicato.core.RunResult` and returns a bool.
"""

from __future__ import annotations

from typing import Any

from zicato.core.loss import BUDGET_ABORT_CAUSE, is_infra_abort_cause

#: The dotted path a bootstrap budget-blowout entry pins as its predicate spec.
NOT_ABORTED_PATH: str = "zicato.reflection.bootstrap_predicates.not_aborted"

#: RunResult ``abort_reason`` strings that name a GENUINE wall-clock budget abort
#: (the candidate's own over-budget failure) — normalised to the LossProfile
#: :data:`BUDGET_ABORT_CAUSE` vocabulary so :func:`is_infra_abort_cause` classifies
#: them as non-infra (a real failure), not a harness blip.
_BUDGET_ABORT_REASONS: frozenset[str] = frozenset(
    {"wall_clock_budget", "wall_clock_budget_exceeded", BUDGET_ABORT_CAUSE}
)


def not_aborted(run_result: Any) -> bool:
    """``True`` iff the run finished without a *candidate* abort (BOARD-FORMAT.md §1.2).

    A budget-blowout bootstrap entry sets a tightened wall-clock budget; when the
    re-run exceeds it the adapter aborts and stamps ``aborted=True`` on the
    ``RunResult``, so this is a real, structural pass/fail over a finished run —
    honest where a drift-count "absence predicate" would not be.

    But not every abort is the candidate's failure: an INFRA / harness abort (a
    parent/supervisor kill, a worker crash, an unreadable result) says nothing
    about the agent under test, so it must NOT fail the entry. The gate is
    :func:`is_infra_abort_cause` (the same distinction screening/preflight draw):
    an infra abort returns ``True`` (not the candidate's fault); a genuine budget
    (or otherwise non-infra) abort returns ``False``. A clean run is ``True``.
    Defensive: a result missing the attribute reads as not-aborted.
    """
    if not bool(getattr(run_result, "aborted", False)):
        return True
    reason = str(getattr(run_result, "abort_reason", "") or getattr(run_result, "abort_cause", ""))
    cause = BUDGET_ABORT_CAUSE if reason in _BUDGET_ABORT_REASONS else reason
    return is_infra_abort_cause(cause)


__all__ = ["NOT_ABORTED_PATH", "not_aborted"]
