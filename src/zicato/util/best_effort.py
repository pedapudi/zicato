"""A single context manager for the loop's never-abort swallow blocks.

The evolve loop is studded with *best-effort* side effects: re-stamping a
report, publishing a live status, dual-writing the index, closing a sink,
tearing down a server. None of them may abort the run — a failure there is
strictly less important than the optimization the loop exists to do. The
codebase expressed that with a hand-rolled idiom repeated dozens of times::

    try:
        do_the_side_effect()
    except Exception as exc:  # noqa: BLE001 — ... is best-effort
        log.debug("... skipped: %s", exc)

:func:`best_effort` collapses that idiom to one place. The swallow is
*identical* — the same broad ``Exception`` is caught and the same control
flow (fall through, never re-raise) results — so wrapping a block in it is
behavior-preserving. What it adds is **observability**: every swallowed
failure increments a per-``label`` counter (:func:`best_effort_failures`),
which makes an otherwise invisible class of degradation a queryable signal
the loop-health surface can report on.

Preserving the *exact* log line
-------------------------------

Each historical swallow logged a bespoke message (different text, different
format args, sometimes ``log.warning``/``log.exception`` rather than
``log.debug``). To keep that output byte-identical, a call site passes an
``on_error`` callback that reproduces its original logging call verbatim::

    with best_effort("post-close report re-stamp", on_error=_skip):
        restamp_persisted_report(workspace_root, cur)

When no ``on_error`` is given, a uniform ``"%s skipped: %s"`` debug line is
emitted on the package logger instead, which suits a call site with no
particular message to preserve.

The counter is process-local and additive: nothing here is persisted, so it
cannot move any frozen artifact. Tools that want a clean window call
:func:`reset_best_effort_failures` first.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager

__all__ = [
    "best_effort",
    "best_effort_failures",
    "reset_best_effort_failures",
]

log = logging.getLogger("zicato.util.best_effort")

# Process-local tally of swallowed best-effort failures, keyed by the label
# the call site passed. Surfaced read-only via :func:`best_effort_failures`;
# never persisted, so it cannot perturb any golden / frozen artifact.
_FAILURES: Counter[str] = Counter()


@contextmanager
def best_effort(
    label: str,
    *,
    on_error: Callable[[BaseException], None] | None = None,
) -> Iterator[None]:
    """Run a never-abort side effect, swallowing and tallying any failure.

    Wraps a block whose failure must not abort the caller. On any
    :class:`Exception` the block raises, the exception is swallowed (exactly
    as the hand-rolled ``except Exception: # noqa: BLE001`` it replaces), the
    per-``label`` failure counter is incremented, and control falls through.

    Parameters
    ----------
    label:
        Short, stable identifier for this side effect. Used as the key in the
        failure tally and in the default log line. Reuse the same label for
        the same logical side effect so counts aggregate.
    on_error:
        Optional callback invoked with the swallowed exception *before*
        control returns. A call site that must keep an existing log line
        passes a callback reproducing that ``log`` call verbatim, so the
        emitted message is unchanged. When omitted, a uniform
        ``"%s skipped: %s"`` debug line is logged on this module's logger.

    Notes
    -----
    Only :class:`Exception` is caught — :class:`BaseException` subclasses such
    as :class:`KeyboardInterrupt`, :class:`SystemExit`, and
    :class:`asyncio.CancelledError` propagate, matching the original
    ``except Exception`` blocks exactly.
    """
    try:
        yield
    except Exception as exc:  # noqa: BLE001 — the whole point: a never-abort swallow
        _FAILURES[label] += 1
        if on_error is not None:
            on_error(exc)
        else:
            log.debug("%s skipped: %s", label, exc)


def best_effort_failures() -> dict[str, int]:
    """Return a snapshot ``{label: count}`` of swallowed best-effort failures.

    A read-only copy of the process-local tally. ``label`` keys with a zero
    count are never present. The loop-health surface reads this to report how
    many never-abort side effects degraded during a run.
    """
    return dict(_FAILURES)


def reset_best_effort_failures() -> None:
    """Clear the process-local best-effort failure tally.

    Lets a caller (or a test) establish a clean window before measuring the
    failures accrued over a bounded span of work.
    """
    _FAILURES.clear()
