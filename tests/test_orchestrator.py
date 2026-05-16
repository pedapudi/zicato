"""Tests for :mod:`zicato.orchestrator`.

These tests stub every external dependency (LLM callables, harness
adapter, telemetry sink, reducer) so the orchestrator can be exercised
end-to-end without goldfive, google-adk, or any real model traffic.
"""

from __future__ import annotations

import asyncio
import json
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from zicato.core.types import (
    BoardEntry,
    DriftCount,
    ExpectationResult,
    LossProfile,
    RunResult,
    ScoringWeights,
)
from zicato.epoch.lifecycle import new_epoch

# ---------------------------------------------------------------------------
# LLM stub callables — two distinct objects so the two-callable check passes.
# ---------------------------------------------------------------------------


async def _harness_call_llm(system: str, user: str, model: str) -> str:
    del system, user, model
    return ""


def _make_aux_responder(responses: list[str]) -> Any:
    """Return a fresh async aux callable that yields ``responses`` in order."""
    state = {"i": 0}

    async def _aux(system: str, user: str, model: str) -> str:
        del system, user, model
        i = state["i"]
        if i >= len(responses):
            raise AssertionError("stub aux LLM ran out of responses")
        state["i"] = i + 1
        return responses[i]

    return _aux


# ---------------------------------------------------------------------------
# Workspace bootstrap
# ---------------------------------------------------------------------------


