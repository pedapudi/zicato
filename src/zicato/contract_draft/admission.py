"""Validate supplied values for scoring edits."""

from __future__ import annotations

import inspect
from collections.abc import Callable
from functools import wraps
from typing import ParamSpec, TypeVar, get_type_hints

from zicato.core.configuration import validate_authored_value

P = ParamSpec("P")
T = TypeVar("T")


def authored_edit(operation: Callable[P, T]) -> Callable[P, T]:
    """Refuse malformed supplied values before equality checks or conversions.

    Operation annotations retain edit semantics such as null for no edit and
    negative holdout margin for clearing a value. Persisted field constraints
    still validate the resulting contract. Nested mapping arguments derive their
    field types at the operation that owns the mapping.
    """
    signature = inspect.signature(operation)
    annotations = get_type_hints(operation)

    @wraps(operation)
    def checked(*args: P.args, **kwargs: P.kwargs) -> T:
        supplied = signature.bind(*args, **kwargs).arguments
        for name, value in supplied.items():
            if name != "draft":
                validate_authored_value(
                    annotations[name], value, path=f"{operation.__name__}.{name}"
                )
        return operation(*args, **kwargs)

    return checked
