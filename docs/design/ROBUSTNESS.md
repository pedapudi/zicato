# Robustness

This document specifies the six-layer defense model that makes
zicato survive the pathological cases — network hangs, infinite
Python loops, CPU-bound C extensions, orchestrator crashes, OOM
kills. Each layer catches a different class of failure; they
stack because no single layer covers all of them.

The meta-loop in [ARCHITECTURE.md](ARCHITECTURE.md) is correct
against cooperative inner harnesses. This document is what makes
it correct against everything else.

## 1. The six layers

| Layer | Defense | Catches | Status today |
|---|---|---|---|
| **L1** | `asyncio.wait_for` per-call + per-entry + per-evolve budgets | Cooperative network/IO hangs | **shipped** |
| **L2** | structured cancellation (CancelledError) | Async code that yields | **shipped** |
| **L3** | subprocess tournament workers | GIL-holding loops, any user-code pathology | **shipped** — every board-entry run executes in its own `python -m zicato._tournament_worker` process, with a hard per-run wall-clock budget |
| **L4** | orchestrator watchdog (Rust supervisor) | Parent-side wedges | **shipped** (watchdog binary auto-spawned; escalation targets the per-run worker process) |
| **L5** | consecutive-bad circuit breaker | Long unproductive epochs | **shipped** (consecutive-reject counter); richer signals planned |
| **L6** | atomic writes (+ resume markers) | Mid-run crashes | atomic writes **shipped**; the resume protocol **planned** |

The layers nest from inside to outside: L1 and L2 live inside the
run, L3 is the per-run worker process boundary, L4 is the supervisor
process outside the runs, L5 is the loop-level shutoff, L6 is the
on-disk durability story.

> **What this means in practice.** zicato is robust against
> *cooperative* inner harnesses (L1+L2), isolates each run in its own
> OS process so even an uncooperative harness (a GIL-holding C
> extension, a `while True: pass`) is hard-killed at a per-run boundary
> under a wall-clock budget (**L3 shipped** —
> `src/zicato/_tournament_worker.py`), durably writes its state
> (L6 atomic writes), runs an external watchdog that escalates a stalled
> run by SIGTERM → SIGKILL on that run's own worker PID (L4), and stops
> a fruitlessly-rejecting loop (L5). The remaining production-hardening
> gap is the **resume protocol** that makes a post-kill restart pick up
> cleanly (planned, see [RUNTIME.md](RUNTIME.md) §4).

```
┌─────────────────────────────────────────────────────────────────┐
│  L6: atomic writes + resume markers                              │
│  ┌─────────────────────────────────────────────────────────────┐ │
│  │  L5: consecutive-bad circuit breaker (loop level)            │ │
│  │  ┌────────────────────────────────────────────────────────┐  │ │
│  │  │  L4: Rust supervisor (watchdog over orchestrator)       │  │ │
│  │  │  ┌──────────────────────────────────────────────────┐    │ │ │
│  │  │  │  L3: subprocess worker boundary                    │    │ │ │
│  │  │  │  ┌────────────────────────────────────────────┐    │    │ │ │
│  │  │  │  │  L2: structured cancellation                │    │    │ │ │
│  │  │  │  │  ┌──────────────────────────────────────┐    │    │    │ │ │
│  │  │  │  │  │  L1: asyncio.wait_for per call         │    │    │    │ │ │
│  │  │  │  │  └──────────────────────────────────────┘    │    │    │ │ │
│  │  │  │  └────────────────────────────────────────────┘    │    │ │ │
│  │  │  └──────────────────────────────────────────────────┘    │ │ │
│  │  └────────────────────────────────────────────────────────┘  │ │
│  └─────────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────┘
```

A failure caught by L1 never reaches L2. A failure that bypasses
L1 and L2 hits L3 (the process boundary). The outer layers exist
because the inner layers can fail in well-understood ways.

## 2. Layer-by-layer

### 2.1 L1 — `asyncio.wait_for` per-call timeouts

Every `await`-able call inside zicato that can hang gets wrapped
in `asyncio.wait_for(coro, timeout=...)`. Wrappers exist for the
two `call_llm` callables and for any adapter-level IO.

```python
async def aux_call_llm_with_budget(
    system: str,
    user: str,
    model: str,
    budget_s: float = 120.0,
) -> str:
    try:
        return await asyncio.wait_for(
            auxiliary_call_llm(system=system, user=user, model=model),
            timeout=budget_s,
        )
    except asyncio.TimeoutError as e:
        raise ZicatoAuxLLMTimeout(model=model, budget_s=budget_s) from e
```

**What it catches.** Network-level hangs in well-behaved clients
that respect `asyncio.CancelledError` — a TCP connect that
hangs, a sluggish LLM endpoint that streams the first token then
stops, an HTTP read that the underlying client implements with
asyncio primitives.

