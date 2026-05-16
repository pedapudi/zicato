# Runtime

This document describes the **runtime layer** that surrounds zicato's
meta-loop: the on-disk state files in `.zicato/runtime/`, the
supervisor binary auto-spawned by `zicato evolve`, the heartbeat and
escalation protocols, the resume semantics on orchestrator restart,
and the concurrency model that lets parallel tournaments coexist on
one workspace.

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
3. **Single static binary for the watchdog + dashboard.** The
   supervisor is a Rust binary, not a Python process. The
   orchestrator (Python) is the thing prone to GIL wedges; making
   the watchdog a separate language with a separate runtime is the
   cheapest way to make sure the watchdog cannot itself be the cause
   of the failure it's there to catch.
4. **One mental model for the operator.** `zicato evolve` auto-spawns
   the supervisor and prints the dashboard URL. The operator never
   has to remember "did I start the dashboard?" for the common case.

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
├── control_log/                    # consumed commands persist here for audit
│   ├── 2026-05-14T12:34:50Z_pause_epoch.json
│   └── ...
├── supervisor.pid                  # supervisor's PID, written on spawn
└── supervisor.stdout / .stderr     # supervisor's redirected output
```

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
  "evolve_args": ["--rounds", "10", "--mode", "tournament"],
  "hostname": "workstation.local"
}
```

**Acquisition.** Python's `fcntl.flock(LOCK_EX | LOCK_NB)` on the
file descriptor. The lock is held for the lifetime of the
orchestrator process; the file is removed (best-effort) on clean
exit. The supervisor does NOT acquire the lock — there's only one
orchestrator, but the supervisor reads it to know whose heartbeat
it's watching.

