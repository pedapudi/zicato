"""End-to-end-ish test: ``evolve`` auto-rolls the epoch on contract drift.

Runs the full :func:`zicato.orchestrator.evolve_n_rounds` loop with a
stub adapter and stub LLMs (no goldfive, no google-adk, no real model
traffic), then edits the live rubric file and runs ``evolve`` again —
the second run must detect the contract change and create a new epoch.
"""

from __future__ import annotations

import asyncio
import json
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from tests._foe_support import stand_in_proposer_block
from tests._orchestrator_harness import target_call_llm
from tests._stub_adapter import make_stub_adapter
from zicato.core.types import (
    BoardEntry,
    DriftCount,
    ExpectationResult,
    LossProfile,
)
from zicato.epoch.lifecycle import current_epoch_id, list_epochs

# ---------------------------------------------------------------------------
# LLM stubs
# ---------------------------------------------------------------------------


def _proposer_response() -> str:
    """A schema-valid proposer response targeting the stub marker."""
    return json.dumps(
        {
            "hypothesis": {
                "core_idea": "swap the greeting string",
                "modulating": ["greeting"],
                "why": "Baseline round exercising the orchestrator.",
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


def _make_aux() -> Any:
    """An aux callable that always returns a fresh valid proposer response.

    Unlike the orchestrator test's exhausting responder, this one never
    runs dry — auto-epoch close also calls the aux LLM (analysis pass),
    and the round count across two evolve invocations is hard to
    predict, so an inexhaustible stub keeps the test simple.
    """

    async def _aux(system: str, user: str, model: str) -> str:
        del user, model
        # The proposer expects JSON; the analysis pass expects prose.
        # Returning JSON for everything is fine — the analysis pass
        # tolerates arbitrary text.
        if "hypothesis" in system or "proposer" in system.lower():
            return _proposer_response()
        return _proposer_response()

    return _aux


# ---------------------------------------------------------------------------
# Workspace bootstrap — a *registered* workspace with live contract files
# ---------------------------------------------------------------------------


def _bootstrap_registered(tmp_path: Path) -> tuple[Path, Path]:
    """Create a registered workspace + a mutable source tree.

    Returns ``(workspace_root, rubric_path)``. No epoch is created —
    the first ``evolve`` is expected to auto-create ``e0``.
    """
    workspace = tmp_path / ".zicato"
    workspace.mkdir()

    # Live contract files next to the workspace.
    board = tmp_path / "board.jsonl"
    rubric = tmp_path / "rubric.md"
    scoring = tmp_path / "scoring.json"
    board.write_text(
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
    rubric.write_text("# Rubric\n- Be careful.\n")
    scoring.write_text(json.dumps({"pass_weight": 1.0}))

    # The mutable source tree.
    agent = tmp_path / "agent"
    agent.mkdir()
    (agent / "agent.py").write_text(
        '"""Stub harness source for tests."""\n'
        "\n"
        '# zicato:mutable id="greeting"\n'
        'GREETING = "hello"\n'
    )

    (workspace / "config.json").write_text(
        json.dumps(
            {
                "instance_id": "test",
                "proposer": stand_in_proposer_block(tmp_path / "foe"),
                "adapter": {
                    "kind": "import",
                    "factory": "tests._stub_adapter:make_stub_adapter",
                },
                "adk_entrypoint": "pkg.mod:agent",
                # This suite asserts the directory-backend snapshot layout
                # (epochs/.../generations/v0/snapshot/) after a contract
                # roll, so it pins the directory backend; the git default
                # keeps generations in the private repo, not that path.
                "generation_source_backend": "directory",
                "mutable_trees": [str(agent)],
                "source_roots": [str(agent)],
                "contract": {
                    "board_path": str(board),
                    "rubric_path": str(rubric),
                    "scoring_path": str(scoring),
                },
            }
        )
    )
    return workspace, rubric


# ---------------------------------------------------------------------------
# Adapter / telemetry stubs (mirrors tests/test_orchestrator.py)
# ---------------------------------------------------------------------------


def _install_stub_adapter_factory(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_factory = types.ModuleType("zicato.adapter_factory")

    def make_adapter_from_config(workspace_config: dict[str, Any]) -> Any:
        del workspace_config
        return make_stub_adapter()

    fake_factory.make_adapter_from_config = make_adapter_from_config  # type: ignore[attr-defined]
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

    # The L3 subprocess-isolation refactor moved per-run execution into a
    # worker subprocess that cannot see these sys.modules stubs. These
    # auto-epoch tests exercise the evolve loop above the per-run
    # mechanism, so we stub ``runner._run_single`` directly with the same
    # canned LossProfile the in-process reduce_loss stub would produce.
    import zicato.tournament.runner as _runner_mod

    async def _fake_run_single(
        *,
        adapter: Any,
        generation: Any,
        entry: BoardEntry,
        weights: Any,
        config: Any,
        workspace_root: Path,
        epoch_id: str,
        side: str,
        match_id: str = "",
    ) -> LossProfile:
        del adapter, weights, config, workspace_root, side, match_id
        expectation_result = (
            ExpectationResult(kind="predicate", passed=True)
            if entry.expectation is not None
            else None
        )
        return LossProfile(
            run_id=f"r-{generation.id}-{entry.id}",
            entry_id=entry.id,
            generation_id=generation.id,
            epoch_id=epoch_id,
            drift_counts=(DriftCount(kind="off_topic", severity="info", count=0),),
            plan_revisions=0,
            task_failure_ratio=0.0,
            runtime_ms=100,
            wall_clock_budget_exceeded=False,
            expectation_result=expectation_result,
            drift_loss=canned_loss_by_gen.get(generation.id, 0.0),
            pass_fail=canned_pass_by_gen.get(generation.id),
        )

    monkeypatch.setattr(_runner_mod, "_run_single", _fake_run_single)


# ---------------------------------------------------------------------------
# The end-to-end-ish test
# ---------------------------------------------------------------------------


def test_evolve_auto_creates_then_rolls_on_rubric_edit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    workspace, rubric = _bootstrap_registered(tmp_path)
    _install_stub_adapter_factory(monkeypatch)
    # Always promote so each generation advances the head.
    _install_telemetry_stubs(
        monkeypatch,
        canned_loss_by_gen={
            "v0": 5.0,
            "v1": 1.0,
            "v2": 0.5,
            "v3": 0.25,
        },
        canned_pass_by_gen={
            "v0": True,
            "v1": True,
            "v2": True,
            "v3": True,
        },
    )

    from zicato.orchestrator import evolve_n_rounds

    # First evolve — no epoch exists yet → auto-create e0.
    outcomes = asyncio.run(
        evolve_n_rounds(
            rounds=1,
            workspace_root=workspace,
            epoch_id=None,
            target_call_llm=target_call_llm,
            evaluation_call_llm=_make_aux(),
        )
    )
    assert len(outcomes) == 1
    epoch_after_first = current_epoch_id(workspace)
    assert epoch_after_first is not None
    assert epoch_after_first.endswith("_e0")
    assert len(list_epochs(workspace)) == 1

    # Edit the live rubric — the evaluation contract drifts.
    rubric.write_text("# Rubric\n- Be careful.\n- And: cite your sources.\n")

    # Second evolve — must detect the drift and roll to a new epoch.
    outcomes2 = asyncio.run(
        evolve_n_rounds(
            rounds=1,
            workspace_root=workspace,
            epoch_id=None,
            target_call_llm=target_call_llm,
            evaluation_call_llm=_make_aux(),
        )
    )
    assert len(outcomes2) == 1
    epoch_after_second = current_epoch_id(workspace)
    assert epoch_after_second != epoch_after_first
    assert epoch_after_second is not None
    assert epoch_after_second.endswith("_e1")

    # Two epochs now; the first is closed, the second is open.
    epochs = list_epochs(workspace)
    assert len(epochs) == 2
    by_id = {e.id: e for e in epochs}
    assert by_id[epoch_after_first].closed
    assert not by_id[epoch_after_second].closed

    # The rolled epoch's v0 baseline was seeded from the previous
    # epoch's promoted head (the lineage continues).
    new_v0_snapshot = workspace / "epochs" / epoch_after_second / "generations" / "v0" / "snapshot"
    assert new_v0_snapshot.exists()
    # The first evolve promoted v1 (greeting -> "world"); the rolled
    # epoch's v0 must carry that promoted content forward.
    agent_files = list(new_v0_snapshot.rglob("agent.py"))
    assert agent_files, "expected agent.py in the seeded v0 snapshot"
    assert "hello [v1]" in agent_files[0].read_text()

    # Cross-epoch lineage edge recorded.
    lineage = json.loads((workspace / "lineage.json").read_text())
    second = next(e for e in lineage["epochs"] if e["id"] == epoch_after_second)
    assert second["v0_parent"] == epoch_after_first


def test_pending_settlement_finishes_before_contract_drift_rolls_epoch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Auto-roll seeds from the recovered champion, not the pre-crash head."""
    from zicato.epoch.journal import read_experiment
    from zicato.evolve import settlement as settlement_module
    from zicato.evolve.settlement_recovery import (
        commit_field_settlement,
        field_settlement_intent_path,
    )
    from zicato.orchestrator import evolve_n_rounds

    workspace, rubric = _bootstrap_registered(tmp_path)
    _install_stub_adapter_factory(monkeypatch)
    _install_telemetry_stubs(
        monkeypatch,
        canned_loss_by_gen={"v0": 5.0, "v1": 1.0},
        canned_pass_by_gen={"v0": True, "v1": True},
    )

    real_commit = settlement_module.commit_field_settlement

    def stop_after_receipt(root: Path, intent: dict[str, Any]) -> None:
        def stop(boundary: str) -> None:
            if boundary == "receipt_persisted":
                raise RuntimeError(boundary)

        commit_field_settlement(root, intent, crash_checkpoint=stop)

    monkeypatch.setattr(settlement_module, "commit_field_settlement", stop_after_receipt)
    with pytest.raises(RuntimeError, match="receipt_persisted"):
        asyncio.run(
            evolve_n_rounds(
                rounds=1,
                workspace_root=workspace,
                epoch_id=None,
                target_call_llm=target_call_llm,
                evaluation_call_llm=_make_aux(),
            )
        )
    crashed_epoch = current_epoch_id(workspace)
    assert crashed_epoch is not None
    assert read_experiment(workspace, crashed_epoch, "v1").outcome is None

    rubric.write_text("# Rubric\n- Be careful.\n- Cite sources.\n")
    monkeypatch.setattr(settlement_module, "commit_field_settlement", real_commit)
    asyncio.run(
        evolve_n_rounds(
            rounds=1,
            workspace_root=workspace,
            epoch_id=None,
            target_call_llm=target_call_llm,
            evaluation_call_llm=_make_aux(),
        )
    )

    rolled_epoch = current_epoch_id(workspace)
    assert rolled_epoch is not None and rolled_epoch != crashed_epoch
    receipt = json.loads(
        field_settlement_intent_path(workspace, crashed_epoch, 0).read_text(encoding="utf-8")
    )
    assert receipt["state"] == "committed"
    assert read_experiment(workspace, crashed_epoch, "v1").outcome is not None
    assert (workspace / "epochs" / crashed_epoch / "current_generation").read_text().strip() == "v1"
    lineage = json.loads((workspace / "lineage.json").read_text(encoding="utf-8"))
    rolled = next(row for row in lineage["epochs"] if row["id"] == rolled_epoch)
    assert rolled["v0_parent"] == crashed_epoch


def test_contract_drift_discards_unsettled_candidate_before_closing_epoch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A cached candidate cannot cross the evaluation-contract boundary."""
    from zicato.evolve import settlement as settlement_module
    from zicato.orchestrator import evolve_n_rounds

    workspace, rubric = _bootstrap_registered(tmp_path)
    _install_stub_adapter_factory(monkeypatch)
    _install_telemetry_stubs(
        monkeypatch,
        canned_loss_by_gen={"v0": 5.0, "v1": 1.0},
        canned_pass_by_gen={"v0": True, "v1": True},
    )

    real_commit = settlement_module.commit_field_settlement
    monkeypatch.setattr(
        settlement_module,
        "commit_field_settlement",
        lambda _root, _intent: (_ for _ in ()).throw(RuntimeError("before receipt")),
    )
    with pytest.raises(RuntimeError, match="before receipt"):
        asyncio.run(
            evolve_n_rounds(
                rounds=1,
                workspace_root=workspace,
                epoch_id=None,
                target_call_llm=target_call_llm,
                evaluation_call_llm=_make_aux(),
            )
        )
    crashed_epoch = current_epoch_id(workspace)
    assert crashed_epoch is not None

    rubric.write_text("# Rubric\n- Be careful.\n- Cite sources.\n")
    monkeypatch.setattr(settlement_module, "commit_field_settlement", real_commit)
    asyncio.run(
        evolve_n_rounds(
            rounds=1,
            workspace_root=workspace,
            epoch_id=None,
            target_call_llm=target_call_llm,
            evaluation_call_llm=_make_aux(),
        )
    )

    rolled_epoch = current_epoch_id(workspace)
    assert rolled_epoch is not None and rolled_epoch != crashed_epoch
    assert not (workspace / "epochs" / crashed_epoch / "generations" / "v1").exists()
    lineage = json.loads((workspace / "lineage.json").read_text(encoding="utf-8"))
    crashed = next(row for row in lineage["epochs"] if row["id"] == crashed_epoch)
    assert all(row["id"] != "v1" for row in crashed["generations"])
    rolled = next(row for row in lineage["epochs"] if row["id"] == rolled_epoch)
    assert rolled["v0_parent"] == crashed_epoch


def test_evolve_no_auto_epoch_errors_on_drift(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """With auto_epoch disabled, a drifted contract raises instead of rolling."""
    workspace, rubric = _bootstrap_registered(tmp_path)
    _install_stub_adapter_factory(monkeypatch)
    _install_telemetry_stubs(
        monkeypatch,
        canned_loss_by_gen={"v0": 5.0, "v1": 1.0},
        canned_pass_by_gen={"v0": True, "v1": True},
    )

    from zicato.orchestrator import evolve_n_rounds

    # First evolve still creates e0 even with auto_epoch off? No — with
    # auto_epoch off and no epoch, ensure_epoch_for_contract raises.
    with pytest.raises(FileNotFoundError):
        asyncio.run(
            evolve_n_rounds(
                rounds=1,
                workspace_root=workspace,
                epoch_id=None,
                target_call_llm=target_call_llm,
                evaluation_call_llm=_make_aux(),
                auto_epoch=False,
            )
        )

    # Create the first epoch with auto_epoch ON, then drift + retry with
    # auto_epoch OFF → drift error.
    asyncio.run(
        evolve_n_rounds(
            rounds=1,
            workspace_root=workspace,
            epoch_id=None,
            target_call_llm=target_call_llm,
            evaluation_call_llm=_make_aux(),
        )
    )
    rubric.write_text("# Rubric\n- totally different steering text\n")
    with pytest.raises(RuntimeError, match="drifted"):
        asyncio.run(
            evolve_n_rounds(
                rounds=1,
                workspace_root=workspace,
                epoch_id=None,
                target_call_llm=target_call_llm,
                evaluation_call_llm=_make_aux(),
                auto_epoch=False,
            )
        )
