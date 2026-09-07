"""Frozen execution roles bind the settings used by tournament workers."""

import asyncio
import json
from copy import deepcopy
from dataclasses import replace

import pytest

from tests.test_epoch_contract import _write_contract
from zicato.epoch.contract import compute_contract_hash, resolve_contract_inputs


async def alternate_call_llm(system: str, user: str, model: str) -> str:
    return "alternative"


def _configured_inputs(tmp_path, config):
    _write_contract(tmp_path)
    workspace = tmp_path / ".zicato"
    workspace.mkdir(exist_ok=True)
    return resolve_contract_inputs(workspace, workspace_config=config)


@pytest.mark.parametrize("role", ["target", "evaluation", "judge", "user_emulator"])
def test_changed_execution_callable_changes_the_epoch_contract(tmp_path, role):
    config = {
        "models": {
            "engines": {
                "target": {"call_llm": "tests._subprocess_worker_support:target_call_llm"},
                "evaluation": {"call_llm": "tests._subprocess_worker_support:evaluation_call_llm"},
                "alternative": {
                    "call_llm": "tests.test_execution_role_identity:alternate_call_llm"
                },
            },
            "roles": {},
        }
    }
    original = compute_contract_hash(_configured_inputs(tmp_path, config))
    changed = deepcopy(config)
    changed["models"]["roles"][role] = "alternative"
    assert compute_contract_hash(_configured_inputs(tmp_path, changed)) != original


def test_equivalent_role_inheritance_has_one_contract_identity(tmp_path):
    sparse = {
        "models": {
            "engines": {
                "target": {"call_llm": "tests._subprocess_worker_support:target_call_llm"},
                "evaluation": {"call_llm": "tests._subprocess_worker_support:evaluation_call_llm"},
            }
        }
    }
    expanded = deepcopy(sparse)
    expanded["models"]["roles"] = {
        "target": "target",
        "evaluation": "evaluation",
        "judge": "evaluation",
        "user_emulator": "evaluation",
        "adjudicator": "evaluation",
        "proposer": "evaluation",
        "proposer_generate": "evaluation",
        "proposer_review": "evaluation",
    }
    assert compute_contract_hash(_configured_inputs(tmp_path, sparse)) == compute_contract_hash(
        _configured_inputs(tmp_path, expanded)
    )


def _standalone(tmp_path):
    from tests._subprocess_worker_support import make_stub_adapter
    from zicato.core import ScoringWeights
    from zicato.epoch.lifecycle import new_epoch
    from zicato.models_config import execution_roles_for_runtime
    from zicato.runtime_factory import make_runtime_config

    authored = {
        "runtime": {
            "target_call_llm": "tests._subprocess_worker_support:target_call_llm",
            "evaluation_call_llm": "tests._subprocess_worker_support:evaluation_call_llm",
        }
    }
    inputs = _configured_inputs(tmp_path, authored)
    workspace = tmp_path / ".zicato"
    runtime = make_runtime_config(authored, workspace_root=workspace)
    inputs = replace(inputs, execution_roles=execution_roles_for_runtime(runtime))
    epoch = new_epoch(
        workspace,
        "standalone",
        inputs.board_path,
        inputs.brief_path,
        ScoringWeights(),
        contract=inputs,
    )
    return workspace, epoch, runtime, make_stub_adapter()


def test_execution_role_mapping_order_does_not_change_contract(tmp_path):
    workspace, epoch, runtime, _adapter = _standalone(tmp_path)
    from zicato.epoch.execution import load_epoch_execution_contract

    selected = load_epoch_execution_contract(workspace, epoch.id, workspace_config={})
    inputs = selected._inputs()
    roles = json.loads(inputs.execution_roles)
    reordered = json.dumps(dict(reversed(tuple(roles.items())))).encode()
    assert compute_contract_hash(inputs) == compute_contract_hash(
        replace(inputs, execution_roles=reordered)
    )


