"""A round consumes the selected epoch's captured evaluation inputs."""

from __future__ import annotations

import json
import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from tests import test_epoch_contract_carryover as contract_fixtures
from zicato.core.types import ScoringWeights
from zicato.epoch.contract import resolve_contract_inputs
from zicato.epoch.lifecycle import new_epoch


@pytest.fixture
def workspace(tmp_path):
    return contract_fixtures.workspace.__wrapped__(tmp_path)


def _selected(workspace):
    from zicato.epoch.execution import load_epoch_execution_contract

    inputs = resolve_contract_inputs(workspace)
    epoch = new_epoch(
        workspace,
        "retained",
        inputs.board_path,
        "## Forbidden edits\n- `immutable`\n",
        ScoringWeights(pass_weight=3),
        contract=inputs,
    )
    return load_epoch_execution_contract(workspace, epoch.id)


class PreparationObserved(Exception):
    """Stop before constructing a runtime or proposing a candidate."""


@pytest.mark.asyncio
async def test_round_uses_selected_epoch_and_retained_skills(workspace: Path, monkeypatch) -> None:
    skills = workspace.parent / "proposers" / "tuned" / "skills"
    skills.mkdir()
    skill = skills / "review.md"
    skill.write_text("Inspect the contract before editing.\n")
    inputs = resolve_contract_inputs(workspace)
    selected = new_epoch(
        workspace,
        "selected",
        inputs.board_path,
        "Selected brief",
        ScoringWeights(pass_weight=3),
        contract=inputs,
    )
    skill.write_text("Skip inspection and edit immediately.\n")
    inputs.board_path.write_text(
        '{"id":"other","kind":"single_turn","wall_clock_budget_seconds":60,"input":"other"}\n'
    )
    new_epoch(
        workspace,
        "marker",
        inputs.board_path,
        "Marker brief",
        ScoringWeights(pass_weight=9),
        contract=replace(inputs),
    )
    observed = {}

    def build_agent(spec, **kwargs):
        observed["skill"] = spec.skills[0].body
        return object()

    def inspect_inputs(board, weights):
        observed["entries"] = [entry.id for entry in board]
        observed["pass_weight"] = weights.pass_weight
        raise PreparationObserved

    monkeypatch.setattr("zicato.proposer.agent.build_proposer_agent", build_agent)
    monkeypatch.setattr("zicato.evolve.round_entry._declared_custom_judge_names", inspect_inputs)
    from zicato.evolve.invocation import validated_invocation
    from zicato.evolve.round_entry import _evolve_once

    # These assertions stop during preparation; executable entrypoint admission
    # is covered by the real adapter boundary tests.
    monkeypatch.setattr("zicato.check.require_workspace_valid", lambda *a, **k: None)
    async with validated_invocation(workspace, selected.id, "retained") as invocation:
        retained = invocation.execution_contract
        for body in ("Unrelated live proposal instruction.", "Another live instruction."):
            skill.write_text(body)
            with pytest.raises(PreparationObserved):
                await _evolve_once(invocation=invocation, epoch_id=selected.id)
            assert invocation.execution_contract is retained
            assert observed == {
                "skill": "Inspect the contract before editing.\n",
                "entries": ["e1"],
                "pass_weight": 3,
            }


def test_captured_views_survive_live_edits_and_defensive_decoding(workspace):
    from zicato.check.context import CheckContext
    from zicato.check.validators import proposal_runtime

    selected = _selected(workspace)
    configuration = json.loads((workspace / "config.json").read_text())
    configuration["adapter"]["factory"] = "missing:factory"
    configuration["contract"]["proposer_static_checks"] = []
    configuration["runtime"] = {}
    (workspace / "config.json").write_text(json.dumps(configuration))
    raw = selected.raw_scoring
    raw["pass_weight"] = 999
    adapter = selected.adapter_configuration
    adapter["adapter"]["args"].append("changed")
    external = selected.external_proposer
    external.workspace_config["contract"]["proposer_static_checks"].clear()
    with CheckContext(workspace, execution_contract=selected) as check:
        assert check.epoch_id == selected.epoch_id
        assert check.scoring.pass_weight == 3
        assert check.board[0].id == "e1"
        assert check.adapter_error is None
        assert list(proposal_runtime(check)) == []
    assert selected.brief.forbidden_ids == ("immutable",)
    assert selected.static_checks == ("ruff", "mypy")
    assert selected.adapter_configuration["adapter"]["args"] == ["carryover"]
    assert selected.external_proposer.static_checks == ("ruff", "mypy")
    selected.verify_implementation()


def test_missing_execution_capture_is_refused(workspace):
    from zicato.epoch.execution import load_epoch_execution_contract

    selected = _selected(workspace)
    record = workspace / "epochs" / selected.epoch_id / "execution.json"
    retained = record.with_suffix(".retained")
    record.rename(retained)
    with pytest.raises(FileNotFoundError, match="execution.json"):
        load_epoch_execution_contract(workspace, selected.epoch_id)
    assert retained.read_bytes() == selected.bindings_bytes


def test_retained_execution_is_independent_of_workspace_location(workspace):
    from zicato.epoch.execution import load_epoch_execution_contract

    selected = _selected(workspace)
    moved_project = workspace.parent.with_name(workspace.parent.name + "_moved")
    shutil.move(workspace.parent, moved_project)
    moved = moved_project / workspace.name
    loaded = load_epoch_execution_contract(moved, selected.epoch_id)
    assert loaded.contract_hash == selected.contract_hash
    assert loaded.board_bytes == selected.board_bytes
    assert loaded.external_proposer.workspace_root == moved


