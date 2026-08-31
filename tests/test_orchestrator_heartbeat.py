"""Tests for the heartbeat + workspace-lock lifecycle in evolve_n_rounds."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from tests._orchestrator_harness import (
    _harness_call_llm,
    _make_aux_responder,
    _valid_proposer_response,
)
from zicato.core.types import (
    BoardEntry,
    DriftCount,
    ExpectationResult,
    LossProfile,
    RunResult,
    ScoringWeights,
)
from zicato.epoch.lifecycle import new_epoch
from zicato.runtime.lock import WorkspaceLockHeld, acquire_workspace_lock
from zicato.runtime.paths import heartbeat_path, lock_path
from zicato.runtime.state import read_heartbeat


def _bootstrap_workspace(tmp_path: Path) -> tuple[Path, str]:
    workspace = tmp_path / ".zicato"
    workspace.mkdir()
    (workspace / "config.json").write_text(
        json.dumps(
            {
                "instance_id": "test",
                "created_at": "2026-05-14T00:00:00Z",
                # Hand-built directory-backend snapshot layout below; pin the
                # directory backend so the git default does not look for git
                # tags this fixture never writes.
                "generation_source_backend": "directory",
                "adapter": {"kind": "stub"},
            }
        )
    )

    board_src = tmp_path / "board.jsonl"
    board_src.write_text(
        json.dumps(
            {
                "id": "entry_a",
                "kind": "single_turn",
                "wall_clock_budget_seconds": 60,
                "input": "hello",
            }
        )
        + "\n"
    )
    brief_src = tmp_path / "brief.md"
    brief_src.write_text("# Proposer brief\n- Be careful.\n")

    cfg = new_epoch(
        workspace,
        name="alpha",
        board_source=board_src,
        brief_source=brief_src,
        weights=ScoringWeights(promote_margin=0.01),
        auto_close_previous=False,
    )

    v0_dir = workspace / "epochs" / cfg.id / "generations" / "v0"
    snap = v0_dir / "snapshot"
    snap.mkdir(parents=True)
    (snap / "agent.py").write_text(
        '"""Stub harness source."""\n\n# zicato:mutable id="greeting"\nGREETING = "hello"\n'
    )
    return workspace, cfg.id


def _install_stub_adapter_factory(monkeypatch: pytest.MonkeyPatch) -> None:
    class _StubSession:
        async def run(self, entry: BoardEntry, sinks: list[Any], config: Any) -> RunResult:
            del sinks, config
            return RunResult(
                run_id=f"r-{entry.id}",
                entry_id=entry.id,
                final_output="hello world",
                transcript=("hello world",),
                runtime_ms=100,
            )

    class _StubAdapter:
        name = "stub"

        def load(self, snapshot_root: Path) -> _StubSession:
            del snapshot_root
            return _StubSession()

        def mutation_points(self, source_roots: list[Path] | None = None) -> list[Any]:
            del source_roots
            return []

    fake_factory = types.ModuleType("zicato.adapter_factory")
    fake_factory.make_adapter_from_config = lambda cfg: _StubAdapter()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "zicato.adapter_factory", fake_factory)
    import zicato
    import zicato.check

    monkeypatch.setattr(zicato, "adapter_factory", fake_factory, raising=False)
    monkeypatch.setattr(zicato.check, "require_workspace_valid", lambda *a, **k: None)


def _install_telemetry_stubs(
    monkeypatch: pytest.MonkeyPatch,
    *,
    canned_loss_by_gen: dict[str, float],
    canned_pass_by_gen: dict[str, bool],
) -> None:
    sink_mod = types.ModuleType("zicato.telemetry.sink")

    def make_run_sink_path(
        *,
        workspace_root: Path,
        epoch_id: str,
        generation_id: str,
        entry_id: str,
        replicate_index: int = 0,
    ) -> Path:
        del epoch_id, generation_id, entry_id, replicate_index
        return workspace_root / "events.jsonl"

    sink_mod.make_run_sink_path = make_run_sink_path  # type: ignore[attr-defined]

    reducer_mod = types.ModuleType("zicato.telemetry.reducer")

    def reduce_loss(
        events_jsonl_path: Path,
        entry: BoardEntry,
        generation_id: str,
        epoch_id: str,
        expectation_result: ExpectationResult | None,
        runtime_ms: int,
        wall_clock_budget_exceeded: bool,
        weights: Any,
    ) -> LossProfile:
        del events_jsonl_path, runtime_ms, wall_clock_budget_exceeded, weights
        return LossProfile(
            run_id=f"r-{generation_id}-{entry.id}",
            entry_id=entry.id,
            generation_id=generation_id,
            epoch_id=epoch_id,
            drift_counts=(DriftCount(kind="off_topic", severity="info", count=0),),
            plan_revisions=0,
            task_failure_ratio=0.0,
            runtime_ms=100,
            wall_clock_budget_exceeded=False,
            expectation_result=expectation_result,
            drift_loss=canned_loss_by_gen.get(generation_id, 0.0),
            pass_fail=canned_pass_by_gen.get(generation_id),
        )

    def read_loss_profile(path: Path) -> LossProfile:
        del path
        raise FileNotFoundError

    reducer_mod.reduce_loss = reduce_loss  # type: ignore[attr-defined]
    reducer_mod.read_loss_profile = read_loss_profile  # type: ignore[attr-defined]

    # Real, dependency-light meta_loop so the structural-span call sites can
    # import ``meta_span`` (a no-op here — no ambient emitter is bound).
    import zicato.telemetry.meta_loop as meta_loop_mod

    telemetry_pkg = types.ModuleType("zicato.telemetry")
    telemetry_pkg.sink = sink_mod  # type: ignore[attr-defined]
    telemetry_pkg.reducer = reducer_mod  # type: ignore[attr-defined]
    telemetry_pkg.meta_loop = meta_loop_mod  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "zicato.telemetry", telemetry_pkg)
    monkeypatch.setitem(sys.modules, "zicato.telemetry.sink", sink_mod)
    monkeypatch.setitem(sys.modules, "zicato.telemetry.reducer", reducer_mod)
    monkeypatch.setitem(sys.modules, "zicato.telemetry.meta_loop", meta_loop_mod)


def test_evolve_n_rounds_writes_heartbeat_and_releases_lock(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The beater writes ``heartbeat.json`` and the lock is released on exit."""
    workspace, epoch_id = _bootstrap_workspace(tmp_path)
    _install_stub_adapter_factory(monkeypatch)
    _install_telemetry_stubs(
        monkeypatch,
        canned_loss_by_gen={"v0": 2.0, "v1": 1.0},
        canned_pass_by_gen={"v0": True, "v1": True},
    )

    from zicato.orchestrator import evolve_n_rounds

    outcomes = asyncio.run(
        evolve_n_rounds(
            rounds=1,
            workspace_root=workspace,
            epoch_id=epoch_id,
            harness_call_llm=_harness_call_llm,
            auxiliary_call_llm=_make_aux_responder([_valid_proposer_response()]),
            instance_id="hb-test",
        )
    )

    assert len(outcomes) == 1

    # Heartbeat file exists and carries our pid + instance.
    hb = read_heartbeat(workspace)
    assert hb is not None
    assert hb.pid == os.getpid()
    assert hb.instance_id == "hb-test"
    assert hb.phase.startswith("evolve_n_rounds:done") or hb.phase.startswith("after_round_")
    # round_index was bumped to 0 during the round; survives shutdown.
    assert hb.round_index == 0

    # Lock has been released — re-acquiring should succeed immediately.
    assert not lock_path(workspace).exists()
    fresh = acquire_workspace_lock(workspace, "follow-up", steal_stale=False)
    assert fresh.instance_id == "follow-up"


