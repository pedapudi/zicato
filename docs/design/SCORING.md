# Scoring

zicato scores each generation against a frozen board and uses the
score to drive tournament promotion. The score is a **weighted scalar**
combining two signals:

- **Drift-derived loss.** A weighted sum across drift counts (by kind
  and severity), plan revisions, task failure ratio, runtime
  features, and abort. Always available; works without ground-truth
  expectations.
- **Pass-rate.** The fraction of board entries whose expectation
  predicate / regex / schema / judge passed. Only entries with an
  expectation contribute.

The two combine into a single tournament scalar via tunable weights.
This document specifies both halves, their combination, and the
promotion gate.

## 1. Why both signals

Drift loss alone scores "**how cleanly** did the run execute?". A
generation that produces fewer drift events generally has cleaner
runs — fewer plan revisions, fewer escalations, less looping.

Pass-rate alone scores "**did it do the right thing?**". A generation
that answers more questions correctly is better at its job
independent of how chaotic the path was.

Each on its own has a failure mode:

- **Drift only** misses the silent-success regression: the agent
  produces the wrong answer, smoothly, with no drift. Score goes up;
  quality went down.
- **Pass-rate only** misses the chaotic-success: the agent produces
  the right answer after seven plan revisions, three escalations, and
  burning through the budget. Score holds; quality went down.

Weighting both makes the system sensitive to both kinds of regression.
The operator gets a knob (the weights) to emphasize one over the
other when the project's priorities shift.

Both components are **always computed when both are available**. The
operator opts into pass-rate by attaching expectations to board entries
— expectations are not required for the loop to function, but a
generation can only beat its parent on pass-rate if expectations
exist on the board.

## 2. Per-entry drift loss

For one entry, the reducer computes a per-entry drift loss from the
fields of the `LossProfile` (see [TELEMETRY.md](TELEMETRY.md)):

```
drift_loss[entry] =
      sum over kind k of:
          per_kind_weight[k]      * drift_counts_by_kind[k]
    + sum over severity s of:
          severity_multiplier[s]  * drift_counts_by_severity[s]
    + plan_revisions_weight       * plan_revisions
    + task_failure_weight         * task_failure_ratio
    + escalation_weight           * escalations
    + runtime_weight              * runtime_fraction_over_budget
    + abort_weight                * (1 if aborted else 0)
```

Where:

| Symbol | Default | Meaning |
|---|---|---|
| `per_kind_weight[k]` | `1.0` (most), `2.0` for CRITICAL-class kinds like `INTENT_DIVERGENCE`, `HUMAN_INTERVENTION_REQUIRED` | Per-`DriftKind` weight. |
| `severity_multiplier[s]` | `INFO`: `0.5`, `WARNING`: `1.0`, `CRITICAL`: `2.0` | Multiplier on per-severity counts. |
| `plan_revisions_weight` | `0.5` | Each `PlanRevised` event is half a unit of loss. |
| `task_failure_weight` | `5.0` | The ratio is in `[0, 1]`; the weight scales it to a meaningful contribution. |
| `escalation_weight` | `1.0` | Each `lifecycle == ESCALATING` event. |
| `runtime_weight` | `2.0` | Multiplier on the runtime-over-budget fraction. |
| `abort_weight` | `20.0` | Heavy constant if the run aborted. |
| `runtime_fraction_over_budget` | derived | `max(0, (runtime_ms - 0.9 * budget_ms) / budget_ms)`; zero unless runtime exceeded 90% of budget. |

The per-kind weights are stored in `scoring.json` per epoch:

```json
{
  "per_kind_weight": {
    "DRIFT_KIND_CONFABULATION_RISK": 1.5,
    "DRIFT_KIND_CAPABILITY_MISMATCH": 2.0,
    "DRIFT_KIND_LOOPING_REASONING": 1.0,
    "DRIFT_KIND_LOOPING_TOOL_CALL": 1.0,
    "DRIFT_KIND_INTENT_DIVERGENCE": 2.0,
    "DRIFT_KIND_HUMAN_INTERVENTION_REQUIRED": 5.0,
    "_default": 1.0
  },
  "severity_multiplier": {
    "INFO": 0.5,
    "WARNING": 1.0,
    "CRITICAL": 2.0
  },
  "plan_revisions_weight": 0.5,
  "task_failure_weight": 5.0,
  "escalation_weight": 1.0,
  "runtime_weight": 2.0,
  "abort_weight": 20.0,
  "w_drift": 0.5,
  "w_pass": 0.5,
  "tournament_margin": 0.05,
  "drift_tolerance_band": 0.02
}
```

