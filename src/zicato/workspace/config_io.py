"""The workspace root's ``config.json`` — its location, its parse, its shape.

The file at the root of a ``.zicato/`` tree carries the bookkeeping every
command needs: which harness adapter to load, which source roots are
mutable, which store holds the generation source trees, the model roles,
and the ``runtime`` tuning block. This module is the only place in the tree
that opens it.

:func:`read_workspace_config` resolves the path, parses the JSON once, and
returns a :class:`WorkspaceConfig` — the whole mapping under
:attr:`WorkspaceConfig.raw`, plus typed fields for the blocks and keys that
callers read one at a time. Absence and malformation have one rule each: an
absent file yields a config whose :attr:`~WorkspaceConfig.exists` is
``False`` and whose every field holds its absent-key default, so a
best-effort reader needs no error handling; a file that is not parseable
JSON, or whose top level is not a JSON object, raises :class:`ValueError`
naming the path and the failure. A command that cannot proceed without an
initialized workspace adds :meth:`WorkspaceConfig.require`, whose argument
is the operator-side remedy — the only thing that legitimately differs
between such commands.

The loader does no outer→inner ``.zicato`` descent: callers hand it the
inner workspace root, and a command that accepts either spelling loads both
candidates and takes the first that exists. Path math for the records
*inside* the workspace lives in :mod:`zicato.core.workspace` and
:mod:`zicato.workspace.layout`. The typed tree of process-level knobs
(dataclass defaults plus pinned CLI flags) is a different object entirely
and lives in :mod:`zicato.config`.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

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


def _mapping(value: Any) -> Mapping[str, Any]:
    """A block read off the config: the mapping itself, or empty."""
    return value if isinstance(value, Mapping) else {}


def _str_tuple(value: Any) -> tuple[str, ...]:
    """A list-of-strings key read off the config, or empty."""
    if not isinstance(value, list):
        return ()
    return tuple(str(item) for item in value)


@dataclass(frozen=True, slots=True)
class WorkspaceConfig:
    """One workspace's parsed ``config.json``.

    Each block and key below is normalized to its absent-key default when
    the file omits it or holds the wrong JSON type, so a reader takes the
    value rather than re-checking the shape. The ``mutable_trees`` key is a
    later spelling of ``source_roots`` that some readers prefer over it and
    others fall back to; those readers take both off :attr:`raw` in the
    order they want, because the order differs between them.
    """

    #: The file this was read from, whether or not it is there. Carried so
    #: an error or a write targets the location the read used.
    path: Path
    #: Whether the file was on disk.
    exists: bool
    #: The whole parsed JSON object. The form the factories consume:
    #: :func:`zicato.runtime_factory.make_runtime_config`,
    #: :func:`zicato.adapter_factory.make_adapter_from_config`,
    #: :func:`zicato.models_config.load_models_config` and
    #: :func:`zicato.config.health_config_from_workspace` each read several
    #: keys and validate them their own way.
    raw: Mapping[str, Any] = field(default_factory=dict)
    #: The ``runtime`` block — instance id, seed, concurrency, worker
    #: containment, and the pre-flight and backoff knobs.
    runtime: Mapping[str, Any] = field(default_factory=dict)
    #: The ``contract`` block — the recorded paths of the live board, brief,
    #: scoring and proposer sources, and the declared static checks.
    contract: Mapping[str, Any] = field(default_factory=dict)
    #: The ``source_roots`` key: the mutable source trees ``zicato epoch
    #: register`` recorded.
    source_roots: tuple[str, ...] = ()
    #: The model id forwarded to the auxiliary LLM, from the top-level
    #: ``auxiliary_model`` key or the ``runtime`` block's, in that order.
    #: Empty when neither is set.
    auxiliary_model: str = ""
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
    runtime = _mapping(raw.get("runtime"))
    backend = raw.get(GENERATION_SOURCE_BACKEND_KEY)
    return WorkspaceConfig(
        path=path,
        exists=True,
        raw=raw,
        runtime=runtime,
        contract=_mapping(raw.get("contract")),
        source_roots=_str_tuple(raw.get("source_roots")),
        auxiliary_model=str(raw.get("auxiliary_model") or runtime.get("auxiliary_model") or ""),
        generation_source_backend=backend if isinstance(backend, str) else "",
    )


def write_workspace_config(workspace_root: Path, config: dict[str, Any]) -> None:
    """Atomically write ``config.json`` under ``workspace_root``.

    The workspace directory must already exist. Writes through a
    temp-and-rename so a partial write never leaves the file truncated.
    """
    if not workspace_root.exists():
        raise FileNotFoundError(
            f"workspace {workspace_root!s} does not exist; run `zicato init` first"
        )
    target = _config_path(workspace_root)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n")
    tmp.replace(target)


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
