"""The :class:`SelectionStrategy` abstraction and its value types.

A :class:`SelectionStrategy` owns *scheduling + bracket bookkeeping +
champion-advance + intra-tournament stopping* for ONE epoch's tournament
structure. It is **stateful across matchups within a single tournament
resolution** and constructed fresh per resolution (one per evolve round
for the gauntlet; one spanning the whole bracket for the others).

The defining constraint — load-bearing across every structure — is that
the **promote gate is unchanged**. ``zicato.tournament.gate.evaluate_gate``
remains the per-duel accept/reject test. The strategy NEVER re-decides a
single duel: it reads the gate's verdict (``MatchupResult.outcome``) and
interprets it per its own bracket/Swiss/racing rules. This keeps the
per-task feasibility guarantee intact for every structure.

See ``docs/design/TOURNAMENT-STRUCTURES.md`` for the full spec.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, Literal

from zicato.core.types import Experiment, TournamentDecision
from zicato.tournament.gate import GateOutcome


@dataclass(frozen=True, slots=True)
class Contestant:
    """A generation in the field: the champion or a proposed challenger.

    Fields
    ------
    generation_id:
        The lineage id of the generation (``"v3"``, or a freshly-minted
        child id like ``"v4"``).
    role:
        ``"champion"`` is the protected incumbent; ``"challenger"`` is
        everyone the proposer produced this round.
    snapshot_root:
        The on-disk snapshot tree, or ``None`` until the experiment is
        applied (the champion is already on disk).
    experiment:
        The proposer's :class:`Experiment` for a challenger, or ``None``
        for the champion (which has no new experiment this round).
    """

    generation_id: str
    role: Literal["champion", "challenger"]
    snapshot_root: Path | None = None
    experiment: Experiment | None = None


@dataclass(frozen=True, slots=True)
class Matchup:
    """A single duel the strategy wants run next.

    Fields
    ------
    matchup_id:
        Stable id within the tournament (links a result back to the
        bracket node / Swiss pairing / racing rung that scheduled it).
    left, right:
        The two contestants. By convention ``left`` is the
        incumbent/higher-seed; the gate treats ``left`` as the nominal
        parent and ``right`` as the nominal child.
    board_subset:
        ``None`` ⇒ the full board. A racing rung passes a tuple of entry
        ids to evaluate on a board slice.
    replicates:
        How many paired board runs to average before scoring (``>= 1``).
        ``1`` is the gauntlet's exact single-run path; bracket structures
        default to ``>= 2`` because replication, not bracket shape, is the
        noise lever.
    round_index:
        The bracket round / Swiss round / racing rung this matchup belongs
        to. Carried through to the persisted record for the dashboard.
    bracket_slot:
        Single/double-elim bracket position (e.g. ``"WB-R1-0"``); empty
        for structures without a bracket.
    """

    matchup_id: str
    left: Contestant
    right: Contestant
    board_subset: tuple[str, ...] | None = None
    replicates: int = 1
    round_index: int = 0
    bracket_slot: str = ""


@dataclass(frozen=True, slots=True)
class MatchupResult:
    """A completed duel, handed back to the strategy.

    Fields
    ------
    matchup_id:
        The id of the :class:`Matchup` that produced this result.
    left_agg, right_agg:
        The two aggregate dicts (``aggregate_generation_score`` output)
        for ``left`` and ``right`` respectively.
    outcome:
        The **unchanged** :class:`~zicato.tournament.gate.GateOutcome`
        from ``evaluate_gate``, treating ``left`` as parent and ``right``
        as child. ``outcome.decision`` is the gate's verdict; the strategy
        interprets it per its own rules and never re-implements the gate.
    left_id, right_id:
        The generation ids of the two sides (mirrors the matchup so a
        result is self-describing for the audit trail).
    round_index, bracket_slot:
        Copied from the matchup for the persisted bracket record.
    """

    matchup_id: str
    left_id: str
    right_id: str
    left_agg: dict[str, Any]
    right_agg: dict[str, Any]
    outcome: GateOutcome
    round_index: int = 0
    bracket_slot: str = ""

    def left_scalar(self) -> float:
        """The ``left`` side's scalar (lower is better)."""
        return float(self.left_agg.get("scalar", 0.0))

    def right_scalar(self) -> float:
        """The ``right`` side's scalar (lower is better)."""
        return float(self.right_agg.get("scalar", 0.0))

    def lower_scalar_id(self) -> str:
        """The id of the side with the lower (better) scalar.

        Used by the challenger-vs-challenger bracket nodes, which have no
        incumbent: the gate is run with ``left`` as nominal parent, and
        the winner is the side the gate prefers — equivalently the lower
        scalar. ``outcome.delta_scalar`` is ``right - left``, so a
        negative delta means ``right`` is better. Ties keep ``left`` (the
        higher seed) as the historical no-improvement convention.
        """
        if self.outcome.delta_scalar < 0.0:
            return self.right_id
        return self.left_id


