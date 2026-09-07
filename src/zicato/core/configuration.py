"""Strict authored values and editor schemas derived from dataclass declarations."""

from __future__ import annotations

import math
import types
from collections.abc import Mapping
from dataclasses import MISSING, Field, fields, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Literal, TypeVar, Union, cast, get_args, get_origin, get_type_hints

from zicato.core.constraints import KnobConstraint
from zicato.core.field_docs import field_docs

T = TypeVar("T")


class ConfigurationError(ValueError):
    """An authored value violates the declaration at a persisted field path."""

    def __init__(self, path: str, category: str, detail: str) -> None:
        self.path = path
        self.category = category
        super().__init__(f"{path}: {detail}")


def persisted_key(declared: Field[Any]) -> str:
    """The JSON spelling belongs to the same field as its type and default."""
    return str(declared.metadata.get("persisted_name") or declared.name)


def authored_dataclass_from_json(cls: type[T], raw: object, *, path: str) -> T:
    """Validate authored JSON before constructing any configuration object."""
    values = _authored_values(cls, raw, path=path, construct=True)
    try:
        return cls(**values)
    except ConfigurationError:
        raise
    except ValueError as exc:
        raise ConfigurationError(path, "value", str(exc)) from exc


def validate_authored_overlay(cls: type[Any], raw: object, *, path: str) -> None:
    """Validate supplied fields; constraints between fields wait for composition."""
    _authored_values(cls, raw, path=path, construct=False)


def validate_authored_value(annotation: Any, raw: object, *, path: str) -> None:
    """Check an operation argument against its declaration before editing a draft."""
    _decode(annotation, raw, path)


def _authored_values(cls: type[Any], raw: object, *, path: str, construct: bool) -> dict[str, Any]:
    if not is_dataclass(cls):
        raise TypeError(f"expected a configuration dataclass, got {cls}")
    if not isinstance(raw, Mapping):
        raise ConfigurationError(path, "type", "expected an object")
    declared = {persisted_key(item): item for item in fields(cls) if item.init}
    for key in raw:
        if key not in declared:
            accepted = ", ".join(sorted(declared))
            raise ConfigurationError(
                f"{path}.{key}", "unknown", f"unknown field; accepted: {accepted}"
            )
    annotations = get_type_hints(cls)
    values: dict[str, Any] = {}
    for key, item in declared.items():
        location = f"{path}.{key}"
        if key in raw and raw[key] is None and item.metadata.get("null_uses_default"):
            continue
        if key not in raw:
            if construct and item.default is MISSING and item.default_factory is MISSING:
                raise ConfigurationError(location, "missing", "required field is absent")
            continue
        supplied = raw[key]
        if item.metadata.get("case_insensitive") and type(supplied) is str:
            supplied = supplied.upper()
        value = _decode(annotations[item.name], supplied, location, construct=construct)
        constraint = item.metadata.get("constraint")
        if isinstance(constraint, KnobConstraint):
            try:
                constraint.check(item.name, value)
            except ValueError as exc:
                raise ConfigurationError(location, "range", str(exc)) from exc
        values[item.name] = value
    return values


def _decode(annotation: Any, raw: object, path: str, *, construct: bool = True) -> Any:
    origin, args = get_origin(annotation), get_args(annotation)
    if origin in (Union, types.UnionType):
        if raw is None and type(None) in args:
            return None
        candidates = [arg for arg in args if arg is not type(None)]
        if len(candidates) == 1:
            return _decode(candidates[0], raw, path, construct=construct)
        for candidate in candidates:
            try:
                return _decode(candidate, raw, path, construct=construct)
            except ConfigurationError:
                continue
        raise ConfigurationError(path, "type", f"expected one of {candidates}")
    if isinstance(annotation, type) and is_dataclass(annotation):
        if not construct:
            return _authored_values(annotation, raw, path=path, construct=False)
        return authored_dataclass_from_json(annotation, raw, path=path)
    if origin in (Mapping, dict):
        if not isinstance(raw, Mapping):
            raise ConfigurationError(path, "type", "expected an object")
        if any(type(key) is not str for key in raw):
            raise ConfigurationError(path, "type", "expected string object keys")
        return {
            key: _decode(args[1], value, f"{path}.{key}", construct=construct)
            for key, value in raw.items()
        }
    if origin in (tuple, list):
        if not isinstance(raw, list | tuple):
            raise ConfigurationError(path, "type", "expected an array")
        values = [
            _decode(args[0], value, f"{path}[{index}]", construct=construct)
            for index, value in enumerate(raw)
        ]
        return tuple(values) if origin is tuple else values
    if origin is Literal:
        if not any(type(raw) is type(value) and raw == value for value in args):
            raise ConfigurationError(path, "value", f"expected one of {args}; got {raw!r}")
        return raw
    if isinstance(annotation, type) and issubclass(annotation, Enum):
        if not any(type(raw) is type(member.value) for member in annotation):
            raise ConfigurationError(path, "type", "expected an enum value of its declared type")
        try:
            return annotation(raw)
        except ValueError as exc:
            raise ConfigurationError(path, "value", f"expected one of {list(annotation)}") from exc
    if annotation is Path:
        if type(raw) is not str:
            raise ConfigurationError(path, "type", "expected a path string")
        return Path(raw).expanduser()
    if annotation is Any:
        if isinstance(raw, Mapping):
            return _decode(Mapping[str, Any], raw, path)
        if isinstance(raw, list | tuple):
            return _decode(list[Any], raw, path)
        if raw is None or type(raw) in (str, bool, int):
            return raw
        if type(raw) is float and math.isfinite(raw):
            return raw
        raise ConfigurationError(path, "type", "expected a finite JSON value")
    if annotation is float:
        if type(raw) not in (int, float):
            raise ConfigurationError(path, "type", "expected a finite number")
        try:
            value = float(cast("int | float", raw))
        except OverflowError as exc:
            raise ConfigurationError(path, "range", "expected a finite number") from exc
        if not math.isfinite(value):
            raise ConfigurationError(path, "range", "expected a finite number")
        return value
    expected = {bool: "a boolean", int: "an integer", str: "a string", type(None): "null"}
    if annotation in expected:
        if type(raw) is not annotation:
            raise ConfigurationError(path, "type", f"expected {expected[annotation]}")
        return raw
    raise TypeError(f"unsupported configuration declaration {annotation!r} at {path}")


