# Robustness

Six defense layers keep zicato running through pathological cases:
network hangs, infinite Python loops, CPU-bound C extensions,
orchestrator crashes, and out-of-memory kills. Each layer catches a
different class of failure, and they stack because no single layer
covers all of them.

The meta-loop described in [ARCHITECTURE.md](ARCHITECTURE.md) is
correct against inner harnesses that cooperate with cancellation. The
layers below extend that correctness to harnesses that do not.

## 1. The six layers

The table is the definition of each layer's identifier. Everywhere
else, this document and its companions use the layer's name.

| Layer | Name | Mechanism | Catches | Status today |
|---|---|---|---|---|
| **L1** | the per-call and per-budget timeouts | `asyncio.wait_for` around each call, each board entry, and each evolve invocation | cooperative network and IO hangs | **shipped** |
| **L2** | structured cancellation | `CancelledError` caught at task boundaries | async code that yields | **shipped** |
| **L3** | the subprocess worker boundary | one `python -m zicato._tournament_worker` process per board-entry run | loops that hold the interpreter lock, and any other user-code pathology | **shipped**, with a hard per-run wall-clock budget |
| **L4** | the orchestrator watchdog (the Rust supervisor) | a separate Rust process watching the state files | parent-side wedges | **shipped**; the watchdog binary is auto-spawned and escalation targets the per-run worker process |
| **L5** | the consecutive-bad circuit breaker | a consecutive-reject counter that stops the loop | long unproductive epochs | **shipped**; richer signals planned |
| **L6** | atomic writes and resume markers | temp file, fsync, rename | mid-run crashes | **shipped**, including the conservative crash-resume protocol |

The layers nest from inside to outside. The timeouts and structured
cancellation run inside the run itself. The subprocess worker boundary
wraps each run in its own operating-system process. The orchestrator
watchdog is a process outside every run. The circuit breaker acts at
the level of the whole loop. Atomic writes and resume markers cover
on-disk durability.

> **What this amounts to.** The timeouts and structured cancellation
> make zicato robust against inner harnesses that cooperate with
> cancellation. The subprocess worker boundary isolates each run in its
> own operating-system process, so an uncooperative harness — a C
> extension that holds the interpreter lock, a `while True: pass` — is
> hard-killed at a per-run boundary under a wall-clock budget
> (`src/zicato/_tournament_worker.py`). Atomic writes make the state on
> disk durable. The orchestrator watchdog escalates a stalled run by
> SIGTERM then SIGKILL on that run's own worker process id. The circuit
> breaker stops a loop that keeps rejecting. A restart after a kill
> picks up through the conservative resume protocol, which reuses an
> interrupted generation's persisted patches and cached board units where
> they are known-good and discards the directory otherwise (see
> [RUNTIME.md](RUNTIME.md) §4).

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

A failure caught by a timeout never reaches structured cancellation.
A failure that escapes both reaches the subprocess worker boundary.
The outer layers exist because the inner layers fail in
well-understood ways.

## 2. Layer-by-layer

### 2.1 Per-call and per-budget timeouts

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

**What it does not catch.** Any case where the timeout exception
fires inside the event loop but the underlying work does not stop.
This includes blocking IO inside a `to_thread` call, where the thread
keeps going; CPU-bound work in a coroutine, where the event loop never
gets to schedule the exception; and bugs where the await point does
not surrender the loop.

Timeouts are the cheapest defense, and everything inside zicato uses
them. They are also the weakest: on their own they guarantee only
cooperative termination, which is not enough for production.

#### Layered wall-clock budgets

Timeout budgets apply at two nested granularities, and **both apply at
once** — the inner ceiling does not replace the outer one.

