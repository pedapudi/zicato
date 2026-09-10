"""What `tools/affected_tests.py` promises, pinned against the real tree.

The tool narrows a run, so the failure that matters is a MISS: a test it
leaves out that the change would have broken. Every assertion below is
about that direction. Over-selection costs time and is never a failure
here.

The tool lives outside `testpaths`, so this file rides CI on the same
explicit argument `tools/test_prose_lint.py` does.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import affected_tests as at  # noqa: E402 — the sys.path pin above is what finds it

ROOT = Path(__file__).resolve().parents[1]


def test_empty_committed_range_has_an_explicit_empty_result() -> None:
    completed = subprocess.run(
        [sys.executable, str(ROOT / "tools/affected_tests.py"), "--range", "HEAD...HEAD"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result["status"] == "known-empty"
    assert result["tests"] == []


@pytest.fixture(scope="module")
def table() -> dict[str, str]:
    return at._module_paths()


@pytest.fixture(scope="module")
def graph(table: dict[str, str]) -> dict[str, set[str]]:
    return at.build_graph(table)


# ---------------------------------------------------------------------------
# The escape hatches: a change the graph cannot narrow selects everything.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "changed",
    [
        pytest.param("tests/conftest.py", id="the suite root"),
        pytest.param("pyproject.toml", id="the project file"),
        pytest.param("uv.lock", id="the lock file"),
        pytest.param("tools/parity/lib/normalize.py", id="the parity tooling"),
        pytest.param("src/zicato/dashboard/static/js/core/api.js", id="a front-end source"),
    ],
)
def test_an_unnarrowable_change_selects_the_whole_suite(
    changed: str, table: dict[str, str], graph: dict[str, set[str]]
) -> None:
    """Each of these reaches tests by a route the import graph cannot see."""
    assert (ROOT / changed).exists(), f"{changed} moved; update this case"
    _selected, _reasons, full_suite = at.select([changed], table, graph)
    assert full_suite, f"{changed} was narrowed, but nothing establishes what it reaches"


def test_a_harness_module_is_narrowed_by_the_graph(
    table: dict[str, str], graph: dict[str, set[str]]
) -> None:
    """A `tests/_*.py` module is resolved by the graph.

    It is a plain Python module that every one of its importers names in an
    `import` statement, so the graph gives a precise answer. Replying with the
    whole suite instead was the largest single cause of a full-suite
    verdict across the last ten merged changes.
    """
    changed = "tests/_orchestrator_harness.py"
    assert (ROOT / changed).exists(), f"{changed} moved; update this case"
    selected, _reasons, full_suite = at.select([changed], table, graph)
    assert not full_suite, f"a harness module was not narrowed: {full_suite}"
    importers = {
        rel
        for rel in at.test_files(table)
        if "tests._orchestrator_harness" in graph[at._module_name(rel) or ""]
    }
    assert importers, "nothing imports the orchestrator harness; pick another module"
    assert importers <= set(selected)
    assert len(selected) < len(at.test_files(table))


def test_a_deleted_file_selects_the_whole_suite(
    table: dict[str, str], graph: dict[str, set[str]]
) -> None:
    """What depended on a file that is gone cannot be read off the tree."""
    _selected, _reasons, full_suite = at.select(
        ["src/zicato/core/a_module_this_change_deleted.py"], table, graph
    )
    assert full_suite


def test_a_dynamic_import_the_parser_cannot_evaluate_selects_the_whole_suite(
    tmp_path: Path,
) -> None:
    """A variable module name reaches anything, so it narrows to nothing."""
    facts = at._facts("src/zicato/adapter_factory.py")
    assert facts.unresolved, (
        "adapter_factory resolves its adapter by a name computed at runtime; "
        "if that stopped being true, pick another file for this case"
    )
    del tmp_path


# ---------------------------------------------------------------------------
# The edges the parser adds beyond `import` statements.
# ---------------------------------------------------------------------------


def test_subprocess_dependency_is_recorded_for_worker_execution(
    graph: dict[str, set[str]],
) -> None:
    """Worker execution launches a subprocess with `-m`, outside Python imports.

    Without this edge a change to the worker would leave every test that
    drives a real tournament unselected.
    """
    assert "zicato._tournament_worker" in graph["zicato.tournament.worker_execution"]


def test_the_dashboard_server_is_an_edge_out_of_its_launchers(
    graph: dict[str, set[str]],
) -> None:
    """The CLI launches the server with `-m`."""
    assert "zicato.dashboard" in graph["zicato.cli.commands.evolve"]


def test_a_literal_dynamic_import_is_an_edge(graph: dict[str, set[str]]) -> None:
    """`importlib.import_module("zicato.index.query")` is a resolvable edge."""
    assert "zicato.index.query" in graph["zicato.query.eval_view"]


def test_a_path_named_in_prose_is_not_read(table: dict[str, str]) -> None:
    """A docstring citing a chapter must not count as reading it.

    Most of the runtime package cites the development guide. Counting a
    citation as a read made a documentation-only change select 324 of the
    358 test files, which is the failure this rule exists to stop.
    """
    named = at.modules_naming("docs/dev-guide/11-testing.md", table)
    # This file names the chapter in the line above, which is a read as far
    # as the search can tell. The claim is about the runtime package.
    assert {module for module in named if module.startswith("zicato")} == set()


def test_a_path_named_in_executable_string_data_is_read(table: dict[str, str]) -> None:
    """`LEDGER_PATH = "docs/design/LINE-BUDGET.md"` is a read, and is found."""
    assert "tools.line_budget" in at.modules_naming("docs/design/LINE-BUDGET.md", table)


@pytest.mark.parametrize("path,reader", sorted(at.TRACED_DATA_FILES.items()))
def test_every_traced_data_file_still_has_the_reader_it_claims(
    path: str, reader: str, table: dict[str, str]
) -> None:
    """A traced file is only safe while the search still finds its reader.

    These are the non-Python files answered through the graph rather than
    with the whole suite. If one stops being named by the module named
    here, the search would return a set that is narrow AND wrong, so this
    fails instead.
    """
    assert (ROOT / path).exists(), f"{path} moved; update TRACED_DATA_FILES"
    assert reader in at.modules_naming(path, table)


# ---------------------------------------------------------------------------
# The property that makes the tool safe to use at all.
# ---------------------------------------------------------------------------


def test_a_changed_test_file_selects_itself(
    table: dict[str, str], graph: dict[str, set[str]]
) -> None:
    selected, reasons, full_suite = at.select(["tests/test_core_types.py"], table, graph)
    assert not full_suite
    assert "tests/test_core_types.py" in selected
    assert reasons["tests/test_core_types.py"] == ["the test file itself changed"]


def test_a_changed_module_selects_the_tests_that_import_it(
    table: dict[str, str], graph: dict[str, set[str]]
) -> None:
    """The direct importers of a module are always in its selection."""
    changed = "src/zicato/selection/evidence_gate.py"
    selected, _reasons, full_suite = at.select([changed], table, graph)
    assert not full_suite
    importers = {
        rel
        for rel in at.test_files(table)
        if "zicato.selection.evidence_gate" in graph[at._module_name(rel) or ""]
    }
    assert importers, "no test imports the evidence gate directly; pick another module"
    assert importers <= set(selected)


def test_the_selection_is_a_subset_of_the_suite(
    table: dict[str, str], graph: dict[str, set[str]]
) -> None:
    """Whatever is printed is runnable: every entry is a collected test file."""
    selected, _reasons, _full = at.select(
        ["src/zicato/core/types.py", "tests/test_core_types.py"], table, graph
    )
    assert set(selected) <= set(test_paths := set(at.test_files(table)))
    assert all((ROOT / rel).exists() for rel in test_paths)


def test_a_prose_only_change_reaches_no_test(
    table: dict[str, str], graph: dict[str, set[str]]
) -> None:
    prose = ["README.md", "docs/design/SCORING.md", "docs/dev-guide/04-evaluation-statistics.md"]
    selected, _reasons, full_suite = at.select(prose, table, graph)
    assert not full_suite
    assert selected == []


@pytest.fixture
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    files = {
        "README.md": "Description.\n",
        "src/zicato/unit.py": "VALUE = 1\n",
        "src/zicato/worker.py": "VALUE = 1\n",
        "src/zicato/loader.py": 'import importlib\nimportlib.import_module("zicato.unit")\n',
        "tests/_fixture.py": "from zicato import unit\n",
        "tests/test_unit.py": "from tests import _fixture\n",
        "tests/test_loader.py": "from zicato import loader\n",
        "tests/test_worker.py": 'ARGS = ["python", "-m", "zicato.worker"]\n',
        "tools/test_tool.py": "def test_tool(): pass\n",
    }
    for rel, content in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "add", ".")
    git(
        tmp_path,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "-c",
        "core.hooksPath=/dev/null",
        "commit",
        "-qm",
        "Fixture repository",
    )
    git(tmp_path, "update-ref", "refs/remotes/origin/main", "HEAD")
    monkeypatch.setattr(at, "ROOT", tmp_path)
    at._facts.cache_clear()
    at._path_literals.cache_clear()
    yield tmp_path
    at._facts.cache_clear()
    at._path_literals.cache_clear()


def git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=root, text=True, capture_output=True, check=True)
    return result.stdout


def test_default_combines_branch_staged_unstaged_and_untracked(repository: Path) -> None:
    (repository / "src/zicato/worker.py").write_text("VALUE = 2\n")
    git(repository, "add", ".")
    git(
        repository,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "-c",
        "core.hooksPath=/dev/null",
        "commit",
        "-qm",
        "Change worker",
    )
    (repository / "src/zicato/unit.py").write_text("VALUE = 2\n")
    git(repository, "add", ".")
    (repository / "tests/_fixture.py").write_text("from zicato import unit\nVALUE = 3\n")
    (repository / "docs").mkdir()
    (repository / "docs/note.md").write_text("Description.\n")
    selection = at.build_selection()
    assert selection.status == "selected"
    assert set(selection.tests) == {
        "tests/test_unit.py",
        "tests/test_loader.py",
        "tests/test_worker.py",
    }
    state = selection.comparison
    assert state.branch == ("src/zicato/worker.py",)
    assert state.staged == ("src/zicato/unit.py",)
    assert state.unstaged == ("tests/_fixture.py",)
    assert state.untracked == ("docs/note.md",)
    assert state.head == git(repository, "rev-parse", "HEAD").strip()
    assert state.revisions
    assert state.include_worktree
    assert len(state.content_digest) == 64

    committed = at.build_selection("origin/main...HEAD")
    assert committed.tests == ("tests/test_worker.py",)
    assert not committed.comparison.include_worktree
    assert committed.comparison.changed == ["src/zicato/worker.py"]
    assert committed.comparison.staged == state.staged
    assert committed.comparison.unstaged == state.unstaged
    assert committed.comparison.untracked == state.untracked


def test_prose_only_changes_are_known_empty_and_do_not_launch_pytest(
    repository: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (repository / "README.md").write_text("Updated description.\n")
    selection = at.build_selection()
    assert selection.status == "known-empty"
    assert selection.comparison.changed == ["README.md"]

    def unexpected(*args: object, **kwargs: object) -> None:
        pytest.fail("a known empty selection launched a process")

    monkeypatch.setattr(at.subprocess, "run", unexpected)
    assert at.run_selection(selection) == 0
    assert "known empty" in capsys.readouterr().out


@pytest.mark.parametrize(
    "change", ["delete", "rename", "untracked", "unknown", "docs-binary", "prose-prefix", "syntax"]
)
def test_unresolved_changes_select_full_python_coverage(repository: Path, change: str) -> None:
    source = repository / "src/zicato/unit.py"
    if change == "delete":
        source.unlink()
    elif change == "rename":
        source.rename(source.with_name("renamed.py"))
    elif change == "untracked":
        (repository / "src/zicato/new_module.py").write_text("VALUE = 1\n")
    elif change == "unknown":
        (repository / "settings.bin").write_bytes(b"unknown")
        git(repository, "add", ".")
    elif change == "docs-binary":
        (repository / "docs").mkdir()
        (repository / "docs/data.bin").write_bytes(b"unknown")
    elif change == "prose-prefix":
        (repository / "README.md.data").write_bytes(b"unknown")
    else:
        source.write_text("invalid python !\n")
    selection = at.build_selection()
    assert selection.status == "unresolved-full"
    assert selection.tests == ("tests/", "tools/test_tool.py")
    assert selection.full_suite_reasons
    if change == "rename":
        assert "src/zicato/unit.py" in selection.comparison.unstaged
        assert "src/zicato/renamed.py" in selection.comparison.untracked


def test_staged_renames_keep_the_deleted_path(repository: Path) -> None:
    git(repository, "mv", "src/zicato/unit.py", "src/zicato/renamed.py")
    selection = at.build_selection()
    assert selection.status == "unresolved-full"
    assert selection.comparison.staged == ("src/zicato/renamed.py", "src/zicato/unit.py")


def test_invalid_base_fails_visibly(repository: Path) -> None:
    with pytest.raises(ValueError, match="git diff"):
        at.build_selection("missing-base...HEAD")
    git(repository, "update-ref", "-d", "refs/remotes/origin/main")
    with pytest.raises(ValueError, match="git diff"):
        at.build_selection()


def test_changed_tool_tests_select_themselves(repository: Path) -> None:
    (repository / "tools/test_tool.py").write_text("def test_tool(): assert True\n")
    assert at.build_selection().tests == ("tools/test_tool.py",)


def test_arguments_are_literal_and_checker_failure_is_preserved(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = "tests/test_name; $(touch marker).py"
    (repository / path).write_text("def test_example(): pass\n")
    git(repository, "add", ".")
    selection = at.build_selection()
    assert selection.status == "selected"
    assert selection.tests == (path,)
    calls = []

    def record(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 7)

    monkeypatch.setattr(at.subprocess, "run", record)
    keyword = "name or $(touch marker)"
    assert at.run_selection(selection, ["-k", keyword]) == 7
    assert calls == [
        ([sys.executable, "-m", "pytest", "-k", keyword, path], {"cwd": repository, "check": False})
    ]
    assert not (repository / "marker").exists()


def test_missing_tests_and_malformed_selection_fail_before_execution(repository: Path) -> None:
    from dataclasses import replace

    empty = at.build_selection()
    with pytest.raises(ValueError, match="status and test paths"):
        at.run_selection(replace(empty, status="selected"))
    with pytest.raises(ValueError, match="does not exist"):
        at.run_selection(replace(empty, status="selected", tests=("tests/missing.py",)))
    with pytest.raises(ValueError, match="unknown selection status"):
        at.run_selection(replace(empty, status="invalid"))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="unresolved dependency reason"):
        at.run_selection(replace(empty, status="unresolved-full", tests=("tests/",)))


def test_selection_refreshes_file_facts_in_one_process(repository: Path) -> None:
    path = repository / "src/zicato/unit.py"
    path.write_text("VALUE = 2\n")
    first = at.build_selection()
    path.write_text("import importlib\nimportlib.import_module(variable)\n")
    second = at.build_selection()
    assert first.status == "selected"
    assert second.status == "unresolved-full"
    assert first.comparison.content_digest != second.comparison.content_digest
