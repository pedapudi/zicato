"""The tournament worker grades synthetic entries by their kind's drift requirement.

Each test spawns a real worker process on a synthetic board entry whose
goldfive-driven runner is replaced by one that emits scripted drift events
(:mod:`tests._synthetic_worker_support`). The assertions read the
``loss.json`` the worker wrote, so they cover sink close, grading, and loss
reduction as production runs them.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from tests._runtime_builders import make_generation
from tests.test_subprocess_workers import _entry, _worker_env, _write_args_file

pytestmark = [pytest.mark.integration]

_SUPPORT = "tests._synthetic_worker_support"


def _grade(
    tmp_path: Path,
    kind: str,
    drift: list[tuple[str, str]],
    *,
    predicate: str | None = None,
) -> dict[str, Any]:
    """Run one synthetic entry through the worker and return its expectation result."""
    workspace = tmp_path / ".zicato"
    workspace.mkdir()
    generation = make_generation(workspace)
    args_path = tmp_path / "args.json"
    _write_args_file(
        args_path,
        workspace=workspace,
        generation=generation,
        entry=_entry(),
        result_path=tmp_path / "result.json",
        adapter_factory="tests._subprocess_worker_support:make_completing_adapter",
    )
    args = json.loads(args_path.read_text(encoding="utf-8"))
    entry = args["entry"]
    entry["kind"] = kind
    entry["context"]["scripted_drift"] = json.dumps(drift)
    if kind == "synthetic_adversarial":
        entry["adversarial_agent_spec"] = f"{_SUPPORT}:_scripted_runner"
        entry["required_drift_kinds"] = ["tool_error"]
    if predicate is not None:
        entry["expectation"] = {"kind": "predicate", "spec": f"{_SUPPORT}:{predicate}"}
    args_path.write_text(json.dumps(args), encoding="utf-8")

    proc = subprocess.run(  # noqa: S603
        [sys.executable, "-m", _SUPPORT, str(args_path)],
        env=_worker_env(),
        timeout=60,
        check=False,
        capture_output=True,
    )
    assert proc.returncode == 0, proc.stderr.decode()
    loss = json.loads(Path(args["loss_path"]).read_text(encoding="utf-8"))
    result: dict[str, Any] = loss["expectation_result"]
    return result


def test_adversarial_entry_fails_when_the_required_drift_is_missing(tmp_path: Path) -> None:
    result = _grade(tmp_path, "synthetic_adversarial", [("tool_error", "info")])
    assert result["passed"] is False
    assert "missing required drift kinds: tool_error" in result["detail"]


def test_adversarial_entry_passes_when_the_required_drift_fires(tmp_path: Path) -> None:
    result = _grade(tmp_path, "synthetic_adversarial", [("tool_error", "warning")])
    assert result["passed"] is True


def test_clean_entry_fails_on_a_warning_drift(tmp_path: Path) -> None:
    result = _grade(tmp_path, "synthetic_clean", [("tool_error", "warning")])
    assert result["passed"] is False
    assert "tool_error@warning" in result["detail"]


def test_clean_entry_passes_with_only_info_drift(tmp_path: Path) -> None:
    result = _grade(tmp_path, "synthetic_clean", [("tool_error", "info")])
    assert result["passed"] is True


def test_failing_explicit_expectation_fails_a_satisfied_adversarial_entry(
    tmp_path: Path,
) -> None:
    result = _grade(
        tmp_path,
        "synthetic_adversarial",
        [("tool_error", "critical")],
        predicate="output_is_other",
    )
    assert result["passed"] is False
    assert result["detail"].startswith("drift requirement passed")
    assert "expectation failed" in result["detail"]


def test_passing_explicit_expectation_cannot_rescue_a_missed_drift(tmp_path: Path) -> None:
    result = _grade(tmp_path, "synthetic_adversarial", [], predicate="output_is_done")
    assert result["passed"] is False
    assert result["score"] == 0.0
    assert result["detail"].startswith("drift requirement failed")
    assert "expectation passed" in result["detail"]
