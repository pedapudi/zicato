"""Known decisions through the recommended proposal, screening, and evidence composition."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from tests._recommended_loop_support import CHOSEN_POLICIES, POLICY_PATH, bootstrap, run_round
from zicato.core.measurement import (
    MeasurementDraw,
    MeasurementPurpose,
    iter_measurement_attempts,
    measurement_artifact_path,
)
from zicato.core.runtime import RoundTokenLedger
from zicato.core.runtime_context import WorkerRuntimeContext
from zicato.core.workspace import field_tournament_path
from zicato.epoch.genstore import default_generation_store
from zicato.epoch.git_genstore import GitGenerationStore
from zicato.epoch.journal import read_experiment, read_journal
from zicato.epoch.lifecycle import load_epoch
from zicato.epoch.lineage import load_lineage
from zicato.epoch.round_log import RoundLog
from zicato.epoch.settlement_receipt import field_settlement_intent_path
from zicato.evolve.generation_phase import current_generation
from zicato.runtime.paths import active_runs_dir
from zicato.telemetry.reducer import read_loss_profile
from zicato.tournament.artifacts import artifact_paths
from zicato.tournament.records import read_field_tournament_record
from zicato.tournament.unit_cache import persisted_loss_slots

pytestmark = pytest.mark.integration


@pytest.fixture
def measured_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    started = time.perf_counter()
    workspace, epoch_id, configuration = bootstrap(tmp_path)
    report: dict[str, Any] = {
        "contract_hash": load_epoch(workspace, epoch_id).contract_hash,
        "configuration": configuration,
        "configuration_digest": hashlib.sha256(
            json.dumps(configuration, sort_keys=True).encode()
        ).hexdigest(),
        "setup_seconds": time.perf_counter() - started,
        "workers": [],
    }
    spawn = asyncio.create_subprocess_exec

    async def observe_spawn(*args: Any, **kwargs: Any) -> Any:
        process = await spawn(*args, **kwargs)
        if "zicato._tournament_worker" in args:
            payload = json.loads(Path(args[-1]).read_text())
            context = WorkerRuntimeContext.from_json(payload["runtime_context"])
            report["workers"].append(
                {
                    "pid": process.pid,
                    "generation": context.run.generation_id,
                    "entry": payload["entry"]["id"],
                    "measurement": payload["measurement"],
                }
            )
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", observe_spawn)
    execution_started = time.perf_counter()
    try:
        yield workspace, epoch_id, report
    finally:
        report["execution_seconds"] = time.perf_counter() - execution_started
        report["worker_launches"] = len(report["workers"])
        report["worker_launches_by_purpose"] = {
            purpose: sum(
                worker["measurement"]["purpose"] == purpose for worker in report["workers"]
            )
            for purpose in sorted(
                {worker["measurement"]["purpose"] for worker in report["workers"]}
            )
        }
        (workspace / "recommended-acceptance-report.json").write_text(json.dumps(report, indent=2))


def assert_source_identity(workspace: Path, epoch_id: str, generation: str, tmp_path: Path) -> None:
    store = default_generation_store(workspace)
    experiment = read_experiment(workspace, epoch_id, generation)
    rebuilt = store.derive_scratch(epoch_id, "v0", experiment.patches, tmp_path / generation)
    expected = hashlib.sha256((rebuilt / POLICY_PATH).read_bytes()).hexdigest()
    assert (
        hashlib.sha256(store.read_file(epoch_id, generation, str(POLICY_PATH))).hexdigest()
        == expected
    )
    mounted = store.materialize_snapshot(epoch_id, generation)
    assert hashlib.sha256((mounted / POLICY_PATH).read_bytes()).hexdigest() == expected
    assert (
        str(experiment.patches[0].new_content).strip().strip("\"'") == CHOSEN_POLICIES[generation]
    )
    records = workspace / "epochs" / epoch_id / "generations" / generation / "runs"
    measured = []
    for run in records.iterdir():
        for _, loss_path in persisted_loss_slots(run):
            profile = read_loss_profile(loss_path)
            assert profile.measurement is not None
            assert profile.measurement.purpose in {
                MeasurementPurpose.TOURNAMENT,
                MeasurementPurpose.CONFIRMATION,
            }
            source = json.loads(
                (artifact_paths(loss_path)[0] / "evaluated-source.json").read_text()
            )
            assert source["source_digest"] == expected
            measured.append(profile)
    assert measured


def assert_independent_confirmation(evidence: dict[str, Any]) -> None:
    """Selection matchups retain their audit without adding inferential samples."""
    ordinary = [attempt for attempt in evidence["attempts"] if attempt["budget_spent"] == 0]
    assert ordinary
    assert all(attempt["eligibility"] == "selection_only" for attempt in ordinary)
    admitted = [attempt for attempt in evidence["attempts"] if attempt["eligibility"] == "eligible"]
    assert admitted
    assert all(attempt["budget_spent"] == 1 for attempt in admitted)
    assert all(
        attempt["measurement_draw"]["purpose"] == str(MeasurementPurpose.CONFIRMATION)
        for attempt in admitted
    )
    assert all(attempt["measurement_draw"]["base_seed"] == 17 for attempt in admitted)
    assert len({attempt["measurement_draw"]["draw"] for attempt in admitted}) == len(admitted)
    assert evidence["n_duels"] == len(admitted)
    assert evidence["confirmation_status"] == "satisfied"
    assert evidence["difference"]["comparison_count"] == 4 * (32 + 1)


@pytest.mark.asyncio
@pytest.mark.slow
async def test_recommended_complete_round(measured_workspace: Any, tmp_path: Path) -> None:
    workspace, epoch_id, report = measured_workspace
    outcomes = await run_round(workspace, epoch_id)
    report["outcomes"] = [str(outcome) for outcome in outcomes]
    assert len(outcomes) == 1
    assert outcomes[0].tournament_decision == "promoted"
    assert outcomes[0].parent_scalar == pytest.approx(3.5)
    assert outcomes[0].child_scalar == pytest.approx(0.0)
    assert current_generation(workspace, epoch_id) == "v1"
    accepted = read_experiment(workspace, epoch_id, "v1").outcome
    assert accepted is not None and accepted.evidence is not None
    assert_independent_confirmation(accepted.evidence)
    report["accepted_evidence"] = accepted.evidence
    events = RoundLog(workspace, epoch_id, 0).read()
    sampled = [event for event in events if event.type == "candidate_sampled"]
    screened = [event for event in events if event.type == "candidate_screened"]
    selected = [event for event in events if event.type == "critique_selected"]
    assert len(sampled) == 12
    assert len(screened) == 12
    assert len(selected) == 4
    assert all(event.payload["index"] == 0 for event in selected)
    assert sum(event.payload["vetoed"] for event in screened) == 8
    for generation in CHOSEN_POLICIES:
        assert_source_identity(workspace, epoch_id, generation, tmp_path / "reconstructed")
        outcome = read_experiment(workspace, epoch_id, generation).outcome
        assert outcome is not None
        assert outcome.tournament_decision == ("promoted" if generation == "v1" else "rejected")
    workers = report["workers"]
    purposes = {worker["measurement"]["purpose"] for worker in workers}
    assert {
        str(MeasurementPurpose.SCREEN),
        str(MeasurementPurpose.TOURNAMENT),
        str(MeasurementPurpose.CONFIRMATION),
    } <= purposes
    coordinates = {
        (worker["generation"], worker["entry"], json.dumps(worker["measurement"], sort_keys=True))
        for worker in workers
        if worker["measurement"]["purpose"] != str(MeasurementPurpose.SCREEN)
    }
    assert len(coordinates) == sum(
        worker["measurement"]["purpose"] != str(MeasurementPurpose.SCREEN) for worker in workers
    )
    for generation in CHOSEN_POLICIES:
        assert {
            worker["measurement"]["draw"]
            for worker in workers
            if worker["generation"] == generation
            and worker["measurement"]["purpose"] == str(MeasurementPurpose.TOURNAMENT)
        } == {0, 1}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exhaust_budget",
    [
        pytest.param(False, id="measured", marks=pytest.mark.slow),
        pytest.param(True, id="unmeasured"),
    ],
)
async def test_partial_application_preserves_confirmation_requirements(
    measured_workspace: Any, monkeypatch: pytest.MonkeyPatch, exhaust_budget: bool, tmp_path: Path
) -> None:
    from zicato.evolve import field
    from zicato.evolve.field_execution import execute_field_tournament

    workspace, epoch_id, report = measured_workspace
    derive = GitGenerationStore.derive_generation
    failed_applications = []

    def fail_fourth(
        self: Any,
        epoch_id: str,
        parent_generation_id: str,
        child_generation_id: str,
        patches: Any,
        *,
        enumeration_roots: Any = None,
    ) -> Path:
        if child_generation_id == "v4":
            failed_applications.append(child_generation_id)
            raise ValueError("controlled candidate application failure")
        return derive(
            self,
            epoch_id,
            parent_generation_id,
            child_generation_id,
            patches,
            enumeration_roots=enumeration_roots,
        )

    monkeypatch.setattr(GitGenerationStore, "derive_generation", fail_fourth)
    execute = execute_field_tournament

    async def exhaust_before_measurement(field_round: Any, candidates: Any) -> Any:
        report["planned_candidates"] = field_round.field_size
        if not exhaust_budget:
            return await execute(field_round, candidates)
        ledger = RoundTokenLedger(1)
        ledger.add(1)
        report["budget_spent_before_tournament"] = ledger.spent
        config = replace(field_round.prepared.config, token_ledger=ledger)
        return await execute(
            replace(field_round, prepared=replace(field_round.prepared, config=config)),
            candidates,
        )

    monkeypatch.setattr(field, "execute_field_tournament", exhaust_before_measurement)
    outcomes = await run_round(workspace, epoch_id)
    report["outcomes"] = [str(outcome) for outcome in outcomes]
    assert failed_applications
    assert report["planned_candidates"] == 4
    tournament = read_field_tournament_record(
        field_tournament_path(workspace, epoch_id, "v1")
    ).to_dict()
    assert {row["generation_id"]: row["status"] for row in tournament["field_status"]} == {
        "v1": "applied",
        "v2": "applied",
        "v3": "applied",
        "v4": "rejected",
    }
    report["field_status"] = tournament["field_status"]
    if not exhaust_budget:
        assert len(outcomes) == 1 and outcomes[0].tournament_decision == "promoted"
        assert current_generation(workspace, epoch_id) == "v1"
        outcome = read_experiment(workspace, epoch_id, "v1").outcome
        assert outcome is not None and outcome.evidence is not None
        assert_independent_confirmation(outcome.evidence)
        report["accepted_evidence"] = outcome.evidence
        for generation in ("v1", "v2", "v3"):
            assert_source_identity(workspace, epoch_id, generation, tmp_path / "reconstructed")
            settled = read_experiment(workspace, epoch_id, generation).outcome
            assert settled is not None
            assert settled.tournament_decision == ("promoted" if generation == "v1" else "rejected")
        return
    assert len(outcomes) == 1 and outcomes[0].tournament_decision == "deferred"
    assert tournament["decision"] == "deferred"
    assert current_generation(workspace, epoch_id) == "v0"
    for generation in ("v1", "v2", "v3"):
        outcome = read_experiment(workspace, epoch_id, generation).outcome
        assert outcome is not None
        assert outcome.tournament_decision != "promoted"
        records = workspace / "epochs" / epoch_id / "generations" / generation / "runs"
        attempts = [
            attempt
            for run in records.iterdir()
            for replicate in range(2)
            for attempt in iter_measurement_attempts(
                measurement_artifact_path(
                    run,
                    "loss",
                    MeasurementDraw(MeasurementPurpose.TOURNAMENT, replicate),
                    base_seed=17,
                )
            )
        ]
        assert attempts
        assert all(read_loss_profile(path).execution_started is False for path in attempts)
        assert not any(persisted_loss_slots(run) for run in records.iterdir())
    assert not any(
        worker["generation"] != "v0"
        and worker["measurement"]["purpose"] in {"tournament", "evidence_confirmation"}
        for worker in report["workers"]
    )


@pytest.mark.asyncio
@pytest.mark.slow
async def test_interrupted_recommended_field_recovers(
    measured_workspace: Any, tmp_path: Path
) -> None:
    workspace, epoch_id, report = measured_workspace
    control = workspace / "acceptance-control.json"
    control.write_text(json.dumps({"pause_generation": "v1"}))
    signal = workspace / "acceptance-worker-started.json"
    task = asyncio.create_task(run_round(workspace, epoch_id))
    try:
        async with asyncio.timeout(45):
            while not signal.exists():
                if task.done():
                    await task
                    pytest.fail("the round settled before its controlled worker started")
                await asyncio.sleep(0.02)
        started = json.loads(signal.read_text())
        report["interrupted_worker"] = started
        assert current_generation(workspace, epoch_id) == "v0"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        for worker in report["workers"]:
            with pytest.raises(ProcessLookupError):
                os.kill(worker["pid"], 0)
        assert not list(active_runs_dir(workspace).glob("*.json"))
        assert current_generation(workspace, epoch_id) == "v0"
        assert not field_settlement_intent_path(workspace, epoch_id, 0).exists()
        control.write_text("{}")
        outcomes = await run_round(workspace, epoch_id)
        report["outcomes"] = [str(outcome) for outcome in outcomes]
        assert len(outcomes) == 1 and outcomes[0].tournament_decision == "promoted"
        assert current_generation(workspace, epoch_id) == "v1"
        accepted = read_experiment(workspace, epoch_id, "v1").outcome
        assert accepted is not None and accepted.evidence is not None
        assert_independent_confirmation(accepted.evidence)
        report["accepted_evidence"] = accepted.evidence
        for generation in CHOSEN_POLICIES:
            assert_source_identity(workspace, epoch_id, generation, tmp_path / "reconstructed")
            assert read_experiment(workspace, epoch_id, generation).outcome is not None
        receipts = list((workspace / "epochs" / epoch_id).rglob("field_settlement.json"))
        assert len(receipts) == 1
        receipt = json.loads(receipts[0].read_text())
        assert receipt["state"] == "committed"
        journal = read_journal(workspace, epoch_id)
        for generation in CHOSEN_POLICIES:
            assert journal.count(f"## {generation} — ") == 1
        lineage_epoch = next(
            row for row in load_lineage(workspace).to_dict()["epochs"] if row["id"] == epoch_id
        )
        nodes = lineage_epoch["generations"]
        assert len(nodes) == 5
        assert {node["id"] for node in nodes} == {"v0", *CHOSEN_POLICIES}
        for node in nodes:
            if node["id"] != "v0":
                assert node["parent_id"] == "v0"
                assert node["promoted"] is (node["id"] == "v1")
        report["settlement_id"] = receipt["settlement_id"]
    finally:
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
