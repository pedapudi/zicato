"""Authored harness declaration and its operational import locations."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from zicato.core.configuration import authored_dataclass_from_json, dataclass_to_jsonable


def _metadata(description: str, *, operational: bool = False) -> dict[str, Any]:
    return {
        "description": description,
        "scope": "operational" if operational else "evaluation-contract",
        "rolls_epoch": not operational,
        "secret_reference": False,
    }


@dataclass(frozen=True, slots=True)
class AdapterDeclaration:
    """One declaration drives construction and evaluation-contract identity."""

    kind: Literal["adk", "import"] = field(
        metadata=_metadata("Harness adapter implementation kind.")
    )
    entrypoint: str | None = field(
        default=None, metadata=_metadata("Dotted entrypoint for the built-in adapter.")
    )
    factory: str | None = field(
        default=None, metadata=_metadata("Dotted callable constructing a custom adapter.")
    )
    args: tuple[Any, ...] = field(
        default=(),
        metadata={
            **_metadata("Positional factory arguments."),
        },
    )
    options: Mapping[str, Any] = field(
        default_factory=dict,
        metadata=_metadata("Keyword factory arguments, validated by the factory."),
    )
    mutable_trees: tuple[str, ...] = field(
        default=(),
        metadata={
            **_metadata("Source directories copied into each candidate."),
        },
    )
    import_roots: tuple[str, ...] = field(
        default=(),
        metadata=_metadata(
            "Fixed driver import directories, relative to the workspace parent unless absolute.",
            operational=True,
        ),
    )
    stock_grading_confirmed: bool = field(
        default=False,
        metadata=_metadata(
            "Confirm that stock grading measures this custom target.", operational=True
        ),
    )

    def __post_init__(self) -> None:
        required = self.entrypoint if self.kind == "adk" else self.factory
        if not required or not required.strip():
            name = "entrypoint" if self.kind == "adk" else "factory"
            raise ValueError(f"adapter kind={self.kind!r} requires a non-empty {name!r}")
        if any(not root.strip() for root in (*self.mutable_trees, *self.import_roots)):
            raise ValueError("adapter paths must not be empty")

    def document(self) -> dict[str, Any]:
        """Return the canonical authored shape, including explicit defaults."""
        return dataclass_to_jsonable(self)


def adapter_declaration(config: Mapping[str, Any]) -> AdapterDeclaration:
    """Read the workspace's declared adapter."""
    raw = config.get("adapter")
    if raw is None:
        raise ValueError("workspace has no 'adapter' registration")
    return authored_dataclass_from_json(AdapterDeclaration, raw, path="adapter")


def registered_mutable_trees(config: Mapping[str, Any], workspace_root: Path) -> tuple[Path, ...]:
    """Resolve the declared source directories against the workspace parent."""
    if config.get("adapter") is None:
        return ()
    return tuple(
        (workspace_root.resolve().parent / tree).resolve()
        for tree in adapter_declaration(config).mutable_trees
    )


@dataclass(frozen=True, slots=True)
class DriverImportContext:
    """Resolved fixed-driver locations, separate from candidate source identity."""

    roots: tuple[Path, ...] = ()
    mutable_packages: tuple[str, ...] = ()

    @classmethod
    def from_config(cls, config: Mapping[str, Any], workspace_root: Path) -> DriverImportContext:
        if config.get("adapter") is None:
            return cls()
        declaration = adapter_declaration(config)
        base = workspace_root.resolve().parent
        roots = tuple(dict.fromkeys((base / root).resolve() for root in declaration.import_roots))
        packages = tuple(
            sorted(
                {
                    Path(tree).name
                    for tree in registered_mutable_trees(config, workspace_root)
                    if Path(tree).name.isidentifier()
                }
            )
        )
        return cls(roots, packages)

    def document(self) -> dict[str, Any]:
        return {
            "roots": [str(root) for root in self.roots],
            "mutable_packages": list(self.mutable_packages),
        }

    @classmethod
    def from_document(cls, raw: Mapping[str, Any]) -> DriverImportContext:
        roots, packages = raw.get("roots", []), raw.get("mutable_packages", [])
        if not isinstance(roots, list) or not all(isinstance(root, str) for root in roots):
            raise ValueError("driver import roots must be an array of absolute paths")
        if any(not Path(root).is_absolute() for root in roots):
            raise ValueError("driver import roots must be absolute")
        if not isinstance(packages, list) or not all(
            isinstance(name, str) and name.isidentifier() for name in packages
        ):
            raise ValueError("mutable package names must be Python identifiers")
        return cls(tuple(Path(root) for root in roots), tuple(packages))
