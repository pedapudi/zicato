# Scoring

zicato scores each generation against a frozen board and uses the
score to drive tournament promotion. The score is a **weighted scalar**
combining two signals:

- **Drift-derived loss.** A weighted sum across drift counts (by kind
  and severity), plan revisions, task failure ratio, runtime
  features, and abort. Always available; works without ground-truth
  expectations.
- **Pass-rate.** The fraction of board entries whose outcome checks
  (`expectations` — `Predicate` / `Rubric`) passed. Only entries with
  a non-empty `expectations` list contribute.

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
fields of the `LossProfile` (see [TELEMETRY.md](TELEMETRY.md)). Each
drift count is weighted by its **severity** and, for custom-judge
drift, by its **judge name**; the per-kind multiplier stacks on top.
Plan revisions and (optionally) runtime add their own terms:

```
drift_loss[entry] =
      sum over (kind k, severity s) of:
          per_kind_weights[k] * severity_weights[s] * drift_counts[k, s]
    + plan_revision_weight * plan_revisions
    + runtime_weight       * runtime_seconds
```

Custom-judge drift is a refinement *within* the loss, not a separate
term. Every custom-judge violation is a `custom`-kind drift carrying
the judge's stable `judge_name`; the reducer attributes it to that
judge and weights it by `per_judge_weights[judge_name]` (stacked with
`severity_weights[s]`), falling back to `default_judge_weight` when the
judge has no entry. See §2.2.

Where (defaults are the `ScoringWeights` field defaults):

| Symbol | Default | Meaning |
|---|---|---|
| `severity_weights[s]` | `info`: `1.0`, `warning`: `3.0`, `critical`: `10.0` | Per-severity multiplier (keys are lowercase). A missing key contributes `0.0`. |
| `per_kind_weights[k]` | empty mapping → uniform `1.0` weighting across kinds | Optional per-drift-kind multiplier, keyed on the lowercase drift-kind string (e.g. `confabulation_risk`). Stacks multiplicatively with `severity_weights`. |
| `per_judge_weights[j]` | empty mapping → falls back to `default_judge_weight` | Per-custom-judge multiplier, keyed on the judge `name`. See §2.2. |
| `default_judge_weight` | `1.0` | Fallback multiplier for a custom judge absent from `per_judge_weights`. |
| `plan_revision_weight` | `0.5` | Each plan-revision event is half a unit of loss. |
| `runtime_weight` | `0.0` | Coefficient on per-second runtime. Off by default — operators usually rely on the per-entry wall-clock budget as a hard ceiling rather than scoring runtime continuously. |

There is no separate `task_failure`, `escalation`, or `abort` weight in
the shipped `ScoringWeights` — a budget-exceeded run is recorded via
`LossProfile.wall_clock_budget_exceeded` and scored worst-case by the
reducer, not via a dedicated weight.

