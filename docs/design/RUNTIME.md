# Runtime

This document describes the **runtime layer** that surrounds zicato's
meta-loop: the on-disk state files in `.zicato/runtime/`, the
watchdog supervisor binary auto-spawned by `zicato evolve`, the
heartbeat and escalation protocols, the resume semantics on
orchestrator restart, and the concurrency model that lets parallel
tournaments coexist on one workspace.

> **What ships today (reconciled with the code).** The `.zicato/runtime/`
> state files, the heartbeat (`HeartbeatBeater`), the pid-based
> workspace lock, the atomic-write helper, and the control-file
> protocol module are all shipped (`src/zicato/runtime/`). The Rust
> watchdog supervisor (`crates/supervisor/`) is shipped and
> auto-spawned by `evolve` in **watchdog-only mode** (`--no-dashboard`):
> it runs the heartbeat/run staleness loops, escalates
> SIGTERM → grace → SIGKILL, and serves a `/statusz` probe. The live
> **dashboard is a separate Python (Starlette) service** spawned
> alongside the watchdog (see [DASHBOARD.md](DASHBOARD.md)) — *not* the
> Rust binary. The **subprocess tournament workers (L3) are now
> SHIPPED**: every board-entry run executes in its own
> `python -m zicato._tournament_worker` subprocess
> (`src/zicato/_tournament_worker.py`, spawned by
> `src/zicato/tournament/runner.py`), which lets a per-run wall-clock
> budget be **hard-enforced** by killing the process and gives each run
> a fresh interpreter (no module-cache bleed between generations). One
> layer remains **planned**: **orchestrator-side consumption of control
> commands** — the dashboard writes `control/` files today, but the
> orchestrator does not yet read them at safe points. Sections that
> still describe a not-yet-shipped shape are marked **(planned)**
> inline.
>
> **Transport fidelity across the worker boundary.** Because scoring
> now happens *inside* the subprocess, two correctness guarantees keep
> a worker's verdict identical to an in-process one: the per-epoch
> `per_judge_weights` survives the args-file transport into the worker
> (`_tournament_worker._weights_from_args`), so a duel is scored
> under the same per-judge weighting the parent configured; and the
> in-run process judges grade against the **real tool-call ledger** the
> run produced, not a narrated approximation of it (so a board judge
> like `file_findability` sees what the agent actually did).

The runtime layer is the **production-readiness pass**. The meta-loop
described in [ARCHITECTURE.md](ARCHITECTURE.md) is correct against
cooperative inner harnesses; the runtime layer is what makes it
correct against the pathological cases (network hangs, infinite
loops, orchestrator crashes, OOM kills). For the layered defense
model that motivates the file shapes in this document, see
[ROBUSTNESS.md](ROBUSTNESS.md). For the live UI built on top of the
state files described here, see [DASHBOARD.md](DASHBOARD.md).

## 1. Design goals

The runtime layer holds to four goals; everything else in the design
follows from them.

1. **No state in process memory that can be lost on crash.** Every
   piece of runtime state lives on disk as a plain file. A crashed
   orchestrator can be restarted and pick up where it left off; a
   crashed supervisor can be restarted with no loss of information.
2. **No LLM in the watchdog path.** Hang detection, escalation, and
   process-kill decisions are made by a fast, deterministic process
   that reads file timestamps. An LLM call in this path would be the
   thing the watchdog is meant to defend against.
3. **A separate-language binary for the watchdog.** The watchdog
   supervisor is a Rust binary, not a Python process. The
   orchestrator (Python) is the thing prone to GIL wedges; making
   the watchdog a separate language with a separate runtime is the
   cheapest way to make sure the watchdog cannot itself be the cause
   of the failure it's there to catch. (The same binary *can* also
   serve the dashboard UI, but as shipped that role is split out into
   a separate Python service — see §3 and [DASHBOARD.md](DASHBOARD.md).)
4. **One mental model for the operator.** `zicato evolve` auto-spawns
   the watchdog supervisor *and* the dashboard service, and prints the
   dashboard URL. The operator never has to remember "did I start the
   dashboard?" for the common case.

## 2. State file layout

All runtime state lives under `.zicato/runtime/`. The directory is
created lazily by `zicato evolve` and torn down (orphan files
removed) at clean exit. Files in this directory are the **single
source of truth** for live state; nothing important is held only in
Python memory or only in the supervisor's memory.

```
.zicato/runtime/
├── lock.json                       # exclusive workspace lock
├── heartbeat.json                  # orchestrator pulse, bumped every 1-5s
├── dashboard.json                  # dashboard's actually-bound host/port
├── active_tournament.json          # current tournament shape + per-entry status
├── active_runs/
│   ├── {run_id}.json               # one file per in-flight tournament run
│   └── ...
├── control/                        # incoming operator commands (written by dashboard)
│   ├── pause_epoch                 # presence = "pause requested"
│   ├── skip_round                  # presence = "skip current round"
│   ├── kill_runs/
│   │   └── {run_id}                # presence = "kill this run"
│   ├── promote/
│   │   └── {generation_id}         # presence = "force promote this generation"
│   ├── reject/
│   │   └── {generation_id}         # presence = "force reject"
│   └── rubric_replacement.txt      # contents = new rubric body
└── control_log/                    # consumed commands persist here for audit (planned)
    ├── 2026-05-14T12:34:50Z_pause_epoch.json
    └── ...
```

The path helpers for this tree ship in `src/zicato/runtime/paths.py`.
`dashboard.json` is written by the Python dashboard service once it
binds (it walks `+1` from its preferred port if taken), so `evolve`
can read back the *actually-bound* port rather than assume one. The
watchdog supervisor is auto-spawned by `evolve` as a child process; it
does not write a `.pid` / `.stdout` / `.stderr` file under `runtime/`
(its stdio is inherited from `evolve`). `control_log/` is created by
the runtime helpers, but the audit-on-consume flow is **(planned)** —
see §2.5.

### 2.1 `lock.json` — exclusive workspace lock

The orchestrator acquires an exclusive lock on the workspace at
startup. This prevents two `zicato evolve` invocations from
interleaving writes to the same epoch directory — a class of bug
that's hard to detect after the fact and corrupts the journal.

```json
{
  "pid": 84321,
  "instance_id": "default",
  "started_at": "2026-05-14T12:34:50.123Z",
  "workspace_root": "/home/op/myagent/.zicato"
}
```

**Acquisition.** A **pid-based JSON lock**, not `fcntl.flock`
(`src/zicato/runtime/lock.py`). The file records the owning pid; the
lock is held for the lifetime of the orchestrator process and removed
(best-effort) on clean exit. A `flock`-style advisory lock was rejected
because it leaves no human-readable owner behind and is released
invisibly on process death; the pid-JSON form lets the next invocation
*see* the stale owner and decide. The supervisor does NOT acquire the
lock — there's only one orchestrator, but it can read the file to know
whose heartbeat it's watching.

**Stale lock handling.** If `lock.json` exists but the named PID is
not alive, the new orchestrator considers the lock stale and steals
it (`steal_stale=True`, the default). The liveness check is
`os.kill(pid, 0)` — cheap, no signal actually delivered; `ESRCH` means
dead. A live foreign pid raises `WorkspaceLockHeld`. Re-acquisition by
the same pid is idempotent. This recovers automatically from
kernel-level kills, host reboots, and any case where the orchestrator
died without clean exit.

**Why not a fixed lockfile name like `.lock`.** The richer JSON
payload is what makes stale-lock recovery readable: a stale lock
written by yesterday's run carries enough metadata that the operator
can confirm "yes, this is left over" without grep-ing logs.

### 2.2 `heartbeat.json` — orchestrator pulse

The orchestrator writes a fresh heartbeat every 1-5 seconds (default
2s; configurable). The supervisor reads it; a stale heartbeat is
the primary signal that the orchestrator wedged.

```json
{
  "pid": 84321,
  "instance_id": "default",
  "started_at": "2026-05-14T12:34:50.123Z",
  "last_heartbeat": "2026-05-14T12:35:02.418Z",
  "phase": "tournament",
  "epoch_id": "hardened_research",
  "generation_id": "v5",
  "round_index": 4,
  "round_started_at": "2026-05-14T12:34:55.000Z",
  "harmonograf_url": ""
}
```

