# zicato — runtime v2: the channel abstraction

> **This document is a design proposal with a staged delivery plan; it is not a
> description of the shipped system.** It generalizes zicato's one explicit
> producer-consumer protocol — the control channel — into a single **Channel**
> abstraction that every cross-process exchange uses, replacing hand-rolled
> mutable snapshot files with event-sourced logs. Source comments cite the
> numbered phases below as "RUNTIME-V2.md Phase *N*", so that numbering is
> fixed and each phase is named by what it delivers. Companion to
> [`RUNTIME.md`](RUNTIME.md), [`ROBUSTNESS.md`](ROBUSTNESS.md),
> [`STORAGE.md`](STORAGE.md), and the [`REIMPLEMENTATION.md`](REIMPLEMENTATION.md)
> roadmap.

## Context

zicato runs as separated processes — orchestrator, dashboard (Starlette), Rust
supervisor, subprocess workers — coordinating **only through the filesystem**
(`.zicato/`). That coordination runs today through hand-rolled producer-consumer
channels, each with its own file format, write discipline, atomicity guarantee,
and polling or inotify path:

- **live state** — orchestrator/runner *produce* `heartbeat.json`,
  `active_runs/*`, `active_tournament.json`; dashboard + supervisor *consume*.
- **control commands** — dashboard *produces* `control/*`; the orchestrator
  *consumes* them at its safe points
  (`src/zicato/runtime/control_consumer.py`). The consumer was unwired when
  this proposal was written, which is the gap the staged plan below closes.
- **kill markers** — parent *produces* `control/kill_requests/<run>`; supervisor
  *consumes*.
- **telemetry** — workers *produce* `events.jsonl`; reducer + dashboard *consume*.
- **index dual-write** — orchestrator *produces* canonical files; index *projects*.

Every recurring **liveness bug** has the same root: mutable snapshot files with
multiple writers and hand-rolled consumers.

- `_publish_active_tournament` does a **read-modify-write while the runner also
  writes the same file**, so one writer's update is lost.
- Three separate atomic-write implementations exist, and the weakest of them
  writes the most frequently updated file.
- Reading `events.jsonl` on the server-sent events (SSE) hot path emits a
  spurious `run_log` frame ahead of `state_change`, so consumers see the two out
  of order.
- The client rebuilds the DOM on a heartbeat that carries no change, which makes
  the view flash; only comments enforce the discipline that prevents it.
