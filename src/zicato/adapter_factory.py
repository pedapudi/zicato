"""Build a :class:`HarnessAdapter` from a workspace config dict.

The CLI / orchestrator layers do not want to know which adapter shape
is in use — they read ``config["adapter"]["kind"]`` and hand the rest
of the adapter sub-dict to this factory. New adapter shapes get a new
``kind`` value and a small dispatch branch here; the rest of the
codebase is untouched.

The factory does not import vendor SDKs at module level: each branch
imports its concrete adapter class lazily so the optional dependency
on goldfive / google-adk only fires when an operator actually selects
the corresponding kind. Tests that exercise unrelated branches do not
need the heavy extras installed.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any


def make_adapter_from_config(workspace_config: Mapping[str, Any]) -> Any:
    """Dispatch on ``workspace_config['adapter']['kind']`` to build an adapter.

    Supported kinds:

    * ``"adk"`` — :class:`zicato.adapters.adk.ADKHarnessAdapter`. Reads
      ``adapter['entrypoint']`` (a ``"module.path:agent_symbol"`` string)
      and an optional ``adapter['mutable_trees']`` list of filesystem
      paths. Missing ``mutable_trees`` defers to the adapter's own
      best-effort default (the directory of the entrypoint module).

    Backwards-compatibility hook: the older ``zicato register`` flow
    persists ``config['adk_entrypoint']`` + ``config['mutable_trees']``
    at the workspace-config top level. When ``config['adapter']`` is
    absent but those legacy keys are present, we treat that as
    ``kind="adk"`` so workspaces registered before the factory landed
    keep working without a manual edit.

    Parameters
    ----------
    workspace_config:
        The dict returned by
        :func:`zicato.workspace_loader.load_workspace_config` (or any
        equivalent loader). Treated as read-only.

    Returns
    -------
    HarnessAdapter
        Concrete adapter instance ready for ``load`` /
        ``mutation_points`` calls.

    Raises
    ------
    ValueError
        Unknown ``kind`` value, or missing required adapter fields.
    """
    adapter_dict = workspace_config.get("adapter")
    if adapter_dict is None:
        # Legacy fallback — `zicato register` writes these top-level keys.
        legacy_entry = workspace_config.get("adk_entrypoint")
        if legacy_entry:
            adapter_dict = {
                "kind": "adk",
                "entrypoint": legacy_entry,
                "mutable_trees": list(workspace_config.get("mutable_trees", []) or []),
            }
        else:
            raise ValueError(
                "workspace_config has no 'adapter' block and no legacy "
                "'adk_entrypoint' key; cannot construct a HarnessAdapter"
            )

    if not isinstance(adapter_dict, Mapping):
        raise ValueError(
            f"workspace_config['adapter'] must be a mapping, got {type(adapter_dict).__name__}"
        )

    kind = adapter_dict.get("kind")
    if not kind or not isinstance(kind, str):
        raise ValueError("workspace_config['adapter']['kind'] must be a non-empty string")

    if kind == "adk":
        return _build_adk(adapter_dict)

    raise ValueError(f"unknown adapter kind {kind!r}")


def _build_adk(adapter_dict: Mapping[str, Any]) -> Any:
    """Construct an :class:`ADKHarnessAdapter` from its config sub-dict."""
    entrypoint = adapter_dict.get("entrypoint")
    if not entrypoint or not isinstance(entrypoint, str):
        raise ValueError("adapter kind='adk' requires a non-empty 'entrypoint' string")
    raw_trees = adapter_dict.get("mutable_trees", [])
    if raw_trees is None:
        trees: list[Path] | None = None
    else:
        trees = [Path(t) for t in raw_trees]

    # Lazy import so this factory module remains importable without
    # google-adk / goldfive being installed.
    from zicato.adapters.adk import ADKHarnessAdapter

    return ADKHarnessAdapter(entrypoint=entrypoint, mutable_trees=trees)


__all__ = ["make_adapter_from_config"]
