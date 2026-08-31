"""Declared bounds for evaluation-contract knobs.

A bounded knob names the values it admits once, in the metadata of the
dataclass field that holds it. The contract loader applies every
declaration when it builds the frozen contract, and the tournament builder
consults the same declaration before it edits a draft, so one rule has one
wording whichever surface refuses the value.

Rejection messages name the CONTRACT field rather than the argument the
surface happens to spell: ``set_screening(entries=-1)`` is refused as
``screen_entries must be >= 0``, because ``screen_entries`` is the name the
saved contract and every error downstream of it use.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, fields
from typing import Any

#: Key under which a :class:`KnobConstraint` is stored in a field's
#: ``metadata`` mapping.
CONSTRAINT_KEY = "constraint"


def require_finite_number(name: str, value: object) -> int | float:
    """Return ``value`` as a number, rejecting a non-numeric or non-finite one.

    A scoring contract controls promotion decisions, so ``NaN`` and infinities
    cannot be treated as ordinary numeric values.  In particular, comparison
    with ``NaN`` is always false and can otherwise bypass a gate condition.
    JSON accepts those spellings by default, and direct dataclass construction
    can supply them too, so validation belongs at the frozen-contract boundary.
    This is the same policy :func:`zicato.scoring.transforms.validate_transform_spec`
    already applies to transform params, generalized to the rest of the contract.

    The accepted value is returned unconverted, so a caller that goes on to
    compare it against a bound has a number the type checker recognises. An
    ``int`` too large to convert (``float(10**400)`` raises ``OverflowError``)
    therefore still passes through as the ``int`` it is.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"{name} must be a finite number, got {value!r}")
    # An ``int`` is finite by construction, and JSON can carry one too large to
    # convert, so only a ``float`` needs the explicit finiteness test.
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"{name} must be finite, got {value!r}")
    return value


def require_finite_mapping(name: str, values: Mapping[str, object]) -> None:
    """Validate every numeric coefficient in one scoring-weight mapping."""
    for key, value in values.items():
        require_finite_number(f"{name}[{key!r}]", value)


def _bound(minimum: float) -> str:
    """Render a bound the way an operator writes it: ``0`` rather than ``0.0``."""
    return str(int(minimum)) if float(minimum).is_integer() else str(minimum)


@dataclass(frozen=True, slots=True)
class KnobConstraint:
    """The set of values one evaluation-contract knob admits.

    A constraint with no ``minimum`` and no ``choices`` still requires a
    finite number — the floor every numeric contract knob sits on.

    Fields
    ------
    minimum:
        Inclusive lower bound. ``None`` leaves the knob unbounded below.
    choices:
        The closed vocabulary a string-valued knob may hold. Mutually
        exclusive with ``minimum``.
    allow_none:
        Whether ``None`` is a value in its own right — the knob's "auto" or
        "unset" token — rather than a missing number.
    label:
        The name to use in the rejection message when it differs from the
        field name: the dotted path of a knob nested in a contract block
        (``ladder.budget``), or the key of an otherwise opaque mapping.
    """

    minimum: float | None = None
    choices: tuple[str, ...] | None = None
    allow_none: bool = False
    label: str = ""

    def check(self, name: str, value: object) -> None:
        """Raise :class:`ValueError` when ``value`` is outside the knob's domain."""
        shown = self.label or name
        if self.allow_none and value is None:
            return
        if self.choices is not None:
            if value not in self.choices:
                known = ", ".join(sorted(self.choices))
                raise ValueError(f"{shown} must be one of {{{known}}}, got {value!r}")
            return
        number = require_finite_number(shown, value)
        if self.minimum is not None and number < self.minimum:
            suffix = " or None" if self.allow_none else ""
            raise ValueError(f"{shown} must be >= {_bound(self.minimum)}{suffix}, got {value!r}")


def validate_knobs(instance: Any) -> None:
    """Apply every constraint declared on ``instance``'s fields.

    Called from the ``__post_init__`` of each frozen contract dataclass, so
    a knob is bounded by declaring the bound and nothing else.
    """
    for declared in fields(instance):
        constraint = declared.metadata.get(CONSTRAINT_KEY)
        if isinstance(constraint, KnobConstraint):
            constraint.check(declared.name, getattr(instance, declared.name))


def knob_constraint(owner: type[Any], name: str) -> KnobConstraint:
    """Return the constraint declared on one contract field.

    Raises :class:`KeyError` for an unknown field or one that declares no
    constraint, so a surface that delegates its validation here cannot
    quietly stop validating when a declaration is dropped.
    """
    for declared in fields(owner):
        if declared.name == name:
            constraint = declared.metadata.get(CONSTRAINT_KEY)
            if isinstance(constraint, KnobConstraint):
                return constraint
            raise KeyError(f"{owner.__name__}.{name} declares no constraint")
    raise KeyError(f"{owner.__name__} has no field {name!r}")


def require_knob(owner: type[Any], name: str, value: object) -> None:
    """Refuse a value the named contract knob's declared constraint forbids.

    The entry point for a surface that validates BEFORE constructing the
    contract dataclass — the tournament builder rejects an edit at the
    operation boundary, where it can name the argument, instead of letting
    the draft carry the value to a later ``dataclasses.replace``.
    """
    knob_constraint(owner, name).check(name, value)