def _bootstrap_workspace(tmp_path: Path) -> tuple[Path, str]:
    """Create a workspace + one epoch + a v0 baseline snapshot.

    The snapshot contains a single Python file with one zicato:mutable
    marker so the enumerator + applier paths exercise the real code.
    """
    workspace = tmp_path / ".zicato"
    workspace.mkdir()
    (workspace / "config.json").write_text(
        json.dumps(
            {
                "instance_id": "test",
                "created_at": "2026-05-14T00:00:00Z",
                "adapter": {
                    "kind": "stub",
                    # We replace the dispatch in the test below.
                },
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
    rubric_src = tmp_path / "rubric.md"
    rubric_src.write_text("# Rubric\n- Be careful.\n")

    cfg = new_epoch(
        workspace,
        name="alpha",
        board_source=board_src,
        rubric_source=rubric_src,
        weights=ScoringWeights(promote_margin=0.01),
        auto_close_previous=False,
    )

    # Build v0 snapshot.
    v0_dir = workspace / "epochs" / cfg.id / "generations" / "v0"
    snap = v0_dir / "snapshot"
    snap.mkdir(parents=True)
    (snap / "agent.py").write_text(
        '"""Stub harness source for tests."""\n'
        "\n"
        '# zicato:mutable id="greeting"\n'
        'GREETING = "hello"\n'
    )
    return workspace, cfg.id


# ---------------------------------------------------------------------------
# Adapter / telemetry stubs
# ---------------------------------------------------------------------------


def _install_stub_adapter_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Replace zicato.adapter_factory with one that dispatches kind='stub'."""

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

    def make_adapter_from_config(workspace_config: dict[str, Any]) -> Any:
        del workspace_config
        return _StubAdapter()

    fake_factory.make_adapter_from_config = make_adapter_from_config  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "zicato.adapter_factory", fake_factory)
    # Re-bind on the zicato namespace so `from zicato import adapter_factory`
    # picks up the stub.
    import zicato

    monkeypatch.setattr(zicato, "adapter_factory", fake_factory, raising=False)


def _install_telemetry_stubs(
    monkeypatch: pytest.MonkeyPatch,
    *,
    canned_loss_by_gen: dict[str, float],
    canned_pass_by_gen: dict[str, bool],
) -> None:
    """Install ad-hoc telemetry.sink / .reducer modules."""

    sink_mod = types.ModuleType("zicato.telemetry.sink")

    def make_run_sink_path(
        *,
        workspace_root: Path,
        epoch_id: str,
        generation_id: str,
        entry_id: str,
    ) -> Path:
        del epoch_id, generation_id, entry_id
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

    # read_loss_profile is also imported by the orchestrator — point it
    # at a stub that returns an empty list-safe LossProfile when the
    # file does not exist.
    def read_loss_profile(path: Path) -> LossProfile:
        del path
        raise FileNotFoundError

    reducer_mod.reduce_loss = reduce_loss  # type: ignore[attr-defined]
    reducer_mod.read_loss_profile = read_loss_profile  # type: ignore[attr-defined]

    telemetry_pkg = types.ModuleType("zicato.telemetry")
    telemetry_pkg.sink = sink_mod  # type: ignore[attr-defined]
    telemetry_pkg.reducer = reducer_mod  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "zicato.telemetry", telemetry_pkg)
    monkeypatch.setitem(sys.modules, "zicato.telemetry.sink", sink_mod)
    monkeypatch.setitem(sys.modules, "zicato.telemetry.reducer", reducer_mod)


# ---------------------------------------------------------------------------
# Proposer canned response
# ---------------------------------------------------------------------------


def _valid_proposer_response() -> str:
    """A schema-valid response targeting the stub snapshot's marker."""
    return json.dumps(
        {
            "hypothesis": {
                "core_idea": "swap the greeting string",
                "modulating": ["greeting"],
                "why": "Baseline drift baseline run, exercising the orchestrator.",
                "expected_drift_movements": [
                    {
                        "kind": "off_topic",
                        "direction": "decrease",
                        "magnitude": "small",
                    }
                ],
                "expected_pass_rate_delta": "+0.0 to +0.1",
                "risks": "harmless",
            },
            "patches": [
                {
                    "mutation_id": "greeting",
                    "op": "replace",
                    "new_content": '"world"',
                    "rationale": "different greeting word",
                }
            ],
        }
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_evolve_once_promotes_on_improvement(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A child with strictly lower drift_loss and same pass_rate promotes."""
    workspace, epoch_id = _bootstrap_workspace(tmp_path)
    _install_stub_adapter_factory(monkeypatch)
    _install_telemetry_stubs(
        monkeypatch,
        canned_loss_by_gen={"v0": 2.0, "v1": 1.0},
        canned_pass_by_gen={"v0": True, "v1": True},
    )

    from zicato.orchestrator import evolve_once

    outcome = asyncio.run(
        evolve_once(
            workspace_root=workspace,
            epoch_id=epoch_id,
            harness_call_llm=_harness_call_llm,
            auxiliary_call_llm=_make_aux_responder([_valid_proposer_response()]),
        )
    )

    assert outcome.tournament_decision == "promoted"
    assert outcome.parent_generation_id == "v0"
    assert outcome.proposed_generation_id == "v1"
    assert outcome.child_scalar < outcome.parent_scalar

    # experiment.json + patches/{id}.json exist for v1.
    v1_dir = workspace / "epochs" / epoch_id / "generations" / "v1"
    assert (v1_dir / "experiment.json").exists()
    body = json.loads((v1_dir / "experiment.json").read_text())
    assert body["outcome"]["tournament_decision"] == "promoted"
    assert len(body["patch_ids"]) == 1
    patch_file = v1_dir / "patches" / f"{body['patch_ids'][0]}.json"
    assert patch_file.exists()

    # Snapshot was applied: the new greeting landed.
    snap_text = (v1_dir / "snapshot" / "agent.py").read_text()
    assert '"world"' in snap_text

    # current_generation marker bumped.
    marker = workspace / "epochs" / epoch_id / "current_generation"
    assert marker.exists()
    assert marker.read_text().strip() == "v1"

    # Journal entry appended.
    journal = (workspace / "epochs" / epoch_id / "journal.md").read_text()
    assert "swap the greeting string" in journal


def test_evolve_once_rejects_when_child_regresses(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A child with higher drift and lower pass_rate does NOT promote."""
    workspace, epoch_id = _bootstrap_workspace(tmp_path)
    _install_stub_adapter_factory(monkeypatch)
    _install_telemetry_stubs(
        monkeypatch,
        canned_loss_by_gen={"v0": 0.0, "v1": 5.0},
        canned_pass_by_gen={"v0": True, "v1": False},
    )

    from zicato.orchestrator import evolve_once

    outcome = asyncio.run(
        evolve_once(
            workspace_root=workspace,
            epoch_id=epoch_id,
            harness_call_llm=_harness_call_llm,
            auxiliary_call_llm=_make_aux_responder([_valid_proposer_response()]),
        )
    )

    assert outcome.tournament_decision == "rejected"
    assert outcome.rejection_reason  # non-empty

    # current_generation marker NOT bumped — still v0 (no marker yet).
    marker = workspace / "epochs" / epoch_id / "current_generation"
    assert not marker.exists()

    # Experiment.json still persisted with the rejected outcome.
    v1_dir = workspace / "epochs" / epoch_id / "generations" / "v1"
    body = json.loads((v1_dir / "experiment.json").read_text())
    assert body["outcome"]["tournament_decision"] == "rejected"


def test_evolve_n_rounds_stops_on_consecutive_rejections(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Three rejections in a row should halt the loop early."""
    workspace, epoch_id = _bootstrap_workspace(tmp_path)
    _install_stub_adapter_factory(monkeypatch)
    # Same canned losses → every round rejects.
    _install_telemetry_stubs(
        monkeypatch,
        canned_loss_by_gen={"v0": 0.0, "v1": 5.0, "v2": 5.0, "v3": 5.0, "v4": 5.0},
        canned_pass_by_gen={"v0": True, "v1": False, "v2": False, "v3": False, "v4": False},
    )

    from zicato.orchestrator import evolve_n_rounds

    # Need a fresh proposer response per round because each call
    # consumes one — supply 10 (more than enough for any path).
    responses = [_valid_proposer_response() for _ in range(10)]
    outcomes = asyncio.run(
        evolve_n_rounds(
            rounds=8,
            workspace_root=workspace,
            epoch_id=epoch_id,
            harness_call_llm=_harness_call_llm,
            auxiliary_call_llm=_make_aux_responder(responses),
            max_consecutive_rejections=3,
        )
    )
    assert len(outcomes) == 3
    assert all(o.tournament_decision == "rejected" for o in outcomes)


def test_evolve_round_writes_per_patch_layout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The orchestrator persists patches via the per-patch storage layout."""
    workspace, epoch_id = _bootstrap_workspace(tmp_path)
    _install_stub_adapter_factory(monkeypatch)
    _install_telemetry_stubs(
        monkeypatch,
        canned_loss_by_gen={"v0": 2.0, "v1": 1.0},
        canned_pass_by_gen={"v0": True, "v1": True},
    )

    from zicato.orchestrator import evolve_once

    asyncio.run(
        evolve_once(
            workspace_root=workspace,
            epoch_id=epoch_id,
            harness_call_llm=_harness_call_llm,
            auxiliary_call_llm=_make_aux_responder([_valid_proposer_response()]),
        )
    )

    v1 = workspace / "epochs" / epoch_id / "generations" / "v1"
    body = json.loads((v1 / "experiment.json").read_text())
    assert "patches" not in body  # inline form is NEVER written by new code
    assert isinstance(body["patch_ids"], list)
    assert len(body["patch_ids"]) == 1
    assert (v1 / "patches" / f"{body['patch_ids'][0]}.json").exists()


# ---------------------------------------------------------------------------
# mutations.json per-epoch snapshot
# ---------------------------------------------------------------------------


def test_mutations_json_path_helper(tmp_path: Path) -> None:
    """mutations_json_path resolves under the epoch directory."""
    from zicato.core.workspace import epoch_dir, mutations_json_path

    p = mutations_json_path(tmp_path, "ep1")
    assert p == epoch_dir(tmp_path, "ep1") / "mutations.json"
    assert p.name == "mutations.json"


def test_evolve_once_dumps_mutations_json(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """evolve_once snapshots the enumerated mutation surface to mutations.json.

    The file lands at ``epochs/{epoch}/mutations.json`` and is a JSON
    array of objects with exactly the
    ``{id, kind, file, line_start, line_end, content, content_hash}``
    shape — Path fields stringified.
    """
    workspace, epoch_id = _bootstrap_workspace(tmp_path)
    _install_stub_adapter_factory(monkeypatch)
    _install_telemetry_stubs(
        monkeypatch,
        canned_loss_by_gen={"v0": 2.0, "v1": 1.0},
        canned_pass_by_gen={"v0": True, "v1": True},
    )

    from zicato.core.workspace import mutations_json_path
    from zicato.orchestrator import evolve_once

    asyncio.run(
        evolve_once(
            workspace_root=workspace,
            epoch_id=epoch_id,
            harness_call_llm=_harness_call_llm,
            auxiliary_call_llm=_make_aux_responder([_valid_proposer_response()]),
        )
    )

    snapshot_path = mutations_json_path(workspace, epoch_id)
    assert snapshot_path.exists()
    points = json.loads(snapshot_path.read_text())
    assert isinstance(points, list)
    # The stub snapshot carries a single zicato:mutable marker.
    assert len(points) == 1
    point = points[0]
    assert set(point.keys()) == {
        "id",
        "kind",
        "file",
        "line_start",
        "line_end",
        "content",
        "content_hash",
    }
    assert point["id"] == "greeting"
    assert point["kind"] == "span"
    # Path fields are stringified for JSON.
    assert isinstance(point["file"], str)
    assert point["file"].endswith("agent.py")
    assert isinstance(point["line_start"], int)
    assert isinstance(point["line_end"], int)
    assert '"hello"' in point["content"]
    assert isinstance(point["content_hash"], str)
    # No leftover .tmp file from the atomic write.
    assert not snapshot_path.with_name(snapshot_path.name + ".tmp").exists()
