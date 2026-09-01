"""Scripted-orchestrator harness: the stubs an evolve-loop test runs against.

Twenty-two test modules drive :mod:`zicato.orchestrator` end to end with
every external dependency replaced — the two LLM callables, the harness
adapter, the telemetry sink and the loss reducer — so a round completes
with no model traffic, no google-adk and no goldfive. The stubs that make
that possible live here rather than inside any one test module, because
they are shared scaffolding and not the subject of any single suite.

Three of the stubs carry decisions that are easy to undo by accident:

* :func:`bootstrap_workspace` pins the DIRECTORY generation-source
  backend, because it hand-builds the ``epochs/*/generations/v0/snapshot``
  layout and the git default would look for tags this fixture never
  writes. It also pins the deterministic contract knobs from
  :mod:`tests._contract_pins`, since these suites script a single propose
  per round and a single paired run per duel.
* :func:`install_stub_adapter_factory` leaves ``make_adapter_from_spec``
  real. That is how a worker subprocess and the pre-spend gate's probe
  rebuild the adapter, so stubbing it would hide the reconstruction path
  the tests mean to exercise.
* :func:`install_telemetry_stubs` grafts the real
  ``split_judge_attributed_kind`` onto the stubbed reducer. The health
  assessment imports it, and its absence was swallowed by a best-effort
  ``try``/``except`` — so without the graft no round wrote a health record
  and nothing failed to say so.
"""

from __future__ import annotations

import asyncio
import json
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from tests._contract_pins import deterministic_weights
from zicato.core.types import (
    BoardEntry,
    DriftCount,
    ExpectationResult,
    LossProfile,
)
from zicato.epoch.lifecycle import new_epoch


async def harness_call_llm(system: str, user: str, model: str) -> str:
    del system, user, model
    return ""


def make_aux_responder(responses: list[str]) -> Any:
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


def bootstrap_workspace(tmp_path: Path) -> tuple[Path, str]:
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
                # This bootstrap hand-builds the directory-backend snapshot
                # layout (epochs/.../generations/v0/snapshot/), so it pins the
                # directory backend explicitly — the git default reads its
                # generations from git tags this fixture never writes.
                "generation_source_backend": "directory",
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
    brief_src = tmp_path / "brief.md"
    brief_src.write_text("# Proposer brief\n- Be careful.\n")

    cfg = new_epoch(
        workspace,
        name="alpha",
        board_source=board_src,
        brief_source=brief_src,
        # Pinned deterministic knobs (replicates 1, evidence gate off,
        # single-sample proposer): these tests drive SCRIPTED proposers and
        # stub reducers whose call sequences assume the historical
        # single-run duel. See tests/_contract_pins.py.
        weights=deterministic_weights(promote_margin=0.01),
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
        "\n"
        "\n"
        "def greet(name):\n"
        '    # zicato:mutable:code id="greet_logic"\n'
        '    return GREETING + " " + name\n'
        "    # zicato:mutable:end\n"
    )
    return workspace, cfg.id


def install_stub_adapter_factory(
    monkeypatch: pytest.MonkeyPatch,
    *,
    bypass_workspace_gate: bool = True,
) -> None:
    """Replace zicato.adapter_factory with one that dispatches kind='stub'.

    ``bypass_workspace_gate`` patches out the pre-spend gate
    (:mod:`zicato.check`). Most suites here drive the loop against a
    deliberately minimal fixture workspace that has no reason to satisfy
    a real workspace's invariants, and want to exercise orchestration
    below the gate. Pass ``False`` when the fixture workspace is meant to
    be valid and the gate is part of what is under test; the adapter
    itself is reconstructible either way, and
    :func:`~tests._stub_adapter.stub_adapter_pythonpath` is what a
    subprocess needs to reach it.
    """
    from tests._stub_adapter import make_stub_adapter
    from zicato.adapter_factory import make_adapter_from_spec

    fake_factory = types.ModuleType("zicato.adapter_factory")

    def make_adapter_from_config(workspace_config: dict[str, Any]) -> Any:
        del workspace_config
        return make_stub_adapter()

    fake_factory.make_adapter_from_config = make_adapter_from_config  # type: ignore[attr-defined]
    # The spec-shaped builder is not stubbed: it is how a worker (and the
    # gate's probe) rebuilds the adapter, and it must stay the real one.
    fake_factory.make_adapter_from_spec = make_adapter_from_spec  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "zicato.adapter_factory", fake_factory)
    # Re-bind on the zicato namespace so `from zicato import adapter_factory`
    # picks up the stub.
    import zicato
    import zicato.check

    monkeypatch.setattr(zicato, "adapter_factory", fake_factory, raising=False)
    if bypass_workspace_gate:
        monkeypatch.setattr(zicato.check, "require_workspace_valid", lambda *a, **k: None)