@pytest.mark.parametrize("change", ["runtime", "missing_epoch"])
def test_durable_tournament_refuses_unprepared_or_conflicting_runtime(
    tmp_path, monkeypatch, change
):
    from zicato.core import Generation
    from zicato.epoch.execution import load_epoch_execution_contract
    from zicato.tournament.runner import run_tournament

    workspace, epoch, runtime, adapter = _standalone(tmp_path)
    selected = load_epoch_execution_contract(workspace, epoch.id, workspace_config={})
    runtime = (
        replace(runtime, target_call_llm=alternate_call_llm) if change == "runtime" else runtime
    )
    epoch_id = epoch.id if change == "runtime" else "unprepared"
    generation = Generation(
        id="v0", epoch_id=epoch_id, parent_id=None, snapshot_root=tmp_path / "source", created_at=""
    )

    def forbidden_lookup(**kwargs):
        pytest.fail("cache lookup happened before contract admission")

    monkeypatch.setattr("zicato.tournament.scheduling._resolve_cached_unit", forbidden_lookup)
    with pytest.raises(ValueError, match="prepare an epoch"):
        asyncio.run(
            run_tournament(
                adapter=adapter,
                parent_gen=generation,
                child_gen=replace(generation, id="v1"),
                board=selected.board_with_meta[0],
                weights=selected.scoring,
                config=runtime,
                workspace_root=workspace,
                epoch_id=epoch_id,
                force_fresh=False,
            )
        )


def test_prepared_standalone_runtime_uses_real_workers_then_reuses_their_measurements(tmp_path):
    from zicato.core import Generation
    from zicato.epoch.execution import load_epoch_execution_contract
    from zicato.tournament.runner import run_tournament

    workspace, epoch, runtime, adapter = _standalone(tmp_path)
    selected = load_epoch_execution_contract(workspace, epoch.id, workspace_config={})
    source = tmp_path / "source"
    source.mkdir()
    (source / "target.py").write_text("VALUE = 1\n")
    parent = Generation(
        id="v0", epoch_id=epoch.id, parent_id=None, snapshot_root=source, created_at=""
    )
    kwargs = dict(
        adapter=adapter,
        parent_gen=parent,
        child_gen=replace(parent, id="v1"),
        board=selected.board_with_meta[0],
        weights=selected.scoring,
        config=runtime,
        workspace_root=workspace,
        epoch_id=epoch.id,
        force_fresh=False,
    )
    asyncio.run(run_tournament(**kwargs))
    before = {
        path: path.read_bytes()
        for path in workspace.glob("epochs/*/generations/*/runs/*/seed-none/loss*.json")
    }
    assert len(before) == 4
    assert all(json.loads(body)["execution_started"] for body in before.values())
    asyncio.run(run_tournament(**kwargs))
    assert {path: path.read_bytes() for path in before} == before


@pytest.mark.parametrize("entrypoint", ["once", "rounds"])
def test_selected_epoch_refuses_callable_override_before_auxiliary_or_proposal_work(
    tmp_path, monkeypatch, entrypoint
):
    from tests.test_epoch_contract_carryover import workspace as workspace_fixture
    from tests.test_epoch_execution import _selected
    from zicato.evolve.loop import evolve_n_rounds
    from zicato.evolve.round_entry import evolve_once

    workspace = workspace_fixture.__wrapped__(tmp_path)
    path = workspace / "config.json"
    configuration = json.loads(path.read_bytes())
    configuration["runtime"].update(
        {
            "target_call_llm": "tests._subprocess_worker_support:target_call_llm",
            "evaluation_call_llm": "tests._subprocess_worker_support:evaluation_call_llm",
        }
    )
    path.write_text(json.dumps(configuration))
    selected = _selected(workspace)

    async def forbidden_work(**kwargs):
        pytest.fail("auxiliary work began before selected-runtime admission")

    monkeypatch.setattr("zicato.check.require_workspace_valid", lambda *a, **k: None)
    monkeypatch.setattr("zicato.proposer.agent.build_proposer_agent", lambda *a, **k: object())
    monkeypatch.setattr(
        "zicato.proposer.best_of_n.wrap_with_proposer_quality", lambda agent, *a, **k: agent
    )
    monkeypatch.setattr("zicato.evolve.round_entry._maybe_calibrate_noise_floor", forbidden_work)
    monkeypatch.setattr("zicato.evolve.epoching.ensure_epoch_for_contract", forbidden_work)
    call = evolve_once if entrypoint == "once" else evolve_n_rounds
    kwargs = {} if entrypoint == "once" else {"rounds": 1}
    with pytest.raises(ValueError, match="runtime execution roles differ from the selected epoch"):
        asyncio.run(
            call(
                workspace_root=workspace,
                epoch_id=selected.epoch_id,
                target_call_llm=alternate_call_llm,
                **kwargs,
            )
        )
