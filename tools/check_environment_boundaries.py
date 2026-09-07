"""Require every direct environment access to have an approved boundary owner."""

from __future__ import annotations

import ast
from pathlib import Path

from zicato.config import ENVIRONMENT_BOUNDARIES


def environment_accesses(source: str) -> set[str]:
    """Find direct accesses, including imported aliases and assigned module aliases."""
    tree = ast.parse(source)
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    modules, values = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.asname or alias.name for alias in node.names if alias.name == "os")
        if isinstance(node, ast.ImportFrom) and node.module == "os":
            values.update(
                alias.asname or alias.name
                for alias in node.names
                if alias.name in {"environ", "getenv", "*"}
            )
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and ast.unparse(node.value) in modules:
            modules.update(ast.unparse(target) for target in node.targets)
    accesses = set()
    for node in ast.walk(tree):
        direct = (
            isinstance(node, ast.Attribute)
            and node.attr in {"environ", "getenv"}
            and ast.unparse(node.value) in modules
        ) or (isinstance(node, ast.Name) and node.id in values and isinstance(node.ctx, ast.Load))
        if not direct:
            continue
        owner = node
        names = []
        while owner in parents:
            owner = parents[owner]
            if isinstance(owner, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                names.insert(0, owner.name)
        accesses.add(".".join(names) or "<module>")
    if "*" in values:
        accesses.add("<wildcard import>")
    return accesses


def boundary_errors(root: Path) -> list[str]:
    """Reject unapproved access sites and declarations with no remaining reader."""
    approved = {
        (item.module, function) for item in ENVIRONMENT_BOUNDARIES for function in item.functions
    }
    actual = {
        (path.relative_to(root / "src").as_posix(), function)
        for path in (root / "src" / "zicato").rglob("*.py")
        for function in environment_accesses(path.read_text())
    }
    return [
        f"unapproved environment access: {module}:{function}"
        for module, function in sorted(actual - approved)
    ] + [
        f"unused environment declaration: {module}:{function}"
        for module, function in sorted(approved - actual)
    ]


def main() -> int:
    errors = boundary_errors(Path(__file__).resolve().parents[1])
    for error in errors:
        print(error)
    return bool(errors)


if __name__ == "__main__":
    raise SystemExit(main())
