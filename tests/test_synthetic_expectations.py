"""Tests for the synthetic-entry expectation matchers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from zicato.core.types import RuntimeConfig
from zicato.synthetic.expectations import (
    evaluate_no_drift,
    evaluate_required_drift,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


async def _noop_call_llm(system: str, user: str, model: str) -> str:
    return ""


async def _noop_call_llm_b(system: str, user: str, model: str) -> str:
    return ""


def _make_config(tmp_path: Path) -> RuntimeConfig:
    """Build a throwaway :class:`RuntimeConfig` for tests.

    The matchers take a config purely for forward-compatibility; they
    do not currently inspect any field. The config still needs to be
    a real instance with the documented two-callable rule honored so
    we do not encode an invalid shape into the tests.
    """
    return RuntimeConfig(
        instance_id="test",
        workspace_root=tmp_path,
        target_call_llm=_noop_call_llm,
        evaluation_call_llm=_noop_call_llm_b,
    )


def _write_jsonl(path: Path, records: list[dict[str, Any]]) -> Path:
    """Write a JSONL fixture file and return its path."""
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return path


def _drift_proto_record(kind: str, severity: str, **extra: Any) -> dict[str, Any]:
    """Build a goldfive-shape drift event (protobuf JSON form).

    Uses camelCase keys + ``DRIFT_*``/``DRIFT_SEVERITY_*`` enum strings,
    matching what ``MessageToJson`` emits.
    """
    return {
        "runId": "run-1",
        "sequence": extra.get("sequence", 0),
        "driftDetected": {
            "kind": kind,
            "severity": severity,
            "detail": extra.get("detail", ""),
            "currentTaskId": extra.get("current_task_id", ""),
            "currentAgentId": extra.get("current_agent_id", ""),
            "id": extra.get("id", ""),
        },
    }


def _drift_snake_record(kind: str, severity: str) -> dict[str, Any]:
    """Build a drift event in zicato's snake_case fallback shape."""
    return {
        "run_id": "run-1",
        "sequence": 0,
        "drift_detected": {"kind": kind, "severity": severity},
    }


# ---------------------------------------------------------------------------
# evaluate_required_drift
# ---------------------------------------------------------------------------


async def test_required_drift_passes_when_all_kinds_fire_at_warning(tmp_path: Path) -> None:
    path = _write_jsonl(
        tmp_path / "events.jsonl",
        [
            _drift_proto_record("DRIFT_OFF_TOPIC", "DRIFT_SEVERITY_WARNING"),
            _drift_proto_record("DRIFT_LOOPING_REASONING", "DRIFT_SEVERITY_CRITICAL"),
        ],
    )
    result = await evaluate_required_drift(
        path,
        ["off_topic", "looping_reasoning"],
        _make_config(tmp_path),
    )
    assert result.passed is True
    assert result.kind == "predicate"
    assert "off_topic" in result.detail
    assert "looping_reasoning" in result.detail


async def test_required_drift_fails_when_a_kind_is_missing(tmp_path: Path) -> None:
    path = _write_jsonl(
        tmp_path / "events.jsonl",
        [
            _drift_proto_record("DRIFT_OFF_TOPIC", "DRIFT_SEVERITY_WARNING"),
        ],
    )
    result = await evaluate_required_drift(
        path,
        ["off_topic", "looping_reasoning"],
        _make_config(tmp_path),
    )
    assert result.passed is False
    assert "looping_reasoning" in result.detail
    assert "off_topic" not in result.detail.split(":", 1)[1]


async def test_required_drift_fails_when_only_info_fires(tmp_path: Path) -> None:
    # INFO drift does not satisfy a requirement — observational severity
    # would trivially make every adversarial-entry expectation pass.
    path = _write_jsonl(
        tmp_path / "events.jsonl",
        [
            _drift_proto_record("DRIFT_OFF_TOPIC", "DRIFT_SEVERITY_INFO"),
        ],
    )
    result = await evaluate_required_drift(
        path,
        ["off_topic"],
        _make_config(tmp_path),
    )
    assert result.passed is False
    assert "off_topic" in result.detail


async def test_required_drift_accepts_snake_case_fallback_form(tmp_path: Path) -> None:
    # Internal fixtures may write the snake_case form rather than the
    # protobuf-JSON camelCase form. The matcher tolerates both.
    path = _write_jsonl(
        tmp_path / "events.jsonl",
        [
            _drift_snake_record("off_topic", "warning"),
            _drift_snake_record("looping_reasoning", "critical"),
        ],
    )
    result = await evaluate_required_drift(
        path,
        ["off_topic", "looping_reasoning"],
        _make_config(tmp_path),
    )
    assert result.passed is True


async def test_required_drift_ignores_non_drift_events(tmp_path: Path) -> None:
    path = _write_jsonl(
        tmp_path / "events.jsonl",
        [
            {"runId": "run-1", "sequence": 0, "runStarted": {"timestamp": "now"}},
            {"runId": "run-1", "sequence": 1, "taskStarted": {"taskId": "t1"}},
            _drift_proto_record("DRIFT_OFF_TOPIC", "DRIFT_SEVERITY_WARNING"),
        ],
    )
    result = await evaluate_required_drift(
        path,
        ["off_topic"],
        _make_config(tmp_path),
    )
    assert result.passed is True


