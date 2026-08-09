"""Readable golden mismatch reports for the pytest-driven parity gates.

The CONTRACT-HASH and CLI-HELP gates are plain scripts and print their own
unified diff on failure. The REINDEX-DUMP and MOCK-GOLDEN gates are pytest
tests comparing two multi-thousand-line strings, and pytest's own assertion
rewriting truncates that comparison to something unreadable under ``-q``
(the mode ``tools/parity.sh`` runs them in, and therefore the mode CI sees).

So those two gates build their own failure message: the reason, plus a
bounded unified diff of the normalized text. That way a red gate in a CI log
names the exact lines that moved instead of only the exit code.
"""

from __future__ import annotations

import difflib

#: How many unified-diff lines to include. Enough to localize a real
#: regression; bounded so a wholesale reordering cannot bury the log.
_MAX_DIFF_LINES = 120


def golden_mismatch_message(
    reason: str,
    expected: str,
    actual: str,
    *,
    golden_path: str,
    max_lines: int = _MAX_DIFF_LINES,
) -> str:
    """Return ``reason`` followed by a bounded unified diff, golden vs actual."""
    diff = list(
        difflib.unified_diff(
            expected.splitlines(),
            actual.splitlines(),
            fromfile=f"golden ({golden_path})",
            tofile="actual",
            lineterm="",
        )
    )
    shown = diff[:max_lines]
    body = "\n".join(shown)
    if len(diff) > max_lines:
        body += f"\n... {len(diff) - max_lines} more diff lines suppressed ..."
    return (
        f"{reason}\n\n"
        f"{body}\n\n"
        f"If the change is intentional, re-capture with: "
        f"bash tools/parity.sh --update\n"
    )


__all__ = ["golden_mismatch_message"]
