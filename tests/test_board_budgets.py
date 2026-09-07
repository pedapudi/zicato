"""Budget outliers use a strict threshold and preserve entry identity."""

import pytest

from zicato.board.budgets import assess_budget_outliers
from zicato.core.types import BoardEntry


@pytest.mark.parametrize(
    ("budgets", "median", "outlier_positions"),
    [
        ([], 0, ()),
        ([1000], 0, ()),
        ([10, 20], 15, ()),
        ([10, 10, 100], 10, ()),
        ([10, 10, 101], 10, (2,)),
    ],
)
def test_budget_outliers(
    budgets: list[int], median: float, outlier_positions: tuple[int, ...]
) -> None:
    entries = [
        BoardEntry(id="duplicate", kind="single_turn", input="task", wall_clock_budget_seconds=n)
        for n in budgets
    ]
    assessment = assess_budget_outliers(entries)
    assert assessment.median_seconds == median
    assert len(assessment.outliers) == len(outlier_positions)
    for outlier, position in zip(assessment.outliers, outlier_positions, strict=True):
        assert outlier is entries[position]