This is the shipped `Heartbeat` dataclass (`src/zicato/runtime/state.py`).
`last_heartbeat` is the freshness timestamp the watchdog keys on
(`started_at` is the orchestrator's boot time); the per-run population is
tracked in the separate `active_runs/*.json` files rather than inlined
here.

**Atomicity.** Written via the atomic-write helper
— that is, write to `heartbeat.json.tmp`, `fsync`, then `rename`.
Readers (the watchdog supervisor, the dashboard) always see either the
old or the new content, never a partial write. This matters because the
supervisor polls on a short interval and a partial read would be a
false positive for "orchestrator went silent".

**Cadence.** A heartbeat is written:

- Every 2s on a timer (the floor — guarantees no false wedge
  detection during long-but-progressing work).
- On every phase transition (`proposing → applying → running →
  tournament → journaling → ...`) — captured fresh on each step.
- On a phase change, the orchestrator bumps the beat immediately
  (`HeartbeatBeater.bump_now`) so the dashboard and watchdog see the
  new phase without waiting for the next timer tick.

**Staleness thresholds.** The shipped watchdog uses **two** thresholds
keyed on `last_heartbeat` age (both configurable on the supervisor
binary): `--heartbeat-stale-warn` (default 30s — log a warning) and
`--heartbeat-stale-kill` (default 90s — escalate). The two-stage
threshold is loose enough to absorb a slow disk sync or a
paused-by-debugger orchestrator before it warns, and looser still
before it escalates.

### 2.3 `active_tournament.json` — current tournament shape

A single file describing the in-progress tournament, refreshed on
every entry-completion event. The dashboard's "active tournament"
panel reads from this file; the supervisor uses the per-entry
status to compute the predicted gate verdict (see
[DASHBOARD.md](DASHBOARD.md) §4).

```json
{
  "round": 4,
  "epoch": "hardened_research",
  "parent_generation": "v4",
  "candidate_generation": "v5",
  "started_at": "2026-05-14T12:34:55.000Z",
  "entries": [
    {
      "entry_id": "short_solar",
      "weight": 1.0,
      "parent_status": "done",
      "parent_drift_loss": 0.42,
      "parent_pass_fail": true,
      "candidate_status": "done",
      "candidate_drift_loss": 0.31,
      "candidate_pass_fail": true,
      "delta_drift_loss": -0.11,
      "delta_pass_fail": "tie"
    },
    {
      "entry_id": "long_solar_with_constraints",
      "weight": 1.5,
      "parent_status": "done",
      "parent_drift_loss": 0.55,
      "parent_pass_fail": false,
      "candidate_status": "running",
      "candidate_drift_loss": null,
      "candidate_pass_fail": null,
      "delta_drift_loss": null,
      "delta_pass_fail": null
    },
    ...
  ]
}
```

**Entry status values.** `queued | running | done | aborted | killed`.
`aborted` distinguishes wall-clock-budget-exhausted from `killed`
(operator force-kill via dashboard).

**Update points.**

- Tournament start: file written with all entries `queued`, both
  sides null.
- Each side of each entry transitions `queued → running → done`
  (or `aborted` / `killed`). The orchestrator rewrites the file
  atomically on every transition.
- Tournament end: file is moved to
  `.zicato/epochs/{epoch}/generations/v{N}/tournament.json` as a
  durable record, and `active_tournament.json` is removed.

The dashboard's predicted gate verdict (best/worst case for
remaining entries) is computed from this file alone — no extra
state needed. See [DASHBOARD.md](DASHBOARD.md) §4 for the
projection function.

### 2.4 `active_runs/{run_id}.json` — per-in-flight-run state

For every tournament run currently executing, one file. The shipped
`ActiveRun` dataclass and its read/write helpers live in
`src/zicato/runtime/state.py`.

> **Worker ownership (SHIPPED — L3).** The model described here — the
> *subprocess worker* that owns the run writes its own status file,
> independent of the orchestrator, so detection survives an
> orchestrator-side wedge — is the **L3** shape and is now **shipped**.
> The per-run worker (`_tournament_worker.py`) writes
> `active_runs/{run_id}.json` with `pid = os.getpid()` and bumps
> `last_progress` from a per-run heartbeat thread
> (`RunHeartbeatBeater`), removing the file on a clean exit; the
> orchestrator reaps a worker that died without cleaning up. (See
> [ROBUSTNESS.md](ROBUSTNESS.md) §2.3.)

As shipped (`ActiveRun`), the file carries:

```json
{
  "run_id": "e4f2_short_solar_candidate",
  "pid": 84522,
  "started_at": "2026-05-14T12:35:00.000Z",
  "last_progress": "2026-05-14T12:35:05.000Z",
  "wall_clock_budget_seconds": 120,
  "deadline": "2026-05-14T12:37:00.000Z",
  "events_jsonl_path": ".zicato/epochs/hardened_research/generations/v5/runs/short_solar/events.jsonl",
  "entry_id": "short_solar",
  "generation_id": "v5",
  "epoch_id": "hardened_research"
}
```

**`last_progress` cadence.** The writer bumps `last_progress`
(`touch_active_run_progress`) as the run makes progress. The watchdog
treats a run whose `last_progress` is older than `--run-stale-kill`
(default 120s) — or whose `deadline` has passed — as a candidate for
escalation, independently of whether the orchestrator itself is
healthy. The per-run subprocess worker is the one bumping
`last_progress` (via its `RunHeartbeatBeater` thread), so the signal
survives an orchestrator-side wedge.

**File lifecycle (shipped L3 shape).**

- Created when the worker is spawned (orchestrator does the
  `asyncio.create_subprocess_exec`; the worker's first action is
  writing this file with its own PID).
- Updated by the worker as it makes progress.
- Removed by the worker on clean exit, OR removed by the
  orchestrator if the worker exited without cleaning up (the
  orchestrator reaps zombies and ensures the file matches actual
  process state).

The `write_active_run` / `touch_active_run_progress` /
`remove_active_run` helpers (`runtime/state.py`) are the same writers;
they now run **inside the worker** rather than in the orchestrator.

**Why a separate file per run, not one big `active_runs.json`.**
Concurrent writes. The orchestrator may launch a fresh worker while
another worker is updating its own status. One-file-per-run
means no inter-worker write contention; each worker is the sole
writer of its own file.

### 2.5 `control/` and `control_log/` — operator action channel

`control/` is where the dashboard writes operator commands.

> **What ships today.** The **write side** is live: the Python
> dashboard's POST endpoints (and `src/zicato/runtime/control.py`'s
> `write_command`) drop the command files described below atomically.
> The runtime module also exposes the **consume side**
> (`consume_command`, `is_paused`, `list_pending_commands`), which
> moves a consumed file into `control_log/`. What is **(planned)** is
> the orchestrator *calling* the consume side: the evolve loop does not
> yet read `control/` at its safe points, so a command written today is
> recorded but not yet acted on. The intended contract is below.

The orchestrator is to read `control/` at **safe points only** —
between board entries, between rounds, between epoch lifecycle stages —
never mid-run. When a command is consumed, the file is moved
atomically into `control_log/` with a timestamp prefix, preserving
an immutable audit trail.

The full protocol — file shapes, safe-point semantics, write-back
permissions per command — is documented in
[DASHBOARD.md](DASHBOARD.md) §5. This section enumerates only the
file layout, since it's part of the runtime contract.

| File | Trigger | Form |
|---|---|---|
| `control/pause_epoch` | dashboard "pause" button | empty file; presence is the signal |
| `control/skip_round` | "skip" button on active round | empty file |
| `control/kill_runs/{run_id}` | "kill" button on a run row | empty file per run |
| `control/promote/{gen_id}` | "force promote" button (v1.3) | empty file per generation |
| `control/reject/{gen_id}` | "force reject" button (v1.3) | empty file per generation |
| `control/rubric_replacement.txt` | "edit proposer brief" panel | text file; contents replace `brief.md` |

Consumed-command record in `control_log/`:

```json
{
  "ts_consumed": "2026-05-14T12:38:15.412Z",
  "command": "pause_epoch",
  "issued_via": "dashboard",
  "issued_by": "operator-localhost",
  "consumed_at_safe_point": "between_rounds",
  "effect": "epoch paused; awaiting resume"
}
```

