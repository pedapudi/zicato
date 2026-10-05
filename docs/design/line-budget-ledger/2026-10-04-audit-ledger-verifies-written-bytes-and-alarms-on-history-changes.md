# Audit ledger verifies written bytes and alarms on history changes

| Measurement | Delta |
|---|---:|
| Total | +926 |
| Production | +435 |
| Production logic | +300 |

The supervisor's audit ledger hashes and verifies the exact payload bytes it
writes, reloads its recorded decisions, contract hashes, findings and breaks on
restart, records an alarm when the orchestrator's records later state a
different value or none, verifies its chain every integrity tick, reports
records removed while it runs and a partial final line it removes, and
timestamps each integrity scan result. The integrity loop
restarts after a panicking scan. The production lines are in
`crates/supervisor/src/ledger.rs`, the integrity loop in `watchdog.rs`, and the
scan timestamps; the remaining lines are two integration test files and the
supervisor documentation.
