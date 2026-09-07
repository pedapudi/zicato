"""The builder's knob help, read from the scoring configuration's docstrings.

Each contract knob is a field of :class:`~zicato.core.scoring_config.ScoringWeights`
or of one of its nested config dataclasses, and each of those classes documents
its fields in a ``Fields`` section of its docstring. That section is the one
text for a knob: :func:`knob_help` reads it and ``GET /builder/config`` serves
it, so the builder's help popovers show the docstrings and no copy of them
lives in the browser code.

The served map is keyed by the knob's contract path, the dotted key an
operator sees in ``scoring.json`` (``pass_weight``, ``overfitting.ladder.budget``),
and each value carries the help paragraphs as plain text and the default
rendered for display.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import MISSING, asdict, fields, is_dataclass

from zicato.core.field_docs import field_docs as field_docs
from zicato.core.field_docs import plain_text as plain_text
from zicato.core.scoring_config import CONTRACT_KNOB_TYPES, ScoringWeights


def render_default(value: object) -> str:
    """Render a field default the way the builder's popover shows it."""
    if isinstance(value, bool):
        return "on" if value else "off"
    if value is None:
        return "unset"
    if isinstance(value, str):
        return value or "empty"
    if isinstance(value, int | float):
        return repr(value)
    if isinstance(value, tuple):
        return " ".join(str(item) for item in value)
    if is_dataclass(value) and not isinstance(value, type):
        return json.dumps(asdict(value), sort_keys=True, default=str)
    if isinstance(value, Mapping):
        return json.dumps(dict(value), sort_keys=True, default=str)
    return repr(value)


def _field_default(declared_default: object, factory: object) -> object:
    if declared_default is not MISSING:
        return declared_default
    if factory is not MISSING and callable(factory):
        return factory()
    return MISSING


def knob_paths() -> dict[type, str]:
    """Each contract knob dataclass with the dotted prefix its fields carry.

    ``ScoringWeights`` fields carry no prefix; a nested config's fields carry
    the path of the field that holds it (``overfitting.ladder.`` for
    :class:`~zicato.core.scoring_config.LadderConfig`).
    """
    prefixes: dict[type, str] = {ScoringWeights: ""}

    def walk(owner: type, prefix: str) -> None:
        for declared in fields(owner):
            default = _field_default(declared.default, declared.default_factory)
            if type(default) in CONTRACT_KNOB_TYPES:
                nested = prefix + declared.name + "."
                prefixes[type(default)] = nested
                walk(type(default), nested)

    walk(ScoringWeights, "")
    return prefixes


def knob_help() -> dict[str, dict[str, str]]:
    """Every documented contract knob, keyed by contract path.

    Each value holds ``help`` (the field's docstring entry as plain text,
    paragraphs separated by a blank line) and ``default`` (the rendered
    field default). A field without a docstring entry is absent; the knob
    registry's guard test requires an entry for every knob the builder
    exposes.
    """
    served: dict[str, dict[str, str]] = {}
    for owner, prefix in knob_paths().items():
        docs = field_docs(owner)
        for declared in fields(owner):
            text = docs.get(declared.name)
            if text is None:
                continue
            default = _field_default(declared.default, declared.default_factory)
            served[prefix + declared.name] = {
                "help": text,
                "default": render_default(default),
            }
    return served


__all__ = ["field_docs", "knob_help", "knob_paths", "plain_text", "render_default"]
