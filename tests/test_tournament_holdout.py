"""Train/holdout holdout-confirmation through ``run_tournament``.

These exercise the full-mode runner end-to-end (with ``_run_single``
stubbed) to confirm the holdout-gated promotion of OVERFITTING.md §12 #1:

* a challenger that wins on the TRAIN slice but REGRESSES on the holdout
  is rejected (``holdout_not_confirmed``) — board memorization is caught;
* a challenger that wins on the train slice AND holds the holdout is
  promoted, and the reported scalar is the TRAIN scalar (selection steers
  on train, the holdout is confirmation-only);
* with no holdout (board too small / disabled), behaviour is unchanged.

The holdout slice is declared with the explicit ``holdout`` tag so the
split is deterministic regardless of id-hash, keeping the assertions
crisp.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

import zicato.tournament.runner as runner_mod
from zicato.board.split import HOLDOUT_TAG
from zicato.core import (
    BoardEntry,
    DriftCount,
    ExpectationResult,
    Generation,
    LossProfile,
    RuntimeConfig,
    ScoringWeights,
)
from zicato.tournament.runner import run_tournament


def _loss(*, generation_id: str, entry_id: str, drift_loss: float) -> LossProfile:
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
        expectation_result=ExpectationResult(kind="predicate", passed=True),
        drift_loss=drift_loss,
        pass_fail=True,
    )


def _stub_run_single(
    monkeypatch: pytest.MonkeyPatch,
    canned: dict[tuple[str, str], LossProfile],
) -> None:
    async def fake_run_single(
        *,
        adapter: Any,
        generation: Generation,
        entry: BoardEntry,
        weights: ScoringWeights,
        config: RuntimeConfig,
        workspace_root: Path,
        epoch_id: str,
        side: str,
        match_id: str = "",
    ) -> LossProfile:
        del adapter, weights, config, workspace_root, epoch_id, side, match_id
        return canned[(generation.id, entry.id)]

    monkeypatch.setattr(runner_mod, "_run_single", fake_run_single)


def _board() -> list[BoardEntry]:
    # Four train entries + one explicitly-tagged holdout entry.
    train = [
        BoardEntry(id=f"t{i}", kind="single_turn", wall_clock_budget_seconds=60, input="x")
        for i in range(4)
    ]
    holdout = BoardEntry(
        id="h0",
        kind="single_turn",
        wall_clock_budget_seconds=60,
        tags=(HOLDOUT_TAG,),
        input="x",
    )
    return [*train, holdout]


def _runtime(tmp_path: Path) -> RuntimeConfig:
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


def _gen(tmp_path: Path, gen_id: str, parent: str | None) -> Generation:
    return Generation(
        id=gen_id,
        epoch_id="e0",
        parent_id=parent,
        snapshot_root=tmp_path / f"snap_{gen_id}",
        created_at="2024-01-01T00:00:00Z",
    )


def _run(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, holdout_child_drift: float) -> Any:
    board = _board()
    canned: dict[tuple[str, str], LossProfile] = {}
    for e in board[:4]:  # train: champion 2.0 -> challenger 1.0 (clear train win)
        canned[("v0", e.id)] = _loss(generation_id="v0", entry_id=e.id, drift_loss=2.0)
        canned[("v1", e.id)] = _loss(generation_id="v1", entry_id=e.id, drift_loss=1.0)
    # Holdout: champion 2.0; the challenger's holdout drift is the variable.
    canned[("v0", "h0")] = _loss(generation_id="v0", entry_id="h0", drift_loss=2.0)
    canned[("v1", "h0")] = _loss(generation_id="v1", entry_id="h0", drift_loss=holdout_child_drift)
    _stub_run_single(monkeypatch, canned)
    return asyncio.run(
        run_tournament(
            adapter=object(),
            parent_gen=_gen(tmp_path, "v0", None),
            child_gen=_gen(tmp_path, "v1", "v0"),
            board=board,
            weights=ScoringWeights(promote_margin=0.1),
            config=_runtime(tmp_path),
            workspace_root=tmp_path,
            epoch_id="e0",
        )
    )


def test_holdout_regression_flips_a_train_win_to_reject(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The challenger improves the train slice (2.0 -> 1.0) but the holdout
    # entry REGRESSES hard (2.0 -> 5.0): a memorized win, caught by the gate.
    result = _run(monkeypatch, tmp_path, holdout_child_drift=5.0)
    assert result.outcome.decision == "rejected"
    assert "holdout_not_confirmed" in result.outcome.reason
    # The reported / selection scalar is the TRAIN scalar (drift mean 1.0),
    # NOT a blend that includes the holdout's 5.0.
    assert result.child_agg["drift_loss_mean"] == pytest.approx(1.0)


def test_holdout_confirmation_promotes_and_reports_train_scalar(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The challenger improves both train and holdout — a real, general win.
    result = _run(monkeypatch, tmp_path, holdout_child_drift=1.0)
    assert result.outcome.decision == "promoted"
    # Selection steers on the train scalar; the holdout never blends in.
    assert result.child_agg["drift_loss_mean"] == pytest.approx(1.0)
    assert result.parent_agg["drift_loss_mean"] == pytest.approx(2.0)
