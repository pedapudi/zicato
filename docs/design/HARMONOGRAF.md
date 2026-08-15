# Harmonograf integration

Canonical, single-source-of-truth design for how zicato self-hosts
harmonograf — the execution-trace console — and surfaces it to the
operator at **both** the board-run level and the zicato (meta-loop)
level. This doc supersedes the scattered prose in `TELEMETRY.md` and
`TOURNAMENT.md`; when those two disagree with this file, this file wins.

> Vocabulary note: harmonograf is the *execution view* of a single run
> (a goldfive event stream rendered as a Gantt + lifelines). The zicato
> dashboard is the *competition view* across runs. The two are
> complementary — harmonograf answers "what did THIS run's agent do,
> step by step", the dashboard answers "which candidate won, and why".

## 1. Server lifecycle — ONE persistent server per workspace

There is exactly **one** harmonograf server per workspace, bound to that
workspace's `<ws>/.harmonograf/harmonograf.db` (sqlite). The single-open
contract is enforced through `<ws>/.harmonograf/server.json`:

* `telemetry/harmonograf_supervisor.py:ensure_workspace_harmonograf`
  is the **only** launch path. It reads `server.json`; if it names a
  server whose pid is alive **and** whose web port answers a TCP connect,
  it REUSES it (`launched=False`, caller must not shut it down).
  Otherwise it launches a fresh server (`start_harmonograf`) bound to the
  workspace db, rewrites `server.json`, and returns `launched=True`.
* Every launcher routes through this helper: `zicato dashboard` /
  `zicato dashboard --view builder` (`dashboard/server.py:_ensure_workspace_harmonograf`)
  AND a live `zicato evolve` (`orchestrator.py:_resolve_or_launch_harmonograf`).
  Because all paths consult `server.json` first, no two processes ever
  open the same sqlite file: whoever wins the race writes the record, the
  loser reuses it.
* **Shutdown ownership**: only the process that `launched=True` shuts the
  server down. A reused server is left running for its owner. An evolve
  that launched the server tears it down in its teardown `finally`; a
  standalone dashboard that launched it tears it down on process exit.
  The persisted **db** outlives every server, so a later dashboard
  process relaunches a server over the same db for post-mortem viewing.

The server binds **two** ports: a browser-facing gRPC-Web port (the
`web_url`, used for dashboard deep-links) and a native gRPC port (the
`grpc_target`, which the per-run / meta-loop sinks dial). Conflating the
two — dialing the web port over native gRPC — silently drops all
telemetry, so the split is load-bearing (see `telemetry/sink.py`
`resolve_harmonograf_grpc_target` and the internal `ZICATO_HARMONOGRAF_GRPC`
handoff — set by the auto-launch lifecycle, not by operators).

Failure isolation is absolute: a missing `harmonograf_server` dep, a
port-bind failure, or any startup exception yields a **no-op handle**
(`web_url=""`) and a logged WARNING. Evolve and the dashboard always
continue — harmonograf is additive, never load-bearing.

The client and server are installed by the `observability` and `all` profiles,
not by the base wheel. A base installation therefore follows this failure path
by design and records JSONL telemetry. The warning names the profile that
enables the live view; see [`INSTALL-PROFILES.md`](INSTALL-PROFILES.md).

## 2. Session taxonomy

Harmonograf keys every view by a **session id** (`[a-zA-Z0-9_-]{1,128}`).
Two distinct kinds of session exist in a zicato workspace:

### 2a. Board-run session (one per inner-harness run)

When a board entry runs, the worker calls `goldfive.run(agent, input,
sinks=[jsonl, harmonograf])`. goldfive mints a fresh per-run
`Session.run_id` (a uuid4) and **stamps it as `Event.session_id` on every
emitted event** (`goldfive/runner.py`). Both sinks therefore see the same
id:

* the **JSONL sink** writes `session_id` to disk → the post-run reducer
  reads it (`telemetry/reducer.py`, `evt.session_id`/`sessionId`) into
  `LossProfile.adk_session_id` → persisted in `loss.json`;
* the **HarmonografSink** forwards each event via `emit_goldfive_event`
  carrying that same `Event.session_id`; harmonograf's ingest
  (`ingest.py:_handle_goldfive_event`) auto-creates/routes the session
  under that exact id.

