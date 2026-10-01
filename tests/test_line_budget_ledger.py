"""The line-budget ledger checked end to end in a throwaway repository.

Each test copies the tool, the policy document, and the budget file into a new
repository, commits a small source tree, and runs ``tools/line_budget.py`` the
way CI does. The repository's first commit carries one entry that brings each
limit down to what the small tree measures, as the ledger requires of every
commit. The source files are sized so each measurement moves by a different
amount: a Python file with a docstring adds three total and production lines
and one executable line, and a test file adds one total line.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

from tools.line_budget import (
    CONFIG,
    HISTORY_DIGEST,
    LEDGER,
    LEDGER_PATH,
    ROOT,
    history_digest,
    parse_history,
)

# Written out rather than imported, so the path an entry lives at is pinned here.
ENTRIES_PATH = "docs/design/line-budget-ledger"

GIT = ("git", "-c", "user.name=t", "-c", "user.email=t@t")
# Three total and production lines, one of them executable.
SOURCE = '"""A module."""\n\nvalue = 1\n'
# A change adding SOURCE under src/ and one test line moves the measurements by these.
MOVED = (4, 3, 1)
MEASUREMENTS = ("total", "production", "production_logic")
# A path the tool excludes from every measurement. The fixture holds it with six
# lines, and a second file with three lines that the tool counts.
EXCLUDED = "tests/data/elim_states_served.json"
EXCLUSION = f'    "{EXCLUDED}",\n'
COUNTED = "tests/data/counted.json"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        [*GIT, *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def _write(repo: Path, files: dict[str, str]) -> None:
    for name, content in files.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(content)


def _commit(repo: Path, files: dict[str, str], message: str) -> str:
    _write(repo, files)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _repository(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "tools").mkdir()
    shutil.copyfile(ROOT / "tools/line_budget.py", repo / "tools/line_budget.py")
    shutil.copyfile(CONFIG, repo / CONFIG.name)
    (repo / LEDGER_PATH).parent.mkdir(parents=True)
    shutil.copyfile(LEDGER, repo / LEDGER_PATH)
    _write(
        repo,
        {
            "src/zicato/core/a.py": "one = 1\n",
            EXCLUDED: "[\n" + "1,\n" * 4 + "]\n",
            COUNTED: "[\n1\n]\n",
        },
    )
    _git(repo, "add", "-A")
    measured = _measured(repo)
    start = json.loads(CONFIG.read_text())["starting_limits"]
    deltas = tuple(measured[key] - int(start[key]) for key in MEASUREMENTS)
    _commit(repo, {f"{ENTRIES_PATH}/2026-01-01-fixture.md": _entry("Fixture", deltas)}, "base")
    return repo


def _measured(repo: Path) -> dict[str, int]:
    """The three measurements the tool prints for the repository's worktree."""
    lines = _run(repo).stdout.splitlines()[:3]
    return {
        key: int(line.split("lines")[0].split()[-1].replace(",", ""))
        for key, line in zip(MEASUREMENTS, lines, strict=True)
    }


def _entry(title: str, deltas: tuple[int, int, int], reason: str = "Why it moved.") -> str:
    total, production, logic = deltas
    return (
        f"# {title}\n\n| Measurement | Delta |\n|---|---:|\n"
        f"| Total | {total:+,} |\n| Production | {production:+,} |\n"
        f"| Production logic | {logic:+,} |\n\n{reason}\n"
    )


def _change(name: str) -> dict[str, str]:
    """A source file and a test file whose sizes move the measurements by MOVED."""
    return {f"src/zicato/core/{name}.py": SOURCE, f"tests/test_{name}.py": "x = 1\n"}


def _run(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "tools/line_budget.py", *args], cwd=repo, capture_output=True, text=True
    )


def _limit(result: subprocess.CompletedProcess[str]) -> int:
    """The total limit the tool printed beside the total measurement."""
    return int(result.stdout.splitlines()[0].split("limit")[1].replace(",", ""))