The weights are stored in `scoring.json` per epoch. Keys are the
`ScoringWeights` field names; severity keys and drift-kind keys are
**lowercase**:

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
  "per_judge_weights": {
    "cite-before-metric": 2.0,
    "ack-before-edit": 0.5
  },
  "default_judge_weight": 1.0,
  "plan_revision_weight": 0.5,
  "runtime_weight": 0.0,
  "promote_margin": 0.01,
  "pass_rate_monotonicity": true
}
```

Per-kind weight lookup uses `per_kind_weights[k]` if present and a
uniform `1.0` otherwise — there is no `_default` sentinel key. An empty
`per_kind_weights` mapping weights every kind uniformly; the operator
adds entries only to elevate or demote specific drift kinds.

> **Future / roadmap.** `ScoringWeights` also carries a multi-objective
> surface — `namespace_weights` and `namespace_monotonicity` — that
> generalises drift loss into namespaced metrics (`drift:`, `cost:`,
> `latency:`, `rubric:`, `schema:`, `output:`). These ship today on the
> dataclass but are the forward path for the cost-aware scoring of
> target 2 (§9); the single-axis drift-loss view in this section is the
> primary surface for v0 boards. See §5 for the per-namespace
> monotonicity gate.

### 2.1 Why drift counts are weighted, not pass/fail-only

A drift event is **information**. It says "the runtime saw this shape
of trouble during this run". Counting drift events as a loss term
captures the cleanness of the run independent of whether the final
answer was right. Counting only `pass_fail` would discard that signal.

The weights are part of the **evaluation contract** (they live in
`scoring.json`, which is frozen per epoch). Changing weights changes
the contract; if the operator wants different weights, they start a
new epoch.

### 2.2 Per-judge weights

A board's custom judges (`Judge.custom` / `Judge.python` — see
[BOARD-FORMAT.md](BOARD-FORMAT.md) §4) all emit the same drift kind,
`custom`. `per_kind_weights["custom"]` (or the uniform `1.0` when it is
unset) therefore weights *every* custom judge identically. When that is
too coarse — when a violation of one judge should count for more than a
violation of another — `per_judge_weights` is the finer knob.

`per_judge_weights` is a mapping **keyed on the judge `name`** (the
first argument to `Judge.custom` / `Judge.python`, and the same
string carried as `judge_name` on the emitted drift). The reducer,
when it counts a `custom` drift, attributes it to its authoring judge
(folded into the drift kind as `custom:<judge_name>`) and looks up
`per_judge_weights[judge_name]`; if the judge has no entry there, it
falls back to `default_judge_weight` (default `1.0`).

In the `ScoringWeights` dataclass these are the `per_judge_weights` and
`default_judge_weight` fields. Like every other weight they are frozen
per epoch — changing them changes the evaluation contract and rolls the
epoch. `per_judge_weights` mirrors `per_kind_weights`: `per_kind_weights`
discriminates by drift kind, `per_judge_weights` discriminates one step
finer, by `judge_name`, within the `custom` kind. The reducer also
preserves the per-judge breakdown on each `LossProfile.per_judge_loss`
(a tuple of `JudgeLoss` records) so downstream attribution does not have
to re-walk the events.

Example: a board with two judges, `cite-before-metric` and
`ack-before-edit`, where a missing citation is a real defect but a
missing acknowledgement is a nicety:

```json
"per_judge_weights": {
  "cite-before-metric": 2.0,
  "ack-before-edit": 0.5
}
```

A third custom judge not listed here is weighted by
`default_judge_weight` (default `1.0`).

### 2.3 Aborted runs are scored worst-case

A run that exhausted its wall-clock budget didn't produce a result.
The expectation can't fire and the drift counts are incomplete; the
reducer can't tell whether the agent was about to succeed or still
flailing. There is no dedicated abort weight in `ScoringWeights` —
instead the reducer records `LossProfile.wall_clock_budget_exceeded`
and treats the run as worst-case for that entry when it derives the
profile's `drift_loss` and `pass_fail`. The conservative
interpretation — "this is the worst possible outcome for this entry" —
is therefore baked into the per-run profile, not applied as a separate
scoring term.

## 3. Per-entry pass-rate

For an entry with a non-empty `expectations` list, the reducer
evaluates every outcome check against the run result and ANDs the
results into `pass_fail: bool` — the entry passes iff every
expectation passes (advisory `Rubric`s, `threshold=None`, always
pass). For an entry with an empty `expectations` list, `pass_fail` is
`None` and the entry does not contribute to the pass-rate
denominator.

The outcome-check kinds and their evaluation are specified in
[BOARD-FORMAT.md](BOARD-FORMAT.md) §3.

Note the division of labour: an entry's **process** checks (`judges`,
[BOARD-FORMAT.md](BOARD-FORMAT.md) §4) never touch pass-rate. A
violated judge emits a `DriftKind.CUSTOM` drift, which is counted in
`drift_counts_by_kind` and feeds the **drift-loss** side of the score
(§2). Pass-rate is purely the outcome-check side; drift loss is where
both built-in and custom-judge violations land.

## 4. Per-generation aggregate score

Given a generation's loss profiles for all N board entries
(`aggregate_generation_score` then `combined_scalar` in
`telemetry/scoring.py`):

```
drift_loss_mean = mean over entries i of loss_profile[i].drift_loss

