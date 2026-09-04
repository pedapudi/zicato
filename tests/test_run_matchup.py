"""Tests for ``run_matchup``, the canonical selection-layer duel runner.

Proves that the canonical runner (1) agrees with the standalone tournament
API for the same champion-vs-challenger pair, (2) honours
``board_subset`` (racing rungs), and (3) averages per-entry losses under
``replicates`` while keeping the gate composition unchanged. The runner's
subprocess ``_run_single`` is stubbed with canned losses — no live runs.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import zicato.tournament.runner as runner_mod
from tests._runtime_builders import runtime_config
from zicato.core import (
    BoardEntry,
    Generation,
    LossProfile,
    RuntimeConfig,
    ScoringWeights,
)
from zicato.core.types import DriftCount, ExpectationResult
from zicato.tournament.runner import run_matchup, run_tournament


def _loss(*, generation_id: str, entry_id: str, drift_loss: float, pass_fail: bool | None):
    expectation = (
        ExpectationResult(kind="predicate", passed=bool(pass_fail))
        if pass_fail is not None
        else None
    )
    return LossProfile(
        run_id=f"run-{generation_id}-{entry_id}",
        entry_id=entry_id,
        generation_id=generation_id,
        epoch_id="e0",
        drift_counts=(DriftCount(kind="off_topic", severity="info", count=0),),
        plan_revisions=0,
        task_failure_ratio=0.0,
        runtime_ms=1000,
        wall_clock_budget_exceeded=False,
        expectation_result=expectation,
        drift_loss=drift_loss,
        pass_fail=pass_fail,
    )


def _board() -> list[BoardEntry]:
    return [
        BoardEntry(id="entry_a", kind="single_turn", wall_clock_budget_seconds=60, input="x"),
        BoardEntry(id="entry_b", kind="single_turn", wall_clock_budget_seconds=60, input="y"),
    ]


def _gen(tmp_path: Path, gen_id: str) -> Generation:
    return Generation(
        id=gen_id,
        epoch_id="e0",
        parent_id=None,
        snapshot_root=tmp_path / f"snap_{gen_id}",
        created_at="2024-01-01T00:00:00Z",
    )


def _stub_run_single(monkeypatch, canned, *, log: list | None = None):
    async def fake_run_single(
        *, adapter, generation, entry, weights, config, workspace_root, epoch_id, side, match_id=""
    ):
        del adapter, weights, config, workspace_root, epoch_id, side, match_id
        if log is not None:
            log.append((generation.id, entry.id))
        return canned[(generation.id, entry.id)]

    monkeypatch.setattr(runner_mod, "_run_single", fake_run_single)


def test_run_matchup_matches_run_tournament_gauntlet(monkeypatch, tmp_path):
    """For a champion-vs-challenger pair, run_matchup's verdict == run_tournament's."""
    canned = {
        ("v0", "entry_a"): _loss(
            generation_id="v0", entry_id="entry_a", drift_loss=2.0, pass_fail=True
        ),
        ("v0", "entry_b"): _loss(
            generation_id="v0", entry_id="entry_b", drift_loss=2.0, pass_fail=True
        ),
        ("v1", "entry_a"): _loss(
            generation_id="v1", entry_id="entry_a", drift_loss=1.0, pass_fail=True
        ),
        ("v1", "entry_b"): _loss(
            generation_id="v1", entry_id="entry_b", drift_loss=1.0, pass_fail=True
        ),
    }
    weights = ScoringWeights(promote_margin=0.01)

    _stub_run_single(monkeypatch, canned)
    tour = asyncio.run(
        run_tournament(
            adapter=object(),
            parent_gen=_gen(tmp_path, "v0"),
            child_gen=_gen(tmp_path, "v1"),
            board=_board(),
            weights=weights,
            config=runtime_config(tmp_path),
            workspace_root=tmp_path,
            epoch_id="e0",
        )
    )

    _stub_run_single(monkeypatch, canned)
    match = asyncio.run(
        run_matchup(
            adapter=object(),
            left_gen=_gen(tmp_path, "v0"),
            right_gen=_gen(tmp_path, "v1"),
            board=_board(),
            weights=weights,
            config=runtime_config(tmp_path),
            workspace_root=tmp_path,
            epoch_id="e0",
        )
    )

    assert match.outcome.decision == tour.outcome.decision == "promoted"
    assert match.outcome.delta_scalar == pytest.approx(tour.outcome.delta_scalar)
    assert match.parent_agg["scalar"] == pytest.approx(tour.parent_agg["scalar"])
    assert match.child_agg["scalar"] == pytest.approx(tour.child_agg["scalar"])


def test_run_matchup_honours_board_subset(monkeypatch, tmp_path):
    """A board_subset limits the duel to the named entries (racing rung)."""
    log: list = []
    canned = {
        ("v0", "entry_a"): _loss(
            generation_id="v0", entry_id="entry_a", drift_loss=1.0, pass_fail=True
        ),
        ("v1", "entry_a"): _loss(
            generation_id="v1", entry_id="entry_a", drift_loss=0.5, pass_fail=True
        ),
    }
    _stub_run_single(monkeypatch, canned, log=log)
    result = asyncio.run(
        run_matchup(
            adapter=object(),
            left_gen=_gen(tmp_path, "v0"),
            right_gen=_gen(tmp_path, "v1"),
            board=_board(),
            weights=ScoringWeights(),
            config=runtime_config(tmp_path),
            workspace_root=tmp_path,
            epoch_id="e0",
            board_subset=("entry_a",),
        )
    )
    # Only entry_a ran (subset of the 2-entry board) — for both sides.
    assert {e for _, e in log} == {"entry_a"}
    assert result.parent_agg["entry_count"] == 1


def test_run_matchup_replicates_average_losses(monkeypatch, tmp_path):
    """With replicates>1 the per-entry drift loss is averaged across runs."""
    # Two runs with different drift losses; the average should be used.
    seq = {"v1_entry_a": [0.0, 1.0]}  # mean 0.5
    calls = {"v1_entry_a": 0}

    async def fake_run_single(
        *, adapter, generation, entry, weights, config, workspace_root, epoch_id, side, match_id=""
    ):
        del adapter, weights, config, workspace_root, epoch_id, side, match_id
        key = f"{generation.id}_{entry.id}"
        if generation.id == "v1":
            i = calls[key]
            calls[key] += 1
            return _loss(
                generation_id="v1", entry_id=entry.id, drift_loss=seq[key][i], pass_fail=True
            )
        return _loss(generation_id="v0", entry_id=entry.id, drift_loss=2.0, pass_fail=True)

    monkeypatch.setattr(runner_mod, "_run_single", fake_run_single)

    board = [BoardEntry(id="entry_a", kind="single_turn", wall_clock_budget_seconds=60, input="x")]
    result = asyncio.run(
        run_matchup(
            adapter=object(),
            left_gen=_gen(tmp_path, "v0"),
            right_gen=_gen(tmp_path, "v1"),
            board=board,
            weights=ScoringWeights(),
            config=runtime_config(tmp_path),
            workspace_root=tmp_path,
            epoch_id="e0",
            replicates=2,
        )
    )
    # Challenger drift_loss_mean is the average of the two replicate runs.
    assert result.child_agg["drift_loss_mean"] == pytest.approx(0.5)


def test_run_matchup_applies_diff_complexity_to_the_correct_competitor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Each side's source diff reaches only that side's aggregate and gate."""

    canned = {
        (generation_id, entry.id): _loss(
            generation_id=generation_id,
            entry_id=entry.id,
            drift_loss=1.0,
            pass_fail=True,
        )
        for generation_id in ("v0", "v1")
        for entry in _board()
    }
    _stub_run_single(monkeypatch, canned)
    right_diff = {"added": 4, "removed": 1, "patches": 2}

    result = asyncio.run(
        run_matchup(
            adapter=object(),
            left_gen=_gen(tmp_path, "v0"),
            right_gen=_gen(tmp_path, "v1"),
            board=_board(),
            weights=ScoringWeights(diff_complexity_weight=0.1),
            config=runtime_config(tmp_path),
            workspace_root=tmp_path,
            epoch_id="e0",
            right_diff_size=right_diff,
        )
    )

    assert "diff_size" not in result.parent_agg
    assert result.child_agg["diff_size"] == right_diff
    assert result.child_agg["scalar_components"]["diff_complexity"] == pytest.approx(0.7)


