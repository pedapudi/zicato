"""Generated configuration output remains usable by editors and operators."""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner
from jsonschema import Draft202012Validator

from zicato.cli.commands.config import inspect_config_cmd
from zicato.core.settings import InvocationOverlay
from zicato.workspace.config_inspection import (
    configuration_fields,
    configuration_reference,
    configuration_scaffold,
    configuration_schemas,
    effective_configuration,
)
from zicato.workspace.config_io import write_workspace_config
from zicato.workspace.config_schema import workspace_declaration
from zicato.workspace_loader import scoring_weights_from_dict


def test_generated_defaults_validate_with_the_editor_and_authored_decoders():
    schemas = configuration_schemas()
    for complete in (False, True):
        documents = configuration_scaffold(complete=complete)
        for filename, document in documents.items():
            schema = schemas[filename]
            Draft202012Validator.check_schema(schema)
            Draft202012Validator(schema).validate(document)
        workspace_declaration(documents["config.json"])
        scoring_weights_from_dict(documents["scoring.json"])


def test_every_declared_field_has_complete_explanation_metadata():
    fields = configuration_fields()
    for name, field in fields.items():
        assert field.get("description"), name
        assert field["x-path"], name
        assert field["x-scope"] in {"operational", "evaluation-contract"}, name
        assert type(field["x-rolls-epoch"]) is bool, name
        assert type(field["x-secret-reference"]) is bool, name
        assert "x-cli" in field, name
        assert "default" in field or field["x-required"], name
    assert fields["models.engines.<name>.api_key_env"]["x-secret-reference"] is True
    for name in (
        "models.roles",
        "models.engines.<name>.model",
        "models.engines.<name>.call_llm",
        "models.engines.<name>.revision",
    ):
        assert fields[name]["x-scope"] == "operational"
        assert fields[name]["x-rolls-epoch"] is False
    assert fields["runtime.seed"]["x-scope"] == "evaluation-contract"
    assert fields["runtime.seed"]["x-rolls-epoch"] is False
    assert fields["runtime.parallelism"]["x-cli"] == "--parallelism"
    reference = configuration_reference()
    assert all(f"## `{name}`" in reference for name in fields)


def test_effective_values_report_sources_without_reading_credentials(tmp_path, monkeypatch):
    workspace = tmp_path / ".zicato"
    workspace.mkdir()
    write_workspace_config(
        workspace,
        {
            "runtime": {"parallelism": 3},
            "models": {"engines": {"evaluation": {"model": "example", "api_key_env": "TEST_KEY"}}},
        },
    )
    (tmp_path / "scoring.json").write_text('{"promote_margin": 0.25}')
    monkeypatch.setenv("TEST_KEY", "private-credential")
    result = effective_configuration(
        workspace, overlay=InvocationOverlay({"runtime": {"seed": 17}})
    )
    assert result["runtime.parallelism"] == {"value": 3, "source": "workspace"}
    assert result["runtime.seed"] == {"value": 17, "source": "invocation"}
    assert result["scoring.promote_margin"] == {"value": 0.25, "source": "workspace"}
    assert result["runtime.max_tokens_per_round"]["source"] == "default"
    assert "private-credential" not in json.dumps(result)


def test_cli_explains_named_engine_fields_and_emits_editor_schema():
    runner = CliRunner()
    explanation = runner.invoke(inspect_config_cmd, ["models.engines.judge.api_key_env"])
    assert explanation.exit_code == 0, explanation.output
    assert json.loads(explanation.output)["x-secret-reference"] is True
    schema = runner.invoke(inspect_config_cmd, ["--schema"])
    assert schema.exit_code == 0, schema.output
    assert json.loads(schema.output) == configuration_schemas()


@pytest.mark.parametrize(
    "arguments",
    [
        ["runtime.paralellism"],
        ["--schema", "--effective"],
        ["--complete"],
        ["--schema", "--sources"],
    ],
)
def test_cli_rejects_unknown_fields_and_ambiguous_output_requests(arguments):
    result = CliRunner().invoke(inspect_config_cmd, arguments)
    assert result.exit_code == 2
