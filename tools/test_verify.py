"""Verification completeness and failure handling without running the full plan."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import verify  # noqa: E402
from test_workflows import load_workflow  # noqa: E402

from tests import test_slow_tier_registry  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
selection_repository = test_slow_tier_registry.selection_repository


def test_python_partitions_preserve_the_full_selected_identities(
    selection_repository: Path,
) -> None:
    plan = {check.name: check.command for check in verify.check_plan()}
    selected = []
    for marker in (
        "not node and not cascade_oc",
        plan["python-default"][-1],
        plan["python-slow"][-1],
    ):
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "--collect-only", "-q", "-m", marker],
            cwd=selection_repository,
            text=True,
            capture_output=True,
            check=True,
            env={
                **os.environ,
                "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
                "PYTEST_ADDOPTS": "",
                "PYTHONPATH": str(ROOT),
            },
        )
        selected.append({line for line in result.stdout.splitlines() if "::test_" in line})
    complete, default, slow = selected
    assert complete == {"tests/test_mixed.py::test_fast", "tests/test_mixed.py::test_slow"}
    assert default.isdisjoint(slow)
    assert default | slow == complete


def test_complete_plan_and_ci_have_the_same_required_checks_once() -> None:
    plan = verify.select_checks(verify.check_plan(), "complete")
    counts: Counter[str] = Counter()
    for path in (ROOT / ".github/workflows").glob("*.yml"):
        for job in load_workflow(path)["jobs"].values():
            for step in job.get("steps", []):
                args = shlex.split(step.get("run", ""))
                if "tools/verify.py" in args:
                    assert "--only" in args, f"CI must declare its visible selection: {args}"
                    selected = verify.select_checks(
                        verify.check_plan(), "complete", args[args.index("--only") + 1]
                    )
                    counts.update(check.name for check in selected)
    assert counts == Counter(check.name for check in plan)
    assert {check.name for check in plan} == {
        "python-style",
        "python-format",
        "python-types",
        "import-boundaries",
        "python-default",
        "python-slow",
        "parity",
        "dashboard-javascript",
        "rust-format",
        "rust-clippy",
        "rust-tests",
        "installed-supervisor",
        "prose",
        "line-budget",
        "ledger",
    }


def test_required_commands_keep_independent_coverage_owners() -> None:
    plan = {check.name: check.command for check in verify.check_plan()}
    assert plan["parity"][-2:] == ("--skip", "PYTEST,MYPY")
    assert plan["python-types"][-2:] == ("mypy", "src/zicato/")
    assert plan["import-boundaries"][-1] == "tools/check_imports.py"
    assert plan["python-default"][-1] == "not node and not cascade_oc and not slow"
    assert plan["python-slow"][-1] == "slow and not node and not cascade_oc"
    for name in ("python-default", "python-slow"):
        assert "tests/" in plan[name]
        assert "tools/test_verify.py" in plan[name]
        assert "tools/test_affected_tests.py" in plan[name]
        assert not any("tools/parity/lib/test_" in arg for arg in plan[name])


def test_local_entry_points_dispatch_through_the_plan() -> None:
    expected = {
        "check": "--mode complete",
        "check-fast": "--mode iteration",
        "test": "--only python-default,python-slow",
        "test-fast": "--only python-default",
        "test-affected": "--only python-affected",
        "parity": "--only parity",
        "supervisor-check": "--only rust-format,rust-clippy,rust-tests",
    }
    for target, selection in expected.items():
        result = subprocess.check_output(["make", "-n", target], cwd=ROOT, text=True)
        assert f"tools/verify.py {selection}" in result
        assert result.count("tools/verify.py") == 1


@pytest.mark.parametrize("requested", ["", "unknown", "python-style,", "python-style,python-style"])
def test_invalid_selection_fails_before_any_command(requested: str) -> None:
    with pytest.raises(ValueError):
        verify.select_checks(verify.check_plan(), "complete", requested)


def test_empty_or_duplicate_registries_cannot_report_success(tmp_path: Path) -> None:
    check = verify.check_plan()[0]
    for plan in ((), (check, check)):
        with pytest.raises(ValueError):
            verify.select_checks(plan, "complete")
        with pytest.raises(ValueError):
            verify.run_checks(plan, 1, tmp_path)


@pytest.fixture
def runner_repository(tmp_path: Path) -> Path:
    root = tmp_path / "repository"
    (root / "tools").mkdir(parents=True)
    for name in ("verify.py", "affected_tests.py"):
        shutil.copyfile(ROOT / "tools" / name, root / "tools" / name)
    shutil.copyfile(ROOT / "Makefile", root / "Makefile")
    (root / ".gitignore").write_text("__pycache__/\n")
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "-c",
            "core.hooksPath=/dev/null",
            "commit",
            "-qm",
            "Fixture repository",
        ],
        cwd=root,
        check=True,
    )
    subprocess.run(["git", "update-ref", "refs/remotes/origin/main", "HEAD"], cwd=root, check=True)
    return root


def test_checker_failures_cross_both_ci_and_make_entry_points(runner_repository: Path) -> None:
    executables = runner_repository / "bin"
    executables.mkdir()
    node = executables / "node"
    node.write_text("#!/bin/sh\nexit 23\n")
    node.chmod(0o755)
    env = {**os.environ, "PATH": f"{executables}:{os.environ['PATH']}"}
    workflow = load_workflow(ROOT / ".github/workflows/ci.yml")
    step = next(
        step
        for step in workflow["jobs"]["dashboard-javascript"]["steps"]
        if step.get("name") == "Dashboard JavaScript behaviour"
    )
    ci_command = shlex.split(step["run"])
    ci_command[0] = sys.executable
    commands = (
        ci_command,
        ["make", "-s", "node-test", f"VERIFY={sys.executable} tools/verify.py"],
    )
    for command in commands:
        result = subprocess.run(
            command, cwd=runner_repository, env=env, text=True, capture_output=True
        )
        assert result.returncode != 0
        assert "dashboard-javascript: FAIL (23)" in result.stdout
        assert result.stdout.count("Running dashboard-javascript") == 1


def test_cli_refuses_zero_execution_and_bad_worker_counts(runner_repository: Path) -> None:
    for args in (
        ["--only", ""],
        ["--only", "missing"],
        ["--workers", "0"],
        ["--base", ""],
        ["--only", "missing", "--only", "python-style"],
    ):
        result = subprocess.run(
            [sys.executable, "tools/verify.py", *args],
            cwd=runner_repository,
            text=True,
            capture_output=True,
        )
        assert result.returncode == 2
        assert "Running " not in result.stdout


def test_affected_empty_selection_is_a_reported_iteration_result(runner_repository: Path) -> None:
    result = subprocess.run(
        [sys.executable, "tools/verify.py", "--only", "python-affected"],
        cwd=runner_repository,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "known empty" in result.stdout


def test_execution_records_missing_programs_signals_and_resource_bounds(
    runner_repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(verify, "ROOT", runner_repository)
    program = (
        "import json, os, pathlib; "
        "pathlib.Path('resources.json').write_text(json.dumps({"
        "k: os.environ[k] for k in "
        "['OMP_NUM_THREADS', 'OPENBLAS_NUM_THREADS', 'CARGO_BUILD_JOBS']}))"
    )
    checks = (
        verify.Check("resources", "Python", (), (sys.executable, "-c", program)),
        verify.Check("missing", "Python", (), ("program-that-does-not-exist",)),
        verify.Check(
            "signal",
            "Python",
            (),
            (sys.executable, "-c", "import os,signal; os.kill(os.getpid(),signal.SIGTERM)"),
        ),
    )
    reports = tmp_path / "reports"
    assert verify.run_checks(checks, 2, reports) == 1
    result = json.loads((reports / "results.json").read_text())
    assert [check["status"] for check in result["checks"]] == [0, 127, 143]
    assert result["revision"]
    assert json.loads((runner_repository / "resources.json").read_text()) == {
        "OMP_NUM_THREADS": "1",
        "OPENBLAS_NUM_THREADS": "1",
        "CARGO_BUILD_JOBS": "2",
    }
    with pytest.raises(ValueError, match="must be empty"):
        verify.run_checks(checks, 2, reports)


def test_iteration_preserves_other_languages_when_python_selection_is_empty(
    runner_repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    monkeypatch.setattr(
        verify,
        "build_selection",
        lambda: SimpleNamespace(
            status="known-empty",
            comparison=SimpleNamespace(changed=["src/zicato/dashboard/static/test/example.mjs"]),
        ),
    )
    names = {check.name for check in verify.select_checks(verify.check_plan(), "iteration")}
    assert "dashboard-javascript" in names
    assert "python-affected" in names
