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

The two combine into a single tournament scalar. The combination is
**operator-tunable** at three levels of escalating power: a fixed
vocabulary of linear weights (the 90%), a declarative **transform
registry** for non-linear shapes (`pow` / `harmonic` / `cap` / `clip`
/ `log1p`) with no operator code, and a dotted-spec **plugin** escape
hatch for arbitrary pure logic — the same operator-owned, contract-
referenced mechanism `predicates.py` and `judges.py` already use. This
document specifies both halves, their combination (weights → transforms
→ plugins), the seam architecture, scoring provenance, and the
promotion gate.

> **Not "fixed linear weights only".** Earlier zicato could shape the
> score *only* through linear weights, so every new scoring *shape* (a
> non-linear recall curve, a diminishing-returns aggregation, a cap)
> leaked into core as a bespoke field or an unconditional special-case.
> Issue #19 closed that gap. The linear weights below are the neutral
> default; §11 documents the transform registry + plugin seams that
> reshape them without a core edit, and §2.4 covers the source-hashing
> that rolls the epoch when a plugin body changes.

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
  "pass_rate_monotonicity": true,
  "pass_rate_monotonicity_scope": "per_entry"
}
```

`pass_rate_monotonicity_scope` is optional and defaults to `"per_entry"`;
set it to `"aggregate"` to gate on the overall pass-rate instead of every
individual entry (see Rule 2 in §5).

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

### 2.4 Scoring config is part of the frozen contract

Every `ScoringWeights` field — the linear weights, the gate knobs, the
declarative transforms (§11.1), and the plugin specs (§11.2) — folds
into the **frozen per-epoch contract hash** through the
field-enumerating canonicalizer (`zicato/epoch/contract.py`). Changing
any of them rolls the epoch: the next `evolve` closes the current epoch
and opens a fresh one, so a scoring change is never silently applied to
an in-flight epoch's already-scored generations.

For the dotted-spec plugins this goes one step further than a plain
field: the canonicalizer hashes the **plugin spec string AND the
resolved module's source** (`spec_with_source_hash`), so editing a
plugin's *body* — not just its dotted reference — is detected as a
contract change and rolls the epoch. This is the **single
source-hashing mechanism shared across every grading plugin**
(predicates, judges, the outcome summarizer, and the scoring `scalar_fn`
/ `drift_reducer`): a body edit anywhere on the operator's grading
surface rolls the contract consistently. The declarative transforms
need no source hashing — they serialize natively as dicts and are
covered by the field canonicalizer for free.

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

Given a generation's loss profiles for all N board entries, the live
path is `aggregate_generation_score` in `tournament/scoring.py`, which
mechanically aggregates the per-run profiles (means, namespace rollups)
and then synthesises the scalar through **Seam 2** — the scoring
dispatcher `zicato.scoring.dispatch.resolve_scalar`, which composes the
built-in formula (`zicato.scoring.builtins.builtin_scalar`) with any
declarative `pass_transform` and dotted-spec `scalar_fn` plugin (§11).
The built-in formula is:

```
drift_loss_mean = mean over entries i of loss_profile[i].drift_loss

observed        = count of entries i whose pass_fail is not None
passes          = count of those entries with pass_fail == True
pass_rate       = passes / observed   (or 1.0 when observed == 0)

scalar = drift_weight * drift_loss_mean + pass_weight * (1.0 - mean_score)
```

(The pass term runs on the uniform continuous `mean_score` axis, issue
#18; on an all-bool board `mean_score == pass_rate`, so this is
byte-identical to the historical `(1 - pass_rate)` term. There is a
`telemetry/scoring.py::combined_scalar` helper, but it is a TEST-ONLY
parity reference — the live scalar is the dispatcher path above.)

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
`pass_rate_monotonicity` is `true`, the default). The *granularity* is
selected by `pass_rate_monotonicity_scope` (`"per_entry"` | `"aggregate"`,
default `"per_entry"`):

- **`per_entry`** (default, back-compatible) — for every entry where the
  parent recorded `pass_fail == True`, the child MUST also record
  `pass_fail == True`. A child whose `pass_fail` comes back `False` *or*
  `None` (the expectation no longer evaluated, or the entry did not run)
  on a previously-passing entry is a regression. If any such entry
  regressed the gate rejects with `"pass-rate regression on entries:
  <id>, <id>, ..."` (every regressing entry id, sorted). Entries the
  parent failed or had no expectation for are not gated on this rule.
