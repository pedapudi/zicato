"""One owner for record enumeration, pinned structurally.

"Which generations / board-entry runs / rounds does this epoch hold" is
answered by :mod:`zicato.workspace.reads` and nowhere else. That claim is
easy to state and easy to lose: any module can build
``epochs/{id}/generations`` and call ``iterdir()`` on it, and thirty-nine
sites across twenty-four modules did — sorting lexically, numerically, by
name length, or not at all, so the same epoch's lineage came back in a
different order depending on which reader was asked.

This module re-derives the claim from the source tree on every run. It parses
every module under ``src/zicato`` and reports each call that enumerates a
record directory's children, then asserts the result equals ``_UNCONVERTED``,
the pinned set of sites still awaiting conversion — empty today. Adding a walk
fails; converting a pinned one fails until the set is trimmed, so the set can
only shrink.

What counts as a record enumeration
-----------------------------------
A call to ``iterdir`` / ``scandir`` / ``os.listdir`` whose receiver is a
record directory — named by :func:`~zicato.workspace.layout.WorkspaceLayout`'s
``generations_dir`` / ``runs_dir`` / ``rounds_dir`` accessors, by the
same-named module functions, or by a ``/ "generations"`` style path join —
including through a local variable the same function assigned from one.

A ``glob`` / ``rglob`` on such a directory counts only when the pattern's
last segment is a bare ``*``, which is the directory-wildcard form that
enumerates records. A pattern ending in a filename (say
``"*/runs/*/events*.jsonl"``) discovers telemetry FILES across the tree; that
is a different operation, the reader does not offer it, and it is left alone.

Where the pattern is allowed to live
------------------------------------
:mod:`zicato.workspace.reads` owns the enumeration, and the storage backends
implement the listing it calls. Both are exempt by module path.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "zicato"

#: Modules allowed to enumerate record directories: the reader that owns the
#: question, and the storage backends whose listing it is expressed in.
_OWNERS = frozenset(
    {
        "workspace/reads.py",
        "storage/files.py",
        "storage/memory.py",
    }
)

#: Names whose CALL yields a record directory (``layout.runs_dir(...)`` and
#: the module-level ``runs_dir(workspace_root, ...)`` alike). Word boundaries
#: matter: ``active_runs_dir(...)`` is the live runtime state directory, not a
#: board-entry run record, and must not match.
_RECORD_DIR_CALL = re.compile(r"\b(generations_dir|runs_dir|rounds_dir)\s*\(")

#: A path join that builds a record directory literally.
_RECORD_DIR_JOIN = re.compile(r"/\s*['\"](generations|runs|rounds)['\"]")

#: The listing calls that enumerate a directory's children.
_LISTING_METHODS = frozenset({"iterdir", "scandir", "listdir"})

#: The listing calls that take a pattern.
_GLOB_METHODS = frozenset({"glob", "rglob"})

#: Record enumerations that still live outside the reader, as
#: ``module path -> the enclosing functions``. Every entry is a site to
#: convert, not a site to keep, and the set is empty today.
_UNCONVERTED: dict[str, frozenset[str]] = {}


def _is_record_dir(expression: str) -> bool:
    """Whether an expression's source text names a record directory."""
    return bool(_RECORD_DIR_CALL.search(expression) or _RECORD_DIR_JOIN.search(expression))


def _enumerates_records(call: ast.Call, record_names: set[str]) -> bool:
    """Whether one call enumerates the children of a record directory."""
    func = call.func
    if not isinstance(func, ast.Attribute):
        return False
    receiver = ast.unparse(func.value)
    if isinstance(func.value, ast.Name):
        names_a_record_dir = func.value.id in record_names
    else:
        names_a_record_dir = _is_record_dir(receiver)
    if not names_a_record_dir:
        return False
    if func.attr in _LISTING_METHODS:
        return True
    if func.attr in _GLOB_METHODS and call.args:
        pattern = call.args[0]
        if isinstance(pattern, ast.Constant) and isinstance(pattern.value, str):
            return pattern.value.rsplit("/", 1)[-1] == "*"
    return False


def _record_dir_names(function: ast.AST) -> set[str]:
    """Local names a function assigned a record directory to."""
    names: set[str] = set()
    for node in ast.walk(function):
        if not isinstance(node, ast.Assign) or node.value is None:
            continue
        if not _is_record_dir(ast.unparse(node.value)):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                names.add(target.id)
    return names


def _functions(tree: ast.Module) -> list[tuple[str, ast.AST]]:
    """Every function in a module, as ``(qualified name, node)``."""
    out: list[tuple[str, ast.AST]] = []

    def visit(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                out.append((prefix + child.name, child))
                visit(child, prefix + child.name + ".")
            elif isinstance(child, ast.ClassDef):
                visit(child, prefix + child.name + ".")
            else:
                visit(child, prefix)

    visit(tree, "")
    return out


def _scan() -> dict[str, set[str]]:
    """Every record enumeration outside the owners, as module -> functions."""
    found: dict[str, set[str]] = {}
    for path in sorted(SRC.rglob("*.py")):
        relative = path.relative_to(SRC).as_posix()
        if relative in _OWNERS:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for name, function in _functions(tree):
            record_names = _record_dir_names(function)
            for node in ast.walk(function):
                if isinstance(node, ast.Call) and _enumerates_records(node, record_names):
                    found.setdefault(relative, set()).add(name)
    return found


def test_record_enumeration_lives_in_one_reader() -> None:
    """No module outside the reader grows a new record walk.

    The pinned set is the remaining work, so an unpinned finding is a
    regression and a pinned finding that disappeared is progress the set has
    to record.
    """
    found = _scan()
    expected = {module: set(functions) for module, functions in _UNCONVERTED.items()}
    assert found == expected, (
        "record enumeration moved. New walks belong in zicato.workspace.reads "
        "(generation_ids / run_entry_ids / round_indices); a converted site "
        "must be dropped from _UNCONVERTED.\n"
        f"  found:   { {k: sorted(v) for k, v in sorted(found.items())} }\n"
        f"  pinned:  { {k: sorted(v) for k, v in sorted(expected.items())} }"
    )


def test_scanner_detects_a_planted_walk(tmp_path: Path) -> None:
    """The scanner is not vacuous: it flags the shapes it claims to flag.

    A guard that finds nothing because its matcher is broken passes exactly
    like a guard over a clean tree. These four samples are the real forms the
    tree used before consolidation, plus the two the rule deliberately allows.
    """
    module = ast.parse(
        """
def direct(layout, epoch_id):
    for child in layout.generations_dir(epoch_id).iterdir():
        yield child.name

def through_a_local(layout, epoch_id):
    runs_root = layout.runs_dir(epoch_id, "v0")
    return sorted(p.name for p in runs_root.iterdir())

def through_a_join(root, epoch_id):
    gens = root / "epochs" / epoch_id / "generations"
    return list(gens.iterdir())

def through_a_glob(layout, epoch_id):
    return list(layout.rounds_dir(epoch_id).glob("*"))

def discovers_files(root):
    gens = root / "generations"
    return sorted(gens.glob("*/runs/*/events.jsonl"))

def reads_live_runtime_state(paths):
    runs_dir = paths.active_runs_dir
    return [p.name for p in runs_dir.iterdir()]
"""
    )
    flagged = {
        name
        for name, function in _functions(module)
        for node in ast.walk(function)
        if isinstance(node, ast.Call) and _enumerates_records(node, _record_dir_names(function))
    }
    assert flagged == {"direct", "through_a_local", "through_a_join", "through_a_glob"}
