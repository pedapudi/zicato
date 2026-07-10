"""Board reflection — Measurement System Analysis for the evaluation contract.

zicato's evolve loop treats ``board + scoring + judges + gate`` as a trusted
oracle. But that stack is an **instrument**, and every promotion is a
**measurement**; if the instrument is noisy, invalid, or insensitive the loop
optimizes against a broken signal. This package is the engine that validates
the instrument itself — the design of record is
:doc:`docs/design/BOARD-REFLECTION.md`.

One engine, three surfaces
--------------------------

The dedicated ``zicato reflect`` mode, the default-on evolve pre-flight
(:mod:`zicato.epoch.preflight`), and the continuous passive tier are the
**same analysis at three cadences and cost points**, so this package is one
engine behind three surfaces:

* :mod:`~zicato.reflection.plan` — the pre-registered run plan
  (``plan.json``): candidates, entries, replicate count, adjudicator, checks.
  Mirrors the mandatory-hypothesis discipline — ``--pre-register`` writes it
  and STOPS before spending budget, so the loss can never be p-hacked to
  whatever a run happened to show.
* :mod:`~zicato.reflection.corpus` — the observation corpus. A **passive**
  ingest that references (never copies) the lineage's already-persisted run
  artifacts with zero LLM budget, plus an **active** scheduler that mirrors
  the pre-flight's reserved-replicate discipline
  (``REFLECTION_REPLICATE_BASE = 5000 + j``) to produce fresh draws.
* :mod:`~zicato.reflection.analysis` — the **pure** pillar-1 (reliability) and
  pillar-2 (discrimination / power) analyzers over the corpus. No I/O; the
  noise floor is CONSUMED from the persisted epoch record, decision-flip is a
  seeded bootstrap through the pure gate decision, and judge self-consistency
  feeds the EXISTING :func:`zicato.health.diagnostics.detect_noisy_judge`
  unchanged (adjudication — pillar 3 — and calibration — pillar 4 — land in a
  later phase).

The operator-only output rule
-----------------------------

Everything this package produces — the corpus, the reliability/discrimination
analyses, and (later) the findings, scorecards, and adjudication rationales —
is **operator-facing only**. Nothing reflection produces is ever placed in the
proposer's prompt envelope: a proposer that could read the judge audit would
optimize against the judges' measured blind spots, which is the overfitting
program's threat model one level up. If a future decision ever crosses
reflection signal to the proposer it MUST reuse the banded / sanitized /
visibility-gated ``proposer/prompts.py`` machinery, never raw reflection output
(BOARD-REFLECTION.md §"the proposer envelope").

Running reflection never rolls the epoch — it is measurement, not evolution.
Only ACTING on a recommendation (a contract edit through the builder) does.
"""

from __future__ import annotations

from zicato.reflection.plan import ReflectionPlan, make_reflection_id, new_plan

__all__ = [
    "ReflectionPlan",
    "make_reflection_id",
    "new_plan",
]

# NOTE: the adjudicator / scorecards / findings modules (pillars 3-4) are NOT
# eagerly imported here — they pull goldfive lazily (via the reliability
# context glue) and the builder op layer (via findings' signature validation),
# so importing them at package-init would widen this package's import surface.
# Import them from their submodules (``zicato.reflection.adjudicator`` etc.).
