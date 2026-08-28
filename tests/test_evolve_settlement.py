"""Tests for the typed tournament-settlement boundary."""

from __future__ import annotations

from zicato.evolve.settlement import ordered_promotions


def test_ordered_promotions_places_the_champion_pointer_first() -> None:
    """A multi-promote keeps one explicit head and deterministic side branches."""

    assert ordered_promotions("v4", {"v2", "v4", "v3"}) == ("v4", "v2", "v3")


def test_ordered_promotions_is_deterministic_without_a_primary_head() -> None:
    """A headless promoted set still has stable persistence order."""

    assert ordered_promotions(None, {"v9", "v7", "v8"}) == ("v7", "v8", "v9")