**What it doesn't catch.** Anything where the timeout exception
fires inside the event loop but the underlying work doesn't
actually stop. This includes blocking IO inside a `to_thread`
call (the thread keeps going), CPU-bound work in a coroutine
(the event loop never gets to schedule the exception), and bugs
where the await point doesn't actually surrender the loop.

L1 is the **cheapest** defense; everything inside zicato uses it.
But it's also the **weakest**: an L1-only defense is a
"cooperative termination only" guarantee. Production needs more.

#### Layered wall-clock budgets

L1 budgets are applied at two nested granularities, and **both
apply at once** — the inner ceiling does not replace the outer one.

* **Per-entry budget.** Each `BoardEntry` carries its own
  `wall_clock_budget_seconds`, threaded into the run's worker args file.
  As shipped (L3, §2.3) the per-run subprocess **worker** enforces it —
  it self-aborts at the deadline (`asyncio.wait_for` *inside* the
  worker) and the L4 supervisor backstops by SIGKILLing the worker PID
  if it wedges — so a run that overruns is aborted and scored worst-case
  (`abort_reason="wall_clock_budget"`) even against an uncooperative
  harness. This bounds *one* run, hard. The `racing` structure can also
  tighten this per duel via `matchup_budget_seconds` /
  `final_rung_budget_seconds` (the grind guard,
  [TOURNAMENT-STRUCTURES.md §3.5](TOURNAMENT-STRUCTURES.md#35-racing-the-endorsed-bracket-shaped-option)),
  which ride on the `Matchup` and become the worker's board-unit budget.

* **Per-evolve total budget.** `evolve_n_rounds` accepts an optional
  `max_wall_clock_seconds` ceiling on the *whole* `zicato evolve`
  invocation (`None` — the default — leaves the loop unbounded). It
  bounds the *aggregate*: an invocation of N entries × M rounds could
  otherwise run for an unbounded total even though every individual
  run respected its per-entry budget. The orchestrator records a
  monotonic start time and enforces the ceiling two ways:

  - **Between rounds** — before starting the next round, if the
    elapsed time has reached the budget the loop stops cleanly and
    returns the rounds gathered so far (same shape as the
    consecutive-reject circuit breaker).
  - **Within a round** — each round's work is wrapped in
    `asyncio.wait_for` with a timeout of the *remaining* budget, so a
    single long round cannot blow the total. A round cancelled this
    way is recorded as an aborted round (a synthetic
    `EvolveRoundOutcome` with a `wall_clock_budget` rejection reason)
    and the loop stops.

  ```python
  # in zicato.orchestrator.evolve_n_rounds
  remaining = max_wall_clock_seconds - (time.monotonic() - budget_start)
  try:
      outcome = await asyncio.wait_for(_run_round(), timeout=remaining)
  except asyncio.TimeoutError:
      # finishing this round would overrun the total budget
      outcome = _budget_aborted_outcome(parent_id, max_wall_clock_seconds)
  ```

**The blocking-call caveat applies to the per-evolve budget.** The
*per-evolve total* budget is an L1 `asyncio.wait_for` guard in the
orchestrator process: it pre-empts only *cooperative* async work, so a
round wedged in a blocking call or a GIL-holding C extension in the
orchestrator itself is not hard-killed by it. The *per-entry* budget,
by contrast, is now enforced at the **L3 worker process boundary**
(§2.3) — a wedged run is killed by SIGTERM → SIGKILL on its own worker
PID regardless of cooperation. So the per-evolve ceiling bounds the
cooperative aggregate case honestly, while each individual run is
bounded hard by its own process.

### 2.2 L2 — structured cancellation

When `asyncio.wait_for` fires, it raises `CancelledError` in the
coroutine. zicato code that owns long-running async work catches
`CancelledError` at task boundaries and cleans up: closes file
handles, flushes the events sink, writes a "killed" marker on
the run's state file.

```python
async def run_one_entry(entry: BoardEntry, ...) -> RunResult:
    sink = JSONLPersistenceSink(path=..., mode="write")
    try:
        return await asyncio.wait_for(
            adapter.run_entry(entry, sinks=[sink]),
            timeout=entry.wall_clock_budget_seconds,
        )
    except asyncio.TimeoutError:
        # adapter is being told to cancel; let it propagate up
        raise
    except asyncio.CancelledError:
        # we were cancelled from above; clean up and re-raise
        sink.close()
        raise
    finally:
        sink.close()  # idempotent
```

**What it catches.** Cleanup correctness on cooperative cancel.
Without L2, an L1 timeout would leave the events.jsonl in a
half-written state — the file handle never gets flushed and the
operator sees a truncated event stream.

**What it doesn't catch.** Anything L1 missed (because L2 only
runs when L1 has already fired).

L1 and L2 together are the **v1 robustness floor**. They are
correct against cooperative inner harnesses, which is the
inner-harness contract zicato can credibly demand from
operators. Adversarial cases (the agent has a `while True: pass`
in production by accident) need L3.

### 2.3 L3 — subprocess tournament workers (the non-negotiable layer)

> **Shipped.** Every board-entry tournament run executes in its own
> `python -m zicato._tournament_worker` subprocess
> (`src/zicato/_tournament_worker.py`, spawned per run by
> `src/zicato/tournament/runner.py` via
> `asyncio.create_subprocess_exec`). The worker enforces the run's
> wall-clock budget by self-aborting at the deadline, and the L4
> supervisor SIGKILLs the worker PID if it wedges — so the per-run
> process boundary that the argument below motivates is now in place.
> Each run also gets a fresh interpreter (no module-cache bleed between
> generations), and scoring inside the worker reads the transported
> `per_judge_weights` + the real tool-call ledger (see
> [RUNTIME.md](RUNTIME.md) §5).

**This is the load-bearing layer.** Without L3, zicato cannot
honour its robustness story against any agent that doesn't
cooperate with cancellation. With L3, zicato is robust against
*any* pathology in user code.

#### The GIL discussion

Python's Global Interpreter Lock (GIL) means at most one Python
bytecode thread runs at any moment. The event loop is just
another thread; `asyncio.wait_for`'s timeout fires only when the
event loop next runs.

Consider an agent's `instruction` that, when interpreted by an
LLM, returns Python code with an accidental infinite loop:

```python
# the agent's tool implementation
def shape_response(data):
    while data.get("not_done"):    # bug: never decremented
        # CPU-bound, no await
        data = transform(data)
    return data
```

This code:

- Holds the GIL the entire time.
- Has no `await` points, so the event loop never gets to run.
- Means `asyncio.wait_for` set on this call **never fires**. The
  timeout exception is queued for the event loop to deliver
  whenever it next gets the GIL, which is never.

The same shape applies to:

- A C extension that holds the GIL (some numerical libraries, some
  legacy bindings).
- A `time.sleep(99999)` call (blocks the thread; GIL held).
- A regex `re.match` on an O(2^n) backtracking pattern.
- A `deepcopy` of a recursive data structure.

**`asyncio.wait_for` cannot pre-empt any of these.** The only
reliable defense is OS process boundary.

#### The subprocess design

Every tournament run is executed in a **separate Python
subprocess**:

```
zicato-orchestrator (Python)
    │
    │ spawns
    ▼
zicato _worker --run-id e4f2_short_solar_parent --side parent ...
    │
    │ (independent Python interpreter)
    ▼
adapter.run_entry(entry, sinks=[...])
    │
    │ ... if this hangs, the orchestrator's event loop is fine ...
```

Signal escalation:

```
1. orchestrator notices the worker is stuck (wall-clock deadline,
   or supervisor reports stalled heartbeat)
2. orchestrator sends SIGTERM
3. wait ESCALATION_GRACE_SECONDS (default 10s)
4. worker still alive → SIGKILL
5. orchestrator reaps, finalises active_runs/{run_id}.json,
   moves on.
```

**SIGKILL is uninterruptible.** No userspace code, no matter how
pathological, can resist it. The kernel terminates the process.
This is the absolute floor of zicato's robustness story.

#### Cost of L3

- Process spawn overhead: ~50-200ms per run on Linux (more on
  macOS).
- Module import overhead: ~100-500ms for an ADK adapter,
  per worker.
- IPC overhead: zero in the v1.1 design (workers write to disk;
  no shared memory).

For a 10-entry board running parent + candidate, that's 20
workers × (spawn + import) ≈ 4-14s overhead per round. Acceptable
relative to actual run times (10-300s per run). Future
optimisation: a worker pool that keeps interpreters warm; see
[STORAGE.md](STORAGE.md) §G7.

#### What L3 catches that L1+L2 don't

| Pathology | L1 catches? | L3 catches? |
|---|---|---|
| HTTP client respects `CancelledError` and hangs on connect | yes | yes |
| HTTP client's underlying socket is blocking (sync IO in a thread pool) | no | yes |
| Agent code has `while True: pass` | no | yes |
| Agent code calls C extension that holds GIL for 5 minutes | no | yes |
| Agent code has accidental fork bomb | no | yes (worker SIGKILL takes the whole process tree) |
| Agent code OOMs the worker | no | yes (worker exits with signal, orchestrator reaps) |
| Agent code segfaults | no | yes (worker exits; orchestrator finalises as crashed) |

L3 is the **floor on pathology**. Whatever the inner harness
does, the worker dies and the round continues. The events.jsonl
may be partial; that's acceptable — the loss reducer treats a
partial run as an aborted run, scores it as worst-case, and the
tournament continues.

#### Worker termination → state finalisation

The worker's `active_runs/{run_id}.json` is the durable record
of what happened. When the worker exits:

- **Clean (exit 0).** Worker removed the file before exiting.
  Orchestrator's reaper sees `wait()` returns `(pid, 0)` and a
  missing file — interprets as "ran to completion". loss.json
  exists.
- **Aborted (exit 7, wall-clock).** Worker stamped
  `phase: "aborted"` and removed the file. loss.json exists with
  the abort marker.
- **SIGKILLed.** File still has `phase: "agent_running"`.
  Reaper sees `wait()` returns `(pid, signal=KILL)` and a stale
  file — rewrites to `phase: "killed"`, cause:
  `worker_killed_unrecoverably`. No loss.json; tournament treats
  this entry as worst-case for the affected side.
- **Crashed.** Worker exited with non-zero non-7 code, file
  still has `phase: "agent_running"`. Reaper rewrites to
  `phase: "crashed"`, captures exit code.

The orchestrator's reaper runs at every safe point in the
tournament loop. There's a hard floor: the reaper runs at least
every 5 seconds even when the orchestrator is idle, so dead
workers get cleaned up promptly even if the orchestrator is
otherwise paused.

### 2.4 L4 — orchestrator watchdog (Rust supervisor)

The orchestrator is a Python process. It can wedge too — a GIL
wedge in zicato's own code (unlikely but possible), an event loop
that gets into a busy cycle, a signal handler that mishandles
SIGCHLD. The orchestrator watching itself is not a defense.

L4 is the watchdog supervisor — a separate Rust process, separate
language, separate runtime (`crates/supervisor/`). It **ships today**:
`zicato evolve` auto-spawns it (in watchdog-only mode), and it watches
`heartbeat.json` and the `active_runs/*` files, escalating
SIGTERM → grace → SIGKILL on a stalled run. With L3 shipped, each
`active_runs/{run_id}.json` carries that run's own worker PID, so the
watchdog kills exactly one stalled run without touching the
orchestrator or any sibling run.

See [RUNTIME.md](RUNTIME.md) §3 for the supervisor's lifecycle,
state model, and escalation protocol. This section names what L4
catches that the inner layers don't.

| Pathology | L1+L2 catches? | L3 catches? | L4 catches? |
|---|---|---|---|
| Worker hangs in the inner harness | no | yes (worker SIGKILL) | yes (supervisor escalates if orchestrator is slow to notice) |
| Orchestrator wedges (zicato bug in `evolve_round`) | no | no | yes (supervisor surfaces "stalled" status to operator; operator decides to restart) |
| Both orchestrator AND a worker wedge simultaneously | no | depends on the orchestrator | yes (supervisor escalates the worker directly; orchestrator status flagged) |
| Supervisor itself wedges | no | no | no — but: this is the smallest surface; Rust + no LLM; the supervisor is the thing the design tries hardest to keep simple. If it wedges, the dashboard (a separate process) still reads the state files directly and the operator sees it. |

L4 deliberately does NOT auto-kill the orchestrator. An
orchestrator stall is more likely to be a slow LLM endpoint than
a bug; killing the orchestrator would lose work that's actually
about to finish. The supervisor surfaces "stalled" to the
operator; the operator's wrapper script (systemd, supervisord,
etc.) is what decides whether to restart. zicato doesn't
reinvent process supervision.

