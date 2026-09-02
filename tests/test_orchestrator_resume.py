"""End-to-end crash-resume through the orchestrator's evolve loop.

These tests drive a real :func:`zicato.orchestrator.evolve_n_rounds`
against the same hermetic stubs ``tests/test_orchestrator.py`` uses
(stub adapter, stub telemetry, text-shim proposer), then prove the
conservative resume protocol's two load-bearing properties:

* **A tournament interrupted with completed board units resumes WITHOUT
  re-running them.** We stop one round immediately before its settlement
  receipt is written, instrument ``_run_single`` to count agent runs, then
  re-enter the loop. The completed units are cache HITs — ``_run_single`` is
  never called for them — and lineage / journal are not corrupted.

* **A clean workspace (nothing to resume) is byte-identical to today** —
  the resume hook returns the no-op plan and a fresh round runs exactly
  as it always did.

The classification table itself is unit-tested directly in
``tests/test_runtime_resume.py``; this file is the integration proof
that the orchestrator wiring actually re-enters the loop and hits the
cache.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

import zicato.tournament.runner as _runner_mod

# Capture the GENUINE loss serde at import time, before any test installs the
# orchestrator telemetry stubs (which shadow zicato.telemetry.reducer in
# sys.modules with a stub that has no working read/write).
from tests._orchestrator_harness import (
    bootstrap_workspace,
    harness_call_llm,
    install_stub_adapter_factory,
    install_telemetry_stubs,
    make_aux_responder,
    run_evolve_once,
)
from zicato.telemetry.reducer import read_loss_profile as _REAL_READ_LOSS
from zicato.telemetry.reducer import write_loss_profile as _REAL_WRITE_LOSS


def _wire_real_loss_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the stub reducer's loss read/write at the REAL implementations.

    ``install_telemetry_stubs`` installs a reducer stub whose
    ``read_loss_profile`` always raises and that has no
    ``write_loss_profile`` — so the per-unit ``loss.json`` cache is inert
    under the default orchestrator stubs. The resume test needs a *working*
    cache (that is the whole point), so we splice the genuine read/write
    serde (captured at module import, before the stub shadowed the module)
    onto the already-installed stub. ``reduce_loss`` stays stubbed (no
    worker subprocess / no goldfive).
    """
    import sys

    reducer_mod = sys.modules["zicato.telemetry.reducer"]
    monkeypatch.setattr(reducer_mod, "read_loss_profile", _REAL_READ_LOSS, raising=False)
    monkeypatch.setattr(reducer_mod, "write_loss_profile", _REAL_WRITE_LOSS, raising=False)


def _run_single_counter(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """Wrap the installed ``_run_single`` stub with a per-(gen,entry) tally.

    Returns the live counter dict (keys ``"{gen}/{entry}"``). The wrapper
    delegates to whatever ``_run_single`` the telemetry stubs installed, so
    the loss-profile shape is unchanged — only the call is counted.
    """
    counts: dict[str, int] = {}
    inner = _runner_mod._run_single

    async def _counting_run_single(*, generation: Any, entry: Any, **kwargs: Any) -> Any:
        key = f"{generation.id}/{entry.id}"
        counts[key] = counts.get(key, 0) + 1
        return await inner(generation=generation, entry=entry, **kwargs)

    monkeypatch.setattr(_runner_mod, "_run_single", _counting_run_single)
    return counts


def test_resume_reuses_completed_units_without_rerun(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An interrupted v1 tournament resumes; cached units are not re-run."""
    workspace, epoch_id = bootstrap_workspace(tmp_path)
    install_stub_adapter_factory(monkeypatch)
    install_telemetry_stubs(
        monkeypatch,
        canned_loss_by_gen={"v0": 2.0, "v1": 1.0},
        canned_pass_by_gen={"v0": True, "v1": True},
    )
    _wire_real_loss_cache(monkeypatch)

    # Stop after the completed board units land but before settlement records
    # its receipt. This is a reachable interruption state: the experiment and
    # loss cache exist, while outcome, lineage verdict, and champion marker do
    # not yet claim the round committed.
    import zicato.evolve.settlement as settlement_module

    real_commit = settlement_module.commit_field_settlement

    def _stop_before_receipt(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("injected crash before settlement receipt")

    monkeypatch.setattr(settlement_module, "commit_field_settlement", _stop_before_receipt)
    with pytest.raises(RuntimeError, match="injected crash before settlement receipt"):
        run_evolve_once(workspace, epoch_id, make_aux_responder([]))
    monkeypatch.setattr(settlement_module, "commit_field_settlement", real_commit)

    gens = workspace / "epochs" / epoch_id / "generations"
    v1_dir = gens / "v1"
    assert (v1_dir / "runs" / "entry_a" / "loss.json").is_file()
    assert (gens / "v0" / "runs" / "entry_a" / "loss.json").is_file()

    # An aux responder that RAISES if the proposer is ever consulted —
    # resume must reuse the persisted experiment, never re-propose.
    def _aux_must_not_propose(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("resume re-proposed instead of reusing persisted experiment")

    # --- Resume: re-enter the loop with the completed units on disk. ---
    counts = _run_single_counter(monkeypatch)
    from zicato.orchestrator import evolve_n_rounds

    outcomes = asyncio.run(
        evolve_n_rounds(
            rounds=1,
            workspace_root=workspace,
            epoch_id=epoch_id,
            harness_call_llm=harness_call_llm,
            auxiliary_call_llm=_aux_must_not_propose,
        )
    )

    # The resumed round produced an outcome for v1 (not a fresh v2).
    assert len(outcomes) == 1
    assert outcomes[0].proposed_generation_id == "v1"

    # The completed board units were cache HITS — _run_single never ran for
    # either side's already-cached entry.
    assert counts.get("v1/entry_a", 0) == 0, counts
    assert counts.get("v0/entry_a", 0) == 0, counts

    # v1 now carries a committed outcome again (the resumed round journaled).
    body = json.loads((v1_dir / "experiment.json").read_text())
    assert body["outcome"] is not None

    # Lineage is not corrupted: v1 appears exactly once. append_to_lineage
    # upserts by id, so even though the first round had recorded v1 too, a
    # correct resume leaves a single v1 node — never a duplicate.
    from zicato.epoch.lineage import load_lineage

    lineage_after = load_lineage(workspace)
    v1_records = [
        node
        for epoch in lineage_after["epochs"]
        if epoch["id"] == epoch_id
        for node in epoch["generations"]
        if node["id"] == "v1"
    ]
    assert len(v1_records) == 1, lineage_after
    # No v2 was minted — resume re-used v1 rather than advancing.
    assert not (workspace / "epochs" / epoch_id / "generations" / "v2").exists()


def test_clean_workspace_evolve_is_unchanged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """With nothing to resume, evolve runs a normal fresh round (no v-skip)."""
    workspace, epoch_id = bootstrap_workspace(tmp_path)
    install_stub_adapter_factory(monkeypatch)
    install_telemetry_stubs(
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
            harness_call_llm=harness_call_llm,
            auxiliary_call_llm=make_aux_responder([]),
        )
    )

    # A clean cold start mints v1 exactly as it always did.
    assert len(outcomes) == 1
    assert outcomes[0].parent_generation_id == "v0"
    assert outcomes[0].proposed_generation_id == "v1"
    assert (workspace / "epochs" / epoch_id / "generations" / "v1" / "experiment.json").exists()