Per-kind weight lookup uses `per_kind_weight[k]` if present,
`per_kind_weight["_default"]` otherwise. This lets the operator
elevate or demote specific drift kinds without re-listing the full
taxonomy.

### 2.1 Why drift counts are weighted, not pass/fail-only

A drift event is **information**. It says "the runtime saw this shape
of trouble during this run". Counting drift events as a loss term
captures the cleanness of the run independent of whether the final
answer was right. Counting only `pass_fail` would discard that signal.

The weights are part of the **evaluation contract** (they live in
`scoring.json`, which is frozen per epoch). Changing weights changes
the contract; if the operator wants different weights, they start a
new epoch.

### 2.2 Why an abort is a heavy constant

A run that exhausted its wall-clock budget didn't produce a result.
The expectation can't fire; the drift counts are incomplete; the
reducer can't tell whether the agent was about to succeed or still
flailing. The conservative interpretation is "this is the worst
possible outcome for this entry", and the heavy `abort_weight`
ensures that interpretation drives the score.

`abort_weight = 20.0` is large but not infinite. A run that aborts but
also racked up many drift events scores even worse — the abort is the
heaviest single term, not the only term.

## 3. Per-entry pass-rate

For an entry with an expectation, the reducer evaluates the
expectation against the run result and stamps `pass_fail: bool`. For
an entry without an expectation, `pass_fail` is `None` and the entry
does not contribute to the pass-rate denominator.

The expectation kinds and their evaluation are specified in
[BOARD-FORMAT.md](BOARD-FORMAT.md) §3.

## 4. Per-generation aggregate score

Given a generation's loss profiles for all N board entries:

```
total_weight   = sum over entries i of entry.weight
weighted_drift = sum over entries i of entry.weight * loss_profile[i].drift_loss / total_weight

passes_count   = sum over entries i with expectation of
                     entry.weight * (1 if pass_fail else 0)
total_with_exp = sum over entries i with expectation of entry.weight
pass_rate      = passes_count / max(1, total_with_exp)

gen_score = w_drift * weighted_drift + w_pass * (1.0 - pass_rate)
```

Notes:

- `weighted_drift` is the weighted **mean** drift loss across entries.
  Per-entry `weight` from the board scales contributions; the
  denominator normalises back so a board where every weight is `1.0`
  behaves identically to an unweighted mean.
- `pass_rate` is the weighted fraction of entries-with-expectations
  that passed. If no entry has an expectation, `pass_rate` is `1.0`
  by convention (no failures observed → no failures to count) and
  the `w_pass` term contributes 0 to the score.
- Both terms are oriented so **lower is better**:
  - `weighted_drift` is non-negative, larger when the runs were
    messier.
  - `1.0 - pass_rate` is in `[0, 1]`, larger when more entries
    failed.
- `gen_score` is non-negative; smaller is better.

The aggregate is stored in `gen_score.json`:

```json
{
  "generation": "v1",
  "epoch_id": "initial",
  "weighted_drift": 1.23,
  "pass_rate": 0.85,
  "total_entries": 20,
  "entries_with_expectation": 15,
  "passes_weighted": 12.75,
  "score": 0.69,
  "w_drift": 0.5,
  "w_pass": 0.5,
  "computed_at": "2026-04-03T12:34:00Z"
}
```

`zicato tournament v3 v4` displays both generations' `gen_score.json`
side by side.

### 4.1 Default weights and the calibration problem

The default weights in `scoring.json` are a *starting point*, not a
final answer. The right weights depend on:

- Which drift kinds the operator cares about most for *this* inner
  harness.
- How costly an abort really is (does it always mean failure, or
  sometimes the agent was on the right track and just slow?).
- Whether pass-rate or cleanness dominates the operator's notion
  of "better".

The honest position is that **good defaults are unknown until the
loop has run real epochs**. The recommended workflow:

1. Start with equal weights (`w_drift = w_pass = 0.5`) and the
   per-kind weights from §2.
