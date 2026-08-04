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
- The full schema — thirteen tables (§3).
- `zicato reindex` / `zicato reindex-generations` — the rebuild
  commands (§4).
- Self-healing: the index maintains itself (§5).
- Where SQLite is and is NOT used in zicato (§6).
- The Rust supervisor's read path via `rusqlite` (§7).

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
-- promote/reject decisions and the scalar delta, round over round
SELECT ran_at, parent_generation_id, child_generation_id,
       decision, delta_scalar
FROM tournaments
WHERE epoch_id = '2026-05-15_e1'
ORDER BY ran_at;
```

```sql
-- board entries that never differentiate parent from child
SELECT entry_id
FROM loss_profiles
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
  (the `LossProfile` projection), never raw events. See §6.

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
  fixes it — no manual repair.
- If the schema changes between zicato versions, `reindex`
  rebuilds under the new schema. The index is disposable, so a
  full rebuild is always the clean path. As a convenience for an
  *existing* file opened by a newer writer, `apply_schema` also
  carries out a small in-place additive migration (e.g. the v1 → v2
  column adds, §4.2) so an incremental `ingest_*` write does not
  force a rebuild first — but a rebuild remains the canonical
  recovery.
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
   runs(v5--*, ...)                ── derived
   loss_profiles(v5--*, ...)       ── derived
   judge_losses(v5--*, ...)        ── derived
   tournaments(v4 vs v5, ...)      ── derived
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
reindex-on-restart, §4.4) catches it up. An ahead index — a
row referencing a file that was never written — would be a
phantom, and the ordering rule makes that impossible.

The index write itself uses a SQLite transaction so the *set*
of rows for one round lands atomically: a reader never sees half
a round's rows.

### 2.4 Single writer

Only the orchestrator (`zicato evolve`, and the one-shot
subcommands `analyze` / `tournament` when run standalone) writes
`index.db`. The Rust supervisor opens the database **read-only**
(§7). `zicato reindex` / `zicato reindex-generations` are writers,
expected to run off the happy path while no `evolve` is in flight;
they are not part of the live loop. SQLite's own file locking plus
the WAL-mode posture (§6) are the concurrency backstop, consistent
with the single-writer-per-file rule the rest of the runtime layer
follows (see [RUNTIME.md](RUNTIME.md)).

## 3. Schema

The schema is defined authoritatively in
`src/zicato/index/schema.py` as plain SQL DDL (kept as SQL strings,
not an ORM, precisely so the Rust supervisor can mirror it
verbatim). The current `SCHEMA_VERSION` is **14** (additive migrations
have since added, among others, the `generations.elo*` visibility-rating
columns — §3.2 — and the v14 `ingest_cursors` self-heal table, §5.2).
That module is the contract; this section documents it.

The index has **thirteen tables** — nine mirroring the artifact
hierarchy: `epochs` → `generations` → `experiments` → `patches`, and
`generations` → `runs` → `loss_profiles` / `metric_counts` /
`judge_losses`, with `tournaments` as the per-round comparison
record — plus the two reflection tables added at schema v11
(`reflections`, `judge_scorecards`), the `pareto_frontier`
projection added at v13, and the `ingest_cursors` self-heal table
added at v14 (§5.2). The last is the one table that is not a
projection of a canonical file: it records *what the workspace
looked like* when each epoch was last projected, so divergence is
detectable without re-deriving every row.

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
              ┌──────────┴──────────┐
              ▼                     ▼
      ┌────────────────┐    ┌────────────────┐
      │  metric_counts │    │  judge_losses  │
      └────────────────┘    └────────────────┘