- **`aggregate`** — reject only when the child's *overall* pass-rate falls
  below the parent's by more than a tiny float-noise tolerance
  (`PASS_RATE_MONOTONICITY_TOLERANCE`). The child may trade *which*
  entries pass as long as the net pass-rate holds or improves. The reject
  reason reports the overall rate: `"pass-rate regression: overall
  pass-rate fell by <Δ> (champion <p> -> challenger <c>)"`.

There is no `"off"` scope value — disable the rule entirely with
`pass_rate_monotonicity: false`. The same scope is applied to the
holdout-confirmation step, so the train and holdout slices use one
consistent policy.

**Choosing a scope.** `per_entry` is the right policy when *every* board
entry is a must-not-regress invariant — a regression suite where any flip
is a real breakage. `aggregate` is the right policy for *sampled
evaluation boards*, where each entry is one noisy sample of a capability:
individual pass/fail is subject to run-to-run nondeterminism (sampling,
retrieval ties, timeouts), and a strictly-better challenger should not be
permanently vetoed by a single entry flip when the aggregate — the thing
the operator actually optimizes — improved or held. Under `per_entry`,
the champion's exact passing *set* becomes a frozen invariant and any
nondeterministic entry turns into a ratchet that no amount of aggregate
improvement can overcome; `aggregate` trades that ratchet for a net-rate
guard.

**Rule 3 — per-namespace monotonicity** (roadmap surface; see the §2
note). For each namespace whose flag in `namespace_monotonicity` is
`true`, the child's weighted per-namespace aggregate may not have moved
in the namespace's "worse" direction (the sign of the `namespace_weights`
coefficient already folds direction into the aggregate). A regression
rejects with `"monotonicity_regression on namespace=<ns>, ..."`. Zero-
weight namespaces are skipped. The shipped defaults guard `rubric:` and
`schema:` and leave `drift:` unguarded.

If no rule rejects, the gate returns `decision="promoted"`.

Rule 3's "did this namespace move the wrong way" test is
`tournament.gate.regressed_namespaces`, and it is public because it is
asked outside the gate too: the gate-rule view renders it, and the Pareto
frontier record ([`PARETO-FRONTIER.md`](PARETO-FRONTIER.md)) uses it as
admission control. There is exactly one implementation, so the record and
the gate cannot disagree about what a regression is.

A weighted sum is a projection, so a challenger that wins on an
under-weighted axis loses the scalar and is dropped. That candidate is now
*recorded* — never re-decided — beside the champion lineage; see
[`PARETO-FRONTIER.md`](PARETO-FRONTIER.md). The record reads the same
axes, signs, units, and `promote_margin` this document defines, and adds
no knob of its own.

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

The per-entry-vs-aggregate granularity is operator-selectable via
`pass_rate_monotonicity_scope` (see Rule 2). The *namespace* monotonicity
rule (Rule 3) is already aggregate-scoped — it compares per-namespace
*means*, not per-entry pass/fail — so the same scope field does not apply
there. The analogous knob for namespaces would be "all tracked namespaces
combined vs each individually", a different axis the operator already
controls by choosing which namespaces to flag in `namespace_monotonicity`.
A combined-axis namespace scope is a **documented follow-up**, deliberately
not built alongside the pass-rate scope to avoid conflating two distinct
concepts under one field.

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
- **Cost-aware scoring.** Token counts and per-call cost are now
  carried (`LossProfile.tokens_spent` surfaces under the `cost:`
  namespace), and a cost-aware penalty no longer needs a core edit — a
  `scalar_fn` plugin reading `ctx.namespace_aggregates["cost:"]`
  expresses it (§11.5). What remains roadmap is folding cost into the
  *default* scalar shape; this is forced by **target 2** (see
  [DOGFOOD-TARGETS.md](DOGFOOD-TARGETS.md)).
- **Operator pinning.** "This entry must pass" or "the score must
  improve on this tag slice" as hard gates are not in v0. The
  proposer brief's `## Forbidden` list covers the mutation-side of
  pinning; the scoring side is implicit through pass-rate
  monotonicity.

These are roadmap items, not contract failures. The v0 score is
intentionally narrow.

## 11. Pluggable scoring — transforms and plugins (issue #19)

The linear weights above are the neutral default; they cannot express a
non-linear *shape* (a quadratic recall curve, a diminishing-returns
aggregation, a cap, a cost-aware blend). Historically every such shape
leaked into core as a bespoke `ScoringWeights` field plus a formula edit
(`pass_exponent`), or — worse — an *unconditional* core special-case
that changed scoring for every operator (the harmonic `looping_reasoning`
edit). Issue #19 gives scoring the **operator-owned, contract-referenced
plugin** treatment `predicates.py` / `judges.py` already have, at two
**seams**:

