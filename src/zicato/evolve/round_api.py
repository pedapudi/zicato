"""Typed public values returned by the round pipeline."""

# ruff: noqa: E402
from __future__ import annotations

import logging
import time  # noqa: F401  — kept as the ``orch.time`` clock seam (see __all__)
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

log = logging.getLogger("zicato.orchestrator")

CallLLM = Callable[[str, str, str], Awaitable[str]]

#: The :attr:`EvolveRoundOutcome.tournament_decision` value for a round the
#: endpoint-outage circuit DEFERRED
#: (:attr:`~zicato.core.runtime.RuntimeConfig.infra_abort_round_threshold`).
#: Distinct from ``"rejected"`` on purpose: the evolve loop must not count
#: it toward the consecutive-rejection breaker (the experiment was never
#: judged), and it backs off + re-reconciles instead of re-proposing.
DEFERRED_INFRA_DECISION = "deferred_infra"


@dataclass(frozen=True, slots=True)
class EvolveRoundOutcome:
    """One round's summary, returned by :func:`evolve_once`.

    Fields
    ------
    parent_generation_id:
        Lineage head this round challenged.
    proposed_generation_id:
        Id assigned to the child generation the proposer produced.
    tournament_decision:
        ``"promoted"`` or ``"rejected"``. ``"deferred"`` is mapped to
        ``"rejected"`` for the orchestrator's bookkeeping — the
        evolve loop only advances on promotions.
        :data:`DEFERRED_INFRA_DECISION` (``"deferred_infra"``) is the ONE
        additional value: the endpoint-outage circuit tripped, nothing
        was journaled (the experiment persists un-outcomed for the
        crash-resume reconciliation), and the loop backs off before the
        next round.
    rejection_reason:
        Symbolic / human-readable string when the round did not
        promote. Empty string on a successful promotion.
    parent_scalar:
        Parent generation's scalar score (drift + pass terms weighted).
    child_scalar:
        Child generation's scalar score.
    delta_scalar:
        ``child_scalar - parent_scalar``. Negative = improvement.
    health_summary:
        One-line summary of the round's loop-health assessment (see
        :func:`zicato.health.diagnostics.assess_loop_health`). Empty
        string when the health sibling is unavailable or the assessment
        could not be run — the round's outcome is unaffected either way.
    health_critical:
        ``True`` when the round's loop-health assessment surfaced at
        least one CRITICAL finding (e.g. degenerate scoring producing no
        signal). ``False`` otherwise, including when no assessment ran.
    """

    parent_generation_id: str
    proposed_generation_id: str
    tournament_decision: str
    rejection_reason: str
    parent_scalar: float
    child_scalar: float
    delta_scalar: float
    health_summary: str = ""
    health_critical: bool = False


def _declared_custom_judge_names(board: list[Any], weights: Any) -> frozenset[str]:
    """Return the names of the custom judges declared by the contract.

    A custom judge is addressable in a proposer hypothesis as a
    ``drift:<judge_name>`` metric even though, on the goldfive side, it
    emits under the single ``"custom"`` drift kind. The set of valid
    judge names is the union of:

    * every ``JudgeSpec.name`` on every board entry (``board[*].judges``);
    * every key of :attr:`ScoringWeights.per_judge_weights`.

    Threaded into :func:`zicato.proposer.proposer.propose_experiment` so
    the hypothesis validator accepts ``drift:<judge_name>`` for a declared
    judge and still rejects an unknown drift kind.
    """
    names: set[str] = set()
    for entry in board:
        for judge in getattr(entry, "judges", ()) or ():
            judge_name = getattr(judge, "name", None)
            if judge_name:
                names.add(str(judge_name))
    per_judge = getattr(weights, "per_judge_weights", None) or {}
    names.update(str(k) for k in per_judge)
    return frozenset(names)


# ---------------------------------------------------------------------------
# Contract-hash auto-epoching
# ---------------------------------------------------------------------------
# The roll-at-evolve-time decision and its helpers live in
# ``zicato.evolve.epoching``.


# ---------------------------------------------------------------------------
# evolve_once
# ---------------------------------------------------------------------------