* **Per-entry budget.** Each `BoardEntry` carries its own
  `wall_clock_budget_seconds`, threaded into the run's worker args file.
  The per-run subprocess worker (§2.3) enforces it: the worker
  self-aborts at the deadline through an `asyncio.wait_for` inside
  itself, and the orchestrator watchdog backstops by sending SIGKILL to
  the worker's process id if it wedges. A run that overruns is therefore
  aborted and scored worst-case
  (`abort_reason="wall_clock_budget"`) even against an uncooperative
  harness. This bounds one run, hard. The `racing` structure can also
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
per-evolve total budget is an `asyncio.wait_for` guard in the
orchestrator process, so it pre-empts only cooperative async work. A
round wedged in a blocking call, or in a C extension that holds the
interpreter lock inside the orchestrator itself, is not hard-killed by
it. The per-entry budget is enforced instead at the subprocess worker
boundary (§2.3): a wedged run is killed by SIGTERM then SIGKILL on its
own worker process id, whether or not it cooperates. The per-evolve
ceiling therefore bounds the cooperative aggregate case honestly, while
each individual run is bounded hard by its own process.

### 2.2 Structured cancellation

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

**What it catches.** Cleanup correctness when a cancel is honoured.
Without structured cancellation, a timeout would leave `events.jsonl`
half written: the file handle is never flushed and the operator sees a
truncated event stream.

**What it does not catch.** Anything the timeouts missed, because
structured cancellation only runs once a timeout has fired.

Timeouts and structured cancellation together form the cooperative
floor. They are correct against inner harnesses that honour
cancellation, which is the contract zicato can credibly demand from
operators. A harness that ignores cancellation — an agent that
accidentally ships a `while True: pass` — needs the subprocess worker
boundary.

### 2.3 The subprocess worker boundary

> **Shipped.** Every board-entry tournament run executes in its own
> `python -m zicato._tournament_worker` subprocess
> (`src/zicato/_tournament_worker.py`, spawned per run by
> `src/zicato/tournament/runner.py` via
> `asyncio.create_subprocess_exec`). The worker enforces the run's
> wall-clock budget by self-aborting at the deadline, and the
> orchestrator watchdog sends SIGKILL to the worker's process id if it
> wedges, which puts the per-run process boundary that the argument
> below motivates in place. Each run also gets a fresh interpreter, so
> no module cache carries between generations, and scoring inside the
> worker reads the transported `per_judge_weights` together with the
> real tool-call ledger (see [RUNTIME.md](RUNTIME.md) §5).

**This is the load-bearing layer.** Without the subprocess worker
boundary, zicato cannot honour its robustness story against an agent
that ignores cancellation. With it, zicato is robust against any
pathology in user code.

#### The GIL discussion

Python's Global Interpreter Lock (GIL) allows at most one Python
bytecode thread to run at any moment. The event loop is one more such
thread, so an `asyncio.wait_for` timeout fires only when the event loop
next runs.

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
  older bindings).
- A `time.sleep(99999)` call (blocks the thread; GIL held).
- A regex `re.match` on an O(2^n) backtracking pattern.
- A `deepcopy` of a recursive data structure.

**`asyncio.wait_for` cannot pre-empt any of these.** The only
reliable defense is an operating-system process boundary.

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

#### Cost of the worker boundary

- Process spawn overhead: roughly 50 to 200 milliseconds per run on
  Linux, more on macOS.
- Module import overhead: roughly 100 to 500 milliseconds per worker
  for an agent development kit (ADK) adapter.
- Inter-process communication overhead: none, because workers write to
  disk and share no memory.

For a 10-entry board running parent and candidate, that is 20 workers
of spawn plus import, or roughly 4 to 14 seconds of overhead per round.
That is acceptable against run times of 10 to 300 seconds. A worker
pool that keeps interpreters warm would remove most of it; no such pool
is built.

#### What the worker boundary catches that the inner layers do not

| Pathology | Caught by the timeouts? | Caught by the worker boundary? |
|---|---|---|
| HTTP client respects `CancelledError` and hangs on connect | yes | yes |
| HTTP client's underlying socket is blocking (sync IO in a thread pool) | no | yes |
| Agent code has `while True: pass` | no | yes |
| Agent code calls C extension that holds GIL for 5 minutes | no | yes |
| Agent code has accidental fork bomb | no | yes (worker SIGKILL takes the whole process tree) |
| Agent code exhausts memory in the worker | no | yes (worker exits with signal, orchestrator reaps) |
| Agent code segfaults | no | yes (worker exits; orchestrator finalises as crashed) |

