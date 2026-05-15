"""Tests for ``zicato.tournament.runner``.

The runner integrates several layers; tests here stub:

* The harness adapter — returns a session that records which entries
  it was asked to run against which sink paths but does no real work.
* The lazily-imported ``zicato.telemetry.sink`` / ``.reducer`` modules
  — built as ad-hoc ``types.ModuleType`` instances and inserted into
  ``sys.modules`` for the duration of one test. The sink stub returns
  a deterministic per-run path; the reducer stub returns canned
  :class:`LossProfile` instances keyed by ``(generation_id, entry_id)``.

The two-callable invariant on :class:`RuntimeConfig` is honored by
constructing two distinct lambda objects (identity-unequal) for
``harness_call_llm`` and ``auxiliary_call_llm``.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import sys
import types
from pathlib import Path
from typing import Any

import pytest

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


class _StubSession:
    """Records every (entry_id, sink_path) the runner hands it."""

    def __init__(self, log: list[tuple[str, str, Path]], generation_id: str) -> None:
        self._log = log
        self._generation_id = generation_id

    async def run(self, entry: BoardEntry, sink_path: Path) -> None:
        self._log.append((self._generation_id, entry.id, sink_path))


class _StubAdapter:
    """Returns a fresh stub session per ``load`` call."""

    def __init__(self, log: list[tuple[str, str, Path]]) -> None:
        self._log = log
        # Map snapshot_root → generation_id for sessions we hand out.
        self._snapshot_to_gen: dict[Path, str] = {}

    def register_generation(self, generation: Generation) -> None:
        self._snapshot_to_gen[generation.snapshot_root] = generation.id

    def load(self, snapshot_root: Path) -> _StubSession:
        generation_id = self._snapshot_to_gen[snapshot_root]
        return _StubSession(self._log, generation_id)


def _install_telemetry_stubs(
    monkeypatch: pytest.MonkeyPatch,
    *,
    canned: dict[tuple[str, str], LossProfile],
) -> list[Path]:
    """Install fake ``zicato.telemetry.sink`` / ``.reducer`` modules.

    Returns the list the sink stub appends each path it was asked for —
    tests use it to verify the runner iterates the board in order.
    """
    requested_paths: list[Path] = []

    sink_mod = types.ModuleType("zicato.telemetry.sink")

    def make_run_sink_path(
        *,
        workspace_root: Path,
        epoch_id: str,
        generation_id: str,
        entry_id: str,
    ) -> Path:
        path = (
            workspace_root
            / "epochs"
            / epoch_id
            / "generations"
            / generation_id
            / "runs"
            / entry_id
            / "events.jsonl"
        )
        requested_paths.append(path)
        return path

    sink_mod.make_run_sink_path = make_run_sink_path  # type: ignore[attr-defined]

    reducer_mod = types.ModuleType("zicato.telemetry.reducer")

    def reduce_loss(
        *,
        events_jsonl_path: Path,
        entry_id: str,
        generation_id: str,
        epoch_id: str,
        weights: ScoringWeights,
    ) -> LossProfile:
        del events_jsonl_path, epoch_id, weights  # unused in stub
        return canned[(generation_id, entry_id)]

    reducer_mod.reduce_loss = reduce_loss  # type: ignore[attr-defined]

    telemetry_pkg = types.ModuleType("zicato.telemetry")
    telemetry_pkg.sink = sink_mod  # type: ignore[attr-defined]
    telemetry_pkg.reducer = reducer_mod  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "zicato.telemetry", telemetry_pkg)
    monkeypatch.setitem(sys.modules, "zicato.telemetry.sink", sink_mod)
    monkeypatch.setitem(sys.modules, "zicato.telemetry.reducer", reducer_mod)

    return requested_paths


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


# ---------------------------------------------------------------------------
# run_tournament — full mode
# ---------------------------------------------------------------------------


def test_run_tournament_iterates_board_for_both_generations(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Full mode runs every entry under both generations and aggregates correctly."""
    parent_gen = Generation(
        id="v0",
        epoch_id="e0",
        parent_id=None,
        snapshot_root=tmp_path / "snap_v0",
        created_at="2024-01-01T00:00:00Z",
    )
    child_gen = Generation(
        id="v1",
        epoch_id="e0",
        parent_id="v0",
        snapshot_root=tmp_path / "snap_v1",
        created_at="2024-01-02T00:00:00Z",
    )

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

    requested_paths = _install_telemetry_stubs(monkeypatch, canned=canned)

    log: list[tuple[str, str, Path]] = []
    adapter = _StubAdapter(log)
    adapter.register_generation(parent_gen)
    adapter.register_generation(child_gen)

    board = _make_board()
    weights = ScoringWeights(promote_margin=0.01)
    config = _make_runtime_config(tmp_path)

    result = asyncio.run(
        run_tournament(
            adapter=adapter,
            parent_gen=parent_gen,
            child_gen=child_gen,
            board=board,
            weights=weights,
            config=config,
            workspace_root=tmp_path,
            epoch_id="e0",
        )
    )

    # The adapter saw all 4 (generation, entry) combinations, parent first.
    assert [(gen, entry_id) for gen, entry_id, _ in log] == [
        ("v0", "entry_a"),
        ("v0", "entry_b"),
        ("v1", "entry_a"),
        ("v1", "entry_b"),
    ]
    # Sink paths were requested for the same 4 combinations.
    assert len(requested_paths) == 4

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
    parent_gen = Generation(
        id="v0",
        epoch_id="e0",
        parent_id=None,
        snapshot_root=tmp_path / "snap_v0",
        created_at="2024-01-01T00:00:00Z",
    )
    child_gen = Generation(
        id="v1",
        epoch_id="e0",
        parent_id="v0",
        snapshot_root=tmp_path / "snap_v1",
        created_at="2024-01-02T00:00:00Z",
    )

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

    _install_telemetry_stubs(monkeypatch, canned=canned)

    log: list[tuple[str, str, Path]] = []
    adapter = _StubAdapter(log)
    adapter.register_generation(parent_gen)
    adapter.register_generation(child_gen)

    config = _make_runtime_config(tmp_path)
    result = asyncio.run(
        run_tournament(
            adapter=adapter,
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
    parent_gen = Generation(
        id="v0",
        epoch_id="e0",
        parent_id=None,
        snapshot_root=tmp_path / "snap_v0",
        created_at="2024-01-01T00:00:00Z",
    )
    child_gen = Generation(
        id="v1",
        epoch_id="e0",
        parent_id="v0",
        snapshot_root=tmp_path / "snap_v1",
        created_at="2024-01-02T00:00:00Z",
    )

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
                adapter=_StubAdapter([]),
                parent_gen=parent_gen,
                child_gen=child_gen,
                board=_make_board(),
                weights=ScoringWeights(),
                config=config,
                workspace_root=tmp_path,
                epoch_id="e0",
            )
        )