def _config_seq(tmp_path: Path) -> RuntimeConfig:
    """A config with ``parallelism=1`` so board units launch one batch at a time."""

    async def harness_call(system: str, user: str, model: str) -> str:
        return ""

    async def aux_call(system: str, user: str, model: str) -> str:
        return ""

    return RuntimeConfig(
        instance_id="test",
        workspace_root=tmp_path,
        target_call_llm=harness_call,
        evaluation_call_llm=aux_call,
        parallelism=1,
    )


def _big_board(n: int) -> list[BoardEntry]:
    return [
        BoardEntry(id=f"entry_{i}", kind="single_turn", wall_clock_budget_seconds=60, input="x")
        for i in range(n)
    ]


def test_run_matchup_budget_returns_partial_aggregate(monkeypatch, tmp_path):
    """A tiny matchup budget stops launching units and marks the rest budget-exceeded.

    The first board unit sleeps past the (tiny) budget; every later unit is
    NOT launched — it is recorded as a budget-exceeded LossProfile for BOTH
    sides — so the duel returns a PARTIAL aggregate instead of grinding the
    whole board.
    """
    board = _big_board(4)
    ran: list[str] = []

    async def slow_run_single(
        *, adapter, generation, entry, weights, config, workspace_root, epoch_id, side, match_id=""
    ):
        del adapter, weights, config, workspace_root, epoch_id, side, match_id
        ran.append(entry.id)
        await asyncio.sleep(0.05)  # push the running total past the tiny budget
        return _loss(generation_id=generation.id, entry_id=entry.id, drift_loss=1.0, pass_fail=True)

    monkeypatch.setattr(runner_mod, "_run_single", slow_run_single)

    result = asyncio.run(
        run_matchup(
            adapter=object(),
            left_gen=_gen(tmp_path, "v0"),
            right_gen=_gen(tmp_path, "v1"),
            board=board,
            weights=ScoringWeights(),
            config=_config_seq(tmp_path),
            workspace_root=tmp_path,
            epoch_id="e0",
            match_id="racing-final",
            matchup_budget_seconds=0.01,  # spent after the first unit's sleep
        )
    )

    # Not every entry was launched — the budget cut the sweep short.
    launched = set(ran)
    assert launched != {e.id for e in board}, "budget did not stop launching units"

    # Every board entry STILL has a (left, right) loss pair — the partial
    # aggregate covers the full board, with skipped units synthesised.
    assert set(result.per_entry_losses) == {e.id for e in board}

    # At least one skipped unit is marked budget-exceeded on BOTH sides.
    skipped_ids = {e.id for e in board} - launched
    assert skipped_ids, "expected some units to be skipped"
    for entry_id in skipped_ids:
        left_loss, right_loss = result.per_entry_losses[entry_id]
        assert left_loss.wall_clock_budget_exceeded is True
        assert right_loss.wall_clock_budget_exceeded is True

    # A skipped unit is persisted (cache hit next time): re-running with the
    # SAME (now generous) budget reuses every persisted unit and launches
    # NOTHING new.
    ran.clear()
    asyncio.run(
        run_matchup(
            adapter=object(),
            left_gen=_gen(tmp_path, "v0"),
            right_gen=_gen(tmp_path, "v1"),
            board=board,
            weights=ScoringWeights(),
            config=_config_seq(tmp_path),
            workspace_root=tmp_path,
            epoch_id="e0",
            match_id="racing-final",
            matchup_budget_seconds=1000.0,
            fast=True,  # cache-first reuse of every persisted unit
        )
    )
    assert ran == [], "persisted budget-exceeded units were not reused as cache hits"


