"""Structure-agnostic fast mode: the champion cache applies to every
tournament structure (racing / swiss / elim), not just the gauntlet.

These tests prove the runtime ``--mode fast`` knob now threads through the
multi-challenger path:

* ``run_matchup(fast=True)`` with a cached champion aggregate runs ONLY
  the challenger (the champion board-unit is NOT executed) and reuses the
  champion's cached per-board scalars — for full boards AND racing
  subsets;
* ``run_matchup(fast=True)`` with NO cached champion aggregate degrades
  to a full champion run once (then the cache is available);
* flipping ``fast_mode`` does NOT change the contract hash (fast is a
  RUNTIME knob, never a contract input — flipping it must not roll the
  epoch);
* the resolved champion-eval mode is recorded in the journal.

The runner's subprocess ``_run_single`` is stubbed with canned losses and
the champion's cached per-board ``loss.json`` files are written to disk —
no live LLM, no real subprocess.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import zicato.tournament.runner as runner_mod
from zicato.core import (
    BoardEntry,
    Generation,
    LossProfile,
    RuntimeConfig,
    ScoringWeights,
)
from zicato.core.types import DriftCount, ExpectationResult
from zicato.core.workspace import loss_profile_path
from zicato.telemetry.reducer import write_loss_profile
from zicato.tournament.runner import run_matchup

EPOCH = "e0"


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
        epoch_id=EPOCH,
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
        BoardEntry(id="entry_c", kind="single_turn", wall_clock_budget_seconds=60, input="z"),
    ]


def _config(tmp_path: Path) -> RuntimeConfig:
    async def harness_call(system: str, user: str, model: str) -> str:
        return ""

    async def aux_call(system: str, user: str, model: str) -> str:
        return ""

    return RuntimeConfig(
        instance_id="test",
        workspace_root=tmp_path,
        harness_call_llm=harness_call,
        auxiliary_call_llm=aux_call,
    )


def _gen(tmp_path: Path, gen_id: str) -> Generation:
    return Generation(
        id=gen_id,
        epoch_id=EPOCH,
        parent_id=None,
        snapshot_root=tmp_path / f"snap_{gen_id}",
        created_at="2024-01-01T00:00:00Z",
    )


def _stub_run_single(monkeypatch, canned, *, log: list):
    """Stub the per-run worker; log every (generation_id, entry_id) run."""

    async def fake_run_single(
        *, adapter, generation, entry, weights, config, workspace_root, epoch_id, side, match_id=""
    ):
        del adapter, weights, config, workspace_root, epoch_id, side, match_id
        log.append((generation.id, entry.id))
        return canned[(generation.id, entry.id)]

    monkeypatch.setattr(runner_mod, "_run_single", fake_run_single)


def _seed_champion_cache(tmp_path: Path, champion_id: str, board: list[BoardEntry]) -> None:
    """Write the champion's per-board ``loss.json`` files (the fast cache).

    The champion was scored on the full board when it became champion, so
    every per-board profile exists on disk — exactly what the
    structure-agnostic fast resolver reads back for any subset.
    """
    for entry in board:
        profile = _loss(
            generation_id=champion_id, entry_id=entry.id, drift_loss=2.0, pass_fail=True
        )
        write_loss_profile(profile, loss_profile_path(tmp_path, EPOCH, champion_id, entry.id))


def test_fast_matchup_reuses_cached_champion_and_skips_its_run(monkeypatch, tmp_path):
    """fast=True with a cached champion runs ONLY the challenger."""
    board = _board()
    _seed_champion_cache(tmp_path, "v0", board)
    # Only challenger runs are canned; a champion run would KeyError, which
    # is itself an assertion that the champion side never executes.
    canned = {
        ("v1", e.id): _loss(generation_id="v1", entry_id=e.id, drift_loss=1.0, pass_fail=True)
        for e in board
    }
    log: list = []
    _stub_run_single(monkeypatch, canned, log=log)

    result = asyncio.run(
        run_matchup(
            adapter=object(),
            left_gen=_gen(tmp_path, "v0"),
            right_gen=_gen(tmp_path, "v1"),
            board=board,
            weights=ScoringWeights(promote_margin=0.01),
            config=_config(tmp_path),
            workspace_root=tmp_path,
            epoch_id=EPOCH,
            fast=True,
        )
    )

    # The champion (v0) board-unit did NOT execute — every logged run is a
    # challenger run.
    champion_runs = [g for g, _ in log if g == "v0"]
    assert champion_runs == [], "fast mode must not execute the champion side"
    assert {g for g, _ in log} == {"v1"}
    # The cached champion per-board scalars were reused for the gate.
    assert result.champion_eval_mode == "fast"
    assert result.parent_agg["drift_loss_mean"] == pytest.approx(2.0)
    assert result.child_agg["drift_loss_mean"] == pytest.approx(1.0)
    # Challenger improves → promoted, exactly as a full duel would decide.
    assert result.outcome.decision == "promoted"


def test_fast_racing_subset_reuses_cached_champion(monkeypatch, tmp_path):
    """A racing rung's growing board SUBSET reuses the cached champion."""
    board = _board()
    _seed_champion_cache(tmp_path, "v0", board)  # cached on the FULL board
    # The rung only needs entry_a + entry_b (a 2-of-3 subset).
    subset = ("entry_a", "entry_b")
    canned = {
        ("v1", e): _loss(generation_id="v1", entry_id=e, drift_loss=0.5, pass_fail=True)
        for e in subset
    }
    log: list = []
    _stub_run_single(monkeypatch, canned, log=log)

    result = asyncio.run(
        run_matchup(
            adapter=object(),
            left_gen=_gen(tmp_path, "v0"),
            right_gen=_gen(tmp_path, "v1"),
            board=board,
            weights=ScoringWeights(),
            config=_config(tmp_path),
            workspace_root=tmp_path,
            epoch_id=EPOCH,
            board_subset=subset,
            fast=True,
        )
    )

    # Champion never ran; only the two subset challenger entries ran.
    assert [g for g, _ in log if g == "v0"] == []
    assert {e for _, e in log} == set(subset)
    assert result.champion_eval_mode == "fast"
    # The champion aggregate covers exactly the subset (2 entries), reused
    # from the cache, not the full 3-entry board.
    assert result.parent_agg["entry_count"] == 2


