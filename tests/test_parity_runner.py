"""Exercise parity's shell boundary without running nested verification suites."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

GATES = [
    "PYTEST",
    "CONTRACT-HASH",
    "CLI-HELP",
    "REINDEX-DUMP",
    "MOCK-GOLDEN",
    "MOCK-GOLDEN-GAUNTLET",
    "MOCK-GOLDEN-GAUNTLET-FAST",
    "MOCK-GOLDEN-RACING-FAST",
    "MOCK-GOLDEN-TWO-ROUND-RACING",
    "MOCK-GOLDEN-SWISS",
    "MOCK-GOLDEN-SINGLE-ELIM",
    "MOCK-GOLDEN-DOUBLE-ELIM",
    "MYPY",
]


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    tools = tmp_path / "tools"
    tools.mkdir()
    shutil.copyfile(Path(__file__).resolve().parents[1] / "tools/parity.sh", tools / "parity.sh")
    golden = tools / "parity/golden"
    golden.mkdir(parents=True)
    (golden / "mypy_baseline.txt").write_text("0\n")
    runner = tmp_path / "uv"
    runner.write_text(
        '#!/bin/bash\nprintf "%s\\n" "$*" >> "$COMMAND_LOG"\n'
        'if [ "$CHECKER_SIGNAL" = 1 ]; then kill -TERM "$$"; fi\n'
        'printf "%s\\n" "$CHECKER_OUTPUT"\nexit "$CHECKER_STATUS"\n'
    )
    runner.chmod(0o755)
    return tmp_path


def run_parity(
    checkout: Path,
    *arguments: str,
    status: int = 0,
    signal: bool = False,
    output: str = "",
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(checkout / "tools/parity.sh"), *arguments],
        env={
            **os.environ,
            "PATH": f"{checkout}{os.pathsep}{os.environ['PATH']}",
            "COMMAND_LOG": str(checkout / "commands.txt"),
            "CHECKER_STATUS": str(status),
            "CHECKER_SIGNAL": str(int(signal)),
            "CHECKER_OUTPUT": output,
        },
        capture_output=True,
        text=True,
        timeout=10,
    )


@pytest.mark.parametrize(
    "arguments",
    [
        ("--only", "DOES-NOT-EXIST"),
        ("--skip", "DOES-NOT-EXIST"),
        ("--only",),
        ("--skip",),
        ("--only", "--update"),
        ("--only", ""),
        ("--skip", ""),
        ("--only", "MYPY,"),
        ("--only", "MYPY,,PYTEST"),
        ("--only", "*"),
        ("--only", "MYPY", "--skip", "MYPY"),
        ("--skip", ",".join(GATES)),
    ],
)
def test_invalid_selection_fails_before_any_checker(
    checkout: Path, arguments: tuple[str, ...]
) -> None:
    result = run_parity(checkout, *arguments)
    assert result.returncode == 2, result.stdout + result.stderr
    assert "PARITY: GREEN" not in result.stdout
    assert not (checkout / "commands.txt").exists()


@pytest.mark.parametrize("status", [1, 2, 127])
@pytest.mark.parametrize("update", [False, True])
def test_type_checker_failure_cannot_pass_or_update_baseline(
    checkout: Path, status: int, update: bool
) -> None:
    baseline = checkout / "tools/parity/golden/mypy_baseline.txt"
    before = baseline.read_bytes()
    result = run_parity(
        checkout, "--only", "MYPY", *(("--update",) if update else ()), status=status
    )
    assert result.returncode != 0
    assert "PARITY: GREEN" not in result.stdout
    assert f"exit status {status}" in result.stdout + result.stderr
    assert baseline.read_bytes() == before


def test_type_checker_signal_fails(checkout: Path) -> None:
    result = run_parity(checkout, "--only", "MYPY", signal=True)
    assert result.returncode != 0
    assert "PARITY: GREEN" not in result.stdout
    assert "exit status 143" in result.stdout + result.stderr


def test_diagnostic_count_cannot_allow_failed_type_checker(checkout: Path) -> None:
    result = run_parity(checkout, "--only", "MYPY", status=1, output="source.py:1: error: invalid")
    assert result.returncode != 0


def test_default_runs_every_declared_gate_once(checkout: Path) -> None:
    result = run_parity(checkout)
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"Selected gates: {', '.join(GATES)}" in result.stdout
    assert len((checkout / "commands.txt").read_text().splitlines()) == len(GATES)
    for gate in GATES:
        assert f"  {gate}\n" in result.stdout


def test_partial_selection_combines_options_and_excludes_once(checkout: Path) -> None:
    result = run_parity(
        checkout, "--only", "MYPY,PYTEST", "--only", "CLI-HELP,MYPY", "--skip", "PYTEST"
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Selected gates: CLI-HELP, MYPY\n" in result.stdout
    commands = (checkout / "commands.txt").read_text().splitlines()
    assert len(commands) == 2
    assert "cli_help.py" in commands[0]
    assert commands[1] == "run mypy src/zicato/"


def test_failed_gate_does_not_prevent_reporting_other_selected_gates(checkout: Path) -> None:
    result = run_parity(checkout, "--only", "PYTEST,MYPY", status=127)
    assert result.returncode != 0
    assert len((checkout / "commands.txt").read_text().splitlines()) == 2
    assert "  PYTEST\n" in result.stdout and "  MYPY\n" in result.stdout