### 2.5 L5 — consecutive-bad circuit breaker

Long unproductive epochs are wasteful. If the proposer is
repeatedly producing patches that don't promote, the operator
should know — and `evolve` should stop on its own rather than
burning hours of compute.

```
zicato evolve --rounds 20 --max-consecutive-rejections 3
                                   │
                                   │ K consecutive rejects?
                                   ▼
                              exit with code 6
                              ("no promotions; loop appears stuck")
```

K defaults to 3 (configurable). A consecutive-reject window is
the simplest version; future versions can layer in:

- Pattern: same drift kinds aren't moving across multiple
  rounds.
- Pattern: hypothesis match-rate is below 25% across recent
  rounds (the proposer is guessing, not reasoning).
- Pattern: every recent reject was for the same `rejection_reason`.

L5 is **partially shipped in v1** (just the consecutive-rejects
counter). The richer signals land in v1.1 as part of the
operator-visible metrics surfaced via the dashboard.

The circuit breaker is the only layer that's deliberately
"smart" about loop quality. The others are about preventing the
loop from breaking; L5 is about preventing the loop from
*wasting time even when working*.

### 2.6 L6 — atomic writes (+ resume markers)

Every disk write in zicato uses the atomic-rename pattern (see
[RUNTIME.md](RUNTIME.md) §6) — this **ships today**: every state file
is either fully-old or fully-new on read. The resume *markers* exist on
disk (`current_generation`, the `outcome` block presence in
`experiment.json`), but the **resume protocol** that reads them to
restart an interrupted loop is **planned** (see [RUNTIME.md](RUNTIME.md)
§4). So today L6 guarantees no torn writes; automatic crash-resume is
the not-yet-shipped half.

