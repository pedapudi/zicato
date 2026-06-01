"""Swiss-style tournament over the champion + challenger field.

``rounds_n`` Swiss rounds; each round pairs generations of near-equal
standing into duels (the champion participates as a contestant). Standing
is the Copeland score (duels won), tie-broken by mean scalar. No
elimination — every contestant plays every round (a bye when the field is
odd). After the final round, the top-standing generation is crowned ONLY
if it clears the real champion-gate against the reigning champion (so a
Swiss winner that does not actually beat the incumbent is not promoted).

Maps to Copeland identification (SELECTION.md §6.2); Swiss is
non-adaptive racing (§7). Per-pairing ``replicates >= 2`` is how it earns
noise robustness.
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
)


class SwissStrategy(SelectionStrategy):
    """Fixed-round Swiss, then a champion-gate confirmation of the leader."""

    structure = "swiss"
    _default_replicates = 2

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        super().__init__(params)
        self._champion: Contestant | None = None
        self._field: list[Contestant] = []  # champion + challengers
        self._by_id: dict[str, Contestant] = {}
        self._rounds_n = max(1, _param_int(self.params, "rounds_n", 4))
        self._replicates = max(1, _param_int(self.params, "replicates", self._default_replicates))
        self._round_index = 0
        self._pending: dict[str, tuple[Contestant, Contestant]] = {}
        self._copeland: dict[str, int] = {}
        self._scalar_sum: dict[str, float] = {}
        self._scalar_n: dict[str, int] = {}
        self._records: list[RoundRecord] = []
        self._round_matches: list[MatchRecord] = []
        self._audit: list[MatchupResult] = []
        self._scheduled_round = -1  # last round we emitted pairings for
        # Final champion-gate confirmation.
        self._final_scheduled = False
        self._final_result: MatchupResult | None = None
        self._final_match_id = "swiss-final"
        self._leader: Contestant | None = None

    def field_size(self) -> int:
        return max(1, _param_int(self.params, "field_size", 2))

    def seed(self, champion: Contestant, challengers: Sequence[Contestant]) -> None:
        self._champion = champion
        self._field = [champion, *challengers]
        for c in self._field:
            self._by_id[c.generation_id] = c
            self._copeland.setdefault(c.generation_id, 0)
            self._scalar_sum.setdefault(c.generation_id, 0.0)
            self._scalar_n.setdefault(c.generation_id, 0)

    # -- scheduling --------------------------------------------------------

    def next_matchups(self) -> Sequence[Matchup]:
        if self._champion is None:
            return ()
        # Still resolving the current Swiss round.
        if self._pending:
            return ()
        # All Swiss rounds played → schedule the champion-gate confirmation.
        if self._round_index >= self._rounds_n:
            return self._maybe_final()
        return self._schedule_swiss_round()

    def _schedule_swiss_round(self) -> Sequence[Matchup]:
        ordered = self._standing_order()
        matchups: list[Matchup] = []
        i = 0
        slot = 0
        n = len(ordered)
        while i < n:
            if i + 1 < n:
                left = self._by_id[ordered[i]]
                right = self._by_id[ordered[i + 1]]
                mid = f"r{self._round_index}_m{slot}"
                self._pending[mid] = (left, right)
                matchups.append(
                    Matchup(
                        matchup_id=mid,
                        left=left,
                        right=right,
                        replicates=self._replicates,
                        round_index=self._round_index,
                    )
                )
                i += 2
            else:
                # Bye for the odd contestant: a free Copeland point.
                bye_id = ordered[i]
                self._copeland[bye_id] = self._copeland.get(bye_id, 0) + 1
                self._round_matches.append(
                    MatchRecord(
                        match_id=f"r{self._round_index}_m{slot}",
                        competitors=(bye_id,),
                        winner=bye_id,
                        bye=True,
                    )
                )
                i += 1
            slot += 1
        self._scheduled_round = self._round_index
        return matchups

    def _standing_order(self) -> list[str]:
        ids = [c.generation_id for c in self._field]
        ids.sort(
            key=lambda gid: (
                -self._copeland.get(gid, 0),
                self._mean_scalar(gid),
                gid,
            )
        )
        return ids

    def _mean_scalar(self, gid: str) -> float:
        n = self._scalar_n.get(gid, 0)
        return self._scalar_sum.get(gid, 0.0) / n if n else 0.0

    def _maybe_final(self) -> Sequence[Matchup]:
        if self._final_scheduled or self._champion is None:
            return ()
        order = self._standing_order()
        leader_id = next((g for g in order if g != self._champion.generation_id), None)
        if leader_id is None:
            # No challenger; champion stands.
            self._final_scheduled = True
            return ()
        self._leader = self._by_id[leader_id]
        self._final_scheduled = True
        return (
            Matchup(
                matchup_id=self._final_match_id,
                left=self._champion,
                right=self._leader,
                replicates=self._replicates,
                round_index=self._round_index,
            ),
        )

    # -- result folding ----------------------------------------------------

    def record_result(self, result: MatchupResult) -> None:
        self._audit.append(result)
        self._tally_scalar(result.left_id, result.left_scalar())
        self._tally_scalar(result.right_id, result.right_scalar())

        if result.matchup_id == self._final_match_id and self._final_scheduled:
            self._final_result = result
            return

        pair = self._pending.pop(result.matchup_id, None)
        if pair is None:
            return
        left, right = pair
        winner_id = result.lower_scalar_id()
        self._copeland[winner_id] = self._copeland.get(winner_id, 0) + 1
        self._round_matches.append(
            MatchRecord(
                match_id=result.matchup_id,
                competitors=(left.generation_id, right.generation_id),
                winner=winner_id,
                decision=result.outcome.decision,
                delta_scalar=result.outcome.delta_scalar,
            )
        )
        # When the round's pairings have all landed, close the round.
        if not self._pending:
            self._records.append(
                RoundRecord(
                    round_index=self._round_index,
                    label=f"Swiss round {self._round_index + 1}",
                    matches=tuple(self._round_matches),
                )
            )
            self._round_matches = []
            self._round_index += 1

    def _tally_scalar(self, gid: str, scalar: float) -> None:
        self._scalar_sum[gid] = self._scalar_sum.get(gid, 0.0) + scalar
        self._scalar_n[gid] = self._scalar_n.get(gid, 0) + 1

    def resolved(self) -> bool:
        if self._champion is None:
            return True
        if self._final_result is not None:
            return True
        # Resolved without a final only when there is no challenger leader.
        return self._final_scheduled and self._leader is None

    def champion(self) -> SelectionDecision:
        audit = tuple(self._audit)
        if self._final_result is None or self._leader is None:
            return SelectionDecision(
                promoted_generation_id=None,
                decision="rejected",
                reason="swiss leader did not clear the champion gate",
                matchups=audit,
                standings=self._standings(None),
            )
        outcome = self._final_result.outcome
        promoted = outcome.decision == "promoted"
        promoted_id = self._leader.generation_id if promoted else None
        return SelectionDecision(
            promoted_generation_id=promoted_id,
            decision=outcome.decision,
            reason=outcome.reason,
            matchups=audit,
            crowning_matchup_id=self._final_match_id,
            standings=self._standings(promoted_id),
        )

    def _standings(self, promoted_id: str | None) -> tuple[Standing, ...]:
        rows: list[Standing] = []
        for gid in self._standing_order():
            c = self._by_id[gid]
            role = (
                "champion"
                if (self._champion and gid == self._champion.generation_id)
                else ("challenger")
            )
            status = "champion" if (promoted_id is not None and gid == promoted_id) else "alive"
            rows.append(
                Standing(
                    generation_id=gid,
                    rank=0,
                    scalar=self._mean_scalar(gid),
                    wins=self._copeland.get(gid, 0),
                    losses=0,
                    status=status,  # type: ignore[arg-type]
                    role=role,  # type: ignore[arg-type]
                )
            )
            del c
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
            for i, s in enumerate(rows)
        )

    def rounds(self) -> tuple[RoundRecord, ...]:
        recs = list(self._records)
        if self._final_result is not None and self._champion and self._leader:
            outcome = self._final_result.outcome
            winner = (
                self._leader.generation_id
                if outcome.decision == "promoted"
                else (self._champion.generation_id)
            )
            recs.append(
                RoundRecord(
                    round_index=self._round_index,
                    label="Champion gate",
                    matches=(
                        MatchRecord(
                            match_id=self._final_match_id,
                            competitors=(
                                self._champion.generation_id,
                                self._leader.generation_id,
                            ),
                            winner=winner,
                            decision=outcome.decision,
                            delta_scalar=outcome.delta_scalar,
                        ),
                    ),
                )
            )
        return tuple(recs)


__all__ = ["SwissStrategy"]
