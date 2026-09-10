"""Scheduling omissions remain attempts and cannot become measurement evidence."""

from __future__ import annotations

import asyncio
import json
import random
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

import zicato.tournament.worker_execution as _tournament_worker_execution
from tests._contract_pins import deterministic_weights
from tests._runtime_builders import make_generation, runtime_config
from zicato.core import BoardEntry
from zicato.core.measurement import MeasurementDraw, MeasurementPurpose
from zicato.core.runtime import RoundTokenLedger
from zicato.core.workspace import run_id_for_unit
from zicato.runtime.lock import acquire_workspace_lock
from zicato.selection.evidence_gate import _count_pair_duels
from zicato.selection.standings_ext import audit_duels, audit_matrix
from zicato.selection.strategy import MatchupResult
from zicato.telemetry.reducer import read_loss_profile, write_loss_profile
from zicato.testing.fixtures import make_loss_profile
from zicato.tournament import scheduling
from zicato.tournament.gate import evaluate_gate, holdout_confirms
from zicato.tournament.scoring import aggregate_generation_score
from zicato.tournament.unit_cache import _resolve_cached_unit, _unit_loss_path


@pytest.mark.asyncio
@pytest.mark.parametrize("replicates", [1, 2])
@pytest.mark.parametrize("entry_ids", [("first",), ("first", "remaining"), ("remaining", "first")])
@pytest.mark.parametrize("improvement", [0.0, 1.0])
async def test_budget_skip_retries_in_fresh_round_and_coalesces_requests(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replicates: int,
    entry_ids: tuple[str, ...],
    improvement: float,
) -> None:
    workspace = tmp_path / "workspace"
    parent = make_generation(workspace, "parent")
    child = make_generation(workspace, "child")
    board = [
        BoardEntry(id=name, kind="single_turn", input=name, wall_clock_budget_seconds=60)
        for name in entry_ids
    ]
    entries = len(board)
    config = replace(runtime_config(workspace), parallelism=1)
    calls: list[tuple[str, str, int]] = []

    async def measured(**kwargs: Any) -> Any:
        generation, entry = kwargs["generation"], kwargs["entry"]
        replicate = MeasurementDraw.from_context(entry.context)
        calls.append((generation.id, entry.id, replicate))
        await asyncio.sleep(0)
        baseline = 2.0 + random.Random(f"42:{entry.id}:{replicate}").uniform(-0.1, 0.1)
        return make_loss_profile(
            run_id=run_id_for_unit(
                generation.id,
                entry.id,
                replicate,
                base_seed=kwargs["config"].seed,
                epoch_id=generation.epoch_id,
            ),
            generation_id=generation.id,
            entry_id=entry.id,
            epoch_id="e0",
            drift_loss=baseline if generation.id == "parent" else baseline - improvement,
            pass_fail=True,
            tokens_spent=100,
        )

    monkeypatch.setattr(_tournament_worker_execution, "_run_single", measured)

    with acquire_workspace_lock(workspace, "test") as writer:

        async def matchup(runtime: Any, identity: str) -> Any:
            return await scheduling._run_replicated(
                writer=writer,
                adapter=object(),
                left_gen=parent,
                right_gen=child,
                board=board,
                weights=deterministic_weights(),
                config=runtime,
                workspace_root=workspace,
                epoch_id="e0",
                replicates=replicates,
                match_id=identity,
                fast=True,
            )

        first = await matchup(
            replace(config, max_tokens_per_round=100, token_ledger=RoundTokenLedger(100)),
            "budget-limited-round",
        )
        assert len(calls) == 2
        second, concurrent = await asyncio.gather(
            matchup(config, "fresh-round"), matchup(config, "concurrent-matchup")
        )
        assert len(calls) == 2 * entries * replicates
        assert len(set(calls)) == len(calls)
        assert second[:2] == concurrent[:2]
        for generation in (parent, child):
            for entry in board:
                for replicate in range(replicates):
                    slot = _unit_loss_path(
                        workspace,
                        "e0",
                        generation.id,
                        entry.id,
                        MeasurementDraw(MeasurementPurpose.TOURNAMENT, replicate),
                        base_seed=config.seed,
                    )
                    attempts = list(slot.parent.glob(f"{slot.stem}.a*.json"))
                    expected_skip = replicate > 0 or entry.id != board[0].id
                    assert len(attempts) == int(expected_skip)
                    if expected_skip:
                        skipped = json.loads(attempts[0].read_text())
                        assert skipped["execution_started"] is False
                        assert skipped["not_completed_reason"] == "scheduling_budget_exhausted"
                        assert skipped["match_id"] == "budget-limited-round"
                        assert skipped["tokens_spent"] == 0
                    assert read_loss_profile(slot).execution_started is True
            assert first[3][generation.id].fresh == 1

        weights = deterministic_weights()
        complete_verdict = "promoted" if improvement else "rejected"
        first_verdict = "deferred" if entries * replicates > 1 else complete_verdict
        for losses, expected in ((first, first_verdict), (second, complete_verdict)):
            parent_agg = aggregate_generation_score(list(losses[0].values()), weights)
            child_agg = aggregate_generation_score(list(losses[1].values()), weights)
            verdict = evaluate_gate(
                parent_agg,
                child_agg,
                weights,
            )
            assert verdict.decision == expected
            audit = [MatchupResult("draw", "parent", "child", parent_agg, child_agg, verdict)]
            if expected == "deferred":
                assert "incomplete execution" in verdict.reason
                assert audit_duels(audit) == []
                assert not audit_matrix(audit).ids
                assert _count_pair_duels(audit, "parent", "child") == 0
            elif improvement:
                assert audit_duels(audit) == [("child", "parent")]
                assert _count_pair_duels(audit, "parent", "child") == 1
            else:
                assert audit_duels(audit) == []
                assert _count_pair_duels(audit, "parent", "child") == 0


