"""Tests for ``zicato.tournament.runner`` generation-level orchestration.

Since the L3 subprocess-isolation refactor, the per-entry run mechanism
(:func:`zicato.tournament.runner._run_single`) spawns a worker
subprocess. The end-to-end subprocess behaviour — args-file shape,
result-file shape, parent-side budget escalation, supervisor-kill
handling — is covered by :mod:`tests.test_subprocess_workers`.

These tests focus on what the runner module still owns *in-process*:
``run_tournament`` / ``run_fast_mode`` iterating the board across
generations, aggregating per-entry :class:`LossProfile` instances, and
running the gate. They stub :func:`_run_single` with a canned-loss
lookup so the orchestration logic is exercised without spawning real
subprocesses.

The two-callable invariant on :class:`RuntimeConfig` is honored by
constructing two distinct callables (identity-unequal) for
``harness_call_llm`` and ``auxiliary_call_llm``.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
from pathlib import Path
from typing import Any

import pytest

import zicato.tournament.runner as runner_mod
from zicato.core import (
    BoardEntry,
    DriftCount,
    ExpectationResult,
    Generation,
    LossProfile,
    RuntimeConfig,
    ScoringWeights,
)
from zicato.tournament.runner import (
    TournamentResult,
    run_fast_mode,
    run_tournament,
)

# ---------------------------------------------------------------------------
# Fixtures and stubs
# ---------------------------------------------------------------------------


def _loss(
    *,
    generation_id: str,
    entry_id: str,
    drift_loss: float,
    pass_fail: bool | None,
) -> LossProfile:
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


def _stub_run_single(
    monkeypatch: pytest.MonkeyPatch,
    *,
    canned: dict[tuple[str, str], LossProfile],
) -> list[tuple[str, str]]:
    """Replace ``_run_single`` with a canned-loss lookup.

    Returns the call log — a list of ``(generation_id, entry_id)`` tuples
    in the order ``_run_single`` was invoked, so tests can assert the
    runner iterates the board in order, parent generation first.
    """
    call_log: list[tuple[str, str]] = []

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
    ) -> LossProfile:
        del adapter, weights, config, workspace_root, epoch_id, side
        call_log.append((generation.id, entry.id))
        return canned[(generation.id, entry.id)]

    monkeypatch.setattr(runner_mod, "_run_single", fake_run_single)
    return call_log


def _make_board() -> list[BoardEntry]:
    return [
        BoardEntry(
            id="entry_a",
            kind="single_turn",
            wall_clock_budget_seconds=60,
            input="hello",
        ),
        BoardEntry(
            id="entry_b",
            kind="single_turn",
            wall_clock_budget_seconds=60,
            input="world",
        ),
    ]


def _make_runtime_config(tmp_path: Path) -> RuntimeConfig:
    # Two distinct callables (identity-unequal) — the runner re-checks
    # the two-callable invariant as defense in depth.
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


def _make_generation(tmp_path: Path, gen_id: str, parent: str | None) -> Generation:
    return Generation(
        id=gen_id,
        epoch_id="e0",
        parent_id=parent,
        snapshot_root=tmp_path / f"snap_{gen_id}",
        created_at="2024-01-01T00:00:00Z",
    )


# ---------------------------------------------------------------------------
# run_tournament — full mode
# ---------------------------------------------------------------------------


def test_run_tournament_iterates_board_for_both_generations(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Full mode runs every entry under both generations and aggregates correctly."""
    parent_gen = _make_generation(tmp_path, "v0", None)
    child_gen = _make_generation(tmp_path, "v1", "v0")

    canned = {
        ("v0", "entry_a"): _loss(
            generation_id="v0", entry_id="entry_a", drift_loss=2.0, pass_fail=True
        ),
        ("v0", "entry_b"): _loss(
            generation_id="v0", entry_id="entry_b", drift_loss=2.0, pass_fail=False
        ),
        ("v1", "entry_a"): _loss(
            generation_id="v1", entry_id="entry_a", drift_loss=1.0, pass_fail=True
        ),
        ("v1", "entry_b"): _loss(
            generation_id="v1", entry_id="entry_b", drift_loss=1.0, pass_fail=True
        ),
    }
    call_log = _stub_run_single(monkeypatch, canned=canned)

    board = _make_board()
    weights = ScoringWeights(promote_margin=0.01)
    config = _make_runtime_config(tmp_path)

    result = asyncio.run(
        run_tournament(
            adapter=object(),
            parent_gen=parent_gen,
            child_gen=child_gen,
            board=board,
            weights=weights,
            config=config,
            workspace_root=tmp_path,
            epoch_id="e0",
        )
    )

    # The runner drove all 4 (generation, entry) combinations, parent first.
    assert call_log == [
        ("v0", "entry_a"),
        ("v0", "entry_b"),
        ("v1", "entry_a"),
        ("v1", "entry_b"),
    ]

    # Result wires generation ids through.
    assert isinstance(result, TournamentResult)
    assert result.parent_generation_id == "v0"
    assert result.child_generation_id == "v1"

    # Per-entry losses contains tuples (parent_loss, child_loss).
    assert set(result.per_entry_losses) == {"entry_a", "entry_b"}
    parent_a, child_a = result.per_entry_losses["entry_a"]
    assert parent_a.drift_loss == 2.0
    assert child_a.drift_loss == 1.0

    # Aggregate dicts carry expected fields.
    assert result.parent_agg["drift_loss_mean"] == 2.0
    assert result.child_agg["drift_loss_mean"] == 1.0
    assert result.parent_agg["pass_rate"] == 0.5
    assert result.child_agg["pass_rate"] == 1.0

    # Gate: child improves both drift and pass-rate → promoted.
    assert result.outcome.decision == "promoted"
    assert result.outcome.delta_scalar < 0
    assert result.outcome.delta_pass_rate == 0.5


