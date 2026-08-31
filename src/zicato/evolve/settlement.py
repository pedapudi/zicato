"""Typed result passed from tournament evaluation to durable settlement."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from zicato.core.types import OutcomeRecord
from zicato.selection.strategy import SelectionDecision

if TYPE_CHECKING:
    from zicato.evolve.propose_apply import _AppliedChallenger


@dataclass(frozen=True, slots=True)
class CandidateSettlement:
    """The terminal outcome for one applied challenger."""

    challenger: _AppliedChallenger
    outcome: OutcomeRecord


@dataclass(frozen=True, slots=True)
class RoundSettlement:
    """The complete decision that one persistence tail commits."""

    decision: SelectionDecision
    primary_promoted_generation_id: str | None
    promoted_generation_ids: tuple[str, ...]
    candidates: tuple[CandidateSettlement, ...]


def ordered_promotions(primary: str | None, promoted: set[str]) -> tuple[str, ...]:
    """Return the primary champion first, followed by other promoted ids."""

    if primary is None:
        return tuple(sorted(promoted))
    return (primary, *sorted(promoted - {primary}))


__all__ = ["CandidateSettlement", "RoundSettlement", "ordered_promotions"]
