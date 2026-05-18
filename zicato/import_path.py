"""Shared dotted-path importer used across the zicato runtime.

A single implementation ensures ``module:attr`` and ``module.attr`` are
resolved identically everywhere — the board predicate loader, the runtime
factory, the tournament worker, and the CLI evolve command all delegate
here rather than maintaining divergent parsers.

Accepted forms
--------------

``pkg.module:attr``
    Entry-point style (colon separator). The string up to the colon is the
    module to import; the string after the colon is the attribute name.

``pkg.module.attr``
    Plain dotted form. The last component is the attribute; everything
    before the last dot is the module.

Both forms accept nested attribute access when the ``attr`` portion itself
contains dots (e.g. ``pkg.mod:Outer.method`` or
``pkg.mod.Outer.method``).

``<locals>``-qualified names are rejected explicitly because closure-local
callables cannot be re-imported from their enclosing scope in a fresh
interpreter session.
"""

from __future__ import annotations

import importlib
from typing import Any


def import_dotted_path(path: str, *, label: str = "dotted path") -> Any:
    """Import an object by dotted path, returning it.

    Both ``module:attr`` (colon-separated, entry-point style) and
    ``module.attr`` (dot-separated) forms are accepted. The ``label``
    parameter is embedded in error messages so callers can identify which
    configuration field produced a bad path.

    Parameters
    ----------
    path:
        The dotted path to resolve.
    label:
        Human-readable name for the path used in :class:`ValueError`
        messages (e.g. ``"harness_call_llm"`` or ``"predicate"``).

    Returns
    -------
    Any
        The object at the resolved path.

    Raises
    ------
    ValueError
        The path is malformed, the module cannot be imported, or the
        attribute is absent.
    """
    if ":" in path:
        module_path, _, attr = path.partition(":")
    else:
        module_path, _, attr = path.rpartition(".")

    if not module_path or not attr:
        raise ValueError(f"{label}: {path!r} must be 'pkg.module.attr' or 'pkg.module:attr'")

    try:
        module = importlib.import_module(module_path)
    except ImportError as exc:
        raise ValueError(
            f"{label}: could not import module {module_path!r} from {path!r}: {exc}"
        ) from exc

    obj: Any = module
    for part in attr.split("."):
        if part == "<locals>":
            raise ValueError(
                f"{label}: {path!r} refers to a closure-local object that "
                "cannot be re-imported in a subprocess"
            )
        try:
            obj = getattr(obj, part)
        except AttributeError:
            raise ValueError(
                f"{label}: {path!r}: {module_path!r} has no attribute {part!r}"
            ) from None

    return obj


__all__ = ["import_dotted_path"]
