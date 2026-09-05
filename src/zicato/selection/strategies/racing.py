"""Racing / successive-halving over the challenger field.

Rung 0 duels every challenger against the champion on a board SUBSET of
size ``rung0_board_size`` (or ``board_fraction`` of the board); after a
rung, eliminate the worst ``1 - 1/eta`` by scalar; survivors re-duel on a
larger slice; repeat until one survivor or the full board is consumed.
Elimination is by RANK within the rung (best-arm identification) rather than the
gate — the gate is applied only at the FINAL rung, on the full board, to
the last survivor.

When the epoch carries a measured A/A noise floor
(``params["noise_floor_delta_std"]``, injected by
:func:`zicato.selection.make_strategy`), a rung cuts only what its own
sample resolves: a candidate whose gap to the cut line is below the minimum
detectable effect at the rung's sample advances with the survivors, and the
next rung's larger slice resolves it. Without a floor the cut is by rank
alone.

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

import hashlib
import logging
import math
from collections.abc import Sequence
from typing import Any

from zicato.selection.strategies.champion_gate import ChampionGateStrategy
from zicato.selection.strategy import (
    Contestant,
    MatchRecord,
    Matchup,
    MatchupResult,
    RoundRecord,
    _param_float,
    _param_int,
    _param_opt_float,
    pending_match_record,
)
from zicato.tournament.detectable_effect import minimum_detectable_effect

log = logging.getLogger("zicato.selection.racing")

#: The nested-prefix schedule: each rung takes a prefix of the authored
#: JSONL order, and each prefix contains the one before it. The default for
#: every contract that does not name a schedule.
LEGACY_SLICE_SCHEDULE = "prefix"

#: Nested prefixes of a deterministic permutation derived from the board's
#: entry ids alone.
SHUFFLED_SLICE_SCHEDULE = "shuffled_v1"

#: Every ``slice_schedule`` racing accepts. The builder validates an edit
#: against this same tuple so a typo is a contract-time error rather than a
#: round-start one.
SLICE_SCHEDULES: tuple[str, ...] = (LEGACY_SLICE_SCHEDULE, SHUFFLED_SLICE_SCHEDULE)


def _shuffled_order(board_ids: Sequence[str]) -> tuple[str, ...]:
    """Return a stable permutation of a frozen board.

    The ordering is a pure function of the entry ids — already covered by the
    board's contract hash — so the same board always yields the same
    permutation, on resume and across machines, while neither authored JSONL
    row order nor a process-global random seed can decide an elimination.
    Each id's rank is its SHA-256 digest keyed by the whole id set; the id
    itself tie-breaks, making a digest collision deterministic too.
    """
    if not board_ids:
        return ()
    unique_ids = sorted({str(entry_id) for entry_id in board_ids})
    seed_material = "\x1e".join(unique_ids)

    def rank(entry_id: str) -> bytes:
        return hashlib.sha256(f"{seed_material}\x1c{entry_id}".encode()).digest()

    return tuple(sorted(unique_ids, key=lambda entry_id: (rank(entry_id), entry_id)))


class RacingStrategy(ChampionGateStrategy):
    """Successive-halving rungs, then a final full-board champion-gate duel."""

    structure = "racing"
    _final_match_id = "racing-final"
    _final_label = "Champion gate"
    # The rungs run board slices; the crowning duel runs the whole board.
    _final_board_fraction = 1.0
    # Racing's replication is INTRINSIC — the escalating board slices are the
    # sample rather than per-duel ``replicates`` — so it pins 1 even though the base
    # default is now 2 (a per-duel replicate would re-run a slice, not
    # enlarge it). Declared explicitly so the shared default-replicates map
    # reads a stable value.
    _default_replicates = 1

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        super().__init__(params)
        self._eta = max(2, _param_int(self.params, "eta", 2))
        self._board_fraction = _param_float(self.params, "board_fraction", 0.25)
        self._rung0 = _param_int(self.params, "rung0_board_size", 0)  # 0 ⇒ use fraction
        raw_ids = self.params.get("board_ids", ())
        self._board_ids: tuple[str, ...] = tuple(str(x) for x in raw_ids)
        self._slice_schedule = str(self.params.get("slice_schedule", LEGACY_SLICE_SCHEDULE))
        if self._slice_schedule not in SLICE_SCHEDULES:
            valid = ", ".join(repr(s) for s in SLICE_SCHEDULES)
            raise ValueError(
                f"racing slice_schedule must be one of {valid}; got {self._slice_schedule!r}"
            )
        self._slice_board_ids = (
            _shuffled_order(self._board_ids)
            if self._slice_schedule == SHUFFLED_SLICE_SCHEDULE
            else self._board_ids
        )

        # --- Matchup-level wall-clock budgets (opt-in; None ⇒ uncapped). ---
        # ``matchup_budget_seconds`` caps EVERY duel's total board-unit
        # wall-clock; ``final_rung_budget_seconds`` overrides it for the
        # final full-board crowning duel specifically — the rung that runs
        # the FULL board × replicates × both sides and is the pathological
        # grinder (each board individually under its per-board budget, but
        # their sum unbounded). When the latter is unset the former applies
        # to the final duel too; when both are unset no cap applies.
        self._matchup_budget_s = _param_opt_float(self.params, "matchup_budget_seconds")
        final_budget = _param_opt_float(self.params, "final_rung_budget_seconds")
        self._final_budget_s = final_budget if final_budget is not None else self._matchup_budget_s
        # The epoch's measured A/A ``delta_std`` (absent, null, or non-positive
        # means no floor): the dispersion of one full-board duel's difference,
        # from which each rung derives what its slice can resolve.
        self._noise_delta_std = _param_opt_float(self.params, "noise_floor_delta_std")

        self._rung = 0
        self._alive: list[Contestant] = []
        self._pending: dict[str, tuple[Contestant, Contestant]] = {}
        self._rung_scalars: dict[str, float] = {}
        # The champion defends EVERY duel as the shared ``left`` side, so it is
        # never recorded into ``_scalars`` (keyed on the challenger ``right``).
        # We keep its last-known running scalar separately so the in-flight
        # rung's champion lane carries a real ``projected_scalar`` benchmark
        # (the dashed line the field races against) instead of a blank lane that
        # forces the scalar-track to a delta-only domain. None until the first
        # duel of the first rung lands.
        self._champion_scalar: float | None = None
        self._eliminated_round: dict[str, int] = {}
        self._rung_started = False
        self._survivor: Contestant | None = None

    def seed(self, champion: Contestant, challengers: Sequence[Contestant]) -> None:
        super().seed(champion, challengers)
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
        return self._slice_board_ids[:size]

    def _rung_fraction(self) -> float:
        total = len(self._board_ids)
        if total == 0:
            return 1.0
        return self._rung_board_size() / total

    def _is_final_rung(self) -> bool:
        # A rung is final when the slice is the full board or only one
        # challenger remains.
        return self._rung_fraction() >= 1.0 or len(self._alive) <= 1

    def _rung_detectable_gap(self) -> float | None:
        """The smallest scalar gap this rung's sample can resolve, or ``None``.

        The floor's ``delta_std`` is the deviation of the difference of two
        full-board scalars. Taking the ``M`` board entries as independent,
        equally weighted units of one full-board scalar, one entry-replicate
        has deviation ``delta_std · √(M/2)``, and a rung that scores ``m``
        entries at ``r`` replicates holds ``m·r`` such units per arm. The
        two-sample minimum detectable effect at that sample is the gap; a
        smaller one is inside the rung's noise. ``None`` without a floor, on
        an empty board, or when the sample is below two units per arm.
        """
        if self._noise_delta_std is None:
            return None
        total = len(self._board_ids)
        size = self._rung_board_size()
        if total == 0 or size == 0:
            return None
        unit_sd = self._noise_delta_std * math.sqrt(total / 2.0)
        return minimum_detectable_effect(unit_sd, size * self._replicates)

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
                    matchup_budget_seconds=self._matchup_budget_s,
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
                stage_index=self._final_stage_index(),
                # The final crowning duel runs the FULL board × replicates ×
                # both sides — the grind this budget exists to bound. Prefer
                # the final-rung-specific cap, falling back to the matchup cap.
                matchup_budget_seconds=self._final_budget_s,
            ),
        )

    def _finalist(self) -> Contestant | None:
        return self._survivor

    def _final_stage_index(self) -> int:
        return self._rung + 1

    # -- result folding ----------------------------------------------------

    def record_result(self, result: MatchupResult) -> None:
        self._audit.append(result)
        # The challenger is the ``right`` side in a racing duel.
        self._scalars[result.right_id] = result.right_scalar()
        # The champion is the shared ``left`` defender of every duel. Keep its
        # last-known running scalar so ``_live_progress`` can seed the champion
        # lane's projected benchmark. The N concurrent duels of a rung each
        # aggregate the champion over only THEIR board slice, so take the
        # most-favourable (lowest loss) seen — never let a less-progressed duel
        # regress the benchmark (mirrors the keystone fold's champion guard).
        if self._champion is not None and result.left_id == self._champion.generation_id:
            champ_scalar = result.left_scalar()
            if self._champion_scalar is None or champ_scalar < self._champion_scalar:
                self._champion_scalar = champ_scalar

        if self._capture_final_result(result):
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
        # 1/eta (best-arm identification rather than the gate).
        ranked = sorted(
            self._alive,
            key=lambda c: (self._rung_scalars.get(c.generation_id, float("inf")), c.generation_id),
        )
        keep_n = max(1, int(math.floor(len(ranked) / self._eta)))
        # A candidate the rung's sample cannot separate from the last
        # survivor advances too: the next rung's larger slice resolves it.
        gap = self._rung_detectable_gap()
        if gap is not None and keep_n < len(ranked):
            line = self._rung_scalars.get(ranked[keep_n - 1].generation_id, float("inf"))
            while keep_n < len(ranked):
                scalar = self._rung_scalars.get(ranked[keep_n].generation_id, float("inf"))
                if scalar - line >= gap:
                    break
                log.info(
                    "racing rung %d: %s trails the cut line by %.6g, below the %.6g the "
                    "rung's sample resolves; it advances",
                    self._rung,
                    ranked[keep_n].generation_id,
                    scalar - line,
                    gap,
                )
                keep_n += 1
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

    def _no_promotion_reason(self) -> str:
        return "no racing survivor cleared the full-board champion gate"

    def _is_eliminated(self, gid: str) -> bool:
        # A rung cuts by rank: every challenger below the survivor cut is out.
        return gid in self._eliminated_round

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
        champion_id = self._champion.generation_id if self._champion is not None else None
        lanes: list[str] = []
        if champion_id is not None:
            lanes.append(champion_id)
        for _mid, (_left, right) in self._pending.items():
            lanes.append(right.generation_id)
        for gid in lanes:
            row: dict[str, Any] = {"inflight": 1}
            if total > 0:
                row["boards_total"] = int(total)
            # The champion is the shared ``left`` defender, so it is not in
            # ``_scalars`` (challenger-keyed) — seed it from the dedicated
            # ``_champion_scalar`` benchmark so its lane shows the real
            # last-known champion loss rather than a blank lane.
            scalar = self._champion_scalar if gid == champion_id else self._scalars.get(gid)
            if scalar is not None:
                # The lane's last-known running aggregate vs the champion.
                row["projected_scalar"] = float(scalar)
                row["projected"] = True
            progress[gid] = row
        return progress

    def _pending_stage_round(self) -> RoundRecord | None:
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
        return None


__all__ = [
    "LEGACY_SLICE_SCHEDULE",
    "SLICE_SCHEDULES",
    "SHUFFLED_SLICE_SCHEDULE",
    "RacingStrategy",
]
