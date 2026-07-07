"""The convergence target's writing policy — the ONE mutation point.

``STYLE_RULES`` is a semicolon-separated token list the deterministic
harness (:mod:`zicato_examples.target_0_convergence.harness`) parses at
run time FROM ITS OWN generation snapshot. Each *defect token* left in
the list suppresses one feature of the synthesised output (failing that
feature's board predicate) and emits one ``drift_detected`` frame, so
the generation's scalar is a pure, hand-computable function of the
remaining tokens.

The seeded defects (and the predicate each one fails):

* ``verbose-prose``   — appends a long filler paragraph (fails
  ``is_concise``).
* ``omit-summary``    — suppresses the ``SUMMARY:`` line (fails
  ``has_summary``).
* ``skip-citations``  — suppresses the ``[source: ...]`` citation
  (fails ``has_citations``).

One further token the proposer's negative-control round can INTRODUCE:

* ``fabricate-metrics`` — appends an unverified metric claim (fails
  ``no_fabricated_metrics``). Not seeded; a patch that adds it must be
  rejected by the tournament gate.

Unknown tokens are counted as generic defects (one drift frame each)
but suppress nothing.
"""

from __future__ import annotations

# zicato:mutable id="style_rules" role="writing_policy"
STYLE_RULES = "verbose-prose; omit-summary; skip-citations"

__all__ = ["STYLE_RULES"]