```
per-run events ──(Seam 1: drift_reducer)──▶ per-run drift_loss   # reducer.py
        │  (aggregate: means, namespace rollups — mechanical)
        ▼
per-gen aggregates ──(Seam 2: scalar_fn)──▶ per-gen scalar       # tournament/scoring.py
        ▼
(parent, child) ──(gate)──▶ decision                             # gate.py (§5)
```

Both seams are reshaped by a **hybrid**: a declarative transform
registry for the common 90% (no operator code, serializable) plus a
dotted-spec plugin escape hatch for arbitrary logic. Every layer is
**neutral by default** — absent any new config, scoring is byte-identical
to §4.

### 11.1 Declarative transform registry

`zicato.scoring.transforms` ships a handful of named, pure, parameterized
shapes, each a single `{"op": "<name>", ...params}` spec:

| `op` | params | shape |
|---|---|---|
| `linear` | — | identity (the neutral default) |
| `pow` | `exponent` | `x ** exponent` (the replacement for the retired `pass_exponent`) |
| `harmonic` | — | `1 + 1/2 + … + 1/n` (diminishing returns; the opt-in `looping_reasoning` curve) |
| `cap` | `max` | `min(x, max)` |
| `clip` | `lo`, `hi` | clamp to `[lo, hi]` (requires `lo <= hi`) |
| `log1p` | — | `log(1 + x)` |

Two `ScoringWeights` slots take a transform:

```json
"pass_transform":  { "op": "pow", "exponent": 2.0 },
"drift_kind_aggregation": {
    "looping_reasoning": { "op": "harmonic" },
    "off_topic":         { "op": "cap", "max": 5 }
}
```

- **`pass_transform`** (Seam 2) reshapes the scalar's pass/miss term
  `(1 - mean_score)`. `{"op":"pow","exponent":2.0}` reproduces the
  retired `pass_exponent=2` quadratic-recall behaviour. Absent /
  `linear` is today's plain linear miss term.
- **`drift_kind_aggregation`** (Seam 1) reshapes, per drift KIND, how
  that kind's *count* aggregates into the drift loss
  (`severity × kind_weight × transform(count)` in place of
  `… × count`). An absent kind entry is `linear`, i.e. today's built-in.
  `{"looping_reasoning":{"op":"harmonic"}}` opts THIS contract — and no
  other — into the harmonic curve.

A single `op` per slot — no pipelines (arbitrary multi-step logic is a
plugin). Specs are **validated fail-fast at contract load**
(`ScoringWeights.__post_init__` → `validate_transform_spec`): an unknown
op, a missing / non-finite / non-numeric param, a typo'd param name, or a
`clip` with `lo > hi` is rejected loudly, so `apply_transform` is total
at scoring time and never produces a `NaN` mid-run. Both slots serialize
natively and fold into the contract hash (§2.4).

### 11.2 Dotted-spec plugins (the escape hatch)

For anything the registry can't express (an F-beta recall/precision
blend, a cost-aware penalty reading `ctx.namespace_aggregates["cost:"]`),
two optional dotted specs on the contract, resolved by the **same
importer** predicates / judges use:

```json
"drift_reducer": "mypkg.contract.scoring:my_drift_reducer",   // Seam 1
"scalar_fn":     "mypkg.contract.scoring:my_scalar"           // Seam 2
```

Each is a **pure, deterministic, NO-LLM, no-I/O, no-wall-clock** function
over a read-only typed **frozen context** (`zicato.scoring.api`:
`DriftContext` / `ScalarContext`). Each context carries the **built-in /
post-transform value** (`builtin_loss` / `builtin_scalar`), so a plugin
*wraps/adjusts* the declarative shape rather than reimplementing the
formula:

```python
def my_drift_reducer(ctx: DriftContext) -> float:
    loop = sum(c.count for c in ctx.drift_counts if c.kind == "looping_reasoning")
    base = ctx.builtin_loss - _linear_looping(ctx)
    return base + sum(1.0 / k for k in range(1, int(loop) + 1))
```

The composition order at each seam is **built-in → transform → plugin**:
the plugin sees the post-transform value as its `builtin_*`. Because
scoring is pure, re-scoring an epoch is reproducible — unlike judges,
there is no auxiliary callable to pass.

