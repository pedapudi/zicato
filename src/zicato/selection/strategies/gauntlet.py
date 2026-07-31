"""The gauntlet strategy — the default, today's behaviour exactly.

A faithful re-expression of the historical ``evolve_once`` steps 2-5: one
champion, one challenger, one full-board duel, promote-on-gate. With
``replicates`` pinned to ``1`` this path reproduces the historical
single-run gauntlet outcome byte-for-byte; the UNPINNED default is now
``2`` averaged paired runs (the noise-aware posture — see
``_default_replicates`` on the base class).

Maps to the degenerate single-replicate dueling bandit (SELECTION.md
§6.3).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from zicato.core.types import TournamentDecision
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


class GauntletStrategy(SelectionStrategy):
    """One champion, one challenger, one full-board duel, promote-on-gate."""

    structure = "gauntlet"

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        super().__init__(params)
        self._champion: Contestant | None = None
        self._challenger: Contestant | None = None
        self._result: MatchupResult | None = None
        self._scheduled = False
        # ``replicates`` is the §9-lever-1 knob; the gauntlet inherits the
        # base ``_default_replicates`` (2 — the noise-aware default). Pin
        # ``"replicates": 1`` in the params for the historical single-run
        # duel (deterministic harnesses do).
        self._replicates = max(1, _param_int(self.params, "replicates", self._default_replicates))

    def field_size(self) -> int:
        return 1

    def seed(self, champion: Contestant, challengers: Sequence[Contestant]) -> None:
        self._champion = champion
        if not challengers:
            raise ValueError("gauntlet requires exactly one challenger; got none")
        self._challenger = challengers[0]

    def next_matchups(self) -> Sequence[Matchup]:
        if self._scheduled or self._champion is None or self._challenger is None:
            return ()
        self._scheduled = True
        return (
            Matchup(
                matchup_id="gauntlet",
                left=self._champion,
                right=self._challenger,
                board_subset=None,
                replicates=self._replicates,
                stage_index=0,
            ),
        )

    def record_result(self, result: MatchupResult) -> None:
        self._result = result

    def resolved(self) -> bool:
        return self._result is not None

    def champion(self) -> SelectionDecision:
        if self._result is None or self._challenger is None:
            # No duel ran (degenerate); the champion stands. Say WHICH
            # precondition was missing — an unfielded challenger and a
            # scheduled duel that never reported are different failures
            # with different first moves (issue #129), and the bare
            # sentence covered both.
            if self._challenger is None:
                missing = "no challenger was fielded"
            elif not self._scheduled:
                missing = f"the duel against {self._challenger.generation_id} was never scheduled"
            else:
                missing = (
                    f"the scheduled duel against {self._challenger.generation_id} "
                    "reported no result"
                )
            return SelectionDecision(
                promoted_generation_id=None,
                decision=TournamentDecision.REJECTED,
                reason=f"no challenger duel ran: {missing}",
            )
        outcome = self._result.outcome
        promoted = outcome.decision == "promoted"
        return SelectionDecision(
            promoted_generation_id=self._challenger.generation_id if promoted else None,
            decision=outcome.decision,
            reason=outcome.reason,
            matchups=(self._result,),
            crowning_matchup_id=self._result.matchup_id,
            standings=self._standings(promoted),
        )

    def _standings(self, promoted: bool) -> tuple[Standing, ...]:
        # The gauntlet MAY leave standings empty (the two-row view is
        # enough); we populate a minimal two-row ranking so a uniform
        # reader still gets a coherent ordering.
        if self._result is None or self._champion is None or self._challenger is None:
            return ()
        champ_scalar = self._result.left_scalar()
        chal_scalar = self._result.right_scalar()
        champ = self._champion.generation_id
        chal = self._challenger.generation_id
        if promoted:
            return (
                Standing(
                    chal, rank=1, scalar=chal_scalar, wins=1, status="champion", role="challenger"
                ),
                Standing(
                    champ,
                    rank=2,
                    scalar=champ_scalar,
                    losses=1,
                    status="eliminated",
                    role="champion",
                ),
            )
        return (
            Standing(
                champ, rank=1, scalar=champ_scalar, wins=1, status="champion", role="champion"
            ),
            Standing(
                chal, rank=2, scalar=chal_scalar, losses=1, status="eliminated", role="challenger"
            ),
        )

    def rounds(self) -> tuple[RoundRecord, ...]:
        # The gauntlet back-compat invariant allows an empty ``rounds``,
        # but emitting the canonical one-round / one-match shape costs
        # nothing and is the shape every other structure degenerates to.
        if self._result is None or self._champion is None or self._challenger is None:
            return ()
        outcome = self._result.outcome
        winner = (
            self._challenger.generation_id
            if outcome.decision == "promoted"
            else (self._champion.generation_id)
        )
        return (
            RoundRecord(
                stage_index=0,
                label="Gauntlet",
                matches=(
                    MatchRecord(
                        match_id=self._result.matchup_id,
                        competitors=(
                            self._champion.generation_id,
                            self._challenger.generation_id,
                        ),
                        winner=winner,
                        decision=outcome.decision,
                        delta_scalar=outcome.delta_scalar,
                    ),
                ),
            ),
        )


__all__ = ["GauntletStrategy"]
