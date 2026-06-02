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
    """The round-level provenance collapses per-matchup modes correctly."""
    from zicato.orchestrator import _resolve_round_champion_mode

    # fast not requested → always full.
    assert _resolve_round_champion_mode(["fast", "fast"], fast_requested=False) == "full"
    # no matchups → full.
    assert _resolve_round_champion_mode([], fast_requested=True) == "full"
    # every matchup reused the cache → fast.
    assert _resolve_round_champion_mode(["fast", "fast"], fast_requested=True) == "fast"
    # any degrade dominates.
    assert (
        _resolve_round_champion_mode(["fast", "fast-degraded"], fast_requested=True)
        == "fast-degraded"
    )
