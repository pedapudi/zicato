---
name: zicato-configure-tournament
description: "Configure a zicato tournament and multi-round evolution correctly (alias: configure-tournament). Use before launching any tournament or `--rounds N` evolution run. Explains the two nested levels that are dangerously easy to conflate — the WITHIN-round bracket structure (gauntlet/single_elim/double_elim/swiss/racing) vs the ACROSS-round evolution loop — and the non-obvious fact that the proposer learns from prior_experiments + insights every round EVEN WITHOUT PROMOTION. Provides a decision guide, a recommended starting config, a cost/runtime estimator, and an 'is it actually evolving?' checklist. Use when picking a bracket structure, sizing field/replicates/rounds, debugging 'nothing is evolving', or controlling cost/throttling. Do NOT use to audit whether the board itself is correct — use zicato-audit-board for that first."
---

# Configuring a zicato tournament + multi-round evolution

Most wasted effort on zicato comes from conflating **two nested levels** that
look the same in a command line but mean different things — and from one
non-obvious fact about how the proposer learns. This skill is the *interaction*
skill: how the within-round bracket and the across-round loop fit together, how
the proposer accumulates memory, what it costs, and how to prove it is actually
evolving.

It composes — it does not restate — the structure and builder skills. Defer:

- **Which structure, its params, the noise/incumbent design principles, the
  `scoring.json` `tournament` block** → [`zicato-design-tournament-structure`](../zicato-design-tournament-structure/SKILL.md).
  That is the authoritative source for `field_size` / `replicates` / `rounds_n`
  / `eta` / `board_fraction` and their defaults; this skill summarizes only
  enough to reason about the *two levels* and *cost*.
- **Driving the loop (`--rounds` / `--mode` / wall-clock / stop conditions) and
  the live-run gate** → [`zicato-evolve`](../zicato-evolve/SKILL.md).
- **The epoch → round → generation hierarchy and the two senses of "round"** →
  [`zicato-manage-epochs-and-rounds`](../zicato-manage-epochs-and-rounds/SKILL.md).
- **Assembling the whole contract through the GUI builder** → [`zicato-build-tournament`](../zicato-build-tournament/SKILL.md).
- **The mandatory pre-run hypothesis (per experiment)** → [`zicato-design-experiment`](../zicato-design-experiment/SKILL.md).
- **Reading the proposer's memory channels in telemetry** → [`zicato-read-telemetry`](../zicato-read-telemetry/SKILL.md).

> **Audit the board FIRST.** A misaligned scalar makes evolution converge on
> the wrong objective and *no* tournament config can fix that — the proposer
> faithfully learns whatever the scalar rewards. Run
> [`zicato-audit-board`](../zicato-audit-board/SKILL.md) before sizing a run.

Specs: [TOURNAMENT-STRUCTURES.md](../../docs/design/TOURNAMENT-STRUCTURES.md),
[EXPERIMENT-MEMORY.md](../../docs/design/EXPERIMENT-MEMORY.md),
[SELECTION.md](../../docs/design/SELECTION.md). See
[`../AGENTS.md`](../AGENTS.md) for operating rules (the live-run gate: never
launch a real-LLM `evolve` without the user's explicit go-ahead; derive every
flag from `--help`, not the stale design CLI doc).

## 1. The two nested levels (read this first)

```
ACROSS-round: the EVOLUTION loop  ──  `zicato evolve --rounds N`
  round 1:  proposer ──▶ FIELD of challengers ──▶ [ bracket ] ──▶ gate ──▶ maybe-promote
  round 2:  proposer ──▶ FIELD ...                 │                │
   ▲  (fed prior_experiments + insights)           │                └─ champion-gate: ONE duel
   └──────────────────────────────────────────────┘                   vs the reigning champion
                                          WITHIN-round: the bracket STRUCTURE
                                          (gauntlet | single_elim | double_elim | swiss | racing)
```

