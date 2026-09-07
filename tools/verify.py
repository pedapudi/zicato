"""Run the shared iteration or complete verification plan.

Checks run sequentially. Python workers and native-library threads are
bounded independently; each requested check runs once and preserves its
failure status. Reports describe this invocation and are never a pass cache.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from collections import Counter
from dataclasses import asdict, dataclass
from fnmatch import fnmatch
from pathlib import Path

from affected_tests import build_selection, python_test_paths

ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Check:
    name: str
    language: str
    inputs: tuple[str, ...]
    command: tuple[str, ...]
    required: bool = True


def check_plan(
    workers: int = 4,
    base: str = "origin/main",
    installed_wheel: bool = False,
) -> tuple[Check, ...]:
    """Own every check command and its relevant iteration inputs."""
    if workers < 1:
        raise ValueError("workers must be a positive integer")
    if not base or base.startswith("-"):
        raise ValueError("base must name a revision")
    python = sys.executable
    tests = (python, "-m", "pytest", "-n", str(workers), *python_test_paths())
    python_inputs = ("*.py", "pyproject.toml", "uv.lock")
    runtime = ("src/*", "examples/*", "pyproject.toml", "uv.lock")
    rust = ("crates/*", "Cargo.*", "rust-toolchain*", "src/zicato/runtime/*")
    packaging = (
        (python, "-I", "tools/check_installed_supervisor.py", ".supervisor-cache/zicato-supervisor")
        if installed_wheel
        else (python, "tools/check_installed_supervisor.py", "--build")
    )
    return (
        Check("python-style", "Python", python_inputs, (python, "-m", "ruff", "check", ".")),
        Check(
            "python-format",
            "Python",
            python_inputs,
            (python, "-m", "ruff", "format", "--check", "."),
        ),
        Check("python-types", "Python", runtime, (python, "-m", "mypy", "src/zicato/")),
        Check(
            "import-boundaries",
            "Python",
            (*runtime, "tools/check_imports.py"),
            (python, "tools/check_imports.py"),
        ),
        Check(
            "python-default",
            "Python",
            ("*",),
            (*tests, "-m", "not node and not cascade_oc and not slow"),
        ),
        Check(
            "python-slow", "Python", ("*",), (*tests, "-m", "slow and not node and not cascade_oc")
        ),
        Check(
            "parity",
            "Python",
            (*runtime, "tools/parity*"),
            ("bash", "tools/parity.sh", "--skip", "PYTEST,MYPY"),
        ),
        Check(
            "dashboard-javascript",
            "JavaScript",
            ("src/zicato/dashboard/static/*",),
            ("node", "src/zicato/dashboard/static/test/run-all.mjs"),
        ),
        Check("rust-format", "Rust", rust, ("cargo", "fmt", "--check")),
        Check(
            "rust-clippy",
            "Rust",
            rust,
            ("cargo", "clippy", "--all-targets", "--", "-D", "warnings"),
        ),
        Check("rust-tests", "Rust", rust, ("cargo", "test")),
        Check(
            "installed-supervisor",
            "packaging",
            (*rust, "hatch_build.py", "pyproject.toml", "uv.lock"),
            packaging,
        ),
        Check(
            "prose",
            "prose",
            ("*.md", "*.py", "tools/prose_lint_baseline.json"),
            (python, "tools/prose_lint.py", "--baseline", "tools/prose_lint_baseline.json"),
        ),
        Check("line-budget", "repository", ("*",), (python, "tools/line_budget.py", "--check")),
        Check(
            "ledger",
            "repository",
            ("docs/design/LINE-BUDGET.md", ".line-budget.json", "tools/line_budget.py"),
            (python, "tools/line_budget.py", "--check-ledger", "--base", base),
        ),
        Check(
            "python-affected",
            "Python",
            ("*",),
            (python, "tools/affected_tests.py", "--run", "--", "-n", str(workers)),
            False,
        ),
    )


def select_checks(
    plan: tuple[Check, ...],
    mode: str,
    only: str | None = None,
) -> tuple[Check, ...]:
    names = [check.name for check in plan]
    if not names or len(names) != len(set(names)):
        raise ValueError("check registry is empty or contains duplicate names")
    if only is not None:
        requested = only.split(",")
        if not all(requested) or len(requested) != len(set(requested)):
            raise ValueError("check selection is empty or contains duplicate names")
        unknown = set(requested) - set(names)
        if unknown:
            raise ValueError(f"unknown checks: {', '.join(sorted(unknown))}")
        return tuple(check for check in plan if check.name in requested)
    if mode == "complete":
        return tuple(check for check in plan if check.required)
    if mode != "iteration":
        raise ValueError(f"unknown verification mode: {mode}")
    selection = build_selection()
    changed = selection.comparison.changed
    shared = any(
        path in {"tools/verify.py", "Makefile", "pyproject.toml", "uv.lock"}
        or path.startswith(".github/")
        for path in changed
    )
    return tuple(
        check
        for check in plan
        if check.name == "python-affected"
        or (
            check.name not in {"python-default", "python-slow", "parity"}
            and (
                shared
                or any(fnmatch(path, pattern) for path in changed for pattern in check.inputs)
            )
        )
    )


def run_checks(checks: tuple[Check, ...], workers: int, report_dir: Path) -> int:
    """Execute each selected check once and record failures, including missing tools."""
    if not checks or any(count != 1 for count in Counter(c.name for c in checks).values()):
        raise ValueError("execution requires a nonempty set of distinct checks")
    report_dir.mkdir(parents=True, exist_ok=True)
    if any(report_dir.iterdir()):
        raise ValueError("the report directory must be empty")
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    worktree = subprocess.check_output(["git", "status", "--porcelain=v1"], cwd=ROOT, text=True)
    env = {**os.environ, "UV_NO_SYNC": "1", "CARGO_BUILD_JOBS": str(workers)}
    for key in (
        "OMP_NUM_THREADS",
        "OPENBLAS_NUM_THREADS",
        "MKL_NUM_THREADS",
        "NUMEXPR_NUM_THREADS",
        "VECLIB_MAXIMUM_THREADS",
    ):
        env[key] = "1"
    results = []
    for check in checks:
        command = list(check.command)
        timing = ROOT / "tools/verification_cost.py"
        if timing.exists():
            command = [
                sys.executable,
                str(timing),
                "--output",
                str(report_dir / f"{check.name}.json"),
                *(["--pytest"] if check.name in {"python-default", "python-slow"} else []),
                "--",
                *command,
            ]
        print(f"Running {check.name}", flush=True)
        started = time.monotonic()
        try:
            status = subprocess.run(command, cwd=ROOT, env=env, check=False).returncode
            status = status if status >= 0 else 128 - status
        except OSError as exc:
            print(f"{check.name}: {exc}", file=sys.stderr)
            status = 127
        results.append(
            {
                "name": check.name,
                "command": check.command,
                "status": status,
                "wall_seconds": time.monotonic() - started,
            }
        )
        print(f"{check.name}: {'PASS' if status == 0 else 'FAIL'} ({status})", flush=True)
    report = {"revision": revision, "worktree": worktree, "workers": workers, "checks": results}
    (report_dir / "results.json").write_text(json.dumps(report, indent=2) + "\n")
    print(f"Verification report: {report_dir}")
    return 1 if any(result["status"] != 0 for result in results) else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--mode", choices=("iteration", "complete"), default="complete")
    parser.add_argument(
        "--only", action="append", help="check names for a partial invocation; repeatable"
    )
    parser.add_argument(
        "--workers", type=int, default=4, help="pytest workers; checks run sequentially"
    )
    parser.add_argument("--base", default=os.environ.get("VERIFICATION_BASE", "origin/main"))
    parser.add_argument(
        "--list", action="store_true", help="emit the selected check metadata as JSON"
    )
    parser.add_argument("--report-dir", type=Path)
    parser.add_argument(
        "--installed-wheel", action="store_true", help="verify an already installed wheel"
    )
    args = parser.parse_args(argv)
    try:
        selected = select_checks(
            check_plan(args.workers, args.base, args.installed_wheel),
            args.mode,
            None if args.only is None else ",".join(args.only),
        )
        if not selected:
            raise ValueError("no checks selected")
        if args.list:
            print(json.dumps([asdict(check) for check in selected], indent=2))
            return 0
        if args.report_dir:
            args.report_dir.mkdir(parents=True, exist_ok=True)
        directory = Path(tempfile.mkdtemp(prefix="zicato-verification-", dir=args.report_dir))
        return run_checks(selected, args.workers, directory)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