async def test_required_drift_empty_required_kinds_fails(tmp_path: Path) -> None:
    path = _write_jsonl(tmp_path / "events.jsonl", [])
    result = await evaluate_required_drift(path, [], _make_config(tmp_path))
    assert result.passed is False
    assert "empty" in result.detail


async def test_required_drift_missing_file_treated_as_no_drift(tmp_path: Path) -> None:
    # Missing file == empty replay. The matcher fails (because nothing
    # could have fired) rather than raising — the runner is responsible
    # for surfacing "events.jsonl was never written" separately.
    result = await evaluate_required_drift(
        tmp_path / "does_not_exist.jsonl",
        ["off_topic"],
        _make_config(tmp_path),
    )
    assert result.passed is False
    assert "off_topic" in result.detail


async def test_required_drift_skips_malformed_lines(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    valid = json.dumps(_drift_proto_record("DRIFT_OFF_TOPIC", "DRIFT_SEVERITY_WARNING"))
    path.write_text("not json\n" + valid + "\n\n{broken\n", encoding="utf-8")
    result = await evaluate_required_drift(
        path,
        ["off_topic"],
        _make_config(tmp_path),
    )
    assert result.passed is True


# ---------------------------------------------------------------------------
# evaluate_no_drift
# ---------------------------------------------------------------------------


async def test_no_drift_passes_on_empty_file(tmp_path: Path) -> None:
    path = _write_jsonl(tmp_path / "events.jsonl", [])
    result = await evaluate_no_drift(path, _make_config(tmp_path))
    assert result.passed is True
    assert "no drift" in result.detail


async def test_no_drift_passes_when_only_info_drift(tmp_path: Path) -> None:
    # INFO drift is observational; it does not count against a clean run.
    path = _write_jsonl(
        tmp_path / "events.jsonl",
        [
            _drift_proto_record("DRIFT_OFF_TOPIC", "DRIFT_SEVERITY_INFO"),
            _drift_proto_record("DRIFT_LOOPING_REASONING", "DRIFT_SEVERITY_INFO"),
        ],
    )
    result = await evaluate_no_drift(path, _make_config(tmp_path))
    assert result.passed is True
    assert "info" in result.detail.lower()


async def test_no_drift_fails_on_warning_drift(tmp_path: Path) -> None:
    path = _write_jsonl(
        tmp_path / "events.jsonl",
        [
            _drift_proto_record("DRIFT_OFF_TOPIC", "DRIFT_SEVERITY_WARNING"),
        ],
    )
    result = await evaluate_no_drift(path, _make_config(tmp_path))
    assert result.passed is False
    assert "off_topic" in result.detail
    assert "warning" in result.detail


async def test_no_drift_fails_on_critical_drift(tmp_path: Path) -> None:
    path = _write_jsonl(
        tmp_path / "events.jsonl",
        [
            _drift_proto_record("DRIFT_OFF_TOPIC", "DRIFT_SEVERITY_INFO"),
            _drift_proto_record("DRIFT_LOOPING_REASONING", "DRIFT_SEVERITY_CRITICAL"),
        ],
    )
    result = await evaluate_no_drift(path, _make_config(tmp_path))
    assert result.passed is False
    assert "looping_reasoning" in result.detail
    assert "critical" in result.detail


async def test_no_drift_accepts_snake_case_form(tmp_path: Path) -> None:
    path = _write_jsonl(
        tmp_path / "events.jsonl",
        [
            _drift_snake_record("off_topic", "info"),
            _drift_snake_record("looping_reasoning", "warning"),
        ],
    )
    result = await evaluate_no_drift(path, _make_config(tmp_path))
    assert result.passed is False
    assert "looping_reasoning" in result.detail


async def test_no_drift_ignores_non_drift_events(tmp_path: Path) -> None:
    path = _write_jsonl(
        tmp_path / "events.jsonl",
        [
            {"runId": "run-1", "sequence": 0, "runStarted": {}},
            {"runId": "run-1", "sequence": 1, "taskCompleted": {"taskId": "t1"}},
        ],
    )
    result = await evaluate_no_drift(path, _make_config(tmp_path))
    assert result.passed is True


async def test_no_drift_unknown_severity_treated_as_non_scoring(tmp_path: Path) -> None:
    # Defensive: an event with a severity string we don't recognise
    # should NOT fail a clean run. The matcher is forgiving on the
    # replay path so unparseable fixtures cannot wedge a clean entry
    # into failure.
    path = _write_jsonl(
        tmp_path / "events.jsonl",
        [
            {
                "runId": "run-1",
                "sequence": 0,
                "driftDetected": {"kind": "DRIFT_OFF_TOPIC", "severity": "UNKNOWN"},
            },
        ],
    )
    result = await evaluate_no_drift(path, _make_config(tmp_path))
    assert result.passed is True
