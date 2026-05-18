# zicato-supervisor

Single-binary watchdog for the zicato runtime state files. Auto-spawned
by `zicato evolve` (always with `--no-dashboard`); can also be run
standalone against an existing workspace.

The binary is watchdog-only:

1. **Watchdog.** Polls `.zicato/runtime/heartbeat.json` and the per-run
   files under `.zicato/runtime/active_runs/`. If the orchestrator's
   heartbeat or any run's `last_progress` goes stale past the configured
   thresholds, the supervisor sends SIGTERM, waits a grace period, and
   escalates to SIGKILL.
2. **`/statusz`.** A terse, self-contained operational page (and
   `/statusz.json`) reporting the watchdog's own state. Always served.

The dashboard UI is no longer the supervisor's concern — it is served
by the standalone Python dashboard service (`zicato.dashboard`), which
`zicato evolve` spawns separately. The legacy in-binary dashboard
routes still compile but are never mounted under `--no-dashboard`.

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
`strip = true`, giving roughly 4 MB on x86_64-linux.

## Run

```
./target/release/zicato-supervisor --workspace /path/to/.zicato
```

The server prints its listening URL to stdout
(`zicato-supervisor listening on http://127.0.0.1:7892`).

### CLI flags

| Flag                            | Default     | Meaning                                                        |
| ------------------------------- | ----------- | -------------------------------------------------------------- |
| `--workspace PATH`              | `.zicato`   | Workspace root (contains `runtime/`, `epochs/`)                |
| `--port N`                      | `7892`      | Preferred port; tries `N..=N+10` if busy                       |
| `--bind ADDR`                   | `127.0.0.1` | Bind address                                                   |
| `--read-only`                   | off         | Reject all `POST /api/control/*` with 403                      |
| `--interval SECS`               | `2`         | Watchdog poll interval                                         |
| `--heartbeat-stale-warn SECS`   | `30`        | Log a warning when the heartbeat is this old                   |
| `--heartbeat-stale-kill SECS`   | `90`        | Escalate to SIGTERM/SIGKILL when the heartbeat is this old     |
| `--run-stale-warn SECS`         | `30`        | Log a warning when a run's last_progress is this old           |
| `--run-stale-kill SECS`         | `120`       | Escalate when a run is this stalled (or past its deadline)     |
| `--log LEVEL`                   | `info`      | Log level (`RUST_LOG` overrides)                               |
| `--daemon`                      | off         | Fork into the background (best-effort; stdout/stderr kept)     |

## State file contract

The supervisor reads, but does not write, the runtime state files. Their
shapes are defined by the Python side; this crate uses
`#[serde(default)]` on every field so additions don't break the
supervisor at runtime. Schema reference lives in
[`docs/design/RUNTIME.md`](../docs/design/RUNTIME.md) (added by the
sibling R3 design-docs branch).

Files consumed:

- `.zicato/runtime/heartbeat.json` — `{pid, instance_id, last_heartbeat, phase, epoch_id, generation_id, round}`
- `.zicato/runtime/lock.json` — `{pid, instance_id, started_at, workspace}`
- `.zicato/runtime/active_runs/{run_id}.json` — `{run_id, pid, entry_id, generation_id, started_at, last_progress, deadline, phase, progress, message}`
- `.zicato/runtime/active_tournament.json` — `{tournament_id, generation_id, parent_generation_id, round, entries[], gate, partial_aggregate, predicted_verdict}`
- `.zicato/current_epoch` — single-line epoch id marker
- `.zicato/lineage.json` — `{generations[], edges[]}`

Files written (POST endpoints only, when not `--read-only`):

- `.zicato/runtime/control/pause_epoch`
- `.zicato/runtime/control/skip_round`
- `.zicato/runtime/control/kill_runs/{run_id}`
- `.zicato/runtime/control/promote/{generation_id}`
- `.zicato/runtime/control/reject/{generation_id}`
- `.zicato/runtime/control/rubric_replacement.txt`

All writes go through `path.tmp` + `rename`, matching the atomicity the
Python orchestrator expects.

## HTTP API

- `GET /` — dashboard UI (or placeholder if the UI bundle wasn't compiled in)
- `GET /static/*path` — UI assets
- `GET /api/state` — composite snapshot
- `GET /api/heartbeat` — heartbeat only
- `GET /api/active-runs` — list of active run state objects
- `GET /api/active-tournament` — current tournament shape
- `GET /api/lineage` — generation DAG
- `GET /api/health` — `{status, version, uptime_seconds, read_only, workspace}`
- `GET /events` — SSE stream; sends `snapshot` on connect, then `state_change` events
- `POST /api/control/pause` — `{reason?}`
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
cargo test
```

Runs unit tests inline in each module plus the integration tests in
`tests/integration_test.rs`. The integration suite spins up the server
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
