"""Tests for the tooling configuration files.

These checks ensure that pyproject.toml stays parseable and contains the
expected core dependencies, that the Makefile keeps its standard targets,
and that the CI workflow file is present. They guard against accidental
breakage of the developer ergonomics layer.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_pyproject_parses() -> None:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert data["project"]["name"] == "zicato"
    deps = data["project"]["dependencies"]
    assert any(d.startswith("click") for d in deps)
    assert any(d.startswith("jsonschema") for d in deps)


def test_makefile_has_targets() -> None:
    content = (ROOT / "Makefile").read_text()
    for target in ("install", "test", "lint", "format", "typecheck", "check"):
        assert f"\n{target}:" in content or content.startswith(f"{target}:")


def test_ci_workflow_present() -> None:
    assert (ROOT / ".github" / "workflows" / "ci.yml").exists()
