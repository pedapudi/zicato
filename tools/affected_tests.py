"""Select the test files a change can reach, by static import graph.

The full suite is the merge gate and this tool is not wired into it. What
it shortens is the inner loop: while iterating on a change, run the tests
that could possibly see it rather than all of them.

The graph is built by parsing, never by importing, so running this tool
executes none of the code it reasons about. A test file is selected when
its transitive imports reach a changed module. Three edges the parser
cannot see from `import` statements alone are added explicitly:

* a `[sys.executable, "-m", "some.module", ...]` argv, which is how the
  tournament runner reaches the worker and the CLI reaches the dashboard
  server. The edge is read off the argv list itself, so a new subprocess
  entry point needs no change here.
* `importlib.import_module("some.module")` with a literal argument.
* a dotted path or file stem named in a test's own text, which is how a
  fixture points an adapter or a callable at a module it never imports.

Where the graph CANNOT resolve something, the answer is the whole suite
rather than a guess. That happens when a changed file dynamically imports
a name the parser cannot evaluate (a variable or an f-string), and when a
changed file is one every test depends on or one this tool cannot reason
about at all: the suite's conftest, the project or lock file, the parity
tooling, or a non-Python file whose readers are unknown.

Usage:

    python tools/affected_tests.py                 # branch and worktree changes
    python tools/affected_tests.py --range HEAD~3  # committed comparison only
    python tools/affected_tests.py --explain       # JSON, with readable reasons
    python tools/affected_tests.py --run -- -n0    # execute without shell expansion

The JSON result distinguishes selected tests, a known empty selection and
unresolved dependencies requiring full Python coverage. It records the
comparison and changed paths. The runner skips pytest for a known empty
selection and preserves pytest's exit status for every executed selection.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import subprocess
import sys
from collections.abc import Iterable, Iterator
from dataclasses import asdict, dataclass
from functools import cache
from pathlib import Path
from typing import Literal

ROOT = Path(__file__).resolve().parents[1]

#: Import roots this tool resolves. A dotted name outside them is a third
#: party or the standard library, which a change in this repository cannot
#: move, so the graph stops there.
PACKAGE_ROOTS: tuple[tuple[str, str], ...] = (
    ("zicato", "src/zicato"),
    ("zicato_examples", "examples/zicato_examples"),
    ("tests", "tests"),
    ("tools", "tools"),
)

#: A change to one of these selects the whole suite. The first three are
#: imported by every test through pytest's own machinery or the dependency
#: set; the last is the tooling that decides whether a run is even valid.
#:
#: A `tests/_*.py` harness module is absent from this list. It is a plain
#: module the graph resolves precisely, and replying with the whole suite
#: for one threw away an answer the graph already had: on the last ten
#: merged changes it was the largest single cause of a full-suite verdict.
ALWAYS_FULL_SUITE: tuple[str, ...] = (
    "tests/conftest.py",
    "pyproject.toml",
    "uv.lock",
    "tools/parity/",
)

#: Trees holding prose. Prose is never data a test reads as behaviour, so a
#: change to one of these is inert — unless a tracked module NAMES the file,
#: which `tools/line_budget.py` does for `docs/design/LINE-BUDGET.md` and
#: then reads. The naming check keeps this from rotting: a document a tool
#: starts reading stops being inert without an edit here.
PROSE_TREES: tuple[str, ...] = ("docs/", "skills/", "README.md", "CHANGELOG.md")


def _is_prose(rel: str) -> bool:
    return Path(rel).suffix.lower() in {".md", ".rst", ".txt"} and any(
        rel == tree or (tree.endswith("/") and rel.startswith(tree)) for tree in PROSE_TREES
    )


#: Non-Python files whose readers a full-path search finds COMPLETELY, so a
#: change to one is traced through the graph rather than answered with the
#: whole suite. Nothing else earns this: a module can name a path for a
#: reason that is not a read (`tools/line_budget.py` names
#: `tests/data/reader_parity_snapshot.json` only to exclude it from the
#: count), and a narrow WRONG answer is worse than a blunt right one.
#: tools/test_affected_tests.py pins each entry against its reader, so an
#: entry that stops being true fails a test rather than silently narrowing
#: a run. The budget file is here because the ratchet policy makes almost
#: every pull request touch it.
TRACED_DATA_FILES: dict[str, str] = {".line-budget.json": "tools.line_budget"}

#: Dynamic-import call targets the parser evaluates when the argument is a
#: string literal, and which force the whole suite when it is not.
DYNAMIC_IMPORT_CALLS = frozenset(
    {
        "importlib.import_module",
        "__import__",
        "import_dotted_path",
        "import_module",
    }
)


def _run_git(*args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise ValueError(f"git {' '.join(args)} failed: {completed.stderr.strip()}")
    return completed.stdout


def changed_paths(ref_range: str) -> list[str]:
    """Paths on both sides of renames, preserving spaces and shell syntax."""
    if ref_range.startswith("-") or not ref_range:
        raise ValueError("the comparison must name a revision or revision range")
    return _git_paths("diff", "--name-only", "--no-renames", ref_range, "--")


def _git_paths(*args: str) -> list[str]:
    return sorted(filter(None, _run_git(args[0], "-z", *args[1:]).split("\0")))


@dataclass(frozen=True)
class Comparison:
    """Resolved commits and the working files examined for this selection."""

    ref_range: str
    revisions: tuple[str, ...]
    head: str
    include_worktree: bool
    branch: tuple[str, ...]
    staged: tuple[str, ...]
    unstaged: tuple[str, ...]
    untracked: tuple[str, ...]
    content_digest: str

    @property
    def changed(self) -> list[str]:
        worktree = self.staged + self.unstaged + self.untracked if self.include_worktree else ()
        return sorted(set(self.branch + worktree))


@dataclass(frozen=True)
class Selection:
    """Python iteration coverage and the evidence used to choose it."""

    status: Literal["selected", "known-empty", "unresolved-full"]
    reason: str
    tests: tuple[str, ...]
    reasons: dict[str, list[str]]
    full_suite_reasons: tuple[str, ...]
    comparison: Comparison


def _comparison(ref_range: str | None) -> Comparison:
    comparison_range = ref_range or "origin/main...HEAD"
    branch = tuple(changed_paths(comparison_range))
    head = _run_git("rev-parse", "--verify", "HEAD").strip()
    revisions = tuple(_run_git("rev-parse", "--revs-only", comparison_range, "HEAD").splitlines())
    staged = tuple(_git_paths("diff", "--name-only", "--no-renames", "--cached", "--"))
    unstaged = tuple(_git_paths("diff", "--name-only", "--no-renames", "--"))
    untracked = tuple(_git_paths("ls-files", "--others", "--exclude-standard"))
    digest = hashlib.sha256()
    for rel in sorted(set(branch + staged + unstaged + untracked)):
        path = ROOT / rel
        digest.update(rel.encode() + b"\0")
        digest.update(path.read_bytes() if path.is_file() else b"<missing>")
        digest.update(b"\0")
    return Comparison(
        comparison_range,
        revisions,
        head,
        ref_range is None,
        branch,
        staged,
        unstaged,
        untracked,
        digest.hexdigest(),
    )


def _module_name(rel: str) -> str | None:
    """The dotted module a repository-relative Python path defines."""
    for package, root in PACKAGE_ROOTS:
        prefix = root + "/"
        if not rel.startswith(prefix):
            continue
        tail = rel[len(prefix) :]
        if not tail.endswith(".py"):
            return None
        parts = tail[: -len(".py")].split("/")
        if parts[-1] == "__init__":
            parts = parts[:-1]
        return ".".join([package, *parts]) if parts else package
    return None


def _module_paths() -> dict[str, str]:
    """Every dotted module this tool resolves, mapped to its path."""
    table: dict[str, str] = {}
    for _package, root in PACKAGE_ROOTS:
        for path in sorted((ROOT / root).rglob("*.py")):
            rel = path.relative_to(ROOT).as_posix()
            name = _module_name(rel)
            if name is not None:
                table[name] = rel
    return table


def _literal(node: ast.expr) -> str | None:
    """The node's value when it is a plain string literal."""
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _called_name(node: ast.Call) -> str:
    """A call's dotted callee as written, e.g. `importlib.import_module`."""
    parts: list[str] = []
    current: ast.expr = node.func
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
    return ".".join(reversed(parts))


