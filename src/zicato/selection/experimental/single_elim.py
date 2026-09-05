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

from zicato.selection.standings_ext import (
    read_rating,
    read_resolver,
)
from zicato.selection.strategies.champion_gate import ChampionGateStrategy
from zicato.selection.strategy import (
    Contestant,
    MatchRecord,
    Matchup,
    MatchupResult,
    RoundRecord,
    pending_match_record,
)


class SingleEliminationStrategy(ChampionGateStrategy):
    """Bracket over challengers, then a final champion-gate duel."""

    structure = "single_elim"
    # Replicated duels — the noise lever, and the base default too.
    _default_replicates = 2
    _final_match_id = "final"
    _final_label = "Final"
    _final_bracket_slot = "final"

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        super().__init__(params)
        # Bracket state.
        self._current_round: list[Contestant] = []  # survivors entering the next round
        self._stage_index = 0
        self._pending: dict[str, tuple[Contestant, Contestant]] = {}
        self._round_matches: list[MatchRecord] = []
        self._eliminated_round: dict[str, int] = {}
        self._survivor: Contestant | None = None
        # Opt-in rating / resolver knobs (absent ⇒ the scalar behaviour,
        # byte-identical). They only re-order the INTERNAL standings /
        # survivor pick — never the gate.
        self._rating = read_rating(self.params)
        self._resolver = read_resolver(self.params)

    def seed(self, champion: Contestant, challengers: Sequence[Contestant]) -> None:
        super().seed(champion, challengers)
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
                mid = f"WB-R{self._stage_index}-{slot}"
                self._pending[mid] = (left, right)
                matchups.append(
                    Matchup(
                        matchup_id=mid,
                        left=left,
                        right=right,
                        replicates=self._replicates,
                        stage_index=self._stage_index,
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
                        match_id=f"WB-R{self._stage_index}-{slot}",
                        competitors=(bye.generation_id,),
                        winner=bye.generation_id,
                        bracket_slot=f"WB-R{self._stage_index}-{slot}",
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
                    stage_index=self._stage_index,
                    # "Bracket round" (not bare "Round") so the within-tournament
                    # stage never reads as the outer evolution round on the
                    # dashboard, where a candidate's birth ROUND is the page.
                    label=f"Bracket round {self._stage_index + 1}",
                    matches=tuple(self._round_matches),
                )
            )
            self._round_matches = []
        self._stage_index += 1

    def _maybe_final(self) -> Sequence[Matchup]:
        if self._final_scheduled or self._survivor is None:
            return ()
        self._survivor = self._pick_finalist(self._survivor)
        self._final_scheduled = True
        return (
            Matchup(
                matchup_id=self._final_match_id,
                left=self._champion,  # type: ignore[arg-type]
                right=self._survivor,
                replicates=self._replicates,
                stage_index=self._final_stage_index(),
                bracket_slot=self._final_bracket_slot,
            ),
        )

    def _finalist(self) -> Contestant | None:
        return self._survivor

    def _final_stage_index(self) -> int:
        return self._stage_index

    # -- result folding ----------------------------------------------------

    def record_result(self, result: MatchupResult) -> None:
        self._audit.append(result)
        self._scalars[result.left_id] = result.left_scalar()
        self._scalars[result.right_id] = result.right_scalar()
        if self._capture_final_result(result):
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
        self._eliminated_round[loser.generation_id] = self._stage_index
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

    def _no_promotion_reason(self) -> str:
        return f"no finalist cleared the champion gate: {self._no_final_detail()}"

    def _no_final_detail(self) -> str:
        """Why no final was decided, with whatever the bracket measured.

        Three distinct situations shared one sentence: a bracket that
        never seeded, one that ran duels but produced no finalist, and a
        final that was scheduled and never reported. Only the third means
        the finalist LOST to the champion, and the first two are wiring
        faults — so the reason names which one it is and, where a finalist
        exists, the two scalars the crowning duel would have compared
        (issue #129).
        """
        if self._champion is None:
            return "no champion was seeded"
        if self._survivor is None:
            return (
                f"the bracket produced no finalist from {len(self._challengers)} "
                f"challenger(s) over {len(self._audit)} duel(s)"
            )
        champ = self._scalars.get(self._champion.generation_id)
        finalist = self._scalars.get(self._survivor.generation_id)
        measured = (
            f" (champion {champ:.6f} vs finalist {finalist:.6f})"
            if champ is not None and finalist is not None
            else ""
        )
        return (
            f"the final against finalist {self._survivor.generation_id} "
            f"reported no result{measured}"
        )

    def _is_eliminated(self, gid: str) -> bool:
        # One node loss ends a challenger's run in a single-elimination bracket.
        return gid in self._eliminated_round

    # -- live (in-flight) projection --------------------------------------

    def _pending_stage_round(self) -> RoundRecord | None:
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
                stage_index=self._stage_index,
                label=f"Bracket round {self._stage_index + 1}",
                matches=tuple(matches),
            )
        return None


__all__ = ["SingleEliminationStrategy"]
