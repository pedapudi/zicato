"""Racing / successive-halving over the challenger field.

Rung 0 duels every challenger against the champion on a board SUBSET of
size ``rung0_board_size`` (or ``board_fraction`` of the board); after a
rung, eliminate the worst ``1 - 1/eta`` by scalar; survivors re-duel on a
larger slice; repeat until one survivor or the full board is consumed.
Elimination is by RANK within the rung (best-arm identification), not the
gate — the gate is applied only at the FINAL rung, on the full board, to
the last survivor.

This is the one bracket-shaped structure SELECTION.md endorses for
zicato's regime: replication is intrinsic (escalating board slices =
escalating sample). Maps to successive halving / ASHA (§2③).

The strategy is told the board's entry ids via ``params["board_ids"]``.
The orchestrator defaults these to the FULL epoch board when the spec
does not pin an explicit subset (see :func:`zicato.selection.make_strategy`),
so the CLI-flag form (``--tournament-structure racing``) slices the board
out of the box; an explicit ``params["board_ids"]`` still overrides to race
on a subset. When the ids are absent entirely (e.g. a bare unit-test
construction with no board) it falls back to whole-board duels per rung, so
a misconfiguration degrades gracefully rather than erroring.
"""

from __future__ import annotations

import math
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
    _param_float,
    _param_int,
    pending_match_record,
)


