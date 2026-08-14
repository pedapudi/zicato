"""Tests for ``zicato inspect environment`` — the merited env-var report.

The command surfaces :func:`zicato.config.describe_env_vars` (which,
since the env-var rationalization, describes only the deliberately-kept
process-boundary contracts) so the environment surface is discoverable
without grepping the tree. The rendered text is pinned here as a
content golden: the merited names, their role labels, and the pointer
telling operators that knobs live on flags + config.json.
"""

from __future__ import annotations

import json

from click.testing import CliRunner

from zicato.cli.commands.config import config_env_cmd, render_env_report
from zicato.cli.discovery import build_cli_root
from zicato.config import describe_env_vars


def test_environment_report_is_grouped_under_inspect() -> None:
    runner = CliRunner()
    result = runner.invoke(build_cli_root(), ["--help"])
    assert result.exit_code == 0, result.output
    assert "inspect" in result.output
    assert "config" not in build_cli_root().commands
    result = runner.invoke(build_cli_root(), ["inspect", "environment", "--help"])
    assert result.exit_code == 0, result.output


def test_config_env_reports_the_merited_set() -> None:
    """The report names every merited variable under its role label."""
    runner = CliRunner()
    result = runner.invoke(config_env_cmd)
    assert result.exit_code == 0, result.output

    # Every described variable (and only those) appears.
    for info in describe_env_vars():
        assert info.name in result.output, f"missing {info.name}"

    # The golden content: role labels present as group headings...
    for role in (
        "harness-contract",
        "internal-handoff",
        "secrets-boundary",
        "external-integration",
        "test-toggle",
    ):
        assert f"[{role}]" in result.output, f"missing role heading {role}"

    # ...and the operator pointer: knobs live on flags + config.json.
    assert "CLI flags" in result.output
    assert "config.json" in result.output

    # None of the deleted operator env knobs are advertised.
    for deleted in (
        "ZICATO_MAX_WALL_CLOCK_SECONDS",
        "ZICATO_WORKSPACE",
        "ZICATO_INSTANCE_ID",
        "ZICATO_AUX_CALL_TIMEOUT",
        "ZICATO_PARALLELISM",
        "ZICATO_HARNESS_CALL_TIMEOUT_MS",
        "ZICATO_SUPERVISOR_BINARY",
        "ZICATO_DASHBOARD_STATIC_DIR",
        "ZICATO_HEALTH_SCORING_WINDOW",
    ):
        assert deleted not in result.output, f"deleted knob {deleted} advertised"


def test_config_env_golden_text_shape() -> None:
    """A small literal golden on the report's load-bearing lines."""
    text = render_env_report()
    assert text.startswith("Environment variables zicato touches — the deliberate set.")
    assert "ZICATO_RUN_SCRATCH_DIR" in text
    assert "ZICATO_HARMONOGRAF_URL" in text
    assert "ZICATO_HARMONOGRAF_GRPC" in text
    assert "<models.<role>.api_key_env>" in text
    assert "<runtime.worker_env_passthrough>" in text
    assert "GOLDFIVE_AGENT_CALL_TIMEOUT_MS" in text
    assert "ZICATO_SKIP_HOOK_CHECK" in text
    assert "ZICATO_PARITY_UPDATE" in text
    # The harmonograf handoff is explicitly marked as not an operator knob.
    assert "NOT an operator knob" in text


def test_config_env_json_output() -> None:
    """``--json`` emits the same set machine-readably."""
    runner = CliRunner()
    result = runner.invoke(config_env_cmd, ["--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert isinstance(payload, list)
    by_name = {row["name"]: row for row in payload}
    assert by_name["ZICATO_RUN_SCRATCH_DIR"]["role"] == "harness-contract"
    assert by_name["ZICATO_HARMONOGRAF_URL"]["role"] == "internal-handoff"
    assert set(by_name) == {info.name for info in describe_env_vars()}