The worker boundary is the floor on pathology. Whatever the inner
harness does, the worker dies and the round continues. The
`events.jsonl` file may be left partial, which is acceptable: the loss
reducer treats a partial run as an aborted run, scores it worst-case,
and the tournament continues.

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
tournament loop, and at least every 5 seconds even when the
orchestrator is idle, so dead workers are cleaned up promptly while the
orchestrator is paused.

### 2.4 The orchestrator watchdog (the Rust supervisor)

The orchestrator is itself a Python process, and it can wedge: on the
interpreter lock inside zicato's own code, on an event loop that enters
a busy cycle, or on a signal handler that mishandles SIGCHLD. An
orchestrator watching itself is not a defense.

The watchdog supervisor is a separate Rust process with its own
language and runtime (`crates/supervisor/`). It ships today: `zicato
evolve` auto-spawns it in watchdog-only mode, and it watches
`heartbeat.json` and the `active_runs/*` files, escalating from SIGTERM
through a grace period to SIGKILL on a stalled run. Each
`active_runs/{run_id}.json` carries that run's own worker process id,
so the watchdog kills exactly one stalled run without touching the
orchestrator or any sibling run.

See [RUNTIME.md](RUNTIME.md) §3 for the supervisor's lifecycle,
state model, and escalation protocol. The table below names what the
watchdog catches that the inner layers do not.

| Pathology | Caught by the timeouts and cancellation? | Caught by the worker boundary? | Caught by the watchdog? |
|---|---|---|---|
| Worker hangs in the inner harness | no | yes (worker SIGKILL) | yes (supervisor escalates if orchestrator is slow to notice) |
| Orchestrator wedges (zicato bug in `evolve_round`) | no | no | yes (supervisor surfaces "stalled" status to operator; operator decides to restart) |
| Both orchestrator and a worker wedge at once | no | depends on the orchestrator | yes (supervisor escalates the worker directly; orchestrator status flagged) |
| Supervisor itself wedges | no | no | no. The supervisor is the smallest surface in the system: Rust, and no model calls. If it wedges, the dashboard is a separate process that still reads the state files directly, so the operator sees the state. |

The watchdog does not auto-kill the orchestrator, by design. An
orchestrator stall is more often a slow model endpoint than a bug, and
killing the orchestrator would discard work that is about to finish.
The supervisor surfaces the stalled status to the operator, and the
operator's wrapper script — systemd, supervisord, or similar — decides
whether to restart. zicato does not reinvent process supervision.

### 2.5 The consecutive-bad circuit breaker

Long unproductive epochs waste time and money. If the proposer keeps
producing patches that do not promote, the operator should be told, and
`evolve` should stop on its own rather than consuming hours of compute.

```
zicato evolve --rounds 20 --max-consecutive-rejections 3
                                   │
                                   │ K consecutive rejects?
                                   ▼
                              exit with code 6
                              ("no promotions; loop appears stuck")
```

The consecutive-reject threshold K defaults to 3 and is configurable.
The loop also stops on a degenerate loop-health verdict, which
`stop_on_degenerate_health` enables by default
(`src/zicato/evolve/loop.py`). Three richer patterns are unbuilt: the
same drift kinds failing to move across multiple rounds, a hypothesis
match-rate below 25 percent across recent rounds (which would indicate
the proposer is guessing rather than reasoning), and every recent reject
carrying the same `rejection_reason`. None of the three has a detector in
`src/zicato/health/diagnostics.py`.

The circuit breaker is the only layer that judges loop quality. The
others keep the loop from breaking; the circuit breaker keeps a working
loop from wasting time.

### 2.6 Atomic writes and resume markers

Every disk write in zicato uses the atomic-rename pattern (see
[RUNTIME.md](RUNTIME.md) §6), so every state file reads as either wholly
its old contents or wholly its new contents. The resume markers on disk
— `current_generation`, and the presence of the `outcome` block in
`experiment.json` — are read at `evolve` start by `prepare_resume`
(`src/zicato/runtime/resume.py`, called from
`src/zicato/evolve/loop.py`). It clears the dead run's `runtime/` state
and then classifies the latest generation: an un-outcomed generation
with a readable experiment, a snapshot and at least one `loss.json`
resumes in place, reusing the persisted patches and the cached board
units; every other shape discards the directory and re-runs. This layer
therefore guarantees both no torn writes and a conservative restart (see
[RUNTIME.md](RUNTIME.md) §4).