So the id streamed to harmonograf and the id surfaced to the dashboard as
`adk_session_id` are **the same uuid4**. The deep-link route is

```
<web_url>/#/session/<adk_session_id>
```

and it resolves to a real session — no client-side `session_id` override
is needed on the worker's `Client(name="zicato")` (the envelope carries
the id). The dashboard reads `adk_session_id` from `loss.json` (completed
runs) and surfaces it on run-like records (`runHeader`, `ab_grid` cells,
active-run rows).

### 2b. Zicato meta-loop session (one per evolve invocation)

The orchestrator's own LLM calls — the proposer and the in-loop process
judges — are a distinct goldfive session: the **meta-loop**. It is the
operator's "Gantt view of zicato itself" (proposer call → judge → next
generation, repeated across rounds). Its id is **deterministic** for a
given evolve start time:

```
meta_loop_session_id(evolve_started_at_iso)
  == "zicato-meta-loop-<sanitized ISO>"      # telemetry/harmonograf_supervisor.py
```

The `MetaLoopEmitter` (`telemetry/meta_loop.py`) attaches a JSONL sink
(`<ws>/.zicato/runtime/meta_loop_events.jsonl`) **and** a harmonograf
sink whose `Client(session_id=<meta_loop_session_id>)` pins every
proposer/judge envelope onto that one session. Deep-link route:

```
<web_url>/#/session/<meta_loop_session_id>
```

**Recovering the id post-mortem**: the deterministic id depends on the
evolve start ISO, which the dashboard does not otherwise know. The
canonical recovery is to read the `session_id`/`sessionId` off the first
line of `meta_loop_events.jsonl`
(`state_reader.py:read_meta_loop_session_id`). That works during a live
evolve and post-mortem off the persisted JSONL.

## 3. The two dashboard surfaces

### 3a. Per-run deep-links (wherever runs are shown)

Built by `js/core/harmonograf.js` (`harmonografRunUrl`, `harmonografLink`,
`harmonografMini`). They resolve `adk_session_id` off the record and gate
on `harmonografIsLive()`. They render:

