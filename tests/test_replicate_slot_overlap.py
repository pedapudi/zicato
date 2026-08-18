"""Pins for overlapped replicate slots (#251).

A matchup that runs R replicates used to run its slots behind a barrier:
slot N+1 launched only once every board unit of slot N had settled, so up
to ``parallelism - 1`` permits sat idle at each of the R-1 slot boundaries
while the slowest unit of the slot finished. The slots now run overlapped
against ONE shared semaphore whenever neither budget knob is engaged.

What these pin:

* a unit of slot 1 starts before the LAST unit of slot 0 ends (the freed
  permit is actually used);
* an entry never holds two of its own replicates in flight — the ordering
  rule that keeps a matchup's in-flight units distinct in
  ``(generation, entry, replicate)``;
* ``parallelism`` is still the ceiling ACROSS the overlapped slots (the
  fast round supplies no semaphore of its own, so a per-slot semaphore
  would run ``replicates × parallelism`` units at once);
* the slots stay sequential on both budget paths — a bound token ledger
  and a matchup wall-clock deadline — because that boundary is where the
  stop-launching decision is made;
* the overlapped slots share ONE incremental scorer, projecting over
  ``board × replicates`` units;
* the per-slot maps come back in SLOT order, so the fold's
  representative replicate is replicate 0 rather than whichever entry
  chain settled first.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import Any

import pytest

from tests._contract_pins import deterministic_weights
from zicato.core.board import BoardEntry
from zicato.core.runtime import RoundTokenLedger, RuntimeConfig
from zicato.core.types import Generation, LossProfile

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Fixtures: a board, a config, a generation, and a recording fake board unit
# ---------------------------------------------------------------------------


def _board(size: int) -> list[BoardEntry]:
    return [
        BoardEntry(
            id=f"entry_{i}",
            kind="single_turn",
            wall_clock_budget_seconds=60,
            input=f"hello {i}",
        )
        for i in range(size)
    ]


def _config(*, parallelism: int, token_ledger: RoundTokenLedger | None = None) -> RuntimeConfig:
    async def _harness(system: str, user: str, model: str) -> str:
        return ""

    async def _auxiliary(system: str, user: str, model: str) -> str:
        return ""

    return RuntimeConfig(
        instance_id="t",
        workspace_root=Path("/nonexistent"),
        harness_call_llm=_harness,
        auxiliary_call_llm=_auxiliary,
        parallelism=parallelism,
        token_ledger=token_ledger,
    )


def _generation(gen_id: str) -> Generation:
    return Generation(
        id=gen_id,
        epoch_id="e1",
        parent_id=None,
        snapshot_root=Path("/nonexistent/snapshot"),
        created_at="2026-08-17T00:00:00Z",
    )


def _loss(entry_id: str, replicate_index: int) -> LossProfile:
    """A LossProfile tagged with the slot it came from."""
    return LossProfile(
        run_id=f"{entry_id}-r{replicate_index}",
        entry_id=entry_id,
        generation_id="v1",
        epoch_id="e1",
        drift_counts=(),
        plan_revisions=0,
        task_failure_ratio=0.0,
        runtime_ms=1,
        wall_clock_budget_exceeded=False,
        expectation_result=None,
        drift_loss=float(replicate_index),
        pass_fail=None,
    )


class _UnitRecorder:
    """A fake board unit that records when each unit starts and ends.

    ``hold_entry``'s slot-0 unit keeps its permit until some LATER slot's
    unit starts — that is exactly the tail an overlapped scheduler fills.
    The wait is bounded so a scheduler that never overlaps fails the
    assertion instead of hanging the suite.
    """

    def __init__(self, *, hold_entry: str | None = None, dwell: float = 0.0) -> None:
        self.events: list[tuple[str, str, int]] = []
        self.scorers: list[Any] = []
        self._hold_entry = hold_entry
        self._dwell = dwell
        self._later_slot_started = asyncio.Event()

    async def run(self, entry_id: str, replicate_index: int) -> None:
        self.events.append(("start", entry_id, replicate_index))
        if replicate_index > 0:
            self._later_slot_started.set()
        if entry_id == self._hold_entry and replicate_index == 0:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._later_slot_started.wait(), timeout=5.0)
        else:
            await asyncio.sleep(self._dwell)
        self.events.append(("end", entry_id, replicate_index))

    # -- readings taken from the recording -------------------------------

    def overlapped(self) -> bool:
        """Whether a later slot's unit started before slot 0 had drained."""
        last_slot_0_end = max(
            i for i, (phase, _e, r) in enumerate(self.events) if phase == "end" and r == 0
        )
        later_starts = [
            i for i, (phase, _e, r) in enumerate(self.events) if phase == "start" and r > 0
        ]
        return bool(later_starts) and min(later_starts) < last_slot_0_end

    def max_concurrent(self) -> int:
        live = 0
        peak = 0
        for phase, _entry, _replicate in self.events:
            live += 1 if phase == "start" else -1
            peak = max(peak, live)
        return peak

    def repeated_entry_in_flight(self) -> bool:
        """Whether any entry ever held two of its own replicates at once."""
        live: set[str] = set()
        for phase, entry_id, _replicate in self.events:
            if phase == "start":
                if entry_id in live:
                    return True
                live.add(entry_id)
            else:
                live.discard(entry_id)
        return False

    def slots_ran_sequentially(self) -> bool:
        """Whether every slot-N event precedes every slot-N+1 start."""
        slot_at_index = [r for _phase, _entry, r in self.events]
        return slot_at_index == sorted(slot_at_index)


