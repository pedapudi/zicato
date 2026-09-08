"""Authored workspace configuration fails before coercion or publication."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from zicato.core.configuration import ConfigurationError, dataclass_schema, dataclass_to_jsonable
from zicato.models_config import role_spec_from_dict
from zicato.proposer.foe_config import load_foe_proposer_config, scaffold_proposer_block
from zicato.workspace.config_io import read_workspace_config, write_workspace_config
from zicato.workspace.config_schema import WorkspaceDeclaration, workspace_declaration


@pytest.mark.parametrize(
    ("raw", "path"),
    [
        ({"runtmie": {"parallelism": 1}}, "config.runtmie"),
        ({"runtime": {"seed": True}}, "config.runtime.seed"),
        ({"contract": {"scoring_paht": "scoring.json"}}, "config.contract.scoring_paht"),
        ({"models": {"engines": {"judge": {"model": "example", "revision": 3}}}}, "revision"),
        ({"calibrate_noise_floor": 1}, "config.calibrate_noise_floor"),
        ({"storage_gc": {"on_epoch_close": "true"}}, "config.storage_gc.on_epoch_close"),
        ({"storage_gc": {"keep_last_n": 0}}, "config.storage_gc.keep_last_n"),
        ({"storage_gc": {"keep_last_n": True}}, "config.storage_gc.keep_last_n"),
    ],
)
def test_invalid_workspace_fields_are_rejected(raw, path):
    with pytest.raises(ConfigurationError, match=path):
        workspace_declaration(raw)


def test_proposer_options_and_unknown_fields_are_not_coerced():
    block = scaffold_proposer_block()
    block["model"]["options"]["flag"] = True
    with pytest.raises(ValueError, match="proposer.model.options.flag: expected a string"):
        load_foe_proposer_config({"proposer": block})
    block = scaffold_proposer_block()
    block["budegt"] = {"model_calls": 1}
    with pytest.raises(ValueError, match="proposer.budegt: unknown field"):
        load_foe_proposer_config({"proposer": block})
    with pytest.raises(ConfigurationError, match="revision: expected a string"):
        role_spec_from_dict({"model": "example", "revision": 3})


def test_invalid_write_leaves_existing_configuration_bytes_unchanged(tmp_path: Path):
    write_workspace_config(tmp_path, {"runtime": {"parallelism": 3}})
    before = read_workspace_config(tmp_path).path.read_bytes()
    with pytest.raises(ConfigurationError, match="config.runtime.parallelism"):
        write_workspace_config(tmp_path, {"runtime": {"parallelism": 2.8}})
    assert read_workspace_config(tmp_path).path.read_bytes() == before


def test_complete_declaration_roundtrips_through_decoder_and_editor_schema(tmp_path: Path):
    raw = dataclass_to_jsonable(WorkspaceDeclaration())
    raw["proposer"] = scaffold_proposer_block()
    raw["adapter"] = {"kind": "import", "factory": "example_wiring:make_adapter"}
    raw["models"] = {"engines": {"evaluation": {"model": "example-model"}}, "roles": {}}
    schema = dataclass_schema(WorkspaceDeclaration)
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(raw)
    declared = workspace_declaration(json.loads(json.dumps(raw)))
    write_workspace_config(tmp_path, raw)
    assert read_workspace_config(tmp_path).values == declared


def test_authored_defaults_match_factories_and_generated_schema():
    from zicato.core.adapter_config import adapter_declaration
    from zicato.core.settings import resolve_configuration
    from zicato.runtime_factory import resolve_host_worker_permits

    proposer = scaffold_proposer_block()
    proposer.update(binary="~/proposal-runtime", _guide={"binary": "Choose an executable."})
    raw = {
        "adapter": {
            "kind": "import",
            "factory": "tests._stub_adapter:make_stub_adapter",
        },
        "proposer": proposer,
        "runtime": {
            "worker_permit_dir": "~/worker-permits",
            "log_level": "dEbUg",
        },
    }
    Draft202012Validator(dataclass_schema(WorkspaceDeclaration)).validate(raw)
    declared = workspace_declaration(raw)
    assert declared.adapter == adapter_declaration(raw)
    assert declared.adapter.args == ()
    assert load_foe_proposer_config(raw).binary == Path("~/proposal-runtime").expanduser()
    assert declared.proposer.guide == proposer["_guide"]
    resolved = resolve_configuration(raw)
    assert resolved.values.runtime.parallelism == 4
    assert resolved.values.runtime.propose_parallelism == 4
    assert resolved.values.runtime.log_level == "DEBUG"
    assert resolved.values.runtime.worker_permit_dir == Path("~/worker-permits").expanduser()
    for setting, expected in ((True, None), (False, 0), (None, None), (3, 3)):
        raw["runtime"]["host_worker_permits"] = setting
        Draft202012Validator(dataclass_schema(WorkspaceDeclaration)).validate(raw)
        assert resolve_host_worker_permits(raw["runtime"])[0] == expected

    nullable_defaults = {
        "parallelism": None,
        "propose_parallelism": None,
        "infra_abort_round_threshold": None,
        "infra_backoff_base_s": None,
        "infra_backoff_cap_s": None,
        "max_tokens_per_round": None,
        "preflight_probe_points": None,
        "worker_env_passthrough": None,
        "preflight_probe_mutation_ids": None,
    }
    errors = list(
        Draft202012Validator(dataclass_schema(WorkspaceDeclaration)).iter_errors(
            {"runtime": nullable_defaults}
        )
    )
    assert {error.path[-1] for error in errors} == set(nullable_defaults)
    with pytest.raises(ConfigurationError, match="config.runtime.parallelism"):
        resolve_configuration({"runtime": nullable_defaults})


def test_declared_telemetry_and_invocation_override_retain_source():
    from zicato.core.settings import InvocationOverlay, resolve_configuration
    from zicato.evolve.lifecycle_services import _resolve_harmonograf_url

    raw = {"integration": {"harmonograf_url": "http://127.0.0.1:9100"}}
    selected = resolve_configuration(raw)
    assert (
        _resolve_harmonograf_url(Path("."), selected.values.integration)
        == raw["integration"]["harmonograf_url"]
    )
    assert selected.sources["integration.harmonograf_url"] == "workspace"
    raw["integration"] = {"harmonograf_url": "http://127.0.0.1:9200"}
    assert resolve_configuration(raw).values.integration.harmonograf_url == "http://127.0.0.1:9200"
    overridden = resolve_configuration(
        raw,
        overlay=InvocationOverlay({"integration": {"harmonograf_url": "http://127.0.0.1:9300"}}),
    )
    assert overridden.values.integration.harmonograf_url == "http://127.0.0.1:9300"
    assert overridden.sources["integration.harmonograf_url"] == "invocation"
