"""How a run of the note writer is graded.

Each board entry names one of these by dotted path in its ``expectation``
block. A predicate takes the run's :class:`~zicato.core.RunResult` and
returns a bool, and it must never raise: a run can abort part way and
hand a predicate an empty output, and a grader that raises turns a
measurable failure into an unmeasurable one.

Each predicate here checks one feature the style policy either produces
or suppresses, so every token the proposer removes moves one entry from
fail to pass and the board can tell a partial fix from a complete one. A
board whose entries all pass, or all fail, measures nothing — it cannot
rank two candidates.

These are the operator's contract. The loop never rewrites them.
"""

from __future__ import annotations

from typing import Any

#: The conciseness budget, in characters. The note with every feature
#: present and no filler stays well under it; the filler alone exceeds it.
CONCISE_MAX_CHARS = 400


def _output(result: Any) -> str:
    """The run's final output, lowercased, or empty when there is none."""
    return str(getattr(result, "final_output", "") or "").lower()


def has_note(result: Any) -> bool:
    """The writer produced a note at all.

    Passes for every generation, which is what makes it useful: it fails
    only when the system under test did not run, separating a broken
    adapter from a badly-scoring policy.
    """
    return "note:" in _output(result)


def has_summary(result: Any) -> bool:
    """The note carries its ``SUMMARY:`` line.

    Fails while ``omit-summary`` remains in the policy.
    """
    return "summary:" in _output(result)


def has_citation(result: Any) -> bool:
    """The note attributes its claim to a source.

    Fails while ``skip-citations`` remains in the policy.
    """
    return "[source:" in _output(result)


def is_concise(result: Any) -> bool:
    """The note stays under the character budget.

    Fails while ``verbose-prose`` remains in the policy. An empty output
    is not concise — it is a run that produced nothing, and a predicate
    that rewarded it would rank a broken generation above a working one.
    """
    output = _output(result)
    return 0 < len(output) <= CONCISE_MAX_CHARS


__all__ = [
    "CONCISE_MAX_CHARS",
    "has_citation",
    "has_note",
    "has_summary",
    "is_concise",
]