The audit trail is the **safety net for operator overrides**. v1.3
adds gate-override commands (`promote`, `reject`); the audit log
ensures the override is recorded next to the original tournament
record, so the journal cannot be silently rewritten.

## 3. The watchdog supervisor binary

`zicato-supervisor` is a Rust binary (`crates/supervisor/`,
built `cargo build --release -p zicato-supervisor`). It is
auto-spawned by `zicato evolve` (opt out with `--no-dashboard`) and
killed when `evolve` exits. The binary is **resolved** at spawn time
from, in order: a configured `supervisor_binary` path, the bundled
`zicato/_bin/zicato-supervisor` (placed by the build hook), the system
`PATH`, then a dev-checkout `target/release/` build. If none resolve,
`evolve` prints a warning and runs **without** the watchdog.

The binary is capable of two roles, but **as shipped only one is
used**:

- **Watchdog (always on).** Polls `.zicato/runtime/heartbeat.json` and
  the per-run files under `active_runs/`; on stale heartbeat or stalled
  run it escalates SIGTERM → grace → SIGKILL. It also serves a terse
  `/statusz` (and `/statusz.json`) operational probe. No LLM, no
  in-memory authoritative state — every decision is a pure function of
  the on-disk files.
- **Dashboard server (compiled, not mounted).** The binary *can* serve
  the HTTP + SSE dashboard, but `zicato evolve` always spawns it with
  `--no-dashboard`, so those routes are not mounted. The live dashboard
  UI is served by the **separate Python service** (see §3.0 and
  [DASHBOARD.md](DASHBOARD.md)).

The watchdog uses a filesystem watcher plus a poll loop on
`.zicato/runtime/`.

### 3.0 Two processes: watchdog + dashboard service

`zicato evolve` spawns **two** children (unless `--no-dashboard`):

| Process | What it is | Default port | Role |
|---|---|---|---|
| `zicato-supervisor` | Rust binary, spawned `--no-dashboard` | `7920` (walks `7920..=7930`) | watchdog + `/statusz` |
| `python -m zicato.dashboard` | Python/Starlette service | `7892` (walks `+1` up to 10×) | the dashboard UI + API the operator opens |

They bind **distinct** default ports so neither walks onto the other.
The dashboard's URL `evolve` prints is read back from
`runtime/dashboard.json` (the port the dashboard *actually* bound),
never assumed. This split — dashboard as its own Python service rather
than a role of the Rust binary — was a deliberate decision (see the
ecosystem notes; the Rust binary's in-process dashboard routes are
retained but dormant).

### 3.1 Lifecycle

```
┌─────────────────────────────────────────────────────────────────┐
│  zicato evolve (Python orchestrator process)                    │
│  ─────────────────────────────────────────                      │
│  1. Acquire .zicato/runtime/lock.json (pid-based JSON).         │
│  2. Start the HeartbeatBeater (writes heartbeat.json).          │
│  3. Spawn zicato-supervisor (watchdog) with --no-dashboard,     │
│     and python -m zicato.dashboard (the UI service).            │
│  4. Read runtime/dashboard.json; print the dashboard URL.       │
│  5. Run the meta-loop (rounds 1..N).                            │
│  6. On exit: tear down the dashboard first (free its port),     │
│     then the watchdog — SIGTERM, wait up to 5s, SIGKILL if      │
│     still alive. Release the lock.                              │
└─────────────────────────────────────────────────────────────────┘
                          │ spawns (×2)
                          ▼
┌─────────────────────────────────────────────────────────────────┐
│  zicato-supervisor (Rust, --no-dashboard)                       │
│  ─────────────────────────                                      │
│  1. Resolve workspace; open the runtime/ watcher.               │
│  2. Bind --port (default 7920, +1 up to +10) for /statusz.      │
│  3. Loop: on the poll interval (default 2s), check heartbeat    │
│     staleness and per-run staleness; escalate on threshold      │
│     (see §3.3). Record escalations in a ring buffer /statusz    │
│     reads back.                                                 │
│  4. On SIGTERM: shut down cleanly.                              │
└─────────────────────────────────────────────────────────────────┘
```

The supervisor is **strictly downstream of the orchestrator**.
The orchestrator decides what to do; the supervisor watches it do
it and yells when it stops. The one exception is the SIGTERM /
SIGKILL escalation — the supervisor can kill a stuck run's process
without the orchestrator's involvement. See §3.3.

### 3.2 Heartbeat protocol

```
orchestrator                           supervisor
     │                                      │
     │ write heartbeat.json (t=0)           │
     ├─────────────────────────────────────►│  inotify: rename → reload
     │                                      │
     │ ... working ...                      │  set last_seen = t
     │                                      │
     │ write heartbeat.json (t=2)           │
     ├─────────────────────────────────────►│  inotify: rename → reload
     │                                      │
     │ ... working ...                      │  set last_seen = t+2
     │                                      │
     │     ╳  (orchestrator wedges)         │  tick: now - last_hb = 20s  OK
     │                                      │  tick: now - last_hb = 35s  WARN (>30)
     │                                      │  tick: now - last_hb = 95s  STALE (>90)
     │                                      │
     │                                      │  -> log "orchestrator stalled"
     │                                      │  -> record in /statusz ring buffer
     │                                      │
     │                                      │  (supervisor does NOT kill the
     │                                      │   orchestrator. The orchestrator
     │                                      │   may simply be slow. Killing it
     │                                      │   is an operator decision.)
```

The two thresholds are `--heartbeat-stale-warn` (default 30s, log
only) and `--heartbeat-stale-kill` (default 90s). On the
kill threshold the orchestrator's stall is recorded; the watchdog does
not itself terminate the orchestrator.

**The supervisor does NOT kill the orchestrator on heartbeat
staleness.** The orchestrator might be slow for legitimate reasons
(GC pause, slow LLM endpoint, paused by debugger). The watchdog logs /
exposes "orchestrator looks stalled" on `/statusz` (and the dashboard
surfaces it) and the operator decides. The supervisor only escalates
*runs* automatically — see §3.3.

If the operator wants automatic orchestrator restart, that's a
process-supervisor concern (systemd, supervisord, k8s); not
zicato's job to reinvent.

### 3.3 Escalation (SIGTERM → grace → SIGKILL)

For each `active_runs/{run_id}.json`, the supervisor escalates when
EITHER condition holds:

- the run's `deadline` (set to `started_at + wall_clock_budget_seconds`)
  has passed, OR
- the run's `last_progress` is older than `--run-stale-kill`
  (default 120s). (`--run-stale-warn`, default 30s, logs first.)

```
  t=0       run is past its deadline OR last_progress > run-stale-kill
            │
            ▼
  t=0       supervisor logs "escalating run {run_id}"; records it in
            │ the /statusz escalation ring buffer
            │
            ▼
  t=0       supervisor sends SIGTERM to the run's PID
            │
            │ waits the escalation grace period (default 5s)
            │
            ▼
  t=grace   process cooperated → exits → done
            OR
  t=grace   process still alive → SIGKILL
            │
            ▼
            The run's active_runs/{run_id}.json is left for the
            orchestrator to clean up (it notices the process is dead
            and finalises the run).
```

**Why two-stage SIGTERM → SIGKILL.** SIGTERM gives the run a
chance to flush its goldfive event sink (so the events.jsonl is
not truncated mid-event) and exit cleanly. SIGKILL is uninterruptible
— the process is gone immediately and we lose the last few events.
Always preferring SIGKILL would corrupt the JSONL on every
escalation; always preferring SIGTERM would leave a truly wedged
process hanging forever. The "run's PID" is the per-run subprocess
worker's own PID (L3, shipped), so a SIGKILL takes out exactly that one
run's process and leaves the orchestrator running — see
[ROBUSTNESS.md](ROBUSTNESS.md) §2.3.

**Why the supervisor, not the orchestrator, owns escalation.** The
orchestrator IS a Python process; a wedge in the orchestrator
would prevent it from sending the signal. The supervisor is in a
different language with a different runtime; a wedge in one cannot
wedge the other. This is the layered defense in
[ROBUSTNESS.md](ROBUSTNESS.md) §L4.

