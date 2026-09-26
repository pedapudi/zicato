---
name: zicato-index-ops
description: Rebuild the derived SQLite analytical index (`zicato repair index`) and run read-only SQL against `.zicato/index.db` for cross-run analytics. Use when the index looks stale after a hand-edit, or when you want to query loss/tournament/judge history across many runs.
---

# zicato index-ops (the analytical index)

`.zicato/index.db` is a **derived, rebuildable SQLite projection** of the
canonical workspace files. The filesystem (`experiment.json`, the per-run
loss records, `lineage.json`, each round's `field_settlement.json`, …) is the
source of truth; the index is a query cache. Never hand-edit `index.db`;
rebuild it instead. `evolve` and the dashboard build a missing or
wrong-version index and re-project any epoch whose files changed at startup,
so a routine rebuild is rarely needed; after a hand-edit of a canonical file,
rebuild so the index re-derives (AGENTS.md rule 4).

> Guardrails: `zicato repair index` is read-only against workspace files and
> spends no LLM budget — safe to run. All SQL below must be **read-only SELECTs**. Use
> `.venv/bin/zicato`; do not run `uv sync`. Do not modify an existing `.zicato/`
> you were not asked to touch.

## Rebuild

```sh
Z=.venv/bin/zicato

# Full rebuild: builds a private scratch database from the canonical files,
# then publishes it over index.db; a failed build keeps the existing index.
# Prints the counts of epochs / generations / experiments / runs / loss
# profiles / metric counts / tournaments indexed.
$Z repair index                          # defaults to --workspace .zicato
$Z repair index --workspace path/to/.zicato

# Targeted repair of ONLY the generations table (parent, promotion state,
# creation time, birth round) from the resolved lineage. Idempotent,
# read-only against workspace files; other tables are untouched.
$Z repair generations
```

> Both commands take **only** `--workspace`. There is no `--verify` integrity
> check and no per-epoch rebuild; a rebuild is always whole-workspace. Verify
> with `.venv/bin/zicato repair index --help` before scripting a flag.

A cheap stand-in for a `--verify`: copy the workspace, rebuild the copy, and
compare the printed counts or the row counts from the SQL below against the
live index. Exit-1-on-drift behaviour does not exist.

## Schema (`src/zicato/index/schema.py`, SCHEMA_VERSION 15)

`PRAGMA user_version` is authoritative; a mismatch means run `zicato repair index`.
(The version number rises as columns are added — read `SCHEMA_VERSION` in
`schema.py` rather than trusting any number here.) Tables and their key columns:

- **epochs** — `epoch_id` (PK), `contract_hash`, `created_at`, `closed`,
  `goal`, `parent_epoch_id`.
- **generations** — (`epoch_id`,`generation_id`) PK, `parent_generation_id`,
  `promoted`, `created_at`, `round_index`, plus the rating columns `elo`,
  `elo_se`, `elo_games`. The ratings come from a **batch Bradley-Terry /
  Plackett-Luce fit** folded in at the END of a rebuild, over the whole
  `tournaments` match ledger — order-independent, so any re-derivation reproduces
  identical ratings. `elo_games` counts the observations a generation appeared in
  (a racing rung counts once per survivor and per cut arm), and a generation that
  appeared in none has NULL `elo`/`elo_se` rather than a carried-forward number.
  Ratings are visibility only, never gate inputs, and the fold is best-effort —
  a fold failure leaves the canonical rows intact with the rating columns unset.
- **experiments** — (`epoch_id`,`generation_id`) PK, `hypothesis_core_idea`,
  `hypothesis_why`, `hypothesis_json`, `tournament_decision`,
  `rejection_reason`, `scalar_score_delta`, `drift_loss_delta`,
  `pass_rate_delta`, `outcome_json`.
- **patches** — `patch_id` (PK), `epoch_id`, `generation_id`, `mutation_id`,
  `op`, `rationale`.
- **runs** — `run_id` (PK), `epoch_id`, `generation_id`, `entry_id`,
  `started_at`, `ended_at`, `aborted`, `runtime_ms`, `tournament_id`,
  `match_id`. **One row per measurement**: every purpose (tournament duels,
  calibration, pre-flight probes, screens, confirmations) and every draw. The
  `run_id` spells the measurement, e.g.
  `seed-none.tournament.r1.<digest>`; `match_id` names what it served
  (`gauntlet`, `aa-calibration:0`, `contract-preflight:degraded:<id>`, …).
- **loss_profiles** — `run_id` (PK), `epoch_id`, `generation_id`, `entry_id`,
  `drift_loss`, `pass_fail`, `runtime_ms`, `wall_clock_budget_exceeded`,
  `loss_json`, `tournament_id`, `match_id`, `cached`, `source_epoch`,
  `source_run`, `abort_cause`. (The continuous per-entry `score` / `metrics` stay
  inside `loss_json` — they are not promoted to columns; read them via
  `json_extract(loss_json, '$.score')` / `'$.metrics'`.)
  **One row per `(generation, entry)`**: only the tournament measurement at
  draw 0 under the selected seed is projected here. Further replicates and
  every non-tournament measurement stay in `runs` / `metric_counts` /
  `judge_losses` and in the workspace files.
- **metric_counts** — `run_id`, `namespace` (the prefix without its colon,
  e.g. `cost`), `name` (the full metric name, e.g. `cost:llm_calls`),
  `severity`, `count`; rows exist for every measurement in `runs`.
- **tournaments** — `tournament_id` (PK), `epoch_id`, `parent_generation_id`,
  `child_generation_id`, `decision`, `parent_scalar`, `child_scalar`,
  `delta_scalar`, `rejection_reason`, `ran_at`, plus the structure columns
  `structure`, `structure_params_json`, `competitors_json`, `rounds_json`,
  `standings_json`, `field_status_json`, `champion_eval_mode`,
  `champion_run_ref`.
- **judge_losses** — (`run_id`,`judge_name`) PK, `weighted_loss`, `raw_loss`,
  `weight`.
- **reflections** — `reflection_id` (PK), `epoch_id`, `created_at`, `mode`,
  `executed`, `noise_floor_max_abs_delta`, `decision_flip_p`, `n_findings`,
  `n_judges`, `verdict_counts_json`.
- **judge_scorecards** — (`reflection_id`,`judge_name`) PK, the confusion counts
  `tp`/`fp`/`fn`/`tn`/`ambiguous`, `precision`, `recall`, `f1`,
  `severity_accuracy`, `disagreement_rate`, `kappa`, `exercised`,
  `redundant_with_json`. Both tables are written by `zicato inspect reflection run` (board
  reflection) rather than by the evolve loop — empty in a workspace that never reflected.
- **pareto_frontier** — `epoch_id`, `generation_id`, `status`,
  `round_admitted`, `round_retired`, `retired_reason`,
  `champion_generation_id`, `scalar`, `axis_values_json`,
  `beats_champion_on_json`.
- **ingest_cursors** — per-epoch counts of the canonical files last projected
  (`experiments_count`, `runs_count`, `round_dirs_count`,
  `reflections_count`, `lineage_generations_count`, `last_ingested_at`); the
  startup check compares them with the workspace to find epochs to
  re-project.
- **schema_meta** — `key`, `value`: a readable mirror of the schema version.

## Read-only queries

Open the DB read-only so nothing can mutate it:

```sh
sqlite3 -readonly .zicato/index.db
# without the sqlite3 CLI:
.venv/bin/python -c "import sqlite3,sys; c=sqlite3.connect('file:.zicato/index.db?mode=ro', uri=True); [print(r) for r in c.execute(sys.argv[1])]" "SELECT ..."
```

```sql
-- Schema version (should equal SCHEMA_VERSION in schema.py; else reindex).
PRAGMA user_version;

-- Tournament ledger for an epoch: who won, by how much, and why a reject.
SELECT child_generation_id, decision, parent_scalar, child_scalar,
       delta_scalar, rejection_reason
FROM tournaments
WHERE epoch_id = '<epoch_id>'
ORDER BY ran_at;

-- Hypothesis vs outcome per generation (the journal in tabular form).
SELECT generation_id, hypothesis_core_idea, tournament_decision,
       scalar_score_delta, drift_loss_delta, pass_rate_delta
FROM experiments
WHERE epoch_id = '<epoch_id>'
ORDER BY generation_id;

-- Which mutation points get touched most (where the proposer keeps poking).
SELECT mutation_id, op, COUNT(*) AS n
FROM patches
WHERE epoch_id = '<epoch_id>'
GROUP BY mutation_id, op
ORDER BY n DESC;

-- Mean drift-loss and pass-rate per generation across the board (replicate 0).
SELECT generation_id,
       ROUND(AVG(drift_loss), 4) AS mean_drift,
       ROUND(AVG(pass_fail), 3)  AS pass_rate,
       COUNT(*)                  AS runs
FROM loss_profiles
WHERE epoch_id = '<epoch_id>'
GROUP BY generation_id
ORDER BY generation_id;

-- Per-judge weighted loss for one generation (per_judge_weights effect).
SELECT j.judge_name,
       ROUND(AVG(j.weighted_loss), 4) AS mean_weighted,
       ROUND(AVG(j.weight), 3)        AS weight
FROM judge_losses j
JOIN loss_profiles lp ON lp.run_id = j.run_id
WHERE lp.epoch_id = '<epoch_id>' AND lp.generation_id = 'v4'
GROUP BY j.judge_name
ORDER BY mean_weighted DESC;

-- Hottest drift metrics across the epoch (what keeps going wrong).
SELECT mc.namespace, mc.name, mc.severity, SUM(mc.count) AS total
FROM metric_counts mc
JOIN runs r ON r.run_id = mc.run_id
WHERE r.epoch_id = '<epoch_id>'
GROUP BY mc.namespace, mc.name, mc.severity
ORDER BY total DESC
LIMIT 20;

-- Lineage: parent → child chain and which generations were promoted.
SELECT generation_id, parent_generation_id, promoted
FROM generations
WHERE epoch_id = '<epoch_id>'
ORDER BY generation_id;
```

## See also

- `docs/design/ANALYTICAL-INDEX.md` — the files-canonical / index-derived
  discipline and the full schema rationale.
- `src/zicato/index/schema.py` — the authoritative DDL (cross-language contract).
- sibling skills: `zicato-step-loop`, `zicato-triage-stuck-loop`
  (drift/loss patterns feed both).
