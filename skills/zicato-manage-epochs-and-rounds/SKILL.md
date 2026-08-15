---
name: zicato-manage-epochs-and-rounds
description: The mental model + operations for zicato's epoch/round/generation hierarchy — what an epoch is (a sealed evaluation CONTRACT), how contract-hash auto-epoching rolls epochs, the TWO senses of "round" (outer evolve round vs a tournament's inner rounds), champion/challenger vs parent/child, champion_eval_mode (full/fast/fast-degraded), the mandatory pre-run hypothesis, and the advanced `zicato epoch` escape hatches (list/new/close/switch/set-goal/gc). Use this when an operator is confused about epoch boundaries, "rounds", lineage attribution, or wants to force an epoch by hand.
---

# zicato — epochs and rounds

This is the **mental-model** skill. `zicato evolve` manages epochs and rounds
for you (`skills/zicato-evolve` drives the loop); this skill explains the
hierarchy so you can answer "did that roll a new epoch?", "which round minted
this generation?", and "why is the champion not being re-run?" — and reach for
the `zicato epoch` escape hatches only when you must.

Always call the CLI from the project venv: `.venv/bin/zicato …`. Install with
`uv sync --all-extras`, never bare `uv sync`. Read-only inspection and `--help`
only — do **not** launch a live `evolve` (it spends LLM budget; the live-run
gate in [AGENTS.md](../../AGENTS.md) and `skills/zicato-evolve` applies).
See [EPOCHS-AND-JOURNALING.md](../../docs/design/EPOCHS-AND-JOURNALING.md).

## 1. The hierarchy (define each crisply)

```
epoch                      a sealed evaluation CONTRACT + a goal
  └─ evolve round          ONE meta-loop step: one tournament, one crowning
       └─ generation       a candidate child snapshot (vN) the proposer minted
            └─ run          one board-entry execution under one generation
```

- **Epoch** — the unit of evaluation **contract**: a frozen board + proposer
  brief + scoring (weights + gate + tournament structure) + the registered
  target-adapter *identity* (entrypoint + mutable trees), plus an operator
  **goal**. Generations *within* an epoch are directly comparable; *across*
  epochs they are not. Generations are linearly ordered `v0 → v1 → … → vN`;
  `v0` is the baseline (a fresh epoch's `v0` is the promoted head of the
  epoch it rolled from — see §4.4).
- **Evolve round** — one `zicato evolve --rounds N` step. Each round = the
  orchestrator proposes a challenger field, runs ONE tournament over it, and
  crowns. Round numbers are global within the epoch and independent of
  promotion: the 17th round is round 17 even if only 12 promoted.
- **Generation** — a candidate child snapshot (`vN/`) the proposer produced in
  a round, carrying its `experiment.json` (hypothesis + patches, then outcome).
- **Run** — a single board-entry execution under one generation, persisted at
  `generations/{id}/runs/{entry_id}/` (`events.jsonl` + `loss.json`).

## 2. The TWO senses of "round" (load-bearing — commonly confused)

When someone says "round", disambiguate FIRST. They mean one of two things:

| | OUTER evolve round | INNER tournament round |
|---|---|---|
| What it is | One meta-loop step (`--rounds N`) | One scheduling step *inside* a single tournament |
| Count | One tournament per outer round | Many, only for non-gauntlet structures |
| Examples | round 0, round 1, … | swiss rounds, single/double-elim **bracket** rounds, racing **rungs** |
| Code | the round loop in `zicato/evolve/loop.py::evolve_n_rounds` | `Matchup.stage_index` / `RoundRecord` in `zicato/selection/strategy.py` (the field was once *also* called `round_index`, which is exactly why this table exists; readers still accept the legacy key) |
| Stamped where | `Generation.round_index` / `Experiment.round_index` (§3) | the selection strategy's bracket state + the settled `tournaments` index row |

