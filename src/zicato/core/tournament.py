"""Tournament-structure types: decision/scope literals, match record, structure.

Split out of :mod:`zicato.core.types`; re-exported from there and from
:mod:`zicato.core` so existing import paths keep working.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal

# ---------------------------------------------------------------------------
# Tournament decision / structure
# ---------------------------------------------------------------------------


class TournamentDecision(StrEnum):
    """The tournament's decision about an experiment.

    * :attr:`PROMOTED` (``"promoted"``) — child wins; becomes the new
      lineage head.
    * :attr:`REJECTED` (``"rejected"``) — child loses or regresses a hard
      gate.
    * :attr:`DEFERRED` (``"deferred"``) — neither wins decisively; lineage
      head unchanged but the experiment is kept for analysis.

    A :class:`~enum.StrEnum`, so a member equals its lowercase wire token
    and serialises through ``json.dumps`` with no converter — the JSON
    output and contract hash are byte-identical to the prior ``Literal``.
    The three members are exactly the prior ``Literal`` tokens, so any
    value loaded from disk as a bare ``str`` still compares equal.
    """

    PROMOTED = "promoted"
    REJECTED = "rejected"
    DEFERRED = "deferred"


class Side(StrEnum):
    """The tournament side a scheduled run belongs to.

    * :attr:`PARENT` (``"parent"``) — the champion / lineage head.
    * :attr:`CHILD` (``"child"``) — the challenger being evaluated.

    A :class:`~enum.StrEnum`, so a member equals its lowercase wire token
    and serialises identically to the bare string. These are the two
    gauntlet sides; non-gauntlet structures carry an opaque competitor
    generation id in the same ``side`` slot, so the slot's storage type
    stays ``str`` — this enum names only the two closed gauntlet tokens
    at the sites that produce them.
    """

    PARENT = "parent"
    CHILD = "child"


#: Granularity of the promote gate's pass-rate monotonicity check, gating
#: how a pass-rate movement rejects a challenger when
#: :attr:`ScoringWeights.pass_rate_monotonicity` is on:
#:
#: * ``"per_entry"`` (default, the default behaviour) — EVERY entry the
#:   champion passed must still pass on the challenger; any entry that
#:   flips champion-pass → challenger-fail rejects. The right policy when
#:   every board entry is a must-not-regress invariant (a regression
#:   suite).
#: * ``"aggregate"`` — reject only when the challenger's OVERALL pass-rate
#:   falls below the champion's (modulo a small float-noise tolerance). A
#:   challenger may trade individual entries as long as the net pass-rate
#:   holds or improves. The right policy for sampled evaluation boards
#:   where individual pass/fail is noisy and promotions should track the
#:   optimized aggregate.
#:
#: There is intentionally no ``"off"`` token: ``off`` is already expressed
#: by ``pass_rate_monotonicity=False``. Keeping the on/off switch a bool
#: and the granularity a separate field means existing ``scoring.json``
#: documents are byte-identical (the new field defaults to ``"per_entry"``)
#: and the contract hash is unchanged for every epoch already on disk.
PassRateMonotonicityScope = Literal["per_entry", "aggregate"]


#: The five v1 tournament structures. ``"gauntlet"`` is the default and
#: reproduces the historical king-of-the-hill behaviour byte-for-byte.
#: The other four are configurable per-epoch via the ``tournament`` block
#: of ``scoring.json`` (see :class:`TournamentStructure`). The string
#: tokens are the closed enum the loader validates against and the keys
#: the selection-strategy registry maps to concrete strategy classes.
VALID_TOURNAMENT_STRUCTURES: tuple[str, ...] = (
    "gauntlet",
    "single_elim",
    "double_elim",
    "swiss",
    "racing",
)


@dataclass(frozen=True, slots=True)
class MatchOutcome:
    """One match a generation played inside its tournament.

    A small audit record carried on :class:`OutcomeRecord` so a
    non-gauntlet structure (bracket / Swiss / racing) can record, per
    generation, which opponents it faced and how each duel went. A
    gauntlet leaves :attr:`OutcomeRecord.match_record` empty — its single
    crowning duel is already described by the top-level outcome fields.

    Fields
    ------
    match_id:
        Stable id of the match within the tournament (e.g. ``"WB-R0-0"``,
        ``"r2_m1"``, ``"rung1"``).
    opponent:
        The generation id this generation was paired against. Empty for a
        bye or an N-way racing rung.
    won:
        ``True`` when this generation was the match's winner (the side the
        gate / rank preferred).
    delta_scalar:
        ``this.scalar - opponent.scalar`` for the match. Negative = this
        generation scored the lower (better) loss.
    """

    match_id: str
    opponent: str
    won: bool
    delta_scalar: float


@dataclass(frozen=True, slots=True)
class TournamentStructure:
    """The per-epoch tournament structure and its tuning params.

    Part of the frozen evaluation contract: it is modelled as a field of
    :class:`ScoringWeights` (and therefore folds into the contract hash
    automatically), so changing the structure — or any param — rolls the
    epoch, exactly as retuning ``promote_margin`` does. A gauntlet
    champion and a Swiss champion are selected under different rules and
    are not directly comparable, which is the contract-roll
    rationale.

    Fields
    ------
    structure:
        One of :data:`VALID_TOURNAMENT_STRUCTURES`. Defaults to
        ``"gauntlet"`` — the shipped king-of-the-hill behaviour.
    params:
        A structure-specific JSON object, stored and round-tripped
        verbatim as an opaque ``Mapping[str, Any]`` (the same
        forward-compat posture :attr:`BoardEntry.context` takes). The
        data layer enforces only that this is a mapping; per-key
        semantics (``field_size``, ``replicates``, ``swiss.rounds_n``,
        ``racing.eta`` / ``board_fraction`` / ``rung0_board_size``, …)
        are owned by the selection strategy that reads them.

    The default factory :meth:`gauntlet` yields the fully-defaulted
    gauntlet spec an absent ``tournament`` block resolves to.
    """

    structure: str = "gauntlet"
    params: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.structure not in VALID_TOURNAMENT_STRUCTURES:
            valid = ", ".join(repr(s) for s in VALID_TOURNAMENT_STRUCTURES)
            raise ValueError(
                f"invalid tournament structure {self.structure!r}; " f"valid values are: {valid}"
            )
        if not isinstance(self.params, Mapping):
            raise ValueError(
                f"tournament params must be a JSON object (mapping), got "
                f"{type(self.params).__name__}"
            )

    @classmethod
    def gauntlet(cls) -> TournamentStructure:
        """The fully-defaulted gauntlet spec (the back-compat default)."""
        return cls(structure="gauntlet", params={})


def _default_tournament_structure() -> TournamentStructure:
    """Default-factory for :attr:`ScoringWeights.tournament_structure`."""
    return TournamentStructure.gauntlet()
