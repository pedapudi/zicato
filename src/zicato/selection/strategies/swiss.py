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

from zicato.selection.standings_ext import (
    apply_uncertainty_guard,
    rating_order,
    read_rating,
    read_resolver,
    read_uncertainty_threshold,
    resolver_leader,
)
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
        self._stage_index = 0
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
        # Opt-in rating / resolver / uncertainty-guard knobs (absent ⇒
        # today's Copeland/scalar behaviour, byte-identical). These only
        # ever re-order the INTERNAL standings / leader pick and add a
        # promotion-blocking defer — never the gate.
        self._rating = read_rating(self.params)
        self._resolver = read_resolver(self.params)
        self._uncertainty_threshold = read_uncertainty_threshold(self.params)

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
        if self._stage_index >= self._rounds_n:
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
                mid = f"r{self._stage_index}_m{slot}"
                self._pending[mid] = (left, right)
                matchups.append(
                    Matchup(
                        matchup_id=mid,
                        left=left,
                        right=right,
                        replicates=self._replicates,
                        stage_index=self._stage_index,
                    )
                )
                i += 2
            else:
                # Bye for the odd contestant: a free Copeland point.
                bye_id = ordered[i]
                self._copeland[bye_id] = self._copeland.get(bye_id, 0) + 1
                self._round_matches.append(
                    MatchRecord(
                        match_id=f"r{self._stage_index}_m{slot}",
                        competitors=(bye_id,),
                        winner=bye_id,
                        bye=True,
                    )
                )
                i += 1
            slot += 1
        self._scheduled_round = self._stage_index
        return matchups

    def _standing_order(self) -> list[str]:
        ids = [c.generation_id for c in self._field]
        if self._rating == "bradley_terry":
            return self._theta_standing_order(ids)
        ids.sort(
            key=lambda gid: (
                -self._copeland.get(gid, 0),
                self._mean_scalar(gid),
                gid,
            )
        )
        return ids

    def _theta_standing_order(self, ids: list[str]) -> list[str]:
        """Bradley--Terry theta-rank, falling back to Copeland/scalar.

        When ``rating="bradley_terry"`` the field is ordered by fitted
        latent strength (best-first). Contestants the audit has not yet
        rated (no resolvable duel for them) keep the existing Copeland /
        scalar ordering among themselves and sort *after* every rated
        contestant — so an early-round call (empty audit) is identical to
        the Copeland order, and the theta order takes over as duels land.
        """
        order = rating_order(self._audit)
        if not order:
            ids.sort(key=lambda gid: (-self._copeland.get(gid, 0), self._mean_scalar(gid), gid))
            return ids
        rank = {gid: i for i, gid in enumerate(order)}
        ids.sort(
            key=lambda gid: (
                rank.get(gid, len(order)),
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
        leader_id = self._pick_leader()
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
                stage_index=self._stage_index,
            ),
        )

    def _pick_leader(self) -> str | None:
        """The challenger to face the champion in the crowning duel.

        Default (no ``resolver``): the top non-champion in the standings
        order (Copeland/scalar, or theta-rank when ``rating`` is set) —
        byte-identical to the historical ``next(...)`` pick. When a
        ``resolver`` knob is set, the proposed internal leader comes from the
        resolver over the duel matrix (Smith-prune + Ranked Pairs, or
        Copeland); if the resolver names the champion (or yields nothing),
        fall back to the top non-champion in the standings order. The leader
        is always a non-champion challenger — the resolver only proposes; the
        unchanged champion gate still decides promotion.
        """
        assert self._champion is not None
        champ = self._champion.generation_id
        order = self._standing_order()
        default_leader = next((g for g in order if g != champ), None)
        if self._resolver is None:
            return default_leader
        proposed = resolver_leader(self._audit, self._resolver)
        if proposed is not None and proposed != champ and proposed in self._by_id:
            return proposed
        return default_leader

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
                    stage_index=self._stage_index,
                    label=f"Swiss round {self._stage_index + 1}",
                    matches=tuple(self._round_matches),
                )
            )
            self._round_matches = []
            self._stage_index += 1

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
        assert self._champion is not None
        decision, reason, deferred = apply_uncertainty_guard(
            outcome.decision,
            outcome.reason,
            audit=self._audit,
            parent_id=self._champion.generation_id,
            child_id=self._leader.generation_id,
            threshold=self._uncertainty_threshold,
        )
        promoted = decision == "promoted"
        promoted_id = self._leader.generation_id if promoted else None
        del deferred
        return SelectionDecision(
            promoted_generation_id=promoted_id,
            decision=decision,  # type: ignore[arg-type]
            reason=reason,
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
                    stage_index=self._stage_index,
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

    # -- live (in-flight) projection --------------------------------------

    def _pending_round(self) -> RoundRecord | None:
        # Mid Swiss round: ``_pending`` holds the scheduled pairings and
        # ``_round_matches`` any byes already recorded for the round.
        if self._pending:
            matches = list(self._round_matches)
            for mid, (left, right) in self._pending.items():
                matches.append(
                    pending_match_record(
                        mid,
                        (left.generation_id, right.generation_id),
                    )
                )
            return RoundRecord(
                stage_index=self._stage_index,
                label=f"Swiss round {self._stage_index + 1}",
                matches=tuple(matches),
            )
        # Champion-gate scheduled but its result has not landed yet.
        if self._final_scheduled and self._final_result is None and self._leader is not None:
            assert self._champion is not None
            return RoundRecord(
                stage_index=self._stage_index,
                label="Champion gate",
                matches=(
                    pending_match_record(
                        self._final_match_id,
                        (self._champion.generation_id, self._leader.generation_id),
                    ),
                ),
            )
        return None

    def _live_standings(self) -> tuple[Standing, ...]:
        return self._standings(None)


__all__ = ["SwissStrategy"]