def _subprocess_module(elements: list[ast.expr]) -> Iterator[str]:
    """Modules named by a `"-m", "<module>"` pair inside an argv sequence.

    Reading the pair off the argv is what keeps this from being a list of
    known entry points: a spawn added later is found without an edit here.
    """
    for index, element in enumerate(elements[:-1]):
        if _literal(element) == "-m":
            target = _literal(elements[index + 1])
            if target:
                yield target


class ModuleFacts:
    """What one source file imports, and what it imports unresolvably."""

    def __init__(self, imports: frozenset[str], unresolved: tuple[str, ...]) -> None:
        self.imports = imports
        #: Descriptions of dynamic imports whose target the parser could not
        #: evaluate. A change to a file with any of these selects the whole
        #: suite, because what it reaches is unknown.
        self.unresolved = unresolved


@cache
def _facts(rel: str) -> ModuleFacts:
    """Parse one file for the names it imports, statically and dynamically."""
    try:
        tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"), filename=rel)
    except (OSError, SyntaxError) as exc:
        # A file that will not parse is a file whose imports are unknown.
        return ModuleFacts(frozenset(), (f"{rel} did not parse ({exc.__class__.__name__})",))

    imports: set[str] = set()
    unresolved: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                # No package under PACKAGE_ROOTS uses relative imports; one
                # appearing later is a resolution this tool never learned.
                unresolved.append(f"{rel}:{node.lineno} relative import")
                continue
            if node.module:
                imports.add(node.module)
                imports.update(f"{node.module}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Call):
            callee = _called_name(node)
            if callee in DYNAMIC_IMPORT_CALLS or callee.endswith(".import_dotted_path"):
                target = _literal(node.args[0]) if node.args else None
                if target:
                    imports.add(target.split(":")[0])
                else:
                    unresolved.append(f"{rel}:{node.lineno} {callee}(<not a literal>)")
            else:
                # An argv reaches a spawn either as one sequence
                # (`subprocess.run([exe, "-m", mod])`) or spread across the
                # call's own arguments, which is how
                # `asyncio.create_subprocess_exec(exe, "-m", mod)` is
                # written. Both forms are read.
                imports.update(_subprocess_module(node.args))
        elif isinstance(node, ast.List | ast.Tuple):
            imports.update(_subprocess_module(node.elts))

    return ModuleFacts(frozenset(imports), tuple(unresolved))


@cache
def _path_literals(rel: str) -> frozenset[str]:
    """String literals in a file that are not its docstrings.

    Reading a file is written as a path in executable string data —
    `LEDGER_PATH = "docs/design/LINE-BUDGET.md"`. CITING a document is
    written in a docstring, and citations are everywhere: much of the
    runtime package names a chapter of the development guide in its opening
    paragraph without ever opening one. Docstrings are therefore dropped,
    and comments never reach the syntax tree to begin with.
    """
    try:
        tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"), filename=rel)
    except (OSError, SyntaxError):
        return frozenset()
    documented = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    }
    return frozenset(
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in documented
    )


