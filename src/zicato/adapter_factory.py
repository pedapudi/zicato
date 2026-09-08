"""Construct the declared harness in a coordinator or a fresh worker."""

from __future__ import annotations

import inspect
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any


def make_adapter_from_config(
    workspace_config: Mapping[str, Any], *, workspace_root: Path | None = None
) -> Any:
    """Construct the canonical adapter declaration using workspace-relative driver roots."""
    from zicato.core.adapter_config import (
        DriverImportContext,
        adapter_declaration,
        registered_mutable_trees,
    )
    from zicato.driver_imports import driver_import_scope

    declaration = adapter_declaration(workspace_config)
    root = workspace_root or Path(".zicato")
    with driver_import_scope(DriverImportContext.from_config(workspace_config, root)):
        document = declaration.document()
        document["mutable_trees"] = [
            str(tree) for tree in registered_mutable_trees(workspace_config, root)
        ]
        return _build_adk(document) if declaration.kind == "adk" else _build_import(document)


def _build_adk(adapter_dict: Mapping[str, Any]) -> Any:
    """Construct the built-in adapter lazily."""
    entrypoint = adapter_dict.get("entrypoint")
    if not entrypoint or not isinstance(entrypoint, str):
        raise ValueError("adapter kind='adk' requires a non-empty 'entrypoint' string")
    raw_trees = adapter_dict.get("mutable_trees", [])
    if not isinstance(raw_trees, list | tuple) or any(not isinstance(t, str) for t in raw_trees):
        raise ValueError("adapter mutable_trees must be a list of paths")
    trees = [Path(t) for t in raw_trees]

    # Lazy import so this factory module remains importable without
    # google-adk / goldfive being installed.
    from zicato.adapters.adk import ADKHarnessAdapter

    return ADKHarnessAdapter(entrypoint=entrypoint, mutable_trees=trees)


def _build_import(adapter_dict: Mapping[str, Any]) -> Any:
    """Call a module factory with declared positional and keyword arguments."""
    factory_path = adapter_dict.get("factory")
    if not factory_path or not isinstance(factory_path, str):
        raise ValueError("adapter kind='import' requires a non-empty 'factory' dotted path")
    raw_args = adapter_dict.get("args", [])
    if not isinstance(raw_args, Sequence) or isinstance(raw_args, str | bytes):
        raise ValueError(
            f"adapter kind='import' 'args' must be a list, got {type(raw_args).__name__}"
        )

    # Lazy import — mirrors the worker's own resolution helper.
    from zicato.import_path import import_dotted_path

    factory = import_dotted_path(factory_path, label="adapter factory")
    if not callable(factory):
        raise ValueError(
            f"adapter kind='import': factory {factory_path!r} resolved to "
            f"{type(factory).__name__}, expected a callable"
        )
    options = adapter_dict.get("options", {})
    if not isinstance(options, Mapping):
        raise ValueError("adapter options must be an object of factory keyword arguments")
    try:
        signature = inspect.signature(factory)
    except ValueError:
        signature = None
    if signature is not None:
        try:
            signature.bind(*raw_args, **options)
        except TypeError as exc:
            raise ValueError(f"adapter factory {factory_path!r} arguments: {exc}") from exc
    return factory(*raw_args, **options)


def make_adapter_from_spec(spec: Mapping[str, Any]) -> Any:
    """Reconstruct an adapter from its serializable worker specification."""
    kind = spec.get("kind")
    if kind == "adk":
        return _build_adk(spec)
    if kind == "import":
        return _build_import(spec)
    raise ValueError(f"cannot reconstruct adapter kind {kind!r} from a worker spec")


def validate_harness_run(session: Any) -> None:
    """Require the asynchronous harness interface used by probes and workers."""
    run = getattr(session, "run", None)
    if not callable(run) or not inspect.iscoroutinefunction(run):
        raise ValueError("loaded harness must expose async run(entry, sinks, config)")
    try:
        inspect.signature(run).bind(object(), object(), object())
    except (TypeError, ValueError) as exc:
        raise ValueError("loaded harness must expose async run(entry, sinks, config)") from exc
