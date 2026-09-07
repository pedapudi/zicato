"""Timing must preserve failures and distinguish parallel sums from elapsed time."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_failed_command_is_recorded_without_success(tmp_path):
    report = tmp_path / "failed.json"
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "tools/verification_cost.py"),
            "--output",
            str(report),
            "--",
            sys.executable,
            "-c",
            "raise SystemExit(23)",
        ],
        cwd=ROOT,
    )
    assert result.returncode == 23
    data = json.loads(report.read_text())
    assert data["exit_status"] == 23
    assert data["wall_seconds"] > 0
    assert data["pytest"] is None


def test_parallel_report_records_selection_phases_and_failed_test(tmp_path):
    fixture = tmp_path / "test_cases.py"
    fixture.write_text(
        "import os, subprocess, sys\n"
        "def test_pass(tmp_path):\n"
        "    child = tmp_path / 'test_child.py'\n"
        "    child.write_text('def test_child(): pass')\n"
        "    config = tmp_path / 'pytest.ini'\n"
        "    config.write_text('[pytest]')\n"
        "    env = {k: v for k, v in os.environ.items() if k != 'PYTHONPATH'}\n"
        "    result = subprocess.run([sys.executable, '-m', 'pytest', '-c', str(config),\n"
        "        str(child)], cwd=tmp_path, env=env, capture_output=True, text=True)\n"
        "    assert result.returncode == 0, result.stdout + result.stderr\n"
        "def test_fail(): assert False\n"
    )
    config = tmp_path / "pytest.ini"
    config.write_text("[pytest]\n")
    report = tmp_path / "parallel.json"
    env = {**os.environ, "PYTEST_ADDOPTS": ""}
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "tools/verification_cost.py"),
            "--pytest",
            "--output",
            str(report),
            "--",
            sys.executable,
            "-m",
            "pytest",
            str(fixture),
            "-c",
            str(config),
            "-n",
            "2",
            "--confcutdir",
            str(tmp_path),
            "-q",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    data = json.loads(report.read_text())
    phases = data["pytest"]
    assert len(phases["selected_tests"]) == 2
    assert len(phases["test_reports"]) == 6
    assert sorted(
        report["outcome"] for report in phases["test_reports"] if report["phase"] == "call"
    ) == ["failed", "passed"]
    assert set(phases["worker_startup_and_collection_seconds"]) == {"gw0", "gw1"}
    assert set(phases["summed_test_phase_seconds"]) == {"setup", "call", "teardown"}
    assert set(phases["processes"]) == {"main", "gw0", "gw1"}
    assert phases["processes"]["gw0"]["observed_thread_max"] >= 1
    assert data["wall_seconds"] >= phases["wall_seconds"]