* on the **candidate** view — the run drill header ("Open this run in
  harmonograf") and each in-flight board row ("execution ▸");
* on the **board grid / matchup** A/B cells (`parent_adk_session_id` /
  `child_adk_session_id`).

### 3b. Zicato-level execution surface (the previously-missing link)

A single, clearly-labelled **"execution ▸"** entry in the top bar
(`variants/T/shell.js`) links to the meta-loop session:

```
<web_url>/#/session/<meta_loop_session_id>
```

It is liveness-gated exactly like the per-run links — present whenever
the persistent server is up (live evolve OR a standalone dashboard that
resolved a persistent server) AND a meta-loop session id is known. It is
the operator's entry point to "harmonograf at the zicato level": the
proposer/judge timeline of the evolution itself. Built by
`harmonografMetaUrl()` / `harmonografMetaLink()` in `core/harmonograf.js`,
keyed on `state.heartbeat.harmonograf_meta_session`.

### 3c. Tournament navigation is a metadata filter, not a new trace type

Each target-run client stamps non-sensitive session labels:

| label | value |
|---|---|
| `zicato.epoch_id` | evaluation-contract epoch |
| `zicato.tournament_id` | current runtime tournament, when present |
| `zicato.match_id` | strategy matchup, when present |
| `zicato.generation_id` | generation under test |
| `zicato.entry_id` | board entry |
| `zicato.side` | `parent` or `child` |
| `zicato.replicate` | replicate index |
| `zicato.trace_kind` | `target` |

The candidate view's **Open tournament traces** link initializes
Harmonograf's ordinary session picker with an exact
`zicato.tournament_id` predicate. Harmonograf assigns no meaning to that key:
it implements only generic session-metadata filtering and continues to render
one selected session at a time. Zicato owns the tournament vocabulary and the
mapping from a tournament to the filter URL. Independent target executions
are never overlaid onto a synthetic shared clock.

These labels are navigation provenance only. Prompts, expected answers,
responses, and filesystem paths must not be placed in session metadata.

## 4. State plumbing — how the ids reach the frontend

`/api/state` (the merged heartbeat) carries:

| field | source | meaning |
|---|---|---|
| `harmonograf_url` | live heartbeat OR dashboard-injected persistent `web_url` | deep-link base |
| `harmonograf_persistent` | dashboard injection | persistent server up ⇒ "live" for deep-links even with no active run |
| `harmonograf_meta_session` | live heartbeat (orchestrator) OR dashboard-injected (read off `meta_loop_events.jsonl`) | the meta-loop session id for the zicato-level link |

The live `Heartbeat` (`runtime/state.py`) carries `harmonograf_url` and
`harmonograf_meta_session`; the standalone dashboard injects both in
`state_reader.py:_read_heartbeat_with_harmonograf` (post-mortem recovery
reads the meta session off the JSONL). Precedence mirrors `harmonograf_url`:
a live evolve's heartbeat value wins; the dashboard only fills when absent.

## 5. Liveness vs post-mortem

`harmonografIsLive()` is true when EITHER (a) a run is in flight (an
active tournament OR ≥1 active run) — the evolve-launched server exists
only then — OR (b) `harmonograf_persistent === true` (a standalone
dashboard resolved a per-workspace server that does NOT die with a run).
So a post-mortem dashboard over a finished workspace still lights up both
surfaces, because `ensure_workspace_harmonograf` relaunched a server over
the persisted db and `state_reader` injected the URL + meta session id.

## 6. Failure isolation summary

Every layer warns-and-continues; harmonograf is never load-bearing:

* missing `harmonograf_server` / `harmonograf_client` → no-op handle /
  `None` sink, JSONL-only telemetry;
* sink construction / emit failure → logged WARNING, event dropped, run
  unaffected (`meta_loop.py:_fan_emit`, `sink.py:_make_harmonograf_sink`);
* unresolvable / absent workspace → no-op workspace handle, dashboard
  still serves with no harmonograf links.

## 7. Structural spans — zicato's own concurrency as lifelines

The proposer + judge emits (§2b) put zicato's LLM calls on the meta-loop
timeline, but the STRUCTURE around them — the round loop, the propose-time
slate, the tournament fan-out, the per-run workers — emitted nothing, so the
"execution ▸" view rendered the target-agent lifelines in a vacuum. The
structural spans close that gap: the `MetaLoopEmitter` brackets each unit of
orchestration work with a paired goldfive `AgentInvocation{Started,Completed}`
envelope, rendered by harmonograf as a nested lifeline.

### 7a. The taxonomy

One generic emit surface — `MetaLoopEmitter.span(name, *, kind, meta)`, an
async context manager — carries all five span kinds (the `SPAN_*` constants).
`kind` selects the harmonograf lane (`agent_name = "zicato.<kind>"`, so every
span of a kind shares one lane); `name` is the per-instance label (`task_id`):

| kind | opened at | one span per | children |
|---|---|---|---|
| `round` | `evolve/loop.py:_run_round` | evolve round (both pipelines) | phases, matchups |
| `phase` | the stage seams | `propose` / `apply` / `gate` | slots (propose), the derive (apply) |
| `matchup` | `tournament/scheduling.py:_bounded` | scheduled board unit | workers |
| `worker` | `tournament/scheduling.py:_run_unit_cache_first` | subprocess run (cache MISS only) | — |
| `slot` | `proposer/best_of_n.py:_run_one_slot` | best-of-N slate slot | — |

### 7b. Nesting — inferred, not threaded

harmonograf builds the span tree from `AgentInvocationStarted.parent_invocation_id`
(`harmonograf_server/ingest.py:_on_agent_invocation_started`). A span reads its
parent from a module-level `contextvars.ContextVar` (`_current_span_id`) and
sets itself as the current id for its body. Because `asyncio` copies the
context when a task is created, a `gather`-fanned child (a board unit, a slate
slot) inherits its enclosing span **automatically** — the fan-out renders as
nested, overlapping lifelines with no explicit parent bookkeeping and no new
parameter on any intervening signature. The emitter itself is reached the same
way: `evolve_n_rounds` binds it to a second contextvar (`_current_emitter`,
`set_current_emitter`) once, and every deep call site opens spans through the
ambient `meta_span(...)` helper. Emitter unbound → `meta_span` is a no-op,
identical to the proposer emits' `meta_loop_emitter is None` path.

The **matchup** span is opened BEFORE its semaphore (`async with (meta_span(...),
semaphore)`), so the gap between the matchup's start and its first worker child
(which begins only after the semaphore admits the unit) is the QUEUE WAIT — a
distinct visual, no separate acquire span needed.

Metadata (`meta`) is **ids / phase-names / timings only** — never board content,
never scores beyond what the §2b judge spans already carry. It rides the
completed envelope's `summary` (a small JSON blob); a worker stamps the run's
goldfive `adk_session_id` there so a harmonograf user can cross-jump into the
board run's own session (§2a). **Deviation — no pid.** No span stamps a
subprocess pid today; the worker span carries `run_id` / `side` / `entry_id` and
the run's `adk_session_id` only (an earlier `_SpanHandle` docstring implied a pid
that was never emitted).

