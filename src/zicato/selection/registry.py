"""The strategy registry — structure string → concrete strategy class.

``make_strategy(spec)`` constructs a fresh :class:`SelectionStrategy` from
a :class:`~zicato.core.types.TournamentStructure`. An unknown structure
string raises with the valid keys listed (the same posture the config
loader takes). Any structure constructed with ``field_size == 1`` degrades
to gauntlet semantics organically — one challenger, one full-board duel —
rather than erroring, mirroring fast mode's graceful degeneracy.
"""

from __future__ import annotations

from collections.abc import Sequence
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


def make_strategy(
    spec: TournamentStructure,
    board_ids: Sequence[str] | None = None,
) -> SelectionStrategy:
    """Construct a fresh strategy for one tournament resolution.

    Parameters
    ----------
    spec:
        The per-epoch :class:`~zicato.core.types.TournamentStructure`
        (structure token + free-form params).
    board_ids:
        The epoch's board entry ids, if known. When provided, they are
        injected into the strategy params as the default ``board_ids`` —
        but ONLY when the spec did not already carry an explicit
        ``board_ids`` (the operator override always wins). This lets
        board-aware structures (racing) default to the full epoch board
        without the operator having to list every id on the CLI, while
        leaving board-agnostic structures (gauntlet, single/double-elim,
        swiss) untouched — they simply ignore the param.

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
    params = dict(spec.params)
    # Default ``board_ids`` to the epoch's full board when the operator
    # did not pin a subset. Explicit ``params["board_ids"]`` always wins.
    if board_ids is not None and "board_ids" not in params:
        params["board_ids"] = tuple(str(x) for x in board_ids)
    return cls(params)


__all__ = ["STRATEGY_REGISTRY", "make_strategy"]
