"""Inspect the declared process environment without revealing credential values."""

from __future__ import annotations

import json

from click.testing import CliRunner

from zicato.cli.commands.config import config_env_cmd, render_env_report
from zicato.cli.discovery import build_cli_root
from zicato.config import describe_env_vars


def test_environment_report_is_grouped_under_inspect() -> None:
    root = build_cli_root()
    assert "config" not in root.commands
    result = CliRunner().invoke(root, ["inspect", "environment", "--help"])
    assert result.exit_code == 0, result.output


def test_text_and_json_report_every_declared_boundary() -> None:
    text = render_env_report()
    result = CliRunner().invoke(config_env_cmd, ["--json"])
    assert result.exit_code == 0, result.output
    rows = json.loads(result.output)
    assert rows == [
        {"name": info.name, "role": info.role, "description": info.description}
        for info in describe_env_vars()
    ]
    for row in rows:
        assert f"{row['name']} ({row['role']})" in text
        assert row["description"] in text
    assert "ZICATO_RUNTIME_CONTEXT" in text
    assert "ZICATO_HARMONOGRAF_URL" not in text
    assert "ZICATO_HARMONOGRAF_GRPC" not in text
