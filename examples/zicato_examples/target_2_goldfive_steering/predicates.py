"""Outcome predicates for target 2 (goldfive steering optimization).

Target 2 is unusual among zicato dogfood targets: the *system under test* is
goldfive itself, and the mutable surface lives inside goldfive's source
tree (judge prompts and the refine prompt). The agent under test is some other agent that goldfive
steers — for the synthetic-adversarial board entries it is a
deliberately-broken testkit agent (LoopingAgent, HallucinatingAgent,
etc.), and for the "normal" entries it is the tiny LlmAgent shipped in
this directory's ``agent_under_test.py``.

The tournament worker grades both synthetic kinds against the run's
event log before any predicate here matters. A ``synthetic_adversarial``
entry fails unless every kind in its ``required_drift_kinds`` fired at
warning or critical severity; a ``synthetic_clean`` entry fails if any
warning or critical drift fired. The worker conjoins that drift verdict
with the entry's ``expectation``, so a predicate named there is an
additional check: the entry passes only when both pass.

Three predicates live here:

* :func:`required_drift_fired` — the expectation of the
  ``synthetic_adversarial`` entries. It always passes, so those entries
  are graded by their drift requirement alone. Replace it to add a
  bespoke check on top of that requirement.

* :func:`no_warning_or_critical_drift` — the expectation of the
  ``synthetic_clean`` negative-control entries. It adds the check that
  the run finished without aborting; the worker's drift rule supplies
  the "no warning or critical drift" check its name describes.

* :func:`output_mentions_target_token` — a generic correctness check
  for the "normal" board entries: did the agent's final output mention
  the word "summary"? Operators add more predicates here as new normal
  entries land. We keep these simple because the goal of target 2 is to
  pressure goldfive's steering layer, not to test a complicated
  workload.
"""

from __future__ import annotations

# zicato:grading — operator-owned pass/fail contract; never a proposer mutation point.
from zicato.core import RunResult


def required_drift_fired(result: RunResult) -> bool:
    """Always pass; the entry's drift requirement is the check.

    The tournament worker applies
    ``zicato.synthetic.expectations.evaluate_required_drift`` to the run's
    event log with the entry's ``required_drift_kinds`` and conjoins the
    result with this predicate. An entry that needs a further condition,
    such as "the final output mentions the word 'cancelled'", names a
    predicate that tests it; the drift requirement still applies.
    """

    # The worker checks required_drift_kinds; this predicate adds nothing.
    del result
    return True


def no_warning_or_critical_drift(result: RunResult) -> bool:
    """Pass when the run finished without aborting.

    The tournament worker applies
    ``zicato.synthetic.expectations.evaluate_no_drift`` to the run's event
    log and conjoins the result with this predicate, so a clean entry
    passes only when no warning or critical drift fired and the run was
    not stopped early, for example by its wall-clock budget.
    """

    return not result.aborted


def output_mentions_target_token(result: RunResult) -> bool:
    """Generic correctness predicate for the "normal" board entries.

    Passes when the agent's final output mentions the word "summary"
    (case-insensitive). The normal entries in this directory's
    ``board.jsonl`` are constructed so that a competent agent will
    produce a summary; failure to do so indicates the steerer either
    over-interrupted the run (degrading task quality) or did not catch
    a drift that derailed it. Either way, the predicate is sensitive to
    "steering wrecked the workload" without us having to engineer a
    sophisticated workload.

    Operators adding more normal entries should write more predicates
    here (one per logical correctness check) rather than overloading
    this one — keeping each predicate single-purpose makes regression
    analysis tractable.
    """

    return "summary" in result.final_output.lower()