def install_telemetry_stubs(
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
        replicate_index: int = 0,
    ) -> Path:
        del epoch_id, generation_id, entry_id, replicate_index
        return workspace_root / "events.jsonl"

    sink_mod.make_run_sink_path = make_run_sink_path  # type: ignore[attr-defined]

    reducer_mod = types.ModuleType("zicato.telemetry.reducer")

    # The real, dependency-light judge-attribution parse — zicato.health's
    # detect_dead_judge imports this from zicato.telemetry.reducer. Grafting
    # it onto the stub (rather than leaving it absent) keeps the real health
    # assessment running for every orchestrator test instead of raising
    # ImportError inside orchestrator._assess_and_persist_loop_health's
    # best-effort try/except, which used to swallow the whole health check
    # silently — no round ever wrote health/round_*.json under this stub.
    from zicato.telemetry.reducer import (
        split_judge_attributed_kind as _real_split_judge_attributed_kind,
    )

    reducer_mod.split_judge_attributed_kind = (  # type: ignore[attr-defined]
        _real_split_judge_attributed_kind
    )

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

    # Stub the harmonograf supervisor so the orchestrator's evolve loop
    # takes its failure-isolation path (empty URL, no-op handle) instead
    # of launching a real in-process harmonograf for every orchestrator
    # test. start_harmonograf returns a no-op handle; the rest of the
    # path treats that as "JSONL-only telemetry", exactly like the
    # pre-#202 behaviour these tests were written against.
    supervisor_mod = types.ModuleType("zicato.telemetry.harmonograf_supervisor")

    class _StubHandle:
        url: str = ""

        def shutdown(self) -> None:
            return None

    def _stub_start_harmonograf(*_args: Any, **_kwargs: Any) -> _StubHandle:
        return _StubHandle()

    supervisor_mod.start_harmonograf = _stub_start_harmonograf  # type: ignore[attr-defined]
    supervisor_mod.HarmonografHandle = _StubHandle  # type: ignore[attr-defined]

    # The real, dependency-light meta_loop module — the structural-span call
    # sites (runner / scheduler / best-of-N) import ``meta_span`` from it. It is
    # a no-op here (no ambient emitter is bound when evolve_once is driven
    # directly), so registering the real module preserves behaviour while
    # keeping the shadow package importable.
    import zicato.telemetry.meta_loop as meta_loop_mod

    telemetry_pkg = types.ModuleType("zicato.telemetry")
    telemetry_pkg.sink = sink_mod  # type: ignore[attr-defined]
    telemetry_pkg.reducer = reducer_mod  # type: ignore[attr-defined]
    telemetry_pkg.harmonograf_supervisor = supervisor_mod  # type: ignore[attr-defined]
    telemetry_pkg.meta_loop = meta_loop_mod  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "zicato.telemetry", telemetry_pkg)
    monkeypatch.setitem(sys.modules, "zicato.telemetry.sink", sink_mod)
    monkeypatch.setitem(sys.modules, "zicato.telemetry.reducer", reducer_mod)
    monkeypatch.setitem(sys.modules, "zicato.telemetry.harmonograf_supervisor", supervisor_mod)
    monkeypatch.setitem(sys.modules, "zicato.telemetry.meta_loop", meta_loop_mod)

    # Since the L3 subprocess-isolation refactor each tournament run
    # spawns a worker subprocess, which cannot see these sys.modules
    # stubs (it runs in a separate interpreter). The orchestrator tests
    # only care about the *evolve-loop* logic above the per-run mechanism,
    # so we stub ``runner._run_single`` to return the same canned
    # LossProfile the in-process reduce_loss stub would have produced.
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
        del adapter, weights, config, side, match_id
        expectation_result = (
            ExpectationResult(kind="predicate", passed=True)
            if entry.expectation is not None
            else None
        )
        # Mirror the clean-exit path of the real _run_single: it folds the
        # finished run into the live SQLite index. The index-wiring tests
        # assert on this; the real subprocess _run_single does it after
        # reading the worker's loss.json.
        _runner_mod._ingest_run_into_index(workspace_root, epoch_id, generation.id, entry.id)
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


def valid_proposer_response() -> str:
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


def run_evolve_once(
    workspace: Path,
    epoch_id: str,
    auxiliary_call_llm: Any,
    **evolve_kwargs: Any,
) -> Any:
    """Run one scripted evolve round and return its outcome.

    Every scripted caller drives the round the same way: the same harness
    callable, the workspace and epoch the bootstrap just produced, and a
    scripted auxiliary callable that stands in for the proposer. Only the
    scripted responses differ, so only they are passed.

    ``evolve_once`` is imported inside this function rather than at module
    scope, which is how the callers did it. It matters: a caller has
    normally just replaced ``zicato.adapter_factory`` and the telemetry
    modules in ``sys.modules``, and importing the orchestrator before that
    would bind the real ones.

    Extra keyword arguments go straight to ``evolve_once``, for the tests
    whose subject is one of its other parameters.
    """
    from zicato.orchestrator import evolve_once  # noqa: PLC0415

    return asyncio.run(
        evolve_once(
            workspace_root=workspace,
            epoch_id=epoch_id,
            harness_call_llm=harness_call_llm,
            auxiliary_call_llm=auxiliary_call_llm,
            **evolve_kwargs,
        )
    )