class RacingStrategy(SelectionStrategy):
    """Successive-halving rungs, then a final full-board champion-gate duel."""

    structure = "racing"

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        super().__init__(params)
        self._champion: Contestant | None = None
        self._challengers: list[Contestant] = []
        self._eta = max(2, _param_int(self.params, "eta", 2))
        self._board_fraction = _param_float(self.params, "board_fraction", 0.25)
        self._rung0 = _param_int(self.params, "rung0_board_size", 0)  # 0 ⇒ use fraction
        raw_ids = self.params.get("board_ids", ())
        self._board_ids: tuple[str, ...] = tuple(str(x) for x in raw_ids)
        self._replicates = max(1, _param_int(self.params, "replicates", 1))

        self._rung = 0
        self._alive: list[Contestant] = []
        self._pending: dict[str, tuple[Contestant, Contestant]] = {}
        self._rung_scalars: dict[str, float] = {}
        self._scalars: dict[str, float] = {}
        self._eliminated_round: dict[str, int] = {}
        self._records: list[RoundRecord] = []
        self._audit: list[MatchupResult] = []
        self._rung_started = False

        # Final full-board champion-gate.
        self._final_scheduled = False
        self._final_result: MatchupResult | None = None
        self._final_match_id = "racing-final"
        self._survivor: Contestant | None = None

    def field_size(self) -> int:
        return max(1, _param_int(self.params, "field_size", 2))

    def seed(self, champion: Contestant, challengers: Sequence[Contestant]) -> None:
        self._champion = champion
        self._challengers = list(challengers)
        self._alive = list(self._challengers)

    # -- board-slice helpers ----------------------------------------------

    def _rung_board_size(self) -> int:
        total = len(self._board_ids)
        if total == 0:
            return 0
        if self._rung0 > 0:
            base = self._rung0
        else:
            base = max(1, int(math.ceil(total * self._board_fraction)))
        size = int(base * (self._eta**self._rung))
        return min(total, size)

    def _rung_board_subset(self) -> tuple[str, ...] | None:
        if not self._board_ids:
            return None  # whole board
        size = self._rung_board_size()
        return self._board_ids[:size]

    def _rung_fraction(self) -> float:
        total = len(self._board_ids)
        if total == 0:
            return 1.0
        return self._rung_board_size() / total

    def _is_final_rung(self) -> bool:
        # A rung is final when the slice is the full board or only one
        # challenger remains.
        return self._rung_fraction() >= 1.0 or len(self._alive) <= 1

    # -- scheduling --------------------------------------------------------

    def next_matchups(self) -> Sequence[Matchup]:
        if self._champion is None:
            return ()
        if self._pending:
            return ()
        if self._survivor is not None:
            return self._maybe_final()
        if not self._alive:
            return ()
        if self._is_final_rung():
            # The last rung crowns a single survivor (lowest scalar so
            # far, or the sole alive challenger), then the champion-gate
            # confirms it on the full board.
            self._survivor = self._pick_survivor()
            return self._maybe_final()
        return self._schedule_rung()

    def _schedule_rung(self) -> Sequence[Matchup]:
        assert self._champion is not None  # guarded by next_matchups()
        champion = self._champion
        subset = self._rung_board_subset()
        matchups: list[Matchup] = []
        self._rung_scalars = {}
        for slot, challenger in enumerate(list(self._alive)):
            mid = f"rung{self._rung}_m{slot}"
            self._pending[mid] = (champion, challenger)
            matchups.append(
                Matchup(
                    matchup_id=mid,
                    left=champion,
                    right=challenger,
                    board_subset=subset,
                    replicates=self._replicates,
                    stage_index=self._rung,
                )
            )
        self._rung_started = True
        return matchups

    def _pick_survivor(self) -> Contestant | None:
        if not self._alive:
            return None
        return min(
            self._alive,
            key=lambda c: (self._scalars.get(c.generation_id, float("inf")), c.generation_id),
        )

    def _maybe_final(self) -> Sequence[Matchup]:
        if self._final_scheduled or self._survivor is None:
            return ()
        self._final_scheduled = True
        return (
            Matchup(
                matchup_id=self._final_match_id,
                left=self._champion,  # type: ignore[arg-type]
                right=self._survivor,
                board_subset=None,  # full board for the crowning gate
                replicates=self._replicates,
                stage_index=self._rung + 1,
            ),
        )

    # -- result folding ----------------------------------------------------

    def record_result(self, result: MatchupResult) -> None:
        self._audit.append(result)
        # The challenger is the ``right`` side in a racing duel.
        self._scalars[result.right_id] = result.right_scalar()

        if result.matchup_id == self._final_match_id and self._final_scheduled:
            self._final_result = result
            return

        pair = self._pending.pop(result.matchup_id, None)
        if pair is None:
            return
        self._rung_scalars[result.right_id] = result.right_scalar()
        # When every duel of this rung has landed, apply the cut.
        if not self._pending:
            self._apply_cut()

    def _apply_cut(self) -> None:
        # Rank survivors by rung scalar (lower is better) and keep the top
        # 1/eta (best-arm identification, not the gate).
        ranked = sorted(
            self._alive,
            key=lambda c: (self._rung_scalars.get(c.generation_id, float("inf")), c.generation_id),
        )
        keep_n = max(1, int(math.floor(len(ranked) / self._eta)))
        survivors = ranked[:keep_n]
        cut = ranked[keep_n:]
        for c in cut:
            self._eliminated_round[c.generation_id] = self._rung
        self._records.append(
            RoundRecord(
                stage_index=self._rung,
                label=f"Rung {self._rung}",
                matches=(
                    MatchRecord(
                        match_id=f"rung{self._rung}",
                        competitors=tuple(c.generation_id for c in ranked),
                        survivors=tuple(c.generation_id for c in survivors),
                        cut=tuple(c.generation_id for c in cut),
                        board_fraction=self._rung_fraction(),
                    ),
                ),
            )
        )
        self._alive = survivors
        self._rung += 1
        self._rung_started = False

    def resolved(self) -> bool:
        if self._champion is None:
            return True
        if self._final_result is not None:
            return True
        return not self._challengers

    def champion(self) -> SelectionDecision:
        audit = tuple(self._audit)
        if self._final_result is None or self._survivor is None:
            return SelectionDecision(
                promoted_generation_id=None,
                decision="rejected",
                reason="no racing survivor cleared the full-board champion gate",
                matchups=audit,
                standings=self._standings(None),
            )
        outcome = self._final_result.outcome
        promoted = outcome.decision == "promoted"
        promoted_id = self._survivor.generation_id if promoted else None
        return SelectionDecision(
            promoted_generation_id=promoted_id,
            decision=outcome.decision,
            reason=outcome.reason,
            matchups=audit,
            crowning_matchup_id=self._final_match_id,
            standings=self._standings(promoted_id),
        )

    def _standings(self, promoted_id: str | None) -> tuple[Standing, ...]:
        ids = [c.generation_id for c in self._challengers]
        if self._champion:
            ids.insert(0, self._champion.generation_id)
        rows: list[Standing] = []
        for gid in ids:
            role = (
                "champion"
                if (self._champion and gid == self._champion.generation_id)
                else ("challenger")
            )
            if promoted_id is not None and gid == promoted_id:
                status = "champion"
            elif gid in self._eliminated_round:
                status = "eliminated"
            else:
                status = "alive"
            rows.append(
                Standing(
                    generation_id=gid,
                    rank=0,
                    scalar=self._scalars.get(gid, 0.0),
                    status=status,  # type: ignore[arg-type]
                    role=role,  # type: ignore[arg-type]
                )
            )
        rows.sort(key=lambda s: (s.scalar, s.generation_id))
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
        if self._final_result is not None and self._champion and self._survivor:
            outcome = self._final_result.outcome
            winner = (
                self._survivor.generation_id
                if outcome.decision == "promoted"
                else (self._champion.generation_id)
            )
            recs.append(
                RoundRecord(
                    stage_index=self._rung + 1,
                    label="Champion gate",
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
                            board_fraction=1.0,
                        ),
                    ),
                )
            )
        return tuple(recs)

    # -- live (in-flight) projection --------------------------------------

    def _live_progress(self) -> dict[str, dict[str, Any]]:
        """Authoritative per-lane live-progress for the in-flight rung.

        The strategy OWNS the rung's topology: which lanes are racing this
        rung (every ``right`` side of a still-pending duel, plus the
        champion as the shared ``left`` defender), each lane's
        ``boards_total`` (the rung's board-slice size), and the lane's
        last-known running scalar vs the champion when one exists. The
        runner's per-board ``projected`` map (the scorer's domain) is
        overlaid later (in the orchestrator's serialiser) to fill the
        live ``boards_done`` + the streaming ``projected_scalar``; here we
        seed only what strategy state already knows.

        Each value is ``{boards_total, inflight, projected?, projected_scalar?}``
        — ``projected_scalar`` is omitted when the lane has no running
        scalar yet (rung 0 before any duel has landed), so the frontend
        falls back gracefully to the boards-progress bar alone. The
        champion lane carries ``boards_total`` + the champion's own
        last-known scalar so the lane has a stable benchmark to race
        against, even while its per-duel ``projected`` is re-aggregated.
        """
        total = self._rung_board_size()
        progress: dict[str, dict[str, Any]] = {}
        lanes: list[str] = []
        if self._champion is not None:
            lanes.append(self._champion.generation_id)
        for _mid, (_left, right) in self._pending.items():
            lanes.append(right.generation_id)
        for gid in lanes:
            row: dict[str, Any] = {"inflight": 1}
            if total > 0:
                row["boards_total"] = int(total)
            scalar = self._scalars.get(gid)
            if scalar is not None:
                # The lane's last-known running aggregate vs the champion.
                row["projected_scalar"] = float(scalar)
                row["projected"] = True
            progress[gid] = row
        return progress

    def _pending_round(self) -> RoundRecord | None:
        # Mid rung: ``_pending`` holds this rung's champion-vs-challenger
        # duels. Emit one pending match per duel (keyed by the rung
        # matchup id) carrying the rung's board fraction + the
        # authoritative per-lane live-progress for the whole rung (the
        # frontend reads ``live_progress`` off the rung's first match).
        if self._pending:
            fraction = self._rung_fraction()
            live_progress = self._live_progress()
            matches = [
                pending_match_record(
                    mid,
                    (left.generation_id, right.generation_id),
                    board_fraction=fraction,
                    # Attach the full per-rung live-progress to the FIRST
                    # match only — the racing model lifts ``live_progress``
                    # off ``matches[0]`` for the whole rung; the remaining
                    # per-duel matches keep it empty to avoid duplication.
                    live_progress=live_progress if slot == 0 else None,
                )
                for slot, (mid, (left, right)) in enumerate(self._pending.items())
            ]
            return RoundRecord(
                stage_index=self._rung,
                label=f"Rung {self._rung}",
                matches=tuple(matches),
            )
        # Final full-board champion-gate scheduled, result not yet landed.
        if self._final_scheduled and self._final_result is None and self._survivor is not None:
            assert self._champion is not None
            return RoundRecord(
                stage_index=self._rung + 1,
                label="Champion gate",
                matches=(
                    pending_match_record(
                        self._final_match_id,
                        (self._champion.generation_id, self._survivor.generation_id),
                        board_fraction=1.0,
                    ),
                ),
            )
        return None

    def _live_standings(self) -> tuple[Standing, ...]:
        return self._standings(None)


__all__ = ["RacingStrategy"]
