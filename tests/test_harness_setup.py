"""Custom harness registration and optional grading failures are observable."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from zicato.adapter_factory import make_adapter_from_config
from zicato.check import CheckContext, build_report
from zicato.cli.discovery import build_cli_root
from zicato.cli.init_cmd import initialize_workspace
from zicato.driver_imports import driver_import_scope, workspace_driver_imports
from zicato.health.diagnostics import assess_loop_health
from zicato.health.summarizer import epoch_summarizer_failures


def test_register_import_factory_arguments_and_operational_root(tmp_path: Path) -> None:
    workspace = tmp_path / "project with spaces" / ".zicato"
    initialize_workspace(workspace, instance_id="setup", example=True)
    driver = workspace.parent / "factory.py"
    driver.write_text("def build(value, *, mode):\n    return (value, mode)\n")
    result = CliRunner().invoke(
        build_cli_root(),
        [
            "epoch",
            "register",
            "--workspace",
            str(workspace),
            "--factory",
            "factory:build",
            "--factory-args",
            "[7]",
            "--factory-options",
            '{"mode":"bounded"}',
            "--import-root",
            ".",
            "--mutable-tree",
            str(workspace.parent / "system_under_test"),
            "--confirm-stock-grading",
        ],
    )
    assert result.exit_code == 0, result.output
    raw = json.loads((workspace / "config.json").read_text())
    assert raw["adapter"]["stock_grading_confirmed"] is True
    assert raw["adapter"]["import_roots"] == ["."]
    assert "adk_entrypoint" not in raw
    assert make_adapter_from_config(raw, workspace_root=workspace) == (7, "bounded")
    assert "goldfive" not in json.loads((workspace.parent / "scoring.json").read_text())
    raw["adapter"]["options"] = {"unrecognized": 2}
    with pytest.raises(ValueError, match="adapter factory.*arguments"):
        make_adapter_from_config(raw, workspace_root=workspace)


def test_preflight_rejects_dead_and_wrong_shape_summarizers_before_invocation(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / ".zicato"
    initialize_workspace(workspace, instance_id="setup", example=True)
    (tmp_path / "hooks.py").write_text(
        "not_callable = 3\ndef wrong_shape(first, second):\n"
        "    raise AssertionError('must not execute')\n"
    )
    scoring_file = tmp_path / "scoring.json"
    scoring = json.loads(scoring_file.read_text())
    for spec in ("missing_hooks:missing", "hooks:not_callable", "hooks:wrong_shape"):
        scoring["outcome_summarizer_spec"] = spec
        scoring_file.write_text(json.dumps(scoring))
        with CheckContext(workspace, live_contract=True) as context:
            failures = build_report(context).findings
        matching = [finding for finding in failures if finding.code == "grading_hook_unresolvable"]
        assert len(matching) == 1 and matching[0].blocking
        assert "must not execute" not in str(matching[0].detail)


def test_equivalent_adapter_defaults_and_operational_locations_preserve_identity(
    tmp_path: Path,
) -> None:
    from zicato.epoch.contract import compute_component_hashes, resolve_contract_inputs

    workspace = tmp_path / ".zicato"
    initialize_workspace(workspace, instance_id="setup", example=True)
    config_path = workspace / "config.json"
    config = json.loads(config_path.read_text())
    expected = compute_component_hashes(resolve_contract_inputs(workspace))
    config["adapter"].update(
        args=[], options={}, import_roots=[str(tmp_path)], stock_grading_confirmed=True
    )
    config_path.write_text(json.dumps(config))
    assert compute_component_hashes(resolve_contract_inputs(workspace)) == expected


def test_stock_grading_advisory_requires_explicit_confirmation(tmp_path: Path) -> None:
    workspace = tmp_path / ".zicato"
    initialize_workspace(workspace, instance_id="setup", example=True)
    board = tmp_path / "board.jsonl"
    entries = [json.loads(line) for line in board.read_text().splitlines()]
    for entry in entries:
        if "id" in entry:
            entry.pop("expectation", None)
    board.write_text("".join(json.dumps(entry) + "\n" for entry in entries))
    with CheckContext(workspace, live_contract=True) as context:
        advisory = [
            finding
            for finding in build_report(context).findings
            if finding.code == "custom_adapter_stock_grading"
        ]
    assert len(advisory) == 1 and not advisory[0].blocking
    config_file = workspace / "config.json"
    config = json.loads(config_file.read_text())
    config["adapter"]["stock_grading_confirmed"] = True
    config_file.write_text(json.dumps(config))
    with CheckContext(workspace, live_contract=True) as context:
        assert "custom_adapter_stock_grading" not in {
            finding.code for finding in build_report(context).findings
        }


def test_runtime_summarizer_failure_warns_and_survives_in_selected_epoch_health(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from zicato.core import ScoringWeights
    from zicato.evolve.decision_support import _render_failure_profile

    workspace = tmp_path / ".zicato"
    initialize_workspace(workspace, instance_id="setup", example=True)
    (tmp_path / "hooks.py").write_text(
        "def broken(losses):\n    raise RuntimeError('summarizer unavailable')\n"
    )
    with driver_import_scope(workspace_driver_imports(workspace)):
        _render_failure_profile(
            [],
            ScoringWeights(outcome_summarizer_spec="hooks:broken"),
            workspace_root=workspace,
            epoch_id="e0",
            round_index=2,
        )
    assert "outcome_summarizer_failed" in caplog.text
    failures = epoch_summarizer_failures(workspace, "e0")
    assert len(failures) == 1 and failures[0]["round_index"] == 2
    assert epoch_summarizer_failures(workspace, "e1") == ()
    report = assess_loop_health({}, [], [], "e0", summarizer_failures=failures)
    assert any(
        finding.code == "outcome_summarizer_failed" and finding.severity == "warning"
        for finding in report.findings
    )
    caplog.clear()
    _render_failure_profile([], ScoringWeights(), workspace_root=workspace, epoch_id="e1")
    assert "outcome_summarizer_failed" not in caplog.text
    assert epoch_summarizer_failures(workspace, "e1") == ()