#### Where atomicity matters

| File | Reader | What partial write would cause |
|---|---|---|
| `heartbeat.json` | supervisor | false "stalled" alarm |
| `active_tournament.json` | dashboard, orchestrator on resume | UI flicker, resume confusion |
| `active_runs/{run_id}.json` | supervisor, orchestrator | escalation on the wrong run |
| `experiment.json` | journal, analysis pass | half-written outcome block; downstream parse failure |
| `gen_score.json` | tournament, dashboard | wrong gate verdict on resume |
| `journal.md` | operator, analysis pass | nothing: this file is append-only and relies on `O_APPEND` semantics rather than rename |
| `events.jsonl` | reducer, dashboard log tail | nothing: this file is append-only and `JSONLPersistenceSink` flushes one line per event |

The two exceptions (`journal.md`, `events.jsonl`) are append-only
and rely on the kernel's `O_APPEND` atomicity for the line size
they use (well below `PIPE_BUF`). Every other file uses the
atomic-rename helper.

#### Resume markers

When `zicato evolve` restarts after a crash, the protocol in
[RUNTIME.md](RUNTIME.md) §4 reads the committed state in
`epochs/{epoch}/` and infers where the interrupted run stopped.
Five markers carry that information:

- `experiment.json` exists ⇒ proposer ran.
- `experiment.json.patches/*.json` files exist ⇒ patches were
  serialised.
- `snapshot/` exists ⇒ applier ran.
- `runs/{entry_id}/loss.json` exists ⇒ that entry's run is
  complete.
- `outcome` field in `experiment.json` ⇒ tournament decided.

The resume protocol uses the presence of each marker to skip work that
is already done. The orchestrator is conservative: when it cannot tell
whether a marker was fully or partially written, it discards the work
and re-runs it.

Atomic writes make resume safe. The worker boundary and the watchdog,
which kill processes mid-run, make resume necessary.

## 3. Failure-mode tables

Each pathology below names the layer that catches it and what the
operator sees.

### 3.1 Network hang in a cooperative HTTP client

```
Symptom:    LLM call to api.example.com sits open for 5 minutes.
Pathway:    aux_call_llm() → httpx.AsyncClient.post() (async-safe)
            → asyncio.wait_for fires at t=120s
Caught by:  the per-call timeouts
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
            → asyncio.wait_for in worker cannot pre-empt (GIL held)
            → worker.wall_clock_deadline timer (separate thread or
              SIGALRM) eventually fires
            → worker exits with code 7
            → orchestrator reaps; logs aborted
            OR
            → supervisor's heartbeat-stale detection fires
            → SIGTERM → grace → SIGKILL
            → orchestrator reaps; logs killed
Caught by:  the subprocess worker boundary and its signal
            escalation; the orchestrator watchdog backstops if the
            orchestrator-side timer fails
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
Caught by:  the subprocess worker boundary
Operator sees: same as 3.2. The dashboard's log tail will show no
               events from the worker in that window; the supervisor's
               heartbeat_stale event explains why.
```

### 3.4 Orchestrator crashes mid-tournament (for example, out of memory)

```
Symptom:    Operator's machine hits memory pressure; the out-of-memory
            killer takes the orchestrator. Workers may still be running.
Pathway:    Orchestrator process gone; lock.json now points to a dead
            process id. Workers continue, since they cannot observe
            that the orchestrator died, and either finish or run to
            their own deadline.
            Supervisor notices the orchestrator's heartbeat stopped
            and broadcasts the stalled event; it does NOT kill the
            workers, because it cannot distinguish a dead orchestrator
            from a paused one.
            Operator restarts: new orchestrator sees stale lock,
            steals it, reaps any zombie workers, finalises their
            active_runs/ entries, then runs the resume protocol.
Caught by:  atomic writes and resume markers; the orchestrator
            watchdog surfaces the stall
Operator sees: dashboard shows "STALLED" indefinitely. On restart,
               new orchestrator logs "stealing stale lock" and "found
               N completed entries since last commit; resuming from
               round X step Y". Some entries' runs may have completed
               but not been journaled; resume protocol picks them up.
```

