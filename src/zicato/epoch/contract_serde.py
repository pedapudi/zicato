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
``scoring.json`` use). The field's ``persisted_name`` metadata records
that spelling for the writer, authored decoder, and historical decoder.
Every other field is written under its own name,
so the on-disk output for an already-correct contract is byte-identical
to the previous hand-written form.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import MISSING, fields, is_dataclass, replace
from typing import TYPE_CHECKING, Any, TypeVar, cast, get_args, get_origin

from zicato.core.configuration import dataclass_to_jsonable, persisted_key

if TYPE_CHECKING:
    from dataclasses import Field

    from zicato.core.scoring_config import ScoringWeights

_T = TypeVar("_T")


def historical_dataclass_from_json(cls: type[_T], data: Mapping[str, Any]) -> _T:
    """Decode historical records with their compatible conversions and defaults.

    Authored input uses ``core.configuration.authored_dataclass_from_json``;
    this reader retains the conversion rules of persisted contract records.

    The inverse of :func:`dataclass_to_jsonable`. Every field is resolved
    by enumerating ``dataclasses.fields()``:

    * a key present in ``data`` is parsed (coerced to the field's declared
      scalar type, recursed for nested dataclasses, copied for mappings /
      sequences);
    * a key absent from ``data`` uses its persisted historical default when
      declared, otherwise its constructor default. Historical defaults preserve
      execution when authored defaults change. Canonical omission metadata
      separately preserves the recorded identity of those values.
    """
    if not (isinstance(cls, type) and is_dataclass(cls)):
        raise TypeError(f"historical_dataclass_from_json expects a dataclass type, got {cls!r}")
    kwargs: dict[str, Any] = {}
    for f in fields(cls):
        if not f.init:
            continue
        key = persisted_key(f)
        if key not in data:
            if "historical_default" in f.metadata:
                kwargs[f.name] = f.metadata["historical_default"]
                continue
            if "historical_default_factory" in f.metadata:
                kwargs[f.name] = f.metadata["historical_default_factory"]()
                continue
            # Absent ⇒ let the dataclass default fill it in. We only skip
            # the kwarg when the field actually HAS a default; a required
            # field with no default would (correctly) raise on construction.
            if _has_default(f):
                continue
        raw = data.get(key)
        kwargs[f.name] = _value_from_jsonable(f.type, raw)
    return cast("_T", cls(**kwargs))


def recorded_experimental_values(data: Mapping[str, Any]) -> dict[str, tuple[str, Any]]:
    """Read feature values stored at their declared historical scoring paths."""
    from zicato.core.scoring_config import ExperimentalConfig  # noqa: PLC0415

    found = {}
    for declared in fields(ExperimentalConfig):
        path = declared.metadata.get("recorded_path")
        if path is None:
            continue
        value: Any = data
        for part in path.split("."):
            if not isinstance(value, Mapping) or part not in value:
                break
            value = value[part]
        else:
            found[declared.name] = (path, value)
    return found


def _relocate_recorded_features(data: Mapping[str, Any]) -> Mapping[str, Any]:
    """Move archived feature settings into their runtime owner without editing files."""
    moved = recorded_experimental_values(data)
    if not moved:
        return data
    result = dict(data)
    raw_experimental = data.get("experimental")
    experimental = dict(raw_experimental) if isinstance(raw_experimental, Mapping) else {}
    for name, (path, value) in moved.items():
        if name in experimental:
            raise ValueError(f"recorded scoring contains both {path} and experimental.{name}")
        # Archived strategy parameters accepted a recognized token or disabled
        # the extension. Preserve that recorded behavior at the typed boundary.
        if name in {"standing_rating", "resolver"}:
            token = value.strip().lower() if isinstance(value, str) else "none"
            choices = (
                {"bradley_terry"} if name == "standing_rating" else {"copeland", "ranked_pairs"}
            )
            value = token if token in choices else "none"
        experimental[name] = value
        target = result
        parts = path.split(".")
        for part in parts[:-1]:
            target[part] = dict(target[part])
            target = target[part]
        del target[parts[-1]]
    # The former memory block has no other fields.
    if "cross_epoch_memory" in moved:
        result.pop("experiment_memory", None)
    result["experimental"] = experimental
    return result


def historical_scoring_from_json(data: Mapping[str, Any]) -> ScoringWeights:
    """Decode recorded scoring, preserving the retired additive release rule.

    The recorded increment was a fixed addition to the release threshold.
    Resolve it using this record's margin before discarding the retired field.
    The original mapping remains available for historical identity checks.
    """
    from zicato.core.scoring_config import ScoringWeights  # noqa: PLC0415

    weights = historical_dataclass_from_json(ScoringWeights, _relocate_recorded_features(data))
    overfitting = data.get("overfitting")
    ladder = overfitting.get("ladder") if isinstance(overfitting, Mapping) else None
    if not isinstance(ladder, Mapping) or "noise_scale" not in ladder:
        return weights
    increment = float(ladder["noise_scale"])
    if not math.isfinite(increment) or increment < 0:
        raise ValueError("recorded ladder.noise_scale must be finite and >= 0")
    if increment == 0:
        return weights
    cfg = weights.overfitting.ladder
    threshold = weights.promote_margin if cfg.threshold is None else cfg.threshold
    return replace(
        weights,
        overfitting=replace(
            weights.overfitting, ladder=replace(cfg, threshold=threshold + increment)
        ),
    )


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
        return historical_dataclass_from_json(resolved, mapping)

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
    # enum-free and Path-free to their leaves; add the branch if one of them
    # grows an enum or a Path.
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
    "historical_dataclass_from_json",
]