def modules_naming(rel: str, table: dict[str, str]) -> set[str]:
    """Tracked modules that name a non-Python path in executable string data.

    A data file, a golden or a document reaches the suite by being READ, and
    a read is written as a path rather than an import. Searching the tracked
    sources for the path is how such a file gets an edge into the graph.

    The search is for the whole repository-relative path, and only outside
    docstrings. A bare file name matches far too much — every module whose
    prose mentions the `Makefile` would count as reading one.
    """
    return {
        module
        for module, module_rel in table.items()
        if any(rel in literal for literal in _path_literals(module_rel))
    }


def _resolve(name: str, table: dict[str, str]) -> str | None:
    """The tracked module a dotted name refers to, or its nearest package.

    `from zicato.core.types import BoardEntry` yields the name
    `zicato.core.types.BoardEntry`, which is an attribute rather than a
    module. Walking the dotted name back to its longest tracked prefix
    resolves that to `zicato.core.types`.
    """
    parts = name.split(".")
    while parts:
        candidate = ".".join(parts)
        if candidate in table:
            return candidate
        parts.pop()
    return None


def build_graph(table: dict[str, str]) -> dict[str, set[str]]:
    """Module -> the tracked modules it imports directly."""
    graph: dict[str, set[str]] = {}
    for name, rel in table.items():
        edges: set[str] = set()
        for imported in _facts(rel).imports:
            resolved = _resolve(imported, table)
            if resolved is not None and resolved != name:
                edges.add(resolved)
        graph[name] = edges
    return graph