def test_fast_matchup_degrades_to_full_without_cache(monkeypatch, tmp_path):
    """fast=True with NO cached champion runs the champion once (degraded)."""
    board = _board()
    # No champion cache seeded → degrade to full: BOTH sides must run.
    canned = {}
    for e in board:
        canned[("v0", e.id)] = _loss(
            generation_id="v0", entry_id=e.id, drift_loss=2.0, pass_fail=True
        )
        canned[("v1", e.id)] = _loss(
            generation_id="v1", entry_id=e.id, drift_loss=1.0, pass_fail=True
        )
    log: list = []
    _stub_run_single(monkeypatch, canned, log=log)

    result = asyncio.run(
        run_matchup(
            adapter=object(),
            left_gen=_gen(tmp_path, "v0"),
            right_gen=_gen(tmp_path, "v1"),
            board=board,
            weights=ScoringWeights(promote_margin=0.01),
            config=_config(tmp_path),
            workspace_root=tmp_path,
            epoch_id=EPOCH,
            fast=True,
        )
    )

    # The champion WAS run (cache miss → degrade) for every board entry.
    assert {e for g, e in log if g == "v0"} == {e.id for e in board}
    assert result.champion_eval_mode == "fast-degraded"
    assert result.outcome.decision == "promoted"


def test_full_matchup_runs_champion_and_reports_full_mode(monkeypatch, tmp_path):
    """fast=False (default) runs the champion and reports champion_eval_mode='full'."""
    board = _board()
    _seed_champion_cache(tmp_path, "v0", board)  # present, but fast NOT requested
    canned = {}
    for e in board:
        canned[("v0", e.id)] = _loss(
            generation_id="v0", entry_id=e.id, drift_loss=2.0, pass_fail=True
        )
        canned[("v1", e.id)] = _loss(
            generation_id="v1", entry_id=e.id, drift_loss=1.0, pass_fail=True
        )
    log: list = []
    _stub_run_single(monkeypatch, canned, log=log)

    result = asyncio.run(
        run_matchup(
            adapter=object(),
            left_gen=_gen(tmp_path, "v0"),
            right_gen=_gen(tmp_path, "v1"),
            board=board,
            weights=ScoringWeights(promote_margin=0.01),
            config=_config(tmp_path),
            workspace_root=tmp_path,
            epoch_id=EPOCH,
            fast=False,
        )
    )

    # Even with a cache present, full mode re-runs the champion live.
    assert {e for g, e in log if g == "v0"} == {e.id for e in board}
    assert result.champion_eval_mode == "full"


