from __future__ import annotations

import json
import subprocess
from pathlib import Path

from tools.line_budget import (
    EXCLUDED_FROM_BUDGET,
    ROOT,
    Report,
    _excluded,
    _production,
    check,
    measure,
)

PYTHON_FIXTURE = '''"""Module docstring.

Second line of it.
"""

# A standing comment.


def run() -> int:
    """One-line docstring."""
    return 1  # A trailing comment leaves the line executable.
'''

JAVASCRIPT_FIXTURE = """// Entry comment.
/* Block comment
   over two lines. */
const x = 1; // A trailing comment leaves the line executable.

/* A whole block on one line. */
export function go() {
  return x;
}
"""


def _report(*, total: int, production: int, logic: int = 0) -> Report:
    return Report(1, total, 1, production, logic, {}, {})


def _config(path: Path, *, total: int = 10, production: int = 5, logic: int = 3) -> Path:
    path.write_text(
        json.dumps(
            {
                "limits": {"total": total, "production": production, "production_logic": logic},
            }
        )
    )
    return path


def _measure(tmp_path: Path, files: dict[str, str]) -> Report:
    """Measure a throwaway repository holding exactly the given files."""
    for name, content in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    return measure(cwd=tmp_path)


def test_exclusions_are_narrow_and_explicit() -> None:
    assert _excluded("README.md")
    assert _excluded("Cargo.lock")
    assert _excluded("docs/presentation/slides/slide-01.svg")
    assert not _excluded("src/zicato/core/scoring.py")
    assert not _excluded("tests/test_scoring.py")


def test_every_exclusion_names_a_tracked_path() -> None:
    listing = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, check=True, capture_output=True, text=True
    ).stdout.splitlines()
    for item in EXCLUDED_FROM_BUDGET:
        assert any(path == item or path.startswith(item) for path in listing), item


def test_production_is_independent_of_tests_and_assets() -> None:
    assert _production("src/zicato/core/scoring.py")
    assert _production("crates/supervisor/src/main.rs")
    assert not _production("tests/test_scoring.py")
    assert not _production("src/zicato/dashboard/static/test/core.test.mjs")
    assert not _production("src/zicato/dashboard/static/brand/wordmark.svg")


def test_measure_reports_language_subsystem_and_production(tmp_path: Path) -> None:
    report = _measure(
        tmp_path,
        {
            "src/zicato/core/a.py": "one = 1\ntwo = 2\n",
            "tests/test_a.py": "test = 0\n",
            "README.md": "excluded\n",
            "uv.lock": "excluded\n",
        },
    )

    assert (report.files, report.lines) == (2, 3)
    assert (report.production_files, report.production_lines) == (1, 2)
    assert report.languages == {"Python": 3}
    assert report.subsystems == {"src/zicato/core": 2, "tests": 1}


def test_python_docstrings_and_comments_stay_out_of_the_logic_count(tmp_path: Path) -> None:
    report = _measure(tmp_path, {"src/zicato/core/a.py": PYTHON_FIXTURE})

    assert report.production_lines == 11
    assert report.production_logic_lines == 2


def test_javascript_comments_stay_out_of_the_logic_count(tmp_path: Path) -> None:
    report = _measure(tmp_path, {"src/zicato/dashboard/static/a.js": JAVASCRIPT_FIXTURE})

    assert report.production_lines == 9
    assert report.production_logic_lines == 4


def test_file_types_without_a_counter_keep_their_raw_count(tmp_path: Path) -> None:
    report = _measure(tmp_path, {"src/zicato/dashboard/static/a.css": "/* c */\nbody { }\n"})

    assert (report.production_lines, report.production_logic_lines) == (2, 2)


def test_check_rejects_one_line_total_overage(tmp_path: Path) -> None:
    config = _config(tmp_path / "budget.json")
    assert check(_report(total=11, production=5), config) == ["total: 11 exceeds 10 by 1"]


def test_check_rejects_one_line_production_overage(tmp_path: Path) -> None:
    config = _config(tmp_path / "budget.json")
    assert check(_report(total=10, production=6), config) == ["production: 6 exceeds 5 by 1"]


def test_check_rejects_one_line_production_logic_overage(tmp_path: Path) -> None:
    config = _config(tmp_path / "budget.json")
    assert check(_report(total=10, production=5, logic=4), config) == [
        "production_logic: 4 exceeds 3 by 1"
    ]