### 3.5 Worker killed for exhausting memory

```
Symptom:    A worker uses too much memory; the out-of-memory killer
            takes it.
Pathway:    Worker exits via SIGKILL; orchestrator's wait() returns
            (pid, signal=9). Worker's active_runs/{run_id}.json was
            still in phase: "agent_running". Orchestrator's reaper
            rewrites to phase: "crashed", cause: "oom".
Caught by:  the subprocess worker boundary, whose isolation keeps the
            out-of-memory kill away from the orchestrator
Operator sees: that entry's run shows "crashed (oom)"; the entry is
               scored as worst-case for that side; tournament
               continues. Dashboard log tail shows no terminal event
               from the worker; supervisor's escalation panel shows
               the SIGKILL.
```

### 3.6 Watchdog supervisor wedges

```
Symptom:    The Rust watchdog supervisor itself stops responding
            (very unlikely; in practice a kernel-level event such as
            memory pressure short of an out-of-memory kill).
Pathway:    /statusz stops responding; escalation stops happening.
Caught by:  No automatic layer — the supervisor IS the watchdog.
Operator sees: The dashboard still works (it is a separate Python
               service reading the same state files directly), so the
               operator can confirm the orchestrator is fine — the
               watchdog dying does not blind the operator. They restart
               the watchdog by terminating `zicato evolve` and
               re-running it, which spawns a fresh supervisor.
```

The watchdog is the smallest and simplest process in the system, and
this case is very unlikely. The answer is graceful
degradation: the dashboard and the state files remain readable. zicato
does not stack a watchdog over the watchdog, which would move the same
question one level up.

### 3.7 Both orchestrator and a worker hang

```
Symptom:    A bug in the orchestrator's worker-wait code AND a
            hung worker.
Pathway:    Orchestrator wedged before it could time-out the
            worker. Supervisor sees:
              - heartbeat stale ⇒ broadcast "stalled"
              - active_runs/{run_id}.json's wall_clock_deadline
                passed AND last_progress stale ⇒ escalate worker
                directly (SIGTERM → grace → SIGKILL).
            Supervisor's escalation is independent of the
            orchestrator. Worker dies. Orchestrator's status
            remains "stalled" — the operator decides what to do
            with it.
Caught by:  the subprocess worker boundary, plus the orchestrator
            watchdog, which escalates the worker independently of
            orchestrator state
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
Pathway:    the consecutive-bad circuit breaker fires; evolve exits
            with code 6.
Caught by:  the consecutive-bad circuit breaker
Operator sees: evolve exits cleanly; journal shows K consecutive
               rejects; the analysis pass does not run because the
               epoch is not closed, but the operator now has the
               signal to revisit the rubric or close the epoch.
```

### 3.9 Model endpoint returns malformed JSON

```
Symptom:    The proposer's `auxiliary_call_llm` returns text that is
            not valid JSON for the hypothesis schema.
Pathway:    Schema validator rejects; proposer is re-prompted with
            an error message; second violation exits with code 4.
Caught by:  schema enforcement, which is a validation rule outside
            the six-layer defense model and is listed here so the
            pathology table is complete
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
Caught by:  the atomic-write contract, under which a failed write
            leaves the previous file intact
Operator sees: evolve exits with a clear error; existing state
               files are unchanged. Operator frees disk space and
               re-runs; resume protocol picks up.
```

## 4. What is in the build, and what is not

### 4.1 What is in the build

The shipped build contains:

- Timeout budgets at three granularities: per board entry
  (`wall_clock_budget_seconds`), per whole invocation
  (`evolve --max-wall-clock-seconds`, enforced both between rounds and
  within a round), and per auxiliary LLM call. The per-call budget,
  tunable with `evolve --aux-call-timeout`, covers the proposer,
  emulator, and judge `auxiliary_call_llm` sites and is
  threaded through those call sites by
  `src/zicato/aux_timeout.py`, which the proposer
  (`src/zicato/proposer/proposer.py`), the emulator
  (`src/zicato/emulator/emulator.py`) and the rubric grader
  (`src/zicato/board/rubric.py`) each import.
- Structured cancellation cleanup in the runner and in the per-entry
  adapter calls.
