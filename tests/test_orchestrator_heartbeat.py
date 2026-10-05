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

from tests._foe_support import stand_in_proposer_block
from tests._orchestrator_harness import (
    evaluation_call_llm,
    target_call_llm,
)
from zicato.core.types import (
    BoardEntry,
    ExpectationResult,
    LossProfile,
    MetricCount,
    RunResult,
    ScoringWeights,
)
from zicato.epoch.lifecycle import new_epoch
from zicato.runtime.lock import WorkspaceLockHeld, acquire_workspace_lock
from zicato.runtime.paths import heartbeat_path, lock_path
from zicato.runtime.state import read_heartbeat


def _bootstrap_workspace(
    tmp_path: Path, entry_ids: tuple[str, ...] = ("entry_a",)
) -> tuple[Path, str]:
    workspace = tmp_path / ".zicato"
    workspace.mkdir()
    (workspace / "config.json").write_text(
        json.dumps(
            {
                "instance_id": "test",
                "proposer": stand_in_proposer_block(tmp_path / "foe"),
                "created_at": "2026-05-14T00:00:00Z",
                # Hand-built directory-backend snapshot layout below; pin the
                # directory backend so the git default does not look for git
                # tags this fixture never writes.
                "generation_source_backend": "directory",
                "adapter": {"kind": "import", "factory": "tests._stub_adapter:make_stub_adapter"},
                "runtime": {},
                "models": {
                    "engines": {
                        "target": {"call_llm": "tests._orchestrator_harness:target_call_llm"},
                        "evaluation": {
                            "call_llm": "tests._orchestrator_harness:evaluation_call_llm"
                        },
                    }
                },
            }
        )
    )

    board_src = tmp_path / "board.jsonl"
    board_src.write_text(
        "".join(
            json.dumps(
                {
                    "id": entry_id,
                    "kind": "single_turn",
                    "wall_clock_budget_seconds": 60,
                    "input": "hello",
                }
            )
            + "\n"
            for entry_id in entry_ids
        )
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
    from zicato.epoch.journal import write_seed_experiment

    write_seed_experiment(workspace, cfg.id, proposed_at=cfg.created_at)
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
    fake_factory.make_adapter_from_config = lambda cfg, *, workspace_root: _StubAdapter()  # type: ignore[attr-defined]
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
    from zicato.telemetry.sink import resolve_harmonograf_grpc_target, resolve_harmonograf_url

    sink_mod = types.ModuleType("zicato.telemetry.sink")
    sink_mod.resolve_harmonograf_url = resolve_harmonograf_url
    sink_mod.resolve_harmonograf_grpc_target = resolve_harmonograf_grpc_target

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
            metric_counts=(MetricCount(name="drift:off_topic", severity="info", count=0),),
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
    import zicato.telemetry as telemetry_pkg
    import zicato.telemetry.meta_loop as meta_loop_mod

    monkeypatch.setattr(telemetry_pkg, "sink", sink_mod)
    monkeypatch.setattr(telemetry_pkg, "reducer", reducer_mod)
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
            target_call_llm=target_call_llm,
            evaluation_call_llm=evaluation_call_llm,
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
    with acquire_workspace_lock(workspace, "follow-up", steal_stale=False) as fresh:
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
            target_call_llm=target_call_llm,
            evaluation_call_llm=evaluation_call_llm,
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
            target_call_llm=target_call_llm,
            evaluation_call_llm=evaluation_call_llm,
            instance_id="seq-test",
        )
    )
    # The fresh log's first event is seq 1 (the LOOP_START of the new run).
    events = progress_log._log(workspace).read()
    assert events[0].seq == 1
    assert events[0].type == progress_log.LOOP_START


