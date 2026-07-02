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
from dataclasses import dataclass, field
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
    stage_index:
        The bracket round / Swiss round / racing rung this matchup belongs
        to — the WITHIN-tournament stage, a different axis from a generation's
        OUTER evolve ``round_index`` (see :class:`RoundRecord`). Carried
        through to the persisted record for the dashboard.
    bracket_slot:
        Single/double-elim bracket position (e.g. ``"WB-R1-0"``); empty
        for structures without a bracket.
    matchup_budget_seconds:
        Optional WALL-CLOCK cap (seconds) on this matchup's TOTAL board-unit
        execution. ``None`` (the default) ⇒ uncapped: every board unit ×
        replicate × side runs to completion exactly as today. When set, the
        runner stops LAUNCHING further board units once the matchup's
        running wall-clock total exceeds the cap and treats the un-run units
        as budget-exceeded losses (see :func:`zicato.tournament.runner.run_matchup`).
        This is distinct from the per-board ``BoardEntry.wall_clock_budget_seconds``
        (which bounds ONE unit): it caps the AGGREGATE of an unbounded board ×
        replicates × both-sides sweep — the failure mode where each unit is
        individually under budget but their sum grinds for hours. Set by a
        strategy that wants to bound a full-board rung (e.g. racing's final
        crowning duel).
    """

    matchup_id: str
    left: Contestant
    right: Contestant
    board_subset: tuple[str, ...] | None = None
    replicates: int = 1
    stage_index: int = 0
    bracket_slot: str = ""
    matchup_budget_seconds: float | None = None


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
    stage_index, bracket_slot:
        Copied from the matchup for the persisted bracket record.
    """

    matchup_id: str
    left_id: str
    right_id: str
    left_agg: dict[str, Any]
    right_agg: dict[str, Any]
    outcome: GateOutcome
    stage_index: int = 0
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

    NB — ``stage_index`` here is the WITHIN-tournament stage index (a bracket
    round / Swiss round / racing rung INSIDE one evolve round); it is a
    DIFFERENT axis from a generation's ``Generation.round_index`` /
    ``Experiment.round_index``, which is the OUTER evolve (epoch-child) round
    the generation was born in. They were once both called ``round_index``;
    the within-tournament axis was renamed to ``stage_index`` to kill that
    overload, so the unqualified word "round" now always means the evolve
    round. ``label`` stays structure-qualified ("Bracket round N" / "Swiss
    round N" / "Rung N" / "Winners' bracket"). The persisted ``rounds[]`` JSON
    key is ``stage_index``; readers still accept the legacy ``round_index``
    key for workspaces written before the rename.
    """

    stage_index: int
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
    # In-flight marker: ``True`` for a scheduled-but-unresolved match in
    # the live ``active_tournament`` envelope (``winner`` is still
    # ``""`` / ``null``); ``False`` for every settled match. The settled
    # ``rounds()`` view never sets it, so the durable record and the
    # post-run envelope are unchanged.
    pending: bool = False
    # Authoritative per-lane LIVE progress for an in-flight racing rung,
    # keyed by competitor ``generation_id``. Each value is a small dict the
    # dashboard's racing scalar-track / survival-funnel consumes directly,
    # rather than reconstructing it from the per-duel ``projected`` map:
    # ``{boards_done, boards_total, projected_scalar?, inflight, projected}``.
    # The STRATEGY owns the topology (which lanes, their ``boards_total`` =
    # the rung's board-slice size, the ``inflight`` flag, and the lane's
    # last-known running scalar vs the champion when available); the runner's
    # per-board ``projected`` map (owned by the scorer) is overlaid at
    # serialise time to fill ``boards_done`` + the live ``projected_scalar``.
    # Empty for every settled match and for non-racing structures, so the
    # durable record + the post-run envelope are byte-unchanged.
    live_progress: dict[str, dict[str, Any]] = field(default_factory=dict)


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

    #: The per-structure default ``replicates`` when ``params["replicates"]``
    #: is unset — the SINGLE SOURCE OF TRUTH for a structure's default
    #: replication. The base default is ``2`` — the noise-aware posture:
    #: evaluations are stochastic, so a duel decided by one paired run is
    #: decided by one noise draw; two averaged runs is the cheapest hedge
    #: (replication, not bracket shape, is the noise lever — see SELECTION.md
    #: §8). The gauntlet and the bracket / Swiss structures inherit it;
    #: racing pins ``1`` (its replication is intrinsic to the escalating
    #: board slices). Pin ``"replicates": 1`` in the structure params for the
    #: historical single-run duel (deterministic harnesses do). EVERY
    #: consumer that needs "the default replicates for this structure" reads
    #: it from here: the strategy ``__init__`` resolves
    #: ``params["replicates"]`` against it, and the builder cost estimator
    #: reads it via
    #: :data:`zicato.selection.registry.STRUCTURE_DEFAULT_REPLICATES` so the
    #: meter can never under-report by assuming a flat ``1``.
    _default_replicates: ClassVar[int] = 2

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        self.params: dict[str, Any] = dict(params or {})

    def replicates(self) -> int:
        """The per-duel replicate count this strategy resolved.

        Every concrete strategy resolves ``params["replicates"]`` against its
        ``_default_replicates`` in ``__init__`` (into ``self._replicates``);
        this is the public read the orchestrator uses to thread the SAME
        resolved value into an execution path that does not run through
        :class:`Matchup` objects (the gauntlet's ``run_tournament`` call).
        Falls back to the class default for a strategy that has not stored
        the attribute.
        """
        return int(getattr(self, "_replicates", self._default_replicates))

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

    # -- live (in-flight) projection --------------------------------------
    #
    # The settled :meth:`rounds` / :meth:`champion` views only materialise
    # once a round (or the whole tournament) has resolved. The orchestrator
    # publishes the live ``active_tournament`` envelope WHILE the
    # tournament runs, so it needs the settled rounds PLUS the round that is
    # currently scheduled-but-unresolved (its matches carry ``winner=""``
    # and ``pending=True``) and the standings-so-far. ``live_rounds`` /
    # ``live_standings`` are the ONE shared projection for that: the base
    # composes them from two small per-strategy hooks
    # (:meth:`_pending_round` / :meth:`_live_standings`) so the live and
    # settled envelopes are byte-compatible shapes and every renderer works
    # identically live and post-run. A structure that schedules nothing
    # pending (the gauntlet, or a resolved strategy) yields exactly
    # ``rounds`` / its settled standings — no special-casing in the
    # orchestrator.

    def live_rounds(self) -> tuple[RoundRecord, ...]:
        """Settled rounds plus the current in-flight round (data-model §2.4).

        The shared live projection: :meth:`rounds` (every round that has
        closed) followed by — when a round is mid-flight — a single
        :class:`RoundRecord` carrying the scheduled-but-unresolved matches
        from :meth:`_pending_round`. Those matches have ``winner=""`` (the
        live ``winner: null`` the orchestrator serialises) and
        ``pending=True``. When nothing is pending the result is exactly
        :meth:`rounds`.
        """
        pending = self._pending_round()
        if pending is None:
            return self.rounds()
        return (*self.rounds(), pending)

    def live_standings(self) -> tuple[Standing, ...]:
        """The standings-so-far while the tournament is still running.

        The shared live projection of :meth:`_live_standings` (the same
        Copeland / scalar ordering the settled :meth:`champion` decision
        uses, with no crowned generation yet). Empty by default — the
        gauntlet has no meaningful pre-result standing.
        """
        return self._live_standings()

    def _pending_round(self) -> RoundRecord | None:
        """The current scheduled-but-unresolved round, or ``None``.

        Default ``None`` (no in-flight round to project — the gauntlet,
        which resolves in one shot, and any resolved strategy). A
        multi-round strategy overrides this to project its pending
        matchup map into a :class:`RoundRecord` whose matches carry
        ``winner=""`` + ``pending=True``.
        """
        return None

    def _live_standings(self) -> tuple[Standing, ...]:
        """The standings-so-far (no crowned generation yet).

        Default empty. A standings-bearing strategy overrides this to
        return its in-progress ranking — typically ``self._standings(None)``
        — so the live envelope shows the leaderboard climb.
        """
        return ()


def pending_match_record(
    match_id: str,
    competitors: tuple[str, ...],
    *,
    bracket_slot: str = "",
    board_fraction: float | None = None,
    live_progress: dict[str, dict[str, Any]] | None = None,
) -> MatchRecord:
    """Build a :class:`MatchRecord` for a scheduled-but-unresolved match.

    The single constructor for an in-flight match across every structure:
    ``winner=""`` (serialised as the contract's ``winner: null``),
    ``decision=""``, ``pending=True``. Centralising it keeps every
    strategy's :meth:`SelectionStrategy._pending_round` emitting the
    identical pending shape.

    ``live_progress`` is the optional authoritative per-lane live-progress
    map (racing rungs supply it; every other structure leaves it empty).
    """
    return MatchRecord(
        match_id=match_id,
        competitors=competitors,
        winner="",
        decision="",
        bracket_slot=bracket_slot,
        board_fraction=board_fraction,
        pending=True,
        live_progress=dict(live_progress) if live_progress else {},
    )


def rung_for_match_id(match_id: str | None) -> str | None:
    """Derive a human-readable rung label from a tournament ``match_id``.

    The per-board-run provenance tag (``LossProfile.match_id``) carries
    the matchup id the run executed within; this projects that id to the
    coarser rung/phase label the dashboard groups runs by. It is a pure
    string projection — no strategy state — so both the analytical index
    reader and the dashboard state reader can call it.

    The mapping (matching the racing structure's ``match_id`` forms):

    * ``"rung0_m2"`` / ``"rung0"`` -> ``"rung 0"``
    * ``"rung1_m0"`` -> ``"rung 1"`` (any ``rung<N>...`` form)
    * ``"racing-final"`` / any ``*-final`` / ``"final"`` -> ``"final"``
    * ``""`` / ``None`` -> ``None`` (an untagged run — a gauntlet duel,
      which never carries a ``match_id``, or a legacy run persisted
      before the tag existed)
    * anything else (bracket slots like ``"WB-R1-0"``, swiss ``"r0_m1"``)
      is returned verbatim so a non-racing structure still gets a stable,
      if un-prettified, label rather than ``None``.

    A gauntlet run is intentionally ``None`` rather than ``"gauntlet"``:
    the gauntlet path runs through ``run_tournament`` and never stamps a
    ``match_id``, so its runs arrive here with ``""`` and read as "no
    rung", which is the honest answer for a single-duel structure.
    """
    if not match_id:
        return None
    mid = match_id.strip()
    if not mid:
        return None
    lo = mid.lower()
    if lo == "final" or lo.endswith("-final"):
        return "final"
    if lo.startswith("rung"):
        # "rung0_m2" / "rung12" -> the leading run of digits after "rung".
        digits = ""
        for ch in lo[len("rung") :]:
            if ch.isdigit():
                digits += ch
            else:
                break
        if digits:
            return f"rung {int(digits)}"
    return mid


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


def _param_opt_float(params: dict[str, Any], key: str) -> float | None:
    """Read an OPTIONAL float param: absent / null / unparseable ⇒ ``None``.

    Unlike :func:`_param_float` there is no scalar default — the field is a
    genuine opt-in switch where "unset" must remain distinguishable from any
    numeric value (used for wall-clock budgets, where ``None`` means "no
    cap"). A non-positive value is also treated as "unset" so ``0`` cannot
    accidentally cap a matchup to nothing.
    """
    raw = params.get(key, None)
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0.0 else None


__all__ = [
    "Contestant",
    "Matchup",
    "MatchupResult",
    "SelectionDecision",
    "Standing",
    "RoundRecord",
    "MatchRecord",
    "SelectionStrategy",
    "pending_match_record",
    "rung_for_match_id",
]
