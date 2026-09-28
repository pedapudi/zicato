"""The line-budget ledger checked end to end in a throwaway repository.

Each test copies the tool, the policy document, and the budget file into a new
repository, commits a small source tree, and runs ``tools/line_budget.py`` the
way CI does. The source files are sized so each measurement moves by a
different amount: a Python file with a docstring adds three total and
production lines and one executable line, and a test file adds one total line.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

from tools.line_budget import CONFIG, LEDGER, LEDGER_PATH, ROOT

# Written out rather than imported, so the path an entry lives at is pinned here.
ENTRIES_PATH = "docs/design/line-budget-ledger"

GIT = ("git", "-c", "user.name=t", "-c", "user.email=t@t")
# Three total and production lines, one of them executable.
SOURCE = '"""A module."""\n\nvalue = 1\n'
# A change adding SOURCE under src/ and one test line moves the measurements by these.
MOVED = (4, 3, 1)


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
    _commit(repo, {"src/zicato/core/a.py": "one = 1\n"}, "base")
    return repo


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


def _starting_total() -> int:
    return int(json.loads(CONFIG.read_text())["starting_limits"]["total"])


def test_a_change_recording_its_own_movement_passes(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
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
    assert f"limit {_starting_total() + MOVED[0]:>9,}" in budget.stdout.splitlines()[0]


def test_a_change_without_an_entry_fails_and_states_the_entry_it_needs(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    fork = _git(repo, "rev-parse", "HEAD")
    _git(repo, "switch", "-q", "-c", "change")
    _commit(repo, _change("b"), "change")

    result = _run(repo, "--check-ledger", "--base", "main")

    assert result.returncode == 1
    assert result.stderr.splitlines()[1:] == [
        f"  total: the entries this change adds record +0, but the tree moved it by +4 "
        f"since {fork[:12]}",
        f"  production: the entries this change adds record +0, but the tree moved it by +3 "
        f"since {fork[:12]}",
        f"  production_logic: the entries this change adds record +0, but the tree moved it "
        f"by +1 since {fork[:12]}",
        f"  record the change in a file under {ENTRIES_PATH}/ whose table states:",
        "    | Total | +4 |",
        "    | Production | +3 |",
        "    | Production logic | +1 |",
    ]


def test_an_entry_misstating_one_measurement_fails_on_that_measurement(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    fork = _git(repo, "rev-parse", "HEAD")
    _git(repo, "switch", "-q", "-c", "change")
    _commit(
        repo,
        {**_change("b"), f"{ENTRIES_PATH}/2026-09-27-b.md": _entry("Add b", (3, 3, 1))},
        "change",
    )

    result = _run(repo, "--check-ledger", "--base", "main")

    assert result.returncode == 1
    assert result.stderr.splitlines()[1] == (
        f"  total: the entries this change adds record +3, but the tree moved it by +4 "
        f"since {fork[:12]}"
    )
    assert len(result.stderr.splitlines()) == 6


def test_independent_changes_merge_in_either_order_without_edits(tmp_path: Path) -> None:
    """Each branch records only its own movement, so neither needs the other's numbers."""
    repo = _repository(tmp_path)
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
    assert f"limit {_starting_total() + 2 * MOVED[0]:>9,}" in budget.stdout.splitlines()[0]


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


def test_an_entry_lets_the_tree_exceed_the_starting_limit(tmp_path: Path) -> None:
    """The limit moves by what the entries record; without the entry the tree would exceed it."""
    repo = _repository(tmp_path)
    measured = int(_run(repo).stdout.split()[1].replace(",", ""))
    config = (repo / CONFIG.name).read_text()
    lowered = config.replace(f'"total": {_starting_total()}', f'"total": {measured}')
    assert lowered != config
    _write(repo, {CONFIG.name: lowered, **_change("b")})
    _git(repo, "add", "-A")

    over = _run(repo, "--check")
    _write(repo, {f"{ENTRIES_PATH}/2026-09-27-b.md": _entry("Add b", MOVED)})
    within = _run(repo, "--check")

    assert over.returncode == 1
    assert f"  total: {measured + 4:,} exceeds {measured:,} by 4" in over.stderr.splitlines()
    assert (within.returncode, within.stderr) == (0, "")
