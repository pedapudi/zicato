"""Build a :class:`HarnessAdapter` from a workspace config dict or a spec.

The CLI / orchestrator layers do not want to know which adapter shape
is in use — they read ``config["adapter"]["kind"]`` and hand the rest
of the adapter sub-dict to this factory. New adapter shapes get a new
``kind`` value and a small dispatch branch here; the rest of the
codebase is untouched.

Two entry points, one per direction across the process boundary:
:func:`make_adapter_from_config` takes what an operator wrote in
``config.json``; :func:`make_adapter_from_spec` takes the serialised
worker spec a tournament worker receives as JSON and rebuilds the same
adapter in a fresh interpreter. They share the ``"import"`` branch, so
the two constructions cannot drift apart.

The factory does not import vendor SDKs at module level: each branch
imports its concrete adapter class lazily so the optional dependency
on goldfive / google-adk only fires when an operator actually selects
the corresponding kind. Tests that exercise unrelated branches do not
need the heavy extras installed.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
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
    * ``"import"`` — a generic factory shape for any non-ADK adapter:
      ``adapter['factory']`` is a ``"module.path:callable"`` dotted path
      imported and called with the optional positional
      ``adapter['args']`` list to produce the adapter object. This is
      the config-side mirror of the spec a subprocess worker
      reconstructs (:func:`make_adapter_from_spec`), so a workspace whose
      harness is a custom adapter can declare it honestly in
      ``config.json`` instead of relying on a test-side factory
      monkeypatch.

    What usually travels with a custom adapter
    ------------------------------------------
    A target zicato does not ship is rarely graded well by stock
    machinery, so an operator setting ``adapter.factory`` (or pointing
    ``adapter.entrypoint`` at their own harness) usually also wants:

    * ``scoring.outcome_summarizer_spec`` — how a finished run is
      reduced to an outcome for THIS target, rather than the default
      summary;
    * a ``predicate`` ``expectation.spec`` on the board entries — a
      dotted path to their own pass/fail callable, rather than the
      literal text or regex matchers, which rarely know what good looks
      like for a bespoke target.

    None of that is enforced: a custom adapter with stock grading is a
    legitimate, if unusual, configuration. It is recorded here because
    the coupling is easy to miss and expensive to discover — the loop
    runs, spends, and optimizes against a grade that was never about
    this target.

    Backwards-compatibility hook: the older ``zicato epoch register`` flow
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
        # Legacy fallback — `zicato epoch register` writes these top-level keys.
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
    if kind == "import":
        return _build_import(adapter_dict)

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


def _build_import(adapter_dict: Mapping[str, Any]) -> Any:
    """Construct an adapter from an ``{"kind": "import", ...}`` block.

    Shared by :func:`make_adapter_from_config` and
    :func:`make_adapter_from_spec`, because the two shapes are identical
    for this kind: ``factory`` is a ``"module.path:callable"`` dotted
    path and the optional ``args`` list is passed positionally. Sharing
    it is what keeps the adapter the orchestrator constructs from
    ``config.json`` and the one a worker reconstructs from its
    serialised spec the same object.
    """
    factory_path = adapter_dict.get("factory")
    if not factory_path or not isinstance(factory_path, str):
        raise ValueError("adapter kind='import' requires a non-empty 'factory' dotted path")
    raw_args = adapter_dict.get("args", [])
    if raw_args is None:
        raw_args = []
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
    return factory(*raw_args)


def make_adapter_from_spec(spec: Mapping[str, Any]) -> Any:
    """Reconstruct a harness adapter from its serialised worker spec.

    The counterpart of :func:`make_adapter_from_config`: the config form
    is what an operator writes, the spec form is what crosses a process
    boundary. A tournament worker rebuilds its adapter from this spec in
    a fresh interpreter, having received it as JSON from
    :func:`zicato.tournament.worker_transport.adapter_worker_spec`, so
    the spec carries only what survives serialisation — no workspace
    config dict and no live objects.

    Two shapes are understood, matching the two ``make_adapter_from_config``
    kinds:

    * ``{"kind": "adk", "entrypoint": ..., "mutable_trees": [...]}`` — the
      production shape; reconstructs an
      :class:`~zicato.adapters.adk.ADKHarnessAdapter` directly.
    * ``{"kind": "import", "factory": "module:callable", "args": [...]}``
      — the generic shape for any non-ADK adapter; the dotted path is
      imported and called with the optional positional ``args``.

    Raises
    ------
    ValueError
        Unknown ``kind``, or missing required fields.
    """
    kind = spec.get("kind")
    if kind == "adk":
        from zicato.adapters.adk import ADKHarnessAdapter  # noqa: PLC0415

        entrypoint = str(spec["entrypoint"])
        raw_trees = spec.get("mutable_trees") or []
        trees = [Path(t) for t in raw_trees] if raw_trees else None
        return ADKHarnessAdapter(entrypoint=entrypoint, mutable_trees=trees)
    if kind == "import":
        return _build_import(spec)
    raise ValueError(f"cannot reconstruct adapter kind {kind!r} from a worker spec")


__all__ = ["make_adapter_from_config", "make_adapter_from_spec"]