- **The subprocess worker boundary.** Every board-entry run executes
  in its own `python -m zicato._tournament_worker` process
  (`src/zicato/_tournament_worker.py`, spawned by
  `src/zicato/tournament/runner.py`) over an ephemeral copy of the
  generation snapshot, under a hard per-run wall-clock budget. This is
  the load-bearing layer (§2.3): it is what lets the watchdog hard-kill
  an uncooperative inner harness, such as a loop holding the interpreter
  lock, at a per-run boundary instead of taking down the whole
  orchestrator.
- The consecutive-reject early stop
  (`evolve --max-consecutive-rejections`, default 3).
- Atomic writes for the runtime state files and for `experiment.json`
  and `gen_score.json`. Every JSON and text file goes through the
  temp-then-rename helper.
- The Rust watchdog supervisor, auto-spawned by `evolve` in
  watchdog-only mode, escalating from SIGTERM through a grace period to
  SIGKILL on a stalled run.
- The `.zicato/runtime/` state files and the dashboard service that
  reads them.

### 4.2 What is unbuilt

Two things are not in the build:

- The `zicato status` and `zicato kill` commands. The dashboard's kill
  control and `zicato health` are the available paths.
- The richer circuit-breaker signals — hypothesis match-rate decay and
  same-drift-kinds detection — beyond the consecutive-reject counter and
  the degenerate-health stop (§2.5).

All six layers are in the build. An uncooperative inner harness is
hard-killed at the per-run worker boundary, a harness that honours
`CancelledError` is covered cooperatively by the timeouts and structured
cancellation first, and a mid-tournament kill is reconciled on the next
start by the conservative resume protocol (§2.6). A discarded round
costs one re-run, which the unit cache makes cheap.

### 4.3 Shipped: the dashboard (observability)

The dashboard ships as a separate Python service rather than as a role
of the Rust binary, auto-spawned by `evolve`. It adds no robustness
layer; it makes the layers' state visible to the operator. See
[DASHBOARD.md](DASHBOARD.md).

### 4.4 Shipped: the dashboard control surface

Both sides of the control-file protocol ship. The dashboard's POST
`/api/control/*` endpoints drop files under `control/`, and the
orchestrator consumes them at safe points — between rounds
(`src/zicato/evolve/loop.py`) and at the start of a round for
`skip_round` (`src/zicato/evolve/gauntlet.py`) — through
`src/zicato/runtime/control_consumer.py`. A consumed command is archived
under `control_log/` with a JSON sidecar naming the consumer and the
reason, and a gate override is recorded in the outcome record and the
journal rather than applied silently. The protocol adds no defense
layer; it records the operator's actions and applies them at safe
points.

## 5. Auxiliary model-call timeout follow-up

The zicato auxiliary call sites — proposer, judge, emulator, and
analysis pass — do not wrap `auxiliary_call_llm` in `asyncio.wait_for`.
The planned follow-up wraps each in a per-call budget, defaulting to
120 seconds, so that a hanging model endpoint cannot wedge a round
before the worker boundary takes over.

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

These wrappers add a timeout to sites that rely on the inner-harness
adapter to enforce its own. The work is small because the budget values
are known good defaults; what remains is finding the call sites and
threading the wrapper through.

The wrappers are not a substitute for the worker boundary. They give a
well-behaved auxiliary client a graceful timeout on the cheap path,
before signal escalation begins.

## 6. Loop health: a toothless evaluation is a failure mode

The six layers above defend against the loop breaking: hangs, crashes,
and out-of-memory kills. They answer whether the loop is broken. A
second, quieter failure mode goes uncovered — a loop that runs
correctly and means nothing.

A meta-loop can satisfy every layer in this document, with no hangs, no
crashes, every round completing cleanly, and every state file
atomically written, and still produce no optimisation signal at all.
This happens when the evaluation itself is degenerate: a board whose
entries all score identically for every generation, a drift signal that
never moves, or a scoring configuration that cannot distinguish any
candidate. The tournament then compares two indistinguishable scalars
round after round, and the proposer can run forever without ever
legitimately promoting.

