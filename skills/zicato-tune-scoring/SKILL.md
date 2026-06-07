---
name: zicato-tune-scoring
description: Edit a zicato scoring.json — drift-loss weights, per_judge_weights/default_judge_weight, severity and per-kind weights, and the promotion gate (promote_margin + pass_rate_monotonicity). Use when calibrating how generations are scored or when tournament decisions disagree with operator intuition. Lower scalar = better.
---

# Tuning `scoring.json`

`scoring.json` turns each generation's `LossProfile` into a single scalar and
defines the tournament promotion gate. **Lower scalar = better.** It is one of
the three live contract files (`board.jsonl`, `brief.md`, `scoring.json`) next
to the workspace; `evolve` reads it there and freezes a per-epoch copy under
`.zicato/epochs/{epoch_id}/scoring.json`. Sibling skills:
`zicato-author-board`, `zicato-write-brief`. See
[SCORING.md](../../docs/design/SCORING.md).

> The on-disk keys (canonical, from `ScoringWeights`) are PLURAL and differ
> from some prose in SCORING.md. Use: `per_kind_weights`, `per_judge_weights`,
> `severity_weights`, `plan_revision_weight`, `runtime_weight`,
> `promote_margin`, `pass_rate_monotonicity` — NOT `per_kind_weight`,
> `w_drift`, `tournament_margin`. The dataclass field names are the truth.

## Two halves of the scalar

1. **Drift-derived loss** — a weighted sum over drift counts (by kind and
   severity), custom-judge violations, plan revisions, task-failure ratio,
   runtime-over-budget, and abort. Always available; needs no expectations.
2. **Pass-rate** — the weighted fraction of board entries whose `expectation`
   passed. Only entries that carry an `expectation` contribute.

`drift_weight` and `pass_weight` are the coefficients combining the two
(roughly `scalar = drift_weight * weighted_drift + pass_weight * (1 - pass_rate)`).

## A real `scoring.json`

The shipped example (`examples/zicato_examples/target_1_presentation/scoring.json`):

```json
{
  "drift_weight": 1.0,
  "pass_weight": 1.0,
  "severity_weights": {
    "info": 1.0,
    "warning": 3.0,
    "critical": 10.0
  },
  "per_kind_weights": {
    "confabulation_risk": 2.0,
    "looping_reasoning": 1.5
  },
  "plan_revision_weight": 0.5,
  "runtime_weight": 0.0,
  "promote_margin": 0.01,
  "pass_rate_monotonicity": true,
  "pass_rate_monotonicity_scope": "per_entry"
}
```

## Every key (defaults from `ScoringWeights`)

| Key | Default | Meaning |
|---|---|---|
| `drift_weight` | `1.0` | Coefficient on the aggregated drift-loss term. |
| `pass_weight` | `1.0` | Coefficient on the `(1 - pass_rate)` term. |
| `severity_weights` | `{"info":1.0,"warning":3.0,"critical":10.0}` | Per-severity multipliers (lowercase keys). Missing key → `0.0` (non-scoring). |
| `per_kind_weights` | `{}` (uniform) | Per-`DriftKind` multipliers. Keys are short lowercase tokens — `confabulation_risk`, `looping_reasoning`, `intent_divergence`, … Stacks multiplicatively with `severity_weights`. |
| `per_judge_weights` | `{}` | Per-custom-judge multipliers keyed on the judge `name`. All custom judges share the `custom` drift kind, so this is the only way to weight them apart. |
| `default_judge_weight` | `1.0` | Fallback multiplier for a custom judge whose `name` is absent from `per_judge_weights`. |
| `plan_revision_weight` | `0.5` | Coefficient on `plan_revisions`. |
| `runtime_weight` | `0.0` | Coefficient on per-second runtime. Usually `0.0` — the wall-clock budget is the hard ceiling; set >0 only when runtime matters intrinsically. |
| `promote_margin` | `0.01` | Minimum scalar improvement the child must show over the parent to be promoted (regression-noise floor). |
| `pass_rate_monotonicity` | `true` | On/off switch for the pass-rate gate. When false, the rule is disabled. |
| `pass_rate_monotonicity_scope` | `"per_entry"` | Granularity when the rule is on: `"per_entry"` rejects if ANY champion-passed entry flips to fail (invariant/regression boards); `"aggregate"` rejects only if the OVERALL pass-rate drops (sampled evaluation boards). |

(The dataclass also carries an optional `regression_gate_enabled` /
`regression_test_command` test-suite gate; leave it off unless the snapshot
ships its own suite.)

## `per_judge_weights` and `default_judge_weight`

A custom judge (a board entry's `judges`, authored with `name`/`mode`/`body`/
`severity` — see `zicato-author-board`) emits the single `custom` drift kind on
violation. `per_kind_weights["custom"]` therefore weights *every* custom judge
identically. When one judge's violation should count for more, key
`per_judge_weights` on the judge `name`:

```json
"per_judge_weights": {
  "no_fabricated_numbers": 3.0,
  "incorporates_feedback": 1.0
},
"default_judge_weight": 1.0
```

A custom judge with no entry here scores at `default_judge_weight`. The names
MUST match the `name` field on the board's judges exactly.

## The promotion gate (two-sided)

A child replaces the parent only when BOTH hold:

- **Drift margin:** `child.scalar < parent.scalar - promote_margin`. A larger
  `promote_margin` demands a more convincing win and absorbs LLM run-to-run
  noise.
- **Pass-rate monotonicity:** with `pass_rate_monotonicity: true`, the gate
  guards pass-rate. The granularity is `pass_rate_monotonicity_scope`:
  - `"per_entry"` (default) — if **any** entry the parent passed comes back
    failing on the child, the child is **rejected** regardless of drift gains
    (rejection reason lists the regressing entries). Best for
    invariant/regression-suite boards where every entry must not regress.
  - `"aggregate"` — reject only when the child's **overall** pass-rate drops
    below the parent's (modulo float noise). A challenger may trade *which*
    entries pass as long as the net holds or improves. Best for sampled
    evaluation boards where individual pass/fail is noisy and a strictly-better
    challenger should not be vetoed by a single entry flip.

  Both guard against the proposer reducing drift by refusing to attempt hard
  entries. Flip `pass_rate_monotonicity` to `false` only for experimental epochs
  that expect non-monotone exploration (there is no `"off"` scope value).

## `scoring.json` is part of the evaluation contract

Weights are frozen per epoch. Editing `scoring.json` changes the contract hash,
and the next `evolve` (default auto-epoching) closes the current epoch and opens
a fresh one. Tune between epochs, not mid-epoch.

## Calibration workflow

Good weights are unknown until the loop has run real epochs.

1. Start near the defaults (`drift_weight = pass_weight = 1.0`, the shipped
   `severity_weights`).
2. Run an epoch.
3. Inspect the journal/analysis: do promoted generations match what you would
   have promoted by eye?
4. On disagreement, tune — a promoted generation whose idea you'd reject ⇒
   drift weights are off; a rejected generation whose patches you'd accept ⇒
   `pass_weight` too high / `promote_margin` too strict. Edit, start a new
   epoch, repeat.

## What good looks like

- Up-weight the drift kinds and judges that matter for *this* harness
  (`per_kind_weights` / `per_judge_weights`); leave the rest at default.
- Keep `pass_rate_monotonicity: true` for serious epochs.
- A `promote_margin` above the observed run-to-run noise floor so spurious
  deltas don't flip promotions.