observed        = count of entries i whose pass_fail is not None
passes          = count of those entries with pass_fail == True
pass_rate       = passes / observed   (or 1.0 when observed == 0)

scalar = drift_weight * drift_loss_mean + pass_weight * (1.0 - pass_rate)
```

Notes:

- `drift_loss_mean` is the **arithmetic mean** drift loss across the
  generation's runs (one run per board entry). The shipped aggregator
  does not re-weight by `BoardEntry.weight` — the per-entry `weight`
  field exists on the board but the v0 aggregator means uniformly; an
  empty generation means `0.0`.
- `pass_rate` counts only runs whose `pass_fail` is not `None`
  (entries with an expectation). If no run has an expectation,
  `pass_rate` is `1.0` by convention (no failures observed) so the
  `pass_weight` term contributes 0 to the scalar.
- Both terms are oriented so **lower is better**:
  - `drift_loss_mean` is non-negative, larger when the runs were
    messier.
  - `1.0 - pass_rate` is in `[0, 1]`, larger when more entries
    failed.
- `scalar` is non-negative; smaller is better.

The two terms are **additive**, not multiplicative, so an epoch can
zero out one axis (set `pass_weight = 0` to ignore expectations
entirely, or `drift_weight = 0` to score on pass-rate alone) without
obliterating the other. The shipped defaults are equal (`drift_weight =
pass_weight = 1.0`), which keeps the two axes commensurate during early
dogfood.

`zicato tournament v3 v4` prints the tournament result — both
generations' aggregates and the gate verdict — as a JSON object.

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

1. Start with equal weights (`drift_weight = pass_weight = 1.0`) and
   the per-kind / severity weights from §2.
2. Run an epoch.
3. Inspect the journal: do the promoted generations align with what
   the operator would have promoted by eye?
4. If the scalar's decisions disagree with the operator's
   intuition, tune `scoring.json`, start a new epoch, repeat.

The first few epochs after registering a new inner harness are
expected to be calibration epochs. The journal makes the disagreement
visible (a promoted generation whose `core_idea` the operator would
have rejected → drift weights are off; a rejected generation whose
patches the operator agrees with → pass-rate weight is too high).

## 5. The tournament promotion gate

The promotion gate (`evaluate_gate` in `tournament/gate.py`) decides
whether the candidate (child) replaces the parent. It applies three
rules **in order** and promotes only if none rejects. It returns a
`GateOutcome` with fields:

| `GateOutcome` field | Meaning |
|---|---|
| `decision` | `"promoted"` or `"rejected"`. (`"deferred"` is in the `TournamentDecision` literal but `evaluate_gate` itself only returns the first two — deferral is a runner-level concept.) |
| `reason` | Human-readable explanation; empty string when promoted, otherwise names the rule that fired (and, for pass-rate / namespace regressions, the entries / namespaces). |
| `delta_scalar` | `child.scalar - parent.scalar`. Negative = improvement. |
| `delta_pass_rate` | `child.pass_rate - parent.pass_rate`. Positive = improvement. |

**Rule 1 — scalar margin.** The combined scalar is "lower is better",
so a promotion needs the child's loss to drop by at least
`promote_margin`. The literal reject condition is:

```
reject iff child.scalar > parent.scalar - promote_margin
```

The rejection reason distinguishes two cases: a child whose loss *rose*
(`delta_scalar > 0`) is rejected with a `"challenger regressed: ..."`
reason; a child that improved but by less than `promote_margin` is
rejected with an `"insufficient improvement: ..."` reason. Both reasons
state the real child-minus-parent delta and cite `promote_margin` as
the threshold.

**Rule 2 — pass-rate monotonicity** (only when
`pass_rate_monotonicity` is `true`, the default). For every entry where
the parent recorded `pass_fail == True`, the child MUST also record
`pass_fail == True`. A child whose `pass_fail` comes back `False` *or*
`None` (the expectation no longer evaluated, or the entry did not run)
on a previously-passing entry is a regression. If any such entry
regressed the gate rejects with `"pass-rate regression on entries:
<id>, <id>, ..."` (every regressing entry id, sorted). Entries the
parent failed or had no expectation for are not gated on this rule.