#### Where atomicity matters

| File | Reader | What partial write would cause |
|---|---|---|
| `heartbeat.json` | supervisor | false "stalled" alarm |
| `active_tournament.json` | dashboard, orchestrator on resume | UI flicker, resume confusion |
| `active_runs/{run_id}.json` | supervisor, orchestrator | escalation on the wrong run |
| `experiment.json` | journal, analysis pass | half-written outcome block; downstream parse failure |
| `gen_score.json` | tournament, dashboard | wrong gate verdict on resume |
| `journal.md` | operator, analysis pass | (this one is append-only; uses `O_APPEND` semantics, not rename) |
| `events.jsonl` | reducer, dashboard log tail | (this one is append-only; line-flush-per-event from JSONLPersistenceSink) |

The two exceptions (`journal.md`, `events.jsonl`) are append-only
and rely on the kernel's `O_APPEND` atomicity for the line size
they use (well below `PIPE_BUF`). Every other file uses the
atomic-rename helper.

#### Resume markers

When `zicato evolve` restarts after a crash, the protocol in
[RUNTIME.md](RUNTIME.md) §4 reads the committed state in
`epochs/{epoch}/` and infers where the previous run left off.
The key markers:

- `experiment.json` exists ⇒ proposer ran.
- `experiment.json.patches/*.json` files exist ⇒ patches were
  serialised.
