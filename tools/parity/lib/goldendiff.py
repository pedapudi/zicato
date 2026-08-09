"""Readable golden mismatch reports for the pytest-driven parity gates.

CONTRACT-HASH and CLI-HELP are plain scripts that print their own diff.
REINDEX-DUMP and MOCK-GOLDEN are pytest tests comparing multi-thousand-line
strings, and pytest's assertion rewriting truncates that to something
unreadable under the ``-q`` that ``tools/parity.sh`` runs them in — so they
build their own message instead.
"""

from __future__ import annotations

import difflib

#: Bounded so a wholesale reordering cannot bury the rest of the log.
_MAX_DIFF_LINES = 120


def golden_mismatch_message(reason: str, expected: str, actual: str, *, golden_path: str) -> str:
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
    body = "\n".join(diff[:_MAX_DIFF_LINES])
    if len(diff) > _MAX_DIFF_LINES:
        body += f"\n... {len(diff) - _MAX_DIFF_LINES} more diff lines suppressed ..."
    return f"{reason}\n\n{body}\n\nIf intentional, re-capture: bash tools/parity.sh --update\n"
