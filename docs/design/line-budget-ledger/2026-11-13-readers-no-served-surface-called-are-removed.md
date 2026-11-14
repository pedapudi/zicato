# Readers that no served surface called are removed

| Measurement | Delta |
|---|---:|
| Total | -5,137 |
| Production | -2,905 |
| Production logic | -1,897 |

The execution-plan tree and its live overlay had no route, and
`/api/live/pipeline` now returns the stepper verdict without building the
plan. The tournament-detail analytics, index readers and small query helpers
that only tests called are removed with the tests that existed for them; the
tests of what ingest writes read the index tables through one test helper.
