"""Plain field descriptions shared by configuration editors and reference output."""

from __future__ import annotations

import inspect
import re
from dataclasses import fields, is_dataclass

_FIELDS_HEADING = re.compile(r"^Fields\n-+\n", re.MULTILINE)
_ENTRY_HEAD = re.compile(r"([a-z_][a-z0-9_]*):")
_ROLE = re.compile(r":[a-z]+:`([^`]*)`")
_LITERAL = re.compile(r"``([^`]*)``")
_STRONG = re.compile(r"\*\*([^*]+)\*\*")
_EMPHASIS = re.compile(r"(?<![\w*])\*(?!\s)([^*\n]+?)\*(?![\w*])")


def plain_text(text: str) -> str:
    """Convert docstring markup to plain text for configuration descriptions.

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


def _declared_field_docs(owner: type) -> dict[str, str]:
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


def field_docs(owner: type) -> dict[str, str]:
    """Read inherited descriptions and explicit field metadata from their owners."""
    result: dict[str, str] = {}
    for declared in reversed(owner.__mro__):
        result.update(_declared_field_docs(declared))
    if is_dataclass(owner):
        for item in fields(owner):
            description = item.metadata.get("description")
            if description:
                result[item.name] = str(description)
    return result