- `snapshot/` exists ⇒ applier ran.
- `runs/{entry_id}/loss.json` exists ⇒ that entry's run is
  complete.
- `outcome` field in `experiment.json` ⇒ tournament decided.

The presence of each marker is what the resume protocol uses
to skip work that's already done. The orchestrator is
**conservative**: when it can't tell whether a marker is fully
written or partially written, it discards the work and re-runs.

L6 makes the resume safe; L3 plus L4 make the resume necessary.

## 3. Failure-mode tables

The layers exist to catch specific pathologies. This section
enumerates the pathologies, which layer catches each, and what
the operator sees.

### 3.1 Network hang in a cooperative HTTP client

```
Symptom:    LLM call to api.example.com sits open for 5 minutes.
Pathway:    aux_call_llm() → httpx.AsyncClient.post() (async-safe)
            → asyncio.wait_for fires at t=120s
Caught by:  L1
Operator sees:  ZicatoAuxLLMTimeout exception in the round; the
               round may either retry or fail the entry depending on
               where in the round we were. Run continues.
```

### 3.2 Inner harness has `while True: pass`

```
Symptom:    A specialist's tool implementation has an infinite loop.
Pathway:    worker calls agent → agent calls tool → tool spins
            → wall_clock_deadline fires inside worker (worker is the
              one who knows the deadline)
            → asyncio.wait_for in worker can't pre-empt (GIL held)
            → worker.wall_clock_deadline timer (separate thread or
              SIGALRM) eventually fires
            → worker exits with code 7
            → orchestrator reaps; logs aborted
            OR
            → supervisor's heartbeat-stale detection fires
            → SIGTERM → grace → SIGKILL
            → orchestrator reaps; logs killed
Caught by:  L3 (worker boundary + signal escalation), L4 (supervisor
            as backstop if orchestrator-side timer fails)
Operator sees: that entry's run scored as aborted; gen_score reflects
               worst-case; tournament continues. Dashboard shows
               the kill event in the log tail and the audit.
```

### 3.3 CPU-bound C extension holds GIL for 10 minutes

```
Symptom:    A model client uses a synchronous TLS library that holds
            the GIL during handshake retries.
Pathway:    Same as 3.2. The worker's asyncio timer queues an
            exception; nothing delivers it; SIGTERM forces exit
            (cooperatively via signal handler) or SIGKILL forces
            exit (uncooperatively).
Caught by:  L3 (process boundary)
Operator sees: same as 3.2. The dashboard's log tail will show no
               events from the worker in that window; the supervisor's
               heartbeat_stale event explains why.
```

### 3.4 Orchestrator crashes mid-tournament (e.g. OOM)

