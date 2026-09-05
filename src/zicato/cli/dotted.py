"""Resolving an operator-typed dotted path at the command line.

Several commands take a callable as a ``pkg.module:attr`` string —
``board audit`` and ``board preflight`` take the two a measurement run
needs, ``board judges`` takes the evaluation one, ``inspect reflection
run`` takes the independent adjudicator. Each of those is an option the
operator typed, so a bad path is a usage error and belongs in click's
formatting rather than a traceback.

``zicato evolve`` is not in that list, by design: which callable a
model role runs on is a property of the workspace, declared once under
``models.engines`` in ``config.json`` and resolved by
:func:`zicato.runtime_factory.resolve_role_call_llm`.
"""

from __future__ import annotations

from typing import Any

import click

from zicato.import_path import import_dotted_path


def import_callable(dotted: str, *, kind: str) -> Any:
    """Resolve ``pkg.mod:attr`` or ``pkg.mod.attr`` to a callable.

    Delegates to :func:`zicato.import_path.import_dotted_path` and re-raises
    any :class:`ValueError` as :class:`click.BadParameter` so the CLI surfaces
    the error with Click's formatting rather than a raw traceback. ``kind``
    names the option the path came from, so the message points at the
    field the operator must fix.
    """
    try:
        fn: Any = import_dotted_path(dotted, label=kind)
    except ValueError as exc:
        raise click.BadParameter(str(exc)) from exc
    if not callable(fn):
        raise click.BadParameter(
            f"{kind}: {dotted!r} resolved to {type(fn).__name__}, expected a callable"
        )
    return fn


__all__ = ["import_callable"]