def dataclass_to_jsonable(obj: Any) -> dict[str, Any]:
    """Copy every declared field to JSON under its persisted spelling."""
    if not is_dataclass(obj) or isinstance(obj, type):
        raise TypeError(f"dataclass_to_jsonable expects a dataclass instance, got {obj!r}")
    return {persisted_key(item): _json_value(getattr(obj, item.name)) for item in fields(obj)}


def _json_value(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return dataclass_to_jsonable(value)
    if isinstance(value, Mapping):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_json_value(item) for item in value]
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    return value


def dataclass_schema(cls: type[Any]) -> dict[str, Any]:
    """Generate a closed object schema from the same fields the decoder validates."""
    annotations = get_type_hints(cls)
    descriptions = field_docs(cls)
    properties: dict[str, Any] = {}
    required = []
    for item in fields(cls):
        if not item.init:
            continue
        key = persisted_key(item)
        schema = _type_schema(annotations[item.name])
        if item.metadata.get("null_uses_default"):
            schema = {"anyOf": [schema, {"type": "null"}]}
        description = item.metadata.get("description") or descriptions.get(item.name)
        if description:
            schema["description"] = description
        if item.metadata.get("null_uses_default"):
            schema["description"] = schema.get("description", "") + " Null selects the default."
        for metadata_key in ("scope", "rolls_epoch", "secret_reference", "cli"):
            if metadata_key in item.metadata:
                schema[f"x-{metadata_key.replace('_', '-')}"] = item.metadata[metadata_key]
        constraint = item.metadata.get("constraint")
        if isinstance(constraint, KnobConstraint):
            if constraint.minimum is not None:
                name = "exclusiveMinimum" if constraint.exclusive_minimum else "minimum"
                schema[name] = constraint.minimum
            if constraint.maximum is not None:
                name = "exclusiveMaximum" if constraint.exclusive_maximum else "maximum"
                schema[name] = constraint.maximum
            if constraint.choices is not None:
                if item.metadata.get("case_insensitive"):
                    words = [
                        "".join(f"[{letter.lower()}{letter.upper()}]" for letter in choice)
                        for choice in constraint.choices
                    ]
                    schema["pattern"] = "^(" + "|".join(words) + r")(?![\s\S])"
                else:
                    schema["enum"] = list(constraint.choices)
                    if constraint.allow_none:
                        schema["enum"].append(None)
        if item.default is MISSING and item.default_factory is MISSING:
            required.append(key)
        elif item.default is not MISSING:
            schema["default"] = _json_value(item.default)
        elif item.default_factory is not MISSING:
            schema["default"] = _json_value(item.default_factory())
        _project_nested_defaults(schema)
        properties[key] = schema
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def _project_nested_defaults(schema: dict[str, Any]) -> None:
    """Describe nested defaults from their owning field's resolved factory."""
    default = schema.get("default")
    if isinstance(default, Mapping):
        for key, child in schema.get("properties", {}).items():
            if key in default:
                child["default"] = default[key]
                _project_nested_defaults(child)


def _type_schema(annotation: Any) -> dict[str, Any]:
    origin, args = get_origin(annotation), get_args(annotation)
    if origin in (Union, types.UnionType):
        return {"anyOf": [_type_schema(arg) for arg in args]}
    if isinstance(annotation, type) and is_dataclass(annotation):
        return dataclass_schema(annotation)
    if origin in (Mapping, dict):
        return {"type": "object", "additionalProperties": _type_schema(args[1])}
    if origin in (tuple, list):
        return {"type": "array", "items": _type_schema(args[0])}
    if origin is Literal:
        return {"enum": list(args)}
    if isinstance(annotation, type) and issubclass(annotation, Enum):
        return {"enum": [item.value for item in annotation]}
    if annotation is Any:
        return {}
    kinds = {
        bool: "boolean",
        int: "integer",
        float: "number",
        str: "string",
        Path: "string",
        type(None): "null",
    }
    if annotation not in kinds:
        raise TypeError(f"unsupported configuration declaration {annotation!r}")
    return {"type": kinds[annotation]}