- A **gauntlet** (the default) tournament has exactly one inner round: champion
  vs one challenger. So in the gauntlet, outer-round and the (single) inner
  round coincide — which is why the two senses are easy to conflate.
- A **swiss / single_elim / double_elim / racing** tournament plays *several*
  inner rounds within ONE outer evolve round, over a multi-challenger field.
  "swiss round 3" or "racing rung 2" is an **inner** round; it does not advance
  the outer `--rounds` counter. See `skills/zicato-design-tournament-structure`.

Rule of thumb: `--rounds`, "round 0/1/2…", and `round_index` on a generation
are **outer**. "bracket round", "swiss round", "rung", anything inside one
crowning — **inner**.

## 3. `round_index` — generation→birth-round attribution

Every generation carries a 0-based **`round_index`**: the OUTER evolve round
that minted it. It is persisted into `experiment.json` (`Experiment.round_index`,
mirrored from `Generation.round_index`) so the dashboard's round-timeline /
champion-spine views can attribute each generation to its birth round.

- Defaults to `0` for the seed `v0` and for pre-feature records that predate
  the stamp.
- It is the OUTER round, never an inner bracket round.
- Read it straight from `experiment.json`:
  `grep round_index .zicato/epochs/<id>/generations/v3/experiment.json`.

## 4. Contract-hash auto-epoching (the core mechanism)

Operators should rarely think about epoch management. They edit the board /
brief / scoring, run `zicato evolve`, and the right thing happens.

### 4.1 What is in the contract (exactly five things)

1. **board** — entries + `expectations` + `judges` + the board-level
   `disable_drift` set (`board.jsonl`).
2. **proposer brief** — operator steering text (`brief.md`).
3. **scoring** — weights + gate thresholds **and the per-epoch tournament
   structure** block (`scoring.json`).
4. **target-adapter IDENTITY** — the registered entrypoint string + the sorted
   list of mutable-tree paths (NOT the source bytes inside those trees — that
   source is exactly what zicato mutates within the epoch).
5. **proposer** — the proposing agent's identity, tools, and the skill modules
   under the configured `proposers/<name>/` dir (or the built-in default
   proposer when none is registered via `register --proposer-path`). Distinct
   from the *proposer brief* (item 2): the brief is per-epoch steering text;
   the proposer is the agent (plus its skills) that consumes it. See
   `skills/zicato-design-proposer` and
   [PROPOSER.md](../../docs/design/PROPOSER.md).

`evolve` reduces these to one `sha256` **contract hash**
(`zicato/epoch/contract.py`), stored on the `EpochConfig.contract_hash` at
creation. The hash is over a **canonicalized** form, so spurious edits do not
roll: a reordered board, CRLF churn in the brief, float-precision noise in
`scoring.json`, and registration-order differences in the trees are all no-ops.

### 4.2 When does it roll vs continue?

Checked **once per `zicato evolve` invocation**, before the round loop starts;
the resolved epoch is pinned for every round (never re-rolls mid-flight).

| Current epoch | auto-epoch ON (default) | `--no-auto-epoch` |
|---|---|---|
| none yet | create the first epoch (`e0`) from the contract, run | error: run `zicato epoch new` |
| hash **matches** (or is empty — legacy) | **continue**, no roll | continue |
| hash **differs** (contract drifted) | **ROLL**: close current (writes `analysis.md`), open a fresh epoch carrying the new contract, run | error naming the changed component |

A legacy epoch with an empty `contract_hash` (created before auto-epoching) is
treated as **always matching** — the orchestrator never rolls it spuriously.
`--epoch <id>` skips the check entirely: an explicit target always wins.

### 4.3 What forces a roll (any contract change)

