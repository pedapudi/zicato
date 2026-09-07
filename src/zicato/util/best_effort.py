"""Report optional-operation failures without invalidating authoritative work.

Failures enter the existing operational log with operation and exception-type
fields. Worker logs retain their run coordinates; round logs retain the invoking
scope. Health reads this saved evidence after the producing process has exited.
Only explicitly optional work belongs inside this context manager.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager

__all__ = ["best_effort", "report_optional_failure"]

log = logging.getLogger("zicato.util.best_effort")


def report_optional_failure(operation: str, exc: Exception) -> None:
    """Persist a diagnostic without retaining potentially sensitive exception text."""
    log.warning(
        "Optional operation %s failed (%s)",
        operation,
        type(exc).__name__,
        extra={"fields": {"operation": operation, "exception_type": type(exc).__name__}},
    )


@contextmanager
def best_effort(
    label: str,
    *,
    on_error: Callable[[BaseException], None] | None = None,
) -> Iterator[None]:
    """Report a failed optional side effect and continue.

    ``on_error`` may retain a caller's more detailed diagnostic. Cancellation,
    process exit and keyboard interruption propagate to the lifecycle owner.
    Configuration, evaluation and authoritative publication must remain outside
    this boundary so their failures propagate normally.
    """
    try:
        yield
    except Exception as exc:  # noqa: BLE001 — the whole point: a never-abort swallow
        report_optional_failure(label, exc)
        if on_error is not None:
            on_error(exc)