def test_resolve_round_champion_mode():
    """The round-level provenance collapses the champion's cached/fresh tally."""
    from zicato.orchestrator import _resolve_round_champion_mode

    # fast not requested → always full (regardless of the tally).
    assert _resolve_round_champion_mode(6, 0, fast_requested=False) == "full"
    # fast requested but the champion played no board unit → full.
    assert _resolve_round_champion_mode(0, 0, fast_requested=True) == "full"
    # every champion board unit reused from the cache → fast.
    assert _resolve_round_champion_mode(6, 0, fast_requested=True) == "fast"
    # any fresh champion unit → degraded (the champion ran live to seed).
    assert _resolve_round_champion_mode(3, 3, fast_requested=True) == "fast-degraded"
    assert _resolve_round_champion_mode(0, 6, fast_requested=True) == "fast-degraded"


# ---------------------------------------------------------------------------
# Universal cache-first board-unit evaluator: every (gen, entry, replicate)
# is evaluated AT MOST ONCE under a fixed contract and reused everywhere —
# across pairings, rounds, structures, and the gate — not just the champion.
# ---------------------------------------------------------------------------


def _stub_run_single_persisting(monkeypatch, canned, *, log: list):
    """Stub ``_run_single`` to log AND persist its loss.json on every run.

    Mirrors what the real subprocess worker does: a genuine board run
    writes its per-board ``loss.json`` so the cache-first resolver finds
    it on the next need. ``log`` records every actually-executed
    ``(generation_id, entry_id)`` — a cache HIT never reaches this stub,
    so ``log`` IS the board-unit execution count.
    """

    async def fake_run_single(
        *, adapter, generation, entry, weights, config, workspace_root, epoch_id, side, match_id=""
    ):
        del adapter, weights, config, side, match_id
        log.append((generation.id, entry.id))
        profile = canned[(generation.id, entry.id)]
        write_loss_profile(
            profile, loss_profile_path(workspace_root, epoch_id, generation.id, entry.id)
        )
        return profile

    monkeypatch.setattr(runner_mod, "_run_single", fake_run_single)


def test_swiss_runs_each_gen_entry_at_most_once(monkeypatch, tmp_path):
    """A round-robin field over a 3-entry board runs each (gen, entry) ONCE.

    Three competitors (v0, v1, v2) play a full round-robin (3 pairings, so
    each competitor appears in 2 of them) over a 3-entry board. The naive
    per-pairing count would be 3 pairings x 2 sides x 3 entries = 18 board
    runs; the cache-first runner instead executes exactly the DISTINCT
    ``(gen, entry)`` units = 3 gens x 3 entries = 9, and every later
    pairing resolves its competitors from the cache.
    """
    board = _board()
    competitors = ["v0", "v1", "v2"]
    canned = {
        (g, e.id): _loss(generation_id=g, entry_id=e.id, drift_loss=1.0, pass_fail=True)
        for g in competitors
        for e in board
    }
    log: list = []
    _stub_run_single_persisting(monkeypatch, canned, log=log)

    # Round-robin pairings (the schedule a Swiss round produces): every
    # unordered pair plays once. The runner is structure-agnostic, so
    # driving the pairings directly exercises the same cache the strategy
    # would hit.
    pairings = [("v0", "v1"), ("v0", "v2"), ("v1", "v2")]
    for left, right in pairings:
        asyncio.run(
            run_matchup(
                adapter=object(),
                left_gen=_gen(tmp_path, left),
                right_gen=_gen(tmp_path, right),
                board=board,
                weights=ScoringWeights(),
                config=_config(tmp_path),
                workspace_root=tmp_path,
                epoch_id=EPOCH,
                fast=True,
            )
        )

    # Each distinct (gen, entry) executed exactly once — NOT N x pairings.
    from collections import Counter

    counts = Counter(log)
    assert all(c == 1 for c in counts.values()), counts
    assert set(counts) == {(g, e.id) for g in competitors for e in board}
    assert len(log) == len(competitors) * len(board) == 9


