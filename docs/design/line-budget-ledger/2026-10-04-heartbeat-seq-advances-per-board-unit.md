# The heartbeat sequence advances inside long phases

| Measurement | Delta |
|---|---:|
| Total | +741 |
| Production | +425 |
| Production logic | +156 |

The evolve loop binds a transition recorder, so each scored board unit and
each settled proposal episode appends a progress transition. Every
transition goes through the one writer that also stamps the heartbeat, so the
heartbeat's sequence number equals the log's last one. The supervisor stops
warning during healthy tournaments, identifies the orchestrator by pid and
process start time, and reports an exited orchestrator's heartbeat once in
its log and as finished on `/statusz`. Readers take the progress log's last
complete event by reading the file from its end. The single-round entry point
binds the same recorder when its caller supplies a heartbeat, and the unused
apply and gate transitions are removed. The run-staleness defaults come from
one set of constants. The supervisor's in-file unit tests count as production
lines.
