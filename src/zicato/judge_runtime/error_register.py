"""Per-judge call-failure register — the third judge outcome, made durable.

A board-declared process judge has two honest answers: "the criterion was
violated" (drift) and "the criterion was not violated" (silence). It has a
third *outcome* — its callable RAISED — and nothing on the run's own wire
distinguishes that from silence.

The laundering is structural rather than accidental. A judge must never crash
a run, so both :class:`~zicato.judge_runtime.builder._InlineCriterionJudge`
and goldfive's ``DefaultSteerer.evaluate_judges`` catch everything a judge
throws. goldfive then emits no ``JudgementEmitted`` for an empty verdict, and
the reducer writes no ``custom:<judge_name>`` drift count.
:func:`zicato.health.diagnostics.detect_dead_judge` infers "fired" from those
drift counts, so without this register it would report a broken judge in the
same words it uses for a healthy judge whose criterion was never met. A
misconfigured judge endpoint would read as a board-design problem, and its
zero drift would make the generation's scalar *better* than the truth.

This module is the counter that survives the catch. It is modelled on
:data:`zicato.models_config._DEFERRED_ROLE_FAILURES`, the register that
solved the same shape of problem one layer down (a deferred role resolution
that fails INSIDE a swallowing judge path): a process-wide dict, written at
the boundary where the exception is caught, read once at the end by the
worker that owns the process.

Scope and lifetime
------------------

The register is PROCESS-WIDE and one worker process evaluates exactly one
board unit (:func:`zicato._tournament_worker.main`), so a snapshot taken at
``loss.json``-write time describes exactly that run. Anything else sharing a
process (a test, an in-process reflection replay) sees a cumulative count;
:func:`clear_judge_errors` resets it.

Observability only: nothing here changes a verdict, a score, or an exit
code. The counters ride out to :attr:`~zicato.core.loss.LossProfile.judge_errors`
and become a loop-health finding.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing-only import
    from zicato.core.loss import JudgeError

#: ``judge_name`` ⇒ ``[invocations, errors, last_error_type]``. Written by the
#: judge boundary (:mod:`zicato.judge_runtime.builder`), read by the worker.
_JUDGE_CALLS: dict[str, list[int | str]] = {}

#: Guards :data:`_JUDGE_CALLS`. Judges are evaluated from one event loop in
#: practice, but a sink / adapter is free to drive them from a worker thread,
#: and a lost increment would understate exactly the failure this register
#: exists to make visible.
_LOCK = threading.Lock()


def record_judge_invocation(judge_name: str) -> None:
    """Count one call of ``judge_name``'s callable.

    Called at the point the judge is about to invoke the thing that can
    fail — the auxiliary LLM for an inline judge, the operator's code for a
    python judge. An observation point with nothing to judge (an empty
    reasoning trace) is NOT an invocation: counting it would put a
    never-called judge at ``0/N errors`` and read as healthy.
    """
    name = str(judge_name or "")
    with _LOCK:
        row = _JUDGE_CALLS.setdefault(name, [0, 0, ""])
        row[0] = int(row[0]) + 1


def record_judge_error(judge_name: str, exc: BaseException) -> None:
    """Count one call of ``judge_name``'s callable that raised.

    Records the exception's TYPE name only. The verbatim message can carry
    an endpoint URL / request id and would land in a scored, indexed
    artifact; the full text stays in the WARNING log and in the reflection
    sidecar's error entry.
    """
    name = str(judge_name or "")
    with _LOCK:
        row = _JUDGE_CALLS.setdefault(name, [0, 0, ""])
        row[1] = int(row[1]) + 1
        row[2] = type(exc).__name__


def judge_error_snapshot() -> tuple[JudgeError, ...]:
    """Judges INVOKED in this process that raised at least once.

    Returns one :class:`~zicato.core.loss.JudgeError` per judge with a
    non-zero error count, in first-invocation order. Judges that never
    raised are omitted: their provenance is the existing drift-count
    evidence, and an entry per healthy judge would grow every ``loss.json``
    on every board for a signal that is always zero.
    """
    from zicato.core.loss import JudgeError  # noqa: PLC0415 — keeps the boundary light

    with _LOCK:
        rows = [(name, int(row[0]), int(row[1]), str(row[2])) for name, row in _JUDGE_CALLS.items()]
    return tuple(
        JudgeError(
            judge_name=name,
            invocations=invocations,
            errors=errors,
            last_error_type=last_error_type,
        )
        for name, invocations, errors, last_error_type in rows
        if errors
    )


def clear_judge_errors() -> None:
    """Reset the register. For tests; a worker process runs one board unit."""
    with _LOCK:
        _JUDGE_CALLS.clear()


__all__ = [
    "clear_judge_errors",
    "judge_error_snapshot",
    "record_judge_error",
    "record_judge_invocation",
]
