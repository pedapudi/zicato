"""Measure one verification command; keep wall time separate from process CPU.

Use ``--pytest`` for collection and setup/call/teardown details. The command
and its arguments follow ``--``. The report records failures and preserves
the command's exit status. Reports describe one invocation, not cached passes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import resource
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def output(*args: str) -> str | None:
    try:
        result = subprocess.run(args, capture_output=True, text=True, check=True)
    except (OSError, subprocess.CalledProcessError):
        return None
    return result.stdout.strip()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--pytest", action="store_true")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("a command is required after --")
    env = os.environ.copy()
    before = resource.getrusage(resource.RUSAGE_CHILDREN)
    with tempfile.TemporaryDirectory(prefix="verification-cost-") as directory:
        phases = Path(directory) / "pytest.json"
        if args.pytest:
            command = [*command, "-p", "tools.pytest_cost", "--cost-report", str(phases)]
        started = time.perf_counter()
        try:
            status = subprocess.run(command, env=env).returncode
        except OSError as exc:
            print(str(exc), file=sys.stderr)
            status = 127
        wall = time.perf_counter() - started
        after = resource.getrusage(resource.RUSAGE_CHILDREN)
        details = json.loads(phases.read_text()) if phases.exists() else None
    cpu_limit = Path("/sys/fs/cgroup/cpu.max")
    memory_limit = Path("/sys/fs/cgroup/memory.max")
    report = {
        "command": command,
        "exit_status": status,
        "revision": output("git", "rev-parse", "HEAD"),
        "worktree": output("git", "status", "--porcelain"),
        "tracked_diff_sha256": hashlib.sha256(
            (output("git", "diff", "HEAD") or "").encode()
        ).hexdigest(),
        "python": sys.version,
        "platform": platform.platform(),
        "rustc": output("rustc", "-Vv"),
        "cargo": output("cargo", "--version"),
        "cpu_count": os.cpu_count(),
        "cpu_affinity_count": len(os.sched_getaffinity(0))
        if hasattr(os, "sched_getaffinity")
        else None,
        "cpu_limit": cpu_limit.read_text().strip() if cpu_limit.exists() else None,
        "memory_limit": memory_limit.read_text().strip() if memory_limit.exists() else None,
        "physical_memory_bytes": os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES"),
        "concurrency_environment": {
            key: env[key]
            for key in (
                "CARGO_BUILD_JOBS",
                "PYTEST_XDIST_AUTO_NUM_WORKERS",
                "OMP_NUM_THREADS",
                "OPENBLAS_NUM_THREADS",
                "MKL_NUM_THREADS",
                "VECLIB_MAXIMUM_THREADS",
                "NUMEXPR_NUM_THREADS",
            )
            if key in env
        },
        "wall_seconds": wall,
        "summed_child_cpu_seconds": (
            after.ru_utime + after.ru_stime - before.ru_utime - before.ru_stime
        ),
        "peak_child_rss_platform_units": after.ru_maxrss,
        "pytest": details,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    return status if status >= 0 else 128 - status


if __name__ == "__main__":
    raise SystemExit(main())
