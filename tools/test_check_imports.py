"""Import checks reject missing ownership and real forbidden dependency paths."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
COMMAND = ROOT / "tools" / "check_imports.py"


def _project(
    root: Path,
    roles: dict[str, str],
    files: dict[str, str],
    contracts: list[dict[str, Any]] | None = None,
) -> None:
    package = root / "src" / "zicato"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    for name in roles:
        directory = package / name
        directory.mkdir()
        (directory / "__init__.py").write_text("", encoding="utf-8")
    for name, content in files.items():
        path = package / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    lines = ["[tool.zicato.namespace_roles]"]
    lines.extend(f"{name} = {json.dumps(role)}" for name, role in roles.items())
    lines.extend(["[tool.zicato.importlinter]", 'root_packages = ["zicato"]'])
    for contract in contracts or []:
        lines.append("[[tool.zicato.importlinter.contracts]]")
        lines.extend(f"{name} = {json.dumps(value)}" for name, value in contract.items())
    (root / "pyproject.toml").write_text("\n".join(lines), encoding="utf-8")


def _run(root: Path, **environment: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(COMMAND), "--root", str(root)],
        cwd=root,
        env={**os.environ, **environment},
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize(
    "path", ["unclassified.py", "unclassified/__init__.py", "unclassified/task.py"]
)
def test_unclassified_namespace_is_rejected(tmp_path: Path, path: str) -> None:
    _project(
        tmp_path, {"core": "execution", "dashboard": "driver"}, {path: "import zicato.dashboard\n"}
    )
    result = _run(tmp_path)
    assert result.returncode == 1
    assert "unclassified=['unclassified']" in result.stderr


@pytest.mark.parametrize(
    ("roles", "files", "expected"),
    [
        (
            {"core": "execution", "dashboard": "driver"},
            {"core/__init__.py": "import zicato.dashboard\n"},
            "zicato.core is not allowed to import zicato.dashboard",
        ),
        (
            {"core": "execution", "bridge": "mixed_library", "report": "coordination"},
            {
                "core/__init__.py": "import zicato.bridge\n",
                "bridge/__init__.py": "import zicato.report\n",
            },
            "zicato.core is not allowed to import zicato.report",
        ),
        (
            {"storage": "primitive", "core": "execution"},
            {"storage/__init__.py": "import zicato.core\n"},
            "zicato.storage is not allowed to import zicato.core",
        ),
    ],
)
def test_forbidden_paths_fail(
    tmp_path: Path, roles: dict[str, str], files: dict[str, str], expected: str
) -> None:
    _project(tmp_path, roles, files)
    result = _run(tmp_path)
    assert result.returncode == 1
    assert expected in result.stdout


def test_root_facade_has_no_driver_exemption(tmp_path: Path) -> None:
    _project(
        tmp_path,
        {"core": "execution", "dashboard": "driver"},
        {"__init__.py": "from . import dashboard\n"},
    )
    result = _run(tmp_path)
    assert result.returncode == 1
    assert "root facade imports a driver: zicato -> zicato.dashboard" in result.stderr


def test_existing_driver_permissions_remain_accepted(tmp_path: Path) -> None:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    driver_contracts = config["tool"]["zicato"]["importlinter"]["contracts"][:1]
    _project(
        tmp_path,
        {name: "driver" for name in ("cli", "dashboard")},
        {
            "cli/__init__.py": "import zicato.dashboard\n",
        },
        driver_contracts,
    )
    result = _run(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Contracts: 1 kept, 0 broken" in result.stdout


def test_renamed_and_duplicate_namespaces_are_rejected(tmp_path: Path) -> None:
    _project(tmp_path, {"core": "execution", "dashboard": "driver"}, {})
    (tmp_path / "src/zicato/core").rename(tmp_path / "src/zicato/renamed")
    result = _run(tmp_path)
    assert result.returncode == 1
    assert "unclassified=['renamed'], absent=['core']" in result.stderr
    (tmp_path / "src/zicato/renamed").rename(tmp_path / "src/zicato/core")
    (tmp_path / "src/zicato/core.py").write_text("", encoding="utf-8")
    result = _run(tmp_path)
    assert result.returncode == 1
    assert "duplicate production namespace: zicato.core" in result.stderr


def test_checker_failure_status_is_preserved(tmp_path: Path) -> None:
    _project(tmp_path, {"core": "execution", "dashboard": "driver"}, {})
    package = tmp_path / "checker" / "importlinter"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "cli.py").write_text("def lint_imports(**kwargs): return 7\n", encoding="utf-8")
    result = _run(tmp_path, PYTHONPATH=str(package.parent))
    assert result.returncode == 7


def test_direct_linter_cannot_report_partial_success(tmp_path: Path) -> None:
    _project(tmp_path, {"core": "execution", "dashboard": "driver"}, {})
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from importlinter.cli import lint_imports; raise SystemExit(lint_imports())",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "Could not read any configuration" in result.stdout