```
Symptom:    Operator's machine hits memory pressure; OOM-killer takes
            the orchestrator. Workers may still be running.
Pathway:    Orchestrator process gone; lock.json now points to a dead
            PID. Workers continue (they don't know the orchestrator
            died) and either finish or run to their own deadline.
            Supervisor notices the orchestrator's heartbeat stopped
            and broadcasts the stalled event; it does NOT kill the
            workers (it doesn't know the orchestrator died vs paused).
            Operator restarts: new orchestrator sees stale lock,
            steals it, reaps any zombie workers, finalises their
            active_runs/ entries, then runs the resume protocol.
Caught by:  L6 (atomic writes + resume markers), L4 (supervisor
            surfaces the stall)
Operator sees: dashboard shows "STALLED" indefinitely. On restart,
               new orchestrator logs "stealing stale lock" and "found
               N completed entries since last commit; resuming from
               round X step Y". Some entries' runs may have completed
               but not been journaled; resume protocol picks them up.
```

### 3.5 Worker OOM-killed

```
Symptom:    A worker uses too much memory; OOM-killer takes it.
Pathway:    Worker exits via SIGKILL; orchestrator's wait() returns
            (pid, signal=9). Worker's active_runs/{run_id}.json was
            still in phase: "agent_running". Orchestrator's reaper
            rewrites to phase: "crashed", cause: "oom".
Caught by:  L3 (worker isolation prevents OOM from taking the
            orchestrator)
Operator sees: that entry's run shows "crashed (OOM)"; the entry is
               scored as worst-case for that side; tournament
               continues. Dashboard log tail shows no terminal event
               from the worker; supervisor's escalation panel shows
               the SIGKILL.
```

### 3.6 Watchdog supervisor wedges

```
Symptom:    The Rust watchdog supervisor itself stops responding
            (extremely unlikely; in practice a kernel-level event like
            memory pressure that didn't quite OOM-kill it).
Pathway:    /statusz stops responding; escalation stops happening.
Caught by:  No automatic layer — the supervisor IS the watchdog.
Operator sees: The dashboard still works (it is a separate Python
               service reading the same state files directly), so the
               operator can confirm the orchestrator is fine — the
               watchdog dying does not blind the operator. They restart
               the watchdog by terminating `zicato evolve` and
               re-running it, which spawns a fresh supervisor.
```

The watchdog is the smallest surface and the lowest-leverage
process; this case is extremely unlikely and graceful degradation
(the dashboard and the state files remain readable) is the answer. We
do not stack a "watchdog for the watchdog"; that would just push the
same question one level up.

### 3.7 Both orchestrator and a worker hang

```
Symptom:    A bug in the orchestrator's worker-wait code AND a
            hung worker.
Pathway:    Orchestrator wedged before it could time-out the
            worker. Supervisor sees:
              - heartbeat stale ⇒ broadcast "stalled"
              - active_runs/{run_id}.json's wall_clock_deadline
                passed AND heartbeat_at stale ⇒ escalate worker
                directly (SIGTERM → grace → SIGKILL).
            Supervisor's escalation is independent of the
            orchestrator. Worker dies. Orchestrator's status
            remains "stalled" — the operator decides what to do
            with it.
Caught by:  L3 (process boundary on worker) + L4 (supervisor
            escalates worker independently of orchestrator state)
Operator sees: supervisor's escalation panel shows the kill; the
               worker's entry shows "killed"; orchestrator's
               stalled state persists. Operator restarts the
               orchestrator; resume protocol picks up.
```

### 3.8 Long unproductive epoch (every round rejects)

```
Symptom:    The proposer keeps proposing patches that fail the
            tournament gate. No promotions for K consecutive
            rounds.
Pathway:    L5 circuit breaker fires; evolve exits with code 6.
Caught by:  L5
Operator sees: evolve exits cleanly; journal shows K consecutive
               rejects; analysis pass would normally not run
               (epoch isn't closed) but the operator now has the
               signal to revisit the rubric or close the epoch.
```

### 3.9 LLM endpoint returns malformed JSON

```
Symptom:    The proposer's `auxiliary_call_llm` returns text that
            isn't valid JSON for the hypothesis schema.
Pathway:    Schema validator rejects; proposer is re-prompted with
            an error message; second violation exits with code 4.
Caught by:  Schema enforcement (not part of the layered defense
            model — this is a validation rule, but worth listing
            for completeness)
Operator sees: round exits with code 4; journal records the
               schema-violation as a no-op round.
```

### 3.10 Disk full during atomic write

```
Symptom:    .write_atomic("heartbeat.json.tmp", ...) fails with ENOSPC.
Pathway:    Exception propagates; orchestrator catches at top level
            and aborts the round cleanly (logs the disk-full
            condition; does not corrupt the existing file because
            the rename never happened).
Caught by:  L6 (the atomic-write contract: failed write leaves the
            old file intact)
Operator sees: evolve exits with a clear error; existing state
               files are unchanged. Operator frees disk space and
               re-runs; resume protocol picks up.
```

