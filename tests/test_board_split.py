"""Tests for :mod:`zicato.board.split` — the train/holdout board split.

The split is a pure, deterministic partition (OVERFITTING.md §3, §12 #1):
an explicit ``holdout`` tag wins outright; otherwise an id-stable hash
threshold selects ~``holdout_fraction`` once the board is big enough; a
board too small to split (or a disabled config) degrades to an empty
holdout so the loop behaves byte-identically to before the split existed.
"""

from __future__ import annotations

from zicato.board.split import HOLDOUT_TAG, split_board
from zicato.core.types import BoardEntry, OverfittingConfig


def _entry(entry_id: str, *, tags: tuple[str, ...] = ()) -> BoardEntry:
    return BoardEntry(
        id=entry_id,
        kind="single_turn",
        wall_clock_budget_seconds=60,
        tags=tags,
        input="hello",
    )


def _board(n: int) -> list[BoardEntry]:
    return [_entry(f"e{i:02d}") for i in range(n)]


def test_explicit_holdout_tag_wins_even_on_a_tiny_board() -> None:
    # A two-entry board is far below any min_board_size_for_split, but an
    # explicit tag overrides the floor outright.
    board = [_entry("a"), _entry("b", tags=(HOLDOUT_TAG,))]
    cfg = OverfittingConfig(min_board_size_for_split=8)
    train, holdout = split_board(board, cfg)
    assert train == ("a",)
    assert holdout == ("b",)


def test_explicit_holdout_tag_wins_even_when_disabled() -> None:
    board = [_entry("a"), _entry("b", tags=(HOLDOUT_TAG,))]
    cfg = OverfittingConfig(enabled=False)
    train, holdout = split_board(board, cfg)
    assert train == ("a",)
    assert holdout == ("b",)


def test_small_board_degrades_to_empty_holdout() -> None:
    board = _board(5)  # below the default min of 8
    train, holdout = split_board(board, OverfittingConfig())
    assert train == tuple(e.id for e in board)
    assert holdout == ()


def test_disabled_config_degrades_to_empty_holdout() -> None:
    board = _board(50)  # plenty big, but the switch is off
    train, holdout = split_board(board, OverfittingConfig(enabled=False))
    assert train == tuple(e.id for e in board)
    assert holdout == ()


def test_hash_split_is_deterministic() -> None:
    board = _board(40)
    cfg = OverfittingConfig(min_board_size_for_split=8, holdout_fraction=0.3)
    a = split_board(board, cfg)
    b = split_board(board, cfg)
    assert a == b
    # Stable under input reordering: the partition is by id, not position.
    reordered = list(reversed(board))
    train_r, holdout_r = split_board(reordered, cfg)
    assert set(train_r) == set(a[0])
    assert set(holdout_r) == set(a[1])


def test_hash_split_respects_the_fraction_approximately() -> None:
    board = _board(200)
    cfg = OverfittingConfig(min_board_size_for_split=8, holdout_fraction=0.3)
    train, holdout = split_board(board, cfg)
    # Union is exact, no overlap.
    assert set(train) | set(holdout) == {e.id for e in board}
    assert not (set(train) & set(holdout))
    # The realised fraction lands near the target on a board this size.
    frac = len(holdout) / len(board)
    assert 0.2 <= frac <= 0.4


def test_partition_preserves_board_order() -> None:
    board = _board(40)
    cfg = OverfittingConfig(min_board_size_for_split=8)
    train, holdout = split_board(board, cfg)
    order = [e.id for e in board]
    assert list(train) == [i for i in order if i in set(train)]
    assert list(holdout) == [i for i in order if i in set(holdout)]


def test_a_fraction_that_would_select_everything_degrades() -> None:
    # holdout_fraction ~1.0 would leave no train slice; the degenerate
    # guard collapses to an empty holdout rather than starving train.
    board = _board(40)
    cfg = OverfittingConfig(min_board_size_for_split=8, holdout_fraction=0.999999)
    train, holdout = split_board(board, cfg)
    assert holdout == ()
    assert train == tuple(e.id for e in board)