def _closure(start: str, graph: dict[str, set[str]]) -> set[str]:
    """Every module reachable from ``start``, including itself."""
    seen = {start}
    stack = [start]
    while stack:
        for edge in graph.get(stack.pop(), ()):
            if edge not in seen:
                seen.add(edge)
                stack.append(edge)
    return seen


def test_files(table: dict[str, str]) -> list[str]:
    """Every collected test file, as repository-relative paths."""
    return sorted(
        rel
        for name, rel in table.items()
        if name.startswith(("tests.", "tools."))
        and Path(rel).name.startswith("test_")
        and not rel.startswith("tools/parity/")
    )


def python_test_paths() -> tuple[str, ...]:
    """Complete Python suite; parity owns its separate golden test modules."""
    return (
        "tests/",
        *(
            path.relative_to(ROOT).as_posix()
            for path in sorted((ROOT / "tools").rglob("test_*.py"))
            if not path.is_relative_to(ROOT / "tools/parity")
        ),
    )


def _triage(changed: Iterable[str], table: dict[str, str]) -> tuple[list[str], set[str]]:
    """Split a change into full-suite reasons and modules to trace.

    The second half carries the non-Python files too: a data file, a golden
    or a document that a tracked module NAMES enters the graph through that
    module, and one nothing names is inert if it holds prose.
    """
    reasons: list[str] = []
    modules: set[str] = set()
    for rel in changed:
        if not (ROOT / rel).exists():
            reasons.append(f"{rel} was deleted, so what depended on it cannot be read")
            continue
        if Path(rel).name == "conftest.py" or any(
            rel == entry or rel.startswith(entry) for entry in ALWAYS_FULL_SUITE
        ):
            reasons.append(f"{rel} is depended on by every test")
            continue
        if not rel.endswith(".py"):
            if _is_prose(rel) or rel in TRACED_DATA_FILES:
                modules |= modules_naming(rel, table)
            else:
                reasons.append(f"{rel} is not Python and its readers are not known")
            continue
        name = _module_name(rel)
        if name is None or name not in table:
            reasons.append(f"{rel} is Python this tool does not resolve to a module")
            continue
        modules.add(name)
        unresolved = _facts(rel).unresolved
        if unresolved:
            reasons.append(f"{rel} imports dynamically: {unresolved[0]}")
    return reasons, modules


def select(
    changed: list[str], table: dict[str, str], graph: dict[str, set[str]]
) -> tuple[list[str], dict[str, list[str]], list[str]]:
    """The test files to run, why each was picked, and any full-suite reasons.

    Returns `(selected, reasons_by_file, full_suite_reasons)`. When the
    third is non-empty the selection is the whole suite and the first two
    describe what the graph would have picked anyway.
    """
    full_suite, changed_modules = _triage(changed, table)

    # A dotted path written in a test's own text reaches a module the test
    # never imports — how a fixture names an adapter or a callable.
    changed_dotted = {name for name in changed_modules if not name.startswith("tests.")}

    reasons: dict[str, list[str]] = {}
    for rel in test_files(table):
        name = _module_name(rel)
        if name is None:
            continue
        why: list[str] = []
        if rel in changed:
            why.append("the test file itself changed")
        for module in sorted(_closure(name, graph) & changed_modules):
            if module != name:
                why.append(f"imports {module}")
        if not why:
            text = (ROOT / rel).read_text(encoding="utf-8")
            named = sorted(dotted for dotted in changed_dotted if dotted in text)
            if named:
                why.append(f"names {named[0]} in its own text")
        if why:
            reasons[rel] = why

    return sorted(reasons), reasons, full_suite


