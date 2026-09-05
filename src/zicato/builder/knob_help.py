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

import inspect
import json
import re
from collections.abc import Mapping
from dataclasses import MISSING, asdict, fields, is_dataclass

from zicato.core.scoring_config import CONTRACT_KNOB_TYPES, ScoringWeights

_FIELDS_HEADING = re.compile(r"^Fields\n-+\n", re.MULTILINE)
_ENTRY_HEAD = re.compile(r"([a-z_][a-z0-9_]*):")
_ROLE = re.compile(r":[a-z]+:`([^`]*)`")
_LITERAL = re.compile(r"``([^`]*)``")
_STRONG = re.compile(r"\*\*([^*]+)\*\*")
_EMPHASIS = re.compile(r"(?<![\w*])\*(?!\s)([^*\n]+?)\*(?![\w*])")


def plain_text(text: str) -> str:
    """Return docstring markup as the plain text the builder shows.

    A cross-reference role keeps its target (``:attr:`X.y``` reads ``X.y``;
    a leading ``~`` keeps the last dotted component, as the rendered
    documentation does); a double-backtick literal and emphasis keep their
    text.
    """

    def role(match: re.Match[str]) -> str:
        target = match.group(1)
        if target.startswith("~"):
            return target[1:].rsplit(".", 1)[-1]
        return target

    text = _ROLE.sub(role, text)
    text = _LITERAL.sub(r"\1", text)
    text = _STRONG.sub(r"\1", text)
    return _EMPHASIS.sub(r"\1", text)


def _paragraphs(lines: list[str]) -> str:
    """Join an entry's lines into paragraphs; a bullet item keeps its own line."""
    paragraphs: list[str] = []
    items: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            if items:
                paragraphs.append("\n".join(items))
                items = []
        elif stripped.startswith("* ") or not items:
            items.append(stripped)
        else:
            items[-1] = f"{items[-1]} {stripped}"
    if items:
        paragraphs.append("\n".join(items))
    return "\n\n".join(paragraphs)


def field_docs(owner: type) -> dict[str, str]:
    """The ``Fields`` section of ``owner``'s docstring, one plain-text entry per field.

    An entry starts at a line holding the field name and a colon at the
    section's own indent; its body is the indented lines that follow, up to
    the next entry. A class without a ``Fields`` section documents no field.
    """
    doc = inspect.getdoc(owner) or ""
    heading = _FIELDS_HEADING.search(doc)
    if heading is None:
        return {}
    entries: dict[str, str] = {}
    name: str | None = None
    body: list[str] = []
    for line in doc[heading.end() :].splitlines():
        head = _ENTRY_HEAD.fullmatch(line)
        if head is not None:
            if name is not None:
                entries[name] = plain_text(_paragraphs(body))
            name, body = head.group(1), []
        elif name is not None:
            body.append(line)
    if name is not None:
        entries[name] = plain_text(_paragraphs(body))
    return entries


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