def test_run_matchup_unset_budget_runs_every_unit(monkeypatch, tmp_path):
    """With no budget set, every board unit × both sides runs (current behaviour)."""
    board = _big_board(4)
    ran: list[tuple[str, str]] = []

    async def fast_run_single(
        *, adapter, generation, entry, weights, config, workspace_root, epoch_id, side, match_id=""
    ):
        del adapter, weights, config, workspace_root, epoch_id, side, match_id
        ran.append((generation.id, entry.id))
        return _loss(generation_id=generation.id, entry_id=entry.id, drift_loss=1.0, pass_fail=True)

    monkeypatch.setattr(runner_mod, "_run_single", fast_run_single)

    result = asyncio.run(
        run_matchup(
            adapter=object(),
            left_gen=_gen(tmp_path, "v0"),
            right_gen=_gen(tmp_path, "v1"),
            board=board,
            weights=ScoringWeights(),
            config=_config_seq(tmp_path),
            workspace_root=tmp_path,
            epoch_id="e0",
            # matchup_budget_seconds omitted ⇒ uncapped, byte-identical to today.
        )
    )

    # Both sides of every board entry ran — nothing skipped.
    assert {entry_id for _, entry_id in ran} == {e.id for e in board}
    assert {gen_id for gen_id, _ in ran} == {"v0", "v1"}
    assert len(ran) == 2 * len(board)
    for entry_id in result.per_entry_losses:
        left_loss, right_loss = result.per_entry_losses[entry_id]
        assert left_loss.wall_clock_budget_exceeded is False
        assert right_loss.wall_clock_budget_exceeded is False


# ---------------------------------------------------------------------------
# cross-matchup parallelism — one shared semaphore caps the whole round
# ---------------------------------------------------------------------------


def _config_par(tmp_path: Path, parallelism: int) -> RuntimeConfig:
    async def harness_call(system: str, user: str, model: str) -> str:
        return ""

    async def aux_call(system: str, user: str, model: str) -> str:
        return ""

    return RuntimeConfig(
        instance_id="test",
        workspace_root=tmp_path,
        target_call_llm=harness_call,
        evaluation_call_llm=aux_call,
        parallelism=parallelism,
    )