def build_selection(ref_range: str | None = None) -> Selection:
    """Choose iteration tests; an explicit range excludes worktree changes."""
    _facts.cache_clear()
    _path_literals.cache_clear()
    comparison = _comparison(ref_range)
    if not comparison.changed:
        return Selection(
            "known-empty", "No files changed in the comparison.", (), {}, (), comparison
        )
    table = _module_paths()
    selected, reasons, full_suite = select(comparison.changed, table, build_graph(table))
    for rel in comparison.untracked if comparison.include_worktree else ():
        if not _is_prose(rel):
            full_suite.append(f"{rel} is untracked and its dependencies are not established")
    if full_suite:
        status: Literal["selected", "known-empty", "unresolved-full"] = "unresolved-full"
        selected = list(python_test_paths())
        reason = "Some changed dependencies cannot be resolved; full Python coverage is required."
    else:
        status = "selected" if selected else "known-empty"
        reason = (
            "The selected tests reach changed files."
            if selected
            else "No changed file reaches a Python test."
        )
    return Selection(status, reason, tuple(selected), reasons, tuple(full_suite), comparison)


def run_selection(selection: Selection, pytest_args: Iterable[str] = ()) -> int:
    """Execute the selected Python tests with literal arguments and honest failures."""
    if selection.status not in {"selected", "known-empty", "unresolved-full"}:
        raise ValueError(f"unknown selection status: {selection.status!r}")
    if bool(selection.tests) != (selection.status != "known-empty"):
        raise ValueError("selection status and test paths disagree")
    if selection.status == "unresolved-full" and not selection.full_suite_reasons:
        raise ValueError("full coverage requires an unresolved dependency reason")
    if selection.status == "known-empty":
        print("No affected Python tests; the selection is known empty.")
        return 0
    for argument in selection.tests:
        path = Path(argument.split("::", 1)[0])
        if path.is_absolute() or ".." in path.parts or not (ROOT / path).exists():
            raise ValueError(f"selected test path does not exist in the repository: {argument!r}")
    print(f"Affected Python tests: {selection.status} ({len(selection.tests)} paths)", flush=True)
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", *pytest_args, *selection.tests], cwd=ROOT, check=False
    )
    return completed.returncode if completed.returncode >= 0 else 128 - completed.returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--range",
        default=os.environ.get("AFFECTED_TEST_RANGE") or None,
        dest="ref_range",
        help="committed comparison only (default includes origin/main...HEAD and worktree changes)",
    )
    parser.add_argument(
        "--explain",
        action="store_true",
        help="write the reason for each selected file to stderr",
    )
    parser.add_argument("--run", action="store_true", help="run the selection with pytest")
    parser.add_argument("pytest_args", nargs=argparse.REMAINDER, help="pytest arguments after --")
    args = parser.parse_args(argv)
    if args.pytest_args and not args.run:
        parser.error("pytest arguments require --run")
    try:
        selection = build_selection(args.ref_range)
        if args.explain:
            print(f"{selection.status}: {selection.reason}", file=sys.stderr)
            for reason in selection.full_suite_reasons:
                print(f"  {reason}", file=sys.stderr)
            for rel, reasons in selection.reasons.items():
                print(f"  {rel}: {'; '.join(reasons)}", file=sys.stderr)
        if args.run:
            extra = args.pytest_args[1:] if args.pytest_args[:1] == ["--"] else args.pytest_args
            return run_selection(selection, extra)
        print(json.dumps(asdict(selection), indent=2))
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