### 3.4 Why Rust

The supervisor could be a Python script, a C++ binary, or a Go
binary. The choice of Rust traces to four practical concerns; each
is non-cosmetic.

| Concern | Python | C++ | Go | **Rust** |
|---|---|---|---|---|
| Single static binary | no (interpreter required) | yes (with care) | yes | **yes** |
| Memory-safe (cannot itself crash and corrupt state) | yes (but GIL wedges) | no | yes | **yes** |
| Native inotify / FSEvents | yes (pyinotify / fsevents) | yes (libinotify) | yes | **yes (notify crate)** |
| Fast startup (~ms; spawned every `evolve`) | slow (50-200ms cold) | fast | fast | **fast (~5-20ms)** |
| HTTP server in stdlib / mature crate | yes (stdlib) | partial | yes (net/http) | **yes (axum / hyper)** |

**Why not Python.** The watchdog is the thing that catches Python
wedges. If the watchdog is itself Python, a process-wide GIL wedge
(CPU-bound C extension, fork misbehaviour, signal-safety violation)
can wedge both the orchestrator and the watchdog at the same time
— precisely the case the design is meant to defend against. The
choice of a different language for the watchdog is structural, not
ergonomic.

**Why not C++.** No safety guarantee. A use-after-free in the
watchdog corrupts state; the cure becomes the disease. Rust
gives us "different runtime" without giving up on memory safety.

**Why not Go.** Defensible alternative. Rejected on two minor
points: garbage-collected runtimes have pause behaviour that the
watchdog's tight timing budget should not have to absorb, and the
binary-size delta between Rust and Go is small enough that the
deployment story is the same. Rust also pairs better with the
single-binary ergonomic story (no `goimports`, no `go mod`-style
ambient).

**Why not eBPF / something exotic.** Watchdog is portable file-IO
and HTTP; it doesn't need kernel-level introspection. Choosing
something exotic adds friction (root, kernel versions, BPF
availability) for no gain.

### 3.5 Concurrency model

Inside `.zicato/runtime/` the writer rules are strict:

| File | Sole writer | Readers |
|---|---|---|
| `lock.json` | orchestrator | supervisor |
| `heartbeat.json` | orchestrator | supervisor, dashboard |
| `dashboard.json` | dashboard service | orchestrator (URL readback) |
| `active_tournament.json` | orchestrator | supervisor, dashboard |
| `active_runs/{run_id}.json` | the per-run subprocess **worker** that owns `run_id` (shipped L3); orchestrator only reaps a dead worker's file | supervisor, dashboard |
| `control/<command>` | dashboard service | orchestrator (planned consumer) |
| `control_log/*` | orchestrator (planned: on consume) | dashboard |

There are no shared writers. Every file has exactly one process
that writes to it; concurrent readers are safe because every write
is atomic-rename. No `fcntl` locks beyond the pid-based `lock.json`;
no shared in-memory mutable state.

**This is the load-bearing invariant for the design.** Locking
correctness in a multi-process system is hard. By making every
file single-writer, we get correctness for free at the cost of
some redundancy.

**Workers writing their own files: why this is safe (shipped L3).**
A worker ONLY writes `active_runs/{run_id}.json` for the run it
owns. Workers do not share files. The orchestrator may DELETE a
worker's file (when reaping a dead worker), but does not write to a
file a live worker owns. The only race window is "worker just deleted
its own file because it finished, orchestrator reads the file expecting
it to still exist" — and the orchestrator handles ENOENT as a normal
terminal state, not an error.

## 4. Resume semantics

> **(Planned.)** The crash-resume *protocol* below — inferring where a
> prior interrupted `evolve` left off and continuing — is **not yet
> shipped** (see §8). The durable artifacts and resume *markers* it
> relies on (atomic writes, the `outcome` block, `current_generation`)
> ship today; reading them to auto-resume is the planned half.

`zicato evolve` is designed to be restartable. The operator can
SIGTERM it, restart the machine, and re-run `zicato evolve` and the
loop continues from wherever it was when interrupted.

### 4.1 What persists across restart

| Artifact | Source of truth | Survives restart? |
|---|---|---|
| Promoted generations | per-generation directories under `epochs/{epoch}/generations/` | **yes** — these are the durable record |
| Pattern detector output | `epochs/{epoch}/patterns/round_NNN.json` | **yes** — written once per round |
| Journal | `epochs/{epoch}/journal.md` | **yes** — append-only |
| `experiment.json` (hypothesis + outcome) | per-generation file | **yes** — atomic update; either has outcome or doesn't |
| Per-run `events.jsonl` | per-entry files under `runs/{entry_id}/` | **yes** — but may be partial if the run was mid-flight |
| Per-run `loss.json` | per-entry files under `runs/{entry_id}/` | **yes** if reducer ran |
| `active_tournament.json` | runtime state | discarded on restart |
| `active_runs/` | runtime state | discarded on restart |
| `heartbeat.json` | runtime state | discarded on restart |

The asymmetry is deliberate: **artifacts in `epochs/` are committed
records; artifacts in `runtime/` are live state.** Resuming reads
the committed records to figure out where to start, then rebuilds
runtime state from scratch.

### 4.2 The resume protocol

When `zicato evolve` starts, before launching any new work:

```python
def resume_or_start_fresh(workspace):
    epoch = workspace.current_epoch()

    # Step 1: clean up stale runtime state.
    stale_lock = read_lock_if_stale(workspace)
    if stale_lock:
        log.warning("stealing stale lock from PID %d", stale_lock.pid)
        finalize_stale_runs(workspace, stale_lock)
        clear_runtime_directory(workspace)
    acquire_lock(workspace)

    # Step 2: figure out where the previous evolve left off.
    last_round = read_last_committed_round(epoch)
    last_outcome = read_outcome(last_round)

    if last_outcome is None:
        # Mid-round at the time of the interruption.
        # Determine which step we were on.
        step = infer_step_from_artifacts(last_round)
        return resume_at(epoch, last_round, step)

    # Step 3: previous round was fully committed. Start a fresh round.
    return start_round(epoch, last_round.number + 1)
```

**`infer_step_from_artifacts`** is the load-bearing helper:

| Artifacts present | Inferred step | Resume action |
|---|---|---|
| `experiment.json` (no outcome) + `patches/` + `snapshot/` + partial `runs/` | tournament-running | re-run only the entries that don't have `loss.json` |
| `experiment.json` (no outcome) + `patches/` + `snapshot/` + complete `runs/` (both sides) | tournament-complete-but-not-journaled | run gate, append outcome, append journal |
| `experiment.json` (no outcome) + `patches/` + `snapshot/` + no `runs/` | applied-but-not-running | start tournament from scratch |
| `experiment.json` (no outcome) + `patches/` + no `snapshot/` | proposed-but-not-applied | re-apply patches, then continue |
| `experiment.json` (no outcome) + no `patches/` | partial proposal | discard the experiment file (proposer is non-deterministic — re-propose fresh) |

The protocol is **conservative**: when it cannot tell exactly what
state things are in, it discards the partial work and re-runs from
the last clean checkpoint. The cost of a wasted re-run is one
round; the cost of a wrong inference is journal corruption.

### 4.3 Finalising stale runs

When the orchestrator steals a stale lock, it walks
`active_runs/` and finalises each entry with `status: "aborted"`,
`abort_reason: "orchestrator_crash"`. This makes the historical
record consistent: the dashboard panel for a stalled run shows
"aborted (orchestrator crash)" rather than "still running" — even
though the worker is long gone.

Workers are also reaped at this step. Any PID in `active_runs/*`
that is still alive gets SIGTERM → SIGKILL (skipping the grace
period; the run is being abandoned). Any PID that's already dead
gets noted in the audit log.

## 5. Worker subprocesses (SHIPPED — L3)

> **Shipped.** The **L3 subprocess worker** is in the tree:
> `src/zicato/_tournament_worker.py`, spawned per board-entry run by
> `src/zicato/tournament/runner.py`. Each run executes in its own OS
> process, so a per-run wall-clock budget can be **hard-enforced** by
> killing the process — the only reliable defense against a
> GIL-wedged or infinite-looping inner harness (§5.1). The shipped
> invocation differs from the original plan below in one respect: it is
> not a `zicato _worker` subcommand with flags but a module entry point
> taking a single JSON **args file** (`python -m
> zicato._tournament_worker <args-file.json>`); §5.2 is updated to that
> shape, the rest of the section describes the live behaviour.

