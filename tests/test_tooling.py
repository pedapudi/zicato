"""Tests for the tooling configuration files.

These checks ensure that pyproject.toml stays parseable and contains the
expected core dependencies, that the Makefile keeps its standard targets,
and that the CI workflow file is present. They guard against accidental
breakage of the developer ergonomics layer.
"""

from __future__ import annotations

import os
import subprocess
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def test_pyproject_parses() -> None:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert data["project"]["name"] == "zicato"
    deps = data["project"]["dependencies"]
    assert any(d.startswith("click") for d in deps)
    assert any(d.startswith("jsonschema") for d in deps)


def test_pyproject_dev_extra_includes_pre_commit() -> None:
    """pre-commit must live in the project venv, not in a global pipx /
    uv-tool install: the shared `.git/hooks/pre-commit` shim resolves
    pre-commit through `.venv/`, so dropping it from the dev extras
    would silently put `git commit` checks on a different toolchain
    than `uv run pre-commit run --all-files`.
    """
    data = tomllib.loads((ROOT / "pyproject.toml").read_text())
    dev = data["project"]["optional-dependencies"]["dev"]
    assert any(d.replace(" ", "").startswith("pre-commit") for d in dev), dev


def test_makefile_has_targets() -> None:
    content = (ROOT / "Makefile").read_text()
    for target in (
        "install",
        "install-hooks",
        "test",
        "lint",
        "format",
        "typecheck",
        "check",
    ):
        assert f"\n{target}:" in content or content.startswith(f"{target}:")


def test_ci_workflow_present() -> None:
    assert (ROOT / ".github" / "workflows" / "ci.yml").exists()


def _resolve_hooks_path() -> Path | None:
    """The directory git looks in for hooks for this checkout.

    Honours `core.hooksPath` if it is set (zicato pins it at the repo
    level so all worktrees share one hooks dir); otherwise falls back
    to the per-repo `<git-common-dir>/hooks/`.
    """
    try:
        configured = subprocess.run(
            ["git", "config", "--get", "core.hooksPath"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        if configured.returncode == 0 and configured.stdout.strip():
            return Path(configured.stdout.strip())
        common = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        return Path(common.stdout.strip()) / "hooks"
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def test_pre_commit_hook_uses_project_venv() -> None:
    """If a pre-commit hook is installed, it must route through the
    *project* venv (or `uv run`, which resolves to the project venv)
    rather than a global pipx / uv-tool / system `pre-commit`. The
    bug this guards against: `git commit` runs different tooling than
    `uv run pre-commit run --all-files` and contributors get forced
    onto `--no-verify`.
    """
    if os.environ.get("ZICATO_SKIP_HOOK_CHECK"):
        pytest.skip("hook check disabled via ZICATO_SKIP_HOOK_CHECK")
    hooks_dir = _resolve_hooks_path()
    if hooks_dir is None:
        pytest.skip("git not available")
    hook = hooks_dir / "pre-commit"
    if not hook.exists():
        pytest.skip(
            f"no pre-commit hook installed at {hook}; "
            "run `make install-hooks` (or `uv run pre-commit install`)"
        )
    content = hook.read_text()
    # The hook may route through the worktree's own .venv (preferred)
    # or via `uv run` (fallback for fresh clones), but it must do at
    # least one of those — never just bare `pre-commit` on PATH.
    routes_through_venv = ".venv/bin/python" in content and "pre_commit" in content
    routes_through_uv = "uv run" in content and "pre-commit" in content
    assert routes_through_venv or routes_through_uv, (
        f"{hook} does not route pre-commit through the project venv. "
        "Reinstall with `make install-hooks` (uses the shared shim that "
        "resolves .venv per worktree) or `uv run pre-commit install`."
    )