def test_evolve_n_rounds_advances_progress_seq_and_marks_terminal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """RUNTIME-V2 Phase 4: the loop advances the progress seq on genuine
    transitions and stamps a terminal marker + heartbeat seq on a clean end.
    """
    from zicato.runtime import progress_log
    from zicato.runtime.paths import progress_log_path

    workspace, epoch_id = _bootstrap_workspace(tmp_path)
    _install_stub_adapter_factory(monkeypatch)
    _install_telemetry_stubs(
        monkeypatch,
        canned_loss_by_gen={"v0": 2.0, "v1": 1.0},
        canned_pass_by_gen={"v0": True, "v1": True},
    )

    from zicato.orchestrator import evolve_n_rounds

    outcomes = asyncio.run(
        evolve_n_rounds(
            rounds=1,
            workspace_root=workspace,
            epoch_id=epoch_id,
            harness_call_llm=_harness_call_llm,
            auxiliary_call_llm=_make_aux_responder([_valid_proposer_response()]),
            instance_id="seq-test",
        )
    )
    assert len(outcomes) == 1

    # The progress log was written and its seq advanced past the first
    # transition — genuine progress was recorded (not just timer beats).
    assert progress_log_path(workspace).exists()
    tail_seq = progress_log.tail_seq(workspace)
    assert tail_seq >= 4  # LOOP_START, ROUND_START, PROPOSE, TOURNAMENT_*, ...

    # The clean end stamped a terminal marker (SETTLED), distinguishable
    # from a stalled run (a mid-flight progress tail).
    assert progress_log.tail_is_terminal(workspace)
    last = progress_log.tail(workspace)
    assert last is not None
    assert last.type == progress_log.SETTLED

    # The heartbeat carries the same tail seq as its liveness cursor.
    hb = read_heartbeat(workspace)
    assert hb is not None
    assert hb.seq == tail_seq

    # A second invocation clears the prior log so its seq restarts from 1
    # (a stale tail must never read as live progress).
    asyncio.run(
        evolve_n_rounds(
            rounds=1,
            workspace_root=workspace,
            epoch_id=epoch_id,
            harness_call_llm=_harness_call_llm,
            auxiliary_call_llm=_make_aux_responder([_valid_proposer_response()]),
            instance_id="seq-test",
        )
    )
    # The fresh log's first event is seq 1 (the LOOP_START of the new run).
    events = progress_log._log(workspace).read()
    assert events[0].seq == 1
    assert events[0].type == progress_log.LOOP_START