@pytest.mark.parametrize(
    "evidence",
    ["missing", "runtime", "started_at", "ended_at", "tokens_spent", "explicit", "unstarted"],
)
def test_historical_zero_runtime_budget_records_require_execution_evidence(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, evidence: str
) -> None:
    profile = make_loss_profile(
        abort_cause="budget_exhausted",
        measurement=MeasurementDraw(MeasurementPurpose.TOURNAMENT, 0),
        wall_clock_budget_exceeded=True,
        runtime_ms=0,
        epoch_id="e0",
        entry_id="entry",
    )
    if evidence == "runtime":
        profile = replace(profile, runtime_ms=1)
    elif evidence == "started_at":
        profile = replace(profile, started_at="2026-09-05T00:00:00Z")
    path = _unit_loss_path(
        tmp_path, "e0", "v0", "entry", MeasurementDraw(MeasurementPurpose.TOURNAMENT, 0)
    )
    write_loss_profile(profile, path)
    if evidence in {"explicit", "unstarted", "ended_at", "tokens_spent"}:
        payload = json.loads(path.read_text())
        if evidence in {"explicit", "unstarted"}:
            payload["execution_started"] = evidence == "explicit"
        elif evidence == "ended_at":
            payload["ended_at"] = "2026-09-05T00:00:00Z"
        else:
            payload["tokens_spent"] = 1
        path.write_text(json.dumps(payload))
    original = path.read_bytes()
    cached = _resolve_cached_unit(
        workspace_root=tmp_path,
        epoch_id="e0",
        generation_id="v0",
        entry_id="entry",
        measurement=MeasurementDraw(MeasurementPurpose.TOURNAMENT, 0),
    )
    from zicato.query.paths import WorkspacePaths
    from zicato.query.replicate_scores import (
        cell_replicate_draws_indexed,
        measurement_band_draws_indexed,
    )
    from zicato.tournament.unit_cache import own_code_board_draws
    from zicato.workspace.layout import WorkspaceLayout
    from zicato.workspace.reads import read_loss

    eligible = evidence not in {"missing", "unstarted"}
    assert (cached is not None) == eligible
    paths = WorkspacePaths(tmp_path)
    assert (
        read_loss(WorkspaceLayout.from_root(tmp_path), "e0", "v0", "entry") is not None
    ) == eligible
    assert bool(own_code_board_draws(path.parent.parent)) == eligible
    assert bool(cell_replicate_draws_indexed(paths, "e0", "v0", "entry")) == eligible
    bands = measurement_band_draws_indexed(paths, "e0", "v0", "entry")
    assert [band.key for _, band, _ in bands] == ([] if eligible else ["ambiguous"])
    assert path.read_bytes() == original
    if evidence == "missing":
        assert "ambiguous historical budget record" in caplog.text


def test_incomplete_confirmation_cannot_promote(tmp_path: Path) -> None:
    generation = make_generation(tmp_path)
    entry = BoardEntry(id="holdout", kind="single_turn", input="x", wall_clock_budget_seconds=1)
    skipped, _ = scheduling._skip_unit_side(
        generation=generation,
        entry=entry,
        weights=deterministic_weights(),
        match_id="confirmation",
        workspace_root=tmp_path,
        epoch_id="e0",
        measurement=MeasurementDraw(MeasurementPurpose.TOURNAMENT, 0),
        side_force_fresh=False,
        provenance=None,
    )
    complete = make_loss_profile(entry_id="holdout", drift_loss=0.0, pass_fail=True)
    weights = deterministic_weights()
    holdout = aggregate_generation_score([skipped], weights)
    assert holdout["entry_count"] == 0
    assert holdout["incomplete_entries"] == ["holdout"]
    verdict = evaluate_gate(
        {"scalar": 2.0},
        {"scalar": 0.0},
        weights,
        holdout_parent_agg=aggregate_generation_score([complete], weights),
        holdout_child_agg=holdout,
    )
    assert verdict.decision == "deferred"
    assert "incomplete execution" in verdict.reason
    assert "incomplete execution" in holdout_confirms(
        aggregate_generation_score([complete], weights), holdout, weights
    )
