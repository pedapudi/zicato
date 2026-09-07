"""Check exhaustive namespace roles and the library's forbidden import paths."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import tomllib
from pathlib import Path
from typing import Any

import grimp
from importlinter.cli import lint_imports

ROLES = {"primitive", "execution", "coordination", "mixed_library", "driver"}


def configuration(root: Path) -> tuple[dict[str, str], dict[str, Any]]:
    """Require every production namespace to have one supported role."""
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    roles = project["tool"]["zicato"]["namespace_roles"]
    if not isinstance(roles, dict) or any(
        not isinstance(role, str) or role not in ROLES for role in roles.values()
    ):
        raise ValueError("namespace_roles must assign a supported role to each namespace")
    discovered: set[str] = set()
    for path in (root / "src" / "zicato").iterdir():
        if path.is_file() and path.suffix == ".py" and path.stem != "__init__":
            name = path.stem
        elif path.is_dir() and any(path.rglob("*.py")):
            name = path.name
        else:
            continue
        if name in discovered:
            raise ValueError(f"duplicate production namespace: zicato.{name}")
        discovered.add(name)
    unclassified = sorted(discovered - roles.keys())
    absent = sorted(roles.keys() - discovered)
    if unclassified or absent:
        raise ValueError(
            f"namespace inventory mismatch: unclassified={unclassified}, absent={absent}"
        )
    if not discovered:
        raise ValueError("no production namespaces discovered")
    config = project["tool"]["zicato"]["importlinter"]
    if config.get("root_packages") != ["zicato"]:
        raise ValueError("import checks require root_packages = ['zicato']")
    contracts = config.setdefault("contracts", [])
    for name, source_roles, forbidden_roles in (
        ("library code cannot import drivers", ROLES - {"driver"}, {"driver"}),
        (
            "execution code cannot import coordination or drivers",
            {"primitive", "execution"},
            {"coordination", "driver"},
        ),
        ("primitives cannot import the rest of the library", {"primitive"}, ROLES - {"primitive"}),
    ):
        sources = sorted(f"zicato.{name}" for name, role in roles.items() if role in source_roles)
        forbidden = sorted(
            f"zicato.{name}" for name, role in roles.items() if role in forbidden_roles
        )
        if sources and forbidden:
            contracts.append(
                {
                    "name": name,
                    "type": "forbidden",
                    "source_modules": sources,
                    "forbidden_modules": forbidden,
                }
            )
    if not contracts:
        raise ValueError("no import restrictions apply to the namespace inventory")
    return roles, config


def check(root: Path) -> int:
    """Check the root facade separately, then all transitive import contracts."""
    roles, config = configuration(root)
    os.chdir(root)
    sys.path.insert(0, str(root / "src"))
    graph = grimp.build_graph("zicato", cache_dir=None)
    drivers = {f"zicato.{name}" for name, role in roles.items() if role == "driver"}
    for module in sorted(graph.modules):
        if ".".join(module.split(".")[:2]) in drivers:
            chain = graph.find_shortest_chain("zicato", module)
            if chain:
                raise ValueError("root facade imports a driver: " + " -> ".join(chain))

    lines = ["[tool.importlinter]"]
    lines.extend(
        f"{key} = {json.dumps(value)}" for key, value in config.items() if key != "contracts"
    )
    for contract in config["contracts"]:
        lines.append("\n[[tool.importlinter.contracts]]")
        lines.extend(f"{key} = {json.dumps(value)}" for key, value in contract.items())
    with tempfile.TemporaryDirectory(prefix="zicato-imports-") as temporary:
        path = Path(temporary) / "pyproject.toml"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return lint_imports(config_filename=str(path), no_cache=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args(argv)
    try:
        return check(args.root.resolve())
    except (OSError, ValueError, KeyError) as exc:
        print(f"import checks failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