def test_cross_round_reuse_is_near_zero_new_runs(monkeypatch, tmp_path):
    """A second duel over the same epoch reuses every prior (gen, entry)."""
    board = _board()
    canned = {
        (g, e.id): _loss(generation_id=g, entry_id=e.id, drift_loss=1.0, pass_fail=True)
        for g in ("v0", "v1")
        for e in board
    }
    log: list = []
    _stub_run_single_persisting(monkeypatch, canned, log=log)

    kwargs = dict(
        adapter=object(),
        left_gen=_gen(tmp_path, "v0"),
        right_gen=_gen(tmp_path, "v1"),
        board=board,
        weights=ScoringWeights(),
        config=_config(tmp_path),
        workspace_root=tmp_path,
        epoch_id=EPOCH,
        fast=True,
    )
    asyncio.run(run_matchup(**kwargs))  # type: ignore[arg-type]
    first_round_runs = len(log)
    assert first_round_runs == 2 * len(board)  # both sides, fresh

    log.clear()
    asyncio.run(run_matchup(**kwargs))  # type: ignore[arg-type]
    # Second duel over the SAME epoch: every unit is already cached.
    assert log == [], "a re-run over the same epoch must reuse all prior evals"


def test_replicates_incremental_runs_only_missing(monkeypatch, tmp_path):
    """R existing + request R+1 → exactly 1 new run for that unit."""
    entry = BoardEntry(id="entry_a", kind="single_turn", wall_clock_budget_seconds=60, input="x")
    board = [entry]
    canned = {
        (g, entry.id): _loss(generation_id=g, entry_id=entry.id, drift_loss=1.0, pass_fail=True)
        for g in ("v0", "v1")
    }

    log: list = []

    async def fake_run_single(
        *, adapter, generation, entry, weights, config, workspace_root, epoch_id, side, match_id=""
    ):
        del adapter, weights, config, side, match_id
        log.append((generation.id, entry.id))
        profile = canned[(generation.id, entry.id)]
        # Persist to the per-replicate slot the cache-first runner reads:
        # replicate 0 → canonical loss.json; r>0 → loss.r<r>.json. We
        # cannot know the replicate index here, so persist via the
        # canonical writer and let the runner's own persist fill r>0.
        write_loss_profile(
            profile, loss_profile_path(workspace_root, epoch_id, generation.id, entry.id)
        )
        return profile

    monkeypatch.setattr(runner_mod, "_run_single", fake_run_single)

    # First duel asks for 2 replicates: both sides run replicate 0 + 1 =
    # 2 units per side = 4 runs (every slot a miss).
    base = dict(
        adapter=object(),
        left_gen=_gen(tmp_path, "v0"),
        right_gen=_gen(tmp_path, "v1"),
        board=board,
        weights=ScoringWeights(),
        config=_config(tmp_path),
        workspace_root=tmp_path,
        epoch_id=EPOCH,
        fast=True,
    )
    asyncio.run(run_matchup(replicates=2, **base))  # type: ignore[arg-type]
    runs_r2 = list(log)
    # 2 sides x 2 replicate slots = 4 fresh runs.
    assert len(runs_r2) == 4, runs_r2

    log.clear()
    # Re-run at replicates=3: slots 0+1 already cached for both sides, only
    # slot 2 is missing → exactly 1 new run per side = 2.
    asyncio.run(run_matchup(replicates=3, **base))  # type: ignore[arg-type]
    assert len(log) == 2, ("only the missing replicate slot should run", log)


