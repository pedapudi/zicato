"""Identify board entries whose time budgets dominate a round."""

from collections.abc import Sequence
from dataclasses import dataclass
from statistics import median

from zicato.core.types import BoardEntry

BUDGET_OUTLIER_FACTOR = 10.0


@dataclass(frozen=True, slots=True)
class BudgetAssessment:
    """The median budget and entries strictly above its outlier threshold."""

    median_seconds: float
    outliers: tuple[BoardEntry, ...]


def assess_budget_outliers(entries: Sequence[BoardEntry]) -> BudgetAssessment:
    """Compare budgets only when at least two entries have a positive median."""
    budgets = [entry.wall_clock_budget_seconds for entry in entries]
    middle = median(budgets) if len(budgets) >= 2 else 0
    outliers = tuple(
        entry
        for entry in entries
        if middle > 0 and entry.wall_clock_budget_seconds > BUDGET_OUTLIER_FACTOR * middle
    )
    return BudgetAssessment(middle, outliers)
