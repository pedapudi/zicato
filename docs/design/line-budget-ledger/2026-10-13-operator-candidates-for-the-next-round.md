# Operator rounds for the evolve loop

| Measurement | Delta |
|---|---:|
| Total | +2,899 |
| Production | +1,612 |
| Production logic | +1,141 |

An operator, or an agent, can now queue a round of hand-written candidates
(issue #570). The production lines are the two commands that copy the
champion and queue edited copies, the queue record and its consumption
rules, the slots that parse queued candidates against the round's champion,
the provenance field and the source predicate on the experiment record, the
source and author columns of the index, and a store read that copies a
generation without git worktree admin. The commands reuse the proposal
episode's projection and validation, and the round reuses its persistence
and settlement path. The other lines are the tests, the regenerated CLI help,
index dump and reader-parity records, and the documentation.

As first proposed the change measured +2,942 total, +1,926 production and
+1,403 production logic. Review removed the command that listed queued
rounds with the control-log reader behind it, the write-back of rejections
into the queue item, the rebuild of an operator's tree from an older
champion (a stale item is now refused), and the standalone operator skill,
which is a section of `skills/zicato-step-loop`. The same review added the
index columns, the rule that no operator-authored text reaches the proposer
or its critic, and the tests for that rule, for holdout confirmation and its
query charge, and for the re-run after crash recovery.
