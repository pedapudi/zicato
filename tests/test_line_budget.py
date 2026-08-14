from __future__ import annotations

import json
import subprocess
from pathlib import Path

from tools.line_budget import Report, _excluded, _production, check, measure


def _report(*, total: int, production: int) -> Report:
    return Report(1, total, 1, production, {}, {})


def _config(path: Path, *, total: int = 10, production: int = 5) -> Path:
    path.write_text(
        json.dumps(
            {
                "limits": {"total": total, "production": production},
            }
        )
    )
    return path


def test_exclusions_are_narrow_and_explicit() -> None:
    assert _excluded("README.md")
    assert _excluded("Cargo.lock")
    assert _excluded("docs/presentation/slides/slide-01.svg")
    assert not _excluded("src/zicato/core/scoring.py")
    assert not _excluded("tests/test_scoring.py")


def test_production_is_independent_of_tests_and_assets() -> None:
    assert _production("src/zicato/core/scoring.py")
    assert _production("crates/supervisor/src/main.rs")
    assert not _production("tests/test_scoring.py")
    assert not _production("src/zicato/dashboard/static/test/core.test.mjs")
    assert not _production("src/zicato/dashboard/static/brand/wordmark.svg")


def test_measure_reports_language_subsystem_and_production(tmp_path: Path) -> None:
    files = {
        "src/zicato/core/a.py": "one\ntwo\n",
        "tests/test_a.py": "test\n",
        "README.md": "excluded\n",
        "uv.lock": "excluded\n",
    }
    for name, content in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)

    report = measure(cwd=tmp_path)

    assert (report.files, report.lines) == (2, 3)
    assert (report.production_files, report.production_lines) == (1, 2)
    assert report.languages == {"Python": 3}
    assert report.subsystems == {"src/zicato/core": 2, "tests": 1}


def test_check_rejects_one_line_total_overage(tmp_path: Path) -> None:
    config = _config(tmp_path / "budget.json")
    assert check(_report(total=11, production=5), config) == ["total: 11 exceeds 10 by 1"]


def test_check_rejects_one_line_production_overage(tmp_path: Path) -> None:
    config = _config(tmp_path / "budget.json")
    assert check(_report(total=10, production=6), config) == ["production: 6 exceeds 5 by 1"]
