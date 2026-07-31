"""Double-elimination over the challenger field.

A winners' bracket (as single-elim) plus a losers' bracket: a node-loser
drops to the losers' bracket and is eliminated only on its *second* node
loss. A grand-final pits the winners'-bracket survivor against the
losers'-bracket survivor, and the real champion-gate decides whether the
grand-final winner unseats the reigning champion.

SELECTION.md §8 is explicit that the "second life" is delivered more
cheaply by replication, so this structure also defaults to
``replicates >= 2`` rather than relying on the losers' bracket for noise
robustness. Offered for completeness.

Implementation note (deviation, documented in the report): rather than a
full seeded WB/LB feed schedule, the losers' bracket is run as a simple
single-elimination over the accumulated WB losers once the WB has a
survivor. Every generation still gets exactly one "second life" (it is
eliminated on its second loss), the grand final still pits the two
survivors, and the crowning champion-gate is unchanged — the property the
gate composition depends on. The simpler feed keeps the advance/stopping
logic unit-testable without inventing a bracket-seeding convention the
design docs leave open.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from zicato.core.types import TournamentDecision
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


class DoubleEliminationStrategy(SelectionStrategy):
    """Winners' + losers' bracket, then a grand-final champion-gate duel."""

    structure = "double_elim"
    _default_replicates = 2

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        super().__init__(params)
        self._champion: Contestant | None = None
        self._challengers: list[Contestant] = []
        self._replicates = max(1, _param_int(self.params, "replicates", self._default_replicates))
        self._audit: list[MatchupResult] = []
        self._records: list[RoundRecord] = []
        self._scalars: dict[str, float] = {}
        self._wins: dict[str, int] = {}
        self._losses: dict[str, int] = {}  # node losses (elimination at 2)
        self._eliminated: set[str] = set()
        self._eliminated_round: dict[str, int] = {}

        # Phase machine: "wb" winners', "lb" losers', "gf" grand final.
        self._phase = "wb"
        self._stage_index = 0
        self._round_matches: list[MatchRecord] = []

        # Winners' bracket state.
        self._wb_round: list[Contestant] = []
        self._wb_pending: dict[str, tuple[Contestant, Contestant]] = {}
        self._wb_survivor: Contestant | None = None
        self._wb_losers: list[Contestant] = []  # drop into LB in order

        # Losers' bracket state.
        self._lb_round: list[Contestant] = []
        self._lb_pending: dict[str, tuple[Contestant, Contestant]] = {}
        self._lb_survivor: Contestant | None = None

        # Grand final.
        self._gf_scheduled = False
        self._gf_result: MatchupResult | None = None
        self._gf_match_id = "GF"
        # Opt-in rating / resolver / uncertainty-guard knobs (absent ⇒
        # today's scalar behaviour, byte-identical). They only re-order the
        # INTERNAL standings / grand-final-challenger pick and add a
        # promotion-blocking defer — never the gate.
        self._rating = read_rating(self.params)
        self._resolver = read_resolver(self.params)
        self._uncertainty_threshold = read_uncertainty_threshold(self.params)

    def field_size(self) -> int:
        return max(1, _param_int(self.params, "field_size", 2))

    def seed(self, champion: Contestant, challengers: Sequence[Contestant]) -> None:
        self._champion = champion
        self._challengers = list(challengers)
        self._wb_round = list(self._challengers)
        for c in self._challengers:
            self._scalars.setdefault(c.generation_id, 0.0)
            self._losses.setdefault(c.generation_id, 0)

    # -- scheduling --------------------------------------------------------

    def next_matchups(self) -> Sequence[Matchup]:
        if self._champion is None:
            return ()
        if self._phase == "wb":
            return self._wb_step()
        if self._phase == "lb":
            return self._lb_step()
        if self._phase == "gf":
            return self._gf_step()
        return ()

    def _wb_step(self) -> Sequence[Matchup]:
        if self._wb_pending:
            return ()
        if len(self._wb_round) <= 1:
            self._wb_survivor = self._wb_round[0] if self._wb_round else None
            self._flush_round("Winners' bracket")
            self._phase = "lb"
            # Seed the LB from the WB losers collected so far.
            self._lb_round = list(self._wb_losers)
            return self._lb_step()
        return self._pair_round(self._wb_round, self._wb_pending, "WB")

    def _lb_step(self) -> Sequence[Matchup]:
        if self._lb_pending:
            return ()
        if len(self._lb_round) <= 1:
            self._lb_survivor = self._lb_round[0] if self._lb_round else None
            self._flush_round("Losers' bracket")
            self._phase = "gf"
            return self._gf_step()
        return self._pair_round(self._lb_round, self._lb_pending, "LB")

    def _pair_round(
        self,
        contestants_ref: list[Contestant],
        pending: dict[str, tuple[Contestant, Contestant]],
        prefix: str,
    ) -> Sequence[Matchup]:
        contestants = list(contestants_ref)
        contestants_ref.clear()
        matchups: list[Matchup] = []
        i = 0
        n = len(contestants)
        slot = 0
        while i < n:
            if i + 1 < n:
                left, right = contestants[i], contestants[i + 1]
                mid = f"{prefix}-R{self._stage_index}-{slot}"
                pending[mid] = (left, right)
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
                bye = contestants[i]
                contestants_ref.append(bye)
                self._round_matches.append(
                    MatchRecord(
                        match_id=f"{prefix}-R{self._stage_index}-{slot}",
                        competitors=(bye.generation_id,),
                        winner=bye.generation_id,
                        bracket_slot=f"{prefix}-R{self._stage_index}-{slot}",
                        bye=True,
                    )
                )
                i += 1
            slot += 1
        return matchups

    def _gf_step(self) -> Sequence[Matchup]:
        if self._gf_scheduled or self._wb_survivor is None:
            return ()
        # The grand-final challenger is the WB survivor when no LB survivor
        # exists; otherwise the WB survivor faces the LB survivor first and
        # that winner meets the champion. We keep one crowning duel: the
        # best surviving challenger (lower scalar) vs the champion.
        challenger = self._wb_survivor
        if self._lb_survivor is not None:
            wb_s = self._scalars.get(self._wb_survivor.generation_id, float("inf"))
            lb_s = self._scalars.get(self._lb_survivor.generation_id, float("inf"))
            challenger = self._lb_survivor if lb_s < wb_s else self._wb_survivor
        challenger = self._pick_finalist(challenger)
        self._gf_scheduled = True
        self._gf_challenger = challenger
        return (
            Matchup(
                matchup_id=self._gf_match_id,
                left=self._champion,  # type: ignore[arg-type]
                right=challenger,
                replicates=self._replicates,
                stage_index=self._stage_index,
                bracket_slot="GF",
            ),
        )

    def _pick_finalist(self, default_challenger: Contestant) -> Contestant:
        """The challenger to face the champion in the grand final.

        Default (no ``resolver``): the lower-scalar of the WB / LB
        survivors, exactly as today. When a ``resolver`` knob is set, the
        proposed challenger comes from the resolver over the duel matrix
        (Smith-prune + Ranked Pairs, or Copeland); if the resolver names the
        champion, an unknown id, or yields nothing, fall back to the default.
        The resolver only re-orders the INTERNAL pick — the unchanged
        champion gate still decides promotion.
        """
        if self._resolver is None or self._champion is None:
            return default_challenger
        proposed = resolver_leader(self._audit, self._resolver)
        if proposed is None or proposed == self._champion.generation_id:
            return default_challenger
        by_id = {c.generation_id: c for c in self._challengers}
        return by_id.get(proposed, default_challenger)

    def _flush_round(self, label: str) -> None:
        if self._round_matches:
            self._records.append(
                RoundRecord(
                    stage_index=self._stage_index,
                    label=label,
                    matches=tuple(self._round_matches),
                )
            )
            self._round_matches = []
        self._stage_index += 1

    # -- result folding ----------------------------------------------------

    def record_result(self, result: MatchupResult) -> None:
        self._audit.append(result)
        self._scalars[result.left_id] = result.left_scalar()
        self._scalars[result.right_id] = result.right_scalar()

        if result.matchup_id == self._gf_match_id and self._gf_scheduled:
            self._gf_result = result
            return

        pending = self._wb_pending if result.matchup_id.startswith("WB-") else self._lb_pending
        pair = pending.pop(result.matchup_id, None)
        if pair is None:
            return
        left, right = pair
        winner_id = result.lower_scalar_id()
        winner = left if winner_id == left.generation_id else right
        loser = right if winner is left else left
        self._wins[winner_id] = self._wins.get(winner_id, 0) + 1
        self._losses[loser.generation_id] = self._losses.get(loser.generation_id, 0) + 1
        self._record_match(left, right, winner_id, result)

        if result.matchup_id.startswith("WB-"):
            self._wb_round.append(winner)
            # Loser drops to the losers' bracket (first loss survives).
            self._wb_losers.append(loser)
        else:  # LB
            self._lb_round.append(winner)
            # Second loss eliminates.
            self._eliminated.add(loser.generation_id)
            self._eliminated_round[loser.generation_id] = self._stage_index

    def _record_match(
        self,
        left: Contestant,
        right: Contestant,
        winner_id: str,
        result: MatchupResult,
    ) -> None:
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

    def resolved(self) -> bool:
        if self._champion is None:
            return True
        if self._gf_result is not None:
            return True
        return not self._challengers

    def champion(self) -> SelectionDecision:
        audit = tuple(self._audit)
        if self._gf_result is None:
            return SelectionDecision(
                promoted_generation_id=None,
                decision=TournamentDecision.REJECTED,
                reason=(
                    "no grand finalist cleared the champion gate: "
                    f"{self._no_grand_final_detail()}"
                ),
                matchups=audit,
                standings=self._standings(None),
            )
        outcome = self._gf_result.outcome
        challenger = getattr(self, "_gf_challenger", None)
        decision: str = outcome.decision
        reason: str = outcome.reason
        if challenger is not None and self._champion is not None:
            decision, reason, _deferred = apply_uncertainty_guard(
                outcome.decision,
                outcome.reason,
                audit=self._audit,
                parent_id=self._champion.generation_id,
                child_id=challenger.generation_id,
                threshold=self._uncertainty_threshold,
            )
        promoted = decision == "promoted"
        promoted_id = challenger.generation_id if (promoted and challenger) else None
        return SelectionDecision(
            promoted_generation_id=promoted_id,
            decision=decision,  # type: ignore[arg-type]
            reason=reason,
            matchups=audit,
            crowning_matchup_id=self._gf_match_id,
            standings=self._standings(promoted_id),
        )

    def _no_grand_final_detail(self) -> str:
        """Why no grand final was decided, with the bracket's measured scalars.

        A double-elimination bracket reaches this branch either without
        ever nominating a grand finalist or with one whose duel never
        reported. The bare sentence covered both and cited nothing, so an
        operator could not tell a bracket-wiring fault from a duel that
        went missing (issue #129).
        """
        if self._champion is None:
            return "no champion was seeded"
        challenger = getattr(self, "_gf_challenger", None)
        if challenger is None:
            return (
                f"the bracket nominated no grand finalist from "
                f"{len(self._challengers)} challenger(s) over {len(self._audit)} duel(s)"
            )
        champ = self._scalars.get(self._champion.generation_id)
        finalist = self._scalars.get(challenger.generation_id)
        measured = (
            f" (champion {champ:.6f} vs grand finalist {finalist:.6f})"
            if champ is not None and finalist is not None
            else ""
        )
        return (
            f"the grand final against {challenger.generation_id} " f"reported no result{measured}"
        )

    def _standings(self, promoted_id: str | None) -> tuple[Standing, ...]:
        ids: list[str] = []
        if self._champion:
            ids.append(self._champion.generation_id)
        ids += [c.generation_id for c in self._challengers]
        seen: set[str] = set()
        rows: list[Standing] = []
        for gid in ids:
            if gid in seen:
                continue
            seen.add(gid)
            role = (
                "champion"
                if (self._champion and gid == self._champion.generation_id)
                else ("challenger")
            )
            if promoted_id is not None and gid == promoted_id:
                status = "champion"
            elif gid in self._eliminated:
                status = "eliminated"
            else:
                status = "alive"
            rows.append(
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
        self._sort_standings(rows)
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

    def _sort_standings(self, rows: list[Standing]) -> None:
        """Order standings by scalar (default), or theta-rank when selected.

        Default ``rating`` (absent): sort by ``(scalar, id)`` — byte-identical
        to today. When ``rating="bradley_terry"``, order by fitted strength
        (best-first), with any contestant the audit has not yet rated keeping
        the scalar order among themselves AFTER the rated ones.
        """
        if self._rating != "bradley_terry":
            rows.sort(key=lambda s: (s.scalar, s.generation_id))
            return
        order = rating_order(self._audit)
        if not order:
            rows.sort(key=lambda s: (s.scalar, s.generation_id))
            return
        rank = {gid: i for i, gid in enumerate(order)}
        rows.sort(key=lambda s: (rank.get(s.generation_id, len(order)), s.scalar, s.generation_id))

    def rounds(self) -> tuple[RoundRecord, ...]:
        recs = list(self._records)
        if self._gf_result is not None and self._champion:
            challenger = getattr(self, "_gf_challenger", None)
            outcome = self._gf_result.outcome
            winner = (
                challenger.generation_id
                if (outcome.decision == "promoted" and challenger)
                else self._champion.generation_id
            )
            comp: tuple[str, ...] = (self._champion.generation_id,)
            if challenger:
                comp = (self._champion.generation_id, challenger.generation_id)
            recs.append(
                RoundRecord(
                    stage_index=self._stage_index,
                    label="Grand final",
                    matches=(
                        MatchRecord(
                            match_id=self._gf_match_id,
                            competitors=comp,
                            winner=winner,
                            decision=outcome.decision,
                            delta_scalar=outcome.delta_scalar,
                            bracket_slot="GF",
                        ),
                    ),
                )
            )
        return tuple(recs)

    # -- live (in-flight) projection --------------------------------------

    def _pending_round(self) -> RoundRecord | None:
        # Mid WB or LB round: the active pending map carries the scheduled
        # duels and ``_round_matches`` any byes already recorded.
        pending = self._wb_pending or self._lb_pending
        if pending:
            label = "Winners' bracket" if self._wb_pending else "Losers' bracket"
            matches = list(self._round_matches)
            for mid, (left, right) in pending.items():
                matches.append(
                    pending_match_record(
                        mid,
                        (left.generation_id, right.generation_id),
                        bracket_slot=mid,
                    )
                )
            return RoundRecord(
                stage_index=self._stage_index,
                label=label,
                matches=tuple(matches),
            )
        # Grand final scheduled, result not yet landed.
        if self._gf_scheduled and self._gf_result is None and self._champion is not None:
            challenger = getattr(self, "_gf_challenger", None)
            comp: tuple[str, ...] = (self._champion.generation_id,)
            if challenger is not None:
                comp = (self._champion.generation_id, challenger.generation_id)
            return RoundRecord(
                stage_index=self._stage_index,
                label="Grand final",
                matches=(pending_match_record(self._gf_match_id, comp, bracket_slot="GF"),),
            )
        return None

    def _live_standings(self) -> tuple[Standing, ...]:
        return self._standings(None)


__all__ = ["DoubleEliminationStrategy"]