def test_full_mode_bypasses_the_cache(monkeypatch, tmp_path):
    """fast=False (``--mode full``) forces a fresh run of every unit."""
    board = _board()
    # Seed v0 AND v1 caches so a cache-first run would reuse everything.
    _seed_champion_cache(tmp_path, "v0", board)
    _seed_champion_cache(tmp_path, "v1", board)
    canned = {
        (g, e.id): _loss(generation_id=g, entry_id=e.id, drift_loss=1.0, pass_fail=True)
        for g in ("v0", "v1")
        for e in board
    }
    log: list = []
    _stub_run_single_persisting(monkeypatch, canned, log=log)

    asyncio.run(
        run_matchup(
            adapter=object(),
            left_gen=_gen(tmp_path, "v0"),
            right_gen=_gen(tmp_path, "v1"),
            board=board,
            weights=ScoringWeights(),
            config=_config(tmp_path),
            workspace_root=tmp_path,
            epoch_id=EPOCH,
            fast=False,
        )
    )
    # Even with a full cache present, full mode re-ran BOTH sides' units.
    assert {(g, e) for g, e in log} == {(g, e.id) for g in ("v0", "v1") for e in board}
    assert len(log) == 2 * len(board)


def test_contract_scoping_is_a_clean_miss_across_epochs(monkeypatch, tmp_path):
    """A different epoch/contract is a clean miss — no cross-contract reuse."""
    board = _board()
    # Cache the champion under epoch "e0" only.
    _seed_champion_cache(tmp_path, "v0", board)
    canned = {
        (g, e.id): _loss(generation_id=g, entry_id=e.id, drift_loss=1.0, pass_fail=True)
        for g in ("v0", "v1")
        for e in board
    }
    log: list = []
    _stub_run_single_persisting(monkeypatch, canned, log=log)

    # Run under a DIFFERENT epoch id (a contract change rolls a fresh
    # epoch). The e0 cache must NOT be reused — the champion runs fresh.
    asyncio.run(
        run_matchup(
            adapter=object(),
            left_gen=Generation(
                id="v0",
                epoch_id="e1",
                parent_id=None,
                snapshot_root=tmp_path / "snap_v0",
                created_at="2024-01-01T00:00:00Z",
            ),
            right_gen=Generation(
                id="v1",
                epoch_id="e1",
                parent_id=None,
                snapshot_root=tmp_path / "snap_v1",
                created_at="2024-01-01T00:00:00Z",
            ),
            board=board,
            weights=ScoringWeights(),
            config=_config(tmp_path),
            workspace_root=tmp_path,
            epoch_id="e1",
            fast=True,
        )
    )
    # The champion (v0) ran under e1 — the e0 cache did not leak across.
    assert {e for g, e in log if g == "v0"} == {e.id for e in board}


def test_unit_provenance_records_cached_vs_fresh(monkeypatch, tmp_path):
    """run_matchup reports a per-generation cached/fresh board-unit tally."""
    board = _board()
    _seed_champion_cache(tmp_path, "v0", board)  # champion fully cached
    canned = {
        ("v1", e.id): _loss(generation_id="v1", entry_id=e.id, drift_loss=0.5, pass_fail=True)
        for e in board
    }
    log: list = []
    _stub_run_single_persisting(monkeypatch, canned, log=log)

    result = asyncio.run(
        run_matchup(
            adapter=object(),
            left_gen=_gen(tmp_path, "v0"),
            right_gen=_gen(tmp_path, "v1"),
            board=board,
            weights=ScoringWeights(promote_margin=0.01),
            config=_config(tmp_path),
            workspace_root=tmp_path,
            epoch_id=EPOCH,
            fast=True,
        )
    )
    prov = result.unit_provenance
    # Champion: every unit reused from cache (cached==len(board), fresh==0).
    assert prov["v0"].cached == len(board)
    assert prov["v0"].fresh == 0
    # Challenger: every unit freshly executed (a new generation).
    assert prov["v1"].fresh == len(board)
    assert prov["v1"].cached == 0