## 4. Phasing

The layers ship in order: the cheapest first, the most invasive
last.

### 4.1 Shipped today: L1 + L2 + L3 + L4 + L5 + atomic writes (L6)

What's in the shipped build:

- L1 `asyncio.wait_for` budgets at the run granularities: per board
  entry (`wall_clock_budget_seconds`) and per whole invocation
  (`evolve --max-wall-clock-seconds`, enforced both between rounds and
  within a round). The per-*aux-call* wrapper (a 120s budget on the
  proposer / emulator / judge `auxiliary_call_llm` sites) is the small
  §5 follow-up — not yet threaded through those call sites.
- L2 structured cancellation cleanup in the runner and per-entry
  adapter calls.
- **L3 subprocess tournament workers.** Every board-entry run executes
  in its own `python -m zicato._tournament_worker` process
  (`src/zicato/_tournament_worker.py`, spawned by
  `src/zicato/tournament/runner.py`) over an ephemeral copy of the
  generation snapshot, under a hard per-run wall-clock budget. This is
  the load-bearing layer (§2.3) — it is what lets the watchdog hard-kill
  an *uncooperative* inner harness (a GIL-holding loop) at a per-run
  boundary instead of taking the whole orchestrator.
- L5 consecutive-reject early stop (`evolve --max-consecutive-rejections`,
  default 3).
- L6 atomic writes for the runtime state files **and** `experiment.json`
  / `gen_score.json` — every JSON/text file goes through the
  temp-then-rename helper.
- L4 the Rust watchdog supervisor, auto-spawned by `evolve`
  (watchdog-only mode), escalating SIGTERM → grace → SIGKILL on a
  stalled run.
- The `.zicato/runtime/` state files and the dashboard service that
  reads them.

### 4.2 Planned: the resume protocol

What is **not yet** in the build:

- The **resume protocol** on orchestrator restart (the markers exist;
  reading them to resume does not).
- `zicato status` / `zicato kill` CLI commands (the dashboard's kill
  control is the current path for that).
- The richer L5 signals (hypothesis match-rate decay, same-drift-kinds
  detection) beyond the consecutive-reject counter.

With L3 shipped, the floor is the **full six-layer model**: an
uncooperative inner harness is hard-killed at the per-run worker
boundary, and inner harnesses that respect `CancelledError` are covered
cooperatively at L1/L2 first. What remains is *resume*: a mid-tournament
kill currently loses in-flight work to re-runs (the unit cache makes the
re-runs cheap), and the resume protocol that would read the existing
markers to skip completed work is still to come.

### 4.3 Shipped: the dashboard (observability)

The dashboard ships as a **separate Python service** (not a role of the
Rust binary), auto-spawned by `evolve`. No new robustness layers — it
makes the layers' state visible to the operator. See
[DASHBOARD.md](DASHBOARD.md).

### 4.4 Partially shipped: the dashboard control surface

The control-file protocol's **write side ships** (the dashboard's POST
`/api/control/*` endpoints drop `control/` files). The orchestrator's
consume-at-safe-points half, the `control_log/` audit, and gate-override
confirmation are **planned** — no new defenses either way; the
operator's actions are recorded and (once consumed) applied at safe
points.

## 5. Auxiliary LLM timeout follow-up

The zicato auxiliary call sites (proposer, judge, emulator,
analysis pass) currently do not wrap `auxiliary_call_llm` in
`asyncio.wait_for`. Small v1 follow-up: wrap each in a per-call
budget (default 120s) so a hanging LLM endpoint doesn't wedge
the round before L3 is in place.

```python
# in zicato.proposer
async def propose_experiment(...) -> Experiment:
    response = await aux_call_llm_with_budget(
        system=PROPOSER_SYSTEM_PROMPT,
        user=render_proposer_prompt(...),
        model=auxiliary_model,
        budget_s=120.0,
    )
    return parse_experiment(response)
```

```python
# in zicato.emulator
async def emulator_turn(...) -> str:
    return await aux_call_llm_with_budget(
        system=EMULATOR_SYSTEM_PROMPT,
        user=render_emulator_context(persona, transcript),
        model=auxiliary_model,
        budget_s=60.0,   # tighter; emulator turns should be quick
    )
```

These wrappers are L1 layered onto sites that currently rely on
the inner-harness adapter to enforce its own timeouts. The
follow-up is described as "small" because the budget values are
known good defaults; the only work is finding the call sites and
threading the wrapper through.

The wrappers do not replace L3. They're the cheap fast path that
gives well-behaved aux clients a graceful timeout before
escalation kicks in.

## 6. Loop health: a toothless evaluation is a failure mode

The six layers above defend against the loop **breaking** —
hangs, crashes, OOMs. They answer "is the loop broken?". There
is a second, quieter failure mode they do not cover: the loop
that is not broken but is **meaningless**.

