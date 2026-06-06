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
  `zicato builder` (`dashboard/server.py:_ensure_workspace_harmonograf`)
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
`resolve_harmonograf_grpc_target` and the `ZICATO_HARMONOGRAF_GRPC` env).

Failure isolation is absolute: a missing `harmonograf_server` dep, a
port-bind failure, or any startup exception yields a **no-op handle**
(`web_url=""`) and a logged WARNING. Evolve and the dashboard always
continue — harmonograf is additive, never load-bearing.

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
