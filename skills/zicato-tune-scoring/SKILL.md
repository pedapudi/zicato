---
name: zicato-tune-scoring
description: Edit a zicato scoring.json — drift-loss weights, per_judge_weights/default_judge_weight, severity and per-kind weights, the declarative transform registry (pass_transform / drift_kind_aggregation), the dotted-spec scalar_fn / drift_reducer plugins, and the promotion gate (promote_margin + pass_rate_monotonicity). Use when calibrating how generations are scored or when tournament decisions disagree with operator intuition. Lower scalar = better.
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
(roughly `scalar = drift_weight * weighted_drift + pass_weight * (1 - mean_score)`).

Tuning is **three layers of escalating power**, neutral by default — adding
none of the lower layers leaves scoring byte-identical to the linear weights:

1. **Linear weights** (below) — the common 90%: which kinds/judges/severities
   matter, and how the two halves combine.
2. **Transform registry** (the "Non-linear shapes" section) — a non-linear
   *shape* (a quadratic recall curve, a diminishing-returns aggregation, a cap)
   without writing operator code. Declarative, serializable.
3. **Plugin escape hatch** (the "Plugins" section) — arbitrary pure logic
   (F-beta, cost-aware) as a dotted-spec function in the operator package.

Reach for the lowest layer that expresses the change. Do NOT add a transform or
plugin to do something linear weights already cover.

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
| `namespace_weights` | `{"drift:":1.0,"cost:":0.001,"latency:":0.0001,"rubric:":-1.0,"output:":0.0,"schema:":5.0}` | Per-namespace coefficients for the multi-objective scalar. The SIGN encodes the namespace's "worse" direction — positive = higher is worse, negative = higher is better (rubric), `0.0` = tracked but not optimised. |
| `namespace_monotonicity` | `{"drift:":false,"rubric:":true,"schema:":true}` | Per-namespace gate guards. A `true` namespace rejects any child that moved in that namespace's worse direction, even when the combined scalar improves. **Default-on for `rubric:` and `schema:`** — see the gate section. |
| `diff_complexity_weight` | `0.0` (off) | Opt-in parsimony/MDL term: adds `weight * (added + removed + patches)` to the challenger's scalar, biasing toward the smaller, more general edit. At `0.0` the term is exactly absent (omitted from the contract hash, so unset contracts never roll). |
| `diff_complexity_ceiling` | `0.0` (off) | Opt-in parsimony CEILING — a hard gate rule, not a loss nudge. Any `<= 0` is off; above that, a challenger whose diff complexity exceeds it is rejected outright. |

(The dataclass also carries an optional `regression_gate_enabled` /
`regression_test_command` test-suite gate; leave it off unless the snapshot
ships its own suite. The train/holdout split, tournament structure, and
proposer-quality knobs also live on `ScoringWeights` — see
`zicato-configure-tournament` and OVERFITTING.md for those.)

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

## The promotion gate (the rules, in the order they fire)

A child replaces the parent only when EVERY applicable rule holds. Rule 1 is
unconditional, Rules 2 and 3 are default-on, Rule 0 is opt-in:

- **Rule 0 — diff-complexity ceiling** (opt-in, `diff_complexity_ceiling > 0`):
  a structural admissibility veto applied *before* the scoring rules, so an
  over-budget edit is rejected naming the ceiling rather than a scoring
  near-miss (`diff_complexity_ceiling: diff complexity 14 exceeds ceiling 10`).
- **Rule 1 — drift margin:** `child.scalar <= parent.scalar - promote_margin`.
  A larger `promote_margin` demands a more convincing win and absorbs LLM
  run-to-run noise.
- **Rule 2 — pass-rate monotonicity:** with `pass_rate_monotonicity: true`, the
  gate guards pass-rate. The granularity is `pass_rate_monotonicity_scope`:
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

- **Rule 3 — per-namespace monotonicity** (default-on for `rubric:` and
  `schema:`): a child that moved a guarded namespace in its worse direction is
  rejected even when the combined scalar improved
  (`monotonicity_regression on namespace=rubric:`). This is the rule operators
  most often forget they have on — a quality drop or a new schema failure vetoes
  an otherwise-winning challenger. Turn a namespace off in
  `namespace_monotonicity` if you mean to allow that trade.