The orchestrator runs each tournament run in a **subprocess worker**
(Python, spawned via `asyncio.create_subprocess_exec` of
`python -m zicato._tournament_worker`). One worker per
(generation, entry) pair; the parent and candidate sides of one
entry are two workers.

### 5.1 Why subprocesses (not threads, not asyncio alone)

Python's GIL means a CPU-bound or GIL-holding-C-extension loop in
the inner harness cannot be pre-empted by `asyncio.wait_for`. The
timeout exception fires when the event loop next runs, which is
never. The ONLY reliable defense is OS process boundary; SIGTERM
then SIGKILL after grace period works against any pathology
including infinite loops and deadlocked threads.

This is non-negotiable for production. See
[ROBUSTNESS.md](ROBUSTNESS.md) §3 for the full layered argument.

### 5.2 Worker contract

A worker is invoked as a module entry point taking a single JSON
**args file** — `_run_single` (`runner.py`) serialises one run's
inputs (run id, generation, entry, side, snapshot root, the scoring
weights incl. `per_judge_weights`, the wall-clock budget, the adapter
spec, the harmonograf URL/gRPC dial, …) to a temp file and spawns:

```
python -m zicato._tournament_worker <args-file.json>
```

via `asyncio.create_subprocess_exec(sys.executable, ...)`. The worker's
first action is writing `active_runs/{run_id}.json` with its own PID.
The worker then:

1. Loads the generation's snapshot.
2. Instantiates the adapter (see §5.4 below).
3. Wraps `auxiliary_call_llm` in the worker's per-call timeout
   layer (still useful as a fast path; see
   [ROBUSTNESS.md](ROBUSTNESS.md) §L1).
4. Calls `adapter.run_entry(entry, sinks=[JSONLPersistenceSink(...)])`.
5. After the terminal event, runs the loss reducer in-worker.
6. Writes `loss.json`.
7. Updates `active_runs/{run_id}.json` to `phase: "done"`.
8. Exits with code 0.

The worker also runs a **heartbeat goroutine** (technically an
`asyncio` task or a `threading.Thread` daemon — whichever the
adapter integrates with cleanest) that bumps `heartbeat_at` on
its own `active_runs/{run_id}.json` every 1s. This is the signal
the supervisor uses for stalled-worker detection independent of
orchestrator health.

### 5.3 Worker termination conditions

| Termination | Worker's exit behavior | `active_runs/{run_id}.json` final state |
|---|---|---|
| Clean (run completed) | exit code 0 | `phase: "done"`, then file removed |
| Wall-clock budget exceeded | `RunAborted(wall_clock_budget)` emitted, exit code 7 | `phase: "aborted"`, abort_reason set |
| Worker crashed (uncaught) | exit code != 0 | `phase: "agent_running"` (worker never got to update) — orchestrator reaps and re-stamps to `crashed` |
| SIGTERM from supervisor | runs cleanup, emits `RunAborted(killed)`, exit | `phase: "killed"`, cause: `supervisor_sigterm` |
| SIGKILL from supervisor | no cleanup; process gone | `phase: "agent_running"` — orchestrator reaps and re-stamps to `killed`, cause: `supervisor_sigkill` |
| `kill_runs/{run_id}` from dashboard | orchestrator forwards SIGTERM to worker | same as SIGTERM path |

