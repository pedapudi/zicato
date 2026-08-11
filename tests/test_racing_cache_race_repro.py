"""Regression coverage for Racing's shared-champion cache single-flight."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

import zicato.tournament.runner as runner_mod
from zicato.core import BoardEntry, Generation, LossProfile, RuntimeConfig, ScoringWeights
from zicato.testing.fixtures import make_loss_profile
from zicato.tournament.runner import _run_unit_cache_first


def _generation(tmp_path: Path, generation_id: str) -> Generation:
    return Generation(
        id=generation_id,
        epoch_id="e0",
        parent_id=None,
        snapshot_root=tmp_path / generation_id,
        created_at="2024-01-01T00:00:00Z",
    )


def _entry() -> BoardEntry:
    return BoardEntry(
        id="entry_a",
        kind="single_turn",
        wall_clock_budget_seconds=60,
        input="hello",
    )


def _config(tmp_path: Path) -> RuntimeConfig:
    async def harness_call(system: str, user: str, model: str) -> str:
        return ""

    async def auxiliary_call(system: str, user: str, model: str) -> str:
        return ""

    return RuntimeConfig(
        instance_id="test",
        workspace_root=tmp_path,
        harness_call_llm=harness_call,
        auxiliary_call_llm=auxiliary_call,
    )


def test_concurrent_racing_matchups_run_a_cold_champion_unit_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """T1: one cold champion cache key starts one worker across a Racing rung."""
    workspace = tmp_path / ".zicato"
    workspace.mkdir()
    champion = _generation(tmp_path, "v0")
    entry = _entry()
    started_matchups: list[str] = []

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
        del adapter, weights, config, workspace_root, side
        started_matchups.append(match_id)
        # Let the second matchup reach the cache while this leader is running.
        await asyncio.sleep(0)
        return make_loss_profile(
            run_id=f"{generation.id}--{entry.id}",
            generation_id=generation.id,
            entry_id=entry.id,
            epoch_id=epoch_id,
            drift_loss=1.0,
            pass_fail=True,
        )

    monkeypatch.setattr(runner_mod, "_run_single", fake_run_single)

    async def run_rung() -> None:
        await asyncio.gather(
            _run_unit_cache_first(
                adapter=object(),
                generation=champion,
                entry=entry,
                weights=ScoringWeights(),
                config=_config(workspace),
                workspace_root=workspace,
                epoch_id="e0",
                side="parent",
                match_id="rung0_m0",
            ),
            _run_unit_cache_first(
                adapter=object(),
                generation=champion,
                entry=entry,
                weights=ScoringWeights(),
                config=_config(workspace),
                workspace_root=workspace,
                epoch_id="e0",
                side="parent",
                match_id="rung0_m1",
            ),
        )

    asyncio.run(run_rung())

    assert started_matchups == ["rung0_m0"]


def test_force_fresh_units_are_not_coalesced(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``--mode full`` keeps its explicit re-sampling semantics."""
    workspace = tmp_path / ".zicato"
    workspace.mkdir()
    champion = _generation(tmp_path, "v0")
    entry = _entry()
    starts = 0

    async def fake_run_single(**kwargs: Any) -> LossProfile:
        nonlocal starts
        starts += 1
        await asyncio.sleep(0)
        generation = kwargs["generation"]
        board_entry = kwargs["entry"]
        return make_loss_profile(
            run_id=f"{generation.id}--{board_entry.id}",
            generation_id=generation.id,
            entry_id=board_entry.id,
            epoch_id=kwargs["epoch_id"],
        )

    monkeypatch.setattr(runner_mod, "_run_single", fake_run_single)

    async def run_full() -> None:
        await asyncio.gather(
            _run_unit_cache_first(
                adapter=object(),
                generation=champion,
                entry=entry,
                weights=ScoringWeights(),
                config=_config(workspace),
                workspace_root=workspace,
                epoch_id="e0",
                side="parent",
                force_fresh=True,
            ),
            _run_unit_cache_first(
                adapter=object(),
                generation=champion,
                entry=entry,
                weights=ScoringWeights(),
                config=_config(workspace),
                workspace_root=workspace,
                epoch_id="e0",
                side="parent",
                force_fresh=True,
            ),
        )

    asyncio.run(run_full())

    assert starts == 2
