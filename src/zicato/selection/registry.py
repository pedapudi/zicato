"""The strategy registry — structure string → concrete strategy class.

``make_strategy(spec)`` constructs a fresh :class:`SelectionStrategy` from
a :class:`~zicato.core.types.TournamentStructure`. An unknown structure
string raises with the valid keys listed (the same posture the config
loader takes). Any structure constructed with ``field_size == 1`` degrades
to gauntlet semantics organically — one challenger, one full-board duel —
rather than erroring, mirroring fast mode's graceful degeneracy.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from zicato.selection.strategies.double_elim import DoubleEliminationStrategy
from zicato.selection.strategies.gauntlet import GauntletStrategy
from zicato.selection.strategies.racing import RacingStrategy
from zicato.selection.strategies.single_elim import SingleEliminationStrategy
from zicato.selection.strategies.swiss import SwissStrategy
from zicato.selection.strategy import SelectionStrategy

if TYPE_CHECKING:
    from zicato.core.types import TournamentStructure

#: Maps the ``structure`` token to its concrete strategy class. Keys match
#: :data:`zicato.core.types.VALID_TOURNAMENT_STRUCTURES`.
STRATEGY_REGISTRY: dict[str, type[SelectionStrategy]] = {
    GauntletStrategy.structure: GauntletStrategy,
    SingleEliminationStrategy.structure: SingleEliminationStrategy,
    DoubleEliminationStrategy.structure: DoubleEliminationStrategy,
    SwissStrategy.structure: SwissStrategy,
    RacingStrategy.structure: RacingStrategy,
}


def make_strategy(spec: TournamentStructure) -> SelectionStrategy:
    """Construct a fresh strategy for one tournament resolution.

    Raises
    ------
    ValueError
        When ``spec.structure`` is not a registry key. (The config loader
        already validates the token at load time via
        :class:`TournamentStructure.__post_init__`; this is the
        defence-in-depth check at construction.)
    """
    cls = STRATEGY_REGISTRY.get(spec.structure)
    if cls is None:
        valid = ", ".join(repr(k) for k in sorted(STRATEGY_REGISTRY))
        raise ValueError(
            f"unknown tournament structure {spec.structure!r}; "
            f"registered structures are: {valid}"
        )
    return cls(dict(spec.params))


__all__ = ["STRATEGY_REGISTRY", "make_strategy"]