**Where the per-run budget comes from.** Each run carries a
`wall_clock_budget_seconds` in its args file, and the worker enforces
it by self-aborting (and the supervisor backstops via the `deadline`).
That budget is normally the per-call/per-run default, but the `racing`
structure can tighten it per duel: its opt-in `matchup_budget_seconds`
/ `final_rung_budget_seconds` params (the **grind guard**,
[TOURNAMENT-STRUCTURES.md §3.5](TOURNAMENT-STRUCTURES.md#35-racing-the-endorsed-bracket-shaped-option))
ride on each scheduled `Matchup` and become the board-unit budget the
worker enforces — so the final full-board crowning duel, the
pathological grinder, can be capped without capping every duel.

The dashboard's "kill" button writes `control/kill_runs/{run_id}`;
the orchestrator notices the file at the next safe point (in this
case, the safe point is "right now" — kill is high-priority and
the orchestrator checks on a short timer) and forwards SIGTERM to
the worker. The supervisor's automatic escalation runs in parallel
as a backstop.

### 5.4 Adapter loading in workers

The adapter (Google ADK, LangChain, plain-callable) is loaded
**per worker**, not shared across workers. Each subprocess gets a
fresh interpreter and a fresh adapter instance. The cost is some
import overhead (~100-500ms per worker for ADK); the benefit is
total isolation. A pathological agent that corrupts global state
in one run does not affect any other run.

`HarnessAdapter.run_entry` is awaited via `asyncio.run` in the
worker's `main`. The worker's event loop dies with the worker;
the orchestrator's event loop is untouched. This is the cleanest
shape — no shared event loop, no cross-process await semantics to
debug.

Adapter-level module caching can be added later as an
optimisation (a long-running worker pool that imports the adapter
once and accepts work over a pipe). v1.1's shape spawns a fresh
worker per run for simplicity; if import overhead becomes
measurable, a pool lands as a follow-up. See
[STORAGE.md](STORAGE.md) §G7 for where the worker pool fits the
git-backed roadmap. **The import overhead has now been measured** —
§5.5 is that follow-up, written up as a gated design rather than
built.

### 5.5 Per-unit spawn cost — the measured tax, and the warm-pool design

> **Status: DESIGN, NOT BUILT.** No pool exists in the tree. The
> per-unit `create_subprocess_exec` in `runner.py` is still the
> shipped shape and this section does not change it. What ships
> alongside this note is only §5.5.7's **host-wide spawn limiter**
> and §5.5.8's **lazy role resolution** — two cheap, independent
> wins. The pool itself is gated on the fork-safety probe (§5.5.4)
> and the measurement plan (§5.5.6) landing green first.

#### 5.5.1 The measured tax

Every board unit pays a full cold-start interpreter: fork/exec of
`sys.executable -m zicato._tournament_worker`, then the import graph
the run needs. Measured on a 12-core Linux box, CPython 3.12, best of
five cold starts each (the harness is described in §5.5.6):

| Phase reached | import s | peak RSS MB | `sys.modules` |
|---|---|---|---|
| bare interpreter | 0.000 | 13 | 46 |
| `zicato._tournament_worker` imported | 0.078 | 31 | 328 |
| `+ zicato.adapters.adk` | 0.079 | 32 | 331 |
| `+ build_adk_model` (pulls `google.adk`) | 0.809 | 120 | 1657 |
| `+ litellm` (first LLM call) | 1.866 | 246 | 3111 |

Total process wall time for the last row — interpreter startup, exec,
and the whole graph — is **≈2.4 s**. Two readings matter:

1. **zicato's own code is not the cost.** The worker module plus the
   ADK adapter is 0.079 s / 32 MB. Everything above that is
   `google.adk` (+0.73 s / +88 MB) and `litellm` (+1.06 s / +126 MB).
2. **The tax is per unit, and it is paid concurrently.** In full mode
   one board unit is two workers (champion + challenger), so the
   default `parallelism = 4` means up to **8 workers × 246 MB ≈ 2 GB**
   of almost entirely *identical* module state resident at the same
   moment, and 8 × ~2.4 s of CPU burnt re-deriving it. A 20-entry
   board at `parallelism = 4` pays the import graph 40 times per
   round.

This is the real, unrefuted finding: **there is no worker pool, and
the import graph is re-derived per board unit.**

#### 5.5.2 What the original report got wrong

The issue that prompted this section proposed a *different* mechanism
— a spawn storm that trips the watchdog and cascades into respawns.
Each link of that chain was checked against the tree and **does not
hold**. Recording the refutations here so the mechanism is not
re-believed the next time the symptom (slow rounds, high RSS) is
observed:

| Claim | Verdict | Evidence in the tree |
|---|---|---|
| Calibration draws spawn `K × board` workers in a burst | **Refuted** | `tournament/calibration.py` runs `for draw in range(runs): await _run_board_units_fast(...)` — the draws are **serial**, and each draw is internally bounded by the fast-mode scheduler's semaphore. Peak concurrency is one draw's worth, not `K` draws' worth. |
| The semaphore does not really bound concurrency | **Refuted** | `scheduling._effective_unit_semaphore` + the `async with (meta_span(...), semaphore)` in each scheduler is the single admission gate every board unit passes. Measured burst at the default is 4–8 concurrent starts, and ~30–66 context switches per start — not a storm. |
| Worker imports trip the parent's watchdog | **Refuted** | The parent's grace is `_PARENT_BUDGET_GRACE_S = 30.0` s (`tournament/worker_transport.py`) on top of the entry's own budget. A 1–2.4 s import cannot consume a 30 s grace. |
| An aborted unit is respawned, cascading | **Refuted** | `_run_single` returns `_aborted_loss_profile(...)` and the scheduler records it; there is **no in-round retry**. An *infra* abort is additionally **not cached** (`scheduling.py`, `is_infra_abort_cause` branch), so the unit gets a correct cache MISS and is re-attempted by a **later round** — a deliberate, bounded re-attempt, not a cascade. |

The symptom the issue observed is real. Its proposed cause is not.
The cause is §5.5.1.

#### 5.5.3 The binding constraint: killability

The obvious fix — a persistent pool of long-lived worker processes
that each execute many units — is **not** available as stated,
because it breaks the invariant the whole L3 layer rests on.

**The killability invariant.** One board unit maps to exactly one
process group, and the supervisor kills that group by negating its
pgid:

* the worker is spawned with `start_new_session=True`
  (`runner.py`), so it `setsid`s before `exec` and becomes the leader
  of a fresh session/process group containing itself plus every
  grandchild the inner harness spawns (shells, helper tools);
* the worker's **first act** is writing
  `active_runs/{run_id}.json` with its own `pid`, its
  `pid_start_time`, and its `pgid` (§2.4);
* the supervisor reads that record and escalates via
  `killpg(pgid, SIGTERM)` → grace → `killpg(pgid, SIGKILL)`
  (`crates/supervisor/src/signal.rs`: `send_sigterm_group` /
  `send_sigkill_group`, vetted through `is_negatable_pgid` and the
  protected-pgid set, with `pid_start_time` guarding against pid
  reuse).

A naive pool destroys this three ways at once. A pool worker that
serves unit *A* then unit *B* has **one** pgid for both, so (a)
`ActiveRun.pgid` no longer identifies a unit, (b) killing unit *A*
kills the pool worker and therefore unit *B* (and every future unit
that worker would have served), and (c) `pid_start_time` — the
guard that proves the record still describes the process the
supervisor is about to signal — becomes constant across units and
stops discriminating. Losing (b) is losing the *entire reason* L3
exists: the hard per-run wall-clock budget.

**So the constraint is not "avoid a pool". It is: any pool must keep
one killable process group per board unit, and must keep
`ActiveRun{pid, pid_start_time, pgid}` meaning exactly what it means
today.** The supervisor must not need a single line of change.

#### 5.5.4 The design that satisfies it: warm pool, per-unit fork+setsid

Exactly one pool shape preserves the invariant:

```
pool parent (one per orchestrator, long-lived)
  ├─ imports goldfive + google.adk + litellm ONCE, at startup
  ├─ never issues an LLM call, never runs a unit itself
  ├─ single-threaded; blocks on a unix socket for unit requests
  └─ per request:  os.fork()  →  child: os.setsid(); run the unit; _exit()
```

The child inherits the parent's already-imported modules through
copy-on-write, so it pays **0 s and ~0 MB** of new import cost, and
then `setsid()`s — making it the leader of a **fresh session and
process group**, exactly as `start_new_session=True` does today.
Therefore:

* `ActiveRun.pid` = the forked child's pid (fresh per unit) — the
  child writes the record itself, unchanged;
* `ActiveRun.pgid` = the child's own new pgid (fresh per unit);
* `ActiveRun.pid_start_time` = the child's start time (fresh per
  unit, so the pid-reuse guard still discriminates);
* `killpg(pgid, …)` kills that unit and its grandchildren, and
  **nothing else** — not the pool parent, not a sibling unit.

The supervisor's contract is untouched. The orchestrator-side change
is confined to the transport: `runner._run_single` sends a request on
the pool socket and awaits a completion notification instead of
calling `create_subprocess_exec`, and the pool parent (not the
orchestrator's event loop) reaps the child. The args-file payload,
the result file, `loss.json`, the heartbeat thread, and the
abort-cause taxonomy all stay as they are.

Note what this design deliberately does *not* do: it does not reuse
an interpreter **across** units. Each unit still gets a fresh address
space (a fork, then never touched again by anything else), so the
module-cache isolation argument of §5.4 — two generations' source
must never share one `sys.modules` — survives intact. The pool
shares only the *pre-generation* import graph, which is identical for
every unit by construction.

**Fork-safety adjudication.** This design is only sound if the
imported graph is safe to fork. Probed against the pinned
dependency set (goldfive at the pinned rev, `google-adk`, `litellm`),
importing all three and inspecting the process immediately after:

| Property after import, before any call | Observed | Why it matters |
|---|---|---|
| non-main threads | **0** (`MainThread` only) | A fork from a multi-threaded process copies only the calling thread, leaving any lock the other threads held permanently locked. Also: CPython 3.12 raises a `DeprecationWarning` on `fork()` in a multi-threaded process. |
| open sockets | **0** (only fd 0/1/2 and `/dev/urandom`) | Two children inheriting one live TCP socket would interleave writes into the same connection — which does not crash, it silently corrupts LLM responses, i.e. corrupts *evaluation data*. This is the failure mode that must be impossible, not merely unlikely. |
| running asyncio event loop | **none** (no loop created) | A forked loop's selector, self-pipe, and pending callbacks are duplicated into both processes. |
| `grpc` loaded | **no** | gRPC's C core is notoriously fork-hostile. It arrives only via the harmonograf sink, which the *child* attaches. |

So the verdict is: **fork-safe, but only under an invariant that no
dependency guarantees.** `litellm` builds `module_level_client`
(`HTTPHandler`) and `module_level_aclient` (`AsyncHTTPHandler`) at
*import* time; today those wrap `httpx` clients that bind no loop and
open no socket until first use, which is precisely why the posture
above is clean. A future `litellm` or `google.adk` release that opens
a connection, starts a background thread, or creates a loop at import
time would break the pool **silently and in the worst possible
direction** — corrupted responses rather than a crash.

Therefore the pool is gated on a **fork-safety probe that runs in
CI**, asserting the four rows above hold for the currently pinned
dependencies, and on the pool parent honouring one rule absolutely:
**the pool parent imports, and never calls.** The moment the parent
issues an LLM call it acquires exactly the connection-pool and
event-loop state that makes forking unsafe. A probe that goes red on
a dependency bump is the signal to fall back to §5.5.7, not to
"investigate later".

Two smaller fork caveats, both cheap to handle in the child:

* **PRNG state.** `random` and `numpy`-style global PRNGs are
  duplicated by fork, so every child would draw the identical
  sequence. The child must reseed from its own `run_id` / `seed`
  before running the unit (zicato already threads a `seed` through
  `RuntimeConfig`, so this is a call, not a design).
* **Inherited descriptors.** The child inherits the parent's fds. The
  parent must hold no workspace lock and no `events.jsonl` handle; the
  per-invocation operator-log stream is opened `O_APPEND` and is
  already written by many workers concurrently today, so that one is
  a no-op.

No GPU/CUDA caveat applies: the dependency set contains no
`torch` / `nvidia-*` / `tensorflow` package, so there is no device
context to be invalidated by fork.

#### 5.5.5 Recycling and failure modes

**Recycling policy.** The pool parent is *not* recycled per unit —
that would reintroduce the cost. It is recycled on exactly three
triggers, each cheap to detect:

1. **Generation roll.** The parent's import graph is
   generation-independent by construction (it imports the *framework*,
   never a snapshot). If that ever stops being true, the parent is
   recycled per generation rather than per unit.
2. **RSS ceiling.** A parent whose RSS has grown past a multiple of
   its post-import baseline is leaking and is replaced between units.
3. **Fork failure or a poisoned parent.** Any `OSError` from `fork()`,
   or a parent that fails its own liveness reply, retires it.

Recycling is always *between* units, never during one, so no in-flight
unit is ever affected.

**Failure modes and their handling.**

| Failure | Consequence | Handling |
|---|---|---|
| Pool parent dies with units in flight | The forked children are `setsid`-detached, so they are **not** killed with it — they keep running, keep heart-beating, and their `active_runs` records stay valid and killable by the supervisor. What is lost is the completion notification. | The orchestrator falls back to the shipped `create_subprocess_exec` path for new units, and the existing "process gone / no result file" reap (§5.3) settles any child whose notification never arrived. Degrading to today's behaviour must always be one branch away. |
| Pool parent wedges (alive, not answering) | New units cannot start. | Liveness deadline on the request/reply; on expiry the parent is retired and the unit spawns cold. |
| `fork()` fails (fd/pid/memory exhaustion) | Unit cannot start. | Spawn cold for that unit; count the failure toward the recycle trigger. |
| Fork-safety probe red after a dependency bump | The pool would be *silently* unsafe. | CI fails; the pool is disabled by default until re-adjudicated. This is why the probe is a gate, not a warning. |
| Orchestrator killed | Pool parent is orphaned. | The parent must exit when its socket peer closes, and — like the ephemeral `ztw-snap-*` trees — be GC-able by the supervisor. |

#### 5.5.6 The measurement plan that gates the build

The pool is a real complexity increase against a shipped, working,
simple design. It is only worth building if the win is large **at the
round level**, not just at the import level. The gate:

**Harness.** Cold-start phase measurement — each phase in a fresh
interpreter, best of *N* runs, reporting `perf_counter` around the
imports, `resource.getrusage(RUSAGE_SELF).ru_maxrss` for peak RSS,
and `len(sys.modules)`. This is the method that produced §5.5.1 and
it is what the pool must be measured against; the numbers in §5.5.1
are the committed **before** baseline.

**Gate 1 — the tax is a material share of a round.** Instrument the
existing `spawn_started` clock in `_run_single` to record, per unit,
the interval from spawn to the worker's first `active_runs` write
(≈ pure startup) alongside total unit runtime. The pool is worth
building only if startup is **≥ 15 % of median unit runtime** on a
representative board. If units are dominated by multi-second LLM
latency, a 2.4 s startup is noise and the pool is not worth its
failure modes — say so and close the issue.

**Gate 2 — peak RSS is actually a constraint.** Record peak
orchestrator-tree RSS across a round at the operator's real
`parallelism`. The pool's memory win is COW-sharing the ~246 MB
graph; if the box never approached its ceiling, this is not a
motivating win either.

**Gate 3 — the fork-safety probe is green** (§5.5.4), as a CI test.

**Gate 4 — killability is preserved, proven by test, not by
argument.** The existing supervisor-kill tests must pass **unchanged**
against the pool transport, plus one new adversarial test: with two
units in flight through one pool parent, kill unit *A*'s pgid and
assert that unit *B* completes normally and that *A*'s abort is
recorded with the same `abort_cause` the cold-spawn path produces.

**Gate 5 — no verdict moves.** `tools/parity.sh` fully green: a
transport change must not move a single scored artifact.

Only with 1–5 green does the pool get built, behind a default-off
runtime knob, with the cold-spawn path retained as the fallback
branch.

#### 5.5.7 The fallback: a host-wide spawn limiter (SHIPPED)

If the fork-safety adjudication ever goes red, the pool is off the
table and the remaining lever is to stop *over*-spawning. That lever
is worth having regardless of the pool, because of a gap the
per-orchestrator semaphore cannot close:

**`parallelism` bounds one orchestrator, and nothing bounds the
host.** Each orchestrator mints its own
`Semaphore(config.parallelism)` (§5.5.2's second row is about that
semaphore working correctly *within* a run). Two concurrent `evolve`
runs on one box therefore admit `2 × parallelism` board units — i.e.
up to `4 × parallelism` workers in full mode — and each worker's
246 MB is real. Nothing in the tree noticed.

The shipped limiter (`zicato.runtime.spawn_permit`) is a **host-wide
permit** held for a worker's lifetime:

* *N* slot files under a **workspace-external** directory
  (`$ZICATO_WORKER_PERMIT_DIR`, else `$XDG_RUNTIME_DIR/zicato/...`,
  else a per-uid path under the system temp dir) — external on
  purpose, so the cap spans workspaces and orchestrators;
* a permit is `fcntl.flock(LOCK_EX | LOCK_NB)` on the first free
  slot; when every slot is held, the acquirer polls with jitter on
  the event loop (never a blocking `flock`, so the orchestrator's
  loop is never parked);
* **`flock` releases on process death**, by the kernel. A crashed
  orchestrator cannot leak a permit, so there is no stale-lock
  reaper to write and no liveness protocol to get wrong. This is the
  reason for `flock` over a counter file.

  Note this is the *opposite* choice from the workspace lock (§2.1),
  which deliberately rejected `flock` because it "is released
  invisibly on process death" and leaves no human-readable owner.
  Both choices are right for their problem. The workspace lock is
  about **identity** — an operator needs to know *which* process owns
  the epoch, and a stale owner is a decision to surface, not to
  silently reclaim. A permit is about **counting**: nobody ever needs
  to know who holds slot 3, and invisible release on death is exactly
  the property that makes a permit unleakable.
* **it degrades OPEN.** Any failure to create the directory, open a
  slot, or use `flock` (an unsupported filesystem, a read-only
  runtime dir, a platform without `fcntl`) yields a permit that
  admits immediately. An infrastructure problem in a throttle must
  never be able to block a run.

The knob is `runtime.host_worker_permits`
(`RuntimeConfig.host_worker_permits`) — a **runtime** knob, never
part of the frozen evaluation contract, so changing it does not roll
the epoch. `null` (the default) means AUTO: `max(4, 2 × cores)`,
deliberately generous enough that a single normal run never waits;
`0` disables the cap entirely; `≥ 1` is an explicit ceiling.

The limiter is a **throttle, not a speed-up** — it makes an
over-subscribed host degrade into queueing rather than into swapping.
It does not reduce the per-unit tax; only the pool does that.

**Which clocks the queue wait is charged to.** The permit is taken
*before* `spawn_started` is stamped, so a wait never inflates the unit's
reported `runtime_ms` — and it is invisible to the per-entry
`wall_clock_budget_seconds`, which the worker enforces on itself only
after it starts. But the wait *is* real time inside the round, so it IS
charged against an evolve invocation's `max_wall_clock_seconds` (the
`asyncio.wait_for` in `evolve/loop.py`) and against an opt-in
`matchup_budget_seconds` deadline. Under heavy contention with both a
tight explicit cap and a total budget set, a healthy round can therefore
be cancelled as `wall_clock_budget_mid_round` while every unit's recorded
runtime sums to well under the budget — accounting an operator cannot
reconcile from the artifacts. This is why a wait logs its **duration** at
INFO: the log line is the only place the missing time appears. The
supervisor's reaper is unaffected either way — it declares an
orchestrator dead only on confirmed pid death, never on staleness, so a
queueing run is never reaped.

Neither budget can deadlock against the permit: nothing held while
waiting for a permit is needed to release one. `parallelism`'s semaphore
slot IS held across the wait, so an orchestrator whose units are all
queued makes no progress until a slot frees — queueing, which is the
intent.

#### 5.5.8 Lazy role resolution (SHIPPED), and one refuted micro-fix

A second cheap win, and a correction to a claim worth recording.

**Refuted: there is no eager `litellm` import to remove.** The
expectation was that the worker imports `litellm` at startup. It does
not — `google.adk.models.lite_llm` calls its own
`_ensure_litellm_imported()` from inside
`LiteLLMClient.acompletion` / `.completion`, i.e. **at the first LLM
call**, and no module in zicato, goldfive, or ADK imports `litellm`
at module scope. Making it "lazy" is therefore a no-op: it is
already lazy. The consequence is worth stating precisely, because it
is *worse* than an eager import, not better: the +1.06 s / +126 MB is
paid **inside the unit's wall-clock budget**, by every worker, and
concurrently across all of them (§5.5.1). Only the pool removes it.

**Shipped: the worker no longer imports `google.adk` for a role it
may never call.** `_tournament_worker` resolved *every* configured
model-spec role (harness, auxiliary, judge) eagerly at startup, and
`build_adk_model` pulls the whole ADK graph — 0.80 s / 88 MB — the
first time any of them is touched. A unit whose entry has no LLM
judge, or which never reaches the auxiliary side, paid for ADK
anyway. Model-spec roles now resolve through
`models_config.lazy_text_call_llm`, which validates the spec shape
**eagerly** (so a malformed `models` block still fails fast, at
startup, where it is debuggable) and defers only the ADK import and
`LiteLlm` construction to the role's first call. Roles given as a
`call_llm` dotted path are unaffected — they never touched ADK.

Measured before/after, same harness, cold start, best of five:

| Worker reaches | s | RSS MB | `sys.modules` |
|---|---|---|---|
| worker only, no role resolved | 0.094 | 31 | 328 |
| **before** — eager `resolve_text_call_llm` | 0.881 | 120 | 1657 |
| **after** — `lazy_text_call_llm`, role never called | 0.080 | 32 | 329 |

The saving is conditional by nature: a unit that does call the role
pays the same cost, just later. It is **0.80 s / 88 MB / 1328 modules**
per worker for every role a unit never exercises, and `0` otherwise —
never a regression. (Re-measured on a different box: the module count
and peak RSS reproduce exactly — +1329 modules, 44 MB → 121 MB — while
the wall time was 2.1 s, not 0.80 s. Trust the *shape* of the table; the
seconds column is machine-specific.)

**The bound on that saving, stated honestly.** It does NOT apply to the
harness role whenever that role's spec sets `endpoint` or `api_key_env`,
because the very next line of the worker calls
`_resolve_inner_model_from_role(args["harness_role"])` — the inner-model
build that lets the target's agents use native function calling instead
of the text shim — and that reaches `build_adk_model` eagerly. In the
endpoint-configured shape (the one live validation uses) ADK is
therefore resident at worker startup regardless, and deferring the
*call_llm* resolution saves nothing on top. The saving is real for a
bare `model`-only spec (where `build_adk_model` returns the model string
and imports nothing) and for the auxiliary / judge roles on a worker
whose harness role is a dotted `call_llm`. Making the inner model lazy
too is not a wrapper away — the adapter rebinds agent trees to a model
*object* at setup — so it is the remaining half of this win.

**Deferral must not turn a config fault into a silent score.** A spec
that validates but cannot RESOLVE (the `adk` extra absent, a `model` id
ADK cannot resolve) used to fail at worker startup, exiting non-zero, so
the parent recorded an infra abort. Deferred, that failure surfaces at
the role's first call — and if that first call is a judge's it is
*swallowed*: `_InlineCriterionJudge` and goldfive's
`DefaultSteerer.evaluate_judges` both catch and log, by hard contract,
because a misbehaving judge must not crash a run. The unit would
otherwise complete with the judge reporting "no signal" at every
observation point: drift undercounted, the scalar better than the truth,
a crowning decided on a judge that never ran. So `lazy_text_call_llm`
records each deferred resolution failure in a process-wide register
(`models_config.deferred_role_failures`) and raises a distinguishable
`RoleResolutionError`, and `_tournament_worker.main` turns a non-empty
register into a non-zero exit — restoring exactly the outcome the eager
path produced.

The durable part of the change is the regression test that pins the
posture: importing `zicato._tournament_worker` must not pull
`google.adk` or `litellm` into `sys.modules`, so a future eager import
at module scope fails CI instead of quietly taxing every board unit.

## 6. Atomic write helper

Every write to a state file goes through one helper. Reads always
see either the previous content or the new content — never a
partial write. The shipped helpers live in `zicato.storage._atomic`
(`atomic_write_json`, `atomic_write_text`, `read_json`) and the
`runtime` package consumes them through that storage seam (a backward
compatible shim re-exports them from `zicato.runtime._atomic`). The
shape is the classic write-temp-then-rename:

```python
# zicato.storage._atomic (paraphrased)
def atomic_write_text(path: pathlib.Path, content: str) -> None:
    """Write `content` to `path` atomically.

    Writes to a sibling `*.tmp` first, then renames into place.
    `rename(2)` on the same filesystem is atomic; readers always see
    the old file or the new file, never a half-written one.
    """
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(content)
    tmp.replace(path)
```

These helpers back `heartbeat.json`, `active_tournament.json`,
each `active_runs/{run_id}.json`, the control-file writes (the
dashboard's POST handlers do the same temp-then-rename), and every
other JSON/text file in `.zicato/`. The exception is `events.jsonl`,
which is append-only and uses goldfive's `JSONLPersistenceSink`
flush protocol (one event per line, line-terminated, flushed per
event — see [TELEMETRY.md](TELEMETRY.md)).

## 7. Observability of the runtime layer itself

The watchdog supervisor logs via `tracing` (level set by `--log` /
`RUST_LOG`; stdio inherited from `evolve`) and exposes its own state on
`/statusz` (and `/statusz.json`). The orchestrator logs via Python's
`logging`. What surfaces the runtime state today:

| Surface | What it shows | Ships? |
|---|---|---|
| watchdog `/statusz` | The watchdog's own view: heartbeat age, per-run staleness, recent escalations. | yes |
| dashboard `GET /api/state` | Composite live snapshot — heartbeat, active tournament, active runs, lineage — plus the rest of the API in [DASHBOARD.md](DASHBOARD.md) §6. | yes |
| `zicato status` one-shot CLI | A filesystem-only snapshot that needs no running supervisor. | **planned** — not in the shipped CLI |
| `zicato kill <run_id>` CLI | Write `control/kill_runs/{run_id}` and wait for consumption. | **planned** — use the dashboard's kill control instead |

The dashboard's `GET /api/*` endpoints read the state files directly,
so the operator can inspect even a half-broken setup ("did the
watchdog die — is the loop still going?") without the watchdog
running.

## 8. Phasing

The runtime layer ships in stages. See
[ROBUSTNESS.md](ROBUSTNESS.md) §4 for the layered defense
mapping; this section is the runtime-layer-specific phasing.

| Phase | What lands |
|---|---|
| **v1** | `.zicato/runtime/lock.json`, `heartbeat.json` (live, written by `HeartbeatBeater`), `asyncio.wait_for` per-call timeouts. **Shipped today.** |
| **v1.1** | Full `.zicato/runtime/` layout + atomic writes. Rust watchdog supervisor (watchdog role only) auto-spawned by `evolve`. SIGTERM → grace → SIGKILL escalation. **Subprocess tournament workers (L3) with hard per-run wall-clock budgets are shipped** (`_tournament_worker.py`). The resume protocol and `zicato status` / `zicato kill` remain **planned**. |
| **v1.2** | Live dashboard, served as a **separate Python service** (HTTP + SSE), auto-spawned by `evolve`. **Shipped today** (the dashboard split out of the Rust binary into its own service). |
| **v1.3** | Interactive dashboard controls. The dashboard's POST control endpoints and the control-file *write* side are **shipped**; the orchestrator's *consumption* of `control/` at safe points and the `control_log/` audit are **planned**. |

The split is deliberate. The watchdog + atomic-write safety work is the
production-readiness pass; the dashboard is a thick layer on top and
got its own phase so the runtime safety work could ship and bake
without being blocked on UI work.

## 9. Cross-references

| Topic | Document |
|---|---|
| The layered defense model and what each layer catches | [ROBUSTNESS.md](ROBUSTNESS.md) |
| The dashboard's panels, endpoints, and control protocol | [DASHBOARD.md](DASHBOARD.md) |
| Per-run events.jsonl and JSONLPersistenceSink details | [TELEMETRY.md](TELEMETRY.md) |
| Generation directory layout (where runs/, experiment.json, etc. live) | [EPOCHS-AND-JOURNALING.md](EPOCHS-AND-JOURNALING.md) |
| Storage roadmap for git-backed workspaces | [STORAGE.md](STORAGE.md) |
| Why the watchdog is a separate language, not in-process | [RATIONALE.md](RATIONALE.md) |
