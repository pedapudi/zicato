"""The system under test: a note writer, described by one style policy.

This is the tree the loop rewrites. ``STYLE_RULES`` is a semicolon-
separated list of style tokens, and the adapter in ``example_wiring``
reads it from whichever generation snapshot is being evaluated, so a
generation's board score is a function of this one string.

Four rules are seeded. ``plain-language`` is one the writer is meant to
follow and the adapter ignores. The other three each suppress a feature
of the note, and each one fails a single board entry:

* ``verbose-prose`` appends a filler paragraph, pushing the note past the
  conciseness budget.
* ``omit-summary`` drops the ``SUMMARY:`` line.
* ``skip-citations`` drops the ``[source: ...]`` citation.

Removing one of those three restores the feature it suppressed and the
score improves. That is the whole example: a defect a proposer can find
in the source, an edit that provably fixes it, and a board that measures
the difference. A converged policy is ``plain-language`` alone.

Replacing this file with your own system under test is the first edit to
make. What has to stay is the ``# zicato:mutable`` marker: it is the only
thing that tells the loop which span it may rewrite, and a tree with no
marker has nothing for a proposer to change.

The writer runs on no model at all. A system under test that does call
one receives it as ``config.target_call_llm`` in ``adapter.py``.
"""

from __future__ import annotations

# zicato:mutable id="style_rules" role="writing_policy"
STYLE_RULES = "plain-language; verbose-prose; omit-summary; skip-citations"

__all__ = ["STYLE_RULES"]
