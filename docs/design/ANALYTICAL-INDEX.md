# Analytical index

This document specifies the `.zicato/index.db` **SQLite analytical
index** — a derived, fully-rebuildable query surface over the
filesystem-canonical workspace.

The index exists because cross-run questions ("which generations
moved `CONFABULATION_RISK`?", "what is the proposer's hypothesis
match-rate across this epoch?", "show me every tournament that
rejected on a pass-rate regression") are **queries**, not
file-walks. The filesystem layout — one JSON file per artifact,
inspectable with `ls` and `cat` — is the right shape for the
operator's primary debugging interface and stays that way. The
index is a sidecar: a cache that makes the cross-cutting views
fast without ever becoming the source of truth.

The shape of this design was anticipated in
[RATIONALE.md §7](RATIONALE.md#7-why-filesystem-layout-not-sqlite):
"When pattern queries become a bottleneck, the right move is to
add an index sidecar (one SQLite file used as a cache,
regenerable from the filesystem), not to make the filesystem
layout the index." This document is that index sidecar made
concrete.

This document covers:

- Why the index exists and what it is *not* (§1).
- The discipline: files canonical, index derived, dual-write +
  full rebuild (§2).
- The full schema — eight tables (§3).
- `zicato reindex` — the rebuild command (§4).
- Where SQLite is and is NOT used in zicato (§5).
- The Rust supervisor's read path via `rusqlite` (§6).

## 1. Why an index

### 1.1 The cross-run query problem

The filesystem layout in
[EPOCHS-AND-JOURNALING.md §2](EPOCHS-AND-JOURNALING.md#2-storage-layout)
is excellent for the operator's per-artifact loop: open one
`experiment.json`, read one `journal.md`, `cat` one `loss.json`.
It is poor for any question that ranges across many artifacts.

Consider these operator questions:

- "Across the whole epoch, which generations had the proposer
  predict `CAPABILITY_MISMATCH` would drop, and did it?"
- "Which board entries never differentiate parent from candidate
  — i.e. always score identically on both sides?"
- "What is the running hypothesis match-rate, round over round?"
- "How much wall-clock and how many auxiliary LLM calls has this
  epoch's tournament cost so far?"
- "Across all epochs, which mutation points correlate with a
  promote?"

Every one of these is a `GROUP BY` / `JOIN` over data that lives
scattered across `epochs/*/generations/*/experiment.json`,
`epochs/*/generations/*/runs/*/loss.json`, and
`epochs/*/generations/*/gen_score.json`. Answering them by
file-walk means: enumerate every generation directory, open and
parse every JSON file, hold the union in memory, and filter.
That is `O(generations × entries)` file opens for a single
question, and it gets re-paid on every question.

The dashboard ([DASHBOARD.md](DASHBOARD.md)) makes this worse:
its tournament-detail analytics (the hypothesis ledger, the
mutation heat map, the cost panel — see
[TOURNAMENT.md §4](TOURNAMENT.md#4-tournament-detail-analytics))
are *all* cross-run aggregates, recomputed every time a panel
refreshes. A file-walk per SSE update does not scale past a
toy epoch.

### 1.2 What the index is

`.zicato/index.db` is a single SQLite file holding a **relational
projection** of the workspace's canonical artifacts. Every row in
every table is derived from a file under `.zicato/epochs/` (or
`.zicato/lineage.json`). The index holds no fact that is not also
on disk in a canonical file.

With the index in place, the questions in §1.1 become single SQL
statements:

```sql
-- hypothesis match-rate, round over round, for one epoch
SELECT round, AVG(matched) AS match_rate
FROM hypothesis_movements
WHERE epoch_id = '2026-05-15_e1'
GROUP BY round
ORDER BY round;
```

```sql
-- board entries that never differentiate parent from candidate
SELECT entry_id
FROM metric_counts
GROUP BY entry_id
HAVING COUNT(DISTINCT drift_loss) <= 1;
```

The cost of the cross-run question drops from
`O(generations × entries)` file opens to one indexed query.

### 1.3 What the index is NOT

- **Not the source of truth.** Every table is derived. If
  `index.db` is deleted, `zicato reindex` reconstructs it exactly
  from the filesystem. Nothing is lost.
- **Not a write target for orchestration logic.** The
  orchestrator never *reads back* a decision from the index. The
  tournament gate reads `gen_score.json`; the resume protocol
  reads `experiment.json`; the proposer reads `patterns/*.json`.
  All of those are files. The index is for *views*, not for
  *control flow*.
- **Not a replacement for the filesystem layout.** `ls`, `cat`,
  `grep`, `git diff` on `.zicato/` all still work and are still
  the operator's first-class interface. The index is additive.
- **Not per-run event storage.** Run telemetry is `events.jsonl`,
  one file per run. The index holds *reduced* per-run features
  (the `LossProfile` projection), never raw events. See §5.

The one-sentence summary: **the filesystem is canonical and
human-legible; the index is derived and machine-fast; they never
disagree because the index is always rebuildable from the
files.**

## 2. The discipline

The index is only safe if three rules hold without exception.

### 2.1 Files are canonical

Every fact has exactly one canonical home: a file under
`.zicato/`. `experiment.json` is the canonical Experiment.
`loss.json` is the canonical LossProfile. `gen_score.json` is
the canonical generation score. `lineage.json` is the canonical
cross-epoch DAG. The index never holds a fact that did not come
from one of these files.

This means: a contributor adding a new artifact adds a new
*file*, then optionally a new *index table* projecting it. The
file lands first; the table is downstream.

### 2.2 The index is derived and fully rebuildable

`zicato reindex` (§4) drops every table and reconstructs the
whole database by walking the filesystem. This is the
correctness backstop:

- If the index is ever suspected stale or corrupt, `reindex`
  fixes it — no manual repair, no migration.
- If the schema changes between zicato versions, `reindex`
  rebuilds under the new schema; there is no index migration
  path to maintain because the index is disposable.
- If an operator hand-edits a file under `.zicato/epochs/`
  (e.g. fixes a malformed `experiment.json`), `reindex` brings
  the index back in line.

A rebuild is `O(total artifacts)` file reads — the same cost as
*one* cross-run file-walk, paid once, after which every query is
indexed. For a large workspace (multiple epochs, hundreds of
generations) a full reindex is seconds, not minutes.

### 2.3 The orchestrator dual-writes live

Waiting for an explicit `reindex` after every round would leave
the dashboard's analytics stale mid-epoch. So the orchestrator
**dual-writes**: whenever it writes a canonical file, it also
writes the corresponding index rows, in the same logical step.

```
round completes
        │
        ▼
write generations/v5/experiment.json (outcome block)   ── canonical
write generations/v5/gen_score.json                    ── canonical
        │
        ▼
upsert into index.db:
   experiments(v5, ...)            ── derived
   hypothesis_movements(v5, ...)   ── derived
   runs(v5, *, ...)                ── derived
   tournaments(round 5, ...)       ── derived
        │
        ▼
broadcast SSE 'round_finished'  (dashboard reads index)
```

The dual-write is **not transactional across the file and the
DB** — the file write and the DB write are two separate
operations. The ordering rule makes this safe:

> **The canonical file is always written first; the index row
> is written second.**

If the orchestrator crashes between the two, the index is
*behind* the filesystem — never *ahead*. A behind index is
self-healing: the next `reindex` (or the resume protocol's
reindex-on-restart, §4.3) catches it up. An ahead index — a
row referencing a file that was never written — would be a
phantom, and the ordering rule makes that impossible.

The index write itself uses a SQLite transaction so the *set*
of rows for one round lands atomically: a reader never sees half
a round's rows.

### 2.4 Single writer

Only the orchestrator (`zicato evolve`, and the one-shot
subcommands `analyze` / `tournament` when run standalone) writes
`index.db`. The Rust supervisor opens the database **read-only**
(§6). `zicato reindex` is a writer but acquires the workspace
lock (`.zicato/runtime/lock.json`) first, so it never races a
live `evolve`. SQLite's own file locking is the backstop, but
the workspace lock is the primary discipline — consistent with
the single-writer-per-file rule the rest of the runtime layer
follows (see [RUNTIME.md](RUNTIME.md)).

## 3. Schema

The index has **eight tables**, mirroring the artifact hierarchy:
`epochs` → `generations` → `experiments` → `patches`, and
`generations` → `runs` → `loss_profiles` / `metric_counts`, with
`tournaments` as the per-round comparison record.

```
┌──────────┐      ┌──────────────┐      ┌──────────────┐      ┌──────────┐
│  epochs  │─1:N─▶│ generations  │─1:1─▶│ experiments  │─1:N─▶│ patches  │
└──────────┘      └──────┬───────┘      └──────────────┘      └──────────┘
                         │
                         │ 1:N
                         ▼
                  ┌──────────────┐      ┌────────────────┐
                  │     runs     │─1:1─▶│  loss_profiles │
                  └──────┬───────┘      └────────────────┘
                         │ 1:N
                         ▼
                  ┌────────────────┐
                  │  metric_counts │
                  └────────────────┘

┌──────────────┐
│ tournaments  │   one row per round: parent vs candidate, gate verdict
└──────────────┘
```

All `*_id` columns are the same string identifiers used in the
filesystem layout (`epoch_id` is the epoch directory name,
`generation` is `v0` / `v1` / ..., `entry_id` is the board entry
id). This makes any index row trivially traceable back to its
canonical file.

### 3.1 `epochs`

One row per epoch directory. Projection of `lineage.json` plus
the epoch's `scoring.json` and `EpochConfig`.

| Column | Type | Source |
|---|---|---|
| `epoch_id` | TEXT PK | epoch directory name |
| `started_at` | TEXT | `lineage.json` |
| `closed_at` | TEXT NULL | `lineage.json` (NULL while open) |
| `v0_parent` | TEXT NULL | `lineage.json` — `epoch:gen` of the predecessor's head |
| `contract_hash` | TEXT | `EpochConfig.contract_hash` |
| `promoted_count` | INTEGER | derived from `lineage.json` |
| `rejected_count` | INTEGER | derived from `lineage.json` |
| `final_generation` | TEXT NULL | `lineage.json` |

### 3.2 `generations`

One row per generation directory under any epoch.

| Column | Type | Source |
|---|---|---|
| `epoch_id` | TEXT | (FK → `epochs`) |
| `generation` | TEXT | `v0` / `v1` / ... |
| `round` | INTEGER | round that produced it (0 for `v0`) |
| `parent_generation` | TEXT NULL | the generation it was proposed against |
| `is_promoted` | INTEGER | 1 if this generation was promoted |
| `weighted_drift` | REAL | `gen_score.json` |
| `pass_rate` | REAL | `gen_score.json` |
| `score` | REAL | `gen_score.json` — the tournament scalar |
| `computed_at` | TEXT | `gen_score.json` |

Primary key `(epoch_id, generation)`.

### 3.3 `experiments`

One row per `experiment.json` (i.e. one per generation except
the `v0` baseline, which has no experiment).

| Column | Type | Source |
|---|---|---|
| `epoch_id` | TEXT | (FK) |
| `generation` | TEXT | (FK → `generations`) |
| `round` | INTEGER | `outcome.round_number` |
| `core_idea` | TEXT | `hypothesis.core_idea` |
| `why` | TEXT | `hypothesis.why` |
| `modulating` | TEXT (JSON array) | `hypothesis.modulating` — mutation-point ids |
| `expected_pass_rate_low` | REAL | `hypothesis.expected_pass_rate_delta.low` |
| `expected_pass_rate_high` | REAL | `hypothesis.expected_pass_rate_delta.high` |
| `tournament_decision` | TEXT NULL | `outcome.tournament_decision` (NULL until tournament runs) |
| `rejection_reason` | TEXT NULL | `outcome.rejection_reason` |
| `drift_loss_delta` | REAL NULL | `outcome.drift_loss_delta` |
| `pass_rate_delta` | REAL NULL | `outcome.pass_rate_delta` |
| `wall_clock_seconds` | REAL NULL | `outcome.wall_clock_seconds` |
| `override_by_operator` | INTEGER | 1 if the gate verdict was operator-overridden (see [DASHBOARD.md §5.3](DASHBOARD.md#53-command-catalogue-and-safe-point-semantics)) |

Primary key `(epoch_id, generation)`. The `modulating` list is
stored as a JSON-encoded array; SQLite's `json_each` makes it
queryable (the mutation heat map in
[TOURNAMENT.md §4.5](TOURNAMENT.md#45-mutation-heat-map) uses
exactly this).

### 3.4 `patches`

One row per `patches/{patch_id}.json` file.

| Column | Type | Source |
|---|---|---|
| `patch_id` | TEXT PK | patch file `id` |
| `epoch_id` | TEXT | (FK) |
| `generation` | TEXT | (FK → `generations`) |
| `mutation_id` | TEXT | patch `mutation_id` |
| `op` | TEXT | `replace` (the v0 op) |
| `rationale` | TEXT | patch `rationale` |

Patch *content* (`new_content`, `new_numeric`, `new_enum`) is
deliberately **not** indexed — it can be large and is never a
query key. An operator inspecting patch content opens the
canonical `patches/{patch_id}.json` file (or runs
`zicato show`). The index holds only what gets filtered or
joined on.

### 3.5 `runs`

One row per `runs/{entry_id}/` directory — i.e. one per
(generation × board entry).

| Column | Type | Source |
|---|---|---|
| `epoch_id` | TEXT | (FK) |
| `generation` | TEXT | (FK → `generations`) |
| `entry_id` | TEXT | board entry id |
| `side` | TEXT | `parent` or `candidate` — which side of the tournament this run was |
| `events_path` | TEXT | relative path to `events.jsonl` (for the harmonograf drill-down) |
| `aborted` | INTEGER | `loss.json` — 1 if `RunAborted` |
| `runtime_ms` | INTEGER | `loss.json` |

Primary key `(epoch_id, generation, entry_id, side)`. The
`events_path` column is the join key between the index (the
competition view) and harmonograf (the execution view) — see
[TOURNAMENT.md §5](TOURNAMENT.md#5-the-harmonograf-split) and §5
below.

### 3.6 `loss_profiles`

One row per `loss.json` — the reduced per-run feature vector.

| Column | Type | Source |
|---|---|---|
| `epoch_id` | TEXT | (FK) |
| `generation` | TEXT | (FK) |
| `entry_id` | TEXT | (FK → `runs`) |
| `side` | TEXT | (FK → `runs`) |
| `drift_loss` | REAL | `LossProfile.drift_loss` |
| `pass_fail` | INTEGER NULL | `LossProfile.pass_fail` (NULL when no expectation) |
| `escalations` | INTEGER | `LossProfile.escalations` |
| `plan_revisions` | INTEGER | `LossProfile.plan_revisions` |
| `task_failure_ratio` | REAL | `LossProfile.task_failure_ratio` |
| `human_intervention_required` | INTEGER | `LossProfile.human_intervention_required` |

Primary key matches `runs`. This table is the scoring-side
projection; the per-entry A/B grid in
[TOURNAMENT.md §4.2](TOURNAMENT.md#42-per-entry-ab-grid) is a
self-join of `loss_profiles` on `entry_id` across the two
`side` values.

### 3.7 `metric_counts`

The drift counts, unpivoted into one row per
(run × drift kind) and (run × severity). The `LossProfile`
carries `drift_counts_by_kind` and `drift_counts_by_severity`
as dicts; storing them unpivoted makes them `GROUP BY`-able.

| Column | Type | Source |
|---|---|---|
| `epoch_id` | TEXT | (FK) |
| `generation` | TEXT | (FK) |
| `entry_id` | TEXT | (FK → `runs`) |
| `side` | TEXT | (FK → `runs`) |
| `metric_kind` | TEXT | `drift_kind` or `severity` |
| `metric_name` | TEXT | e.g. `DRIFT_KIND_CONFABULATION_RISK`, or `CRITICAL` |
| `count` | INTEGER | the count |

No single-column primary key; the natural key is
`(epoch_id, generation, entry_id, side, metric_kind,
metric_name)`. The drift-kind heatmap in
[DASHBOARD.md §4.6](DASHBOARD.md#46-drift-kind-heatmap) is a
`SUM(count) GROUP BY metric_name, round` over this table.

### 3.8 `tournaments`

One row per round — the parent-vs-candidate comparison record.

| Column | Type | Source |
|---|---|---|
| `epoch_id` | TEXT | (FK) |
| `round` | INTEGER | round number |
| `parent_generation` | TEXT | the reigning champion's id |
| `candidate_generation` | TEXT | the challenger's id |
| `parent_score` | REAL | parent `gen_score.json` |
| `candidate_score` | REAL | candidate `gen_score.json` |
| `decision` | TEXT | `promote` or `reject` |
| `rejection_reason` | TEXT NULL | gate's reason if rejected |
| `mode` | TEXT | `tournament` or `fast` |
| `wall_clock_seconds` | REAL | total round wall-clock |
| `aux_llm_calls` | INTEGER | auxiliary LLM calls spent on the round (proposer + emulator) |

Primary key `(epoch_id, round)`. This is the table that backs
the tournament bracket and the per-matchup detail in
[TOURNAMENT.md](TOURNAMENT.md): the bracket *is* `SELECT * FROM
tournaments WHERE epoch_id = ? ORDER BY round`.

### 3.9 A derived view: `hypothesis_movements`

The hypothesis ledger (proposer calibration — see
[TOURNAMENT.md §4.3](TOURNAMENT.md#43-hypothesis-ledger)) needs
the per-kind predicted-vs-actual match. That lives in
`experiment.json`'s `outcome.hypothesis_match`. It is projected
into a table populated alongside `experiments`:

| Column | Type | Source |
|---|---|---|
| `epoch_id` | TEXT | (FK) |
| `generation` | TEXT | (FK → `experiments`) |
| `round` | INTEGER | the round |
| `drift_kind` | TEXT | the kind the hypothesis predicted |
| `predicted_direction` | TEXT | `up` / `down` / `flat` |
| `predicted_magnitude` | TEXT NULL | `minor` / `moderate` / `major` |
| `actual_direction` | TEXT | observed direction |
| `actual_magnitude` | TEXT NULL | observed magnitude |
| `matched` | INTEGER | 1 if the outcome's `hypothesis_match[kind].matched` is true |

The `matched` column carries the **explicit sign+magnitude
match semantics** described in
[TOURNAMENT.md §4.3](TOURNAMENT.md#43-hypothesis-ledger): a
prediction counts as matched only when both the *direction*
(sign) and the *magnitude* bucket agree with the observed
movement. A "predicted down moderate, observed down minor" is
**not** a match — the sign is right but the magnitude bucket is
wrong. The index stores the already-decided `matched` boolean
(the decision is made by the tournament runner when it writes
the `outcome` block); it also stores the four raw
predicted/actual columns so the dashboard can show *why* a
prediction missed without re-deriving the rule.

It is listed here as a ninth physical table rather than a SQL
`VIEW` because it is populated by the dual-write and queried
hot; a view would re-parse JSON on every read.

## 4. `zicato reindex`

`zicato reindex` rebuilds `index.db` from the filesystem. It is
the correctness backstop for the whole index design.

```
zicato reindex [--epoch <id>] [--verify] [--workspace <path>]
```

### 4.1 Behaviour

With no flags, `reindex`:

1. Acquires the workspace lock (`.zicato/runtime/lock.json`) —
   refuses to run if an `evolve` is in flight.
2. Opens (or creates) `.zicato/index.db`.
3. Drops every table and recreates the schema (§3) for the
   current zicato version. Because the index is disposable,
   there is no schema-migration path — a version bump just
   rebuilds.
4. Walks `.zicato/lineage.json`, then every
   `.zicato/epochs/{epoch}/` directory: every `gen_score.json`,
   `experiment.json`, `patches/*.json`, `runs/*/loss.json`.
5. Inserts the derived rows inside a single transaction per
   epoch.
6. Releases the lock.

```
$ zicato reindex
[reindex] workspace: /home/op/myagent/.zicato
[reindex] dropping + recreating 9 tables (schema v3)
[reindex] epoch initial          : 8 generations, 80 runs, 312 patches
[reindex] epoch 2026-05-15_e1    : 5 generations, 50 runs, 191 patches
[reindex] indexed 13 generations across 2 epochs in 1.4s
```

### 4.2 Flags

| Flag | Meaning |
|---|---|
| `--epoch <id>` | Reindex only the named epoch. Drops and rebuilds that epoch's rows only; other epochs' rows are untouched. Useful after hand-editing one epoch's files. |
| `--verify` | Do not rebuild. Instead, walk the filesystem and the index in parallel and report any row that disagrees (or any canonical file with no index row). Exit code `1` if drift is found, `0` if clean. This is the integrity check; CI for the dogfood targets runs it. |
| `--workspace <path>` | Standard workspace override. |

`reindex --verify` is the assertion that the discipline in §2
holds. In normal operation it always reports clean — the
dual-write keeps the index current and the ordering rule keeps
it from ever going ahead of the filesystem. A non-clean
`--verify` means either a crash left the index behind (benign;
a plain `reindex` fixes it) or a bug in the dual-write (a real
defect; `--verify` is how it gets caught).

### 4.3 Reindex on resume

The resume protocol (see [ROBUSTNESS.md §2.6](ROBUSTNESS.md#26-l6-atomic-writes-resume-markers)
and [RUNTIME.md](RUNTIME.md)) runs a scoped `reindex` as one of
its first steps when `zicato evolve` restarts after a crash.
Because the index can only ever be *behind* the filesystem
(§2.3), a restart reindex is purely catch-up: it re-derives any
rows for rounds that completed on disk but crashed before their
index write. The resume protocol then proceeds against the
canonical files as it always has; the index is brought current
purely so the dashboard's analytics are correct from the first
SSE frame after restart.

## 5. Where SQLite is, and is NOT, used

zicato has three distinct storage concerns. SQLite is the right
answer for exactly one of them. This section draws the lines
explicitly because "use SQLite" is a tempting default that would
be wrong for the other two. The same three-way split is laid out
from the storage side in
[STORAGE.md §7](STORAGE.md#7-three-storage-concerns).

| Concern | Substrate | Why not SQLite |
|---|---|---|
| **Generation source trees** | git (v0+1) / directory snapshots (v0) | The data is intrinsically file-shaped; git is the file-shaped versioner and gives `diff` / `log` / `blame` / `bisect` for free. SQLite blobs would give smaller storage and *no tooling*. See [STORAGE.md](STORAGE.md). |
| **Per-run event capture** | `events.jsonl`, one file per run | The access pattern is append-while-running, tail-for-the-log-panel, stream-to-SSE, and replay-once in the reducer. An append-only line-delimited file wins every one of those. A row-per-event SQLite table would add write contention during the run and buy nothing — events are never queried *across* runs (the reducer's `LossProfile` is). See [TELEMETRY.md](TELEMETRY.md). |
| **Cross-run analytical views** | `.zicato/index.db` — **SQLite** | The access pattern is `GROUP BY` / `JOIN` over reduced features across many generations. This is exactly what a relational index is for. |

The principle: SQLite is used for the **derived, queried,
cross-cutting** layer, and *only* there. Source trees go to git;
event capture goes to JSONL. The index never absorbs either —
it projects *from* them (the `runs.events_path` column points
*at* the JSONL; it does not contain the events).

### 5.1 Ecosystem consistency

The choice is consistent with the rest of the
goldfive + harmonograf ecosystem, where SQLite already appears
as a *derived/served* store rather than a primary one:

- **goldfive** ships a `SqliteSink` — an `EventSink`
  implementation that writes events to a SQLite database for
  consumers that want a queryable event store. zicato does not
  use `SqliteSink` for capture (it uses `JSONLPersistenceSink`,
  per [TELEMETRY.md](TELEMETRY.md)) — but the existence of
  `SqliteSink` shows the ecosystem already treats SQLite as a
  legitimate analytical destination, not a foreign element.
- **harmonograf**'s server stores its run records in SQLite —
  the live console reads its served data from a SQLite database.

zicato's `index.db` sits in the same family: a SQLite store used
as a fast, queryable projection, downstream of a canonical
representation. Where zicato differs from `SqliteSink` is the
*role*: `SqliteSink` is a capture sink (a writer in the live
event path); `index.db` is an analytical index (a derived view,
never in the event path). The two are not interchangeable, and
zicato deliberately picks the JSONL sink for capture and the
SQLite index for views.

## 6. The Rust supervisor reads the same `index.db`

The Rust supervisor binary (see [RUNTIME.md](RUNTIME.md) §3,
[DASHBOARD.md](DASHBOARD.md)) serves the live dashboard. The
dashboard's tournament-detail analytics — the hypothesis ledger,
the mutation heat map, the cost panel — are the cross-run
aggregates from §1.1. The supervisor answers them by querying
`.zicato/index.db` directly, via the **`rusqlite`** crate.

```
┌────────────────────────────┐         ┌───────────────────────────┐
│  zicato evolve (Python)    │         │ zicato-supervisor (Rust)  │
│  ───────────────────────   │         │ ────────────────────────  │
│  dual-writes index.db      │         │ opens index.db read-only  │
│  (sqlite3, read-write)     │         │ via rusqlite              │
│  canonical-file-first      │         │ SQLITE_OPEN_READ_ONLY     │
└─────────────┬──────────────┘         └─────────────┬─────────────┘
              │                                      │
              │ writes                        reads  │
              ▼                                      ▼
        ┌─────────────────────────────────────────────────┐
        │            .zicato/index.db (SQLite)            │
        └─────────────────────────────────────────────────┘
```

Properties of the supervisor's read path:

- **Read-only handle.** The supervisor opens the database with
  `SQLITE_OPEN_READ_ONLY`. It is structurally incapable of
  writing the index. The single-writer rule (§2.4) is enforced
  by the open mode, not just by convention.
- **WAL mode.** The orchestrator opens the database in
  write-ahead-log mode (`PRAGMA journal_mode=WAL`). WAL lets the
  supervisor's reads proceed concurrently with the
  orchestrator's writes without either blocking the other — the
  reader sees a consistent snapshot as of its last completed
  transaction.
- **No SSE-driven query storm.** The supervisor does not query
  the index on every inotify event. It queries when a dashboard
  panel that needs an aggregate is first opened, and re-queries
  on the `round_finished` SSE trigger (rounds are minutes
  apart). Per-entry live status still comes from the
  `.zicato/runtime/` state files (which change every second);
  the index is for the *settled* cross-run views.
- **Graceful absence.** If `index.db` does not exist (a fresh
  workspace, or one where `reindex` has never run), the
  supervisor degrades: the live panels driven by
  `.zicato/runtime/` still render; the analytical panels show a
  "run `zicato reindex`" placeholder. The dashboard never hard-
  fails on a missing index.

Why the supervisor reads the index rather than walking the
filesystem itself: the supervisor is deliberately kept simple
and LLM-free (see [ROBUSTNESS.md §2.4](ROBUSTNESS.md#24-l4-orchestrator-watchdog-rust-supervisor)).
Re-implementing the JSON-walk-and-aggregate logic in Rust would
duplicate the projection rules that the Python dual-write
already encodes, and the two would inevitably drift. Reading the
shared `index.db` means the projection logic lives in exactly
one place (the Python dual-write), and the supervisor consumes
its output. The schema in §3 is the contract between the two
processes.

## 7. Cross-references

| Topic | Document |
|---|---|
| The original "add an index sidecar" prediction | [RATIONALE.md §7](RATIONALE.md#7-why-filesystem-layout-not-sqlite) |
| The three storage concerns, from the storage side | [STORAGE.md §7](STORAGE.md#7-three-storage-concerns) |
| Generation trees → git (v0+1 roadmap) | [STORAGE.md](STORAGE.md) |
| Event capture → `events.jsonl` (no SQLite) | [TELEMETRY.md](TELEMETRY.md) |
| The `LossProfile` shape the index projects | [TELEMETRY.md](TELEMETRY.md), [SCORING.md §2](SCORING.md#2-per-entry-drift-loss) |
| `experiment.json` / `gen_score.json` the index derives from | [EPOCHS-AND-JOURNALING.md §3](EPOCHS-AND-JOURNALING.md#3-the-experiment) |
| The tournament analytics the index backs | [TOURNAMENT.md §4](TOURNAMENT.md#4-tournament-detail-analytics) |
| The supervisor binary that reads the index | [RUNTIME.md](RUNTIME.md), [DASHBOARD.md](DASHBOARD.md) |
| `zicato reindex` in the CLI reference | [CLI.md](CLI.md) |
| The component map placing the index in the meta-loop | [ARCHITECTURE.md](ARCHITECTURE.md) |
</content>
</invoke>