One more veto sits *after* those four rules: with the default-on train/holdout
split (`overfitting.enabled`, boards of `>= 6` entries), a win the train slice
measured must also not regress on the holdout, or it flips to
`holdout_not_confirmed`. The holdout is never asked to clear `promote_margin` in
the improving direction — merely holding flat confirms.

## Non-linear shapes — the transform registry

When a linear weight can't express the shape you want (you need a *curve*, not
just a coefficient), reach for a declarative transform. Each is a single
`{"op": "<name>", ...params}` spec from `zicato.scoring.transforms`:

| `op` | params | shape |
|---|---|---|
| `linear` | — | identity (neutral default) |
| `pow` | `exponent` | `x ** exponent` |
| `harmonic` | — | `1 + 1/2 + … + 1/n` (diminishing returns) |
| `cap` | `max` | `min(x, max)` |
| `clip` | `lo`, `hi` | clamp to `[lo, hi]` (needs `lo <= hi`) |
| `log1p` | — | `log(1 + x)` |

Two slots take a transform:

```json
"pass_transform":  { "op": "pow", "exponent": 2.0 },
"drift_kind_aggregation": {
  "looping_reasoning": { "op": "harmonic" },
  "off_topic":         { "op": "cap", "max": 5 }
}
```

- **`pass_transform`** reshapes the pass/miss term `(1 - mean_score)`.
  `{"op":"pow","exponent":2.0}` is the **replacement for the retired
  `pass_exponent`** field — express `pass_exponent=2` as this (a stray
  `pass_exponent` key is rejected at load, not silently dropped). Absent /
  `linear` = today's plain linear miss.
- **`drift_kind_aggregation`** reshapes, per drift KIND, how that kind's *count*
  aggregates into drift loss. An absent kind = `linear` = today's
  `severity × kind_weight × count`. `{"looping_reasoning":{"op":"harmonic"}}`
  opts THIS contract — and no other — into the harmonic looping curve (it used
  to be an unconditional core special-case for everyone).

One `op` per slot — no pipelines. Specs are **validated fail-fast at contract
load**: an unknown op, a missing/non-finite/typo'd param, or a `clip` with
`lo > hi` is rejected loudly at `evolve` time, never producing a `NaN`
mid-scoring.

## Plugins — the escape hatch for arbitrary logic

For anything the registry can't express (an F-beta recall/precision blend, a
cost-aware penalty), name a dotted-spec plugin in the operator package —
resolved by the SAME importer predicates/judges use:

```json
"drift_reducer": "mypkg.contract.scoring:my_drift_reducer",   // Seam 1: per-run drift loss
"scalar_fn":     "mypkg.contract.scoring:my_scalar"           // Seam 2: per-gen scalar
```

Each is a **pure, deterministic, NO-LLM, no-I/O** function over a frozen typed
context (`zicato.scoring.api` — `DriftContext` / `ScalarContext`). The context
carries the post-transform `builtin_loss` / `builtin_scalar`, so a plugin
*wraps/adjusts* the built-in rather than reimplementing it:

```python
# mypkg/contract/scoring.py — immutable to the proposer, like predicates/judges
def my_scalar(ctx) -> float:
    cost = ctx.namespace_aggregates.get("cost:", 0.0)
    return ctx.builtin_scalar + 0.001 * cost   # cost-aware penalty on top of the built-in
```

Rules:
- **Pure only** — no LLM, no I/O, no wall-clock; re-scoring must be reproducible.
- **Fail-open** — a plugin that raises / returns `NaN`/`inf` falls back to the
  pre-plugin (built-in / transformed) value, logged + recorded in provenance.
  Never crashes the run. Watch the dashboard for fail-open flags (next section).
- **Immutable to the proposer** — plugins live in the operator package, never
  enumerated as mutation points.
- `drift_reducer` runs inside the killable worker; `scalar_fn` in the
  orchestrator. Both fold into the contract hash, AND the plugin module's
  **source is hashed** — editing the plugin BODY rolls the epoch.

## Provenance — explaining a scalar

