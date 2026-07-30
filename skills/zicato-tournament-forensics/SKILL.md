---
name: zicato-tournament-forensics
description: Explain a single promote/reject decision — re-score one champion/challenger generation pair in isolation and read exactly why the gate fired (verdict transparency, the per-entry A/B grid, the scalar breakdown, the mutation heatmap, the score trajectory, and cost). Use this when you need to understand why a specific round promoted or rejected.
---

# zicato tournament-forensics — read one promote/reject decision

A **tournament** is a king-of-the-hill matchup: the reigning champion (the
**parent** generation) versus one **challenger** (the **child** proposed against
it), scored over the frozen board, decided by the two-sided promotion gate. This
skill is for forensics on a *single* matchup — "why did this round promote /
reject?". For the epoch-wide retrospective see `skills/zicato-analyze-epoch`; for
cross-epoch lineage see `skills/zicato-lineage`; for the live bracket UI see
`skills/zicato-watch-dashboard`.

Always call the CLI from the project venv: `.venv/bin/zicato ...`. See
[AGENTS.md](../../AGENTS.md).

## Re-score a pair in isolation

```sh
.venv/bin/zicato tournament PARENT CHILD \
    --workspace .zicato [--epoch <id>] [--mode full|fast] [--skip-regression]
```

- `PARENT` and `CHILD` are **generation ids** under the resolved epoch
  (`v2`, `v3`, ...) — parent is the champion, child is the challenger. With
  `--epoch` omitted, the epoch resolves from the workspace's `current_epoch`
  marker.
- `--mode full` (default here; note `zicato evolve` defaults to `fast`): runs
  the **whole board** under both generations and applies the gate against the
  live A/B comparison.