def test_run_tournament_rejects_when_child_regresses_pass_rate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A child that improves drift but regresses pass-rate must be rejected."""
    parent_gen = _make_generation(tmp_path, "v0", None)
    child_gen = _make_generation(tmp_path, "v1", "v0")

    canned = {
        ("v0", "entry_a"): _loss(
            generation_id="v0", entry_id="entry_a", drift_loss=5.0, pass_fail=True
        ),
        ("v0", "entry_b"): _loss(
            generation_id="v0", entry_id="entry_b", drift_loss=5.0, pass_fail=True
        ),
        ("v1", "entry_a"): _loss(
            generation_id="v1", entry_id="entry_a", drift_loss=0.0, pass_fail=True
        ),
        ("v1", "entry_b"): _loss(
            generation_id="v1", entry_id="entry_b", drift_loss=0.0, pass_fail=False
        ),
    }
    _stub_run_single(monkeypatch, canned=canned)

    config = _make_runtime_config(tmp_path)
    result = asyncio.run(
        run_tournament(
            adapter=object(),
            parent_gen=parent_gen,
            child_gen=child_gen,
            board=_make_board(),
            weights=ScoringWeights(),
            config=config,
            workspace_root=tmp_path,
            epoch_id="e0",
        )
    )

    assert result.outcome.decision == "rejected"
    assert "pass-rate regression on entries" in result.outcome.reason
    assert "entry_b" in result.outcome.reason


def test_run_tournament_rejects_two_callable_invariant(tmp_path: Path) -> None:
    """The runner re-checks that harness/auxiliary callables differ."""
    parent_gen = _make_generation(tmp_path, "v0", None)
    child_gen = _make_generation(tmp_path, "v1", "v0")

    async def shared(system: str, user: str, model: str) -> str:
        return ""

    config = RuntimeConfig(
        instance_id="test",
        workspace_root=tmp_path,
        harness_call_llm=shared,
        auxiliary_call_llm=shared,
    )

    with pytest.raises(RuntimeError):
        asyncio.run(
            run_tournament(
                adapter=object(),
                parent_gen=parent_gen,
                child_gen=child_gen,
                board=_make_board(),
                weights=ScoringWeights(),
                config=config,
                workspace_root=tmp_path,
                epoch_id="e0",
            )
        )


def test_run_tournament_stamps_each_entry_on_the_correct_side(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An 8-row tournament ends with exactly 4 parent + 4 child rows, each
    transitioned on its OWN side.

    The runner threads ``side`` ("parent" / "child") into ``_run_single``
    explicitly per generation. This stub mirrors what the real
    ``_run_single`` does to the live ``ActiveTournament`` grid — a
    ``running`` then ``completed`` transition keyed on
    ``(entry_id, side)`` — so the test proves the runner passes the
    correct side and the per-side rows never collide (the 6/2
    mislabeling bug).
    """
    from zicato.runtime.state import read_active_tournament, update_tournament_entry

    parent_gen = _make_generation(tmp_path, "v0", None)
    child_gen = _make_generation(tmp_path, "v1", "v0")

    # 4-entry board -> 8 tournament rows (4 parent + 4 child).
    board = [
        BoardEntry(
            id=f"entry_{n}",
            kind="single_turn",
            wall_clock_budget_seconds=60,
            input="x",
        )
        for n in range(4)
    ]
    canned = {
        (gen, e.id): _loss(generation_id=gen, entry_id=e.id, drift_loss=1.0, pass_fail=True)
        for gen in ("v0", "v1")
        for e in board
    }

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
    ) -> LossProfile:
        del adapter, weights, config, epoch_id
        # Exactly the transitions the real _run_single performs, keyed on
        # (entry_id, side).
        update_tournament_entry(workspace_root, entry.id, side, status="running")
        update_tournament_entry(workspace_root, entry.id, side, status="completed")
        return canned[(generation.id, entry.id)]

    monkeypatch.setattr(runner_mod, "_run_single", fake_run_single)

    # Inspect the ActiveTournament grid right before run_tournament clears
    # it: monkeypatch clear_active_tournament to snapshot the final state.
    captured: dict[str, Any] = {}
    real_clear = runner_mod._runtime_state()[0].clear_active_tournament

    def capturing_clear(workspace_root: Path) -> None:
        snap = read_active_tournament(workspace_root)
        if snap is not None:
            captured["tournament"] = snap
        real_clear(workspace_root)

    import zicato.runtime.state as _state_mod

    monkeypatch.setattr(_state_mod, "clear_active_tournament", capturing_clear)

    config = _make_runtime_config(tmp_path)
    asyncio.run(
        run_tournament(
            adapter=object(),
            parent_gen=parent_gen,
            child_gen=child_gen,
            board=board,
            weights=ScoringWeights(),
            config=config,
            workspace_root=tmp_path,
            epoch_id="e0",
        )
    )

    tournament = captured["tournament"]
    parents = [e for e in tournament.entries if e.side == "parent"]
    children = [e for e in tournament.entries if e.side == "child"]
    # Exactly 4 parent + 4 child — NOT 6/2.
    assert len(parents) == 4
    assert len(children) == 4
    # Every row reached "completed", and crucially each side carries its
    # OWN four entries (no parent-side update bled onto a child row).
    assert all(e.status == "completed" for e in tournament.entries)
    assert {e.entry_id for e in parents} == {e.id for e in board}
    assert {e.entry_id for e in children} == {e.id for e in board}


