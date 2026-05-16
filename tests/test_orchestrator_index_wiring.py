"""Tests for the live SQLite-index dual-write wiring in the orchestrator.

The evolve loop keeps the analytical index current as it runs: each
run's ``loss.json`` triggers ``zicato.index.ingest.ingest_run`` and each
``experiment.json`` write triggers ``ingest_experiment``. These tests
mock the (parallel-landing) ``zicato.index`` sibling and assert the
orchestrator calls into it — and that an index-side failure never aborts
the round.

Everything is stub-driven: no goldfive, no real LLM. The workspace /
adapter / telemetry stubs are shared with :mod:`tests.test_orchestrator`.
"""

from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from tests.test_orchestrator import (
    _bootstrap_workspace,
    _harness_call_llm,
    _install_stub_adapter_factory,
    _install_telemetry_stubs,
    _make_aux_responder,
    _valid_proposer_response,
)

# ---------------------------------------------------------------------------
# Fake zicato.index.ingest sibling
# ---------------------------------------------------------------------------


def _install_fake_index(
    monkeypatch: pytest.MonkeyPatch,
    *,
    run_calls: list[tuple[Any, ...]],
    experiment_calls: list[tuple[Any, ...]],
    raise_on_run: bool = False,
    raise_on_experiment: bool = False,
) -> None:
    """Install a fake ``zicato.index`` package recording every ingest call.

    ``run_calls`` / ``experiment_calls`` are appended to as the
    orchestrator and runner call ``ingest_run`` / ``ingest_experiment``.
    The ``raise_on_*`` flags make the corresponding ingest raise so the
    test can assert the round survives an index-side failure.
    """
    ingest_mod = types.ModuleType("zicato.index.ingest")

    def ingest_run(
        workspace_root: Path,
        db_path: Path,
        epoch_id: str,
        generation_id: str,
        entry_id: str,
    ) -> None:
        run_calls.append((workspace_root, db_path, epoch_id, generation_id, entry_id))
        if raise_on_run:
            raise RuntimeError("simulated index ingest_run failure")

    def ingest_experiment(
        workspace_root: Path,
        db_path: Path,
        epoch_id: str,
        generation_id: str,
    ) -> None:
        experiment_calls.append((workspace_root, db_path, epoch_id, generation_id))
        if raise_on_experiment:
            raise RuntimeError("simulated index ingest_experiment failure")

    def rebuild_index(workspace_root: Path, db_path: Path | None = None) -> None:
        del workspace_root, db_path

    ingest_mod.ingest_run = ingest_run  # type: ignore[attr-defined]
    ingest_mod.ingest_experiment = ingest_experiment  # type: ignore[attr-defined]
    ingest_mod.rebuild_index = rebuild_index  # type: ignore[attr-defined]

    index_pkg = types.ModuleType("zicato.index")
    index_pkg.ingest = ingest_mod  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "zicato.index", index_pkg)
    monkeypatch.setitem(sys.modules, "zicato.index.ingest", ingest_mod)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_evolve_once_triggers_index_ingest(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A round calls ingest_run per run and ingest_experiment for the child."""
    workspace, epoch_id = _bootstrap_workspace(tmp_path)
    _install_stub_adapter_factory(monkeypatch)
    _install_telemetry_stubs(
        monkeypatch,
        canned_loss_by_gen={"v0": 2.0, "v1": 1.0},
        canned_pass_by_gen={"v0": True, "v1": True},
    )

    run_calls: list[tuple[Any, ...]] = []
    experiment_calls: list[tuple[Any, ...]] = []
    _install_fake_index(monkeypatch, run_calls=run_calls, experiment_calls=experiment_calls)

    from zicato.orchestrator import evolve_once

    asyncio.run(
        evolve_once(
            workspace_root=workspace,
            epoch_id=epoch_id,
            harness_call_llm=_harness_call_llm,
            auxiliary_call_llm=_make_aux_responder([_valid_proposer_response()]),
        )
    )

    # Full A/B tournament runs the single board entry under both v0 and
    # v1 → two ingest_run calls.
    ingested_runs = {(gen, entry) for _, _, _, gen, entry in run_calls}
    assert ("v0", "entry_a") in ingested_runs
    assert ("v1", "entry_a") in ingested_runs

    # The index db path is the .zicato/index.db convention.
    for _, db_path, _, _, _ in run_calls:
        assert db_path == workspace / "index.db"

    # ingest_experiment is called for the child generation (at least
    # once — proposer-side write plus the post-tournament outcome write).
    ingested_experiments = {gen for _, _, _, gen in experiment_calls}
    assert "v1" in ingested_experiments
    for _, db_path, eid, _ in experiment_calls:
        assert db_path == workspace / "index.db"
        assert eid == epoch_id


def test_evolve_once_survives_index_ingest_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An index-side failure is swallowed; the round still completes."""
    workspace, epoch_id = _bootstrap_workspace(tmp_path)
    _install_stub_adapter_factory(monkeypatch)
    _install_telemetry_stubs(
        monkeypatch,
        canned_loss_by_gen={"v0": 2.0, "v1": 1.0},
        canned_pass_by_gen={"v0": True, "v1": True},
    )

    run_calls: list[tuple[Any, ...]] = []
    experiment_calls: list[tuple[Any, ...]] = []
    _install_fake_index(
        monkeypatch,
        run_calls=run_calls,
        experiment_calls=experiment_calls,
        raise_on_run=True,
        raise_on_experiment=True,
    )

    from zicato.orchestrator import evolve_once

    # The round must NOT raise even though every ingest call throws.
    outcome = asyncio.run(
        evolve_once(
            workspace_root=workspace,
            epoch_id=epoch_id,
            harness_call_llm=_harness_call_llm,
            auxiliary_call_llm=_make_aux_responder([_valid_proposer_response()]),
        )
    )

    assert outcome.tournament_decision == "promoted"
    # The ingest calls were still attempted (they raised internally).
    assert run_calls, "ingest_run should have been attempted"
    assert experiment_calls, "ingest_experiment should have been attempted"


def test_evolve_once_runs_without_index_sibling(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """With no zicato.index sibling installed the round still completes.

    The dual-write is best-effort; an ImportError is caught and the loop
    runs index-free (``zicato reindex`` can rebuild later).
    """
    workspace, epoch_id = _bootstrap_workspace(tmp_path)
    _install_stub_adapter_factory(monkeypatch)
    _install_telemetry_stubs(
        monkeypatch,
        canned_loss_by_gen={"v0": 2.0, "v1": 1.0},
        canned_pass_by_gen={"v0": True, "v1": True},
    )

    # Ensure no fake index module lingers from another test.
    monkeypatch.delitem(sys.modules, "zicato.index", raising=False)
    monkeypatch.delitem(sys.modules, "zicato.index.ingest", raising=False)

    # Make any import of zicato.index.ingest fail like a missing sibling.
    real_import = __import__

    def _blocking_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "zicato.index.ingest" or name.startswith("zicato.index."):
            raise ImportError(f"no module named {name!r} (simulated)")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", _blocking_import)

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