class _PeakConcurrencyProbe:
    """Stubs ``_run_single`` to track the live concurrent-run peak.

    Each fake run increments a live counter, yields to the loop (so all
    admitted runs overlap), records the running peak, then decrements.
    """

    def __init__(self) -> None:
        self.live = 0
        self.peak = 0

    def install(self, monkeypatch) -> None:
        async def fake_run_single(
            *,
            adapter,
            generation,
            entry,
            weights,
            config,
            workspace_root,
            epoch_id,
            side,
            match_id="",
        ):
            del adapter, weights, config, workspace_root, epoch_id, side, match_id
            self.live += 1
            self.peak = max(self.peak, self.live)
            try:
                # Yield repeatedly so every admitted run is in flight at once
                # if the semaphore lets it be — that is what the peak measures.
                for _ in range(5):
                    await asyncio.sleep(0)
            finally:
                self.live -= 1
            return _loss(
                generation_id=generation.id, entry_id=entry.id, drift_loss=1.0, pass_fail=True
            )

        monkeypatch.setattr(runner_mod, "_run_single", fake_run_single)


def _run_two_concurrent_matchups(
    monkeypatch, tmp_path, *, parallelism: int, shared: asyncio.Semaphore | None
) -> int:
    """Run two matchups concurrently and return the peak ``_run_single`` count.

    Both matchups run in FULL mode, so each admitted board unit fans out
    into two ``_run_single`` calls (champion + challenger) — the semaphore
    counts board units, so the ``_run_single`` peak is ``2 × board-unit
    concurrency``. Returns that observed peak.
    """
    probe = _PeakConcurrencyProbe()
    probe.install(monkeypatch)
    board = _big_board(4)

    def _matchup(right_id: str, match_id: str):
        return run_matchup(
            adapter=object(),
            left_gen=_gen(tmp_path, "v0"),
            right_gen=_gen(tmp_path, right_id),
            board=board,
            weights=ScoringWeights(),
            config=_config_par(tmp_path, parallelism),
            workspace_root=tmp_path,
            epoch_id="e0",
            match_id=match_id,
            unit_semaphore=shared,
        )

    async def _two() -> None:
        await asyncio.gather(_matchup("v1", "m1"), _matchup("v2", "m2"))

    asyncio.run(_two())
    return probe.peak


def test_shared_unit_semaphore_caps_concurrent_matchups(monkeypatch, tmp_path):
    """Two matchups sharing ONE semaphore stay under the single global cap.

    parallelism=2 caps BOARD UNITS at 2 across BOTH matchups combined, even
    though they are scheduled concurrently. Each full-mode board unit fans
    out into 2 ``_run_single`` calls (champion + challenger), so the
    ``_run_single`` peak is bounded by ``2 × parallelism`` = 4 — never the
    ``2 × (2 × parallelism)`` = 8 two independent caps would admit.
    """
    parallelism = 2
    shared = asyncio.Semaphore(parallelism)
    peak = _run_two_concurrent_matchups(
        monkeypatch, tmp_path, parallelism=parallelism, shared=shared
    )
    assert (
        peak <= 2 * parallelism
    ), f"shared semaphore breached: _run_single peak {peak} > 2×cap {2 * parallelism}"
    # The cap was actually exercised (work really did overlap across matchups).
    assert peak >= 1


def test_unshared_semaphore_each_matchup_caps_itself(monkeypatch, tmp_path):
    """Without a shared semaphore, two concurrent matchups each fill their own
    cap — the global peak overshoots a single matchup's cap. This is the
    overshoot the shared semaphore corrects; the contrast pins WHY it exists.
    """
    parallelism = 2
    peak = _run_two_concurrent_matchups(monkeypatch, tmp_path, parallelism=parallelism, shared=None)
    # Two independent board-unit caps of 2 ⇒ up to 4 board units ⇒ up to 8
    # ``_run_single`` calls — strictly more than one matchup's 2×parallelism=4.
    assert peak > 2 * parallelism


def test_average_losses_majority_pass_vote():
    """_average_losses takes a strict-majority vote for pass_fail."""
    from zicato.tournament.runner import _average_losses

    runs = [
        {"e": _loss(generation_id="v1", entry_id="e", drift_loss=1.0, pass_fail=True)},
        {"e": _loss(generation_id="v1", entry_id="e", drift_loss=1.0, pass_fail=True)},
        {"e": _loss(generation_id="v1", entry_id="e", drift_loss=1.0, pass_fail=False)},
    ]
    out = _average_losses(runs)
    assert out["e"].pass_fail is True  # 2 of 3 passed

    runs2 = [
        {"e": _loss(generation_id="v1", entry_id="e", drift_loss=1.0, pass_fail=True)},
        {"e": _loss(generation_id="v1", entry_id="e", drift_loss=1.0, pass_fail=False)},
    ]
    out2 = _average_losses(runs2)
    assert out2["e"].pass_fail is False  # tie is not a strict majority
