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

[RATIONALE.md §7](RATIONALE.md#7-why-the-canonical-layout-is-the-filesystem-rather-than-sqlite)
sets the direction: when pattern queries become a bottleneck, add an
index sidecar — one SQLite file used as a cache, regenerable from the
filesystem — rather than making the filesystem layout itself the index.
This document specifies that sidecar.

This document covers:

- Why the index exists and what it is *not* (§1).
- The discipline: files canonical, index derived, dual-write +
  full rebuild (§2).
- The full schema — thirteen tables plus a version mirror (§3).
- `zicato repair index` / `zicato repair generations` — the rebuild
  commands (§4).
- Self-healing: the index maintains itself (§5).
- Where SQLite is and is NOT used in zicato (§6).
- The readers: the dashboard service and the Rust supervisor (§7).

## 1. Why an index

### 1.1 The cross-run query problem

The filesystem layout in
[EPOCHS-AND-JOURNALING.md §2](EPOCHS-AND-JOURNALING.md#2-storage-layout)
is excellent for the operator's per-artifact loop: open one
`experiment.json`, read one `gen_score.json`, `cat` one loss file.
It is poor for any question that ranges across many artifacts.

Consider these operator questions:

- "Across the whole epoch, which generations had the proposer
  predict `CAPABILITY_MISMATCH` would drop, and did it?"
- "Which board entries never differentiate parent from candidate
  — i.e. always score identically on both sides?"
- "What is the running hypothesis match-rate, round over round?"
- "How much wall-clock and how many evaluation LLM calls has this
  epoch's tournament cost so far?"
- "Across all epochs, which mutation points correlate with a
  promote?"

Every one of these is a `GROUP BY` / `JOIN` over data that lives
scattered across `epochs/*/generations/*/experiment.json`,
`epochs/*/generations/*/runs/*/seed-*/loss.*.json`, and
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
refreshes. A file-walk on every server-sent-events (SSE) update does not
scale beyond a very small epoch.

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
  `index.db` is deleted, `zicato repair index` reconstructs it in full
  from the filesystem. Nothing is lost.
- **Not a write target for orchestration logic.** The
  orchestrator never *reads back* a decision from the index. The
  tournament gate reads the loss files and `gen_score.json`; the
  resume protocol reads `experiment.json` and the round records; the
  pattern detectors read the champion's loss and events files. All of
  those are files. The index serves *views* and the proposer's
  advisory experiment memory (§5); no gate or promotion decision
  reads it.
- **Not a replacement for the filesystem layout.** `ls`, `cat`,
  `grep`, `git diff` on `.zicato/` all still work and are still
  the operator's primary interface. The index is additive.
- **Not per-run event storage.** Run telemetry is one events file per
  measurement (`events.{purpose}.r{draw}.jsonl`). The index holds *reduced* per-run features
  (the `LossProfile` projection), never raw events. See §6.

**The filesystem is canonical and human-legible; the index is derived
and fast to query; the two never disagree, because the index is always
rebuildable from the files.**

## 2. The discipline

The index is only safe if four rules hold without exception.

### 2.1 Files are canonical

Every fact has a single canonical home: a file under
`.zicato/`. `experiment.json` is the canonical Experiment. The
per-measurement loss file
(`runs/{entry_id}/seed-{seed}/loss.{purpose}.r{draw}.json`, called
`loss.json` below) is the canonical LossProfile. `gen_score.json` is
the canonical generation score. `lineage.json` is the canonical
cross-epoch directed acyclic graph of generations. The index never
holds a fact that did not come from one of these files.

This means: a contributor adding a new artifact adds a new
*file*, then optionally a new *index table* projecting it. The
file lands first; the table is downstream.

### 2.2 The index is derived and fully rebuildable

`zicato repair index` (§4) builds a fresh database by walking the
filesystem and publishes it over the old one. This is the
correctness backstop:

- If the index is ever suspected stale or corrupt,
  `zicato repair index` fixes it — no manual repair.
- If the supported schema or projection semantics change, `ensure_index`
  rebuilds the incompatible database from canonical records. Incremental
  writers refuse incompatible indexes and never add columns in place.

- If an operator hand-edits a file under `.zicato/epochs/`
  (e.g. fixes a malformed `experiment.json`), `zicato repair index`
  brings the index back in line.

A rebuild is `O(total artifacts)` file reads — the same cost as
*one* cross-run file-walk, paid once, after which every query is
indexed. For a large workspace (multiple epochs, hundreds of
generations) a full rebuild takes seconds rather than minutes.

### 2.3 The orchestrator dual-writes live

Waiting for an explicit rebuild after every round would leave
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
   runs(v5 measurements, ...)      ── derived
   loss_profiles(v5 selected, ...) ── derived
   judge_losses(v5 measurements)   ── derived
   tournaments(v4 vs v5, ...)      ── derived
        │
        ▼
dashboard SSE stream notices the change (dashboard reads index)
```

The dual-write is **not transactional across the file and the
DB** — the file write and the DB write are two separate
operations. The ordering rule makes this safe:

> **The canonical file is always written first; the index row
> is written second.**

If the orchestrator crashes between the two, the index is
*behind* the filesystem — never *ahead*. A behind index is
self-healing: the heal at the next `evolve` start (§5.3), or an
explicit `zicato repair index`, catches it up. An ahead index — a
row referencing a file that was never written — would be a
phantom, and the ordering rule makes that impossible.

The index write itself uses a SQLite transaction so the *set*
of rows for one round lands atomically: a reader never sees half
a round's rows.

### 2.4 Single writer

Only the orchestrator (`zicato evolve`, and the advanced commands
that settle measurements standalone, such as `zicato tournament run`)
writes `index.db`. The Python dashboard service and the Rust
supervisor open the database **read-only** (§7). `zicato repair index` / `zicato repair generations` are writers,
expected to run off the happy path while no `evolve` is in flight;
they are not part of the live loop. SQLite's own file locking plus
the write-ahead-log posture (§7) are the concurrency backstop, consistent
with the single-writer-per-file rule the rest of the runtime layer
follows (see [RUNTIME.md](RUNTIME.md)).

## 3. Schema

The schema is defined authoritatively in
`src/zicato/index/schema.py` as plain SQL DDL, kept as SQL strings
rather than an ORM so the Rust supervisor can mirror it verbatim. The
supported `SCHEMA_VERSION` is **15**, which includes, among others, the
`generations.elo*` visibility-rating columns (§3.2) and the
`ingest_cursors` self-heal table (§5.2). That module is the contract;
this section documents it.

The index has **thirteen tables**, plus the `schema_meta` version
mirror. Nine mirror the artifact hierarchy: `epochs` → `generations` →
`experiments` → `patches`, and `generations` → `runs` → `loss_profiles` /
`metric_counts` / `judge_losses`, with `tournaments` as the comparison
record. The remaining four are `reflections` and `judge_scorecards`
(§3.10), `pareto_frontier` (§3.11), and `ingest_cursors` (§5.2). `ingest_cursors` is the one table that is not a
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
│ tournaments  │   one crowning row per challenger, plus one field row
└──────────────┘   per multi-candidate tournament
```

All `*_id` columns are the same string identifiers used in the
filesystem layout (`epoch_id` is the epoch directory name,
`generation_id` is `v0` / `v1` / ..., `entry_id` is the board entry
id). `run_id` is the measurement's runtime identifier,
`{seed-qualifier}.{purpose}.r{draw}.{sha256}` (from
`zicato.core.workspace.run_id_for_unit`), where the hash covers the
epoch, generation, and entry ids. The run's coordinates therefore
trace any index row back to its canonical file.

Schema versioning is stamped two ways by `apply_schema`: the SQLite
`PRAGMA user_version` (the authoritative source, readable from any
client) and a one-row `schema_meta` table (a human-legible mirror,
not part of the cross-language contract). A consumer that opens a
database whose `user_version` does not equal `SCHEMA_VERSION` should
treat the index as stale and run `zicato repair index`.

### 3.1 `epochs`

One row per epoch directory. Projection of `lineage.json` plus
the epoch's `config.json` (`EpochConfig`).

| Column | Type | Source |
|---|---|---|
| `epoch_id` | TEXT PK | epoch directory name |
| `contract_hash` | TEXT | `EpochConfig.contract_hash` |
| `created_at` | TEXT | `lineage.json` |
| `closed` | INTEGER | 1 once the epoch is closed |
| `goal` | TEXT | the epoch's goal |
| `parent_epoch_id` | TEXT | predecessor epoch id, cross-epoch lineage |

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
and `promoted` columns are the two that the targeted
`zicato repair generations` command rewrites (§4.3) — they are the
fields a buggy live dual-write was observed to leave stale. The `elo*`
columns are a **read-only analytics fold** (`src/zicato/index/elo.py`),
re-derived from scratch at every reindex and read only by the display
surfaces — never by the gate or the selection path.

### 3.3 `experiments`

One row per readable `experiment.json`, including the synthetic seed
marker a `v0` baseline carries.

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

Primary key `(epoch_id, generation_id)`. The detail the index gives no
dedicated column — mutation-point ids, the expected-pass-rate band, the
per-kind hypothesis match — lives inside the `hypothesis_json` and
`outcome_json` blobs, reached with SQLite's JSON functions
(`json_extract`, `json_each`). The mutation heat map in
[TOURNAMENT.md §4.5](TOURNAMENT.md#45-mutation-heat-map) reads the
modulating ids out of `hypothesis_json` that way rather than from a
separate column.

### 3.4 `patches`

One row per `patches/{patch_id}.json` file.

| Column | Type | Source |
|---|---|---|
| `patch_id` | TEXT PK | patch file `id` |
| `epoch_id` | TEXT | (FK) |
| `generation_id` | TEXT | (FK → `generations`) |
| `mutation_id` | TEXT | patch `mutation_id` |
| `op` | TEXT | the patch's `op` — `replace`, `set_numeric`, or `set_enum` |
| `rationale` | TEXT | patch `rationale` |

Patch *content* (`new_content`, `new_numeric`, `new_enum`) is **not**
indexed: it can be large and is never a query key. An operator
inspecting patch content opens the canonical
`patches/{patch_id}.json` file. The index holds
only what gets filtered or joined on.

### 3.5 `runs`

One row per measurement: every loss file under a generation's
`runs/{entry_id}/seed-{seed}/`, whatever its purpose, draw, or seed.

| Column | Type | Source |
|---|---|---|
| `run_id` | TEXT PK | the measurement's runtime identifier (§3) |
| `epoch_id` | TEXT | (FK) |
| `generation_id` | TEXT | (FK → `generations`) |
| `entry_id` | TEXT | board entry id |
| `started_at` | TEXT | `LossProfile.started_at` |
| `ended_at` | TEXT | `LossProfile.ended_at` |
| `aborted` | INTEGER | 1 if `LossProfile.wall_clock_budget_exceeded` |
| `runtime_ms` | INTEGER | `LossProfile.runtime_ms` |
| `tournament_id` | TEXT NULL | (FK → `tournaments`) — `{epoch_id}:{parent}->{generation}` from the generation's `experiment.json`; NULL when the generation has no parent |
| `match_id` | TEXT NULL | the matchup the run executed within (e.g. `rung0_m2`, `racing-final`); NULL outside a tagged matchup |

Primary key is `run_id`. The "which side of the tournament"
distinction is carried by the run's generation, and `tournament_id`
ties a challenger's runs to the round it was scored in. `idx_runs_tournament` indexes that association. The harmonograf
drill-down join key is the run's `adk_session_id`; the reducer stamps
it into `loss.json`, and no index column holds it. See
[TOURNAMENT.md §5](TOURNAMENT.md#5-the-harmonograf-split) and §6
below.

### 3.6 `loss_profiles`

The reduced per-run feature vector, for the one measurement per
(generation × board entry) that scoring selects: tournament draw 0 at
the seed the generation's `gen_score.json` records, with execution
evidence. Other purposes, draws, and seeds stay in `runs`,
`metric_counts`, and `judge_losses` for audit.

| Column | Type | Source |
|---|---|---|
| `run_id` | TEXT PK | (FK → `runs`) — the measurement's runtime identifier |
| `epoch_id` | TEXT | (FK) |
| `generation_id` | TEXT | (FK) |
| `entry_id` | TEXT | board entry id |
| `drift_loss` | REAL | `LossProfile.drift_loss` |
| `pass_fail` | INTEGER NULL | `LossProfile.pass_fail` (NULL when the entry has no `expectation`) |
| `runtime_ms` | INTEGER | `LossProfile.runtime_ms` |
| `wall_clock_budget_exceeded` | INTEGER | 1 if the run exhausted its wall-clock budget |
| `loss_json` | TEXT (JSON) | the full `LossProfile`, verbatim — the metric counts, plan revisions, and other fields that get no dedicated column live here |
| `tournament_id` | TEXT NULL | (FK → `tournaments`) — the round |
| `match_id` | TEXT NULL | `LossProfile.match_id` |
| `cached` | INTEGER | 1 when the profile was served from the unit cache |
| `source_epoch`, `source_run` | TEXT | the epoch and run whose measurement a cached profile reuses |
| `abort_cause` | TEXT NULL | why the run aborted: budget exhaustion or an infrastructure cause |

Primary key `run_id` (matching `runs`). This table is the
scoring-side projection; the per-entry A/B grid in
[TOURNAMENT.md §4.2](TOURNAMENT.md#42-per-entry-ab-grid) joins the
parent and child generations' `loss_profiles` rows on `entry_id`.
Features the `LossProfile` carries but that are not promoted to
their own column (`plan_revisions`, `task_failure_ratio`, `score`,
`metrics`, the metric counts) are recoverable from `loss_json` with
`json_extract`; the metric counts are *also* unpivoted into
`metric_counts` (§3.7) for `GROUP BY`-able access. `idx_loss_tournament` indexes the tournament association.

### 3.7 `metric_counts`

The run's named measurements, one row per `MetricCount` the
`LossProfile` scores (`LossProfile.scoring_metrics()`): every drift
observation under the `drift:` namespace, including custom-judge drift
as `drift:custom:<judge_name>`, plus the `cost:`, `output:`, and
`schema:` values the reducer derives. Storing them unpivoted makes them
`GROUP BY`-able.

| Column | Type | Source |
|---|---|---|
| `run_id` | TEXT | (FK → `runs`) |
| `namespace` | TEXT | the metric name's prefix without its colon, e.g. `drift` or `cost` |
| `name` | TEXT | the full metric name, e.g. `drift:confabulation_risk` or `drift:custom:cite-before-metric` |
| `severity` | TEXT | the drift severity (`info` / `warning` / `critical`); empty for non-drift metrics |
| `count` | REAL | the measured value |

No primary key declared; the table is reached by `run_id` (the
`idx_metric_run` index) and aggregated. The drift-kind heatmap on the
dashboard's epoch view
([DASHBOARD.md §4.4](DASHBOARD.md#44-the-epoch-level)) is a
`SUM(count) GROUP BY name` over this table joined to `runs` for the
round; the `drift:custom:<judge_name>` rows give the same view sliced
by judge rather than by `DriftKind`. The per-judge weighted
loss is also materialised in its own table — `judge_losses` (§3.9).

### 3.8 `tournaments`

Two kinds of row share this table:

- A **crowning row** per resolved challenger, from its
  `experiment.json` outcome, keyed `{epoch_id}:{parent}->{child}`. It
  describes that challenger's crowning duel against the champion.
- A **field row** per tournament with three or more competitors, from
  the round's `tournaments/field-{first_challenger}.json` snapshot, keyed
  `{epoch_id}:field:{first_challenger}`. It leaves the parent and child
  columns empty and carries the whole field's pairings and standings.

| Column | Type | Source |
|---|---|---|
| `tournament_id` | TEXT PK | the stable id above |
| `epoch_id` | TEXT | (FK) |
| `parent_generation_id` | TEXT | the reigning champion's id (empty on a field row) |
| `child_generation_id` | TEXT | the challenger's id (empty on a field row) |
| `decision` | TEXT | `promoted`, `rejected`, or `deferred` |
| `parent_scalar`, `child_scalar` | REAL NULL | NULL: the outcome records only the delta; the absolute scalars are in each generation's `gen_score.json` |
| `delta_scalar` | REAL | child − parent |
| `rejection_reason` | TEXT NULL | gate's reason if rejected |
| `ran_at` | TEXT | when the round was scored |
| `structure` | TEXT | the tournament structure (`racing`, `gauntlet`, …) |
| `structure_params_json` | TEXT (JSON) | the structure's params (field rows) |
| `competitors_json` | TEXT (JSON) | the competing generation ids |
| `rounds_json` | TEXT (JSON) | the match-by-match record |
| `standings_json` | TEXT (JSON) | the field's standings (field rows) |
| `field_status_json` | TEXT (JSON) | each competitor's field status (field rows) |
| `champion_eval_mode` | TEXT | how the champion side was evaluated: `full`, `fast`, or `fast-degraded` |
| `champion_run_ref` | TEXT NULL | the champion generation's workspace-relative directory |

Primary key `tournament_id`. This is the table that backs the
tournament bracket and the per-matchup detail in
[TOURNAMENT.md](TOURNAMENT.md): the bracket *is* `SELECT * FROM
tournaments WHERE epoch_id = ? ORDER BY ran_at`. `runs` and
`loss_profiles` carry a `tournament_id` FK back to this table so a
round's full per-entry detail is one join away.

### 3.9 `judge_losses`

One row per (run × custom judge) — the per-judge weighted-loss
breakdown that the scoring layer's `per_judge_weights` produces
(see [SCORING.md §2.2](SCORING.md#22-the-judge-channel)).

| Column | Type | Source |
|---|---|---|
| `run_id` | TEXT | (FK → `runs`) — the `{generation_id}--{entry_id}` id |
| `judge_name` | TEXT | the custom judge's `name` |
| `weighted_loss` | REAL | `raw_loss × weight` — the judge's contribution to the `judge:` channel |
| `raw_loss` | REAL | the judge's unweighted loss |
| `weight` | REAL | the `per_judge_weights` weight applied |

Primary key `(run_id, judge_name)`, indexed by
`idx_judge_losses_run`. Where `metric_counts` (§3.7) carries the
raw per-judge *counts*, `judge_losses` carries the per-judge
*weighted loss* — the hypothesis ledger and the per-judge
attribution panels read this table directly rather than
re-deriving the weighting from counts × weights.

### 3.10 `reflections` and `judge_scorecards`

The board-reflection projection
([BOARD-REFLECTION.md](BOARD-REFLECTION.md)). `reflections` holds one
row per reflection directory under `epochs/{e}/reflections/`, keyed on
`reflection_id`: the epoch, creation time, mode, whether the run
executed, the headline reliability numbers
(`noise_floor_max_abs_delta`, `decision_flip_p`), the finding and judge
counts, and the corpus-wide TP/FP/FN/TN/ambiguous tally as
`verdict_counts_json`. `judge_scorecards` holds one row per
(reflection × judge) from that reflection's `scorecards.json`: the
confusion counts, `precision`, `recall`, `f1`, `severity_accuracy`,
`disagreement_rate`, `kappa` (the scorecard's `self_consistency_kappa`),
`exercised`, and `redundant_with_json`. Primary keys are
`reflection_id` and `(reflection_id, judge_name)`.

### 3.11 `pareto_frontier`

The projection of an epoch's canonical `epochs/{e}/pareto_frontier.json`
record: one row per frontier member and one per retirement, with the
admission and retirement rounds, the retirement reason, the champion at
the time, the generation's scalar, and its per-axis values
(`axis_values_json`, `beats_champion_on_json`). The primary key is
`(epoch_id, generation_id, status, round_retired)`, because one
generation can be admitted, retired when it is crowned, and admitted
again. An unreadable record is skipped with a warning and leaves the
epoch's existing rows in place (§5.1).

## 4. `zicato repair index`

`zicato repair index` rebuilds `index.db` from the filesystem. It is
the correctness backstop for the whole index design. It is an
advanced / off-the-happy-path command — `zicato evolve` keeps the
index current via the live dual-write, so an operator reaches for
it only to repair a behind-or-corrupt index.

```
zicato repair index [--workspace <path>]
```

`--workspace` is the **only** flag (default `.zicato`). There is no
`--epoch` and no `--verify` — the command always rebuilds the whole
workspace.

It is not the *routine* path. `zicato evolve` builds an absent index
and heals a diverged one at its own start (§5), so an operator reaches
for the command only in the situations §5.4 names.

### 4.1 Behaviour

`zicato repair index`:

1. Acquires the workspace writer lease after delegated workers finish.
2. Creates a private SQLite file with the supported schema.
3. Walks every canonical epoch, generation, experiment, measurement,
   reflection, and frontier record through its owning reader.
4. Commits and closes the scratch database, then publishes it over the
   derived index. A failure before publication preserves the existing file.
5. Acknowledges the captured epoch revisions and prints indexed row counts.

```
$ zicato repair index
Rebuilt index at /home/op/myagent/.zicato/index.db.
  2 epochs, 13 generations, 12 experiments indexed.
  130 runs, 26 loss profiles, 410 metric counts, 12 tournaments indexed.
```

### 4.2 One supported schema

`SCHEMA_VERSION` is **15**. It identifies both the SQLite layout and the
projection semantics. `apply_schema` stamps `PRAGMA user_version` and its
`schema_meta` mirror only when creating an empty database.

`ensure_index` rebuilds every incompatible version from canonical records.
Incremental writers raise `IndexSchemaError` before changing an incompatible
database. They cannot run a full repair while sibling workers are active.
Python and supervisor queries open read-only and admit only the supported
version. An incompatible index produces unavailable analytical results until
its existing repair owner rebuilds it.

### 4.3 `zicato repair generations` — targeted repair

Alongside the full rebuild, zicato ships a narrow repair command:

```
zicato repair generations [--workspace <path>]
```

The command reconciles indexed generation facts with `lineage.json` under
the workspace writer lease. Parent coordinates, promotion state, creation
timestamps, and birth rounds match the canonical record, including null
values. Ratings and other index tables remain unchanged. Repeating the
repair makes no changes, and canonical files are read only. Use
`zicato repair index` to rebuild the complete projection.

### 4.4 The index after a crash

When `zicato evolve` restarts after a crash (the resume protocol is in
[ROBUSTNESS.md §2.6](ROBUSTNESS.md#26-atomic-writes-and-resume-markers)
and [RUNTIME.md](RUNTIME.md)), its start-of-invocation `ensure_index` and
`heal_index` (§5.3) bring the index current. Because the index can only
ever be *behind* the filesystem (§2.3), the catch-up re-projects the
epochs whose canonical records changed after their last complete
projection. The resume protocol proceeds against the canonical files;
the index is brought current so the dashboard's analytics and the
proposer's experiment memory are correct from the first round after
restart.

## 5. Self-healing: the index maintains itself

`zicato repair index` (§4) is the *forensic* tool. Nothing on the happy
path should ever require an operator to run it. This section
specifies the three mechanisms that make that true, the literal
seam signatures they add, the cursor schema they persist, and the
concurrency rule that governs when a heal or a build may run.

The staleness these mechanisms close costs loop quality. The proposer
reads the index *during* `evolve`: `prior_experiments_for_epoch` supplies the
experiment memory, and the mutation track record supplies the
per-mutation-point hit rate. A stale index does not fail loudly —
it silently returns *fewer* prior experiments, and the loop
degrades in quality with no error anywhere. Keeping the index
current is a loop-quality property rather than a convenience.

### 5.1 Missing or incompatible indexes are rebuilt before publication

```python
# zicato.index.ingest
def ensure_index(
    workspace_root: Path,
    db_path: Path | None = None,
    *,
    action_out: list[str] | None = None,
    writer: WorkspaceLock | None = None,
) -> Path: ...
```

`ensure_index` guarantees that, on return, `index.db` exists and
carries the current `SCHEMA_VERSION`. It builds when — and only
when — one of three things is true:

| Condition | `action_out` value |
|---|---|
| the file is absent | `built:absent` |
| `PRAGMA user_version` != `SCHEMA_VERSION` | `built:stale-schema` |
| the file is not a readable SQLite database | `built:unreadable` |
| none of the above | `present` |

An **equal-version** database is never rebuilt by this rule. Detecting
that its *contents* drifted from the workspace belongs to the cursor
validation and heal (§5.2); the auto-build answers only the structural
question "is there a database of the right shape here at all".

Every version mismatch follows the same rebuild path. No historical table
or column shape is interpreted during incremental ingestion. A rebuild walks
all canonical records before publishing the derived database.

**Temp-then-rename.** Every build — `ensure_index`'s and
`rebuild_index`'s alike — goes through one private helper:

```python
def _build_index_atomically(workspace_root: Path, target: Path) -> None:
    # 1. sweep scratch left by builders that are no longer alive
    # 2. build the FULL index into {target}.{pid}.{uniq}.tmp
    # 3. unlink the OUTGOING file's {target}-wal / {target}-shm
    # 4. os.replace({target}.{pid}.{uniq}.tmp, target)
```

**Two properties of that sequence carry its safety.**

*Each build owns a unique scratch path.* Builds and repairs acquire the
workspace writer lease, or validate the invocation's supplied handle. A
competing invocation cannot enter the build. Scratch names remain unique so
an interrupted builder's files cannot be mistaken for an active build. The
scratch sweep only reclaims files whose recorded process has exited.

*The outgoing sidecars are cleared BEFORE the rename rather than after
it.* A write-ahead log (WAL) left beside a database it does not belong
to is **replayed rather than ignored**. SQLite validates WAL frames
with an internal checksum chain seeded from the WAL header's own salts,
and nothing ties those frames to the main file. So a complete WAL left
by the database that occupied this path before the rename is accepted
and recovered over the one that just replaced it, page 1 included,
carrying `user_version` and the whole schema. Clearing
the sidecars afterwards leaves a window in which that pair is
on disk: a crash inside it, or a reader that opens the pair and
*checkpoints* the foreign frames into the new file, silently resurrects
the **old** index in place of the new one. `PRAGMA integrity_check`
returns `ok` — it is a perfectly valid database, just the wrong one.
Clearing first means the new inode never coexists with a sidecar that
is not its own.

Building into a temp file retires a whole defect class. Building in
place would mean unlinking `index.db` first. Any failure during the
build — an unreadable canonical record, a full disk, a Ctrl-C — would
then leave the operator with a schema-only file and every table empty,
along the very path they ran to *recover* a bad index. Under temp-then-rename
a failed build leaves the existing database byte-untouched.
`rebuild_index` goes through the same helper, so `zicato repair index`
performs a full re-derivation from the files with no destroy-on-failure
hazard.

The frontier-projection guard — warn and skip on a corrupt
`pareto_frontier.json` rather than raise — sits *inside* the build.
The guard and temp-then-rename are complementary: the guard keeps one
bad record from aborting the build; the rename keeps an aborted build
from destroying the existing database.

### 5.2 Epoch revisions, cursor validation, and incremental heal

Before replacing an indexed canonical record, its writer atomically writes
a UUID to `index-revisions/<epoch>.revision`. UUID issuance needs no shared
read/increment step, so delegated workers can issue revisions concurrently.
Writers cover epoch configuration and lifecycle, lineage, experiments, loss
records, field tournaments, reflections, and Pareto frontier records.

A complete epoch projection captures the revisions before reading records.
After SQLite commits, repair writes that snapshot to the database's derived
`<database-name>.revisions.json` sidecar. A full rebuild acknowledges only
after publishing the complete database. A crash before acknowledgement leaves
the revision pending; a later canonical mutation carries a different UUID and
cannot be cleared by an earlier snapshot. Each database has its own sidecar,
so building an alternate index does not acknowledge the default index.

Incremental ingestion never acknowledges epoch revisions. Updating one run
cannot prove that an earlier update to another record reached SQLite. Ordinary
writes therefore require a complete epoch heal before the next settled-memory
read or invocation completion, even if their incremental projections succeeded. This conservative
cost keeps incomplete coverage visible. Manual edits that bypass the writer
APIs require a full `zicato repair index` rebuild.

The cursor table also detects added or removed records:

```sql
CREATE TABLE IF NOT EXISTS ingest_cursors (
  epoch_id                  TEXT PRIMARY KEY,
  experiments_count         INTEGER,
  runs_count                INTEGER,
  round_dirs_count          INTEGER,
  reflections_count         INTEGER,
  lineage_generations_count INTEGER,
  last_ingested_at          TEXT
)
```

| Column | Stamped from | What it records |
|---|---|---|
| `experiments_count` | **index** | `experiments` rows for this epoch |
| `runs_count` | **index** | distinct `(generation_id, entry_id)` in `runs` for this epoch |
| `round_dirs_count` | workspace | entries under `epochs/{e}/rounds/` |
| `reflections_count` | workspace | directories under `epochs/{e}/reflections/` |
| `lineage_generations_count` | workspace | generation entries for this epoch in `lineage.json` |
| `last_ingested_at` | — | when this epoch was last projected (observational) |

Each is compared against the matching **workspace** signal:
`experiments_count` against generation directories holding an
`experiment.json`, `runs_count` against `loss.json` files under
`generations/*/runs/*/`, and the other three against themselves as
they were at the last projection.

**The `Stamped from` column is the one that matters.** A cursor is
useful only when it says something the workspace does not already say.

*Index-stamped, where a 1:1 counterpart exists.* `experiments_count`
and `runs_count` record what this index actually **holds**, so a
comparison against the workspace detects rows that were never
projected. Stamping them from the workspace instead is what made a
crashed dual-write invisible. `_refresh_cursor` runs after every
incremental `ingest_*`, so a workspace-stamped count recorded the files
that were *on disk*, including any the crashed write never projected.
The epoch then validated clean **forever** against an index that did
not hold them. That state is self-consistent and wrong,
which is the one failure mode a staleness signal must not have.

*Workspace-stamped, where no counterpart exists.* The index has
nothing to count for the other three: nothing projects `rounds/` at
all, a reflection *directory* need not yield a row, and the
`generations` table is the union of lineage ids and on-disk
directories rather than the lineage list this signal counts. For these
the cursor means "what the workspace looked like when this epoch was
last projected", and a change since then is the divergence.

`runs_count` is also the signal that makes a crashed dual-write
*detectable at all*. Everything else an epoch accumulates is bracketed
by an experiment; if a round's runs reduced but the process died before
`ingest_run` projected them, no other count moves.

Every count is **cheap**: directory-entry counts and stats, never a
file parse. `lineage.json` is read once for the whole workspace rather
than once per epoch. Validation must be affordable enough to
run at every `evolve` start on a large workspace, which rules out
re-deriving row content to compare it.

The honest cost of index-stamping: a canonical file the projection
cannot read — `_load_loss_profile` returns `None` on a malformed
`loss.json` — is counted by the workspace signal and yields no row, so
the epoch stays divergent and is re-projected once per `evolve` start.
The cost is bounded, and correct in the sense that matters: the index
cannot represent that file, and saying so repeatedly beats recording
that it can.

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
    workspace_root: Path, db_path: Path | None = None,
    *, writer: WorkspaceLock | None = None,
) -> tuple[str, ...]: ...
```

`validate_index` returns the sorted ids of **diverged** epochs.
Four conditions count as divergence:

1. an epoch on disk with no cursor row, because no full epoch projection
   has completed; incremental ingestion only refreshes an existing cursor,
2. an epoch whose cursor row disagrees with any of the five
   signals,
3. an epoch the **index still holds rows for** that is gone from the
   workspace. This set is the union of the cursor table and
   `SELECT DISTINCT epoch_id FROM epochs`, rather than the cursor table
   alone. An incrementally ingested epoch can hold rows before its first
   complete projection establishes a cursor,
4. an epoch revision differs from that database's acknowledged revision,
   including replacements that leave every count unchanged.

`heal_index` re-ingests those epochs and no others, and returns the
ids it healed. For each one it deletes that epoch's rows and re-projects
via the existing `_rebuild_epoch` machinery; for case 3 it deletes
and stops. The delete is epoch-scoped across **every** table, which
matters because three of them carry no `epoch_id` column and must be
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
cross-epoch analytics fold rather than per-epoch rows — deleting and
re-inserting one epoch's generations nulls them, and only a
whole-ledger re-fold restores what a from-scratch rebuild would
have produced.

**The convergence pin.** Heal-then-read and rebuild-from-scratch
must agree. The determinism test corrupts an index (drops one
epoch's rows), heals it, and asserts the SQL `.dump` equals a
from-scratch rebuild's `.dump`. Two cells are outside the pin, both
for the same reason — they are observational rather than derived:

- `ingest_cursors.last_ingested_at` is a wall clock, normalised to
  `<TS>` in the same way the REINDEX-DUMP parity gate already
  normalises every ISO timestamp in the dump.
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

### 5.3 The routine paths, and the concurrency rule

**(a) Invocation startup, proposal memory, and completion.** The `evolve_n_rounds` preflight runs
`ensure_index` then `heal_index` under `best_effort`, and emits a
single log line naming what it did:

```
index: built fresh (absent)
index: healed epochs 2026-08-02_e1, 2026-08-02_e2
index: fresh
```

Render conformance: the log line says what the heal did rather than
only that it ran. A fresh build makes the following heal redundant,
because the build writes every cursor, so the log reports the two as
alternatives rather than in sequence.

The invocation forwards its validated writer handle to the index preflight.
Recovery and delegated worker cleanup must finish before repair reads records
and acknowledges revisions. Any remaining active worker record prevents repair.

Each candidate batch refreshes dirty epochs before reading settled experiment
memory. Its mutation track records use the same settled projection. This read
boundary also covers later rounds within one invocation, including replacements
that leave file counts unchanged.

After canonical producers and services finish, invocation cleanup repairs the
index before releasing its writer. Successful completion leaves the projection
current. A repair failure logs a warning and leaves its revision markers pending;
it does not replace an execution failure or change a promotion decision.

**(b) The dashboard / query read path.** `run()` calls `ensure_index`
**only**, once at server start, never per request. The seam is `run`
rather than `create_app` because `run` is the process-start path both
real launches come through, and building an ASGI app must not have
filesystem side effects.

No **schema-version pre-check** sits in front of that call. A
pre-check that asked `index_schema_version(...) == SCHEMA_VERSION`
first and returned on a match would be cheap, but it would decide the
question `ensure_index` exists to decide, and decide it differently. On
a file that is not a SQLite database at all such a pre-check *raises*,
the best-effort guard swallows the exception, and the dashboard never
repairs the file; `_rebuild_reason` classifies that same file as
`unreadable` and rebuilds it. A pre-check would therefore put the
`built:unreadable` outcome §5.1 documents out of reach from this path,
by running the cheap check in front of the one that classifies the
file. `ensure_index` returns without writing when the index is current,
which is all such a pre-check would buy.

A full heal stays off the read path. Healing writes;
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

> Every build and heal acquires the workspace writer lease or validates the
> supplied `WorkspaceLock`. A second invocation cannot enter, including another
> invocation in the same process. Delegated workers must finish before repair;
> a parent lease alone does not prove their records are complete.

The dashboard skips when another writer owns the workspace. Its preliminary
lock check avoids unnecessary work; acquisition inside `ensure_index` closes
the race between that check and the build. The explicit `zicato repair index`
command uses the same lease protocol. It cannot replace a database while an
invocation is still writing canonical records.

### 5.4 What still requires `zicato repair index`

Routine index maintenance is automatic. Three situations still call for
the explicit command:

- **Manual edits outside the canonical writer APIs.** Changing values or
  swapping files directly does not issue an epoch revision. If those edits
  preserve every cursor count, validation cannot detect them. A full rebuild
  reads the canonical records again and projects the edited values.
- **Determinism assertion.** Proving the index equals a pure
  re-projection of the files (what the REINDEX-DUMP parity gate
  does) requires the from-scratch path by definition.
- **Anything broader than an epoch.** The heal's unit is the epoch;
  a suspected defect that is not epoch-scoped is a rebuild.

## 6. Where SQLite is, and is NOT, used

zicato has three distinct storage concerns, and SQLite is the right
answer for one of them. This section draws the lines because "use
SQLite" is a tempting default that would be wrong for the other two.
[STORAGE.md §2](STORAGE.md#2-the-settled-mechanism-for-each-kind)
lays out the same split from the storage side, across the five kinds
of data a workspace persists.

| Concern | Substrate | Why not SQLite |
|---|---|---|
| **Generation source trees** | git commits, or directory snapshots | The data is intrinsically file-shaped; git is the file-shaped versioner and gives `diff` / `log` / `blame` / `bisect` for free. SQLite blobs would give smaller storage and *no tooling*. See [STORAGE.md](STORAGE.md). |
| **Per-run event capture** | `events.jsonl`, one file per run | The access pattern is append-while-running, tail-for-the-log-panel, stream-to-SSE, and replay-once in the reducer. An append-only line-delimited file wins every one of those. A row-per-event SQLite table would add write contention during the run and buy nothing — events are never queried *across* runs (the reducer's `LossProfile` is). See [TELEMETRY.md](TELEMETRY.md). |
| **Cross-run analytical views** | `.zicato/index.db` — **SQLite** | The access pattern is `GROUP BY` / `JOIN` over reduced features across many generations. This is what a relational index is for. |

The principle: SQLite is used for the **derived, queried,
cross-cutting** layer, and *only* there. Source trees go to git;
event capture goes to JSONL. The index never absorbs either —
it projects *from* them. A run's `events.jsonl` is reached from
its index row by reconstructing the path from the run coordinate
(`{epoch}/generations/{gen}/runs/{entry}/seed-{seed}/events.{purpose}.r{draw}.jsonl`); the
harmonograf drill-down uses the run's `adk_session_id` (in
`loss.json`). The index holds the *reduced* features rather than the
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
  legitimate analytical destination rather than a foreign element.
- **harmonograf**'s server stores its run records in SQLite —
  the live console reads its served data from a SQLite database.

zicato's `index.db` sits in the same family: a SQLite store used
as a fast, queryable projection, downstream of a canonical
representation. Where zicato differs from `SqliteSink` is the
*role*: `SqliteSink` is a capture sink (a writer in the live
event path); `index.db` is an analytical index (a derived view,
never in the event path). The two are not interchangeable: zicato
uses the JSONL sink for capture and the SQLite index for views.

## 7. Readers of `index.db`

Two processes read the index, and neither writes it:

- **The Python dashboard service** (`zicato.dashboard`, spawned by
  `zicato evolve` or run as `zicato dashboard`) serves the console. Its
  query layer (`zicato.query`) opens `index.db` read-only through
  `query/_sqlite.py` (a SQLite URI with `mode=ro`).
- **The Rust supervisor** (`crates/supervisor`, see
  [RUNTIME.md](RUNTIME.md) §3) opens it read-only through the
  **`rusqlite`** crate (`index_db.rs`, `SQLITE_OPEN_READ_ONLY`). `zicato
  evolve` spawns the supervisor with `--no-dashboard`, so there it runs
  only the watchdog and `/statusz`; the supervisor's own dashboard routes,
  and their index queries, serve only when an operator runs the binary
  without that flag.

```
┌────────────────────────────┐   ┌───────────────────────────┐   ┌───────────────────────────┐
│  zicato evolve (Python)    │   │ dashboard service (Python)│   │ zicato-supervisor (Rust)  │
│  dual-writes index.db      │   │ query layer, mode=ro      │   │ rusqlite, READ_ONLY       │
│  canonical-file-first      │   │                           │   │ (dashboard mode only)     │
└─────────────┬──────────────┘   └─────────────┬─────────────┘   └─────────────┬─────────────┘
              │ writes                   reads │                         reads │
              ▼                                ▼                               ▼
        ┌───────────────────────────────────────────────────────────────────────────┐
        │                        .zicato/index.db (SQLite)                          │
        └───────────────────────────────────────────────────────────────────────────┘
```

Properties of the read path:

- **Read-only handles.** Both readers open the database read-only, so
  the single-writer rule (§2.4) is enforced by the open mode rather than
  by convention alone.
- **WAL mode.** Writers open the database in write-ahead-log mode
  (`PRAGMA journal_mode=WAL`). WAL lets reads proceed concurrently with
  the orchestrator's writes without either blocking the other — the
  reader sees a consistent snapshot as of its last completed
  transaction.
- **Settled views only.** Per-entry live status comes from the
  `.zicato/runtime/` state files (which change every second); the
  index serves the *settled* cross-run views.
- **Graceful absence.** If `index.db` does not exist (a fresh
  workspace, or one where no index has been built), the readers
  degrade: the live panels driven by `.zicato/runtime/` still render,
  and the analytical panels show an empty state. The dashboard never
  hard-fails on a missing index.

Why the readers query the index rather than walking the filesystem: the
projection rules live in one place (the Python ingest in
`zicato.index.ingest`), and every reader consumes their output.
Re-implementing the JSON-walk-and-aggregate logic in each reader would
duplicate those rules, and the copies would drift. The schema in §3 is
the contract between the writer and its readers.

## 8. Cross-references

| Topic | Document |
|---|---|
| The original "add an index sidecar" prediction | [RATIONALE.md §7](RATIONALE.md#7-why-the-canonical-layout-is-the-filesystem-rather-than-sqlite) |
| The storage concerns, from the storage side | [STORAGE.md §2](STORAGE.md#2-the-settled-mechanism-for-each-kind) |
| Generation trees stored as git commits | [STORAGE.md](STORAGE.md) |
| Event capture → `events.jsonl` (no SQLite) | [TELEMETRY.md](TELEMETRY.md) |
| The `LossProfile` shape the index projects | [TELEMETRY.md](TELEMETRY.md), [SCORING.md §2](SCORING.md#2-the-metric-channels) |
| `experiment.json` / `gen_score.json` the index derives from | [EPOCHS-AND-JOURNALING.md §3](EPOCHS-AND-JOURNALING.md#3-the-experiment) |
| The tournament analytics the index backs | [TOURNAMENT.md §4](TOURNAMENT.md#4-tournament-detail-analytics) |
| The dashboard service and supervisor binary that read the index | [DASHBOARD.md](DASHBOARD.md), [RUNTIME.md](RUNTIME.md) |
| `zicato repair index` in the CLI reference | [CLI.md](CLI.md) |
| The workspace lock the heal/build rule defers to | [RUNTIME.md](RUNTIME.md) |
| The component map placing the index in the meta-loop | [ARCHITECTURE.md](ARCHITECTURE.md) |
</content>
</invoke>