def test_each_scored_board_unit_advances_the_heartbeat_seq(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A tournament advances the liveness seq once per scored board unit.

    Between the tournament's start and settle transitions the progress log
    carries one ``UnitSettled`` event per scored unit, and the heartbeat is
    written with each of those seqs. The supervisor measures staleness as
    the time since ``seq`` last changed, so a long tournament whose units
    keep finishing never reads as stale.
    """
    from zicato.runtime import heartbeat as heartbeat_mod
    from zicato.runtime import progress_log

    entry_ids = ("entry_a", "entry_b", "entry_c", "entry_d")
    workspace, epoch_id = _bootstrap_workspace(tmp_path, entry_ids)
    _install_stub_adapter_factory(monkeypatch)
    _install_telemetry_stubs(
        monkeypatch,
        canned_loss_by_gen={"v0": 2.0, "v1": 1.0},
        canned_pass_by_gen={"v0": True, "v1": True},
    )
    written_seqs: list[int] = []
    real_write = heartbeat_mod.write_heartbeat

    def recording_write(workspace_root: Path, hb: Any) -> None:
        written_seqs.append(hb.seq)
        real_write(workspace_root, hb)

    monkeypatch.setattr(heartbeat_mod, "write_heartbeat", recording_write)

    from zicato.orchestrator import evolve_n_rounds

    asyncio.run(
        evolve_n_rounds(
            rounds=1,
            workspace_root=workspace,
            epoch_id=epoch_id,
            target_call_llm=target_call_llm,
            evaluation_call_llm=evaluation_call_llm,
            instance_id="unit-seq-test",
        )
    )

    events = progress_log._log(workspace).read()
    types_in_order = [event.type for event in events]
    start = types_in_order.index(progress_log.TOURNAMENT_START)
    settle = types_in_order.index(progress_log.TOURNAMENT_SETTLE)
    # The persisted event type, spelled as readers of the log see it.
    unit_events = [e for e in events[start:settle] if e.type == "UnitSettled"]
    assert len(unit_events) >= len(entry_ids), types_in_order
    assert all(event.seq in written_seqs for event in unit_events), (unit_events, written_seqs)


def test_every_heartbeat_write_carries_the_progress_log_tail_seq(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Across a full round, each heartbeat write carries the log's last ``seq``.

    Every transition, including tournament start and settle, is appended and
    stamped in one step, so no heartbeat lags the log and no log ``seq`` is
    missing from the heartbeats.
    """
    from zicato.runtime import heartbeat as heartbeat_mod
    from zicato.runtime import progress_log

    workspace, epoch_id = _bootstrap_workspace(tmp_path, ("entry_a", "entry_b"))
    _install_stub_adapter_factory(monkeypatch)
    _install_telemetry_stubs(
        monkeypatch,
        canned_loss_by_gen={"v0": 2.0, "v1": 1.0},
        canned_pass_by_gen={"v0": True, "v1": True},
    )
    pairs: list[tuple[int, int]] = []
    real_write = heartbeat_mod.write_heartbeat

    def recording_write(workspace_root: Path, hb: Any) -> None:
        pairs.append((hb.seq, progress_log.tail_seq(workspace_root)))
        real_write(workspace_root, hb)

    monkeypatch.setattr(heartbeat_mod, "write_heartbeat", recording_write)

    from zicato.orchestrator import evolve_n_rounds

    asyncio.run(
        evolve_n_rounds(
            rounds=1,
            workspace_root=workspace,
            epoch_id=epoch_id,
            target_call_llm=target_call_llm,
            evaluation_call_llm=evaluation_call_llm,
            instance_id="seq-agreement-test",
        )
    )

    assert pairs and all(hb_seq == log_seq for hb_seq, log_seq in pairs), pairs
    events = progress_log._log(workspace).read()
    assert {"TournamentStart", "TournamentSettle"} <= {event.type for event in events}
    assert {event.seq for event in events} <= {hb_seq for hb_seq, _ in pairs}
    # Each challenger's proposal phase advances seq as its episodes settle:
    # one per best-of-N slate slot and one for the challenger's proposal.
    types = [event.type for event in events]
    proposal_phase = types[types.index("Propose") : types.index("TournamentStart")]
    assert proposal_phase.count("EpisodeSettled") > proposal_phase.count("Propose"), types


def test_evolve_once_with_a_beater_records_units_and_episodes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The single-round API advances the caller's heartbeat as the loop does."""
    from zicato.runtime import progress_log
    from zicato.runtime.heartbeat import HeartbeatBeater

    workspace, epoch_id = _bootstrap_workspace(tmp_path, ("entry_a", "entry_b"))
    _install_stub_adapter_factory(monkeypatch)
    _install_telemetry_stubs(
        monkeypatch,
        canned_loss_by_gen={"v0": 2.0, "v1": 1.0},
        canned_pass_by_gen={"v0": True, "v1": True},
    )

    from zicato.orchestrator import evolve_once

    async def run_round() -> None:
        beater = HeartbeatBeater(workspace, "once-test", interval_s=60.0)
        await beater.start()
        try:
            await evolve_once(
                workspace_root=workspace,
                epoch_id=epoch_id,
                target_call_llm=target_call_llm,
                evaluation_call_llm=evaluation_call_llm,
                instance_id="once-test",
                beater=beater,
            )
        finally:
            await beater.stop()

    asyncio.run(run_round())

    types = [event.type for event in progress_log._log(workspace).read()]
    assert "UnitSettled" in types, types
    assert "EpisodeSettled" in types, types
    hb = read_heartbeat(workspace)
    assert hb is not None
    assert hb.seq == progress_log.tail_seq(workspace)


def test_only_the_heartbeat_writer_appends_progress() -> None:
    """``append_progress`` has one caller, the writer that also stamps the heartbeat."""
    import zicato

    package = Path(zicato.__file__).parent
    callers = sorted(
        str(path.relative_to(package))
        for path in package.rglob("*.py")
        if "append_progress(" in path.read_text(encoding="utf-8") and path.name != "progress_log.py"
    )
    assert callers == ["evolve/lifecycle_services.py"]


def test_transitions_are_not_recorded_outside_an_evolve_loop(tmp_path: Path) -> None:
    """Without a bound recorder, a scored unit leaves the progress log untouched.

    A standalone tournament must not append after a loop's terminal event:
    the dashboard reads a non-terminal tail as an unfinished run.
    """
    from zicato.runtime import progress_log
    from zicato.runtime.paths import progress_log_path

    progress_log.record_transition(progress_log.UNIT_SETTLED)
    assert not progress_log_path(tmp_path).exists()

    calls: list[str] = []
    token = progress_log.bind_transition_recorder(calls.append)
    try:
        progress_log.record_transition(progress_log.UNIT_SETTLED)
    finally:
        progress_log.reset_transition_recorder(token)
    progress_log.record_transition(progress_log.UNIT_SETTLED)
    assert calls == ["UnitSettled"]


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
            "proposer": stand_in_proposer_block(tmp_path / "foe"),
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
                target_call_llm=target_call_llm,
                evaluation_call_llm=evaluation_call_llm,
                instance_id="hb-test",
            )
        )

    # Heartbeat file was never written, since acquire blew up before beater.start.
    assert not heartbeat_path(workspace).exists()
