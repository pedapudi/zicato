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
from tests._runtime_builders import runtime_config
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
            config=runtime_config(tmp_path),
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


def _run_full_with_call_log(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    train_child_drift: float,
    weights: ScoringWeights,
) -> tuple[Any, list[tuple[str, str]]]:
    calls: list[tuple[str, str]] = []

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
        calls.append((generation.id, entry.id))
        drift = 2.0 if generation.id == "v0" else train_child_drift
        return _loss(generation_id=generation.id, entry_id=entry.id, drift_loss=drift)

    monkeypatch.setattr(runner_mod, "_run_single", fake_run_single)
    result = asyncio.run(
        run_tournament(
            adapter=object(),
            parent_gen=_gen(tmp_path, "v0", None),
            child_gen=_gen(tmp_path, "v1", "v0"),
            board=_board(),
            weights=weights,
            config=runtime_config(tmp_path),
            workspace_root=tmp_path,
            epoch_id="e0",
        )
    )
    return result, calls


def test_full_tournament_train_rejection_never_launches_holdout_units(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    result, calls = _run_full_with_call_log(
        monkeypatch,
        tmp_path,
        train_child_drift=3.0,
        weights=ScoringWeights(promote_margin=0.1),
    )

    assert result.outcome.decision == "rejected"
    assert all(entry_id != "h0" for _generation_id, entry_id in calls)
    assert result.holdout is None
    assert result.holdout_child_scalar is None

    from zicato.core.workspace import ladder_state_path

    assert not ladder_state_path(tmp_path, "e0").exists()


def test_full_tournament_exhausted_budget_never_launches_holdout_units(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from zicato.core.types import LadderConfig, OverfittingConfig

    weights = ScoringWeights(
        promote_margin=0.1,
        overfitting=OverfittingConfig(ladder=LadderConfig(budget=0)),
    )
    result, calls = _run_full_with_call_log(
        monkeypatch,
        tmp_path,
        train_child_drift=1.0,
        weights=weights,
    )

    assert result.outcome.decision == "promoted", "the train decision stands at exhaustion"
    assert all(entry_id != "h0" for _generation_id, entry_id in calls)
    assert result.holdout is not None
    assert result.holdout["holdout_consulted"] is False
    assert result.holdout["ladder_query_reserved"] is False
    assert result.holdout["ladder_budget_before_query"] == 0
    assert result.holdout["ladder_budget_remaining"] == 0
    assert result.holdout_child_scalar is None
    assert "h0" not in result.per_entry_losses


def test_full_tournament_reservation_failure_never_launches_holdout_units(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import zicato.tournament.governance as governance
    from zicato.tournament.governance import LadderStateError

    def fail_write(_path: Path, _data: object) -> None:
        raise OSError("state unavailable")

    monkeypatch.setattr(governance, "atomic_write_json", fail_write)
    calls: list[tuple[str, str]] = []

    async def fake_run_single(
        *, generation: Generation, entry: BoardEntry, **_kwargs: object
    ) -> LossProfile:
        calls.append((generation.id, entry.id))
        drift = 2.0 if generation.id == "v0" else 1.0
        return _loss(generation_id=generation.id, entry_id=entry.id, drift_loss=drift)

    monkeypatch.setattr(runner_mod, "_run_single", fake_run_single)
    with pytest.raises(LadderStateError, match="cannot persist Ladder state"):
        asyncio.run(
            run_tournament(
                adapter=object(),
                parent_gen=_gen(tmp_path, "v0", None),
                child_gen=_gen(tmp_path, "v1", "v0"),
                board=_board(),
                weights=ScoringWeights(promote_margin=0.1),
                config=runtime_config(tmp_path),
                workspace_root=tmp_path,
                epoch_id="e0",
            )
        )

    assert calls
    assert all(entry_id != "h0" for _generation_id, entry_id in calls)


# ---------------------------------------------------------------------------
# confirm_crowning_holdout — the non-gauntlet structures' champion-gate
# Ladder-mediated holdout confirmation (OVERFITTING.md §3/§4).
# ---------------------------------------------------------------------------


def _agg(scalar: float, *, pass_fail: bool | None = True) -> dict[str, Any]:
    """A minimal aggregate dict shaped like ``aggregate_generation_score``."""
    return {
        "scalar": scalar,
        "drift_loss_mean": scalar,
        "pass_rate": 1.0 if pass_fail else 0.0,
        "per_entry": {},
        "namespace_aggregates": {},
    }


def _confirm(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    board: list[BoardEntry],
    holdout_child_drift: float,
) -> Any:
    """Drive ``confirm_crowning_holdout`` for a champion-vs-survivor crowning duel."""
    from zicato.tournament.gate import GateOutcome
    from zicato.tournament.runner import confirm_crowning_holdout

    # The crowning duel already decided a TRAIN win (champion 2.0 -> survivor
    # 1.0). The holdout run is the only thing the helper executes itself.
    canned: dict[tuple[str, str], LossProfile] = {}
    for e in board:
        canned[("v0", e.id)] = _loss(generation_id="v0", entry_id=e.id, drift_loss=2.0)
        drift = holdout_child_drift if HOLDOUT_TAG in e.tags else 1.0
        canned[("v1", e.id)] = _loss(generation_id="v1", entry_id=e.id, drift_loss=drift)
    _stub_run_single(monkeypatch, canned)

    train_outcome = GateOutcome(
        decision="promoted", reason="", delta_scalar=-1.0, delta_pass_rate=0.0
    )
    return asyncio.run(
        confirm_crowning_holdout(
            adapter=object(),
            champion_gen=_gen(tmp_path, "v0", None),
            challenger_gen=_gen(tmp_path, "v1", "v0"),
            board=board,
            train_outcome=train_outcome,
            train_parent_agg=_agg(2.0),
            train_child_agg=_agg(1.0),
            weights=ScoringWeights(promote_margin=0.1),
            config=runtime_config(tmp_path),
            workspace_root=tmp_path,
            epoch_id="e0",
        )
    )


def test_confirm_crowning_holdout_flips_a_holdout_regression(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The survivor won on train but regresses on the holdout entry (1.0 -> 5.0
    # vs champion 2.0): the crowning promote flips to a holdout reject.
    outcome, block, holdout_scalar = _confirm(
        monkeypatch, tmp_path, board=_board(), holdout_child_drift=5.0
    )
    assert outcome.decision == "rejected"
    assert "holdout_not_confirmed" in outcome.reason
    assert block is not None and block["confirmed"] is False
    assert holdout_scalar == pytest.approx(5.0)


def test_confirm_crowning_holdout_confirms_a_general_win(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The survivor holds the holdout (1.0 vs champion 2.0): the crowning
    # promote stands, the evidence block confirms, and the budget decremented.
    outcome, block, holdout_scalar = _confirm(
        monkeypatch, tmp_path, board=_board(), holdout_child_drift=1.0
    )
    assert outcome.decision == "promoted"
    assert block is not None and block["confirmed"] is True
    assert holdout_scalar == pytest.approx(1.0)
    assert block["ladder_budget_remaining"] == block["ladder_budget_total"] - 1


def test_confirm_crowning_holdout_degrades_byte_identically_without_holdout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A board with NO holdout (no tag, below the split floor) ⇒ the helper
    # consults no holdout, charges no budget, and returns the train outcome
    # unchanged with no evidence block — byte-identical to today.
    board = [
        BoardEntry(id="only_a", kind="single_turn", wall_clock_budget_seconds=60, input="x"),
        BoardEntry(id="only_b", kind="single_turn", wall_clock_budget_seconds=60, input="x"),
    ]
    outcome, block, holdout_scalar = _confirm(
        monkeypatch, tmp_path, board=board, holdout_child_drift=5.0
    )
    assert outcome.decision == "promoted"  # the train outcome, untouched
    assert block is None
    assert holdout_scalar is None


def test_confirm_crowning_holdout_does_not_launch_after_budget_exhaustion(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An exhausted Ladder leaves the train decision standing without access.

    The holdout runner is the access boundary.  Reaching it after the durable
    query budget reaches zero would reveal uncharged evidence even if the
    Ladder later withheld that evidence from the decision record.
    """
    from zicato.core.types import LadderConfig, OverfittingConfig
    from zicato.tournament.gate import GateOutcome
    from zicato.tournament.runner import confirm_crowning_holdout

    async def fail_if_launched(**_kwargs: object) -> Any:
        pytest.fail("an exhausted Ladder must not launch a holdout matchup")

    monkeypatch.setattr(runner_mod, "run_matchup", fail_if_launched)
    train_outcome = GateOutcome(
        decision="promoted", reason="", delta_scalar=-1.0, delta_pass_rate=0.0
    )
    weights = ScoringWeights(
        promote_margin=0.1,
        overfitting=OverfittingConfig(ladder=LadderConfig(budget=0)),
    )

    outcome, block, holdout_scalar = asyncio.run(
        confirm_crowning_holdout(
            adapter=object(),
            champion_gen=_gen(tmp_path, "v0", None),
            challenger_gen=_gen(tmp_path, "v1", "v0"),
            board=_board(),
            train_outcome=train_outcome,
            train_parent_agg=_agg(2.0),
            train_child_agg=_agg(1.0),
            weights=weights,
            config=runtime_config(tmp_path),
            workspace_root=tmp_path,
            epoch_id="e0",
        )
    )

    assert outcome is train_outcome
    assert block is not None
    assert block["confirmed"] is None
    assert block["holdout_scalar"] is None
    assert block["holdout_consulted"] is False
    assert block["ladder_budget_remaining"] == 0
    assert block["ladder_query_reserved"] is False
    assert holdout_scalar is None


def test_confirm_crowning_holdout_does_not_launch_when_reservation_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import zicato.tournament.governance as governance
    from zicato.tournament.gate import GateOutcome
    from zicato.tournament.governance import LadderStateError
    from zicato.tournament.runner import confirm_crowning_holdout

    async def fail_if_launched(**_kwargs: object) -> Any:
        pytest.fail("a failed reservation must not launch a holdout matchup")

    def fail_write(_path: Path, _data: object) -> None:
        raise OSError("state unavailable")

    monkeypatch.setattr(runner_mod, "run_matchup", fail_if_launched)
    monkeypatch.setattr(governance, "atomic_write_json", fail_write)
    with pytest.raises(LadderStateError, match="cannot persist Ladder state"):
        asyncio.run(
            confirm_crowning_holdout(
                adapter=object(),
                champion_gen=_gen(tmp_path, "v0", None),
                challenger_gen=_gen(tmp_path, "v1", "v0"),
                board=_board(),
                train_outcome=GateOutcome(
                    decision="promoted",
                    reason="",
                    delta_scalar=-1.0,
                    delta_pass_rate=0.0,
                ),
                train_parent_agg=_agg(2.0),
                train_child_agg=_agg(1.0),
                weights=ScoringWeights(promote_margin=0.1),
                config=runtime_config(tmp_path),
                workspace_root=tmp_path,
                epoch_id="e0",
            )
        )