┌──────────────┐
│ tournaments  │   one row per round: parent vs child, gate verdict
└──────────────┘
```

All `*_id` columns are the same string identifiers used in the
filesystem layout (`epoch_id` is the epoch directory name,
`generation_id` is `v0` / `v1` / ..., `entry_id` is the board entry
id, and `run_id` is the per-run `{generation_id}--{entry_id}`
synthetic id). This makes any index row trivially traceable back to
its canonical file.

Schema versioning is stamped two ways by `apply_schema`: the SQLite
`PRAGMA user_version` (the authoritative source, readable from any
client) and a one-row `schema_meta` table (a human-legible mirror,
not part of the cross-language contract). A consumer that opens a
database whose `user_version` does not equal `SCHEMA_VERSION` should
treat the index as stale and run `zicato reindex`.

### 3.1 `epochs`

One row per epoch directory. Projection of `lineage.json` plus
the epoch's `config.json` (`EpochConfig`).

| Column | Type | Source |
|---|---|---|
| `epoch_id` | TEXT PK | epoch directory name |
| `contract_hash` | TEXT | `EpochConfig.contract_hash` |
| `created_at` | TEXT | `lineage.json` |
| `closed` | INTEGER | 1 once the epoch is closed |
| `goal` | TEXT | the epoch's goal (v2 column) |
| `parent_epoch_id` | TEXT | predecessor epoch id, cross-epoch lineage (v2 column) |

`goal` and `parent_epoch_id` are the two `epochs` columns added in
the v1 → v2 migration (§4.2).

### 3.2 `generations`

One row per generation directory under any epoch.

| Column | Type | Source |
|---|---|---|
| `epoch_id` | TEXT | (FK → `epochs`) |
| `generation_id` | TEXT | `v0` / `v1` / ... |
| `parent_generation_id` | TEXT NULL | the generation it was proposed against |
| `promoted` | INTEGER | 1 if this generation was promoted |
| `created_at` | TEXT | when the generation was created |
| `elo` | REAL NULL | visibility rating on the Elo scale (schema v10) — the Bradley–Terry strength re-fit over the match ledger at reindex, mapped `1500 + θ·400/ln 10`; NULL until the generation has a settled two-competitor duel |
| `elo_se` | REAL NULL | standard error of `elo` (schema v12), same scale |
| `elo_games` | INTEGER NULL | settled observations folded into the fit (schema v10) — two-competitor duels plus racing rung group observations |

Primary key `(epoch_id, generation_id)`. The `parent_generation_id`
and `promoted` columns are exactly the two that the targeted
`zicato reindex-generations` repair rewrites (§4.3) — they are the
fields a buggy live dual-write was observed to leave stale. The `elo*`
columns are a **read-only analytics fold** (`src/zicato/index/elo.py`),
re-derived from scratch at every reindex and read only by the display
surfaces — never by the gate or the selection path.

### 3.3 `experiments`

One row per `experiment.json` (i.e. one per generation except
the `v0` baseline, which has no experiment).

| Column | Type | Source |
|---|---|---|
| `epoch_id` | TEXT | (FK) |
| `generation_id` | TEXT | (FK → `generations`) |
| `hypothesis_core_idea` | TEXT | `hypothesis.core_idea` |
| `hypothesis_why` | TEXT | `hypothesis.why` |
| `hypothesis_json` | TEXT (JSON) | the full hypothesis block, verbatim |
| `tournament_decision` | TEXT | `outcome.tournament_decision` (NULL until the tournament runs) |
| `rejection_reason` | TEXT | `outcome.rejection_reason` |
| `scalar_score_delta` | REAL | `outcome` — child − parent scalar |
| `drift_loss_delta` | REAL | `outcome.drift_loss_delta` |
| `pass_rate_delta` | REAL | `outcome.pass_rate_delta` |
| `outcome_json` | TEXT (JSON) | the full resolved `outcome` block, verbatim |

Primary key `(epoch_id, generation_id)`. The detail the index does
not give a dedicated column — mutation-point ids, the
expected-pass-rate band, the per-kind hypothesis match — lives
inside the `hypothesis_json` / `outcome_json` blobs and is reached
with SQLite's JSON functions (`json_extract`, `json_each`); the
mutation heat map in
[TOURNAMENT.md §4.5](TOURNAMENT.md#45-mutation-heat-map) reads the
modulating ids out of `hypothesis_json` this way rather than from a
separate column.

### 3.4 `patches`

One row per `patches/{patch_id}.json` file.

| Column | Type | Source |
|---|---|---|
| `patch_id` | TEXT PK | patch file `id` |
| `epoch_id` | TEXT | (FK) |
| `generation_id` | TEXT | (FK → `generations`) |
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
| `run_id` | TEXT PK | the synthetic `{generation_id}--{entry_id}` run id |
| `epoch_id` | TEXT | (FK) |
| `generation_id` | TEXT | (FK → `generations`) |
| `entry_id` | TEXT | board entry id |
| `started_at` | TEXT | run start |
| `ended_at` | TEXT | run end |
| `aborted` | INTEGER | `loss.json` — 1 if `RunAborted` |
| `runtime_ms` | INTEGER | `loss.json` |
| `tournament_id` | TEXT | (FK → `tournaments`) — the round this run belonged to (v2 column) |

Primary key is `run_id` (the `{generation_id}--{entry_id}`
synthetic id). The "which side of the tournament" distinction is
carried by the run's generation: the parent and child generations
each get their own run row, and `tournament_id` ties both to the
round they were scored in. `tournament_id` is one of the v2-added
columns (§4.2), indexed by `idx_runs_tournament`. The harmonograf
drill-down join key is the run's `adk_session_id` (stamped into
`loss.json` by the reducer, not stored as an index column) — see
[TOURNAMENT.md §5](TOURNAMENT.md#5-the-harmonograf-split) and §6
below.

### 3.6 `loss_profiles`

One row per `loss.json` — the reduced per-run feature vector.

| Column | Type | Source |
|---|---|---|
| `run_id` | TEXT PK | (FK → `runs`) — the `{generation_id}--{entry_id}` id |
| `epoch_id` | TEXT | (FK) |
| `generation_id` | TEXT | (FK) |
| `entry_id` | TEXT | board entry id |
| `drift_loss` | REAL | `LossProfile.drift_loss` |
| `pass_fail` | INTEGER NULL | `LossProfile.pass_fail` (NULL when the entry's `expectations` list is empty) |
| `runtime_ms` | INTEGER | `LossProfile.runtime_ms` |
| `wall_clock_budget_exceeded` | INTEGER | 1 if the run exhausted its wall-clock budget |
| `loss_json` | TEXT (JSON) | the full `LossProfile`, verbatim — the per-kind counts, escalations, plan revisions, etc. that get no dedicated column live here |
| `tournament_id` | TEXT | (FK → `tournaments`) — the round (v2 column) |

Primary key `run_id` (matching `runs`). This table is the
scoring-side projection; the per-entry A/B grid in
[TOURNAMENT.md §4.2](TOURNAMENT.md#42-per-entry-ab-grid) joins the
parent and child generations' `loss_profiles` rows on `entry_id`.
Features the `LossProfile` carries but that are not promoted to
their own column (`escalations`, `plan_revisions`,
`task_failure_ratio`, `human_intervention_required`, the per-kind
counts) are recoverable from `loss_json` with `json_extract`; the
drift counts are *also* unpivoted into `metric_counts` (§3.7) for
`GROUP BY`-able access. `tournament_id` is a v2-added column
indexed by `idx_loss_tournament`.

### 3.7 `metric_counts`

The drift counts, unpivoted into one row per
(run × drift kind), (run × severity), and (run × custom judge).
The `LossProfile` carries `drift_counts_by_kind`,
`drift_counts_by_severity`, and `drift_counts_by_judge` as
dicts; storing them unpivoted makes them `GROUP BY`-able.

| Column | Type | Source |
|---|---|---|
| `run_id` | TEXT | (FK → `runs`) — the `{generation_id}--{entry_id}` id |
| `namespace` | TEXT | which dict the row came from: the drift-kind, severity, or custom-judge bucket |
| `name` | TEXT | e.g. `DRIFT_KIND_CONFABULATION_RISK`, or a custom judge's `judge_name` |
| `severity` | TEXT | the severity bucket (`INFO` / `WARNING` / `CRITICAL`) where applicable |
| `count` | REAL | the count |

No primary key declared; the table is reached by `run_id` (the
`idx_metric_run` index) and aggregated. The drift-kind heatmap in
[DASHBOARD.md §4.6](DASHBOARD.md#46-drift-kind-heatmap) is a
`SUM(count) GROUP BY name` over this table joined to `runs` for the
round; the custom-judge `namespace` rows give the same view sliced
by `judge_name` rather than by `DriftKind`. The per-judge weighted
loss is also materialised in its own table — `judge_losses` (§3.9).

### 3.8 `tournaments`

One row per round — the parent-vs-child comparison record.

| Column | Type | Source |
|---|---|---|
| `tournament_id` | TEXT PK | the round's stable id |
| `epoch_id` | TEXT | (FK) |
| `parent_generation_id` | TEXT | the reigning champion's id |
| `child_generation_id` | TEXT | the challenger's id |
| `decision` | TEXT | `promote` or `reject` |
| `parent_scalar` | REAL | parent's tournament scalar |
| `child_scalar` | REAL | child's tournament scalar |
| `delta_scalar` | REAL | child − parent |
| `rejection_reason` | TEXT NULL | gate's reason if rejected |
| `ran_at` | TEXT | when the round was scored |

Primary key `tournament_id`. This is the table that backs the
tournament bracket and the per-matchup detail in
[TOURNAMENT.md](TOURNAMENT.md): the bracket *is* `SELECT * FROM
tournaments WHERE epoch_id = ? ORDER BY ran_at`. `runs` and
`loss_profiles` carry a `tournament_id` FK back to this table so a
round's full per-entry detail is one join away.

### 3.9 `judge_losses`

One row per (run × custom judge) — the per-judge weighted-loss
breakdown that the scoring layer's `per_judge_weights` produces
(see [SCORING.md §2.2](SCORING.md#22-per-judge-weights)). This is
the table added in the v1 → v2 migration; it is created by the
regular `CREATE TABLE IF NOT EXISTS` pass (the migrator does not
need an `ALTER` for it — a fresh table on a v1 database).

| Column | Type | Source |
|---|---|---|
| `run_id` | TEXT | (FK → `runs`) — the `{generation_id}--{entry_id}` id |
| `judge_name` | TEXT | the custom judge's `name` |
| `weighted_loss` | REAL | `raw_loss × weight` — the judge's contribution to `drift_loss` |
| `raw_loss` | REAL | the judge's unweighted loss |
| `weight` | REAL | the `per_judge_weights` weight applied |

Primary key `(run_id, judge_name)`, indexed by
`idx_judge_losses_run`. Where `metric_counts` (§3.7) carries the
raw per-judge *counts*, `judge_losses` carries the per-judge
*weighted loss* — the hypothesis ledger and the per-judge
attribution panels read this table directly rather than
re-deriving the weighting from counts × weights.

## 4. `zicato reindex`

`zicato reindex` rebuilds `index.db` from the filesystem. It is
the correctness backstop for the whole index design. It is an
advanced / off-the-happy-path command — `zicato evolve` keeps the
index current via the live dual-write, so an operator reaches for
`reindex` only to repair a behind-or-corrupt index.

```
zicato reindex [--workspace <path>]
```

`--workspace` is the **only** flag (default `.zicato`). There is no
`--epoch` and no `--verify` — `reindex` always rebuilds the whole
workspace.

It is also no longer the *routine* path. `zicato evolve` builds an
absent index and heals a diverged one at its own start (§5), so an
operator reaches for `reindex` only in the situations §5.4 names.

### 4.1 Behaviour

`reindex`:

1. Opens (or creates) `.zicato/index.db`.
2. Drops the database and re-applies the v2 schema (§3) — the
   canonical build path (`rebuild_index`) starts from a clean file.
3. Walks `.zicato/lineage.json`, then every
   `.zicato/epochs/{epoch}/` directory: every `experiment.json`,
   `patches/*.json`, `runs/*/loss.json`, and the resolved outcomes.
4. Inserts the derived rows.
5. Prints a summary of how many epochs, generations, and runs were
   indexed.

```
$ zicato reindex
[reindex] workspace: /home/op/myagent/.zicato
[reindex] indexed 2 epochs, 13 generations, 130 runs
```

### 4.2 Schema versioning, not a `--verify` flag

The shipped `reindex` does not have a `--verify` integrity mode and
does not take an `--epoch` scope. The discipline in §2 (canonical
file first, index row second) is what keeps the index from ever
going *ahead* of the files; a behind index is fixed by a plain
`reindex` (or by the next incremental `ingest_*`).

Schema versioning is the mechanism that makes a rebuild
recognisably necessary. `SCHEMA_VERSION` is **14**, stamped into
`PRAGMA user_version` and the `schema_meta` table by `apply_schema`.
An index whose stamped version is *older* than this build's no longer
waits for an operator to notice — `ensure_index` rebuilds it at the
next `evolve` start or dashboard start (§5.1).
The v1 → v2 migration added five things:

- `epochs.goal`
- `epochs.parent_epoch_id`
- `runs.tournament_id`
- `loss_profiles.tournament_id`
- the whole `judge_losses` table (§3.9)

When a newer writer opens an older v1 file, `apply_schema` performs
the additive `ALTER TABLE` column adds in place (the `judge_losses`
table is created by the ordinary `CREATE TABLE IF NOT EXISTS` pass),
so incremental writes proceed without forcing a rebuild. A full
`reindex` drops the file and re-applies the v2 DDL outright.

### 4.3 `zicato reindex-generations` — targeted repair

Alongside the full rebuild, zicato ships a narrow repair command:

```
zicato reindex-generations [--workspace <path>]
```

It reconciles **only** the `generations` table from disk. It was
added for workspaces whose `generations` rows were written by a
buggy live dual-write — `parent_generation_id` left NULL and
`promoted` clamped to `0` on every row except the seed. It walks
`lineage.json` plus every `experiment.json` and rewrites only the
`parent_generation_id` and `promoted` columns of each `generations`
row; the rest of the index is untouched. It is idempotent and
read-only against the workspace files. For anything broader, use the
full `zicato reindex`.

### 4.4 Reindex on resume

The resume protocol (see [ROBUSTNESS.md §2.6](ROBUSTNESS.md#26-l6-atomic-writes-resume-markers)
and [RUNTIME.md](RUNTIME.md)) brings the index current as one of
its first steps when `zicato evolve` restarts after a crash.
Because the index can only ever be *behind* the filesystem
(§2.3), the restart catch-up is purely additive: the orchestrator
re-derives (via the incremental `ingest_*` path, or a full
`reindex`) any rows for rounds that completed on disk but crashed
before their index write. The resume protocol then proceeds against the
canonical files as it always has; the index is brought current
purely so the dashboard's analytics are correct from the first
SSE frame after restart.

## 5. Self-healing: the index maintains itself

`zicato reindex` (§4) is the *forensic* tool. Nothing on the happy
path should ever require an operator to run it. This section
specifies the three mechanisms that make that true, the literal
seam signatures they add, the cursor schema they persist, and the
concurrency rule that governs when a heal or a build may run.

The motivating defect is not cosmetic. The proposer reads the
index *during* `evolve`: `prior_experiments_for_epoch` supplies the
experiment memory, and the mutation track record supplies the
per-mutation-point hit rate. A stale index does not fail loudly —
it silently returns *fewer* prior experiments, and the loop
degrades in quality with no error anywhere. Keeping the index
current is a loop-quality property, not a convenience.

### 5.1 M1 — absent-or-older index auto-builds, temp-then-rename

```python
# zicato.index.ingest
def ensure_index(
    workspace_root: Path,
    db_path: Path | None = None,
    *,
    action_out: list[str] | None = None,
) -> Path: ...
```

`ensure_index` guarantees that, on return, `index.db` exists and
carries the current `SCHEMA_VERSION`. It builds when — and only
when — one of three things is true:

| Condition | `action_out` value |
|---|---|
| the file is absent | `built:absent` |
| `PRAGMA user_version` < `SCHEMA_VERSION` | `built:stale-schema` |
| the file is not a readable SQLite database | `built:unreadable` |
| none of the above | `present` |

An **equal-version** database is never rebuilt by M1. Detecting
that its *contents* drifted from the workspace is M2's job (§5.2);
M1 answers only the structural question "is there a database of the
right shape here at all".

A **newer** database — `user_version` > `SCHEMA_VERSION` — raises
`IndexSchemaNewerError` with its existing actionable message.
Auto-deleting a newer index is forbidden: the newer build's columns
and semantics are unknown to this one, and the recovery (upgrade
zicato, or delete deliberately) belongs to the operator.

Whole-table additions are why an older-version database is rebuilt
rather than migrated. `apply_schema`'s in-place migrator can add a
column, but it cannot *populate* a table that did not exist — the
v11 reflection tables and the v13 `pareto_frontier` table both
landed empty on an in-place open and stayed empty until a rebuild.
A full rebuild is the only shape that backfills them, so M1 does
the rebuild rather than leaving a technically-current database with
silently empty tables.

**Temp-then-rename.** Every build — `ensure_index`'s and
`rebuild_index`'s alike — goes through one private helper:

```python
def _build_index_atomically(workspace_root: Path, target: Path) -> None:
    # 1. clear any leftover {target}.tmp + its -wal/-shm sidecars
    # 2. build the FULL index into {target}.tmp
    # 3. os.replace({target}.tmp, target)
    # 4. unlink the replaced file's stale {target}-wal / {target}-shm
