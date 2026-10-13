# Operator rounds for the evolve loop

| Measurement | Delta |
|---|---:|
| Total | +2,332 |
| Production | +1,539 |
| Production logic | +1,115 |

An operator, or an agent, can now queue a round of hand-written candidates
(issue #570). The production lines are the two commands that copy the
champion and queue edited copies, the queue record and its consumption
rules, the slots that mint queued candidates against the round's champion,
the provenance field on the experiment record, and a store read that copies
a generation without git worktree admin. The commands reuse the proposal
episode's projection and validation, and the round reuses its persistence
and settlement path. The other lines are the tests, the regenerated CLI help
and reader-parity records, and the documentation.
