"""Tests for ``zicato inspect logs`` — the operator-log tail CLI.

Subprocess-free: drives the command through :class:`click.testing.CliRunner`
against a hand-built ``.zicato/logs/`` stream. Covers the tail, the level
filter, the limit, ``--list``, and the honest empty-workspace behaviour
(prints nothing, exits 0).
"""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from zicato.cli.commands.logs import format_record, logs_cmd


def _write_stream(ws: Path, name: str, records: list[dict]) -> Path:
    import json

    logs = ws / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    path = logs / f"{name}.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return path


_TS = "2026-07-12T08:40:01.100Z"
_RECORDS = [
    {
        "ts": _TS,
        "level": "INFO",
        "component": "zicato.orchestrator",
        "message": "loop booted",
        "epoch_id": "e3",
    },
    {
        "ts": _TS,
        "level": "WARNING",
        "component": "zicato.tournament.runner",
        "message": "over budget",
        "epoch_id": "e3",
        "run_id": "g5--faq",
    },
    {"ts": _TS, "level": "ERROR", "component": "zicato.orchestrator", "message": "endpoint outage"},
]


def test_format_record_shape() -> None:
    """The line renderer names ts, level, component, context, message."""
    line = format_record(_RECORDS[1])
    assert "WARNING" in line
    assert "zicato.tournament.runner" in line
    assert "run_id=g5--faq" in line
    assert "over budget" in line


def test_logs_tail_prints_all_records(tmp_path: Path) -> None:
    ws = tmp_path / ".zicato"
    _write_stream(ws, "20260712T084000Z-1", _RECORDS)
    result = CliRunner().invoke(logs_cmd, ["--workspace", str(ws)])
    assert result.exit_code == 0
    assert "loop booted" in result.output
    assert "over budget" in result.output
    assert "endpoint outage" in result.output


def test_logs_level_filter(tmp_path: Path) -> None:
    ws = tmp_path / ".zicato"
    _write_stream(ws, "20260712T084000Z-1", _RECORDS)
    result = CliRunner().invoke(logs_cmd, ["--workspace", str(ws), "--level", "warning"])
    assert result.exit_code == 0
    assert "loop booted" not in result.output  # INFO filtered out
    assert "over budget" in result.output
    assert "endpoint outage" in result.output  # ERROR >= WARNING


def test_logs_limit_tails_last_n(tmp_path: Path) -> None:
    ws = tmp_path / ".zicato"
    _write_stream(ws, "20260712T084000Z-1", _RECORDS)
    result = CliRunner().invoke(logs_cmd, ["--workspace", str(ws), "--limit", "1"])
    assert result.exit_code == 0
    assert "endpoint outage" in result.output
    assert "loop booted" not in result.output


def test_logs_list_shows_invocations_newest_first(tmp_path: Path) -> None:
    ws = tmp_path / ".zicato"
    _write_stream(ws, "20260712T080000Z-1", _RECORDS[:1])
    _write_stream(ws, "20260712T090000Z-2", _RECORDS[:1])
    result = CliRunner().invoke(logs_cmd, ["--workspace", str(ws), "--list"])
    assert result.exit_code == 0
    lines = [ln for ln in result.output.splitlines() if ln.strip()]
    assert lines[0].startswith("20260712T090000Z-2")  # newest first
    assert lines[1].startswith("20260712T080000Z-1")


def test_logs_specific_invocation(tmp_path: Path) -> None:
    ws = tmp_path / ".zicato"
    _write_stream(ws, "20260712T080000Z-1", [dict(_RECORDS[0], message="old run")])
    _write_stream(ws, "20260712T090000Z-2", [dict(_RECORDS[0], message="new run")])
    result = CliRunner().invoke(
        logs_cmd, ["--workspace", str(ws), "--invocation", "20260712T080000Z-1"]
    )
    assert result.exit_code == 0
    assert "old run" in result.output
    assert "new run" not in result.output


def test_empty_workspace_prints_nothing_exit_zero(tmp_path: Path) -> None:
    """A workspace with no logs prints nothing and exits 0."""
    ws = tmp_path / ".zicato"
    ws.mkdir()
    result = CliRunner().invoke(logs_cmd, ["--workspace", str(ws)])
    assert result.exit_code == 0
    assert result.output.strip() == ""
