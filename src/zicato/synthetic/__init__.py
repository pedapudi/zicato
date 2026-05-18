"""Synthetic adversarial and clean board-entry support for zicato.

Zicato's dogfood-plan target 2 (steering goldfive itself) cannot rely on
drift-count as a loss signal — the steerer's own job is to produce
drift events, so counting them is circular. Instead, target 2 evaluates
the steerer against **synthetic** board entries:

* ``synthetic_adversarial`` — wraps a known-bad agent (a LoopingAgent
  that never terminates, a HallucinatingAgent that fabricates tool
  output, a RefusingAgent that declines benign requests, etc.) under
  ``goldfive.wrap``. The expectation is that goldfive **does** detect
  the drift the adversarial agent is designed to provoke. Pass = drift
  fires; fail = drift missed.

* ``synthetic_clean`` — wraps a deliberately-cooperative reference
  agent. The expectation is the inverse: NO drift fires (modulo INFO-
  severity observational drift, which the steerer emits even on healthy
  runs as a side effect of its own observation passes).

The two entry kinds share runner shape — resolve an agent class via a
dotted path, instantiate it, drive it under ``goldfive.wrap`` with the
entry's user input, persist events to JSONL, and return a
:class:`zicato.core.types.RunResult`. The expectation matchers
(:func:`evaluate_required_drift` and :func:`evaluate_no_drift`) read
the JSONL back independently of the runner and decide pass/fail.

The known-bad agent zoo (``LoopingAgent``, ``HallucinatingAgent``,
``CleanAgent``, etc.) lives upstream in
``goldfive.testkit.adversarial`` and is shipped from the goldfive repo.
That testkit is being implemented in parallel; this module is
**defensive** about its absence. Importing this package does not
require goldfive.testkit to be present — only running an adversarial
or clean entry does, and the failure mode there is a clear
:class:`AdversarialResolutionError` rather than an obscure
``ImportError`` deep inside ``goldfive.wrap``.

Public surface:

* :func:`resolve_adversarial_agent` — dotted-path resolver with a clear
  error type
* :class:`AdversarialResolutionError` — the error raised on bad specs
* :func:`run_adversarial_entry` — runner for ``synthetic_adversarial``
  entries
* :func:`run_clean_entry` — runner for ``synthetic_clean`` entries
* :func:`evaluate_required_drift` — expectation matcher for
  ``synthetic_adversarial`` entries
* :func:`evaluate_no_drift` — expectation matcher for
  ``synthetic_clean`` entries
"""

from __future__ import annotations

from zicato.synthetic.adversarial import (
    AdversarialResolutionError,
    resolve_adversarial_agent,
    run_adversarial_entry,
)
from zicato.synthetic.clean import run_clean_entry
from zicato.synthetic.expectations import (
    evaluate_no_drift,
    evaluate_required_drift,
)

__all__ = [
    "AdversarialResolutionError",
    "resolve_adversarial_agent",
    "run_adversarial_entry",
    "run_clean_entry",
    "evaluate_no_drift",
    "evaluate_required_drift",
]