- `--mode fast`: runs only the **child** and applies the gate against the
  parent's historical aggregate cached in the workspace
  (see [SCORING.md §7](../../docs/design/SCORING.md#7-fast-mode-and-the-tournament)).
  Fast mode honours the contract's `replicates` too, so it is *one side's*
  board run, not necessarily one board pass — and the noise reduction is
  one-sided: the challenger is replicated while the champion stays a single
  frozen cached draw. Use `full` when you want independent draws on both sides.
- `--skip-regression`: skip the regression-suite gate even when scoring enables
  it (a per-invocation override).
- Output is the `GateOutcome` printed as **JSON** (`decision`, `reason`,
  `delta_scalar`, `delta_pass_rate`). The `decision` field is the
  authoritative verdict — `"promoted"` / `"rejected"` / `"deferred"`
  (`TournamentDecision`); branch scripts on that JSON, not on the exit code.

> **WARNING — this is a live, budget-spending run** in `full` mode (it executes
> the inner harness over every board entry, twice). Per the project rules, do
> NOT run a live tournament without the user's explicit go-ahead. For *reading*
> an already-settled decision, inspect the artifacts (below) instead of
> re-running. There is **no `--no-record-outcome` flag** — a live re-run records
> its outcome into the workspace. The four flags above are the whole surface.

## Reading the verdict (the JSON `decision`, not an exit code)

The standalone `zicato tournament` command prints the `GateOutcome` as JSON and
exits `0` on a successful run regardless of the verdict — it does **not** encode
promote/reject in the exit code. Branch on the JSON `decision` field
(`"promoted"` / `"rejected"` / `"deferred"`), which is authoritative. No exit-6
path exists — if you have inherited a script expecting "`0` = promote, `6` =
reject", it is wrong. A non-zero exit is a usage/config/runtime error, not a
reject verdict.

## Read the verdict without re-running

A settled round's full forensic record is already on disk:

```
.zicato/epochs/{id}/generations/{child}/experiment.json     # hypothesis + outcome
.zicato/epochs/{id}/generations/{child}/patches/*.json      # what it changed
.zicato/epochs/{id}/generations/{*}/runs/.../loss.json      # per-entry loss profiles
.zicato/epochs/{id}/generations/{*}/harness_load.json       # what the run actually loaded
.zicato/epochs/{id}/rounds/{round}/round_log.jsonl          # the round's typed event log
```

The `outcome` block in `experiment.json` carries `tournament_decision`,
`rejection_reason`, `drift_loss_delta`, `pass_rate_delta`, and the
`hypothesis_match` array
([EPOCHS-AND-JOURNALING.md §3.3](../../docs/design/EPOCHS-AND-JOURNALING.md#33-outcome-written-after-the-run)).

`round_log.jsonl` is the round's durable, sequenced trace — contract hash,
proposal attempts and the sampled slate, apply/validate, harness-load
provenance, the tournament units, then `gate_evaluated` and the recorded
decision. Its `gate_evaluated` event carries `champion_scalar`,
`challenger_scalar`, and `margin_required` on **both** verdicts, so a duel's
effect size is reconstructable from the log alone — including for promotions,
whose `rule_fired` is empty by design (a clean promote fires no rule). Read
those three fields, never a regex over `rule_fired`: its phrasing varies per
rule (`insufficient improvement: …`, `challenger regressed: …`, `pass-rate
regression on entries: …`, `diff_complexity_ceiling: …`) and it is presentation,
not contract.

**Before trusting any verdict, check the mutations were under test.** Each
generation's `harness_load.json` records the snapshot-relative entrypoint plus
`trees_verified` / `trees_never_imported` — the mutable trees its units did and
did not import. A `tree_never_imported` health warning means *mutations to that
tree cannot have been under test*: the run completed, the board scored, the gate
fired, and the comparison was between two identical unmutated trees. That is a
verdict-invalidating finding, not a quality signal.

**Replicated duels fold before scoring.** With `replicates > 1` (the gauntlet's
default is 2) each replicate caches its own `runs/<entry>/loss.json` (replicate
0) / `loss.r<N>.json`, and the per-entry profiles are folded into ONE profile
the scalar sees: `drift_loss` / `metrics` / `per_judge_loss` / the namespaced
counters are meaned, `score` is the mean of each replicate's *resolved* outcome —
so a replicate that aborted without a score votes its `0.0` rather than
abstaining — and `pass_fail` is a strict-majority vote. The vote can legitimately
disagree in sign with the folded `score` (2 of 5 passing is `pass_fail: false`
and `score: 0.4`); that is the binary and continuous views of one duel, not an
inconsistency. Fields the scalar never reads (`run_id`, `runtime_ms`,
`abort_cause`, `expectation_result`) pass through from replicate 0 only, so do
not read them as properties of the fold. Fewer `loss.r<N>.json` files than
`replicates` is not corruption: a per-round token budget reached mid-slate stops
scheduling further replicate slots and settles with the completed ones, rather
than caching synthetic worst-case losses for units nobody attempted.

## How to read *why* the gate fired

The gate applies its rules **in order**, and the first one to fire owns the
rejection ([SCORING.md §5](../../docs/design/SCORING.md#5-the-tournament-promotion-gate)).
Match the `rejection_reason` you are holding to the rule that produced it:

0. **Diff-complexity ceiling** (opt-in, `diff_complexity_ceiling > 0`). Checked
   *before* the scoring rules, so an over-budget edit is rejected naming the
   ceiling — `diff_complexity_ceiling: diff complexity 14 exceeds ceiling 10` —
   rather than whatever scoring near-miss it may also have tripped.
1. **Margin (drift side).** The scalar is a *loss* — lower is better. Promotion
   needs `child_scalar <= parent_scalar - promote_margin`. A child that improved
   but by less than the margin is a **near-miss reject**
   (`insufficient improvement: …`); a child whose loss *rose* is a **regressed
   reject** (`challenger regressed: …`). `delta_scalar = child - parent`
   (negative = improvement).
2. **Pass-rate monotonicity (pass-rate side).** Under the default
   `per_entry` scope, no board entry the parent passed may regress (child
   fails, errors, or no longer evaluates it). Any such entry →
   `rejection_reason = pass-rate regression on entries: <id>, <id>` and the
   candidate is rejected regardless of drift improvement. The
   `pass_rate_monotonicity_scope` in `scoring.json` selects the granularity:
   `per_entry` (strict — the rule above) or `aggregate` (only the board-wide
   pass-rate may not drop, reading `pass-rate regression: overall pass-rate
   fell by …`). The on/off switch is the separate `pass_rate_monotonicity` bool
   (there is no `off` scope). The scope is part of the scoring weights and is
   carried through the subprocess-worker transport like every other weight —
   read the verdict's own breakdown to see which scope decided it.
3. **Per-namespace monotonicity**, default-on for `rubric:` and `schema:`. A
   child that moved a guarded namespace in its worse direction is rejected even
   when the combined scalar improved: `monotonicity_regression on
   namespace=rubric:`. This is the reason operators are most often surprised by,
   because they never configured it — it ships on.

A fifth veto sits after the rules: with the default-on train/holdout split, a
win the train slice measured must also not regress on the holdout, or it flips
to `holdout_not_confirmed: …`. All the deltas reported alongside it are still
the train-side deltas.

The matchup detail in the Tournament view
([TOURNAMENT.md §3](../../docs/design/TOURNAMENT.md#3-per-matchup-detail)) lays
this out in five sections; read them in order to localize the verdict:

- **Hypothesis** — the proposer's stated intent before the run.
- **Patches** — the `mutation_id`, `op`, and rationale of each change.
- **Per-entry A/B grid** — the board entry by entry, champion side vs challenger
  side, with the per-entry drift Δ and which passes **flipped**. This is the
  heart of a verdict: it shows *which* entries the challenger won, lost, or
  tied, and exactly which pass regressed (the monotonicity trip).
- **Scalar breakdown** — how the per-entry numbers aggregate into the two
  `gen_score`s the gate consumes (`weighted_drift`, `pass_rate`, the weighted
  terms). The per-entry numbers now include the **continuous** outcome
  `score` (a float in `[0,1]`; a binary entry contributes `1.0`/`0.0`) and its
  optional `metrics` (precision/recall) carried up from each run's `loss.json`.
  **Provenance caveat:** the weights behind a `per_judge_loss` attribution
  (`per_judge_weights` / `default_judge_weight` / `pass_rate_monotonicity_scope`)
  are serialised across the subprocess-worker boundary
  (`tournament/runner.py:_weights_spec` ↔ `_tournament_worker.py
  :_weights_from_args`); a weight that doesn't match `scoring.json` in a verdict
  means the transport dropped a field (it once silently scored all custom-judge
  drift at `1.0`) — a scoring-provenance smell, not a candidate-quality one. The
  neighbouring failure mode is closed: a judge/model spec that validates but
  cannot be resolved now exits the worker non-zero (a visible infra abort)
  instead of letting the judge path swallow it and score no drift, which made
  the scalar *better* than the truth.
- **Gate verdict** — the decision with each rule shown: the margin computation
  (`parent.score - candidate.score` vs `required margin`) and the monotonicity
  checks, plus the exact `rejection_reason`. An operator override is never silent
  — it shows the would-have-been verdict alongside the override.

## The cross-round analytics (the wider forensics)

Beyond one matchup, the Tournament view offers epoch-wide analytics
([TOURNAMENT.md §4](../../docs/design/TOURNAMENT.md#4-tournament-detail-analytics)),
all served from the analytical index:

- **Verdict transparency** (§4.1) — the §3.5 gate panel for every round; no
  promotion or discard is ever a black box.
- **Per-entry A/B grid, aggregated** (§4.2) — which entries consistently
  differentiate generations and which never do (the non-differentiating-entry
  signal that loop-health escalates).
- **Optimization trajectory** (§4.4) — champion `gen_score` over rounds
  (monotonically non-increasing) plus every challenger's score, including
  discarded ones; a flat champion line is the *stalled loop* signal.
- **Mutation heat map** (§4.5) — which mutation points correlate with winning
  (touched vs promoted). A **correlation, not causation** — a surface touched
  five times and promoted once is *resisting* improvement; consider the proposer
  brief's `## Forbidden` list.
- **Tournament cost** (§4.6) — wall-clock, aux-LLM calls, and board runs per
  round and per epoch; fast mode shows up as the challenger's board runs alone,
  with no champion pass beside them.

## Guardrails

- venv-only (`.venv/bin/zicato`); never bare `uv sync` (use `--all-extras`).
- Do NOT run a live `tournament` (especially `--mode full`) without explicit
  user go-ahead — it spends LLM budget and records an outcome. Prefer reading
  the settled artifacts.
- Read the verdict from the JSON `decision` (`promoted` / `rejected` /
  `deferred`), not the exit code — the standalone command exits `0` on any
  successful run.

## See also

- [TOURNAMENT.md](../../docs/design/TOURNAMENT.md) — gauntlet structure, matchup detail, the six analytics.
- [SCORING.md](../../docs/design/SCORING.md) — the scalar and the two-sided promotion gate.
- `skills/zicato-analyze-epoch` — the epoch-wide hypothesis-vs-outcome retrospective.
- `skills/zicato-watch-dashboard` — the live Tournament view (bracket + matchup detail).