@pytest.mark.asyncio
async def test_selected_driver_and_operational_settings_reach_runtime_after_live_edits(
    workspace, monkeypatch
):
    import importlib

    from tests.test_driver_import_context import _driver, _package
    from zicato import runtime_factory
    from zicato.core.runtime_context import TelemetryEndpoints
    from zicato.core.settings import InvocationOverlay
    from zicato.evolve.invocation import validated_invocation
    from zicato.evolve.round_entry import _evolve_once

    for name, value in (("retained_driver", "retained"), ("live_driver", "live")):
        directory = workspace.parent / name
        _package(directory, value)
        _driver(directory)
    path = workspace / "config.json"
    authored = json.loads(path.read_text())
    authored["adapter"] = {
        "kind": "import",
        "factory": "fixed_driver:make_adapter",
        "import_roots": ["retained_driver"],
        "mutable_trees": ["retained_driver/candidate_target"],
    }
    authored["runtime"] = {"seed": 17, "parallelism": 2}
    path.write_text(json.dumps(authored))
    selected = _selected(workspace)
    authored["adapter"]["import_roots"] = ["live_driver"]
    path.write_text(json.dumps(authored))
    monkeypatch.setattr("zicato.check.require_workspace_valid", lambda *a, **k: None)
    monkeypatch.setattr("zicato.proposer.agent.build_proposer_agent", lambda *a, **k: object())
    real_factory = runtime_factory.make_runtime_config
    observed = []

    def inspect_runtime(*args, **kwargs):
        runtime = real_factory(*args, **kwargs)
        observed.append(runtime)
        assert importlib.import_module("fixed_driver").VALUE == "retained"
        raise PreparationObserved

    monkeypatch.setattr(runtime_factory, "make_runtime_config", inspect_runtime)

    async def target(system, user, model):
        return "target"

    async def evaluation(system, user, model):
        return "evaluation"

    overlay = InvocationOverlay.from_mapping({"runtime": {"seed": 29}})
    async with validated_invocation(
        workspace, selected.epoch_id, "settings", overlay=overlay
    ) as invocation:
        invocation.telemetry = TelemetryEndpoints("http://127.0.0.1:1234", "127.0.0.1:5678")
        retained = invocation.execution_contract
        authored["runtime"] = {"seed": 41, "parallelism": 7}
        path.write_text(json.dumps(authored))
        for _ in range(2):
            with pytest.raises(PreparationObserved):
                await _evolve_once(
                    invocation=invocation,
                    target_call_llm=target,
                    evaluation_call_llm=evaluation,
                )
            assert invocation.execution_contract is retained
    assert len(observed) == 2
    for runtime in observed:
        assert runtime.seed == 29 and runtime.parallelism == 2
        assert runtime.configuration is invocation.configuration
        assert runtime.configuration.sources["runtime.seed"] == "invocation"
        assert runtime.telemetry == invocation.telemetry
        assert runtime.driver_imports.roots == (workspace.parent / "retained_driver",)


def test_changed_external_identity_and_missing_identity_refuse_execution(workspace, monkeypatch):
    from zicato.epoch.execution import ExecutionContractError, load_epoch_execution_contract

    selected = _selected(workspace)
    monkeypatch.setattr(
        "zicato.proposer.external.resolve_external_spec",
        lambda *args, **kwargs: replace(selected.proposer_spec, external_identity_sha256="changed"),
    )
    with pytest.raises(ExecutionContractError, match="proposer implementation differs"):
        selected.verify_implementation()
    path = workspace / "epochs" / selected.epoch_id / "config.json"
    payload = json.loads(path.read_text())
    payload["contract_hash"] = None
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="contract_hash"):
        load_epoch_execution_contract(workspace, selected.epoch_id)


def test_load_probe_uses_captured_adapter_declaration(workspace, tmp_path):
    from zicato.proposer.validate import run_load_probe

    selected = _selected(workspace)
    configuration = json.loads((workspace / "config.json").read_text())
    configuration["adapter"]["factory"] = "missing:factory"
    (workspace / "config.json").write_text(json.dumps(configuration))
    errors, notes = run_load_probe(
        workspace,
        tmp_path,
        adapter_configuration_json=selected.external_proposer.adapter_configuration_json,
    )
    assert errors == []
    assert notes == []


@pytest.mark.parametrize("checks", [(), ("ruff",)])
def test_patch_validation_uses_explicit_retained_checks(tmp_path, monkeypatch, checks):
    from tests.test_proposer_validate import _build_snapshot, _replace, _validate
    from zicato.proposer.tool_context import ProposerToolContext

    snapshot, mutations = _build_snapshot(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "config.json").write_text(
        json.dumps({"contract": {"proposer_static_checks": ["mypy"]}})
    )
    observed = []
    monkeypatch.setattr(
        "zicato.proposer.validate.run_static_checks",
        lambda names, *args: (observed.append(names) or [], []),
    )
    monkeypatch.setattr("zicato.proposer.validate.run_load_probe", lambda *args: ([], []))
    context = ProposerToolContext(
        workspace_root=workspace,
        generation_root=snapshot,
        epoch_id="retained",
        mutations=mutations,
        static_checks=checks,
    )
    report = _validate(context, _replace("You are a terse assistant."))
    assert report["ok"]
    assert observed == ([checks] if checks else [])
