from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import tools.line_budget as line_budget
from tools.line_budget import (
    EXCLUDED_FROM_BUDGET,
    LEDGER,
    ROOT,
    Lines,
    Point,
    Report,
    _excluded,
    _production,
    check,
    check_ledger,
    check_subsystem_table,
    history,
    measure,
    render_history,
    render_report,
    render_subsystem_table,
    report_json,
    write_summary,
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
    assert report.subsystems == {"src/zicato/core": Lines(2, 2, 2), "tests": Lines(1, 0, 0)}


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


CSS_FIXTURE = """/* A comment the tool holds no counter for. */

body {
  margin: 0;
}
"""

HTML_FIXTURE = """<!-- A comment the tool holds no counter for. -->
<main>
</main>
"""

PER_LANGUAGE = {
    "src/zicato/core/a.py": PYTHON_FIXTURE,
    "src/zicato/dashboard/static/a.js": JAVASCRIPT_FIXTURE,
    "crates/supervisor/src/a.rs": RUST_FIXTURE,
    "src/zicato/builder/a.css": CSS_FIXTURE,
    "src/zicato/tui/a.html": HTML_FIXTURE,
    "tests/test_a.py": "test = 0\n",
}


def test_each_subsystem_carries_its_hand_counted_three_measurements(tmp_path: Path) -> None:
    """One small file per language, each in a subsystem of its own.

    CSS and HTML have no logic counter, so their comment lines count as
    executable in the per-subsystem view exactly as in the enforced count.
    """
    report = _measure(tmp_path, PER_LANGUAGE)

    assert report.subsystems == {
        "crates/supervisor": Lines(24, 24, 5),
        "src/zicato/builder": Lines(5, 5, 5),
        "src/zicato/dashboard": Lines(9, 9, 4),
        "src/zicato/tui": Lines(3, 3, 3),
        "src/zicato/core": Lines(11, 11, 2),
        "tests": Lines(1, 0, 0),
    }
    # Production logic descending, then total descending: the Rust and CSS
    # files tie at five executable lines and the longer file leads.
    assert list(report.subsystems) == [
        "crates/supervisor",
        "src/zicato/builder",
        "src/zicato/dashboard",
        "src/zicato/tui",
        "src/zicato/core",
        "tests",
    ]


def test_subsystems_partition_the_repository_wide_measurements(tmp_path: Path) -> None:
    report = _measure(tmp_path, PER_LANGUAGE)
    lines = report.subsystems.values()

    assert sum(each.total for each in lines) == report.lines == 53
    assert sum(each.production for each in lines) == report.production_lines == 52
    assert sum(each.production_logic for each in lines) == report.production_logic_lines == 19


def test_prose_share_is_the_non_executing_share_of_production_lines() -> None:
    assert Lines(11, 11, 2).prose_share == pytest.approx(9 / 11)
    assert Lines(1, 0, 0).prose_share is None


def test_json_carries_each_subsystem_with_its_prose_share(tmp_path: Path) -> None:
    payload = report_json(_measure(tmp_path, PER_LANGUAGE))

    assert payload["production_logic_lines"] == 19
    assert payload["subsystems"]["src/zicato/core"] == {
        "total": 11,
        "production": 11,
        "production_logic": 2,
        "prose_share": 0.818,
    }
    assert payload["subsystems"]["tests"]["prose_share"] is None


def test_report_lists_every_subsystem_by_production_logic(tmp_path: Path) -> None:
    rows = render_report(_measure(tmp_path, PER_LANGUAGE)).splitlines()

    assert rows[0].split() == [
        "subsystem",
        "total",
        "production",
        "production",
        "logic",
        "prose",
        "share",
    ]
    assert rows[1].split() == ["crates/supervisor", "24", "24", "5", "79.2%"]
    assert rows[-1].split() == ["tests", "1", "0", "0"]


def test_the_ledger_table_lists_only_subsystems_holding_production_files(tmp_path: Path) -> None:
    table = render_subsystem_table(_measure(tmp_path, PER_LANGUAGE)).splitlines()

    assert table[:3] == [
        "| Subsystem | Total | Production | Production logic | Prose share |",
        "|---|---:|---:|---:|---:|",
        "| crates/supervisor | 24 | 24 | 5 | 79.2% |",
    ]
    assert not any(row.startswith("| tests ") for row in table)


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


SUBSYSTEM_DOC = """# Line budgets

## Production logic by subsystem

Prose the writer keeps.

| Subsystem | Total | Production | Production logic | Prose share |
|---|---:|---:|---:|---:|
| crates/supervisor | 24 | 24 | 5 | 79.2% |
| src/zicato/core | 11 | 11 | 2 | 81.8% |

## Ratchet policy
"""

REWRITE_HINT = "run `python tools/line_budget.py --write-summary` to rewrite the table"


def _subsystem_report(**subsystems: Lines) -> Report:
    return Report(1, 0, 1, 0, 0, {}, subsystems)


def test_the_repository_subsystem_table_is_current() -> None:
    assert check_subsystem_table(LEDGER.read_text(), measure()) == []


def test_a_current_subsystem_table_passes() -> None:
    report = _subsystem_report(
        **{"crates/supervisor": Lines(24, 24, 5)}, **{"src/zicato/core": Lines(11, 11, 2)}
    )

    assert check_subsystem_table(SUBSYSTEM_DOC, report) == []


def test_a_stale_subsystem_row_fails() -> None:
    report = _subsystem_report(
        **{"crates/supervisor": Lines(24, 24, 5)}, **{"src/zicato/core": Lines(12, 12, 3)}
    )

    assert check_subsystem_table(SUBSYSTEM_DOC, report) == [
        "subsystem table, src/zicato/core: states 11 | 11 | 2 | 81.8%, "
        "but the tree measures 12 | 12 | 3 | 75.0%",
        REWRITE_HINT,
    ]


def test_a_subsystem_without_a_row_fails() -> None:
    report = _subsystem_report(
        **{"crates/supervisor": Lines(24, 24, 5)},
        **{"src/zicato/core": Lines(11, 11, 2)},
        **{"src/zicato/tui": Lines(3, 3, 1)},
    )

    assert check_subsystem_table(SUBSYSTEM_DOC, report) == [
        "subsystem table: no row for src/zicato/tui, which measures 3 | 3 | 1 | 66.7%",
        REWRITE_HINT,
    ]


def test_a_row_naming_no_measured_subsystem_fails() -> None:
    report = _subsystem_report(**{"crates/supervisor": Lines(24, 24, 5)})

    assert check_subsystem_table(SUBSYSTEM_DOC, report) == [
        "subsystem table, src/zicato/core: no production files measure under that name",
        REWRITE_HINT,
    ]


def test_write_summary_rewrites_the_table_and_nothing_else() -> None:
    report = _subsystem_report(**{"src/zicato/core": Lines(12, 12, 3)})

    written = write_summary(SUBSYSTEM_DOC, report)

    assert written == SUBSYSTEM_DOC.replace(
        "| crates/supervisor | 24 | 24 | 5 | 79.2% |\n| src/zicato/core | 11 | 11 | 2 | 81.8% |",
        "| src/zicato/core | 12 | 12 | 3 | 75.0% |",
    )
    assert check_subsystem_table(written, report) == []


def test_write_summary_fills_a_section_holding_no_table_yet() -> None:
    report = _subsystem_report(**{"src/zicato/core": Lines(12, 12, 3)})
    empty = "## Production logic by subsystem\n\nProse.\n\n## Ratchet policy\n"

    written = write_summary(empty, report)

    assert written == (
        "## Production logic by subsystem\n\nProse.\n\n"
        "| Subsystem | Total | Production | Production logic | Prose share |\n"
        "|---|---:|---:|---:|---:|\n"
        "| src/zicato/core | 12 | 12 | 3 | 75.0% |\n\n## Ratchet policy\n"
    )


def _commit(cwd: Path, files: dict[str, str], message: str) -> str:
    for name, content in files.items():
        (cwd / name).parent.mkdir(parents=True, exist_ok=True)
        (cwd / name).write_text(content)
    run = ["git", "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*run, "add", "-A"], cwd=cwd, check=True)
    subprocess.run([*run, "commit", "-q", "-m", message], cwd=cwd, check=True)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def _recorded_reads(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record the path of every file the tool reads, in order."""
    reads: list[str] = []
    content = line_budget._content

    def read(path: str, ref: str | None, cwd: Path) -> bytes:
        reads.append(path)
        return content(path, ref, cwd)

    monkeypatch.setattr(line_budget, "_content", read)
    return reads


def _repository(tmp_path: Path) -> tuple[Path, str]:
    """A repository of two first-parent commits; returns its path and the first commit.

    The repository is a subdirectory, so a cache written beside it is never
    committed by the ``git add -A`` a later commit runs.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    first = _commit(repo, {"src/zicato/core/a.py": "one = 1\n"}, "first")
    _commit(repo, {"src/zicato/core/a.py": "one = 1\ntwo = 2\n", "tests/a.py": "t = 0\n"}, "second")
    return repo, first


def test_history_measures_each_first_parent_commit(tmp_path: Path) -> None:
    cwd, first = _repository(tmp_path)

    points = history(first, "HEAD", cwd, tmp_path / "cache.json")

    assert [(p.subject, p.subsystems["src/zicato/core"]) for p in points] == [
        ("first", Lines(1, 1, 1)),
        ("second", Lines(2, 2, 2)),
    ]
    assert points[1].subsystems["tests"] == Lines(1, 0, 0)


def test_history_reads_only_blobs_the_cache_has_not_seen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cwd, first = _repository(tmp_path)
    cache = tmp_path / "cache.json"
    history(first, "HEAD", cwd, cache)
    _commit(cwd, {"tests/a.py": "t = 1\n"}, "third")
    reads = _recorded_reads(monkeypatch)

    points = history(first, "HEAD", cwd, cache)

    assert [p.subject for p in points] == ["first", "second", "third"]
    assert reads == ["tests/a.py"]


def test_history_serves_walked_commits_without_reading_the_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cwd, first = _repository(tmp_path)
    cache = tmp_path / "cache.json"
    points = history(first, "HEAD", cwd, cache)
    monkeypatch.setattr(line_budget, "_tree", lambda ref, cwd: pytest.fail("read the tree"))

    assert history(first, "HEAD", cwd, cache) == points


def test_a_cache_written_by_other_counters_is_discarded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cwd, first = _repository(tmp_path)
    cache = tmp_path / "cache.json"
    history(first, "HEAD", cwd, cache)
    monkeypatch.setattr(line_budget, "_tool_digest", lambda: "other counters")
    reads = _recorded_reads(monkeypatch)

    history(first, "HEAD", cwd, cache)

    assert sorted(reads) == ["src/zicato/core/a.py", "src/zicato/core/a.py", "tests/a.py"]


def test_a_since_off_the_first_parent_chain_is_refused(tmp_path: Path) -> None:
    cwd, first = _repository(tmp_path)
    subprocess.run(["git", "checkout", "-q", "-b", "side", first], cwd=cwd, check=True)
    side = _commit(cwd, {"src/zicato/core/b.py": "b = 1\n"}, "side")
    subprocess.run(["git", "checkout", "-q", "main"], cwd=cwd, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@t",
            "merge",
            "-q",
            "--no-ff",
            "-m",
            "m",
            "side",
        ],
        cwd=cwd,
        check=True,
    )

    with pytest.raises(ValueError, match="not on the first-parent chain of HEAD"):
        history(side, "HEAD", cwd, tmp_path / "cache.json")


def test_history_renders_one_row_per_commit() -> None:
    points = [
        Point("abcdef0123", "2026-01-01", "first", {"src/zicato/core": Lines(3, 3, 1)}),
        Point(
            "0123abcdef",
            "2026-01-02",
            "second",
            {"src/zicato/core": Lines(4, 4, 2), "tests": Lines(1, 0, 0)},
        ),
    ]

    rows = render_history(points).splitlines()

    assert rows[0].split() == ["commit", "date", "all", "logic", "core", "subject"]
    assert rows[1].split() == ["abcdef01", "2026-01-01", "1", "1", "first"]
    assert rows[2].split() == ["0123abcd", "2026-01-02", "2", "2", "second"]
    assert render_history(points, "tests").splitlines()[1].split() == [
        "abcdef01",
        "2026-01-01",
        "1",
        "0",
        "first",
    ]