**Stale lock handling.** If `lock.json` exists but the named PID is
not alive, the new orchestrator considers the lock stale and steals
it (logging a warning that names the old PID and the staleness
delta). The check is `kill(pid, 0)` — cheap, no signal actually
delivered. This recovers automatically from kernel-level kills,
host reboots, and any case where the orchestrator died without
clean exit.

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
  "ts": "2026-05-14T12:35:02.418Z",
  "phase": "tournament",
  "epoch": "hardened_research",
  "round": 4,
  "candidate": "v5",
  "parent": "v4",
  "active_runs": ["e4f2_short_solar", "e4f2_long_solar_with_constraints"],
  "active_run_count": 2,
  "tournament_progress": {
    "completed": 6,
    "total": 10,
    "in_flight": 2
  },
  "evolve_started_at": "2026-05-14T12:34:50.123Z"
}
```

**Atomicity.** Written via `write_atomic(path, json.dumps(payload))`
— that is, write to `heartbeat.json.tmp`, `fsync`, then `rename`.
Readers (the supervisor, `zicato status`) always see either the old
or the new content, never a partial write. This matters because the
supervisor polls aggressively (inotify trigger on rename) and a
partial read would be a false positive for "orchestrator went
silent".

**Cadence.** A heartbeat is written:

- Every 2s on a timer (the floor — guarantees no false wedge
  detection during long-but-progressing work).
- On every phase transition (`proposing → applying → running →
  tournament → journaling → ...`) — captured fresh on each step.
- On every `active_runs` change (a new run starts, an existing run
  finishes) — so the supervisor sees the population delta within
  milliseconds, not seconds.

**Staleness threshold.** The supervisor considers the orchestrator
wedged when `now - heartbeat.ts > STALE_GRACE_SECONDS` (default
15s, configurable). 15s = 7-8 expected heartbeat intervals; tight
enough to catch a real wedge, loose enough to absorb a slow disk
sync or a paused-by-debugger orchestrator without false alarms.

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

For every tournament run currently executing, one file. Written by
the subprocess worker that owns the run (NOT by the orchestrator —
the orchestrator spawns the worker, the worker writes its own
status while it runs). This is what makes detection robust against
orchestrator-side wedges: the worker keeps writing even if the
orchestrator hangs.

```json
{
  "run_id": "e4f2_short_solar_candidate",
  "generation": "v5",
  "side": "candidate",
  "entry_id": "short_solar",
  "started_at": "2026-05-14T12:35:00.000Z",
  "pid": 84522,
  "phase": "agent_running",
  "phase_started_at": "2026-05-14T12:35:01.250Z",
  "wall_clock_budget_seconds": 120,
  "wall_clock_deadline": "2026-05-14T12:37:00.000Z",
  "events_file": ".zicato/epochs/hardened_research/generations/v5/runs/short_solar/events.jsonl",
  "last_event_seen_at": "2026-05-14T12:35:04.800Z",
  "last_event_kind": "GoldfiveLLMCallStart",
  "drift_count_so_far": 1,
  "heartbeat_at": "2026-05-14T12:35:05.000Z"
}
```

**Phase values.** `spawning | adapter_init | agent_running |
adapter_terminating | done | aborted | killed`. The narrower
phases let the dashboard show what the run is actually doing
without the operator drilling into the events stream.

**`heartbeat_at` cadence.** The worker bumps its own heartbeat
every 1s while running. The supervisor sees a stalled worker (no
heartbeat bump for >10s, but the entry still says `running`) as a
candidate for SIGTERM independently of whether the orchestrator
itself is healthy.

**File lifecycle.**

- Created when the worker is spawned (orchestrator does the
  `Popen`; worker's first action is writing this file).
- Updated by the worker on phase transitions and on the 1s
  heartbeat timer.
- Removed by the worker on clean exit, OR removed by the
  orchestrator if the worker exited without cleaning up (the
  orchestrator reaps zombies and ensures the file matches actual
  process state).

**Why a separate file per run, not one big `active_runs.json`.**
Concurrent writes. The orchestrator may launch a fresh worker
while another worker is updating its own status. One-file-per-run
means no inter-worker write contention; each worker is the sole
writer of its own file.

### 2.5 `control/` and `control_log/` — operator action channel

`control/` is where the dashboard writes operator commands. The
orchestrator reads `control/` at **safe points only** — between
board entries, between rounds, between epoch lifecycle stages —
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
| `control/proposer_brief_replacement.txt` | "edit proposer brief" panel | text file; contents replace `proposer_brief.md` |

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

## 3. The supervisor binary

`zicato-supervisor` is a single statically-linked Rust binary.
Auto-spawned by `zicato evolve` (opt out with `--no-dashboard`);
killed when `evolve` exits. One binary, two roles running in the
same process:

- **Watchdog**: watches `.zicato/runtime/*` for stale heartbeats
  and stalled runs; escalates SIGTERM → grace → SIGKILL.
- **Dashboard server**: serves HTTP + Server-Sent Events on a
  local port (default `:7892`, +1 if taken).

Both roles share one inotify (or FSEvents on macOS) watcher on
`.zicato/runtime/`. Filesystem events feed both the watchdog logic
and the SSE event stream.

### 3.1 Lifecycle

```
┌─────────────────────────────────────────────────────────────────┐
│  zicato evolve (Python orchestrator process)                    │
│  ─────────────────────────────────────────                      │
│  1. Acquire .zicato/runtime/lock.json (LOCK_EX).                │
│  2. Write initial heartbeat.json.                               │
│  3. Spawn .zicato/runtime/supervisor                            │
│     via Popen([zicato-supervisor, --workspace, .zicato/]).      │
│     Stash PID in supervisor.pid.                                │
│  4. Run the meta-loop (rounds 1..N).                            │
│  5. On clean exit: send SIGTERM to supervisor, wait up to 5s,   │
│     SIGKILL if still alive. Remove lock.json.                   │
└─────────────────────────────────────────────────────────────────┘
                          │ spawns
                          ▼
┌─────────────────────────────────────────────────────────────────┐
│  zicato-supervisor (Rust process)                               │
│  ─────────────────────────                                      │
│  1. Read lock.json; record orchestrator PID.                    │
│  2. Open inotify watch on .zicato/runtime/.                     │
│  3. Bind HTTP listener on --port (default 7892, +1 if taken).   │
│  4. Print URL to stderr (orchestrator captures, re-prints).     │
│  5. Loop:                                                       │
│     - Drain inotify events; update in-memory snapshot.          │
│     - Broadcast to SSE subscribers.                             │
│     - On 1s timer: check heartbeat staleness, run staleness.    │
│     - On staleness > threshold: escalate (see §3.3).            │
│  6. On SIGTERM: drain in-flight HTTP responses, exit cleanly.   │
└─────────────────────────────────────────────────────────────────┘
```

The supervisor is **strictly downstream of the orchestrator**.
The orchestrator decides what to do; the supervisor watches it do
it and yells when it stops. The one exception is the SIGTERM /
SIGKILL escalation — the supervisor can kill a stuck worker
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
     │     ╳  (orchestrator wedges)         │  1s tick: now - last_seen = 5s   OK
     │                                      │  1s tick: now - last_seen = 10s  OK
     │                                      │  1s tick: now - last_seen = 16s  STALE
     │                                      │
     │                                      │  -> log "orchestrator stalled"
     │                                      │  -> set state.stalled = true
     │                                      │  -> broadcast SSE event
     │                                      │
     │                                      │  (supervisor does NOT kill the
     │                                      │   orchestrator. The orchestrator
     │                                      │   may simply be slow. Killing it
     │                                      │   is an operator decision.)
```

**The supervisor does NOT kill the orchestrator on heartbeat
staleness.** The orchestrator might be slow for legitimate reasons
(GC pause, slow LLM endpoint, paused by debugger). The dashboard
surfaces "orchestrator looks stalled" and the operator decides.
The supervisor only kills *workers* automatically — see §3.3.

If the operator wants automatic orchestrator restart, that's a
process-supervisor concern (systemd, supervisord, k8s); not
zicato's job to reinvent.

### 3.3 Escalation (SIGTERM → grace → SIGKILL)

For each `active_runs/{run_id}.json`, the supervisor evaluates two
deadlines:

- `wall_clock_deadline` (set when the worker started).
- `heartbeat_at + WORKER_STALE_GRACE_SECONDS` (default 10s).

When EITHER deadline passes:

```
  t=0       worker is past wall-clock OR has not heartbeat'd for 10s
            │
            ▼
  t=0       supervisor logs "escalating run {run_id}"
            │ writes .zicato/runtime/escalations/{run_id}.json with reason
            │
            ▼
  t=0       supervisor sends SIGTERM to worker PID
            │
            │ waits ESCALATION_GRACE_SECONDS (default 10s)
            │
            ▼
  t=10s     worker has cooperated → cleanup → done
            OR
  t=10s     worker has not cooperated → SIGKILL
            │
            ▼
  t=10s     supervisor reaps the worker (sends signal 0; on ESRCH, dead)
            │ Worker's active_runs/{run_id}.json is left for the
            │ orchestrator to clean up via its own watchdog routine
            │ (the orchestrator notices a worker is dead and finalises
            │ the run as `killed`).
```

**Why two-stage SIGTERM → SIGKILL.** SIGTERM gives the worker a
chance to flush its goldfive event sink (so the events.jsonl is
not truncated mid-event), update its `active_runs/{run_id}.json`
to `killed`, and exit cleanly. SIGKILL is uninterruptible — the
process is gone immediately and we lose the last few events.
Always preferring SIGKILL would corrupt the JSONL on every
escalation; always preferring SIGTERM would leave a truly wedged
worker hanging forever.

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
| `lock.json` | orchestrator | supervisor, `zicato status` |
| `heartbeat.json` | orchestrator | supervisor, `zicato status`, dashboard |
| `active_tournament.json` | orchestrator | supervisor, dashboard |
| `active_runs/{run_id}.json` | the **worker** that owns `run_id` | orchestrator, supervisor, dashboard |
| `control/<command>` | dashboard server | orchestrator (consumer) |
| `control_log/*` | orchestrator (writes on consume) | dashboard, `zicato status` |
| `supervisor.pid` | supervisor | orchestrator, `zicato status` |

There are no shared writers. Every file has exactly one process
that writes to it; concurrent readers are safe because every write
is atomic-rename. No `fcntl` locks beyond `lock.json`; no shared
in-memory mutable state.

**This is the load-bearing invariant for the design.** Locking
correctness in a multi-process system is hard. By making every
file single-writer, we get correctness for free at the cost of
some redundancy (e.g. the orchestrator updates
`active_tournament.json` even though the workers update their own
files — the orchestrator's view is a join that the dashboard reads
from one place).

**Workers writing their own files: why this is safe.** A worker
ONLY writes `active_runs/{run_id}.json` for the run it owns.
Workers do not share files. The orchestrator may DELETE a worker's
file (when reaping a dead worker), but the orchestrator does not
write to a file a live worker owns. The only race window is "worker
just deleted its own file because it finished, orchestrator reads
the file expecting it to still exist" — and the orchestrator
handles ENOENT as a normal terminal state, not an error.

## 4. Resume semantics

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

## 5. Worker subprocesses

The orchestrator runs each tournament run in a **subprocess
worker** (Python, spawned via `multiprocessing` or
`subprocess.Popen` depending on platform — see
[ROBUSTNESS.md](ROBUSTNESS.md) §L3 for the choice). One worker per
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

A worker is invoked as a CLI subcommand:

```
zicato _worker
    --run-id <run_id>
    --generation <v_N>
    --entry-id <entry_id>
    --side {parent|candidate}
    --runtime-dir <.zicato/runtime/>
    --workspace <.zicato/>
```

The worker's first action is writing `active_runs/{run_id}.json`
with its own PID. The worker then:

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
git-backed roadmap.

## 6. Atomic write helper

Every write to a state file goes through one helper. Reads always
see either the previous content or the new content — never a
partial write.

```python
def write_atomic(path: pathlib.Path, content: bytes) -> None:
    """Write `content` to `path` atomically.

    Writes to `path.tmp` first, fsyncs the fd, then renames into place.
    `rename(2)` on the same filesystem is atomic; readers always see
    the old file or the new file, never a half-written one.
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("wb") as f:
        f.write(content)
        f.flush()
        os.fsync(f.fileno())
    tmp.rename(path)
```

This helper is used for `heartbeat.json`, `active_tournament.json`,
each `active_runs/{run_id}.json`, `experiment.json` updates, and
every other file in `.zicato/`. The exception is `events.jsonl`,
which is append-only and uses goldfive's `JSONLPersistenceSink`
flush protocol (one event per line, line-terminated, flushed per
event — see [TELEMETRY.md](TELEMETRY.md)).

## 7. Observability of the runtime layer itself

The runtime layer produces its own logs at
`.zicato/runtime/supervisor.stderr` (Rust supervisor) and via
Python's `logging` module (orchestrator). Three operator commands
surface the runtime state:

| Command | What it shows |
|---|---|
| `zicato status` | One-shot snapshot: lock holder, heartbeat age, active tournament, active runs, dashboard URL. See [CLI.md](CLI.md) §3.14. |
| `zicato kill <run_id>` | Write `control/kill_runs/{run_id}` then wait for the orchestrator to consume it (with a `--timeout` flag). See [CLI.md](CLI.md) §3.15. |
| dashboard `GET /api/state` | The same snapshot as `zicato status` plus per-panel data. See [DASHBOARD.md](DASHBOARD.md) §6. |

`zicato status` does NOT need the supervisor to be running — it
reads the state files directly. This means the operator can debug
even a half-broken setup ("the supervisor crashed, is the loop
still going?") with a fast filesystem read.

## 8. Phasing

The runtime layer ships in stages. See
[ROBUSTNESS.md](ROBUSTNESS.md) §4 for the layered defense
mapping; this section is the runtime-layer-specific phasing.

| Phase | What lands |
|---|---|
| **v1** | `.zicato/runtime/lock.json`, heartbeat.json (informational only — no supervisor yet), `zicato status` reads state files. No supervisor binary; `asyncio.wait_for` per-call timeouts. |
| **v1.1** | Full `.zicato/runtime/` layout. Rust supervisor binary (watchdog role only; no dashboard yet). Subprocess workers. Atomic writes everywhere. Resume protocol. `zicato kill` command. |
| **v1.2** | Supervisor's dashboard role added (HTTP + SSE, read-only). Auto-spawn from `zicato evolve`. |
| **v1.3** | Interactive dashboard controls via the `control/` file protocol. `control_log/` audit. |

The split is deliberate. v1.1 is the production-readiness pass —
subprocess isolation + watchdog + resume — and ships without the
dashboard. The dashboard is a thick layer on top and gets its
own phase so the runtime safety work can ship and bake without
being blocked on UI work.

## 9. Cross-references

| Topic | Document |
|---|---|
| The layered defense model and what each layer catches | [ROBUSTNESS.md](ROBUSTNESS.md) |
| The dashboard's panels, endpoints, and control protocol | [DASHBOARD.md](DASHBOARD.md) |
| Per-run events.jsonl and JSONLPersistenceSink details | [TELEMETRY.md](TELEMETRY.md) |
| Generation directory layout (where runs/, experiment.json, etc. live) | [EPOCHS-AND-JOURNALING.md](EPOCHS-AND-JOURNALING.md) |
| Storage roadmap for git-backed workspaces | [STORAGE.md](STORAGE.md) |
| CLI surface for `zicato status` and `zicato kill` | [CLI.md](CLI.md) |
| Why the supervisor is a separate language, not in-process | [RATIONALE.md](RATIONALE.md) |
