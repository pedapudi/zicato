"""The shared base for structures that end in a champion-gate final.

Four of the five tournament structures — single-elimination,
double-elimination, Swiss, and racing — differ only in how they narrow a
field of challengers down to one. Each then ends the same way: a single
crowning duel between the reigning champion and the finalist its own stages
produced, decided by the unchanged promote gate
(:func:`zicato.tournament.gate.evaluate_gate`), after which the tournament
resolves.

:class:`ChampionGateStrategy` owns that ending. It holds the champion, the
challenger field, the flat audit, the settled round records, and the crowning
duel's result, and it renders every view built on them: the settled rounds,
the in-flight round, the live standings, and the crowned
:class:`~zicato.selection.strategy.SelectionDecision`. A subclass supplies
its own stage bookkeeping — how it pairs, cuts, and advances the field — plus
four descriptions of its final: the round label it is recorded under, the
contestant that reached it, the bracket slot it occupies, and the
within-tournament stage index it sits at.

The gauntlet is not a subclass. It schedules exactly one duel, which IS the
champion gate, so it has no field to narrow and no final to describe apart
from the tournament itself.
"""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Sequence
from dataclasses import replace
from typing import Any, ClassVar, Literal

from zicato.core.types import TournamentDecision
from zicato.selection.standings_ext import (
    apply_uncertainty_guard,
    rating_order,
    resolver_leader,
)
from zicato.selection.strategy import (
    Contestant,
    MatchRecord,
    MatchupResult,
    RoundRecord,
    SelectionDecision,
    SelectionStrategy,
    Standing,
    _param_int,
    pending_match_record,
)


