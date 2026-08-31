from __future__ import annotations

import json
import subprocess
from pathlib import Path

from tools.line_budget import (
    EXCLUDED_FROM_BUDGET,
    LEDGER,
    ROOT,
    Report,
    _excluded,
    _production,
    check,
    check_ledger,
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


RUST_FIXTURE = """//! Module doc comment.

use std::fmt; // A trailing comment leaves the line executable.

const SHAPE: &str = r#"{"key": "value"}"#;

/* Block comment
   /* nested */ over two lines. */
/// Doc comment on the item.
pub fn shape() -> &'static str {
    /* A block ending before code. */ "a { balanced } pair in a string"
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn shape_keeps_its_braces() {
        let closer = "} alone in a string";
        assert_eq!(shape(), "a { balanced } pair in a string");
        assert!(!closer.is_empty() && !SHAPE.is_empty());
    }
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


def test_rust_comments_and_test_modules_stay_out_of_the_logic_count(tmp_path: Path) -> None:
    report = _measure(tmp_path, {"crates/supervisor/src/a.rs": RUST_FIXTURE})

    assert report.production_lines == 24
    # The declaration, the constant, and the three lines of the function body.
    assert report.production_logic_lines == 5


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


LEDGER_FIXTURE = """# Line budgets

## Measurement contract

| Measurement | Baseline (`f9052dd`) | Enforced limit | Limit minus baseline |
|---|---:|---:|---:|
| Total | 100 | 114 | +14 |
| Production | 50 | 55 | +5 |
| Production logic | 30 | 34 | +4 |

## Ratchet policy

Prose the parser walks past.

## Deliberate increases

| Change | Previous | Delta | New | Reason |
|---|---:|---:|---:|---|
| First change (total) | 100 | +10 | 110 | The reason it was worth ten lines. |
| First change (production) | 50 | +5 | 55 | The reason it was worth five lines. |
| First change (production logic) | 30 | +4 | 34 | The reason it was worth four lines. |
| Second change (total) | 108 | +6 | 114 | A reduction to 108 landed between the two rows. |
"""

LEDGER_LIMITS = {"total": 114, "production": 55, "logic": 34}


def _ledger_config(tmp_path: Path) -> Path:
    return _config(tmp_path / "ledger-budget.json", **LEDGER_LIMITS)


def test_the_repository_ledger_passes_its_own_check() -> None:
    assert check_ledger(LEDGER.read_text()) == []


def test_a_well_formed_ledger_passes(tmp_path: Path) -> None:
    assert check_ledger(LEDGER_FIXTURE, config_path=_ledger_config(tmp_path)) == []


def test_a_row_whose_delta_misses_its_new_value_fails(tmp_path: Path) -> None:
    broken = LEDGER_FIXTURE.replace("| 50 | +5 | 55 |", "| 50 | +5 | 57 |")

    errors = check_ledger(broken, config_path=_ledger_config(tmp_path))

    assert errors == ["First change (production) 50 +5 57: the sum is 55"]


def test_a_row_starting_above_the_preceding_row_fails(tmp_path: Path) -> None:
    """A start above the last recorded value means a row was dropped or invented."""
    broken = LEDGER_FIXTURE.replace("| 108 | +6 | 114 |", "| 120 | +6 | 126 |")

    errors = check_ledger(broken, config_path=_ledger_config(tmp_path))

    assert errors == [
        "Second change (total) 120 +6 126: starts above the 110 the preceding row reached"
    ]


def test_a_row_the_base_records_may_not_leave_the_table(tmp_path: Path) -> None:
    rows = LEDGER_FIXTURE.splitlines(keepends=True)
    trimmed = "".join(row for row in rows if not row.startswith("| First change (total) |"))

    errors = check_ledger(trimmed, LEDGER_FIXTURE, _ledger_config(tmp_path))

    assert errors == [
        "First change (total) 100 +10 110: present in the base ledger and missing here"
    ]


def test_a_reworded_reason_keeps_the_row(tmp_path: Path) -> None:
    reworded = LEDGER_FIXTURE.replace(
        "The reason it was worth five lines.", "The same five lines, said another way."
    )

    assert check_ledger(reworded, LEDGER_FIXTURE, _ledger_config(tmp_path)) == []


def test_a_measurement_whose_last_row_sits_below_its_limit_fails(tmp_path: Path) -> None:
    """Raising the limit without a row is caught twice over.

    The ledger rule names the unrecorded increase; the summary rule names the
    table left behind, because raising a limit means the table above the ledger
    now states a value the config does not hold.
    """
    config = _config(tmp_path / "raised.json", total=200, production=55, logic=34)

    errors = check_ledger(LEDGER_FIXTURE, config_path=config)

    assert errors == [
        "total: the last row reaches 114, below the enforced 200",
        "summary table, Total: states the limit 114, but .line-budget.json holds 200",
    ]


def test_a_summary_row_stating_a_limit_the_config_does_not_hold_fails(tmp_path: Path) -> None:
    """The defect this rule exists for: the config moved, the table did not."""
    stale = LEDGER_FIXTURE.replace("| Production | 50 | 55 | +5 |", "| Production | 50 | 51 | +1 |")

    errors = check_ledger(stale, config_path=_ledger_config(tmp_path))

    assert errors == [
        "summary table, Production: states the limit 51, but .line-budget.json holds 55"
    ]


def test_a_summary_row_whose_last_column_misses_the_difference_fails(tmp_path: Path) -> None:
    broken = LEDGER_FIXTURE.replace("| Total | 100 | 114 | +14 |", "| Total | 100 | 114 | +15 |")

    errors = check_ledger(broken, config_path=_ledger_config(tmp_path))

    assert errors == [
        "summary table, Total: the last column states +15, "
        "but the limit minus the baseline is +14"
    ]


def test_deleting_a_summary_row_is_not_a_way_to_pass(tmp_path: Path) -> None:
    rows = LEDGER_FIXTURE.splitlines(keepends=True)
    trimmed = "".join(row for row in rows if not row.startswith("| Production logic |"))

    errors = check_ledger(trimmed, config_path=_ledger_config(tmp_path))

    assert errors == ["summary table: no readable 'Production logic' row"]