@dataclass(frozen=True, slots=True)
class SelectionDecision:
    """The crowned outcome once a tournament resolves.

    Fields
    ------
    promoted_generation_id:
        The generation the orchestrator should promote, or ``None`` when
        the champion stands.
    decision:
        The crowning verdict, reusing
        :data:`~zicato.core.types.TournamentDecision`.
    reason:
        Human-readable; mirrors ``GateOutcome.reason`` for the crowning
        duel.
    matchups:
        The full flat bracket audit (every :class:`MatchupResult`) for the
        journal / dashboard.
    crowning_matchup_id:
        The id of the duel that decided promotion (the final
        champion-gate match). Empty when no crowning duel ran.
    standings:
        The final ranking, one :class:`Standing` per contestant, ordered
        best-first. Empty for a gauntlet (the two-row view is enough).
    """

    promoted_generation_id: str | None
    decision: TournamentDecision
    reason: str
    matchups: tuple[MatchupResult, ...] = ()
    crowning_matchup_id: str = ""
    standings: tuple[Standing, ...] = ()


@dataclass(frozen=True, slots=True)
class Standing:
    """One contestant's position in the final standings.

    Mirrors the dashboard ``standings`` shape (data-model §2.5). The
    orchestrator persists these so a non-gauntlet structure renders a
    leaderboard / bracket without re-deriving them.
    """

    generation_id: str
    rank: int
    scalar: float
    wins: int = 0
    losses: int = 0
    status: Literal["alive", "eliminated", "champion"] = "alive"
    role: Literal["champion", "challenger"] = "challenger"


@dataclass(frozen=True, slots=True)
class RoundRecord:
    """One settled round / rung / bracket-round, for the persisted record.

    Mirrors the data-model ``rounds[]`` shape (§2.4). The strategy emits
    these so the persisted tournament record (and, later, the dashboard)
    can render the structure's progression. ``matches`` carries the
    per-match generalization of today's single champion-vs-challenger row.
    """

    round_index: int
    label: str
    matches: tuple[MatchRecord, ...] = ()


@dataclass(frozen=True, slots=True)
class MatchRecord:
    """One settled match inside a :class:`RoundRecord` (data-model §2.4)."""

    match_id: str
    competitors: tuple[str, ...]
    winner: str = ""
    decision: str = ""
    delta_scalar: float | None = None
    bracket_slot: str = ""
    bye: bool = False
    # Racing rungs: a rung does not crown, it cuts.
    survivors: tuple[str, ...] = ()
    cut: tuple[str, ...] = ()
    board_fraction: float | None = None


class SelectionStrategy(ABC):
    """Owns scheduling + bracket bookkeeping + advance + stopping.

    Constructed fresh per tournament resolution. The lifecycle the driver
    (:func:`resolve_tournament`) walks:

    1. :meth:`field_size` — how many challengers to request.
    2. :meth:`seed` — initialise bracket state from the applied field.
    3. loop: :meth:`next_matchups` → run them → :meth:`record_result`
       until :meth:`resolved`.
    4. :meth:`champion` — the crowned :class:`SelectionDecision`.

    Subclasses interpret ``MatchupResult.outcome`` (the gate verdict) but
    never re-run the gate.
    """

    structure: ClassVar[str]

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        self.params: dict[str, Any] = dict(params or {})

    @abstractmethod
    def field_size(self) -> int:
        """How many challengers the proposer must emit this round."""

    @abstractmethod
    def seed(self, champion: Contestant, challengers: Sequence[Contestant]) -> None:
        """Initialise bracket state from the champion + the applied field."""

    @abstractmethod
    def next_matchups(self) -> Sequence[Matchup]:
        """The duel(s) to run next.

        May return more than one for a parallel round (a Swiss round, a
        racing rung). An empty sequence means nothing is schedulable right
        now; the driver then checks :meth:`resolved`.
        """

    @abstractmethod
    def record_result(self, result: MatchupResult) -> None:
        """Fold one completed duel's gate verdict into bracket state.

        The ONLY place a :class:`GateOutcome` is interpreted.
        """

    @abstractmethod
    def resolved(self) -> bool:
        """True once the tournament has a settled winner (no more duels)."""

    @abstractmethod
    def champion(self) -> SelectionDecision:
        """The crowned :class:`SelectionDecision` once :meth:`resolved`."""

    def rounds(self) -> tuple[RoundRecord, ...]:
        """The settled per-round records (data-model §2.4).

        Default empty (the gauntlet leaves ``rounds`` empty, as the
        back-compat invariant allows). Non-gauntlet strategies override
        this to emit their bracket / Swiss / racing progression.
        """
        return ()


def _param_int(params: dict[str, Any], key: str, default: int) -> int:
    """Read an int param defensively (semantics validation is the strategy's)."""
    try:
        return int(params.get(key, default))
    except (TypeError, ValueError):
        return default


def _param_float(params: dict[str, Any], key: str, default: float) -> float:
    """Read a float param defensively."""
    try:
        return float(params.get(key, default))
    except (TypeError, ValueError):
        return default


__all__ = [
    "Contestant",
    "Matchup",
    "MatchupResult",
    "SelectionDecision",
    "Standing",
    "RoundRecord",
    "MatchRecord",
    "SelectionStrategy",
]