class ChampionGateStrategy(SelectionStrategy):
    """A field structure whose stages end in one duel against the champion."""

    #: The matchup id of the crowning duel. Constant per structure, so the
    #: id a result is matched against is the id the duel was scheduled under.
    _final_match_id: ClassVar[str]
    #: The label of the :class:`RoundRecord` the crowning duel is recorded
    #: under, settled and in-flight alike.
    _final_label: ClassVar[str]
    #: The crowning duel's bracket position. Empty for a structure with no
    #: bracket (Swiss, racing).
    _final_bracket_slot: ClassVar[str] = ""
    #: The share of the board the crowning duel runs, for a structure whose
    #: earlier stages run slices of it (racing). ``None`` leaves the match
    #: record's ``board_fraction`` unset, which is what a structure that
    #: always runs the whole board reports.
    _final_board_fraction: ClassVar[float | None] = None

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        super().__init__(params)
        self._champion: Contestant | None = None
        self._challengers: list[Contestant] = []
        self._replicates = max(1, _param_int(self.params, "replicates", self._default_replicates))
        # Every MatchupResult in record order, including the crowning duel.
        self._audit: list[MatchupResult] = []
        # The rounds the structure has closed, oldest first.
        self._records: list[RoundRecord] = []
        self._final_scheduled = False
        self._final_result: MatchupResult | None = None
        # The duel tallies the default standings read: each contestant's
        # last measured scalar, and its counts of node wins and losses.
        self._scalars: dict[str, float] = {}
        self._wins: dict[str, int] = {}
        self._losses: dict[str, int] = {}
        # The opt-in standings knobs. A structure that accepts them assigns
        # them in its own ``__init__`` from ``read_rating`` / ``read_resolver``
        # / ``read_uncertainty_threshold``; one that does not accept them
        # leaves all three ``None``, which is the scalar behaviour these
        # templates take by default.
        self._rating: str | None = None
        self._resolver: str | None = None
        self._uncertainty_threshold: float | None = None

    def field_size(self) -> int:
        """How many challengers the proposer must emit this round."""
        return max(1, _param_int(self.params, "field_size", 2))

    def seed(self, champion: Contestant, challengers: Sequence[Contestant]) -> None:
        """Record the champion and the applied field.

        A subclass extends this with its own stage state (the first bracket
        round, the Swiss field, the racing rungs) and calls up first.
        """
        self._champion = champion
        self._challengers = list(challengers)

    # -- the crowning duel -------------------------------------------------

    @abstractmethod
    def _finalist(self) -> Contestant | None:
        """The contestant the structure's stages sent to the crowning duel.

        ``None`` until the stages nominate one, and for a field that never
        produced a challenger to nominate.
        """

    @abstractmethod
    def _final_stage_index(self) -> int:
        """The within-tournament stage index the crowning duel sits at."""

    @abstractmethod
    def _no_promotion_reason(self) -> str:
        """Why the champion stands when no crowning duel decided the round."""

    def _capture_final_result(self, result: MatchupResult) -> bool:
        """Store the crowning duel's result, reporting whether this was it.

        The crowning duel is the one result a structure never folds into its
        stage bookkeeping: it decides promotion instead of advancing the
        field. Every ``record_result`` calls this and returns immediately
        when it answers true.
        """
        if self._final_scheduled and result.matchup_id == self._final_match_id:
            self._final_result = result
            return True
        return False

    def _pick_finalist(self, default: Contestant) -> Contestant:
        """The challenger a set ``resolver`` nominates, else ``default``.

        With no ``resolver`` knob the structure's own pick stands. When one
        is set, the nomination comes from the resolver over the duel matrix
        (Smith-prune plus Ranked Pairs, or Copeland); the champion, an
        unknown id, and an empty result all fall back to ``default``. The
        resolver re-orders this INTERNAL pick alone — the unchanged champion
        gate still decides promotion.
        """
        if self._resolver is None or self._champion is None:
            return default
        proposed = resolver_leader(self._audit, self._resolver)
        if proposed is None or proposed == self._champion.generation_id:
            return default
        by_id = {c.generation_id: c for c in self._challengers}
        return by_id.get(proposed, default)

    def champion(self) -> SelectionDecision:
        """The crowned decision once the tournament has resolved.

        Without a decided crowning duel the champion stands and the reason
        is the structure's own account of why. With one, the verdict is the
        gate's, passed through the opt-in uncertainty guard: a promotion the
        fitted ratings cannot separate from noise becomes a defer, and every
        other verdict is unchanged.
        """
        audit = tuple(self._audit)
        finalist = self._finalist()
        if self._final_result is None or finalist is None or self._champion is None:
            return SelectionDecision(
                promoted_generation_id=None,
                decision=TournamentDecision.REJECTED,
                reason=self._no_promotion_reason(),
                matchups=audit,
                standings=self._standings(None),
            )
        outcome = self._final_result.outcome
        decision, reason, _deferred = apply_uncertainty_guard(
            outcome.decision,
            outcome.reason,
            audit=self._audit,
            parent_id=self._champion.generation_id,
            child_id=finalist.generation_id,
            threshold=self._uncertainty_threshold,
        )
        promoted_id = finalist.generation_id if decision == "promoted" else None
        return SelectionDecision(
            promoted_generation_id=promoted_id,
            decision=decision,  # type: ignore[arg-type]
            reason=reason,
            matchups=audit,
            crowning_matchup_id=self._final_match_id,
            standings=self._standings(promoted_id),
        )

    # -- round records -----------------------------------------------------

    def rounds(self) -> tuple[RoundRecord, ...]:
        """The closed rounds, followed by the decided crowning duel."""
        final = self._final_round_record()
        if final is None:
            return tuple(self._records)
        return (*self._records, final)

    def _final_round_record(self) -> RoundRecord | None:
        """The decided crowning duel as its own round record.

        ``None`` while the duel has not reported, so a tournament that ends
        without one records only its stages.
        """
        finalist = self._finalist()
        if self._final_result is None or self._champion is None or finalist is None:
            return None
        outcome = self._final_result.outcome
        winner = (
            finalist.generation_id
            if outcome.decision == "promoted"
            else self._champion.generation_id
        )
        return RoundRecord(
            stage_index=self._final_stage_index(),
            label=self._final_label,
            matches=(
                MatchRecord(
                    match_id=self._final_match_id,
                    competitors=(self._champion.generation_id, finalist.generation_id),
                    winner=winner,
                    decision=outcome.decision,
                    delta_scalar=outcome.delta_scalar,
                    bracket_slot=self._final_bracket_slot,
                    board_fraction=self._final_board_fraction,
                ),
            ),
        )

    # -- live (in-flight) projection --------------------------------------

    @abstractmethod
    def _pending_stage_round(self) -> RoundRecord | None:
        """The structure's own in-flight round, or ``None`` when none runs.

        Covers the stages the structure schedules itself (a bracket round, a
        Swiss round, a racing rung). The crowning duel is projected by
        :meth:`_pending_final_round`, which runs when this yields ``None``.
        """

    def _pending_round(self) -> RoundRecord | None:
        stage = self._pending_stage_round()
        if stage is not None:
            return stage
        return self._pending_final_round()

    def _pending_final_round(self) -> RoundRecord | None:
        """The scheduled crowning duel while its result has not landed."""
        finalist = self._finalist()
        if not self._final_scheduled or self._final_result is not None or finalist is None:
            return None
        assert self._champion is not None
        return RoundRecord(
            stage_index=self._final_stage_index(),
            label=self._final_label,
            matches=(
                pending_match_record(
                    self._final_match_id,
                    (self._champion.generation_id, finalist.generation_id),
                    bracket_slot=self._final_bracket_slot,
                    board_fraction=self._final_board_fraction,
                ),
            ),
        )

    # -- standings ---------------------------------------------------------

    def _is_eliminated(self, gid: str) -> bool:
        """Whether the structure has knocked ``gid`` out of the field.

        False by default, which is what a structure that never eliminates
        reports: Swiss plays every contestant in every round, so its
        standings stay alive until one is crowned.
        """
        return False

    def _standings(self, promoted_id: str | None) -> tuple[Standing, ...]:
        """The field ranked by its duel tallies, best first.

        One row per contestant — the champion first, then the challengers,
        each appearing once — carrying the contestant's measured scalar, its
        win and loss counts, and its status: crowned when it is
        ``promoted_id``, eliminated when :meth:`_is_eliminated` says the
        structure knocked it out, alive otherwise. :meth:`_sort_standings`
        orders the rows and :meth:`_ranked` numbers them.

        A structure that ranks on something other than these tallies
        overrides this. The Swiss standing does: it ranks on Copeland points
        over mean scalars.
        """
        ids = [self._champion.generation_id] if self._champion else []
        ids += [c.generation_id for c in self._challengers]
        rows: list[Standing] = []
        seen: set[str] = set()
        for gid in ids:
            if gid in seen:
                continue
            seen.add(gid)
            role: Literal["champion", "challenger"] = (
                "champion"
                if self._champion is not None and gid == self._champion.generation_id
                else "challenger"
            )
            status: Literal["alive", "eliminated", "champion"] = "alive"
            if promoted_id is not None and gid == promoted_id:
                status = "champion"
            elif self._is_eliminated(gid):
                status = "eliminated"
            rows.append(
                Standing(
                    generation_id=gid,
                    rank=0,
                    scalar=self._scalars.get(gid, 0.0),
                    wins=self._wins.get(gid, 0),
                    losses=self._losses.get(gid, 0),
                    status=status,
                    role=role,
                )
            )
        self._sort_standings(rows)
        return self._ranked(rows)

    def _live_standings(self) -> tuple[Standing, ...]:
        return self._standings(None)

    def _sort_standings(self, rows: list[Standing]) -> None:
        """Order standings in place, best first.

        Default (no ``rating`` knob): by ``(scalar, id)``. With
        ``rating="bradley_terry"``: by fitted strength, with any contestant
        the audit cannot yet rate sorted after every rated one, keeping the
        scalar order among themselves.
        """
        order = rating_order(self._audit) if self._rating == "bradley_terry" else []
        if not order:
            rows.sort(key=lambda s: (s.scalar, s.generation_id))
            return
        rank = {gid: i for i, gid in enumerate(order)}
        rows.sort(key=lambda s: (rank.get(s.generation_id, len(order)), s.scalar, s.generation_id))

    def _ranked(self, rows: Sequence[Standing]) -> tuple[Standing, ...]:
        """Number an already-ordered standings list from 1, best first."""
        return tuple(replace(row, rank=i + 1) for i, row in enumerate(rows))


__all__ = ["ChampionGateStrategy"]
