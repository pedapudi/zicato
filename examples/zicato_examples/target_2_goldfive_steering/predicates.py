"""Outcome predicates for target 2 (goldfive steering optimization).

Target 2 is unusual among zicato dogfood targets: the *inner harness* is
goldfive itself, and the mutable surface lives inside goldfive's source
tree (judge prompts, intervention-ladder threshold knobs, the refine
template). The agent under test is some other agent that goldfive
steers — for the synthetic-adversarial board entries it is a
deliberately-broken testkit agent (LoopingAgent, HallucinatingAgent,
etc.), and for the "normal" entries it is the tiny LlmAgent shipped in
this directory's ``agent_under_test.py``.

Three predicates live here:

* :func:`required_drift_fired` — outcome predicate for
  ``synthetic_adversarial`` entries. The hard check that the run-time
  required-drift assertion has already been wired through
  ``zicato.synthetic.expectations`` (added in parallel by R2-L); this
  Python-side predicate is a permissive placeholder so adversarial
  entries can additionally opt into custom pass/fail Python logic
  without re-implementing the required-drift check.

* :func:`no_warning_or_critical_drift` — outcome predicate for
  ``synthetic_clean`` negative-control entries. Passes whenever the
  clean-agent run completed normally (no abort). The "no
  warning/critical drift" assertion is enforced by the runtime layer
  using the same machinery that drives ``required_drift_kinds`` on the
  adversarial side; this predicate is the per-entry hook.

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
    """Permissive Python-side hook for ``synthetic_adversarial`` entries.

    The actual "did the required drift fire?" check is performed by the
    runtime layer (``zicato.synthetic.expectations``) against the run's
    goldfive event JSONL; the ``required_drift_kinds`` tuple on the
    :class:`zicato.core.BoardEntry` is the source of truth. This Python
    predicate runs ALONGSIDE that runtime check and is intended for
    entries that want to layer additional bespoke pass/fail logic on
    top — e.g. "required drifts fired AND the final output mentions
    the word 'cancelled'". The default behaviour is to pass; entries
    that don't need extra checks point their ``expectation.spec`` here
    and rely on the runtime layer for the real gate.
    """

    # The runtime layer handles the required_drift_kinds check; this
    # Python predicate is a no-op pass.
    del result
    return True


def no_warning_or_critical_drift(result: RunResult) -> bool:
    """Outcome predicate for ``synthetic_clean`` negative-control entries.

    A clean entry passes when the run completed normally — no abort. The
    "no WARNING or CRITICAL drift" assertion is enforced by the runtime
    layer using ``zicato.synthetic.expectations`` (the symmetric side of
    the ``required_drift_kinds`` check on adversarial entries); this
    Python predicate adds the additional gate that the run actually
    finished as opposed to being budget-killed mid-conversation.
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
