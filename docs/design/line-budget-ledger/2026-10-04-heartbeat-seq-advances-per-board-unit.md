# The heartbeat sequence advances per scored board unit

| Measurement | Delta |
|---|---:|
| Total | +306 |
| Production | +225 |
| Production logic | +86 |

The evolve loop binds a recorder that appends a `UnitSettled` progress
transition and stamps its sequence number on the heartbeat as each board unit
is scored, so the supervisor stops warning during healthy tournaments. The
supervisor logs an exited orchestrator's final heartbeat once instead of a
stale warning per tick, and its run-staleness defaults come from one set of
constants. The supervisor's in-file unit tests count as production lines.