A meta-loop can satisfy every layer in this document — no hangs,
no crashes, every round completing cleanly, every state file
atomically written — and still produce **zero optimisation
signal**. This happens when the *evaluation itself* is
degenerate: a board whose entries all score identically for every
generation, a drift signal that never moves, a scoring
configuration that cannot distinguish any candidate. The
tournament then compares two indistinguishable scalars round
after round; the proposer can run forever and never legitimately
promote.

This is a failure mode in exactly the sense this document uses
the term — a state the system can enter where it no longer does
its job — and it is worse than a crash in one specific way: a
crash *stops* and the operator notices; a toothless loop *keeps
running*, consumes budget, and fills the journal with a
confident-looking lineage that means nothing.

The motivating incident: a real run had generations `v0` and `v1`
both score **exactly `1.000000`**. Every robustness layer was
satisfied; the loop reported itself healthy. The degeneracy was
discovered only because an operator manually eyeballed the
journal and noticed the suspicious number — nothing in zicato
flagged it.

So zicato treats **loop health** as a robustness concern, sitting
alongside the six layers rather than downstream of them:

| Concern | Question | Subsystem |
|---|---|---|
| Loop breaks (hang / crash / OOM) | Is the loop *broken*? | Layers L1-L6 (this document) |
| Loop is unproductive | Is the loop *wasting time*? | L5 circuit breaker (§2.5) |
| **Loop is meaningless** | **Is the eval *toothless*?** | **Loop-health diagnostics** |

Loop-health diagnostics is a first-class subsystem: a fixed set
of detectors (degenerate scoring, non-differentiating board
entries, flat drift signal, no-expectations, stalled loop) run
after every round, emit a typed `LoopHealth` report with
`info` / `warning` / `critical` severities, and surface
`critical` findings as a loud bannered orchestrator warning plus
an SSE event to the dashboard's loop-health panel. The orchestrator
**early-stops on sustained degeneracy by default** (two consecutive
CRITICAL rounds → a `degenerate_health` stop reason); this is a
default-on behaviour, not a `--stop-on-degenerate` CLI flag.

Loop health is a close cousin of the L5 circuit breaker (§2.5)
and feeds it — the richer L5 signals (hypothesis match-rate
decay, same drift kinds not moving) *are* loop-health detectors.
But the two answer different questions: L5 fires on an
*unproductive* loop (good eval, the proposer is just not finding
wins); loop health fires on a *meaningless* loop (the eval
itself cannot distinguish anything, so even a perfect proposer
would never promote). An operator needs to know which one they
are looking at.

The full subsystem — every detector, the severity rules, the
`LoopHealth` report schema, the `zicato health` CLI, and the
orchestrator's surfacing behaviour — is specified in
[LOOP-HEALTH.md](LOOP-HEALTH.md).

## 7. What we explicitly do NOT defend against

These are out of scope; including them would expand the surface
without proportional value.

- **Malicious operator input.** The CLI assumes the operator is
  acting in good faith. Anyone who can run `zicato evolve` on
  the workspace can also `rm -rf .zicato/`; the runtime layer
  doesn't sandbox the operator.
- **Network adversaries.** The dashboard is on loopback by
  default; LAN exposure is the operator's choice. No TLS, no
  auth.
- **Resource accounting.** zicato doesn't cgroup-limit the
  workers. An adversarial inner harness can fork-bomb the
  machine in v1.1; L3 catches the worker process but not its
  children. Future hardening: spawn workers in a process group
  and kill the group, not just the leader.
- **LLM-side bugs that produce valid-looking but wrong outputs.**
  The collusion-proof emulator design ([EMULATOR.md](EMULATOR.md))
  is the closest we get to defending against this. The robustness
  layers in this document are about process/IO failures, not
  output-quality failures.

## 8. Cross-references

| Topic | Document |
|---|---|
| State files and the supervisor binary | [RUNTIME.md](RUNTIME.md) |
| The live dashboard view of state | [DASHBOARD.md](DASHBOARD.md) |
| Where atomic writes touch the storage layer | [STORAGE.md](STORAGE.md) |
| Loop-health diagnostics — the detectors, `zicato health`, default-on degenerate early-stop | [LOOP-HEALTH.md](LOOP-HEALTH.md) |
| Resume markers on `experiment.json` | [EPOCHS-AND-JOURNALING.md](EPOCHS-AND-JOURNALING.md) §3 |
| Worker entry point | [CLI.md](CLI.md) |
| The cancellation contract assumed by L1+L2 | [ARCHITECTURE.md](ARCHITECTURE.md) §4 |
| Why subprocess isolation, not threads | [RATIONALE.md](RATIONALE.md) |