Each seam records a parseable provenance token: per-run
`loss.json::scoring_provenance` (Seam 1) and per-generation
`gen_score.json::scalar_provenance` (Seam 2). The dashboard's promote-gate
breakdown decomposes them into a per-side "which transform/plugin shaped this"
view. Token shapes: `builtin`, `transform:pass=pow(2.0)`,
`transform:drift{looping_reasoning=harmonic}`, `plugin:scalar_fn=<spec>`, and
the **fail-open** form `<token> (fallback: <reason>)` — surfaced prominently
(caution-colored) so a silently-degraded plugin is obvious, not buried in a log.

## `scoring.json` is part of the evaluation contract

Weights — AND the transforms and plugin specs — are frozen per epoch. Editing
`scoring.json` (or a referenced plugin's body) changes the contract hash, and
the next `evolve` (default auto-epoching) closes the current epoch and opens a
fresh one. Tune between epochs, not mid-epoch.

## Calibrating `promote_margin` against measurement

`promote_margin` is the one weight you do not have to guess. Two commands
measure the window it must sit inside — both are **live runs that spend budget**
(they execute the harness), so get the operator's go-ahead first:

```sh
.venv/bin/zicato board audit     --runs 5 --harness-call-llm … --auxiliary-call-llm …
.venv/bin/zicato board preflight --runs 5 --harness-call-llm … --auxiliary-call-llm …
```

`board audit` duels the champion against ITSELF and reports the A/A noise floor
(persisted onto the epoch as `noise_floor`). `board preflight` adds the
**degradation signal** — the champion versus deliberately degraded copies of
itself — and places `promote_margin` against the floor and that signal, naming
the side it fell outside of. Every one of these is a WARNING; none stops a run:

- `margin_below_floor` (WARN) — promotions cannot be told from re-rolls of the
  same generation. Raise the margin.
- `margin_above_achievable` (WARN) — the margin exceeds the only movement the
  probe demonstrated. **Read this one carefully.** The probe measures how far
  the scalar moved when a mutation point was DESTROYED — degradation headroom,
  how much the champion has left to LOSE — while a promotion needs movement the
  other way. The two are unrelated in general, and a champion sitting near the
  failing end has little left to break and plenty to gain, so improvement
  headroom is **UNMEASURED** (issue #119). Worth checking the margin against
  what a real fix is worth; NOT evidence the run is null. The signal is also a
  single-point lower bound (one mutation point per probe), so a margin
  deliberately above single-point reach — recombination unions two sub-margin
  fixes — is expected.
- `empty_window` (EMPTY) — the measured signal does not clear the noise floor,
  so **no** margin is defensible. Do not tune the margin; reduce evaluation
  noise (more `replicates`, steadier judges) or strengthen the board.
- `holdout_note` (prose, when the board is split) — the HOLDOUT confirmation's
  own bounds. See below.

The recommended margin is **2.5 × `delta_std`** — the standard deviation of the
A/A `delta_scalar`, the exact quantity the gate thresholds. Do NOT scale
`max_abs_delta` instead: it is a *range* statistic that grows without bound in
the draw count, so more calibration draws would inflate the recommendation
(issue #112). `delta_std` sharpens with more draws, which is the direction a
recommendation should move.

### The holdout has its own margin (issue #118)

`promote_margin` is calibrated against the TRAIN slice. When the split is
active, a promotion must also survive the holdout confirmation on a **smaller**
slice, whose scalar moves in coarser `1/N` steps — and sharing one knob left
real board shapes (the default 12-train / 6-holdout split with one holdout
entry flipping) with **no promotable margin at all**. Two contract fields split
the bounds; both default to exactly today's behaviour and neither moves the
contract hash at its default:

| Field | Default | Set it when |
|---|---|---|
| `holdout_margin` | `None` (= `promote_margin`) | holdout confirmations reject on movement the slice cannot avoid. Rule of thumb for commensurable bounds: `promote_margin × N_train / N_holdout`, roughly double on the default split. |
| `holdout_entry_regression_budget` | `0` (zero tolerance) | a single holdout entry flipping pass→fail is rejecting every candidate. At `0` that rejects at EVERY margin — no `holdout_margin` can fix it, because the pass-rate rule fires before any scalar bound. Set `1` to let the confirmation absorb one entry. |

The TRAIN side keeps its zero-tolerance rule either way, so neither knob
loosens the gate's primary decision.

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
- A `promote_margin` inside the measured window — above the A/A noise floor so
  spurious deltas don't flip promotions, below the achievable signal so
  something can still win.
