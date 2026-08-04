"""Field-enumerating serialization for the frozen evaluation contract.

The epoch contract — :class:`~zicato.core.types.ScoringWeights` and its
nested config dataclasses (:class:`~zicato.core.types.TournamentStructure`,
:class:`~zicato.core.types.OverfittingConfig`,
:class:`~zicato.core.types.LadderConfig`) — is persisted to a per-epoch
frozen ``scoring.json`` and later re-read to recompute the contract hash.
The contract hash itself is derived by a *field-enumerating* canonicalizer
(:func:`zicato.epoch.contract.scoring_to_canon`) that walks
``dataclasses.fields()`` and therefore covers every field automatically.

Historically the on-disk writer and parser were hand-maintained,
field-by-field dicts that had to be kept in lock-step with that
canonicalizer by hand. When a new field was added to a contract dataclass
and threaded through the canonicalizer (which enumerates fields) but NOT
into the hand-written writer, the frozen snapshot silently dropped the
field. On the next ``evolve`` the live contract (carrying the field)
hashed differently from the frozen contract (where the field resolved to
its default) and the orchestrator performed a *spurious* epoch auto-roll.

This module removes the hand-maintenance: the snapshot writer and parser
are derived from ``dataclasses.fields()`` and recurse into nested
dataclasses generically, so adding any scalar or nested field is covered
automatically — the same property the canonicalizer already has. The
general invariant the serializer must uphold is::

    from_dict(to_dict(x)) == x      # round-trip identity, every field
    canon(x) == canon(from_dict(to_dict(x)))   # no spurious auto-roll

Key naming
----------
One field carries a *persisted-key alias* for backwards compatibility:
``ScoringWeights.tournament_structure`` is written under the on-disk key
``"tournament"`` (the shape the dashboard builder and every existing
``scoring.json`` use). :data:`_KEY_ALIASES` records that single rename so
the field-enumerating writer keeps emitting the historical key and the
parser keeps reading it. Every other field is written under its own name,
so the on-disk output for an already-correct contract is byte-identical
to the previous hand-written form.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import MISSING, fields, is_dataclass
from typing import TYPE_CHECKING, Any, TypeVar, cast, get_args, get_origin

if TYPE_CHECKING:
    from dataclasses import Field

_T = TypeVar("_T")

#: Per-dataclass map of ``field name -> on-disk key`` for fields whose
#: persisted key differs from the attribute name. Only one historical
#: rename exists: the scoring weights' ``tournament_structure`` field is
#: stored under ``"tournament"``. Keeping this explicit (rather than
#: deriving keys purely from field names) preserves byte-identical on-disk
#: output for already-correct contracts while the writer stays
#: field-enumerating.
_KEY_ALIASES: dict[str, dict[str, str]] = {
    "ScoringWeights": {"tournament_structure": "tournament"},
}


def _persisted_key(cls_name: str, field_name: str) -> str:
    """Return the on-disk key for ``field_name`` on dataclass ``cls_name``."""
    return _KEY_ALIASES.get(cls_name, {}).get(field_name, field_name)


def dataclass_to_jsonable(obj: Any) -> dict[str, Any]:
    """Serialize a frozen contract dataclass to a JSON-shaped dict.

    Field-enumerating and recursive: every field declared on the
    dataclass is written (under its persisted key — see
    :data:`_KEY_ALIASES`), nested dataclasses recurse, mappings are
    copied to plain ``dict``, and tuples/lists become JSON lists. Adding a
    new field to a contract dataclass is therefore covered with no edit
    here, which is exactly the property that prevents the frozen snapshot
    from silently dropping fields.
    """
    if not is_dataclass(obj) or isinstance(obj, type):
        raise TypeError(f"dataclass_to_jsonable expects a dataclass instance, got {obj!r}")
    cls_name = type(obj).__name__
    out: dict[str, Any] = {}
    for f in fields(obj):
        value = getattr(obj, f.name)
        out[_persisted_key(cls_name, f.name)] = _value_to_jsonable(value)
    return out


def _value_to_jsonable(value: Any) -> Any:
    """Recursively reduce a single field value to a JSON-shaped form."""
    if is_dataclass(value) and not isinstance(value, type):
        return dataclass_to_jsonable(value)
    if isinstance(value, Mapping):
        return {k: _value_to_jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_value_to_jsonable(v) for v in value]
    return value


def jsonable_to_dataclass(cls: type[_T], data: Mapping[str, Any]) -> _T:
    """Build a contract dataclass from a JSON-shaped dict, field by field.

    The inverse of :func:`dataclass_to_jsonable`. Every field is resolved
    by enumerating ``dataclasses.fields()``:

    * a key present in ``data`` is parsed (coerced to the field's declared
      scalar type, recursed for nested dataclasses, copied for mappings /
      sequences);
    * a key absent from ``data`` falls back to the field's default — so a
      legacy ``scoring.json`` written before a field existed loads at that
      field's default, exactly as the hand-written parsers did.

    Because absent fields fall back to their declared default and the
    contract canonicalizer resolves the same defaults, the contract hash
    for an unchanged on-disk contract is unaffected by this parser.
    """
    if not (isinstance(cls, type) and is_dataclass(cls)):
        raise TypeError(f"jsonable_to_dataclass expects a dataclass type, got {cls!r}")
    cls_name = cls.__name__
    kwargs: dict[str, Any] = {}
    for f in fields(cls):
        if not f.init:
            continue
        key = _persisted_key(cls_name, f.name)
        if key not in data:
            # Absent ⇒ let the dataclass default fill it in. We only skip
            # the kwarg when the field actually HAS a default; a required
            # field with no default would (correctly) raise on construction.
            if _has_default(f):
                continue
        raw = data.get(key)
        kwargs[f.name] = _value_from_jsonable(f.type, raw)
    return cast("_T", cls(**kwargs))


def _has_default(f: Field[Any]) -> bool:
    """True iff dataclass field ``f`` declares a default or default_factory."""
    return f.default is not MISSING or f.default_factory is not MISSING


def _value_from_jsonable(field_type: Any, raw: Any) -> Any:
    """Coerce a JSON-shaped value into the field's declared type.

    Nested contract dataclasses recurse; ``Optional`` unwraps ``None``;
    mappings / tuples are rebuilt with their element types coerced; scalar
    leaves (``float`` / ``int`` / ``bool`` / ``str``) are coerced so a
    JSON int round-trips back into a float field the same way the
    hand-written parsers coerced them.
    """
    resolved = _resolve_type(field_type)

    # Optional[...] / X | None — unwrap None, recurse on the inner type.
    inner = _optional_inner(resolved)
    if inner is not None:
        if raw is None:
            return None
        return _value_from_jsonable(inner, raw)

    if isinstance(resolved, type) and is_dataclass(resolved):
        mapping = raw if isinstance(raw, Mapping) else {}
        return jsonable_to_dataclass(resolved, mapping)

    origin = get_origin(resolved)
    if origin in (tuple,):
        args = get_args(resolved)
        elem_type = args[0] if args else Any
        seq = raw if isinstance(raw, list | tuple) else ()
        return tuple(_value_from_jsonable(elem_type, v) for v in seq)
    if origin in (list,):
        args = get_args(resolved)
        elem_type = args[0] if args else Any
        seq = raw if isinstance(raw, list | tuple) else []
        return [_value_from_jsonable(elem_type, v) for v in seq]
    if origin in (Mapping, dict):
        args = get_args(resolved)
        val_type = args[1] if len(args) == 2 else Any
        mapping = raw if isinstance(raw, Mapping) else {}
        return {str(k): _value_from_jsonable(val_type, v) for k, v in mapping.items()}

    # Scalar leaves. Coerce so JSON's int/float/bool fluidity does not
    # leak a wrongly-typed value into a frozen dataclass.
    if resolved is bool:
        return bool(raw)
    if resolved is float:
        return float(raw)
    if resolved is int:
        return int(raw)
    if resolved is str:
        return str(raw)
    # Everything else passes through as the raw JSON value. Correct for
    # ``Literal`` and ``Any``; a TRAP for ``Enum`` and ``Path``, which have
    # no branch above — a contract dataclass that grows a StrEnum or Path
    # field would hydrate it as a bare str while the in-process value
    # carries the declared type (issue #132). The three dataclasses routed
    # through here — ScoringWeights, OverfittingConfig, LadderConfig — are
    # enum-free and Path-free to their leaves today; add the branch when
    # that changes.
    return raw


def _resolve_type(field_type: Any) -> Any:
    """Resolve a (possibly stringised) annotation to a usable type object.

    ``from __future__ import annotations`` stores field types as strings.
    Resolve them against :mod:`zicato.core.types`' namespace so the
    recursive parser can introspect nested dataclasses / generics.
    """
    if not isinstance(field_type, str):
        return field_type
    import zicato.core.types as _types  # noqa: PLC0415

    ns = vars(_types)
    try:
        return eval(field_type, ns)  # noqa: S307 — trusted module-local annotations
    except Exception:  # noqa: BLE001 — fall back to the raw string on any failure
        return field_type


def _optional_inner(resolved: Any) -> Any:
    """If ``resolved`` is ``Optional[X]`` (``X | None``), return ``X``; else ``None``."""
    import types as _stdtypes  # noqa: PLC0415
    import typing as _typing  # noqa: PLC0415

    origin = get_origin(resolved)
    if origin is _typing.Union or origin is getattr(_stdtypes, "UnionType", object()):
        args = [a for a in get_args(resolved) if a is not type(None)]
        if len(args) == 1 and len(get_args(resolved)) == 2:
            return args[0]
    return None


__all__ = [
    "dataclass_to_jsonable",
    "jsonable_to_dataclass",
]