- **WITHIN-round — the bracket STRUCTURE.** Selects at most one winner from
  **one** field of challengers. It only *schedules* duels and *interprets*
  verdicts; it never promotes. The champion is carried, never knocked out by the
  bracket — the bracket crowns an internal leader/survivor, then ONE
  champion-gate duel decides promotion. (All structure detail —
  [`zicato-design-tournament-structure`](../zicato-design-tournament-structure/SKILL.md).)
- **ACROSS-round — the EVOLUTION loop (`--rounds N`).** Each round: the proposer
  is invoked **once** to mutate the current champion into a fresh field; the
  bracket + gate run; a promoted winner advances the champion (the base genome
  future mutations start from). With `--rounds 1` there is no "across" at all.

These are unrelated counters. A non-gauntlet structure also has its OWN inner
rounds (swiss `rounds_n`, an elim bracket's depth, a racing rung) — those are
WITHIN one evolve round, not across. Be explicit about which "round" you mean.

## 2. The proposer learns ACROSS rounds even WITHOUT promotion (the core fact)

This is the most common mis-model. **Learning and promotion are separate
channels.**

- **The learning channel — fed to the proposer every round:**
  - `prior_experiments` — the settled experiment-memory digest: for every prior
    experiment this epoch, its `core_idea` (the hypothesis), the `modulating`
    mutation-point ids it touched, its `decision` (promoted/rejected/deferred),
    and its **`scalar_score_delta`** (the Δscalar outcome; negative = better
    loss). Rendered into the proposer's `## What's already been tried` prompt
    section (see [EXPERIMENT-MEMORY.md](../../docs/design/EXPERIMENT-MEMORY.md)
    §3.2 — the `PriorExperiment` record).
  - `insights` — the per-round decision-telemetry analyzer's digest, rendered
    into the `## Recent telemetry insights` section.

  So round 2's field is informed by round 1's results **whether or not anything
  was promoted.** A rejected challenger still teaches the proposer "that
  mutation moved the scalar the wrong way."

- **The promotion channel — separate.** Promotion only changes the **base
  genome** the proposer mutates *from*. It is not how learning happens. A run
  can promote nothing for many rounds and still be evolving its proposals,
  because the memory digest keeps growing.

**Consequence:** "no promotions" alone does NOT mean "not evolving." "No
promotions AND identical fields every round" means the memory channel is dead.

## 3. Decision guide — which structure when (summary)

Full guide + params + defaults: [`zicato-design-tournament-structure`](../zicato-design-tournament-structure/SKILL.md).
The short map:

| Situation | Structure | Cost note |
|---|---|---|
| One challenger per round; cheapest 1-vs-1 | **gauntlet** (default) | one full-board duel; `replicates 1` |
| A field; you want a full RANKING | **swiss** | `rounds_n` pairings; `replicates ≥ 2` |
| A field; you only need the single best | **single_elim** | bracket depth; `replicates ≥ 2` |
| Same, want a "second chance" vs an upset | **double_elim** | **~3–4× single-elim** (losers'-bracket re-evals) — rarely earns it |
| LARGE field, pick the best cheaply | **racing** | slice-culls via `eta` / `board_fraction`; `replicates 1` (it replicates intrinsically via growing slices) |

Two cross-cutting levers worth internalizing:

- **`replicates` — not bracket shape — is the noise lever.** When a duel's
  verdict flips run-to-run, raise `replicates` (paired board-runs averaged, ~N×
  cost) before reaching for `double_elim`. Strictly cheaper and more honest.
- **`racing` slice-culls cheaply:** rung-0 duels run on a board SLICE
  (`board_fraction`), keep the top `1/eta`, grow the slice, repeat; only the
  final survivor sees the full board + the gate. It never spends a full board on
  an obvious loser.

## 4. Recommended starting config

**Start at gauntlet.** Adopt a field-structure only once the proposer actually
emits multiple challengers worth comparing.

```jsonc
// scoring.json — start here (gauntlet, one challenger, one duel)
{ "tournament": { "structure": "gauntlet", "params": { "replicates": 1 } } }
```

```sh
# A real multi-round run: persistent workspace, ≥2 rounds, deliberate mode.
.venv/bin/zicato evolve --workspace .zicato \
    --rounds 4 \
    --mode fast \
    --harness-call-llm   my_pkg.llms:harness \
    --auxiliary-call-llm my_pkg.llms:aux
```

When a real field exists and a single duel is too noisy, escalate to a field
structure via `--tournament-structure` + `--tournament-param KEY=VALUE` (writes
into `scoring.json` before the contract hash; auto-rolls the epoch — derive the
exact flags from `zicato evolve --help`). Example: race eight candidates:

```sh
.venv/bin/zicato evolve --workspace .zicato --rounds 4 \
    --tournament-structure racing \
    --tournament-param field_size=8 \
    --tournament-param eta=2 \
    --tournament-param board_fraction=0.25 \
    --harness-call-llm my_pkg.llms:harness --auxiliary-call-llm my_pkg.llms:aux
```

`--mode` (real flag, `fast` default): **`fast`** is cache-first — every
`(generation, entry, replicate)` board unit is evaluated at most once and reused
across pairings/rounds/structures; only cache misses run. **`full`** bypasses
the cache and re-evaluates every unit (noise re-sampling / debugging). Choose
`fast` for normal runs; `full` only when you specifically need fresh
re-evaluation. Picking wrong either hides regressions behind a stale cache or
burns budget re-running settled units.

## 5. Cost / runtime estimator

The expensive unit is a **board-run** (one full inner-harness execution of one
entry under its `wall_clock_budget_seconds`). A rough model:

```
board_runs ≈ field_size × replicates × duels_or_rungs × rounds
             + holdout_confirm_runs
wall_clock ≈ board_runs × per_entry_budget ÷ parallelism
```

- `duels_or_rungs` is the structure's schedule: gauntlet = 1; swiss = `rounds_n`
  pairings; an elim bracket = its depth; **double_elim ≈ 3–4× single_elim**;
  racing = its rung count (each rung on a fraction of the board, so racing's
  *effective* board cost is far below the naive product).
- `replicates` multiplies directly (~N× per duel).
- `rounds` is the OUTER evolve loop — total cost scales linearly in it.
- `parallelism` is `RuntimeConfig.parallelism` (the run fan-out semaphore);
  raising it shortens wall-clock but, on a quota-limited endpoint, **invites
  throttling** — the endpoint's own concurrency cap, not zicato's, is usually
  the real ceiling.
- `wall_clock_budget` × retries bites: a per-entry budget interacts badly with
  LLM retries — a retry can blow the budget and the run is recorded aborted
  (worst-case scored). Set `--max-wall-clock-seconds` for the whole invocation
  as a hard stop; the loop halts cleanly between rounds once it is spent.

Sanity-check the estimate before launching; runtime far above it means
throttling or budget×retry blowups — not "the model is slow."

## 6. "Is it actually evolving?" validation checklist

A run can complete cleanly and evolve **nothing**. Verify (read-only — inspect
existing run artifacts, never launch a live loop to test):

- [ ] **Multi-round.** `--rounds N` with `N ≥ 2`. `--rounds 1` cannot evolve —
      there is no "across" round and experiment memory stays empty.
- [ ] **Single persistent workspace.** All rounds share ONE `--workspace` so
      experiment memory carries over. A fresh workspace per experiment makes
      `prior_experiments` empty every round → the proposer never learns. This is
      the single most common "ran fine, evolved nothing" cause.
- [ ] **Memory is fed.** `prior_experiments` is non-empty from round 2 onward
      and per-round `insights` are generated. Verify the proposer's input
      actually contains the `## What's already been tried` / `## Recent telemetry
      insights` sections.
- [ ] **Fields differ round-over-round.** Round 2's field is not identical to
      round 1 — proof the proposer consumed the feedback.
- [ ] **Δscalar trend.** Track the champion / best-field `scalar_score_delta`
      across rounds; it should move in the intended direction (negative =
      improving loss).
- [ ] **Scalar reflects the objective.** Confirm via
      [`zicato-audit-board`](../zicato-audit-board/SKILL.md) that the scalar
      isn't drift-dominated, so the proposer learns the real goal, not a proxy.
- [ ] **Mode is intentional.** `--mode fast` (cache-first) vs `full` chosen
      deliberately for the run's purpose.

### Recipes to verify it (read-only)

```sh
EP=$(cat .zicato/current_epoch)
GENS=.zicato/epochs/$EP/generations

# prior_experiments carries over: every settled experiment this epoch has an
# outcome with a Δscalar — round 2+ proposing on an empty list means memory is dead.
jq -r '.generation_id as $g | .outcome
       | if . == null then "\($g)\t(unsettled)"
         else "\($g)\tdecision=\(.tournament_decision)\tΔscalar=\(.scalar_score_delta)"
         end' "$GENS"/*/experiment.json

# the hypotheses the proposer recorded — confirm round 2's idea reacts to round 1's outcome:
jq -r '.generation_id as $g | "\($g)\t\(.hypothesis.core_idea)\tmodulating=\(.hypothesis.modulating|join(","))"' \
   "$GENS"/*/experiment.json

# Δscalar trend across the lineage (negative = improving):
for e in "$GENS"/*/experiment.json; do
  jq -r 'select(.outcome!=null) | "\(.outcome.scalar_score_delta)\t\(.generation_id)"' "$e"
done | sort -k2

# the analyzer's per-round insight digest (what gets fed back as `insights`):
ls .zicato/epochs/$EP/insights/ 2>/dev/null && cat .zicato/epochs/$EP/insights/latest.md
```

The experiment-memory record fields keyed above are real: `experiment.json`
carries `hypothesis.{core_idea,modulating,...}` and `outcome.{tournament_decision,
scalar_score_delta,...}`; the digest fed to the proposer is the `PriorExperiment`
shape (`core_idea`, `modulating`, `decision`, `scalar_score_delta`) — see
[EXPERIMENT-MEMORY.md](../../docs/design/EXPERIMENT-MEMORY.md) and
[`zicato-design-experiment`](../zicato-design-experiment/SKILL.md).

## 7. Red flags

- **No promotions AND identical fields every round** → memory is not wired
  (fresh-workspace per experiment, or `prior_experiments` empty). The single
  strongest signal that evolution is dead.
- **`--rounds 1`** → no evolution by construction.
- **`prior_experiments` empty in round 2+** → memory not carrying over (check
  it's one persistent workspace).
- **Δscalar flat or worsening while `drift` improves** → the proposer is
  optimizing the wrong metric; the scalar is drift-dominated. Fix via
  [`zicato-audit-board`](../zicato-audit-board/SKILL.md) +
  [`zicato-tune-scoring`](../zicato-tune-scoring/SKILL.md), not the tournament config.
- **Runtime/cost far above the estimate** → concurrency throttling on a
  quota-limited endpoint, or `wall_clock_budget` × LLM-retry blowups.

Cross-check with the automated degeneracy detectors —
[`zicato-diagnose-health`](../zicato-diagnose-health/SKILL.md): a `stalled_loop`
or `degenerate_scoring` finding (exit code 9) confirms a structurally
non-evolving loop.

## When to use / when not to use

- **Use when:** setting up a tournament; choosing a structure; sizing
  `field_size`/`replicates`/`rounds`/`mode`; estimating cost or fighting
  throttling; debugging "it ran but nothing evolved."
- **Do NOT use when:** you suspect the *board* measures the wrong thing — run
  [`zicato-audit-board`](../zicato-audit-board/SKILL.md) FIRST. A misaligned
  scalar makes evolution converge on the wrong objective and no tournament
  config can fix it.
