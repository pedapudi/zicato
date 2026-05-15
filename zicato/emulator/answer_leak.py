"""Post-hoc answer-leakage heuristic.

The emulator's system prompt forbids producing structured answer-shape
content. This module is the second line of defense — a small regex-based
checker that scans every emulator turn before it is forwarded to the
inner harness. If any pattern fires, the driver aborts the run with
``abort_reason='emulator_leak_detected'``.

The patterns intentionally err toward false positives. False positives
fail a run; false negatives let collusion-shaped content through. The
asymmetry is deliberate.
"""

from __future__ import annotations

import re

#: Patterns indicating the emulator output looks like an answer key.
#:
#: Compiled with ``re.IGNORECASE | re.MULTILINE`` in :func:`check_answer_leak`
#: — patterns that anchor with ``^`` apply per-line, not per-string.
LEAK_PATTERNS: tuple[str, ...] = (
    r"```",              # code fence
    r"^\s*\{",           # raw JSON object at line start
    r"^\s*\[",           # raw JSON array at line start
    r"the answer is",
    r"you should output",
    r"correct output is",
    r"expected output",
    r"the schema is",
    r"```json",
)


_COMPILED: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (pat, re.compile(pat, re.IGNORECASE | re.MULTILINE)) for pat in LEAK_PATTERNS
)


def check_answer_leak(text: str) -> str | None:
    """Scan ``text`` for answer-shape leakage.

    Parameters
    ----------
    text:
        The emulator's proposed next user turn.

    Returns
    -------
    str | None
        A human-readable error message naming the offending pattern when
        leakage is suspected, or ``None`` if the text is clean. The
        message is intentionally short — the driver pastes it directly
        into the run's ``abort_reason``-adjacent diagnostic.

    Notes
    -----
    Matching is case-insensitive. Patterns that anchor with ``^`` apply
    per-line (``re.MULTILINE``).
    """
    for pat, compiled in _COMPILED:
        match = compiled.search(text)
        if match is not None:
            return (
                f"answer-leak heuristic fired: pattern {pat!r} matched "
                f"at offset {match.start()}"
            )
    return None


__all__ = ["LEAK_PATTERNS", "check_answer_leak"]