# ---------------------------------------------------------------------------
# run_fast_mode
# ---------------------------------------------------------------------------


def test_run_fast_mode_runs_only_child(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Fast mode runs only the child generation and compares to historical agg."""
    child_gen = _make_generation(tmp_path, "v1", "v0")

    canned = {
        ("v1", "entry_a"): _loss(
            generation_id="v1", entry_id="entry_a", drift_loss=0.5, pass_fail=True
        ),
        ("v1", "entry_b"): _loss(
            generation_id="v1", entry_id="entry_b", drift_loss=0.5, pass_fail=True
        ),
    }
    call_log = _stub_run_single(monkeypatch, canned=canned)

    # Historical aggregate the operator saved off some time ago for v0.
    parent_historical = {
        "drift_loss_mean": 2.0,
        "pass_rate": 1.0,
        "expectation_count": 2,
        "entry_count": 2,
        "scalar": 2.0,
        "per_entry": {
            "entry_a": {"drift_loss": 2.0, "pass_fail": True},
            "entry_b": {"drift_loss": 2.0, "pass_fail": True},
        },
        "generation_id": "v0",
    }

    config = _make_runtime_config(tmp_path)
    result = asyncio.run(
        run_fast_mode(
            adapter=object(),
            child_gen=child_gen,
            board=_make_board(),
            weights=ScoringWeights(promote_margin=0.01),
            config=config,
            workspace_root=tmp_path,
            epoch_id="e0",
            parent_historical_agg=parent_historical,
        )
    )

    # Only the child was run.
    assert set(call_log) == {("v1", "entry_a"), ("v1", "entry_b")}

    # Parent agg passes through unchanged; child agg fresh from this run.
    assert result.parent_agg is parent_historical
    assert result.child_agg["scalar"] == 0.5
    assert result.parent_generation_id == "v0"
    assert result.child_generation_id == "v1"
    # Per-entry tuples are not produced in fast mode.
    assert result.per_entry_losses == {}
    # Big win for the child here.
    assert result.outcome.decision == "promoted"


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


def test_tournament_result_is_json_serializable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``dataclasses.asdict`` + ``json.dumps(default=str)`` round-trips."""
    parent_gen = _make_generation(tmp_path, "v0", None)
    child_gen = _make_generation(tmp_path, "v1", "v0")
    canned = {
        ("v0", "entry_a"): _loss(
            generation_id="v0", entry_id="entry_a", drift_loss=1.0, pass_fail=True
        ),
        ("v0", "entry_b"): _loss(
            generation_id="v0", entry_id="entry_b", drift_loss=1.0, pass_fail=True
        ),
        ("v1", "entry_a"): _loss(
            generation_id="v1", entry_id="entry_a", drift_loss=0.5, pass_fail=True
        ),
        ("v1", "entry_b"): _loss(
            generation_id="v1", entry_id="entry_b", drift_loss=0.5, pass_fail=True
        ),
    }
    _stub_run_single(monkeypatch, canned=canned)
    config = _make_runtime_config(tmp_path)

    result = asyncio.run(
        run_tournament(
            adapter=object(),
            parent_gen=parent_gen,
            child_gen=child_gen,
            board=_make_board(),
            weights=ScoringWeights(),
            config=config,
            workspace_root=tmp_path,
            epoch_id="e0",
        )
    )

    payload = dataclasses.asdict(result)
    encoded = json.dumps(payload, default=str)
    decoded: dict[str, Any] = json.loads(encoded)

    assert decoded["parent_generation_id"] == "v0"
    assert decoded["child_generation_id"] == "v1"
    assert decoded["outcome"]["decision"] == "promoted"
    # Per-entry losses round-tripped through asdict — paths string-ified.
    assert "entry_a" in decoded["per_entry_losses"]