# ---------------------------------------------------------------------------
# run_fast_mode
# ---------------------------------------------------------------------------


def test_run_fast_mode_runs_only_child(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Fast mode runs only the child generation and compares to historical agg."""
    child_gen = Generation(
        id="v1",
        epoch_id="e0",
        parent_id="v0",
        snapshot_root=tmp_path / "snap_v1",
        created_at="2024-01-02T00:00:00Z",
    )

    canned = {
        ("v1", "entry_a"): _loss(
            generation_id="v1", entry_id="entry_a", drift_loss=0.5, pass_fail=True
        ),
        ("v1", "entry_b"): _loss(
            generation_id="v1", entry_id="entry_b", drift_loss=0.5, pass_fail=True
        ),
    }

    _install_telemetry_stubs(monkeypatch, canned=canned)

    log: list[tuple[str, str, Path]] = []
    adapter = _StubAdapter(log)
    adapter.register_generation(child_gen)

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
            adapter=adapter,
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
    assert {(gen, entry_id) for gen, entry_id, _ in log} == {
        ("v1", "entry_a"),
        ("v1", "entry_b"),
    }

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
    parent_gen = Generation(
        id="v0",
        epoch_id="e0",
        parent_id=None,
        snapshot_root=tmp_path / "snap_v0",
        created_at="2024-01-01T00:00:00Z",
    )
    child_gen = Generation(
        id="v1",
        epoch_id="e0",
        parent_id="v0",
        snapshot_root=tmp_path / "snap_v1",
        created_at="2024-01-02T00:00:00Z",
    )
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
    _install_telemetry_stubs(monkeypatch, canned=canned)

    adapter = _StubAdapter([])
    adapter.register_generation(parent_gen)
    adapter.register_generation(child_gen)
    config = _make_runtime_config(tmp_path)

    result = asyncio.run(
        run_tournament(
            adapter=adapter,
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
