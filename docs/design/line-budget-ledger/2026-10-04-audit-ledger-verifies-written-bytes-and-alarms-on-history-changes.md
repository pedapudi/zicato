# Audit ledger verifies written bytes and alarms on history changes

| Measurement | Delta |
|---|---:|
| Total | +608 |
| Production | +274 |
| Production logic | +202 |

The supervisor's audit ledger hashes and verifies the exact payload bytes it
writes, reloads its recorded decisions and contract hashes on restart, records
an alarm when the orchestrator's records later state a different value,
verifies its chain every integrity tick, reports a partial final line it
removes, and timestamps each integrity scan result. The integrity loop
restarts after a panicking scan. The production lines are in
`crates/supervisor/src/ledger.rs`, the integrity loop in `watchdog.rs`, and the
scan timestamps; the remaining lines are two integration test files and the
supervisor documentation.
