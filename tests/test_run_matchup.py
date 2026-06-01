"""Tests for ``run_matchup`` — the selection-layer duel runner.

Proves the new ``run_matchup`` entry point (1) reproduces the gauntlet's
gate verdict against the historical ``run_tournament`` for the same
champion-vs-challenger pair (gauntlet-equivalence), (2) honours
``board_subset`` (racing rungs), and (3) averages per-entry losses under
``replicates`` while keeping the gate composition unchanged. The runner's
subprocess ``_run_single`` is stubbed with canned losses — no live runs.
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
        epoch_id="e0",
        parent_id=None,
        snapshot_root=tmp_path / f"snap_{gen_id}",
        created_at="2024-01-01T00:00:00Z",
    )


def _stub_run_single(monkeypatch, canned, *, log: list | None = None):
    async def fake_run_single(
        *, adapter, generation, entry, weights, config, workspace_root, epoch_id, side
    ):
        del adapter, weights, config, workspace_root, epoch_id, side
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
            config=_config(tmp_path),
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
            config=_config(tmp_path),
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
            config=_config(tmp_path),
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
        *, adapter, generation, entry, weights, config, workspace_root, epoch_id, side
    ):
        del adapter, weights, config, workspace_root, epoch_id, side
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
            weights=ScoringWeights(drift_weight=1.0),
            config=_config(tmp_path),
            workspace_root=tmp_path,
            epoch_id="e0",
            replicates=2,
        )
    )
    # Challenger drift_loss_mean is the average of the two replicate runs.
    assert result.child_agg["drift_loss_mean"] == pytest.approx(0.5)


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
