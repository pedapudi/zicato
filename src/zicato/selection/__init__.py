"""Configurable per-epoch tournament structures — the selection layer.

The :class:`SelectionStrategy` abstraction owns scheduling + bracket
bookkeeping + champion-advance + intra-tournament stopping for one epoch's
tournament structure. The promote gate
(:func:`zicato.tournament.gate.evaluate_gate`) is reused verbatim as the
per-duel acceptance test — strategies never re-decide a duel.

See ``docs/design/TOURNAMENT-STRUCTURES.md`` and
``docs/design/TOURNAMENT-DATA-MODEL.md``.
"""

from __future__ import annotations

from zicato.selection.driver import resolve_tournament
from zicato.selection.registry import STRATEGY_REGISTRY, make_strategy
from zicato.selection.strategy import (
    Contestant,
    MatchRecord,
    Matchup,
    MatchupResult,
    RoundRecord,
    SelectionDecision,
    SelectionStrategy,
    Standing,
)

__all__ = [
    "SelectionStrategy",
    "Contestant",
    "Matchup",
    "MatchupResult",
    "SelectionDecision",
    "Standing",
    "RoundRecord",
    "MatchRecord",
    "make_strategy",
    "STRATEGY_REGISTRY",
    "resolve_tournament",
]
