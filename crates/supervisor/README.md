# zicato-supervisor

Single-binary watchdog for the zicato runtime state files. `zicato
evolve` spawns it with `--workspace <path>` (and spawns nothing when
`evolve` itself runs with `--no-dashboard`); it can also be run
standalone against an existing workspace.

The binary does two things:

1. **Watchdog.** Polls `.zicato/runtime/heartbeat.json` and the per-run
   files under `.zicato/runtime/active_runs/`. Staleness is the time since
   the heartbeat's progress counter (`seq`) last advanced; the evolve loop
   advances it at each loop transition and each scored board unit. A stale
   orchestrator heartbeat produces a warning, and a deeply stale one a
   louder warning; the supervisor never signals the orchestrator, so
   restarting it is a decision for the operator or an external process
   supervisor. Once the orchestrator has exited (no live process has the
   heartbeat's `pid` and `pid_start_time`), the supervisor logs once that
   the heartbeat is final and stops classifying it. If a run's
   `last_progress` goes stale past the configured threshold, or the run
   passes its wall-clock deadline, the supervisor sends SIGTERM to the
   run's worker, waits a grace period, and escalates to SIGKILL. It is
   also the one escalator for the kill requests the Python parent writes
   under `.zicato/runtime/control/kill_requests/`.
2. **`/statusz`.** A terse, self-contained operational page (and
   `/statusz.json`) reporting the watchdog's own state, together with
   `/api/audit/verify`.

The dashboard UI is served by the standalone Python dashboard service
(`zicato.dashboard`), which `zicato evolve` spawns separately.

The watchdog path never invokes an LLM and never reads state from
memory — every decision is a pure function of the on-disk files. The
process can be killed and restarted at any time without losing state.

## Build

This crate is a member of the repo-root Cargo workspace; build it
from the repository root:

```
cargo build --release -p zicato-supervisor
```

Produces a single static-ish binary at
`target/release/zicato-supervisor` (the workspace shares one `target/`
directory at the repo root). The release profile uses thin LTO and
`strip = true`, giving roughly 7 MB on x86_64-linux.

## Run

```
./target/release/zicato-supervisor --workspace /path/to/.zicato
```

The server prints its listening URL to stdout
(`zicato-supervisor listening on http://127.0.0.1:7920`). Its port range
(7920–7930) is disjoint from the Python dashboard's (7892–7902), so the
two never contend when `evolve` starts both.

### CLI flags

`zicato-supervisor --help` is the authoritative list.

| Flag                                  | Default     | Meaning                                                        |
| ------------------------------------- | ----------- | -------------------------------------------------------------- |
| `--workspace PATH`                    | `.zicato`   | Workspace root (contains `runtime/`, `epochs/`)                |
| `--port N`                            | `7920`      | Preferred port; tries `N..=N+10` if busy                       |
| `--bind ADDR`                         | `127.0.0.1` | Bind address                                                   |
| `--interval SECS`                     | `2`         | Watchdog poll interval                                         |
| `--heartbeat-stale-warn SECS`         | `30`        | Log a warning when the heartbeat's `seq` has not advanced for this long |
| `--heartbeat-stale-kill SECS`         | `90`        | Log a deep-stale warning when `seq` has not advanced for this long; the orchestrator is never signalled |
| `--run-stale-warn SECS`               | `30`        | Log a warning when a run's last_progress is this old           |
| `--run-stale-kill SECS`               | `120`       | Escalate when a run without a wall-clock budget is this stalled; a run with a budget escalates at twice its budget |
| `--run-deadline-kill-disabled`        | off         | Do not kill runs that pass their wall-clock deadline           |
| `--run-kill-grace SECS`               | `5`         | SIGTERM-to-SIGKILL grace for a run past its deadline           |
| `--max-run-seconds SECS`              | `21600`     | Ceiling on any run's enforced window, from its `started_at`    |
| `--mutation-containment`              | off         | Check each generation's source change against its byte-range mutation evidence (alarm only) |
| `--promotion-gate`                    | off         | Re-check each recorded promotion against its recorded scores (alarm only) |
| `--divergence-audit`                  | off         | Compare the SQLite index against the canonical files (report only) |
| `--divergence-stuck-age-seconds SECS` | `3600`      | Age past which the divergence audit reports a stuck generation |
| `--ledger-dir PATH`                   | unset       | Write a hash-chained `audit_ledger.jsonl` of actions, decisions and findings here |
| `--log LEVEL`                         | `info`      | Log level (`RUST_LOG` overrides)                               |

## State file contract

The supervisor reads, but does not write, the runtime state files. Their
shapes are defined by the Python side; this crate uses
`#[serde(default)]` on every field so additions don't break the
supervisor at runtime. Schema reference lives in
[`docs/design/RUNTIME.md`](../../docs/design/RUNTIME.md).

Files consumed:

- `.zicato/runtime/heartbeat.json` — `{pid, pid_start_time, instance_id, last_heartbeat, started_at, phase, epoch_id, generation_id, round, seq}`
- `.zicato/runtime/lock.json` — `{pid, instance_id, started_at, workspace}`
- `.zicato/runtime/active_runs/{run_id}.json` — `{run_id, pid, pgid, pid_start_time, producer_pid, producer_start_time, snapshot_path, entry_id, generation_id, epoch_id, started_at, last_progress, deadline, wall_clock_budget_seconds, events_jsonl_path, phase, reported_progress, message}`
- `.zicato/runtime/control/kill_requests/{run_id}` — kill requests from the Python parent
- `.zicato/current_epoch` — single-line epoch id marker
- `.zicato/lineage.json` — `{epochs: [{id, generations[]}]}`

The supervisor removes a kill-request marker once it has acted on it.
With `--ledger-dir` it also appends to its audit ledger, and with
`--mutation-containment` it writes each parent-to-child pair's result to
`epochs/{epoch}/health/mutation_containment_{generation}.json`.

The containment audit reads each pair's source trees from the configured
generation store and the child's `containment.json` evidence. A pair is
`contained`, `violated` (the records bind the files and show a change outside
the mutation units, or the child tree holds a `.pyc`, `.pyo` or `.pyd` file),
`evidence_mismatch` (a record contradicts the files, for example a source
file edited after its evidence was written), or `unverified` (evidence
missing, malformed or unreadable). A decided pair that verified earlier and
is now unverified is marked `evidence_withdrawn`. A pair that moves into
`violated`, `evidence_mismatch` or withdrawn evidence is logged as a
`MUTATION-CONTAINMENT ALERT` and, with a ledger, recorded as a
`diff_containment_alert`. A changed frozen `brief.md` or `scoring.json`
affects every pair in the epoch, so it is recorded once as
`epoch_evidence_changed` naming the input. When a parent's tree changed or
became unreadable after its children's policies were captured, the children
carry `introduced_by` naming the parent and only the parent alarms. A pair with a
recorded decision is re-checked only when the metadata of a file it reads
changes. [`docs/dev-guide/08-supervisor.md`](../../docs/dev-guide/08-supervisor.md)
§8.8.1 has the full rule.

## Audit ledger

With `--ledger-dir`, the supervisor appends one hash-chained JSON record per
line to `audit_ledger.jsonl`: watchdog escalations, integrity findings, and
each generation's decision and each epoch's contract hash the first time it
sees them. Each digest covers the exact payload bytes on the line. On
restart, the supervisor loads what the ledger holds, so it does not record a
decision or contract hash twice. When the orchestrator's files later state a
different value for one of them, it logs a warning and appends a
`history_changed` record.

On restart it also loads the findings it already recorded and does not record a
standing finding again. A recorded decision or contract hash that disappears
from the orchestrator's files for two consecutive ticks produces a
`history_changed` record with observed value `absent`.

Every integrity tick verifies the chain; each break is logged and recorded once
as a `ledger_integrity` record, also across restarts. Opening a ledger that ends in a
partial line removes that line and records its bytes in a `ledger_integrity`
record. The chain detects an edited or reordered record and a record removed
from the middle. It does not detect records removed from the end of the file,
or the file being deleted. A running supervisor knows how many records the
ledger held and reports a shorter or missing file, but only for removals
while it runs.

## HTTP API

Every route is a read-only `GET`:

- `GET /statusz`, `GET /statusz.json` — the watchdog's own state
- `GET /api/audit/verify` — the audit ledger's chain integrity

## Tests

```
cargo test -p zicato-supervisor     # or: make supervisor-test
```

Runs unit tests inline in each module plus the integration tests in
`tests/integration_test.rs`. `make supervisor-check` adds formatting and
clippy. The integration suite spins up the server
against a temporary workspace, exercises every endpoint, runs the
integrity audits end to end, and confirms signal escalation against a
real child process that ignores SIGTERM.

## Troubleshooting

- **Port already in use.** The supervisor automatically tries
  `--port..=--port+10` before giving up.
- **Heartbeat warnings on startup.** Expected: if the Python side
  hasn't written `heartbeat.json` yet there is nothing to honor. The
  log message is `no heartbeat file present` at debug level.
- **`Address already in use` even after a crash.** Linux holds the
  socket in TIME_WAIT for ~60s. Either wait or pick another port.