```

This structurally retires a whole defect class. The previous shape
unlinked `index.db` *first* and then built in place, so any failure
during the build — an unreadable canonical record, a disk-full, a
Ctrl-C — left the operator with a schema-only file and every table
empty, along the very path they had run to *recover* a bad index.
Under temp-then-rename a failed build leaves the existing database
byte-untouched. `rebuild_index` is refactored onto the same helper:
`zicato reindex` keeps its behaviour (a full re-derivation from the
files) minus the destroy-on-failure hazard.

The frontier-projection guard added earlier — warn and skip on a
corrupt `pareto_frontier.json` rather than raise — stays exactly as
it is *inside* the build. Temp-then-rename and the in-build guard
are complementary: the guard keeps one bad record from aborting the
build; the rename keeps an aborted build from destroying the old
database.

### 5.2 M2 — per-epoch cursors, validation, and incremental heal

Schema **v14** adds one additive table.

```sql
CREATE TABLE IF NOT EXISTS ingest_cursors (
  epoch_id                  TEXT PRIMARY KEY,
  experiments_count         INTEGER,
  round_dirs_count          INTEGER,
  reflections_count         INTEGER,
  lineage_generations_count INTEGER,
  last_ingested_at          TEXT
)
```

| Column | Workspace signal it records |
|---|---|
| `experiments_count` | generation directories under `epochs/{e}/generations/` that contain an `experiment.json` |
| `round_dirs_count` | entries under `epochs/{e}/rounds/` |
| `reflections_count` | directories under `epochs/{e}/reflections/` |
| `lineage_generations_count` | generation entries for this epoch in `lineage.json` |
| `last_ingested_at` | when this epoch was last projected (observational) |

The four counts are deliberately **cheap**: directory-entry counts
and stats, never a file parse. `lineage.json` is read once for the
whole workspace, not once per epoch. Validation must be affordable
enough to run at every `evolve` start on a large workspace, which
rules out re-deriving row content to compare it.

`round_dirs_count` is a signal the index has no table for — nothing
projects `epochs/{e}/rounds/`. It is carried anyway because it is
the cheapest proxy for "this epoch advanced": a new round directory
appears at round start, before the experiment that will eventually
land. Re-ingesting an epoch on that signal is idempotent, so a
slightly eager heal costs a walk and nothing else.

```python
def validate_index(
    workspace_root: Path, db_path: Path | None = None
) -> tuple[str, ...]: ...