Any change to the five components above. Concretely: a changed/added/removed
board entry, an added or retuned judge, toggling `disable_drift`, editing the
brief's `## Forbidden` list (or any brief text that survives canonicalization),
retuning a weight / `per_judge_weight` / `promote_margin`, a different
entrypoint, an added/removed mutable tree, **registering a proposer dir or
semantically editing its `agent.py` / a `skills/*.md` module** (the roll
message names the changed component as `proposer`) — **and changing the scoring
`tournament` block**: switching `gauntlet → swiss`, or bumping a param like
`swiss.rounds`. The tournament structure lives inside `scoring.json` and rides
the existing scoring canonicalizer, so a structure change rolls the epoch
exactly as a `promote_margin` retune does (the roll message names the changed
component as `scoring` — the structure *is* scoring). The `evolve
--tournament-structure` / `--tournament-param` flags are a convenience that
writes the block into `scoring.json` *before* the hash is computed, so they
auto-roll just like a hand-edit.

What does NOT roll: named model-engine and role assignments (runtime
configuration, not contract), advisory brief text that canonicalizes to the
same bytes, and target source content. Change an engine's operator-declared
`revision` when its logical deployment changes so provenance can distinguish
it from a mere transport move. `champion_eval_mode` (§5) is runtime provenance,
never a contract input.

### 4.4 Roll mechanics

