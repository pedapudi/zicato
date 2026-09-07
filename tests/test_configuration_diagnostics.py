"""Configuration failures identify the authored field and registered source."""

import pytest
from click.testing import CliRunner

from tests.test_check_gate import _workspace
from zicato.cli.commands.evolve import evolve_cmd
from zicato.cli.commands.setup import setup_cmd
from zicato.core.types import validate_board_entry


@pytest.mark.parametrize("command", ["setup", "dry_run", "evolve"])
def test_malformed_model_settings_name_the_field_without_a_traceback(tmp_path, command):
    workspace = _workspace(
        tmp_path / ".zicato",
        config={"models": {"engines": {"target": {"model": 42}}}},
    )
    cmd = setup_cmd if command == "setup" else evolve_cmd
    args = ["--workspace", str(workspace)]
    if command == "dry_run":
        args.append("--dry-run")
    result = CliRunner().invoke(cmd, args)
    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit)
    assert "models.engines.target.model" in result.output
    assert "expected a string" in result.output
    assert "Traceback" not in result.output


def test_missing_board_kind_names_the_field_and_valid_values():
    with pytest.raises(ValueError, match="missing required field 'kind'.*single_turn"):
        validate_board_entry({"id": "entry", "input": "hello", "wall_clock_budget_seconds": 30})


@pytest.mark.parametrize("command", ["setup", "dry_run"])
@pytest.mark.parametrize("defect", ["board_kind", "annotations"])
def test_setup_diagnostics_use_authored_fields_and_source_paths(tmp_path, command, defect):
    entry = {
        "id": "entry",
        "kind": "single_turn",
        "input": "hello",
        "wall_clock_budget_seconds": 30,
    }
    source = '# zicato:mutable:file id="prompt"\nPROMPT = "hello"\n'
    if defect == "board_kind":
        del entry["kind"]
    else:
        source = 'PROMPT = "hello"\n'
    workspace = _workspace(
        tmp_path / ".zicato",
        config={
            "adapter": {"kind": "import", "factory": "tests.test_check_gate:_make_test_adapter"}
        },
        board=[entry],
        trees={"target_source": source},
    )
    cmd = setup_cmd if command == "setup" else evolve_cmd
    args = ["--workspace", str(workspace)]
    if command == "dry_run":
        args.append("--dry-run")
    result = CliRunner().invoke(cmd, args)
    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit)
    assert "Traceback" not in result.output
    if defect == "board_kind":
        assert "missing required field 'kind'" in result.output
        assert "single_turn" in result.output
    else:
        assert str(tmp_path / "target_source") in result.output
        assert "zicato-check-" not in result.output
