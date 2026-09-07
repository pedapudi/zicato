"""Type admission shared by direct, REST, and tool-driven scoring edits."""

from __future__ import annotations

import inspect
from collections.abc import Callable, Mapping
from functools import wraps
from typing import ParamSpec, TypeVar, get_type_hints

from zicato.core.configuration import validate_authored_value
from zicato.core.scoring_config import contract_knobs

P = ParamSpec("P")
T = TypeVar("T")


def authored_edit(operation: Callable[P, T]) -> Callable[P, T]:
    """Refuse malformed supplied values before equality checks or conversions.

    Operation annotations retain edit semantics such as null for no edit and
    negative holdout margin for clearing a value. Persisted field constraints
    still validate the resulting contract. Nested mapping arguments derive their
    field types from the scoring registry used by the builder's controls.
    """
    signature = inspect.signature(operation)
    annotations = get_type_hints(operation)
    nested = tuple(
        knob
        for knob in contract_knobs()
        if knob.builder_op == operation.__name__ and "." in knob.builder_arg
    )

    @wraps(operation)
    def checked(*args: P.args, **kwargs: P.kwargs) -> T:
        supplied = signature.bind(*args, **kwargs).arguments
        for name, value in supplied.items():
            if name != "draft":
                validate_authored_value(
                    annotations[name], value, path=f"{operation.__name__}.{name}"
                )
        for knob in nested:
            argument, key = knob.builder_arg.split(".", 1)
            value = supplied.get(argument)
            if isinstance(value, Mapping) and key in value:
                validate_authored_value(
                    get_type_hints(knob.owner)[knob.name],
                    value[key],
                    path=f"{operation.__name__}.{knob.builder_arg}",
                )
        return operation(*args, **kwargs)

    return checked