def _patch_full_unit(monkeypatch: pytest.MonkeyPatch, recorder: _UnitRecorder) -> None:
    from zicato.tournament import scheduling as sched

    async def _fake_unit(
        *, entry: BoardEntry, replicate_index: int = 0, scorer: Any = None, **_kw: Any
    ) -> tuple[LossProfile, LossProfile]:
        recorder.scorers.append(scorer)
        await recorder.run(entry.id, replicate_index)
        return _loss(entry.id, replicate_index), _loss(entry.id, replicate_index)

    monkeypatch.setattr(sched, "_run_full_board_unit", _fake_unit)


def _patch_fast_unit(monkeypatch: pytest.MonkeyPatch, recorder: _UnitRecorder) -> None:
    from zicato.tournament import scheduling as sched

    async def _fake_unit(
        *, entry: BoardEntry, replicate_index: int = 0, scorer: Any = None, **_kw: Any
    ) -> LossProfile:
        recorder.scorers.append(scorer)
        await recorder.run(entry.id, replicate_index)
        return _loss(entry.id, replicate_index)

    monkeypatch.setattr(sched, "_run_fast_board_unit", _fake_unit)


async def _run_replicated(
    *, config: RuntimeConfig, board: list[BoardEntry], **kwargs: Any
) -> tuple[dict[str, LossProfile], dict[str, LossProfile]]:
    from zicato.tournament import scheduling as sched

    left, right, _mode, _provenance = await sched._run_replicated(
        adapter=None,
        left_gen=_generation("v0"),
        right_gen=_generation("v1"),
        board=board,
        weights=deterministic_weights(),
        config=config,
        workspace_root=Path("/nonexistent"),
        epoch_id="e1",
        fast=True,
        **kwargs,
    )
    return left, right


# ---------------------------------------------------------------------------
# The overlap itself: freed permits are used, and by whom
# ---------------------------------------------------------------------------


async def test_a_later_slot_starts_before_slot_0_drains(monkeypatch: pytest.MonkeyPatch) -> None:
    """Utilisation: the permit a finished unit frees goes to the next slot.

    Three entries, two permits: one entry's slot-0 unit holds its permit
    while the others finish. Behind the old barrier no slot-1 unit could
    start until that straggler settled.
    """
    recorder = _UnitRecorder(hold_entry="entry_2")
    _patch_full_unit(monkeypatch, recorder)

    left, right = await _run_replicated(
        config=_config(parallelism=2), board=_board(3), replicates=2
    )

    assert recorder.overlapped()
    assert set(left) == set(right) == {"entry_0", "entry_1", "entry_2"}