def test_a_change_recording_its_own_movement_passes(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    before = _limit(_run(repo, "--check"))
    _git(repo, "switch", "-q", "-c", "change")
    _commit(
        repo,
        {**_change("b"), f"{ENTRIES_PATH}/2026-09-27-b.md": _entry("Add b", MOVED)},
        "change",
    )

    ledger = _run(repo, "--check-ledger", "--base", "main")
    budget = _run(repo, "--check")

    assert (ledger.returncode, ledger.stderr) == (0, "")
    assert budget.returncode == 0
    assert _limit(budget) == before + MOVED[0]


def test_a_change_without_an_entry_fails_and_states_the_entry_it_needs(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    _git(repo, "switch", "-q", "-c", "change")
    _commit(repo, _change("b"), "change")
    measured = _measured(repo)

    result = _run(repo, "--check-ledger", "--base", "main")

    assert result.returncode == 1
    assert result.stderr.splitlines()[1:] == [
        f"  total: the limit is {measured['total'] - 4:,}, but the tree measures "
        f"{measured['total']:,}",
        f"  production: the limit is {measured['production'] - 3:,}, but the tree measures "
        f"{measured['production']:,}",
        f"  production_logic: the limit is {measured['production_logic'] - 1:,}, but the tree "
        f"measures {measured['production_logic']:,}",
        f"  record the change in a file under {ENTRIES_PATH}/ whose table states:",
        "    | Total | +4 |",
        "    | Production | +3 |",
        "    | Production logic | +1 |",
    ]


def test_an_entry_misstating_one_measurement_fails_on_that_measurement(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    _git(repo, "switch", "-q", "-c", "change")
    _commit(
        repo,
        {**_change("b"), f"{ENTRIES_PATH}/2026-09-27-b.md": _entry("Add b", (3, 3, 1))},
        "change",
    )
    total = _measured(repo)["total"]

    result = _run(repo, "--check-ledger", "--base", "main")

    assert result.returncode == 1
    assert result.stderr.splitlines()[1:] == [
        f"  total: the limit is {total - 1:,}, but the tree measures {total:,}",
        f"  record the change in a file under {ENTRIES_PATH}/ whose table states:",
        "    | Total | +4 |",
        "    | Production | +3 |",
        "    | Production logic | +1 |",
    ]


def test_independent_changes_merge_in_either_order_without_edits(tmp_path: Path) -> None:
    """Each branch records only its own movement, so neither needs the other's numbers."""
    repo = _repository(tmp_path)
    before = _limit(_run(repo, "--check"))
    for name in ("left", "right"):
        _git(repo, "switch", "-q", "-c", name, "main")
        _commit(
            repo,
            {**_change(name), f"{ENTRIES_PATH}/2026-09-27-{name}.md": _entry(name, MOVED)},
            name,
        )
    _git(repo, "switch", "-q", "main")
    _git(repo, "merge", "-q", "--no-ff", "--no-edit", "left")
    after_left = _git(repo, "rev-parse", "HEAD")

    _git(repo, "merge", "-q", "--no-ff", "--no-edit", "right")

    ledger = _run(repo, "--check-ledger", "--base", after_left)
    budget = _run(repo, "--check")
    assert (ledger.returncode, ledger.stderr) == (0, "")
    assert budget.returncode == 0
    assert _limit(budget) == before + 2 * MOVED[0]


def test_an_entry_already_on_the_base_may_not_be_dropped_or_altered(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    entry = f"{ENTRIES_PATH}/2026-09-27-b.md"
    _commit(repo, {**_change("b"), entry: _entry("Add b", MOVED)}, "b")
    fork = _git(repo, "rev-parse", "HEAD")
    _git(repo, "switch", "-q", "-c", "change")

    _write(repo, {entry: _entry("Add b", (5, 3, 1))})
    altered = _run(repo, "--check-ledger", "--base", "main")
    _write(repo, {entry: _entry("Add b", MOVED, "The same reason, said another way.")})
    reworded = _run(repo, "--check-ledger", "--base", "main")

    assert altered.returncode == 1
    assert (
        f"  2026-09-27-b.md (total +4, production +3, production logic +1): present at "
        f"{fork[:12]} and missing or altered here"
    ) in altered.stderr.splitlines()
    assert (reworded.returncode, reworded.stderr) == (0, "")


def test_an_entry_lets_the_tree_exceed_its_previous_limit(tmp_path: Path) -> None:
    """The limit moves by what the entries record; without the entry the tree would exceed it."""
    repo = _repository(tmp_path)
    limit = _limit(_run(repo, "--check"))
    _write(repo, _change("b"))
    _git(repo, "add", "-A")

    over = _run(repo, "--check")
    _write(repo, {f"{ENTRIES_PATH}/2026-09-27-b.md": _entry("Add b", MOVED)})
    within = _run(repo, "--check")

    assert over.returncode == 1
    assert f"  total: {limit + 4:,} exceeds {limit:,} by 4" in over.stderr.splitlines()
    assert (within.returncode, within.stderr) == (0, "")


def _retarget_exclusion(repo: Path, path: str) -> None:
    """Point the tool's exclusion of EXCLUDED at another path, keeping its line count."""
    tool = repo / "tools/line_budget.py"
    source = tool.read_text()
    assert source.count(EXCLUSION) == 1
    tool.write_text(source.replace(EXCLUSION, f'    "{path}",\n'))


def test_removing_an_exclusion_needs_an_entry_for_the_lines_it_exposes(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    limit = _limit(_run(repo, "--check"))
    _git(repo, "switch", "-q", "-c", "change")
    _retarget_exclusion(repo, "tests/data/absent.json")
    _commit(repo, {}, "count the served states")

    without = (_run(repo, "--check"), _run(repo, "--check-ledger", "--base", "main"))
    _commit(repo, {f"{ENTRIES_PATH}/2026-09-27-count.md": _entry("Count", (6, 0, 0))}, "entry")
    with_entry = (_run(repo, "--check"), _run(repo, "--check-ledger", "--base", "main"))

    assert [result.returncode for result in without] == [1, 1]
    assert f"  total: {limit + 6:,} exceeds {limit:,} by 6" in without[0].stderr.splitlines()
    assert "    | Total | +6 |" in without[1].stderr.splitlines()
    assert [(result.returncode, result.stderr) for result in with_entry] == [(0, ""), (0, "")]


def test_adding_an_exclusion_needs_an_entry_for_the_lines_it_hides(tmp_path: Path) -> None:
    """Without the rule, excluding a counted file would leave its lines as unrecorded room."""
    repo = _repository(tmp_path)
    _git(repo, "switch", "-q", "-c", "change")
    _write(repo, {EXCLUDED: ""})
    _retarget_exclusion(repo, COUNTED)
    _commit(repo, {}, "exclude the counted file")

    budget = _run(repo, "--check")
    without = _run(repo, "--check-ledger", "--base", "main")
    _commit(repo, {f"{ENTRIES_PATH}/2026-09-27-exclude.md": _entry("Exclude", (-3, 0, 0))}, "entry")
    with_entry = _run(repo, "--check-ledger", "--base", "main")

    assert budget.returncode == 0
    assert without.returncode == 1
    assert "    | Total | -3 |" in without.stderr.splitlines()
    assert (with_entry.returncode, with_entry.stderr) == (0, "")


def test_starting_limits_and_closed_rows_hold_their_values_at_the_fork(tmp_path: Path) -> None:
    """Raising a starting limit with the closed table and its digest is still refused."""
    repo = _repository(tmp_path)
    fork = _git(repo, "rev-parse", "HEAD")
    _git(repo, "switch", "-q", "-c", "change")
    start = json.loads(CONFIG.read_text())["starting_limits"]["total"]
    last = [row for row in parse_history(LEDGER.read_text())[0] if row.measurement == "total"][-1]
    baseline = json.loads(CONFIG.read_text())["baseline"]["total"]
    edits = {
        LEDGER_PATH: (
            (
                f"| {last.previous:,} | {last.delta:+,} | {last.new:,} |",
                f"| {last.previous:,} | {last.delta + 4:+,} | {last.new + 4:,} |",
            ),
            (
                f"| Total | {baseline:,} | {start:,} | {start - baseline:+,} |",
                f"| Total | {baseline:,} | {start + 4:,} | {start + 4 - baseline:+,} |",
            ),
        ),
        CONFIG.name: ((f'"total": {start}', f'"total": {start + 4}'),),
    }
    for name, pairs in edits.items():
        text = (repo / name).read_text()
        for old, new in pairs:
            assert text.count(old) == 1
            text = text.replace(old, new)
        (repo / name).write_text(text)
    digest = history_digest(parse_history((repo / LEDGER_PATH).read_text())[0])
    tool = (repo / "tools/line_budget.py").read_text()
    assert tool.count(HISTORY_DIGEST) == 1
    (repo / "tools/line_budget.py").write_text(tool.replace(HISTORY_DIGEST, digest))
    _commit(repo, {"tests/four.txt": "1\n2\n3\n4\n"}, "four lines with no entry")

    result = _run(repo, "--check-ledger", "--base", "main")

    assert result.returncode == 1
    assert result.stderr.splitlines()[1:] == [
        f"  starting limits: .line-budget.json differs from {fork[:12]}; a limit moves only "
        f"by a ledger entry",
        f"  the closed table under '## Changes recorded with running totals' differs from "
        f"{fork[:12]}",
    ]


def test_a_directory_inside_the_ledger_is_refused_by_name(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    _commit(repo, {f"{ENTRIES_PATH}/nested/2026-09-27-b.md": _entry("Add b", (0, 0, 0))}, "nest")

    worktree = _run(repo, "--check-ledger")
    at_ref = _run(repo, "--check", "--ref", "HEAD")

    message = f"  {ENTRIES_PATH}/nested: the ledger holds only entry files"
    assert (worktree.returncode, worktree.stderr.splitlines()[1:]) == (1, [message])
    assert (at_ref.returncode, at_ref.stderr.splitlines()[1:]) == (1, [message])


# A function body whose last statement becomes a docstring once both statements
# above it are gone. Deleting either statement alone moves each measurement by
# -1; deleting both moves the logic count by -3, because "marker" stops executing.
NON_ADDITIVE = 'def f() -> None:\n    x = 0\n\n    y = 0\n    "marker"\n'


def _unbalanced_base(repo: Path) -> None:
    """Merge two changes that each record their own movement and together miss one logic line."""
    _commit(repo, {"src/zicato/core/m.py": NON_ADDITIVE}, "function")
    _commit(repo, {f"{ENTRIES_PATH}/2026-09-26-m.md": _entry("Add m", (5, 5, 4))}, "m entry")
    for name, line in (("left", "    x = 0\n"), ("right", "    y = 0\n")):
        _git(repo, "switch", "-q", "-c", name, "main")
        source = NON_ADDITIVE.replace(line, "")
        entry = _entry(name, (-1, -1, -1))
        _commit(
            repo,
            {"src/zicato/core/m.py": source, f"{ENTRIES_PATH}/2026-09-27-{name}.md": entry},
            name,
        )
        assert _run(repo, "--check-ledger", "--base", "main").returncode == 0
    _git(repo, "switch", "-q", "main")
    _git(repo, "merge", "-q", "--no-ff", "--no-edit", "left")
    _git(repo, "merge", "-q", "--no-ff", "--no-edit", "right")


def test_two_balanced_changes_can_merge_into_an_unbalanced_base(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    _unbalanced_base(repo)
    logic = _measured(repo)["production_logic"]

    result = _run(repo, "--check-ledger")

    assert result.returncode == 1
    assert result.stderr.splitlines()[1] == (
        f"  production_logic: the limit is {logic + 1:,}, but the tree measures {logic:,}"
    )


def test_a_change_is_not_told_to_record_its_bases_imbalance(tmp_path: Path) -> None:
    """The base's gap is reported once, apart from the table for the change."""
    repo = _repository(tmp_path)
    _unbalanced_base(repo)
    fork = _git(repo, "rev-parse", "HEAD")
    _git(repo, "switch", "-q", "-c", "tests-only")
    _commit(repo, {"tests/ten.txt": "x\n" * 10}, "ten test lines")
    base_line = (
        f"  the base {fork[:12]} is off its limits by total +0, production +0, production "
        "logic -1; record that difference in a change of its own"
    )

    without = _run(repo, "--check-ledger", "--base", "main")
    _commit(repo, {f"{ENTRIES_PATH}/2026-09-28-ten.md": _entry("Ten", (10, 0, 0))}, "entry")
    with_entry = _run(repo, "--check-ledger", "--base", "main")

    assert without.stderr.splitlines()[-5:] == [
        base_line,
        f"  record the change in a file under {ENTRIES_PATH}/ whose table states:",
        "    | Total | +10 |",
        "    | Production | +0 |",
        "    | Production logic | +0 |",
    ]
    assert with_entry.returncode == 1
    assert with_entry.stderr.splitlines()[-1] == base_line


def test_one_change_of_its_own_rebalances_the_base(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    _unbalanced_base(repo)
    _git(repo, "switch", "-q", "-c", "rebalance")
    entry = _entry("Merged docstring", (0, 0, -1), "Two merged deletions made a docstring.")
    _commit(repo, {f"{ENTRIES_PATH}/2026-09-28-merged-docstring.md": entry}, "rebalance")

    result = _run(repo, "--check-ledger", "--base", "main")

    assert (result.returncode, result.stderr) == (0, "")


def test_a_base_missing_from_the_clone_is_named(tmp_path: Path) -> None:
    repo = _repository(tmp_path)

    result = _run(repo, "--check-ledger", "--base", "0" * 40)

    assert result.returncode == 1
    assert result.stderr.splitlines()[1] == (
        f"  --base {'0' * 40} is not a commit sharing history with HEAD in this clone; "
        "fetch it or name another base"
    )