def test_evolve_n_rounds_refuses_when_workspace_locked(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """If a live foreign lock exists, evolve_n_rounds raises ``WorkspaceLockHeld``."""
    workspace, epoch_id = _bootstrap_workspace(tmp_path)
    _install_stub_adapter_factory(monkeypatch)
    _install_telemetry_stubs(
        monkeypatch,
        canned_loss_by_gen={"v0": 2.0, "v1": 1.0},
        canned_pass_by_gen={"v0": True, "v1": True},
    )

    # Plant a foreign lock for a definitely-alive pid (our parent).
    from zicato.runtime.paths import ensure_runtime_dirs
    from zicato.runtime.paths import lock_path as _lp
    from zicato.storage import atomic_write_json

    ensure_runtime_dirs(workspace)
    atomic_write_json(
        _lp(workspace),
        {
            "pid": os.getppid(),
            "instance_id": "other",
            "acquired_at": "2026-05-14T00:00:00Z",
            "workspace_root": str(workspace),
        },
    )

    from zicato.orchestrator import evolve_n_rounds

    with pytest.raises(WorkspaceLockHeld):
        asyncio.run(
            evolve_n_rounds(
                rounds=1,
                workspace_root=workspace,
                epoch_id=epoch_id,
                harness_call_llm=_harness_call_llm,
                auxiliary_call_llm=_make_aux_responder([_valid_proposer_response()]),
                instance_id="hb-test",
            )
        )

    # Heartbeat file was never written, since acquire blew up before beater.start.
    assert not heartbeat_path(workspace).exists()
