"""Read and atomically write the strict authored workspace configuration.

The workspace root owns config.json. Missing files produce an absent record;
malformed JSON and invalid declared fields raise before factories read a value.
The parsed record retains authored presence for source attribution and exposes
its validated domain object through WorkspaceConfig.values.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from zicato.storage import atomic_write_text
from zicato.workspace.config_schema import WorkspaceDeclaration, workspace_declaration

CONFIG_FILENAME = "config.json"
LINEAGE_FILENAME = "lineage.json"

#: The ``config.json`` key naming the store that holds generation source
#: trees. Defined here because the loader projects it onto a typed field;
#: :mod:`zicato.epoch.genstore` imports it alongside the backend names it
#: accepts and owns the resolution rules.
GENERATION_SOURCE_BACKEND_KEY = "generation_source_backend"

#: The remedy named when a command that needs an initialized workspace finds
#: no ``config.json`` at all. ``{root}`` is filled with the workspace root.
INIT_REMEDY = "run `zicato init --workspace {root}` to bootstrap"


def _config_path(workspace_root: Path) -> Path:
    """Where one workspace's ``config.json`` lives. The only such decision."""
    return workspace_root / CONFIG_FILENAME


@dataclass(frozen=True, slots=True)
class WorkspaceConfig:
    """A workspace file, its authored values, and its validated declaration."""

    #: The file this was read from, whether or not it is there. Carried so
    #: an error or a write targets the location the read used.
    path: Path
    #: Whether the file was on disk.
    exists: bool
    values: WorkspaceDeclaration = field(default_factory=WorkspaceDeclaration)
    #: The whole parsed JSON object. The form the factories consume:
    #: :func:`zicato.runtime_factory.make_runtime_config`,
    #: :func:`zicato.adapter_factory.make_adapter_from_config`,
    #: :func:`zicato.models_config.load_models_config` and
    #: :func:`zicato.config.health_config_from_workspace` each read several
    #: keys after the root declaration has validated their authored types.
    raw: Mapping[str, Any] = field(default_factory=dict)
    #: The ``runtime`` block — instance id, seed, concurrency, worker
    #: containment, and the pre-flight and backoff knobs.
    runtime: Mapping[str, Any] = field(default_factory=dict)
    #: The ``contract`` block — the recorded paths of the live board, brief,
    #: scoring and proposer sources, and the declared static checks.
    contract: Mapping[str, Any] = field(default_factory=dict)
    #: The mutable source trees declared in the adapter block.
    source_roots: tuple[str, ...] = ()
    #: The model name from the named evaluation engine, empty for callable engines.
    evaluation_model: str = ""
    #: The ``generation_source_backend`` key — which store holds the
    #: generation source trees. Empty is what
    #: :func:`zicato.epoch.genstore.resolve_generation_store_backend`
    #: refuses.
    generation_source_backend: str = ""

    @classmethod
    def absent(cls, workspace_root: Path) -> WorkspaceConfig:
        """The reading of a workspace whose config cannot be used at all.

        What :func:`read_workspace_config` returns for a file that is not
        there, and what a best-effort caller substitutes for one that is
        there but malformed.
        """
        return cls(path=_config_path(workspace_root), exists=False)

    def require(self, remedy: str = INIT_REMEDY) -> WorkspaceConfig:
        """Return this config, or raise when the file was not there.

        ``remedy`` is the operator-side next step named in the error, with
        ``{root}`` filled in from the workspace root: an uninitialized
        workspace wants ``zicato init``, while one with no recorded
        evaluation contract wants ``zicato epoch register``.
        """
        if not self.exists:
            root = self.path.parent
            raise FileNotFoundError(
                f"workspace config not found at {self.path}; {remedy.format(root=root)}"
            )
        return self


def read_workspace_config(workspace_root: Path) -> WorkspaceConfig:
    """Read and parse one workspace's ``config.json``.

    The single entry point for the file; see the module docstring for the
    absence rule. Raises :class:`ValueError` for a file that is present but
    is not valid UTF-8, is not parseable JSON, or does not parse to a JSON
    object, and :class:`OSError` for one that is present but unreadable. A
    best-effort caller that must never propagate catches both.
    """
    path = _config_path(workspace_root)
    if not path.exists():
        return WorkspaceConfig.absent(workspace_root)
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"could not parse {path}: {exc.msg}") from exc
    if not isinstance(loaded, Mapping):
        raise ValueError(
            f"{path}: expected a JSON object at top level, got {type(loaded).__name__}"
        )
    raw = dict(loaded)
    values = workspace_declaration(raw)
    runtime = raw.get("runtime", {})
    return WorkspaceConfig(
        path=path,
        exists=True,
        values=values,
        raw=raw,
        runtime=runtime,
        contract=raw.get("contract", {}),
        source_roots=values.adapter.mutable_trees if values.adapter is not None else (),
        evaluation_model=(
            values.models.engines[values.models.selected_name("evaluation")].model or ""
            if values.models.selected_name("evaluation") in values.models.engines
            else ""
        ),
        generation_source_backend=values.generation_source_backend,
    )


def write_workspace_config(workspace_root: Path, config: dict[str, Any]) -> None:
    """Atomically write ``config.json`` under ``workspace_root``.

    The workspace directory must already exist. Configuration files use
    sorted, indented JSON with a trailing newline and mode 0666 subject
    to the process umask. Callers own any compound read-modify-write lock.
    """
    if not workspace_root.exists():
        raise FileNotFoundError(
            f"workspace {workspace_root!s} does not exist; run `zicato init` first"
        )
    workspace_declaration(config)
    atomic_write_text(
        _config_path(workspace_root),
        json.dumps(config, indent=2, sort_keys=True) + "\n",
        mode=0o666,
    )


def workspace_is_initialized(workspace_root: Path) -> bool:
    """Return True iff ``workspace_root/config.json`` exists."""
    return _config_path(workspace_root).exists()


__all__ = [
    "CONFIG_FILENAME",
    "GENERATION_SOURCE_BACKEND_KEY",
    "INIT_REMEDY",
    "LINEAGE_FILENAME",
    "WorkspaceConfig",
    "read_workspace_config",
    "workspace_is_initialized",
    "write_workspace_config",
]