2. Run an epoch.
3. Inspect the journal: do the promoted generations align with what
   the operator would have promoted by eye?
4. If `gen_score` decisions disagree with the operator's
   intuition, tune `scoring.json`, start a new epoch, repeat.

The first few epochs after registering a new inner harness are
expected to be calibration epochs. The journal makes the disagreement
visible (a promoted generation whose `core_idea` the operator would
have rejected → drift weights are off; a rejected generation whose
patches the operator agrees with → pass-rate weight is too high).

## 5. The tournament promotion gate

The promotion gate decides whether the candidate replaces the parent.
It is two-sided:

- **Drift side: margin.** The candidate's `weighted_drift` must beat
  the parent's by at least `tournament_margin`. A small tolerance
  band on either side absorbs run-to-run noise.
- **Pass-rate side: strict monotonicity.** The candidate must NOT
  regress on any pre-existing pass. If any entry whose expectation
  the parent passed has its candidate `pass_fail` come back `False`,
  the candidate is **rejected** regardless of drift improvements.

Together:

```
promote iff (candidate.score < parent.score - tournament_margin)
        AND for every entry i with expectation:
              if parent.pass_fail[i] is True, then candidate.pass_fail[i] is True
```

(Score is "lower is better"; the margin is subtracted from the parent
to require a meaningful win.)

If the strict-monotonicity check fails, the rejection reason is
`pass_rate_regression_on_<entry_id>` (the first regressing entry is
named). If the margin check fails, the rejection reason is
`insufficient_margin (delta={delta:.3f}, required={margin:.3f})`.

### 5.1 Why strict monotonicity on pass-rate

Pass-rate regressions are *categorical* failures: an entry that
passed before now fails. There is no "small" regression on a passing
entry — either the right answer is still produced or it isn't. Drift
loss is *graded*: messier vs cleaner, on a continuum.

Treating the two differently captures the asymmetry. A candidate
that reduces drift but breaks a passing entry is almost always worse
than the parent (the loop's whole point is to reduce drift WITHOUT
breaking what worked).

The strict gate is also a guard against the proposer over-fitting to
drift patterns at the expense of correctness — a tightened prompt
might reduce CONFABULATION_RISK by also refusing to attempt the
question, which would tank pass-rate. The gate catches this.

### 5.2 Why a tolerance band on drift

LLM-driven runs are noisy. The same generation against the same
entry produces somewhat different drift counts across reruns. The
tournament margin is the operator's stated noise floor: "I only
believe a drift-side improvement if the candidate beats the parent
by more than this."

A `tournament_margin` of `0.05` means the candidate must improve
`score` by at least 0.05 to be promoted. For a typical `gen_score`
in the `0.5 - 1.5` range, that is roughly a 3-10% improvement.

The `drift_tolerance_band` field in `scoring.json` is a separate
parameter intended for future use (multi-trial scoring with
confidence intervals); v0 reads it but only uses
`tournament_margin`.

## 6. Worked example

A small board with 3 entries; v3 → v4 candidate proposal.

**v3 (parent):**

| Entry | Weight | `pass_fail` | `drift_loss` |
|---|---|---|---|
| `short_solar` | 1.0 | True | 0.4 |
| `long_solar` | 1.5 | True | 1.2 |
| `contradictory` | 1.0 | False | 2.5 |

```
total_weight = 1.0 + 1.5 + 1.0 = 3.5
weighted_drift = (1.0*0.4 + 1.5*1.2 + 1.0*2.5) / 3.5 = 4.7 / 3.5 = 1.343

entries_with_exp = all 3 (every entry has an expectation)
passes_weighted = 1.0*1 + 1.5*1 + 1.0*0 = 2.5
total_with_exp = 1.0 + 1.5 + 1.0 = 3.5
pass_rate = 2.5 / 3.5 = 0.714

score = 0.5 * 1.343 + 0.5 * (1.0 - 0.714) = 0.672 + 0.143 = 0.814
```

**v4 (candidate):**

| Entry | Weight | `pass_fail` | `drift_loss` |
|---|---|---|---|
| `short_solar` | 1.0 | True | 0.3 |
| `long_solar` | 1.5 | True | 0.9 |
| `contradictory` | 1.0 | True | 1.8 |