On a roll, `evolve`:
1. closes the current epoch (runs the analysis pass → `analysis.md`, restamps
   the persisted report's masthead to the final closed state),
2. opens a fresh epoch auto-named `e{N}` (N = count of existing epochs;
   override with `--epoch-name`),
3. baselines the new epoch's `v0` from the **promoted head of the closed
   epoch** (recorded as the new epoch's `v0_parent` in `lineage.json`); if the
   closed epoch promoted nothing past its own `v0`, the new epoch seeds from
   the registered mutable trees,
4. prints `contract changed (<component>) — rolled <old> -> <new>` and warns
   that the auto-rolled epoch has **no goal recorded** — nudging you to run
   `zicato epoch set-goal` (§7).

## 5. `champion_eval_mode` (full / fast / fast-degraded)

`--mode` controls the unit cache; `champion_eval_mode` is the per-round
**runtime provenance** of how the CHAMPION side was evaluated. It is recorded
on each challenger's `OutcomeRecord` and carries **no weight in the gate and is
not in the contract hash** — it exists so the journal can attribute champion
sample freshness + cost.

- **`full`** — the champion was run **live** this round (you passed `--mode
  full`, which bypasses the cache and forces a fresh evaluation of every board
  unit; or fast was not applicable).
- **`fast`** — the champion's cached per-board scalars were **reused** and the
  champion was **NOT executed** this round. `--mode fast` (the default) is
  cache-first: every `(generation, entry, replicate)` board unit is evaluated
  at most once and reused across pairings/rounds/structures; only cache misses
  run. The contract's `replicates` still applies to the CHALLENGER side (2 by
  default on the gauntlet), so the round contrasts a replicated challenger
  against one frozen champion aggregate — the noise reduction is one-sided, and
  repeated rounds are not independent draws of the contrast.
- **`fast-degraded`** — fast was requested but **no cache** covered the needed
  boards (the seed/first champion, or a not-yet-covered subset), so the
  champion ran live **once** to seed the cache.

What this means for you:
- **Cost** — `fast` is cheaper: the expensive champion run is amortized via the
  cache. `full` re-samples every unit (noise re-sampling / debugging) and is
  the most expensive. `fast-degraded` is a one-time seed cost.
- **Reading results** — when `champion_eval_mode` is `fast`, the champion's
  per-run numbers are *reused* (not freshly sampled this round); don't read a
  flat champion trajectory as "the champion didn't move" — it wasn't re-run.
  For a clean apples-to-apples re-sample (e.g. to rule out run-to-run noise),
  re-run with `--mode full`.

## 6. Journaling + the MANDATORY pre-run hypothesis

Every experiment carries a structured **hypothesis written BEFORE the run**,
completed with an **outcome** appended to the same `experiment.json` AFTER the
run. The hypothesis is mandatory — a schema-invalid proposer response is
rejected and re-prompted.

- The hypothesis (`HypothesisSpec`): `core_idea`, `modulating` (mutation-point
  ids), `why` (the pattern observation), `expected_drift_movements` /
  `expected_metric_movements`, `expected_pass_rate_delta`, `risks`.
- The outcome (`OutcomeRecord`): realized `drift_movements` (each with a
  per-kind `hypothesis_match` — the load-bearing signal: did the proposer
  *reason* or *guess*?), `scalar_score_delta`, `tournament_decision`, and the
  additive structure fields (`structure`, `final_rank`, `match_record`,
  `champion_eval_mode`).
- The **epoch** carries a **goal** (the operator's intent) + the proposer
  **brief**. The running per-round `journal.md` and the at-close `analysis.md`
  read this ledger. To compose a good hypothesis up front, see
  `skills/zicato-design-experiment` and `skills/zicato-write-brief`; to read the
  ledger after, see `skills/zicato-analyze-epoch`.

### The per-round event log (the round's store-of-record)

Alongside the journal, every settled round writes an append-only typed event
log at `epochs/{epoch}/rounds/{round}/round_log.jsonl`, sequenced (`seq` starts
at 1, +1 per append) and durable under `epochs/` rather than `runtime/`. It
covers the round's whole arc — open (contract hash) → proposal session → apply /
validate → harness-load provenance → tournament units → gate / holdout /
evidence → decision → close — and folds to a typed `RoundRecord`. Read it when
"why did this round decide that?" needs more than the outcome record. Two
fields worth knowing:

- **`gate_evaluated`** carries `champion_scalar` / `challenger_scalar` /
  `margin_required` on **both** decisions, so a duel's effect size is
  reconstructable from the log alone. They default to `None` (not `0.0`) on logs
  that predate them. `rule_fired` is presentation — it names which rule decided
  and is *empty on a clean promote*; never regex numbers out of it.
- **`harness_loaded`** records which module inside a generation's snapshot was
  actually imported (`harness_entrypoint_files`) and which declared mutable trees
  **no** unit imported (`harness_never_imported_trees`). A non-empty entry there
  means that generation's mutations to those trees could not have been under
  test — loop health turns it into a warning. Both are additive and normally
  empty (also empty for a fully cache-served round).

## 7. Operations — the advanced `zicato epoch` subcommands

`zicato evolve` opens / closes / rolls epochs on its own. The `zicato epoch`
group is **off the happy path** — for inspection, to force an epoch boundary by
hand, or to reclaim disk. Confirmed via `--help`, which is always canonical
(`docs/design/CLI.md` is generated from it and says so):

```sh
.venv/bin/zicato epoch --help        # group overview
.venv/bin/zicato epoch list      --workspace .zicato
.venv/bin/zicato epoch new NAME  --workspace .zicato --board board.jsonl \
                                 --brief brief.md [--scoring scoring.json] [--goal "..."]
.venv/bin/zicato epoch close [EPOCH_ID] --workspace .zicato
.venv/bin/zicato epoch switch EPOCH_ID  --workspace .zicato
.venv/bin/zicato epoch set-goal --epoch EPOCH_ID --goal "..." --workspace .zicato
.venv/bin/zicato epoch gc [EPOCH_ID] --workspace .zicato \
                                 (--keep-last N | --keep-promoted-only) [--apply]
```

- **`list`** — every epoch as a markdown table (rendered from `lineage.json`:
  started/closed, promoted/rejected counts, parent). The first thing to run
  when reasoning about lineage. Read-only.
- **`new NAME`** — create an epoch and make it current. The supplied contract
  files are both frozen into `epochs/{id}/` AND published as the workspace's
  *live* contract (so a later `evolve` resolves the same contract and continues
  this epoch rather than spuriously rolling). Auto-closes a still-open previous
  epoch first (stub `analysis.md` — no auxiliary LLM is wired through the CLI
  yet). When stdin is a TTY and `--goal` is omitted you are prompted for one.
- **`close [EPOCH_ID]`** — mark closed and (best-effort) write `analysis.md`
  (current epoch when omitted). The analysis pass runs only when an auxiliary
  LLM is configured; otherwise a stub is written for later regeneration. A
  closed epoch is frozen — read-only. See `skills/zicato-analyze-epoch`.
- **`switch EPOCH_ID`** — re-point the `current_epoch` marker (target must
  exist).
- **`gc [EPOCH_ID]`** — reclaim the disk held by settled-**rejected**
  generations' SOURCE TREES. Records survive: `lineage.json`, the journal,
  experiment/score records, and run telemetry are never touched, so a pruned
  generation stays fully analysable and only loses its browsable tree. The
  promoted chain, in-flight generations, and the seed `v0` are never pruned.
  Pick exactly one of `--keep-last N` / `--keep-promoted-only`; **dry-run by
  default** — pass `--apply` to actually prune.
- **`set-goal --epoch … --goal …`** — fill in (or overwrite) an epoch's goal.
  The intended use is the auto-roll case: a mid-`evolve` roll has no chance to
  prompt, so the new epoch lands with an empty goal + a warning recommending
  this command. Idempotent; refreshes the index `epochs.goal` column.

### When would an operator force an epoch by hand?

- **`epoch new`** — to start an epoch with a hand-chosen name, or to roll for a
  reason the hash **cannot see** (e.g. a regression-baseline rebase that did
  not touch any contract file).
- **`--no-auto-epoch`** on `evolve` — to be *told* about contract drift and
  decide deliberately, instead of rolling automatically.
- **`epoch switch`** — to re-point at an earlier epoch for inspection (but a
  closed epoch is read-only; do not try to re-run it — start a fresh epoch
  instead, whose `v0` baselines off the closed epoch's head).
- **`epoch list`** — anytime, to read the lineage table.

## 8. champion/challenger vs parent/child (when to use which)

They name the **same pair** from two framings — keep them straight:

- **champion / challenger** — the **tournament role**. The champion is the
  current head being defended; the challenger is the candidate trying to
  unseat it. Use this framing when talking about a *match / crowning* (and in
  multi-challenger structures where several challengers contest one champion).
- **parent / child** — the **lineage** relation. The child generation was
  forked from (patched onto) the parent snapshot. Use this framing when talking
  about *snapshots / DAG / where a generation came from* (`parent_generation_id`,
  `v0_parent`, the `lineage.json` `parent_id`).

In a gauntlet the champion IS the parent and the challenger IS the child, so
the two collapse. In a multi-challenger field, "champion vs challenger" is the
role-correct framing for each match; "parent/child" still describes each
challenger's snapshot provenance.

## Guardrails

- venv-only (`.venv/bin/zicato`); install with `uv sync --all-extras`.
- Do **not** launch a live `evolve` to demonstrate a roll — that spends budget
  and trips the live-run gate. Verify wiring with the test suite / the
  deterministic mock target (`skills/zicato-bootstrap`).
- Derive the CLI from `--help` — it is canonical; `docs/design/CLI.md` is a
  generated mirror, so trust `--help` whenever the two disagree.
- A closed epoch is frozen / read-only; never re-open or re-run one.
- An auto-rolled epoch has an empty goal until you run `epoch set-goal`.

## See also

- [EPOCHS-AND-JOURNALING.md](../../docs/design/EPOCHS-AND-JOURNALING.md) — the full epoch lifecycle, journaling, auto-epoching, tournament-structure contract interaction.
- `skills/zicato-evolve` — drive the loop (auto-manages epochs/rounds).
- `skills/zicato-design-tournament-structure` — choose the per-epoch structure (gauntlet / elim / swiss / racing) and its inner rounds.
- `skills/zicato-write-brief` — author the proposer brief + goal that anchor the epoch.
- `skills/zicato-analyze-epoch` — close an epoch and read its hypothesis-vs-outcome ledger.
