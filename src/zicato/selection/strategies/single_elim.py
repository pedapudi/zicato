"""Single-elimination bracket over the challenger field.

The challengers play a single-elimination bracket; the champion enters as
the protected top seed with a bye and meets the bracket survivor in a
final champion-vs-survivor duel that uses the real promote gate. A
challenger-vs-challenger node has no incumbent, so its winner is the side
the gate prefers (the lower scalar); only the final node is a true
three-rule feasibility test against the reigning champion.

``replicates >= 2`` is the recommended default for this structure
(SELECTION.md §2③/§8) — a strong candidate dies to one unlucky run
otherwise.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from zicato.selection.strategy import (
    Contestant,
    MatchRecord,
    Matchup,
    MatchupResult,
    RoundRecord,
    SelectionDecision,
    SelectionStrategy,
    Standing,
    _param_int,
    pending_match_record,
)


class SingleEliminationStrategy(SelectionStrategy):
    """Bracket over challengers, then a final champion-gate duel."""

    structure = "single_elim"
    # Bracket structures default to replicated duels — the noise lever.
    _default_replicates = 2

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        super().__init__(params)
        self._champion: Contestant | None = None
        self._challengers: list[Contestant] = []
        self._replicates = max(1, _param_int(self.params, "replicates", self._default_replicates))
        # Flat audit of every MatchupResult, for SelectionDecision.matchups.
        self._audit: list[MatchupResult] = []
        # Bracket state.
        self._current_round: list[Contestant] = []  # survivors entering the next round
        self._round_index = 0
        self._pending: dict[str, tuple[Contestant, Contestant]] = {}
        self._records: list[RoundRecord] = []
        self._round_matches: list[MatchRecord] = []
        self._scalars: dict[str, float] = {}
        self._wins: dict[str, int] = {}
        self._losses: dict[str, int] = {}
        self._eliminated_round: dict[str, int] = {}
        # Final-phase state.
        self._survivor: Contestant | None = None
        self._final_scheduled = False
        self._final_result: MatchupResult | None = None
        self._final_match_id = ""

    def field_size(self) -> int:
        return max(1, _param_int(self.params, "field_size", 2))

    def seed(self, champion: Contestant, challengers: Sequence[Contestant]) -> None:
        self._champion = champion
        self._challengers = list(challengers)
        # Degenerate to gauntlet when only one challenger is in the field.
        self._current_round = list(self._challengers)
        for c in self._challengers:
            self._scalars.setdefault(c.generation_id, 0.0)

    # -- bracket scheduling ------------------------------------------------

    def next_matchups(self) -> Sequence[Matchup]:
        if self._champion is None:
            return ()
        # Bracket phase: pair up the current round's survivors.
        if self._survivor is None and not self._pending:
            if len(self._current_round) <= 1:
                # The bracket has produced its survivor (or there were 0/1
                # challengers); advance to the final phase.
                self._survivor = self._current_round[0] if self._current_round else None
                self._flush_round()
                return self._maybe_final()
            return self._schedule_round()
        if self._survivor is not None:
            return self._maybe_final()
        return ()

    def _schedule_round(self) -> Sequence[Matchup]:
        matchups: list[Matchup] = []
        contestants = list(self._current_round)
        self._current_round = []  # winners refill this as results land
        i = 0
        n = len(contestants)
        slot = 0
        while i < n:
            if i + 1 < n:
                left, right = contestants[i], contestants[i + 1]
                mid = f"WB-R{self._round_index}-{slot}"
                self._pending[mid] = (left, right)
                matchups.append(
                    Matchup(
                        matchup_id=mid,
                        left=left,
                        right=right,
                        replicates=self._replicates,
                        round_index=self._round_index,
                        bracket_slot=mid,
                    )
                )
                i += 2
            else:
                # Odd one out gets a bye into the next round.
                bye = contestants[i]
                self._current_round.append(bye)
                self._round_matches.append(
                    MatchRecord(
                        match_id=f"WB-R{self._round_index}-{slot}",
                        competitors=(bye.generation_id,),
                        winner=bye.generation_id,
                        bracket_slot=f"WB-R{self._round_index}-{slot}",
                        bye=True,
                    )
                )
                i += 1
            slot += 1
        return matchups

    def _flush_round(self) -> None:
        if self._round_matches:
            self._records.append(
                RoundRecord(
                    round_index=self._round_index,
                    label=f"Round {self._round_index + 1}",
                    matches=tuple(self._round_matches),
                )
            )
            self._round_matches = []
        self._round_index += 1

    def _maybe_final(self) -> Sequence[Matchup]:
        if self._final_scheduled or self._survivor is None:
            return ()
        self._final_scheduled = True
        self._final_match_id = "final"
        return (
            Matchup(
                matchup_id=self._final_match_id,
                left=self._champion,  # type: ignore[arg-type]
                right=self._survivor,
                replicates=self._replicates,
                round_index=self._round_index,
                bracket_slot="final",
            ),
        )

    # -- result folding ----------------------------------------------------

    def record_result(self, result: MatchupResult) -> None:
        self._audit.append(result)
        self._scalars[result.left_id] = result.left_scalar()
        self._scalars[result.right_id] = result.right_scalar()
        if result.matchup_id == self._final_match_id and self._final_scheduled:
            self._final_result = result
            return
        pair = self._pending.pop(result.matchup_id, None)
        if pair is None:
            return
        left, right = pair
        winner_id = result.lower_scalar_id()
        winner = left if winner_id == left.generation_id else right
        loser = right if winner is left else left
        self._wins[winner_id] = self._wins.get(winner_id, 0) + 1
        self._losses[loser.generation_id] = self._losses.get(loser.generation_id, 0) + 1
        self._eliminated_round[loser.generation_id] = self._round_index
        self._current_round.append(winner)
        self._round_matches.append(
            MatchRecord(
                match_id=result.matchup_id,
                competitors=(left.generation_id, right.generation_id),
                winner=winner_id,
                decision=result.outcome.decision,
                delta_scalar=result.outcome.delta_scalar,
                bracket_slot=result.bracket_slot,
            )
        )
        # If every scheduled match of this round has landed, the round is
        # complete; the next next_matchups() call flushes + schedules.
        if not self._pending:
            self._flush_round()

    def resolved(self) -> bool:
        if self._champion is None:
            return True
        if self._final_result is not None:
            return True
        # No challengers at all ⇒ resolved immediately (champion stands).
        return not self._challengers and self._survivor is None and not self._current_round

    def champion(self) -> SelectionDecision:
        all_matchups = self._collect_matchups()
        if self._final_result is None or self._survivor is None or self._champion is None:
            return SelectionDecision(
                promoted_generation_id=None,
                decision="rejected",
                reason="no finalist cleared the champion gate",
                matchups=all_matchups,
                standings=self._standings(None),
            )
        outcome = self._final_result.outcome
        promoted = outcome.decision == "promoted"
        promoted_id = self._survivor.generation_id if promoted else None
        return SelectionDecision(
            promoted_generation_id=promoted_id,
            decision=outcome.decision,
            reason=outcome.reason,
            matchups=all_matchups,
            crowning_matchup_id=self._final_match_id,
            standings=self._standings(promoted_id),
        )

    def _collect_matchups(self) -> tuple[MatchupResult, ...]:
        # ``_audit`` already holds every MatchupResult in record order
        # (including the final), so the flat audit is simply a copy.
        return tuple(self._audit)

    def _standings(self, promoted_id: str | None) -> tuple[Standing, ...]:
        entries: list[Standing] = []
        ids = [self._champion.generation_id] if self._champion else []
        ids += [c.generation_id for c in self._challengers]
        seen: set[str] = set()
        for gid in ids:
            if gid in seen:
                continue
            seen.add(gid)
            status: str = "alive"
            role = (
                "champion"
                if (self._champion and gid == self._champion.generation_id)
                else ("challenger")
            )
            if promoted_id is not None and gid == promoted_id:
                status = "champion"
            elif gid in self._eliminated_round:
                status = "eliminated"
            entries.append(
                Standing(
                    generation_id=gid,
                    rank=0,
                    scalar=self._scalars.get(gid, 0.0),
                    wins=self._wins.get(gid, 0),
                    losses=self._losses.get(gid, 0),
                    status=status,  # type: ignore[arg-type]
                    role=role,  # type: ignore[arg-type]
                )
            )
        entries.sort(key=lambda s: (s.scalar, s.generation_id))
        return tuple(
            Standing(
                generation_id=s.generation_id,
                rank=i + 1,
                scalar=s.scalar,
                wins=s.wins,
                losses=s.losses,
                status=s.status,
                role=s.role,
            )
            for i, s in enumerate(entries)
        )

    def rounds(self) -> tuple[RoundRecord, ...]:
        recs = list(self._records)
        # Append the final as its own round record.
        if self._final_result is not None and self._champion and self._survivor:
            outcome = self._final_result.outcome
            winner = (
                self._survivor.generation_id
                if outcome.decision == "promoted"
                else (self._champion.generation_id)
            )
            recs.append(
                RoundRecord(
                    round_index=self._round_index,
                    label="Final",
                    matches=(
                        MatchRecord(
                            match_id=self._final_match_id,
                            competitors=(
                                self._champion.generation_id,
                                self._survivor.generation_id,
                            ),
                            winner=winner,
                            decision=outcome.decision,
                            delta_scalar=outcome.delta_scalar,
                            bracket_slot="final",
                        ),
                    ),
                )
            )
        return tuple(recs)

    # -- live (in-flight) projection --------------------------------------

    def _pending_round(self) -> RoundRecord | None:
        # Mid bracket round: ``_pending`` holds the scheduled duels and
        # ``_round_matches`` any byes already recorded for this round.
        if self._pending:
            matches = list(self._round_matches)
            for mid, (left, right) in self._pending.items():
                matches.append(
                    pending_match_record(
                        mid,
                        (left.generation_id, right.generation_id),
                        bracket_slot=mid,
                    )
                )
            return RoundRecord(
                round_index=self._round_index,
                label=f"Round {self._round_index + 1}",
                matches=tuple(matches),
            )
        # Final scheduled, result not yet landed.
        if self._final_scheduled and self._final_result is None and self._survivor is not None:
            assert self._champion is not None
            return RoundRecord(
                round_index=self._round_index,
                label="Final",
                matches=(
                    pending_match_record(
                        self._final_match_id,
                        (self._champion.generation_id, self._survivor.generation_id),
                        bracket_slot="final",
                    ),
                ),
            )
        return None

    def _live_standings(self) -> tuple[Standing, ...]:
        return self._standings(None)


__all__ = ["SingleEliminationStrategy"]
