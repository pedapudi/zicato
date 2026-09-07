# Tests of the recommended improvement loop

The tests exercise the shared scoring defaults through
proposal generation, candidate screening, real tournament workers, confirmation,
and recording the final decision. They use deterministic responses and the writing
defects in the convergence example. They require no external service.

`tests/test_recommended_complete_loop.py` reads the recommendation from
`ScoringWeights()`: three proposals per candidate, screening on
two entries, a racing field of four candidates, two tournament replicates, and
up to 32 fresh confirmation draws at confidence threshold 0.8. The tests retain
calibration and the checks that precede execution. The two tasks retain the
example's summary and factuality predicates. Reserving tasks for separate
confirmation requires at least six entries; these cases do not exercise that
holdout behavior.

| Scenario | Controlled condition | Required result |
|---|---|---|
| All candidate changes apply | Each set of proposals contains one acceptable policy and two factuality regressions. Four accepted policies have distinct losses. | All 12 samples and eight screening exclusions are recorded. The best policy promotes after confirmation. Its accepted patch, committed source, mounted source, and source observed by each worker agree. |
| One candidate change fails to apply | The fourth candidate's final application fails after screening. | Three candidates compete. Confirmation still accounts for the four candidates planned before application began. Every planned candidate has a recorded status. |
| Evaluation cannot start | The same application fails; the real scheduler receives an exhausted token budget before tournament execution. | Attempts are recorded as unstarted, no child results become reusable, the decision is deferred, and the champion remains selected. Confirmation does not run because there is no measured improvement to confirm. |
| Execution is interrupted and resumed | A real tournament worker pauses after loading its source; its owning evolve task is cancelled. | Cancellation waits for worker termination. Recovery handles the unfinished candidates and runs a complete round in the same workspace, recording one final decision and selecting the correct champion. |

The fixtures use base seed 17 and at most two workers. Ordinary tournament
and confirmation draws must record distinct purpose, draw number, and seed.
The case where all candidate changes apply checks both configured tournament replicates
and rejects duplicate worker launches for reusable results. Source checks
reconstruct each accepted experiment from its parent and compare the result
with committed and measured bytes.

Ordinary racing matchups select candidates and remain in the audit. They do
not increase inferential sample size: successive board slices can reuse
measurements. Accepted confidence therefore counts only eligible confirmation
draws. The successful, partially applied, and resumed scenarios check that distinction.

Run the scenarios with an explicit file selection:

```sh
uv run pytest -n0 --durations=0 tests/test_recommended_complete_loop.py
```

Each case writes `recommended-acceptance-report.json` in its temporary workspace.
The report records the effective scoring configuration and its digest, the
evaluation contract hash, workspace setup and case execution wall time, actual
worker launches and measurement coordinates, and the recorded decision evidence.
The verification command's pytest cost report supplies fixture setup, call,
and teardown measurements. The workspace report does not measure teardown or
claim that process launch duration is worker execution time.

The tests verify that accepted, evaluated, and promoted source code agree (#495).
They also exercise sufficient evidence before promotion (#486, #487), valid
results before reuse (#488, #489), and worker termination before releasing
resources (#476, #477, #484). Changing shared defaults under #395 also requires
measurements of false promotions, detection of improvements, and cost.

Existing tests with independently established outcomes still cover convergence,
statistical decisions, and recorded reference results. Tests of permitted source
changes also cover serial proposals and other tournament arrangements. Remove
an expensive duplicate only after identifying which surviving assertions cover
its required behavior.
