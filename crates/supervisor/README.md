# zicato-supervisor

Single-binary watchdog for the zicato runtime state files. `zicato
evolve` spawns it with `--workspace <path> --no-dashboard` (and spawns
nothing when `evolve` itself runs with `--no-dashboard`); it can also be
run standalone against an existing workspace.

Under `--no-dashboard` the binary does two things:

1. **Watchdog.** Polls `.zicato/runtime/heartbeat.json` and the per-run
   files under `.zicato/runtime/active_runs/`. If the orchestrator's
   heartbeat or any run's `last_progress` goes stale past the configured
   thresholds, or a run passes its wall-clock deadline, the supervisor
   sends SIGTERM, waits a grace period, and escalates to SIGKILL. It is
   also the one escalator for the kill requests the Python parent writes
   under `.zicato/runtime/control/kill_requests/`.
2. **`/statusz`.** A terse, self-contained operational page (and
   `/statusz.json`) reporting the watchdog's own state. Always served,
   together with `/api/audit/verify`.

The dashboard UI is served by the standalone Python dashboard service
(`zicato.dashboard`), which `zicato evolve` spawns separately. The binary
also carries dashboard, analytical-API, event-stream and control routes;
they are mounted only when it runs without `--no-dashboard`.

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
| `--read-only`                         | off         | Reject all `POST /api/control/*` with 403                      |
| `--no-dashboard`                      | off         | Watchdog only: mount `/statusz`, `/statusz.json` and `/api/audit/verify`, nothing else |
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
| `--daemon`                            | off         | Fork into the background (best-effort; stdout/stderr kept)     |

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
- `.zicato/runtime/active_tournament.events.jsonl` — an append-only event log the supervisor folds into `{tournament_id, generation_id, parent_generation_id, round, started_at, entries[], gate, partial_aggregate, predicted_verdict, structure, rounds, gen_states}`
- `.zicato/runtime/control/kill_requests/{run_id}` — kill requests from the Python parent
- `.zicato/current_epoch` — single-line epoch id marker
- `.zicato/lineage.json` — `{epochs: [{id, generations[]}]}`

Files written by the control endpoints (mounted only without
`--no-dashboard`, and refused under `--read-only`):

- `.zicato/runtime/control/pause_epoch` (removed by `POST /api/control/resume`)
- `.zicato/runtime/control/skip_round`
- `.zicato/runtime/control/kill_runs/{run_id}`
- `.zicato/runtime/control/promote/{generation_id}`
- `.zicato/runtime/control/reject/{generation_id}`
- `.zicato/runtime/control/rubric_replacement.txt`

All writes go through `path.tmp` + `rename`, matching the atomicity the
Python orchestrator expects. With `--ledger-dir` the supervisor also
appends to its audit ledger, and with `--diff-containment` it writes the
audit's findings into the epoch's health directory.

## HTTP API

Always mounted:

- `GET /statusz`, `GET /statusz.json` — the watchdog's own state
- `GET /api/audit/verify` — the audit ledger's chain integrity

Mounted only without `--no-dashboard`:

- `GET /` — dashboard UI (a placeholder, since `static/` is empty)
- `GET /static/*path` — UI assets
- `GET /api/state` — composite snapshot
- `GET /api/epoch` — the current epoch
- `GET /api/run-log` — the run log
- `GET /api/heartbeat` — heartbeat only
- `GET /api/active-runs` — list of active run state objects
- `GET /api/active-tournament` — current tournament shape
- `GET /api/lineage` — generation DAG
- `GET /api/health` — `{status, version, uptime_seconds, read_only, workspace}`
- `GET /events` — SSE stream; sends `snapshot` on connect, then `state_change` events
- `POST /api/control/pause` — `{reason?}`
- `POST /api/control/resume` — clears the pause flag
- `POST /api/control/skip-round` — `{reason?}`
- `POST /api/control/kill/{run_id}`
- `POST /api/control/promote/{generation_id}`
- `POST /api/control/reject/{generation_id}`
- `POST /api/control/brief` — raw text body, replaces the proposer brief

`POST` endpoints return `202 Accepted` on success, `403 Forbidden` when
running with `--read-only`, and `400 Bad Request` if the path-parameter
id contains characters outside `[A-Za-z0-9._-]`.

## Dashboard UI

The dashboard UI lives with the standalone Python dashboard service at
`zicato/dashboard/static/`, which serves it off disk. `crates/supervisor/static/`
is intentionally empty (a single `.gitkeep`) and is retained only so the
`include_dir!` macro in `static_assets.rs` still compiles; under
`--no-dashboard` — which `zicato evolve` always uses — the in-binary
dashboard routes are not mounted at all.

## Tests

```
cargo test -p zicato-supervisor     # or: make supervisor-test
```

Runs unit tests inline in each module plus the integration tests in
`tests/integration_test.rs`. `make supervisor-check` adds formatting and
clippy. The integration suite spins up the server
against a temporary workspace, exercises every endpoint, verifies the
control-file atomic write, and confirms signal escalation against a
real child process that ignores SIGTERM.

## Troubleshooting

- **Port already in use.** The supervisor automatically tries
  `--port..=--port+10` before giving up.
- **Heartbeat warnings on startup.** Expected: if the Python side
  hasn't written `heartbeat.json` yet there is nothing to honor. The
  log message is `no heartbeat file present` at debug level.
- **`Address already in use` even after a crash.** Linux holds the
  socket in TIME_WAIT for ~60s. Either wait or pick another port.
- **`failed to start filesystem watcher`.** Usually means the inotify
  watch limit is exhausted. Increase `fs.inotify.max_user_watches`.
- **SSE clients disconnect after seconds of no traffic.** A keep-alive
  comment is sent every 15s; reverse proxies may need a higher
  read-timeout.
