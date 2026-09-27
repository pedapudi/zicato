# zicato-supervisor

Single-binary watchdog for the zicato runtime state files. `zicato
evolve` spawns it with `--workspace <path>` (and spawns nothing when
`evolve` itself runs with `--no-dashboard`); it can also be run
standalone against an existing workspace.

The binary does two things:

1. **Watchdog.** Polls `.zicato/runtime/heartbeat.json` and the per-run
   files under `.zicato/runtime/active_runs/`. If the orchestrator's
   heartbeat or any run's `last_progress` goes stale past the configured
   thresholds, or a run passes its wall-clock deadline, the supervisor
   sends SIGTERM, waits a grace period, and escalates to SIGKILL. It is
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
| `--heartbeat-stale-warn SECS`         | `30`        | Log a warning when the heartbeat is this old                   |
| `--heartbeat-stale-kill SECS`         | `90`        | Escalate to SIGTERM/SIGKILL when the heartbeat is this old     |
| `--run-stale-warn SECS`               | `30`        | Log a warning when a run's last_progress is this old           |
| `--run-stale-kill SECS`               | `120`       | Escalate when a run is this stalled                            |
| `--run-deadline-kill-disabled`        | off         | Do not kill runs that pass their wall-clock deadline           |
| `--run-kill-grace SECS`               | `5`         | SIGTERM-to-SIGKILL grace for a run past its deadline           |
| `--max-run-seconds SECS`              | `21600`     | Ceiling on any run's enforced window, from its `started_at`    |
| `--diff-containment`                  | off         | Audit each generation's source changes against its mutation spans (alarm only) |
| `--promotion-gate`                    | off         | Re-check each recorded promotion against its recorded scores (alarm only) |
| `--divergence-audit`                  | off         | Compare the SQLite index against the canonical files (report only) |
| `--divergence-stuck-age-seconds SECS` | `3600`      | Age past which the divergence audit reports a stuck generation |
| `--ledger-dir PATH`                   | unset       | Write a hash-chained `audit_ledger.jsonl` of watchdog actions here |
| `--log LEVEL`                         | `info`      | Log level (`RUST_LOG` overrides)                               |

## State file contract

The supervisor reads, but does not write, the runtime state files. Their
shapes are defined by the Python side; this crate uses
`#[serde(default)]` on every field so additions don't break the
supervisor at runtime. Schema reference lives in
[`docs/design/RUNTIME.md`](../../docs/design/RUNTIME.md).

Files consumed:

- `.zicato/runtime/heartbeat.json` — `{pid, instance_id, last_heartbeat, started_at, phase, epoch_id, generation_id, round, seq}`
- `.zicato/runtime/lock.json` — `{pid, instance_id, started_at, workspace}`
- `.zicato/runtime/active_runs/{run_id}.json` — `{run_id, pid, pgid, pid_start_time, producer_pid, producer_start_time, snapshot_path, entry_id, generation_id, epoch_id, started_at, last_progress, deadline, wall_clock_budget_seconds, events_jsonl_path, phase, reported_progress, message}`
- `.zicato/runtime/control/kill_requests/{run_id}` — kill requests from the Python parent
- `.zicato/current_epoch` — single-line epoch id marker
- `.zicato/lineage.json` — `{epochs: [{id, generations[]}]}`

The supervisor removes a kill-request marker once it has acted on it.
With `--ledger-dir` it also appends to its audit ledger, and with
`--diff-containment` it writes the audit's findings into the epoch's
health directory.

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