- The dashboard derives state from three sources that disagree — the live
  envelope, the settled record, and the index — which produces brackets stuck on
  the seeding state (issue #16).
- Liveness is defined as freshness of the heartbeat **timestamp**, which reports
  a healthy orchestrator as dead during a slow model call, and the watchdog then
  kills it.

## The abstraction

A single `Channel` abstraction sits over the storage layer's one `_atomic` write
seam, in two shapes:

- **`EventLog`** — append-only, **single-writer**, each entry a typed record with
  a monotonic `seq`. `append(event)` (one atomic write), `read(from_seq)`,
  `tail()`. Consumers hold a cursor.
- **`CommandQueue`** — many-writer enqueue, single-consumer **claim-once**:
  `enqueue(cmd)`, `claim() -> cmd | None` (atomic move to an archive so each
  command fires exactly once). The existing control protocol is the first
  instance.

Both shapes are atomic by construction and concurrency-safe across processes:
there is no shared memory, and the filesystem operations themselves provide the
synchronization. Each entry is a typed record that describes itself.

## From mutable snapshots to event-sourced views

Today a producer overwrites a mutable snapshot and a consumer reads it. Two
writers race for the same file, and the live view a consumer builds can
contradict the settled record a producer wrote.

Under the channel abstraction a producer **appends events to a single-writer
log**, and the consumer **folds the log into a view**. A settled state is the
terminal event in that log. The log is the single source of truth, and every
view is derived from it, so views cannot contradict each other.

## What each property gives

- **A single writer appending to a log** removes the `active_tournament`
  read-modify-write race, torn writes, and lost updates.
- **Views folded from events** remove the disagreement between the live view and
  the settled record, because a settled state is the log's last event and no
  separate settled record exists to contradict it. This is the source of the
  brackets stuck on the seeding state (issue #16).
- **A monotonic `seq` cursor** gives correct server-sent-events ordering, since
  entries stream in append order and the hot path stops reading `events.jsonl`.
  The same cursor gates rendering: a view re-renders when, and only when, `seq`
  has advanced, so a repeated frame causes no flash. It also makes gaps and
  staleness detectable.
- **Using `seq` as the liveness signal** gives the watchdog a better test than
  heartbeat freshness. Asking whether the producer's `seq` is advancing cannot
  report a busy orchestrator as dead during a slow model call, which removes the
  whole class of watchdog-kill failures rather than one instance of it.

## Channel inventory (migration targets)

| hand-rolled channel today | replacement |
|---|---|
| `active_tournament.json` snapshot (dual-writer) | **one tournament `EventLog`** (orchestrator single-writer) |
| `heartbeat.json` + `active_runs/*` | a runtime `EventLog` |
| `control/*` commands | a **`CommandQueue`** (wire the consumer) |
| `control/kill_requests/*` | a `CommandQueue` |
| `events.jsonl` (worker) | already append-only — adopt the `EventLog` reader |
| meta-loop emitter | an `EventLog` |
| index dual-write | the index **folds the same logs** (closes canonical-vs-derived) |

## Tournament log — event schema (the first migration)

`TournamentStarted(structure, competitors)` · `MatchupStarted(matchup_id,
sides)` · `BoardUnitProgress(matchup_id, entry, done, total, partial)` ·
`MatchupSettled(matchup_id, result)` · `TournamentSettled(decision, standings)`.
Each carries `seq` + `ts`. The dashboard folds them into the structure view;
`TournamentSettled` is the terminal state. The runner is the single writer.

## Phased plan

1. **Phase 1 — build the channel abstraction.** Add `runtime/channel.py`
   (`EventLog` and `CommandQueue`) over the storage seam, with tests. Nothing
   migrates onto it yet.
2. **Phase 2 — carry the control protocol on a `CommandQueue`.** Wire the
   consumer into the evolve loop at safe points for pause, skip-round, promote,
   and reject. Record an operator promote or reject as an explicit override in
   the journal and the outcome. Handle `rubric_replacement` as a contract edit
   that rolls the epoch rather than as a silent patch. This gives the control
   protocol its missing consumer and delivers operator steering.
3. **Phase 3 — move tournament live state onto an `EventLog`.** The orchestrator
   and runner become the single writer of tournament events; the dashboard folds
   the log into the structure view; the producer-consumer parity tests
   (`live_protocol.test.mjs`) show the rendering is identical. This is the
   largest single improvement to liveness reporting.
4. **Phase 4 — move the heartbeat and `active_runs` onto a `Channel`.** The
   server-sent-events stream carries a `seq` cursor, and the watchdog derives
   liveness from `seq` advance.
5. **Phase 5 — fold the logs into the index.** The index becomes a pure
   projection of the same source.

Phases 1 to 3 form the first execution slice; phases 4 and 5 follow.

## Compatibility and migration

The on-disk live-state format changes from snapshots to logs, so the migration
does not preserve behavior. The test suite gates it: the producer-consumer
parity tests assert that the dashboard renders identically, and new tests cover
the log path, with producer and consumer migrated together. A compatibility
reader accepts the snapshot format during the transition, and the Rust
supervisor's state reader, which already tolerates partial writes, adapts to the
log.

## Non-goals

- **No external message broker is introduced.** The filesystem carries every
  exchange, matching the architecture in which the filesystem is canonical, and
  the change adds no runtime dependency.
- **What the dashboard shows is unaffected.** Only the way live state is
  produced and consumed changes; the parity tests show that the rendered output
  is identical.
