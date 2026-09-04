"""Target 0 — the known-answer convergence harness (no LLM anywhere).

A fully deterministic dogfood target that proves the WHOLE evolve loop
end-to-end: real propose → apply → validate → subprocess tournament
workers → gate → persist, with a planted-defect agent whose optimal
scalar is computable by hand. The target is scripted at every seam:

* ``agent/policy.py`` — the mutable surface: a single ``style_rules``
  string seeded with defect tokens.
* ``harness.py`` — :class:`DeterministicPolicyAdapter`, a session that
  reads the policy from its own generation snapshot and synthesises
  output + goldfive drift frames purely from the remaining tokens.
* ``predicates.py`` — one defensive pass/fail predicate per board entry.
* ``mocks.py`` — the scripted proposer (``aux_llm``) whose per-round
  patches drive the loop to a known floor, plus the never-invoked
  ``target_llm`` placeholder.
* ``board.jsonl`` / ``scoring.json`` / ``scoring.effective.json`` — the
  frozen contract, with the ``runtime:`` coefficient at 0 so the floor is an exact
  float.

See ``RUN.md`` for the no-endpoint demo recipe and
``tests/test_convergence_known_answer.py`` for the CI-runnable proof.
"""

from __future__ import annotations

__all__: list[str] = []