### 7c. Disciplines (all tested)

* **Best-effort, never load-bearing.** Every emit goes through
  `_fan_emit`, which swallows per-sink failures; a raising harmonograf sink
  never fails a round (§6). The hot path never awaits the network:
  `HarmonografSink.emit` is a constant-time buffer append its transport drains
  off-thread, and the JSONL sink is a local file append — no emit path blocks
  on the stream, so no buffer-and-drop layer is needed on top.
* **Pairing.** The completed half fires in a `finally`, so an exception, a
  `CancelledError`, or a crash mid-body still closes the span (with
  `outcome="error:<Exc>"` / `"cancelled"`). The completed emit is `shield`ed so
  a task cancellation propagating through the `finally` does not abort the
  closing emit mid-flight — the `CancelledError` still propagates to the caller,
  it is only deferred past the shielded emit. This is best-effort, not a
  guarantee: it holds only while the loop lives — under a HARD loop teardown
  (the event loop closing while the shielded emit is still pending) the closing
  envelope can be destroyed un-emitted. The pairing discipline covers the
  common in-loop cancel, not loop death.
* **Bounded memory.** The span id is a single contextvar string a context
  manager sets-and-resets; there is no per-round-growing span registry. The
  emitter's only mutable state is its monotonic sequence counter.
* **Determinism untouched.** Spans are a RENDERING, never an input:
  `runtime/progress_log.py` remains the SSE / liveness `seq` source of truth,
  and nothing ordering-pinned (RoundLog, journal, goldens, progress seq) routes
  through the emitter. Spans may emit in completion order — harmonograf orders
  by timestamp.
* **Teardown.** The ambient emitter binding is reset and the emitter closed in
  `evolve_n_rounds`' teardown `finally` (before the harmonograf supervisor
  stops); no daemon threads beyond the supervisor's own are spawned.

### 7d. The proposer / judge lifelines are IN the tree

The §2b proposer / judge emits predate the structural spans. Each still emits a
paired `AgentInvocation{Started,Completed}`, but originally serialised its
payload JSON into the STARTED envelope's `parent_invocation_id` (goldfive ships
no `ProposerCallStarted` of its own) — the very field harmonograf reads as the
tree PARENT (`ingest.py:_on_agent_invocation_started`). A JSON blob matches no
invocation, so those lifelines rendered as **detached orphan roots** beside the
span tree — the single most-watched lifeline (the proposer) sitting outside the
unified picture.

They now parent exactly like a structural span: the started envelope's
`parent_invocation_id` carries the ambient `_current_span_id`, so a proposer
call nests under its propose / `slot` span and a judge under `gate`. The payload
moved to the COMPLETED envelope's `summary` — the same home the structural spans
use for `meta`: `_emit_paired_started` stashes the started payload by invocation
id and `_emit_paired_completed` folds it in under the completed metrics. Absent
an ambient span (a bare `propose_experiment` in a unit test) the parent is empty
— the prior root behaviour, preserved.

**Back-compat.** Old `meta_loop_events.jsonl` files still carry blob parents on
their proposer/judge started lines; harmonograf tolerates them (a non-matching
parent is simply treated as a root). The one zicato-side reader of that JSONL —
`read_meta_loop_session_id` (§2b) — reads only `session_id` off the first line,
never the payload or the parent, so it is unaffected by either representation.

Coverage is intentionally scoped to the concurrency-bearing seams. The
sequential `context-build` and `persist` seams, and a dedicated `tournament`
wrapper phase (matchups nest directly on the round span today), are not yet
instrumented — a follow-up, not a correctness gap.
