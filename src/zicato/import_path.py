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

:func:`explain_attribute_error` lives here rather than in a utility module
because this is the shared home of symbol resolution: every loader that
does ``getattr(module, symbol)`` and reports a failure needs the same
distinction between "no such symbol" and "the symbol's construction
raised".
"""

from __future__ import annotations

import importlib
from typing import Any


def explain_attribute_error(container: Any, symbol: str, exc: AttributeError) -> str | None:
    """Explain an :class:`AttributeError` raised by ``getattr(container, symbol)``.

    Returns ``None`` when ``container`` genuinely has no ``symbol``, so the
    caller emits its own "no such symbol" wording. Returns an explanation
    string when the ACCESS is what failed: a PEP-562 module-level
    ``__getattr__`` or a property builds its object on access, so an
    ``AttributeError`` escaping that construction is not evidence the symbol
    is absent. Reporting it as absent points debugging at the wrong file.

    CPython stamps ``.name`` and ``.obj`` on every ``AttributeError`` that
    escapes an attribute access, including one raised inside ``__getattr__``
    (it fills them in when the raiser left them unset). That gives two
    signals:

    * ``.name`` naming a DIFFERENT attribute, or ``.obj`` being a different
      object, means the failure happened further in — construction raised,
      and we say so.
    * Otherwise the error really is about ``symbol`` on ``container``. We
      call that a genuine absence only when the message is the one the
      attribute machinery writes (or is empty, or is just the symbol name).
      A ``__getattr__`` that raises with prose of its own is telling the
      operator something; we pass that through instead of overwriting it.
    """
    name = getattr(exc, "name", None)
    obj = getattr(exc, "obj", None)

    if name is not None and name != symbol:
        return (
            f"resolving {symbol!r} raised {type(exc).__name__}: {exc} "
            "— the symbol exists but its construction failed"
        )
    if obj is not None and obj is not container:
        return (
            f"resolving {symbol!r} raised {type(exc).__name__}: {exc} "
            "— the symbol exists but its construction failed"
        )

    message = str(exc)
    machinery = (
        not message
        or message == symbol
        or f"has no attribute {symbol!r}" in message
        or f"has no {symbol!r} symbol" in message
    )
    if machinery:
        return None
    return (
        f"resolving {symbol!r} raised {type(exc).__name__}: {message} "
        "— the access raised rather than reporting a plain absence"
    )


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
        except AttributeError as exc:
            detail = explain_attribute_error(obj, part, exc)
            if detail is not None:
                raise ValueError(f"{label}: {path!r}: {detail}") from exc
            raise ValueError(
                f"{label}: {path!r}: {module_path!r} has no attribute {part!r}"
            ) from exc

    return obj


__all__ = ["explain_attribute_error", "import_dotted_path"]