**Rule 3 — per-namespace monotonicity** (roadmap surface; see the §2
note). For each namespace whose flag in `namespace_monotonicity` is
`true`, the child's weighted per-namespace aggregate may not have moved
in the namespace's "worse" direction (the sign of the `namespace_weights`
coefficient already folds direction into the aggregate). A regression
rejects with `"monotonicity_regression on namespace=<ns>, ..."`. Zero-
weight namespaces are skipped. The shipped defaults guard `rubric:` and
`schema:` and leave `drift:` unguarded.

If no rule rejects, the gate returns `decision="promoted"`.

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
promote margin is the operator's stated noise floor: "I only
believe an improvement if the candidate beats the parent by more than
this."

`promote_margin` defaults to `0.01` in `ScoringWeights`. A
`promote_margin` of `0.01` means the candidate must lower its combined
`scalar` by at least 0.01 to be promoted. Operators raise it for noisy
boards where small deltas are likely spurious.

There is no separate `drift_tolerance_band` parameter in the shipped
`ScoringWeights` — the single `promote_margin` threshold absorbs
run-to-run noise on the combined scalar. Multi-trial scoring with
confidence intervals remains a roadmap item (§9).

## 6. Worked example

A small board with 3 entries; v3 → v4 candidate proposal.

Using the shipped defaults (`drift_weight = pass_weight = 1.0`,
`promote_margin = 0.01`) and the unweighted mean.

**v3 (parent):**

| Entry | `pass_fail` | `drift_loss` |
|---|---|---|
| `short_solar` | True | 0.4 |
| `long_solar` | True | 1.2 |
| `contradictory` | False | 2.5 |

```
drift_loss_mean = (0.4 + 1.2 + 2.5) / 3 = 4.1 / 3 = 1.367

observed = 3 (every entry has an expectation)
passes   = 2  (short_solar, long_solar)
pass_rate = 2 / 3 = 0.667

scalar = 1.0 * 1.367 + 1.0 * (1.0 - 0.667) = 1.367 + 0.333 = 1.700
```

**v4 (candidate):**

| Entry | `pass_fail` | `drift_loss` |
|---|---|---|
| `short_solar` | True | 0.3 |
| `long_solar` | True | 0.9 |
| `contradictory` | True | 1.8 |

```
drift_loss_mean = (0.3 + 0.9 + 1.8) / 3 = 3.0 / 3 = 1.000
pass_rate = 3 / 3 = 1.0
scalar = 1.0 * 1.000 + 1.0 * 0.0 = 1.000
```

**Tournament gate:**

- Rule 1 (scalar margin): `child.scalar = 1.000`,
  `parent.scalar - promote_margin = 1.700 - 0.01 = 1.690`. Since
  `1.000 > 1.690` is false, the rule does NOT reject. `delta_scalar =
  1.000 - 1.700 = -0.700` (an improvement). ✓
- Rule 2 (pass-rate monotonicity): parent passes were `{short_solar,
  long_solar}`; candidate passes those plus `contradictory`. No
  previously-passing entry regressed. ✓
- Rule 3 (per-namespace monotonicity): with default weights `drift:` is
  unguarded; `rubric:` / `schema:` aggregates are absent here. ✓

**Decision: `promoted`.** v4 becomes the new parent.

If instead v4's `contradictory` came back `pass_fail = True` but
`long_solar` flipped to `False` — even with substantially lower drift —
Rule 2 fires and the gate returns `decision="rejected"` with reason
`"pass-rate regression on entries: long_solar"`.

## 7. Fast mode and the tournament

`zicato evolve --mode fast` skips the parent-vs-candidate A/B run on
the same board. Instead, it accepts or rejects the candidate against
the parent's **historical** score on the same board.

