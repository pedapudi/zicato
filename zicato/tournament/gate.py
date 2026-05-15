"""The promote gate: decide whether a child generation supersedes its parent.

Two rules, applied in order:

1. **Scalar margin.** The child's combined scalar must beat the parent's
   by at least :attr:`ScoringWeights.promote_margin`. The scalar is
   "lower-is-better", so the literal check is::

       child.scalar > parent.scalar - promote_margin  →  reject

   A child that ties or slightly underperforms is rejected as
   ``"insufficient margin"``.

2. **Pass-rate monotonicity** (when
   :attr:`ScoringWeights.pass_rate_monotonicity` is true). For every
   entry where the parent recorded ``pass_fail=True``, the child MUST
   also record ``pass_fail=True``. If any such entry regressed, the
   gate rejects with the entry ids listed in the reason. Entries the
   parent failed (or had no expectation for) are not gated on this
   rule — the child is allowed to improve or stay the same.

If neither rule rejects, the gate promotes.

The :class:`GateOutcome` records the delta values regardless of the
decision, so the journal always has the same shape of evidence to
render whether the experiment passed, failed, or was deferred.

The gate uses ``decision="deferred"`` ONLY when called explicitly by a
caller who has decided neither rule cleanly fired — the function in
this module returns ``"promoted"`` or ``"rejected"``. (Deferral is a
runner-level concept, kept in the :class:`TournamentDecision` literal
type so callers can pre-merge their own deferral logic without a
schema bump.)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from zicato.core import ScoringWeights, TournamentDecision


@dataclass(frozen=True, slots=True)
class GateOutcome:
    """The promote-gate verdict plus the deltas that produced it.

    Fields
    ------
    decision:
        ``"promoted"`` | ``"rejected"`` | ``"deferred"``. The function
        :func:`evaluate_gate` returns ``"promoted"`` or ``"rejected"``
        only; deferral is reserved for higher-level callers.
    reason:
        Human-readable explanation. Empty string when promoted. For
        rejections, identifies which rule fired and (for pass-rate
        regressions) which entries.
    delta_scalar:
        ``child.scalar - parent.scalar``. Negative = improvement.
    delta_pass_rate:
        ``child.pass_rate - parent.pass_rate``. Positive = improvement.
    """

    decision: TournamentDecision
    reason: str
    delta_scalar: float
    delta_pass_rate: float


def _regressed_entries(
    parent_agg: dict[str, Any], child_agg: dict[str, Any]
) -> list[str]:
    """Return ids where parent passed and child failed (sorted).

    "Failed" means either ``pass_fail=False`` OR ``pass_fail=None`` on
    the child side for an entry the parent passed cleanly — a child
    that no longer evaluates a previously-evaluated expectation is
    still a regression in the operator's pass-rate view, and the gate
    flags it. (A child that simply did not run a given entry will not
    appear in the child's ``per_entry`` map at all; that case is
    treated the same way for monotonicity purposes.)
    """
    parent_per: dict[str, dict[str, Any]] = parent_agg.get("per_entry", {})
    child_per: dict[str, dict[str, Any]] = child_agg.get("per_entry", {})
    regressed: list[str] = []
    for entry_id, parent_row in parent_per.items():
        if parent_row.get("pass_fail") is True:
            child_row = child_per.get(entry_id)
            if child_row is None or child_row.get("pass_fail") is not True:
                regressed.append(entry_id)
    regressed.sort()
    return regressed


def evaluate_gate(
    parent_agg: dict[str, Any],
    child_agg: dict[str, Any],
    weights: ScoringWeights,
) -> GateOutcome:
    """Apply the promote gate. See module docstring for the rules."""
    parent_scalar = float(parent_agg["scalar"])
    child_scalar = float(child_agg["scalar"])
    parent_pass = float(parent_agg.get("pass_rate", 1.0))
    child_pass = float(child_agg.get("pass_rate", 1.0))

    delta_scalar = child_scalar - parent_scalar
    delta_pass_rate = child_pass - parent_pass

    # Rule 1: scalar margin.
    if child_scalar > parent_scalar - weights.promote_margin:
        return GateOutcome(
            decision="rejected",
            reason=(
                f"insufficient margin: child scalar {child_scalar:.6f} did not "
                f"beat parent {parent_scalar:.6f} by required margin "
                f"{weights.promote_margin:.6f}"
            ),
            delta_scalar=delta_scalar,
            delta_pass_rate=delta_pass_rate,
        )

    # Rule 2: pass-rate monotonicity on entries the parent passed.
    if weights.pass_rate_monotonicity:
        regressed = _regressed_entries(parent_agg, child_agg)
        if regressed:
            return GateOutcome(
                decision="rejected",
                reason="pass-rate regression on entries: " + ", ".join(regressed),
                delta_scalar=delta_scalar,
                delta_pass_rate=delta_pass_rate,
            )

    return GateOutcome(
        decision="promoted",
        reason="",
        delta_scalar=delta_scalar,
        delta_pass_rate=delta_pass_rate,
    )


__all__ = ["GateOutcome", "evaluate_gate"]
