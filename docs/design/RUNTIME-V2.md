# zicato — runtime v2: the channel abstraction

> **Status: proposal / design note — execution in progress on `feat/runtime-v2`.**
> Generalizes zicato's one explicit producer-consumer protocol (the control
> channel) into a single **Channel** abstraction that every cross-process
> exchange uses, turning ad-hoc mutable snapshots into event-sourced logs.
> Companion to [`RUNTIME.md`](RUNTIME.md), [`ROBUSTNESS.md`](ROBUSTNESS.md),
> [`STORAGE.md`](STORAGE.md), and the [`REIMPLEMENTATION.md`](REIMPLEMENTATION.md)
> roadmap.

## Context

zicato runs as separated processes — orchestrator, dashboard (Starlette), Rust
supervisor, subprocess workers — coordinating **only through the filesystem**
(`.zicato/`). Today that coordination is a dozen hand-rolled producer-consumer
channels, each with its own file format, write discipline, atomicity story, and
polling/inotify path:

- **live state** — orchestrator/runner *produce* `heartbeat.json`,
  `active_runs/*`, `active_tournament.json`; dashboard + supervisor *consume*.
- **control commands** — dashboard *produces* `control/*`; orchestrator should
  *consume* (the consumer is **unwired** — the dashboard's pause/skip/promote/
  reject buttons currently write files nobody reads).
- **kill markers** — parent *produces* `control/kill_requests/<run>`; supervisor
  *consumes*.
- **telemetry** — workers *produce* `events.jsonl`; reducer + dashboard *consume*.
- **index dual-write** — orchestrator *produces* canonical files; index *projects*.

Every recurring **liveness bug** traces to the same root — *mutable snapshot
files with multiple writers and ad-hoc consumers*:

- `_publish_active_tournament` does a **read-modify-write while the runner also
  writes the same file** (lost-update race);
- three forked atomic-write impls, the weakest writing the hottest file;
- reading `events.jsonl` in the SSE hot path emits a spurious `run_log` before
  `state_change` (ordering hazard);
- DOM rebuilds on a no-op heartbeat (flashing), enforced only by comments;
- the dashboard derives state from live envelope **vs** settled record **vs**
  index, which disagree (issue #16, the bracket "stuck on seeding" class);
- liveness = "is the heartbeat **timestamp** fresh?" — which false-positives on a
  slow LLM call (the watchdog-kills-the-orchestrator bug).

## The abstraction

One `Channel` over the single storage `_atomic` seam, in two shapes:

- **`EventLog`** — append-only, **single-writer**, each entry a typed record with
  a monotonic `seq`. `append(event)` (one atomic write), `read(from_seq)`,
  `tail()`. Consumers hold a cursor.
- **`CommandQueue`** — many-writer enqueue, single-consumer **claim-once**:
  `enqueue(cmd)`, `claim() -> cmd | None` (atomic move to an archive so each
  command fires exactly once). The existing control protocol is the first
  instance.

Both are atomic by construction, concurrency-safe across processes (no shared
memory — file ops are the sync), and self-describing (typed records).

## The model shift: snapshots → event-sourced views

Today a producer **overwrites a mutable snapshot**, a consumer reads it — two
writers race, and consumer vs producer disagree (live vs settled).

v2: a producer **appends events to a single-writer log**; the consumer **folds
the log into a view**. "Settled" is just the terminal event. There is **one
source of truth** (the log); the view is derived and consistent by construction.

## Liveness reliability — what each property buys

- **Single-writer append-only** → eliminates the `active_tournament`
  read-modify-write race, torn writes, lost updates.
- **Event-sourced views** → eliminates live-vs-settled disagreement (issue #16 /
  the bracket bug class — there is no separate live envelope vs settled record to
  contradict each other; "settled" = the last event).
- **Monotonic `seq` cursor** → correct SSE ordering (stream entries in append
  order; stop reading `events.jsonl` in the hot path) + principled digest-gating
  (re-render **iff `seq` advanced** — no flashing) + gap/staleness detection.
- **`seq`-as-liveness** → a truer watchdog signal than a heartbeat timestamp:
  "is the producer's `seq` advancing?" can't false-positive on a slow LLM call —
  it removes the *category* of the watchdog-kill bug, not just the instance.

## Channel inventory (migration targets)

| today (ad-hoc) | v2 |
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

1. **Channel abstraction** — `runtime/channel.py` (`EventLog` + `CommandQueue`)
   over the storage seam, + tests. No migration yet.
2. **Control protocol as a `CommandQueue`** — wire the consumer into the evolve
   loop at **safe points** (pause / skip-round / promote / reject); record
   promote/reject as explicit operator **overrides** in the journal/outcome;
   handle `rubric_replacement` as a **contract edit that rolls the epoch** (not a
   silent patch). Completes the dead producer-consumer + delivers operator
   steering.
3. **Tournament live state → `EventLog`** — orchestrator/runner single-writer
   appends tournament events; the dashboard folds the log into the structure
   view; the **producer-consumer parity tests** (`live_protocol.test.mjs`) prove
   identical rendering. (The big liveness win.)
4. **heartbeat / active_runs → `Channel`; `seq`-cursor SSE; watchdog
   liveness-via-`seq`.**
5. **Index folds the logs** — pure projection of the same source.

Phases 1–3 are the first execution slice; 4–5 are follow-ups.

## Compatibility + migration

The on-disk live-state format changes (snapshots → logs) — **not
behavior-preserving**, so it is gated by the **test suite** (especially the
producer-consumer parity tests, which assert the dashboard renders identically)
plus new tests, migrating producer **and** consumer together. A compat reader
tolerates old snapshots during the transition; the Rust supervisor's state
reader (already partial-write-tolerant) adapts to the log.

## Non-goals

- **Not an external message broker.** The filesystem *is* the bus — matching the
  canonical-filesystem architecture; no new runtime dependency.
- **Not changing what the dashboard shows** — only *how* the live state is
  produced/consumed. Rendered output stays identical, proven by the parity tests.