def heal_index(
    workspace_root: Path, db_path: Path | None = None
) -> tuple[str, ...]: ...
```

`validate_index` returns the sorted ids of **diverged** epochs.
Three things count as divergence:

1. an epoch on disk with no cursor row (never ingested, or ingested
   by a build that predates v14),
2. an epoch whose cursor row disagrees with any of the four
   workspace signals,
3. an epoch with a cursor row that is **gone from the workspace** —
   the index is holding rows for something that no longer exists.

`heal_index` re-ingests exactly those epochs and returns the ids it
healed. For each one it deletes that epoch's rows and re-projects
via the existing `_rebuild_epoch` machinery; for case 3 it deletes
and stops. The delete is epoch-scoped across **every** table, which
matters because four of them carry no `epoch_id` column and must be
reached through a subquery:

| Table | Epoch-scoped delete |
|---|---|
| `generations`, `experiments`, `patches`, `runs`, `loss_profiles`, `tournaments`, `reflections`, `pareto_frontier`, `ingest_cursors`, `epochs` | `WHERE epoch_id = ?` |
| `metric_counts`, `judge_losses` | `WHERE run_id IN (SELECT run_id FROM runs WHERE epoch_id = ?)` |
| `judge_scorecards` | `WHERE reflection_id IN (SELECT reflection_id FROM reflections WHERE epoch_id = ?)` |

The subquery deletes run **before** the `runs` / `reflections`
deletes that would strip their lookup rows.

After the last epoch is re-projected, `heal_index` re-runs the Elo
fold over the whole database. The `generations.elo*` columns are a
cross-epoch analytics fold, not per-epoch rows — deleting and
re-inserting one epoch's generations nulls them, and only a
whole-ledger re-fold restores what a from-scratch rebuild would
have produced.

**The convergence pin.** Heal-then-read and rebuild-from-scratch
must agree. The determinism test corrupts an index (drops one
epoch's rows), heals it, and asserts the SQL `.dump` equals a
from-scratch rebuild's `.dump`. Two cells are outside the pin, both
for the same reason — they are observational, not derived:

- `ingest_cursors.last_ingested_at` is a wall clock, normalised to
  `<TS>` exactly as the REINDEX-DUMP parity gate already normalises
  every ISO timestamp in the dump.
- SQLite **rowid assignment order** differs when a heal re-inserts
  one epoch of several into a non-empty table. Convergence is
  therefore *content* identity (DDL in order, INSERT statements as
  a set). For the single-epoch case the tables empty out completely
  and rowids restart at 1, so the raw dump is byte-identical there
  and the test pins that too. No query in the index orders by
  rowid; nothing in the contract depends on it.

Everything else — every projected row of every table — is
byte-identical between the two paths. That is what makes the heal
safe to run automatically: it cannot produce an index a rebuild
would not have produced.

### 5.3 M3 — the routine paths, and the concurrency rule

**(a) `evolve` start.** The `evolve_n_rounds` preflight runs
`ensure_index` then `heal_index` under `best_effort`, and emits
exactly one log line naming what it did:

```
index: built fresh (absent)
index: healed epochs 2026-08-02_e1, 2026-08-02_e2
index: fresh
```

Render conformance: the heal says what it did, never just that it
ran. A fresh build makes the subsequent heal redundant (the build
writes every cursor), so the two are reported as alternatives, not
in sequence.

The seam sits immediately **after** `acquire_workspace_lock` and
before `prepare_resume` — not beside the concurrency-report line a
few statements earlier, which runs *outside* the lock. See the
concurrency rule below for why that placement is load-bearing.

This is the loop-quality fix named at the top of §5: the proposer's
experiment memory and mutation track record read the index later in
the same invocation, and they now read a current one.

It also closes a smaller, previously-invisible staleness. §2.3's
ordering rule writes the canonical file first and the index row
second, and the orchestrator appends to `lineage.json` *after* the
`ingest_experiment` dual-write — so the two `generations` columns the
index takes from lineage, `created_at` and `round_index`, land empty
on the live write and stay that way. Nothing errors; the round simply
leaves a generation with an unknown birth round. Before this feature
they stayed empty until an operator happened to run `zicato reindex`.
Now the next round's preflight sees the epoch's
`lineage_generations_count` move and fills them in. It is also why an
epoch reads as diverged at the *end* of a run: that is the dual-write
ordering showing through, not a defect in the cursor, and the heal
that follows is a genuine correction rather than redundant work.

**(b) The dashboard / query read path.** `create_app` calls
`ensure_index` **only** — the absence/version check, at server
start, never per request.

A full heal is deliberately *not* on the read path. Healing writes;
a reader that heals while an orchestrator dual-writes is the
contention case the single-writer rule (§2.4) exists to prevent,
and it would put a multi-second workspace walk in front of the
first HTTP response. The dashboard's job is to notice that the
index is absent or of the wrong shape and fix *that*; noticing that
its contents drifted is the writer's job, and the writer runs the
heal at the top of every `evolve`.

The read path additionally skips the build on a workspace with no
`epochs/` content at all, preserving the graceful-absence
behaviour §7 specifies: a fresh, never-run workspace renders its
"not yet indexed" empty state rather than gaining a valid-but-empty
`index.db` that flips every reader's degrade branch.

**(c) The concurrency rule.**

> A build or a heal runs only under the workspace lock discipline
> the orchestrator already uses. `evolve` holds `WorkspaceLock` for
> its whole invocation, so the evolve-start build/heal is naturally
> exclusive. Any other process that would build — today, only the
> dashboard's `ensure_index` on an absent or wrong-version database
> — first checks whether the lock is held by a live process; if it
> is, it **skips with a log line and does not retry**.

Skip-not-wait is the right posture for the dashboard: the running
`evolve` that holds the lock is itself building or healing the
index at its own start, so the work the dashboard would do is
already being done by the process that owns the writes. Waiting
would block startup on a lock held for the length of an entire
evolve run; retrying would reintroduce the contention the rule
exists to avoid. The dashboard renders its degraded empty state for
one page load and picks the index up on its next start.

`zicato reindex` remains an explicit operator action and is not
lock-gated — it is the forensic tool, run deliberately off the
happy path, and §2.4 already states the expectation that it runs
while no `evolve` is in flight. What changed is that it is no
longer *destructive* when it fails (§5.1).

### 5.4 What still requires `zicato reindex`

Routine reindexing is now automatic. Four situations still call for
the explicit command:

- **Downgrade recovery.** A database written by a newer zicato
  raises `IndexSchemaNewerError`; auto-deleting it is forbidden, so
  the operator deletes it and rebuilds deliberately.
- **Post-surgery rebuilds.** After hand-editing canonical files in
  a way the cheap cursor signals cannot see — correcting a value
  *inside* an `experiment.json` without changing any file count —
  a full rebuild is what re-derives the changed cells.
- **Determinism assertion.** Proving the index equals a pure
  re-projection of the files (what the REINDEX-DUMP parity gate
  does) requires the from-scratch path by definition.
- **Anything broader than an epoch.** The heal's unit is the epoch;
  a suspected defect that is not epoch-scoped is a rebuild.

## 6. Where SQLite is, and is NOT, used

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
it projects *from* them. A run's `events.jsonl` is reached from
its index row by reconstructing the path from the run coordinate
(`{epoch}/generations/{gen}/runs/{entry}/events.jsonl`); the
harmonograf drill-down uses the run's `adk_session_id` (in
`loss.json`). The index holds the *reduced* features, not the
events.

### 6.1 Ecosystem consistency

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

## 7. The Rust supervisor reads the same `index.db`

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

## 8. Cross-references

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
| The workspace lock the heal/build rule defers to | [RUNTIME.md](RUNTIME.md) |
| The component map placing the index in the meta-loop | [ARCHITECTURE.md](ARCHITECTURE.md) |
</content>
</invoke>