async def test_an_entry_never_holds_two_of_its_own_replicates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ordering rule: an entry's slots run in order, in its own chain."""
    recorder = _UnitRecorder(hold_entry="entry_2")
    _patch_full_unit(monkeypatch, recorder)

    await _run_replicated(config=_config(parallelism=4), board=_board(3), replicates=3)

    assert recorder.overlapped()
    assert not recorder.repeated_entry_in_flight()


async def test_overlapped_slots_never_exceed_parallelism(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One shared semaphore: the ceiling is parallelism, not R × parallelism."""
    recorder = _UnitRecorder(hold_entry="entry_3", dwell=0.01)
    _patch_full_unit(monkeypatch, recorder)

    await _run_replicated(config=_config(parallelism=2), board=_board(4), replicates=3)

    assert recorder.max_concurrent() <= 2
    assert recorder.overlapped()


# ---------------------------------------------------------------------------
# The budget paths keep the barrier — that boundary carries a decision
# ---------------------------------------------------------------------------


async def test_a_bound_token_ledger_keeps_the_slots_sequential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ledger decides between slots whether to launch the next one at all."""
    recorder = _UnitRecorder()
    _patch_full_unit(monkeypatch, recorder)
    # A budget far too large to trip, so every slot runs and the ONLY
    # observable difference is the scheduling shape.
    ledger = RoundTokenLedger(10**9)

    await _run_replicated(
        config=_config(parallelism=4, token_ledger=ledger), board=_board(3), replicates=3
    )

    assert not recorder.overlapped()
    assert recorder.slots_ran_sequentially()
    assert not ledger.clipped


async def test_a_matchup_deadline_keeps_the_slots_sequential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The wall-clock deadline is read between slots, so slots stay ordered."""
    recorder = _UnitRecorder()
    _patch_full_unit(monkeypatch, recorder)

    await _run_replicated(
        config=_config(parallelism=4),
        board=_board(3),
        replicates=3,
        matchup_budget_seconds=300.0,
    )

    assert not recorder.overlapped()
    assert recorder.slots_ran_sequentially()


# ---------------------------------------------------------------------------
# One scorer, and slot-major results
# ---------------------------------------------------------------------------


async def test_overlapped_slots_share_one_scorer_over_every_replicate_unit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two scorers would write disjoint subsets of one live aggregate."""
    recorder = _UnitRecorder()
    _patch_full_unit(monkeypatch, recorder)

    await _run_replicated(config=_config(parallelism=4), board=_board(3), replicates=2)

    assert len({id(scorer) for scorer in recorder.scorers}) == 1
    assert recorder.scorers[0]._board_total == 3 * 2


async def test_slot_maps_come_back_in_slot_order(monkeypatch: pytest.MonkeyPatch) -> None:
    """The fold's representative replicate must be replicate 0.

    ``_average_losses`` carries the fields it cannot fold from the FIRST
    map it is handed, so the transpose back out of the entry chains has to
    restore slot order — not the order the chains settled in. The held
    entry makes the chains settle out of order.
    """
    from zicato.tournament import scheduling as sched

    recorder = _UnitRecorder(hold_entry="entry_2")
    _patch_full_unit(monkeypatch, recorder)

    board = _board(3)
    runs = await sched._run_replicate_slots_full(
        adapter=None,
        parent_gen=_generation("v0"),
        child_gen=_generation("v1"),
        board=board,
        weights=deterministic_weights(),
        config=_config(parallelism=2),
        workspace_root=Path("/nonexistent"),
        epoch_id="e1",
        match_id="m1",
        replicate_base=0,
        replicate_count=3,
        force_fresh=False,
        parent_force_fresh=None,
        provenance=None,
        unit_semaphore=None,
    )

    assert [
        {entry_id: left.run_id for entry_id, left in slot_left.items()}
        for slot_left, _slot_right in runs
    ] == [
        {"entry_0": "entry_0-r0", "entry_1": "entry_1-r0", "entry_2": "entry_2-r0"},
        {"entry_0": "entry_0-r1", "entry_1": "entry_1-r1", "entry_2": "entry_2-r1"},
        {"entry_0": "entry_0-r2", "entry_1": "entry_1-r2", "entry_2": "entry_2-r2"},
    ]


async def test_replicate_base_offsets_every_overlapped_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The evidence pre-gate's reserved base still offsets each slot."""
    from zicato.tournament import scheduling as sched

    recorder = _UnitRecorder()
    _patch_full_unit(monkeypatch, recorder)

    await sched._run_replicate_slots_full(
        adapter=None,
        parent_gen=_generation("v0"),
        child_gen=_generation("v1"),
        board=_board(2),
        weights=deterministic_weights(),
        config=_config(parallelism=4),
        workspace_root=Path("/nonexistent"),
        epoch_id="e1",
        match_id="m1",
        replicate_base=5000,
        replicate_count=2,
        force_fresh=False,
        parent_force_fresh=None,
        provenance=None,
        unit_semaphore=None,
    )

    assert {r for _phase, _entry, r in recorder.events} == {5000, 5001}


# ---------------------------------------------------------------------------
# The fast round: the same overlap, and the semaphore it never supplied
# ---------------------------------------------------------------------------


async def test_fast_slots_overlap_under_one_semaphore(monkeypatch: pytest.MonkeyPatch) -> None:
    """The fast round mints ONE semaphore for all its overlapped slots.

    It passes none of its own, so a per-slot semaphore would be a fresh
    ``Semaphore(parallelism)`` for each slot and the overlapped slots would
    run ``replicates × parallelism`` units at once.
    """
    from zicato.tournament import scheduling as sched

    recorder = _UnitRecorder(hold_entry="entry_3", dwell=0.01)
    _patch_fast_unit(monkeypatch, recorder)

    runs = await sched._run_replicate_slots_fast(
        adapter=None,
        child_gen=_generation("v1"),
        board=_board(4),
        weights=deterministic_weights(),
        config=_config(parallelism=2),
        workspace_root=Path("/nonexistent"),
        epoch_id="e1",
        replicate_count=3,
    )

    assert recorder.overlapped()
    assert recorder.max_concurrent() <= 2
    assert not recorder.repeated_entry_in_flight()
    assert len({id(scorer) for scorer in recorder.scorers}) == 1
    assert recorder.scorers[0]._board_total == 4 * 3
    assert [sorted(slot) for slot in runs] == [sorted(_board(4)[i].id for i in range(4))] * 3


async def _run_fast_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, token_ledger: RoundTokenLedger | None
) -> list[str]:
    """Run a fast round with both slot schedulers stubbed; report which ran."""
    from zicato.tournament import runner

    board = _board(2)
    used: list[str] = []

    async def _fake_overlapped(**kwargs: Any) -> list[dict[str, LossProfile]]:
        used.append("overlapped")
        count = int(kwargs["replicate_count"])
        return [{entry.id: _loss(entry.id, r) for entry in board} for r in range(count)]

    async def _fake_sequential(**kwargs: Any) -> dict[str, LossProfile]:
        used.append("sequential")
        replicate_index = int(kwargs["replicate_index"])
        return {entry.id: _loss(entry.id, replicate_index) for entry in board}

    monkeypatch.setattr(runner, "_run_replicate_slots_fast", _fake_overlapped)
    monkeypatch.setattr(runner, "_run_board_units_fast", _fake_sequential)

    await runner.run_fast_mode(
        adapter=None,
        child_gen=_generation("v1"),
        board=board,
        weights=deterministic_weights(),
        config=_config(parallelism=2, token_ledger=token_ledger),
        workspace_root=tmp_path,
        epoch_id="e1",
        parent_historical_agg={"generation_id": "v0", "scalar": 1.0},
        replicates=3,
    )
    return used


async def test_fast_round_overlaps_its_slots(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    assert await _run_fast_mode(monkeypatch, tmp_path, token_ledger=None) == ["overlapped"]


async def test_fast_round_keeps_the_barrier_under_a_token_ledger(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    used = await _run_fast_mode(monkeypatch, tmp_path, token_ledger=RoundTokenLedger(10**9))
    assert used == ["sequential", "sequential", "sequential"]