**Fail-open semantics.** A plugin that raises, returns `NaN`/`inf`, or
fails to resolve must NOT crash the run. Mirroring `evaluate_judges`, the
dispatcher wraps the call in try/except, logs at WARNING, and **falls
back to the pre-plugin (transformed-or-builtin) value** — and records the
fallback in the provenance (§11.4) so a silently-degraded plugin is
visible, not buried in a log.

**Proposer immutability.** Scoring plugins live in the operator package
(`mypkg/contract/scoring.py`), exactly like predicates / judges, and are
**never** enumerated as mutation points — the proposer does not get to
rewrite the operator's grading. A guard/test keeps the mutation walker
off them.

**Worker ↔ orchestrator parity.** Seam 1 (`drift_reducer`) runs INSIDE
the killable worker subprocess; Seam 2 (`scalar_fn`) in the orchestrator.
Both resolve through one shared importer, and `drift_reducer` (like
`drift_kind_aggregation`) crosses the worker `_weights_spec` boundary so
the worker and orchestrator never disagree on which plugin is active.

### 11.3 Seam architecture (`zicato/scoring/`)

| module | role |
|---|---|
| `api.py` | the frozen typed contexts (`DriftContext` / `ScalarContext`) + the `ScoringProvenance` token type |
| `builtins.py` | the extracted default formulas (`builtin_drift_loss` / `builtin_scalar`) — the value every layer starts from |
| `transforms.py` | the declarative registry (`linear`/`pow`/`harmonic`/`cap`/`clip`/`log1p`) + `validate_transform_spec` |
| `plugins.py` | dotted-spec resolution, source-hashing (`spec_with_source_hash`), and the fail-open `apply_drift_reducer` / `apply_scalar_fn` |
| `dispatch.py` | the single seam the live paths call (`resolve_drift_loss` / `resolve_scalar`) — composes built-in → transform → plugin and emits the provenance |

`reducer.py` (Seam 1) and `tournament/scoring.py` (Seam 2) no longer
inline their formulas: each builds the typed context and hands it to the
matching dispatcher.

### 11.4 Scoring provenance

So a scalar is **explainable without reading code**, each dispatcher
returns a parseable provenance token alongside the value. It is recorded
on the per-run `loss.json` (`LossProfile.scoring_provenance`, Seam 1) and
the per-generation aggregate (`scalar_provenance` in `gen_score.json`,
Seam 2), and surfaced in the dashboard's promote-gate breakdown as a
per-side **scalar decomposition** (which transform / plugin produced the
pass term + each drift component). Token shapes:

| token | meaning |
|---|---|
| `builtin` | the default formula produced it (also: `None` on a pre-#19 run) |
| `transform:pass=pow(2.0)` | Seam-2 pass transform |
| `transform:drift{looping_reasoning=harmonic, off_topic=cap(5)}` | Seam-1 per-kind drift transforms |
| `plugin:scalar_fn=<spec>` / `plugin:drift_reducer=<spec>` | a dotted plugin produced it |
| `<pre-plugin token> (fallback: <reason>)` | **FAIL-OPEN** — a fired plugin failed and fell back to the pre-plugin value |

The fail-open form is surfaced **prominently** (caution-colored) in the
dashboard so a degraded plugin is obvious, never silent.

### 11.5 Worked migration

| change | before #19 | under #19 |
|---|---|---|
| quadratic recall | `pass_exponent` field + `scoring.py` edit | `"pass_transform": {"op":"pow","exponent":2.0}` (no core edit) |
| harmonic looping | unconditional core special-case (all operators) | `"drift_kind_aggregation": {"looping_reasoning":{"op":"harmonic"}}` (opt-in, this contract) |
| F-beta blend | new field + formula | `scalar_fn` plugin, zero core change |
| cost-aware penalty | new field + formula | `scalar_fn` plugin reading `ctx.namespace_aggregates["cost:"]` |

## 10. Cross-references

| Topic | Document |
|---|---|
| `LossProfile` fields and how they're computed | [TELEMETRY.md](TELEMETRY.md) |
| `BoardEntry.weight`, `expectations`, and `judges` | [BOARD-FORMAT.md](BOARD-FORMAT.md) |
| Authoring outcome/process checks and `per_judge_weights` | [BOARD-AUTHORING.md](BOARD-AUTHORING.md) |
| `tournament_decision` field on `experiment.json` | [EPOCHS-AND-JOURNALING.md](EPOCHS-AND-JOURNALING.md) |
| Target 2's non-drift loss model | [DOGFOOD-TARGETS.md](DOGFOOD-TARGETS.md) |
| Why drift loss + pass-rate and not free-text scoring | [RATIONALE.md](RATIONALE.md) |
