"""The strategy registries — structure token → concrete strategy class.

``make_strategy(spec)`` constructs a fresh :class:`SelectionStrategy` from
a :class:`~zicato.core.types.TournamentStructure`. Two registries hold the
tokens: :data:`STRATEGY_REGISTRY` carries the default structure choice
(``gauntlet`` and ``racing``) and :data:`EXPERIMENTAL_STRATEGY_REGISTRY`
the three an operator admits through ``experimental.tournament_structures``
in ``scoring.json``. An experimental token without that opt-in, or a token
in neither registry, raises ``ValueError``. Any structure constructed with
``field_size == 1`` degrades to gauntlet semantics — one challenger, one
full-board duel — rather than erroring, mirroring fast mode's graceful
degeneracy.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from zicato.core.scoring_config import ExperimentalConfig
from zicato.core.tournament import experimental_structure_refusal
from zicato.selection.experimental.double_elim import DoubleEliminationStrategy
from zicato.selection.experimental.single_elim import SingleEliminationStrategy
from zicato.selection.experimental.swiss import SwissStrategy
from zicato.selection.strategies.gauntlet import GauntletStrategy
from zicato.selection.strategies.racing import RacingStrategy
from zicato.selection.strategy import SelectionStrategy

if TYPE_CHECKING:
    from zicato.core.types import TournamentStructure

#: The default structure choice: one challenger against the champion, or a
#: field of challengers raced on escalating board slices.
STRATEGY_REGISTRY: dict[str, type[SelectionStrategy]] = {
    GauntletStrategy.structure: GauntletStrategy,
    RacingStrategy.structure: RacingStrategy,
}

#: The structures that resolve only under the contract opt-in. Keys equal
#: :data:`zicato.core.tournament.EXPERIMENTAL_TOURNAMENT_STRUCTURES`; the
#: two registries together cover
#: :data:`zicato.core.types.VALID_TOURNAMENT_STRUCTURES`.
EXPERIMENTAL_STRATEGY_REGISTRY: dict[str, type[SelectionStrategy]] = {
    SingleEliminationStrategy.structure: SingleEliminationStrategy,
    DoubleEliminationStrategy.structure: DoubleEliminationStrategy,
    SwissStrategy.structure: SwissStrategy,
}


def default_replicates_for(structure: str) -> int:
    """Read the replicate default directly from the declaring strategy class."""
    strategy = STRATEGY_REGISTRY.get(structure) or EXPERIMENTAL_STRATEGY_REGISTRY.get(structure)
    return strategy._default_replicates if strategy is not None else 1


def make_strategy(
    spec: TournamentStructure,
    board_ids: Sequence[str] | None = None,
    *,
    replicates: int | None = None,
    noise_floor_delta_std: float | None = None,
    experimental: ExperimentalConfig | None = None,
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
        swiss) untouched.
    replicates:
        The replicate count in effect for the epoch
        (:func:`zicato.selection.replicates.resolve_replicates`), injected
        as ``params["replicates"]`` only when the spec pins none, so a
        pinned contract count is never overridden. ``None`` leaves the
        strategy to its own default.
    noise_floor_delta_std:
        The epoch's measured A/A ``delta_std`` when the floor carries a
        usable one, injected as ``params["noise_floor_delta_std"]``. Racing
        resolves its rung cuts from it
        (:class:`zicato.selection.strategies.racing.RacingStrategy`); the
        other structures receive no injected floor. ``None`` injects nothing, and racing
        then cuts by rank alone.
    experimental:
        The contract's optional structure, standings-rating and leader-resolver
        settings. An absent config leaves these features inactive.

    Raises
    ------
    ValueError
        When ``spec.structure`` is experimental and the contract has not
        opted in, or is in neither registry. (The config loader already
        validates the token at load time via
        :class:`TournamentStructure.__post_init__` and the opt-in via
        :class:`~zicato.core.scoring_config.ScoringWeights`; this is the
        defence-in-depth check at construction.)
    """
    experimental = experimental if experimental is not None else ExperimentalConfig()
    for key in ("rating", "resolver"):
        if key in spec.params:
            field = "standing_rating" if key == "rating" else "resolver"
            raise ValueError(f"move tournament.params.{key} to experimental.{field}")
    cls = STRATEGY_REGISTRY.get(spec.structure)
    if cls is None and spec.structure in EXPERIMENTAL_STRATEGY_REGISTRY:
        if not experimental.tournament_structures:
            raise ValueError(experimental_structure_refusal(spec.structure))
        cls = EXPERIMENTAL_STRATEGY_REGISTRY[spec.structure]
    if cls is None:
        valid = ", ".join(repr(k) for k in sorted(STRATEGY_REGISTRY))
        raise ValueError(
            f"unknown tournament structure {spec.structure!r}; "
            f"registered structures are: {valid}"
        )
    params = dict(spec.params)
    if "rating" in cls.parameter_names and experimental.standing_rating != "none":
        params["rating"] = experimental.standing_rating
    if "resolver" in cls.parameter_names and experimental.resolver != "none":
        params["resolver"] = experimental.resolver
    # Default ``board_ids`` to the epoch's full board when the operator
    # did not pin a subset. Explicit ``params["board_ids"]`` always wins.
    if "board_ids" in cls.parameter_names and board_ids is not None and "board_ids" not in params:
        params["board_ids"] = tuple(str(x) for x in board_ids)
    if replicates is not None and "replicates" not in params:
        params["replicates"] = replicates
    if (
        "noise_floor_delta_std" in cls.parameter_names
        and noise_floor_delta_std is not None
        and "noise_floor_delta_std" not in params
    ):
        params["noise_floor_delta_std"] = noise_floor_delta_std
    return cls(params)


__all__ = [
    "EXPERIMENTAL_STRATEGY_REGISTRY",
    "STRATEGY_REGISTRY",
    "default_replicates_for",
    "make_strategy",
]
