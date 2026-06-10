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
from zicato.selection.rating import (
    fit_bradley_terry,
    prob_stronger,
    theta_rank,
)
from zicato.selection.registry import (
    STRATEGY_REGISTRY,
    STRUCTURE_DEFAULT_REPLICATES,
    default_replicates_for,
    make_strategy,
)
from zicato.selection.resolve import (
    Duel,
    MarginMatrix,
    build_matrix,
    condorcet_check,
    copeland_order,
    ranked_pairs,
    resolve_leader,
    smith_set,
)
from zicato.selection.strategy import (
    Contestant,
    MatchRecord,
    Matchup,
    MatchupResult,
    RoundRecord,
    SelectionDecision,
    SelectionStrategy,
    Standing,
    rung_for_match_id,
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
    "rung_for_match_id",
    "make_strategy",
    "STRATEGY_REGISTRY",
    "STRUCTURE_DEFAULT_REPLICATES",
    "default_replicates_for",
    "resolve_tournament",
    # Opt-in rating layer (Bradley--Terry).
    "fit_bradley_terry",
    "prob_stronger",
    "theta_rank",
    # Opt-in winner-resolution layer.
    "Duel",
    "MarginMatrix",
    "build_matrix",
    "condorcet_check",
    "smith_set",
    "ranked_pairs",
    "copeland_order",
    "resolve_leader",
]