The fast-mode gate runs the same `evaluate_gate` rules (§5), but the
parent side comes from its cached historical aggregate rather than a
fresh run:

```
fast_promote iff (child.scalar <= parent.historical_scalar - promote_margin)
             AND no pass-rate regression observed on the candidate's run
             AND no guarded-namespace regression

(a stronger promote_margin is advisable in fast mode — historical
parent scores carry their own staleness noise)
```

Fast mode is less rigorous: the world may have drifted (LLM provider
updated; rate-limit shape changed; a tool dependency upgraded
silently) between when the parent was scored and when the candidate
is being scored. The parent's historical score is no longer a fair
comparison.

In exchange, fast mode is **much faster**: one board run instead of
two. The `--mode` flag selects between the two; note the two CLI
entry points ship with *different* defaults — `zicato evolve --mode`
defaults to **fast** (the loop favours iteration speed and re-scores
the champion only when no cache exists), while the standalone `zicato
tournament --mode` defaults to **full** (an explicit one-off re-score
of a specific pair). See [TOURNAMENT.md](TOURNAMENT.md) for the CLI
detail.

## 8. Stamping outcomes onto the experiment

Once the tournament gate has decided, the tournament runner appends
the `outcome` block to the candidate's `experiment.json` (see
[EPOCHS-AND-JOURNALING.md §3.3](EPOCHS-AND-JOURNALING.md#33-outcome-written-after-the-run)).

The `drift_loss_delta`, `pass_rate_delta`, `scalar_score_delta`, and
`tournament_decision` fields in `outcome` (an `OutcomeRecord`) are
populated from the gate's computation. The `rejection_reason` field
(empty string when promoted) carries the gate's `reason`, one of:

- `""` (promoted)
- `"insufficient improvement: ..."` (Rule 1, child improved but under `promote_margin`)
- `"challenger regressed: ..."` (Rule 1, child's loss rose)
- `"pass-rate regression on entries: <id>, ..."` (Rule 2)
- `"monotonicity_regression on namespace=<ns>, ..."` (Rule 3)

This is the single audit trail for why a candidate was or was not
promoted.

## 9. Limits and caveats

A few things scoring does NOT do in v0:

- **Multi-trial scoring per entry.** v0 runs each entry once per
  generation. LLM noise means small score deltas are sometimes
  spurious; the right answer is N trials per entry with confidence
  intervals. v0 leaves this to the conservative `promote_margin`.
- **Cost-aware scoring.** Token counts and per-call cost are not in
  the v0 `LossProfile` (the goldfive `GoldfiveLLMCallStart/End`
  events carry latency but not token usage in v0). A future field
  `cost_units` is the natural extension; this is forced by **target
  2** (see [DOGFOOD-TARGETS.md](DOGFOOD-TARGETS.md)) which needs
  cost as a loss term.
- **Operator pinning.** "This entry must pass" or "the score must
  improve on this tag slice" as hard gates are not in v0. The
  proposer brief's `## Forbidden` list covers the mutation-side of
  pinning; the scoring side is implicit through pass-rate
  monotonicity.

These are roadmap items, not contract failures. The v0 score is
intentionally narrow.

## 10. Cross-references

| Topic | Document |
|---|---|
| `LossProfile` fields and how they're computed | [TELEMETRY.md](TELEMETRY.md) |
| `BoardEntry.weight`, `expectations`, and `judges` | [BOARD-FORMAT.md](BOARD-FORMAT.md) |
| Authoring outcome/process checks and `per_judge_weights` | [BOARD-AUTHORING.md](BOARD-AUTHORING.md) |
| `tournament_decision` field on `experiment.json` | [EPOCHS-AND-JOURNALING.md](EPOCHS-AND-JOURNALING.md) |
| Target 2's non-drift loss model | [DOGFOOD-TARGETS.md](DOGFOOD-TARGETS.md) |
| Why drift loss + pass-rate and not free-text scoring | [RATIONALE.md](RATIONALE.md) |
