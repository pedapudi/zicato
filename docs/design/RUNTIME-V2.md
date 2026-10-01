# zicato — runtime v2: the channel abstraction

> **Status: design proposal with a staged delivery plan, partly shipped.**
> Phases 1, 3 and 4 shipped; phase 2 shipped on the control module's own
> claim-once mechanics rather than on the `CommandQueue` class; phase 5 did
> not ship. [Delivery status](#delivery-status) states what the code does for
> each phase. The Context section records the problems the design set out to
> remove. It generalizes zicato's one explicit
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
(`.zicato/`). Before this design, that coordination ran through hand-rolled
producer-consumer channels, each with its own file format, write discipline, atomicity guarantee,
and polling or inotify path:

- **live state** — orchestrator/runner *produce* `heartbeat.json`,
  `active_runs/*`, `active_tournament.events.jsonl`; the dashboard consumes all
  three, and the supervisor consumes `heartbeat.json` and `active_runs/*`.
- **control commands** — dashboard *produces* `control/*`; the orchestrator
  *consumes* them at its safe points
  (`src/zicato/runtime/control_consumer.py`). The consumer was unwired when
  this proposal was written; phase 2 wired it.
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

Under a snapshot file, a producer overwrites a mutable snapshot and a consumer
reads it. Two
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

| hand-rolled channel | proposed replacement | shipped state |
|---|---|---|
| Tournament live updates | an `EventLog` | shipped: `runtime/active_tournament.events.jsonl`, folded by the Python reader (`zicato.runtime.tournament_log`) |
| `heartbeat.json` + `active_runs/*` | a runtime `EventLog` | partly shipped: the progress log `runtime/progress.events.jsonl` carries the liveness `seq`; `heartbeat.json` (which mirrors that `seq`) and `active_runs/*` remain snapshot files |
| `control/*` commands | a **`CommandQueue`** (wire the consumer) | consumer wired (`zicato.runtime.control_consumer`); claim-once is an atomic move into `control_log/` in `zicato.runtime.control`, and `CommandQueue` is unused |
| `control/kill_requests/*` | a `CommandQueue` | not migrated: one JSON marker per run, written by `request_worker_kill` and cleared by the supervisor |
| `events.jsonl` (worker) | already append-only — adopt the `EventLog` reader | not migrated |
| meta-loop emitter | an `EventLog` | not migrated |
| index dual-write | the index **folds the same logs** (closes canonical-vs-derived) | not shipped |

## Tournament log — event schema

The proposal named five typed events (tournament started, matchup started,
board-unit progress, matchup settled, tournament settled). The shipped log
uses two record types, each carrying `seq` and `ts`:

- **`Snapshot`** — the complete `ActiveTournament` display state, written by
  `write_active_tournament`.
- **`Update`** — the fields that changed (`fields`) and the complete
  replacement rows for changed entries (`entries`, keyed by row position),
  written by `_update_active_tournament`.

A reader replays the last `Snapshot` and every later `Update`; it applies
replacements and computes no tournament rules. The workspace writer
(`WorkspaceLock.tournament_log`, `zicato.runtime.lock`) is the single writer.
`fold_active_tournament` returns `None` when no log exists.

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

## Delivery status

Verified against the code:

- **The channel abstraction (phase 1) — shipped.** `zicato.runtime.channel` defines `Event`,
  `EventLog` and `CommandQueue` over the storage atomic-write layer
  (`CommandQueue.claim` uses `zicato.storage.atomic_claim`). Covered by
  `tests/test_runtime_channel.py`.
- **The control protocol (phase 2) — shipped without `CommandQueue`.** `zicato.runtime.control_consumer`
  drains `pause_epoch`, `skip_round` and `rubric_replacement` between rounds,
  claims `skip_round` at round start, and applies `promote/<gen>` and
  `reject/<gen>` at the gate as a recorded operator override. A
  `rubric_replacement` rolls the epoch. Claim-once and the audit archive live
  in `zicato.runtime.control` (`consume_command` moves the file into
  `control_log/`); no production code instantiates `CommandQueue`.
- **Tournament live state (phase 3) — shipped.** Tournament live state is the event log described
  above; the dashboard parity tests in
  `src/zicato/dashboard/static/test/live_protocol.test.mjs` cover the reader.
- **Heartbeat and liveness (phase 4) — shipped as an added log.** `zicato.runtime.progress_log`
  appends one event per genuine loop transition and a terminal event on a
  clean end (`Settled`, or `Stopped` for a budget or circuit-breaker cut). The heartbeat carries the log's tail `seq`, the
  server-sent-events stream publishes `seq` and a terminal flag, and the
  supervisor's `SeqLiveness` tracker ages the last `seq` change. The
  supervisor's heartbeat checks only warn; they never signal the
  orchestrator. `heartbeat.json` and `active_runs/*` remain snapshot files.
- **Folding the logs into the index (phase 5) — not shipped.** The
  index is projected from the canonical record files; it reads none of
  these logs.

## Compatibility and migration

The on-disk live-state format changed from snapshots to logs for the
tournament state, so that migration did not preserve the file format. The test suite gates it: the producer-consumer
parity tests assert that the dashboard renders identically, and tests cover
the log path, with producer and consumer migrated together.

## Non-goals

- **No external message broker is introduced.** The filesystem carries every
  exchange, matching the architecture in which the filesystem is canonical, and
  the change adds no runtime dependency.
- **What the dashboard shows is unaffected.** Only the way live state is
  produced and consumed changes; the parity tests show that the rendered output
  is identical.