A degenerate evaluation is a failure mode in the sense this document
uses the term — a state the system can enter where it stops doing its
job — and it is worse than a crash in one specific way. A crash stops
the loop and the operator notices. A toothless loop keeps running,
consumes budget, and fills the journal with a confident-looking lineage
that means nothing.

One observed run had generations `v0` and `v1` both score exactly
`1.000000`. Every robustness layer was satisfied and the loop reported
itself healthy. The degeneracy surfaced only because an operator read
the journal and noticed the suspicious number; nothing in zicato
flagged it.

zicato therefore treats loop health as a robustness concern that sits
alongside the six layers rather than downstream of them:

| Concern | Question | Subsystem |
|---|---|---|
| Loop breaks (hang, crash, out-of-memory kill) | Is the loop *broken*? | the six defense layers (this document) |
| Loop is unproductive | Is the loop *wasting time*? | the consecutive-bad circuit breaker (§2.5) |
| **Loop is meaningless** | **Is the evaluation *toothless*?** | **Loop-health diagnostics** |

Loop-health diagnostics is a first-class subsystem. A fixed set of
detectors — degenerate scoring, non-differentiating board entries, flat
drift signal, missing expectations, and a stalled loop — runs after
every round and emits a typed `LoopHealth` report at `info`, `warning`,
or `critical` severity. A `critical` finding surfaces as a bannered
orchestrator warning and as a server-sent event to the dashboard's
loop-health panel. The orchestrator early-stops on sustained degeneracy
by default: two consecutive rounds at `critical` produce a
`degenerate_health` stop reason. This behaviour is on by default rather
than behind a `--stop-on-degenerate` command-line flag.

Loop health is closely related to the consecutive-bad circuit breaker
(§2.5) and feeds it: the circuit breaker's planned richer signals,
hypothesis match-rate decay and drift kinds that fail to move, are
themselves loop-health detectors. The two answer different questions.
The circuit breaker fires on an unproductive loop, where the evaluation
is sound and the proposer is simply not finding wins. Loop health fires
on a meaningless loop, where the evaluation cannot distinguish anything
and even a perfect proposer would never promote. An operator needs to
know which of the two they are looking at.

The full subsystem — every detector, the severity rules, the
`LoopHealth` report schema, the `zicato health` CLI, and the
orchestrator's surfacing behaviour — is specified in
[LOOP-HEALTH.md](LOOP-HEALTH.md).

## 7. What the layers do not defend against

Four threats are out of scope. Covering them would expand the surface
without proportional value.

- **Malicious operator input.** The command-line interface assumes the
  operator acts in good faith. Anyone who can run `zicato evolve` on
  the workspace can also delete `.zicato/` outright, and the runtime
  layer does not sandbox the operator.
- **Network adversaries.** The dashboard binds to loopback by default,
  and exposing it on a local network is the operator's choice. There is
  no transport encryption and no authentication.
- **Resource accounting.** zicato does not apply control-group limits
  to the workers. An adversarial inner harness can fork-bomb the
  machine: the worker boundary catches the worker process but not its
  children. Hardening this means spawning workers in a process group
  and killing the whole group rather than only the group leader.
- **Model-side bugs that produce valid-looking but wrong outputs.** The
  collusion-proof emulator design ([EMULATOR.md](EMULATOR.md)) is the
  nearest defense zicato has. The layers in this document address
  process and IO failures rather than output-quality failures.

## 8. Cross-references

| Topic | Document |
|---|---|
| State files and the supervisor binary | [RUNTIME.md](RUNTIME.md) |
| The live dashboard view of state | [DASHBOARD.md](DASHBOARD.md) |
| Where atomic writes touch the storage layer | [STORAGE.md](STORAGE.md) |
| Loop-health diagnostics — the detectors, `zicato health`, default-on degenerate early-stop | [LOOP-HEALTH.md](LOOP-HEALTH.md) |
| Resume markers on `experiment.json` | [EPOCHS-AND-JOURNALING.md](EPOCHS-AND-JOURNALING.md) §3 |
| Worker entry point | [CLI.md](CLI.md) |
| The cancellation contract the timeouts and structured cancellation assume | [ARCHITECTURE.md](ARCHITECTURE.md) §4 |
| Why runs are isolated in subprocesses rather than threads | [RATIONALE.md](RATIONALE.md) |
