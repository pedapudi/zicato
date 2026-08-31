"""One owner for the workspace configuration file, pinned structurally.

"Where is this workspace's ``config.json``, and what is in it" is answered by
:mod:`zicato.workspace.config_io` and nowhere else. Any module can build
``workspace_root / "config.json"`` and parse it, and thirteen sites across
twelve modules did — one raising on an absent file, another substituting an
empty dict, a third silently swallowing a malformed one, so the same broken
workspace produced a clean error, a defaulted run, or a traceback depending
on which command the operator typed.

This module re-derives the claim from the source tree on every run. It parses
every module under ``src/zicato`` and reports each site that names or parses
the workspace config file, then asserts the result equals ``_UNCONVERTED``,
the pinned set of sites still awaiting conversion — empty today. Adding a
site fails; converting a pinned one fails until the set is trimmed, so the
set can only shrink.

What counts as opening the workspace config
-------------------------------------------
Two shapes:

* a path join whose right operand is the literal ``"config.json"`` or the
  name ``CONFIG_FILENAME``;
* a ``json.load`` / ``json.loads`` call whose argument text names that file.

Each epoch keeps its own ``config.json`` under ``epochs/<id>/``, which is a
different file with a different schema and a different owner
(:class:`~zicato.workspace.layout.WorkspaceLayout` and the epoch lifecycle).
A join whose left side names an epoch directory is therefore that file, not
this one, and is left alone.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "zicato"

#: The module allowed to name and parse the workspace config file.
_OWNERS = frozenset({"workspace/config_io.py"})

#: Filenames that spell the workspace config, as a path join's right operand.
_CONFIG_NAMES = frozenset({"config.json", "CONFIG_FILENAME"})

#: A left operand that names a per-epoch directory, whose ``config.json`` is
#: the epoch record rather than the workspace configuration.
_EPOCH_RECEIVER = re.compile(r"epoch", re.IGNORECASE)


def _names_the_config_file(node: ast.expr) -> bool:
    """Whether a path join builds the workspace config file's path."""
    if not isinstance(node, ast.BinOp) or not isinstance(node.op, ast.Div):
        return False
    right = node.right
    if isinstance(right, ast.Constant) and right.value in _CONFIG_NAMES:
        pass
    elif isinstance(right, ast.Name) and right.id in _CONFIG_NAMES:
        pass
    else:
        return False
    return not _EPOCH_RECEIVER.search(ast.unparse(node.left))


def _parses_the_config_file(node: ast.expr) -> bool:
    """Whether a ``json.load`` / ``json.loads`` call reads that file."""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if not isinstance(func, ast.Attribute) or func.attr not in {"load", "loads"}:
        return False
    if not node.args:
        return False
    text = ast.unparse(node.args[0])
    if not any(name in text for name in _CONFIG_NAMES):
        return False
    return not _EPOCH_RECEIVER.search(text)


def _opens_the_config(node: ast.AST) -> bool:
    """Whether one expression names or parses the workspace config file."""
    return isinstance(node, ast.expr) and (
        _names_the_config_file(node) or _parses_the_config_file(node)
    )


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


#: Workspace-config reads that still live outside the loader, as
#: ``module path -> the enclosing functions``. Every entry is a site to
#: convert, not a site to keep, and the set is empty today.
_UNCONVERTED: dict[str, frozenset[str]] = {}


def _scan() -> dict[str, set[str]]:
    """Every workspace-config read outside the owner, as module -> functions."""
    found: dict[str, set[str]] = {}
    for path in sorted(SRC.rglob("*.py")):
        relative = path.relative_to(SRC).as_posix()
        if relative in _OWNERS:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for name, function in _functions(tree):
            for node in ast.walk(function):
                if _opens_the_config(node):
                    found.setdefault(relative, set()).add(name)
    return found


def test_workspace_config_is_read_in_one_place() -> None:
    """No module outside the loader grows a second read of the config file.

    The pinned set is the remaining work, so an unpinned finding is a
    regression and a pinned finding that disappeared is progress the set has
    to record.
    """
    found = _scan()
    expected = {module: set(functions) for module, functions in _UNCONVERTED.items()}
    assert found == expected, (
        "the workspace config grew a second reader. New reads belong in "
        "zicato.workspace.config_io (read_workspace_config); a converted site "
        "must be dropped from _UNCONVERTED.\n"
        f"  found:   { {k: sorted(v) for k, v in sorted(found.items())} }\n"
        f"  pinned:  { {k: sorted(v) for k, v in sorted(expected.items())} }"
    )


def test_scanner_detects_a_planted_read() -> None:
    """The scanner is not vacuous: it flags the shapes it claims to flag.

    A guard that finds nothing because its matcher is broken passes exactly
    like a guard over a clean tree. These samples are the real forms the tree
    used before consolidation, plus the three the rule deliberately allows:
    the per-epoch record of the same name, and the two per-epoch spellings
    that reach it through the layout.
    """
    module = ast.parse(
        """
def by_literal_join(workspace_root):
    return (workspace_root / "config.json").read_text()

def by_filename_constant(workspace_root):
    return workspace_root / CONFIG_FILENAME

def by_parse(workspace_root):
    return json.loads((workspace_root / "config.json").read_text(encoding="utf-8"))

def by_parse_without_a_join(workspace_root):
    return json.loads(_slurp(workspace_root, "config.json"))

def reads_the_epoch_record(epoch_dir):
    return _read_json_value(epoch_dir / "config.json")

def reads_the_epoch_record_through_the_layout(layout, epoch_id):
    return _read_json_value(layout.epoch_dir(epoch_id) / "config.json")

def reads_the_epoch_record_through_paths(paths, epoch_id):
    return _read_json_value(paths.epochs / epoch_id / "config.json")
"""
    )
    flagged = {
        name
        for name, function in _functions(module)
        for node in ast.walk(function)
        if _opens_the_config(node)
    }
    assert flagged == {
        "by_literal_join",
        "by_filename_constant",
        "by_parse",
        "by_parse_without_a_join",
    }
