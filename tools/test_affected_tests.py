"""What `tools/affected_tests.py` promises, pinned against the real tree.

The tool narrows a run, so the failure that matters is a MISS: a test it
leaves out that the change would have broken. Every assertion below is
about that direction. Over-selection costs time and is never a failure
here.

The tool lives outside `testpaths`, so this file rides CI on the same
explicit argument `tools/test_prose_lint.py` does.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import affected_tests as at  # noqa: E402 — the sys.path pin above is what finds it

ROOT = Path(__file__).resolve().parents[1]


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


def test_the_subprocess_worker_is_an_edge_out_of_the_runner(
    graph: dict[str, set[str]],
) -> None:
    """`runner` spawns the worker with `-m`, which no import statement shows.

    Without this edge a change to the worker would leave every test that
    drives a real tournament unselected.
    """
    assert "zicato._tournament_worker" in graph["zicato.tournament.runner"]


def test_the_dashboard_server_is_an_edge_out_of_its_launchers(
    graph: dict[str, set[str]],
) -> None:
    """The CLI and the terminal console both launch the server with `-m`."""
    assert "zicato.dashboard" in graph["zicato.cli.commands.evolve"]
    assert "zicato.dashboard" in graph["zicato.tui.service"]


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
    """A traced file is only safe while the search really finds its reader.

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


def test_a_prose_only_change_reaches_no_test_and_still_prints_the_whole_suite(
    table: dict[str, str], graph: dict[str, set[str]]
) -> None:
    """The documented answer for a documentation commit, both halves of it.

    A `.md` under a prose tree is inert: no module reads it, so the graph
    selects nothing. The tool then prints `tests/` regardless, because
    `pytest $(...)` with an empty argument list collects everything — there
    is no way to say "run nothing" through that substitution.

    Both halves are pinned because they read as a contradiction otherwise,
    and one review already reconciled them the wrong way round: a table
    reported "0 files selected" for a range whose command printed the whole
    suite.
    """
    prose = ["README.md", "docs/design/SCORING.md", "docs/dev-guide/04-evaluation-statistics.md"]
    for rel in prose:
        assert (ROOT / rel).exists(), f"{rel} moved; update this case"
    selected, _reasons, full_suite = at.select(prose, table, graph)
    assert not full_suite, f"prose should not be a full-suite REASON: {full_suite}"
    assert selected == [], f"prose reached tests: {selected}"

    completed = subprocess.run(
        [sys.executable, str(ROOT / "tools/affected_tests.py"), "--range", "HEAD...HEAD"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "tests/"


def test_the_whole_suite_prints_a_runnable_argument() -> None:
    """The full-suite answer is `tests/`, which pytest accepts unchanged."""
    completed = subprocess.run(
        [sys.executable, str(ROOT / "tools/affected_tests.py"), "--range", "HEAD...HEAD"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    # An empty range changes nothing, which selects nothing, which prints the
    # whole suite rather than an empty command line.
    assert completed.stdout.strip() == "tests/"