```
weighted_drift = (1.0*0.3 + 1.5*0.9 + 1.0*1.8) / 3.5 = 3.45 / 3.5 = 0.986
pass_rate = (1.0*1 + 1.5*1 + 1.0*1) / 3.5 = 3.5 / 3.5 = 1.0
score = 0.5 * 0.986 + 0.5 * 0.0 = 0.493
```

**Tournament gate:**

- Margin: `parent.score - candidate.score = 0.814 - 0.493 = 0.321 >
  0.05` ✓
- Monotonicity: parent passes were `{short_solar, long_solar}`;
  candidate passes those plus `contradictory`. No regression. ✓

**Decision: promote.** v4 becomes the new parent.

If instead v4's `contradictory` was `pass_fail = True` but `long_solar`
flipped to `False` — even with substantially lower drift — the
monotonicity check fails and v4 is rejected.

## 7. Fast mode and the tournament

`zicato evolve --mode fast` skips the parent-vs-candidate A/B run on
the same board. Instead, it accepts or rejects the candidate against
the parent's **historical** score on the same board.

The fast-mode gate:

```
fast_promote iff (candidate.score < parent.historical_score - tournament_margin)
             AND no pass-rate regression observed on the candidate's run

(strictly stronger margin recommended in fast mode — historical
parent scores have their own staleness noise)
```

Fast mode is less rigorous: the world may have drifted (LLM provider
updated; rate-limit shape changed; a tool dependency upgraded
silently) between when the parent was scored and when the candidate
is being scored. The parent's historical score is no longer a fair
comparison.

In exchange, fast mode is **much faster**: one full board run instead
of two. The default is the rigorous tournament. Fast mode is opt-in
per `zicato evolve` invocation, not a project-wide default. Operators
who care about iteration speed in early calibration epochs reach for
it; serious epochs run the tournament.

## 8. Stamping outcomes onto the experiment

Once the tournament gate has decided, the tournament runner appends
the `outcome` block to the candidate's `experiment.json` (see
[EPOCHS-AND-JOURNALING.md §3.3](EPOCHS-AND-JOURNALING.md#33-outcome-written-after-the-run)).

The `drift_loss_delta`, `pass_rate_delta`, and `tournament_decision`
fields in `outcome` are populated from the gate's computation. The
`rejection_reason` field carries either:

- `null` (promote)
- `pass_rate_regression_on_<entry_id>` (monotonicity failure)
- `insufficient_margin (delta=X, required=Y)` (margin failure)

This is the single audit trail for why a candidate was or was not
promoted.

## 9. Limits and caveats

A few things scoring does NOT do in v0:

- **Multi-trial scoring per entry.** v0 runs each entry once per
  generation. LLM noise means small score deltas are sometimes
  spurious; the right answer is N trials per entry with confidence
  intervals. v0 leaves this to the conservative `tournament_margin`.
- **Cost-aware scoring.** Token counts and per-call cost are not in
  the v0 `LossProfile` (the goldfive `GoldfiveLLMCallStart/End`
  events carry latency but not token usage in v0). A future field
  `cost_units` is the natural extension; this is forced by **target
  2** (see [DOGFOOD-TARGETS.md](DOGFOOD-TARGETS.md)) which needs
  cost as a loss term.
- **Operator pinning.** "This entry must pass" or "the score must
  improve on this tag slice" as hard gates are not in v0. The
  rubric's `forbidden:` covers the mutation-side of pinning; the
  scoring side is implicit through pass-rate monotonicity.

These are roadmap items, not contract failures. The v0 score is
intentionally narrow.

## 10. Cross-references

| Topic | Document |
|---|---|
| `LossProfile` fields and how they're computed | [TELEMETRY.md](TELEMETRY.md) |
| `BoardEntry.weight` and `expectation` | [BOARD-FORMAT.md](BOARD-FORMAT.md) |
| `tournament_decision` field on `experiment.json` | [EPOCHS-AND-JOURNALING.md](EPOCHS-AND-JOURNALING.md) |
| Target 2's non-drift loss model | [DOGFOOD-TARGETS.md](DOGFOOD-TARGETS.md) |
| Why drift loss + pass-rate and not free-text scoring | [RATIONALE.md](RATIONALE.md) |
